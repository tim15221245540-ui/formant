"""Telegram bot for Formant. Reads live settings."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

import requests
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from emotion import (
    MULTILINGUAL_MODEL,
    TRANSLATE_STYLES,
    TURBO_MODEL,
    clean_speech_text,
    explain_emotion_error,
    explain_translate_error,
    infer_tts_language,
    normalize_translate_style,
    prepare_speech,
    translate_dialogue,
    tts_model_for_language,
)

from appdir import exclusive, resolve as appdata_dir

HERE = Path(__file__).resolve().parent
APPDATA = appdata_dir(HERE)
LOGO_PATH = HERE / "formant.jpg"
START_CAPTION = (
    "<b>Formant</b> | <i>Resonating beyond text.</i>\n"
    "\n"
    "lifeless robots ❌  Formant breathes nuance, tone, and character into every word you send.\n"
    "\n"
    "<b>Core Features:</b>\n"
    "• 🎭 <b>Emotion-First TTS:</b> Expressive audio delivered in seconds. Adding emotion labels automatically!\n"
    "• 🌍 <b>Multilingual voices:</b> Send Chinese or another language and Formant switches models for you. Non-English voices are not as strong as English yet.\n"
    "• 🎧 <b>Dual Delivery:</b> Get the audio note + translated text side by side.\n"
    "• 🎙️ <b>Voice Cloning:</b> Train a custom voice with a single sample with a few clicks.\n"
    "\n"
    "<i>Send any message to start listening.</i>\n"
    "\n"
    "🎙️ /voice — pick a cloned voice\n"
    "🎭 /emotion — emotion tags on/off\n"
    "🌐 /translate — Chinese caption on/off\n"
    "✒️ /style — caption style\n"
    "📡 /status — engine, voice, and switches"
)

_cache: dict[str, str] = {}
_cache_i = 0
_CACHE_MAX = 40
_tts_lock: asyncio.Lock | None = None
ENGINE_WAIT_S = 240
TTS_TIMEOUT_S = 180


def _work_lock() -> asyncio.Lock:
    global _tts_lock
    if _tts_lock is None:
        _tts_lock = asyncio.Lock()
    return _tts_lock


def _chatterbox_up(settings: dict | None = None) -> bool:
    try:
        requests.get(_box_url(settings) + "/", timeout=1)
        return True
    except requests.RequestException:
        return False


def _tts_post(url: str, body: dict) -> tuple[int, bytes, str]:
    r = requests.post(url, json=body, timeout=TTS_TIMEOUT_S)
    err = "" if r.status_code == 200 else (r.text or "")[:400]
    return r.status_code, r.content, err


async def _wait_engine(message) -> bool:
    if await asyncio.to_thread(_chatterbox_up):
        return True
    status = await message.reply_text("Engine is starting. I'll process this when it's ready…")
    deadline = time.time() + ENGINE_WAIT_S
    while time.time() < deadline:
        await asyncio.sleep(1.2)
        if await asyncio.to_thread(_chatterbox_up):
            try:
                await status.edit_text("Engine is ready.")
            except Exception:
                pass
            return True
    try:
        await status.edit_text("Engine is not ready. Click the waves in Formant, wait until it's live, then send again.")
    except Exception:
        try:
            await message.reply_text("Engine is not ready. Click the waves in Formant, then send again.")
        except TelegramError:
            pass
    return False


def load_token() -> str:
    for p in (APPDATA / "token.txt", HERE / "token.txt", Path(os.getcwd()) / "token.txt"):
        if p.exists():
            t = p.read_text(encoding="utf-8").strip()
            if t:
                return t
    env = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if env:
        return env
    raise SystemExit("No Telegram token. Put it in token.txt next to this file.")


def _read_settings_unlocked() -> dict:
    p = APPDATA / "settings.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_settings_unlocked(data: dict) -> None:
    APPDATA.mkdir(parents=True, exist_ok=True)
    (APPDATA / "settings.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load_settings() -> dict:
    with exclusive(APPDATA / "settings.json"):
        return _read_settings_unlocked()


def save_settings(data: dict) -> None:
    with exclusive(APPDATA / "settings.json"):
        _write_settings_unlocked(data)


def _patch_settings(**kwargs) -> dict:
    with exclusive(APPDATA / "settings.json"):
        settings = _read_settings_unlocked()
        settings.update(kwargs)
        _write_settings_unlocked(settings)
        return settings


def _box_url(settings: dict | None = None) -> str:
    settings = settings or load_settings()
    return (settings.get("chatterboxUrl") or "http://127.0.0.1:8004").rstrip("/")


def _loaded_model_type(settings: dict | None = None) -> str:
    try:
        r = requests.get(_box_url(settings) + "/api/model-info", timeout=4)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and data.get("loaded"):
                return str(data.get("type") or "")
    except requests.RequestException:
        pass
    return ""


def _needed_model_type(repo: str) -> str:
    key = str(repo or "").strip().lower()
    if "multilingual" in key:
        return "multilingual"
    if key in {"chatterbox", "original", "resembleai/chatterbox"}:
        return "original"
    return "turbo"


def _remember_tts_route(model: str, language: str) -> None:
    with exclusive(APPDATA / "settings.json"):
        settings = _read_settings_unlocked()
        settings["model"] = model
        params = dict(settings.get("params") or {})
        params["language"] = language
        settings["params"] = params
        _write_settings_unlocked(settings)


def _switch_box_model(wanted: str, settings: dict | None = None) -> str:
    """Load wanted repo on Chatterbox. Empty string means it is ready."""
    wanted = str(wanted or TURBO_MODEL).strip() or TURBO_MODEL
    need = _needed_model_type(wanted)
    if _loaded_model_type(settings) == need:
        return ""
    url = _box_url(settings)
    try:
        r = requests.post(
            url + "/save_settings",
            json={"model": {"repo_id": wanted}},
            timeout=15,
        )
        if r.status_code != 200:
            return (r.text or "Could not save model.")[:300]
        r = requests.post(url + "/restart_server", json={}, timeout=360)
        if r.status_code != 200:
            return (r.text or "Could not reload the model.")[:300]
    except requests.RequestException as e:
        return str(e)
    deadline = time.time() + ENGINE_WAIT_S
    while time.time() < deadline:
        if _loaded_model_type(settings) == need:
            return ""
        time.sleep(1.2)
    return "Model did not finish loading."


def _chatterbox_dir() -> Path:
    cfg_path = HERE / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cfg = {}
    return Path(cfg.get("chatterbox_dir") or r"D:\Chatterbox-TTS-Server-main")


def list_clone_voices() -> list[str]:
    names: list[str] = []
    try:
        r = requests.get(_box_url() + "/get_reference_files", timeout=4)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                names = [str(x) for x in data]
            elif isinstance(data, dict):
                names = [str(x) for x in (data.get("files") or data.get("reference_files") or [])]
    except requests.RequestException:
        pass
    folder = _chatterbox_dir() / "reference_audio"
    if folder.exists():
        for p in sorted(folder.iterdir()):
            if p.suffix.lower() in {".wav", ".mp3", ".ogg", ".flac", ".m4a"} and p.name not in names:
                names.append(p.name)
    return names or ["ellie.wav"]


def _voice_keyboard(current: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for name in list_clone_voices():
        label = Path(name).stem
        if name == current:
            label = f"· {label}"
        row.append(InlineKeyboardButton(label, callback_data=f"voice:{name}"[:64]))
        if len(row) == 2:
            rows.append(row)
            row = []
        if len(rows) >= 12:
            break
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _remember(text: str) -> str:
    global _cache_i
    _cache_i += 1
    key = str(_cache_i)
    _cache[key] = text
    while len(_cache) > _CACHE_MAX:
        oldest = next(iter(_cache))
        _cache.pop(oldest, None)
    return key


def _emotion_hint(error: str) -> str:
    return explain_emotion_error(error, "en")


def _translate_hint(error: str) -> str:
    return explain_translate_error(error, "en")


def _current_style(settings: dict | None = None) -> str:
    settings = settings if settings is not None else load_settings()
    return normalize_translate_style(settings.get("translateStyle"))


def _style_label(style: str | None = None) -> str:
    key = normalize_translate_style(style)
    return str(TRANSLATE_STYLES[key]["label"])


def _style_keyboard(current: str) -> InlineKeyboardMarkup:
    current = normalize_translate_style(current)
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for key, spec in TRANSLATE_STYLES.items():
        label = str(spec["label"])
        if key == current:
            label = f"· {label}"
        row.append(InlineKeyboardButton(label, callback_data=f"style:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _on_off(args: list[str] | None) -> bool | None:
    if not args:
        return None
    token = args[0].strip().lower()
    if token in ("on", "1", "true", "开"):
        return True
    if token in ("off", "0", "false", "关"):
        return False
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if LOGO_PATH.is_file():
        with LOGO_PATH.open("rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption=START_CAPTION,
                parse_mode=ParseMode.HTML,
            )
        return
    await update.message.reply_text(START_CAPTION, parse_mode=ParseMode.HTML)


async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = load_settings()
    current = settings.get("voiceFile") or "ellie.wav"
    args = context.args or []
    if args:
        picked = _match_voice(" ".join(args))
        if not picked:
            await update.message.reply_text("No voice matches that name.")
            return
        _patch_settings(voiceFile=picked, voiceMode="clone")
        await update.message.reply_text(f"Voice: {Path(picked).stem}")
        return
    await update.message.reply_text(
        f"Current: {Path(current).stem}\nTap to switch:",
        reply_markup=_voice_keyboard(current),
    )


def _match_voice(query: str) -> str | None:
    q = query.strip().lower()
    if not q:
        return None
    names = list_clone_voices()
    for n in names:
        if n.lower() == q or Path(n).stem.lower() == q:
            return n
    hits = [n for n in names if q in Path(n).stem.lower() or q in n.lower()]
    if len(hits) == 1:
        return hits[0]
    return None


async def cmd_emotion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flag = _on_off(context.args)
    settings = load_settings()
    if flag is None:
        on = bool(settings.get("emotionTags"))
        await update.message.reply_text(
            f"Emotion tags are {'on' if on else 'off'}.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("On", callback_data="emotion:on"),
                        InlineKeyboardButton("Off", callback_data="emotion:off"),
                    ]
                ]
            ),
        )
        return
    _patch_settings(emotionTags=flag)
    await update.message.reply_text(f"Emotion tags turned {'on' if flag else 'off'}.")


async def cmd_translate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flag = _on_off(context.args)
    settings = load_settings()
    if flag is None:
        on = bool(settings.get("translateCaptions"))
        style = _style_label(_current_style(settings))
        await update.message.reply_text(
            f"Chinese captions are {'on' if on else 'off'}. Style: {style}.\n"
            "When on, a caption is sent before each voice note. /style to change the voice of the Chinese.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("On", callback_data="tr:on"),
                        InlineKeyboardButton("Off", callback_data="tr:off"),
                    ]
                ]
            ),
        )
        return
    _patch_settings(translateCaptions=flag)
    await update.message.reply_text(f"Chinese captions turned {'on' if flag else 'off'}.")


async def cmd_style(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = load_settings()
    current = _current_style(settings)
    args = context.args or []
    if args:
        q = args[0].strip().lower()
        picked = ""
        if q in TRANSLATE_STYLES:
            picked = q
        else:
            hits = [k for k, spec in TRANSLATE_STYLES.items() if str(spec["label"]).lower() == q]
            if len(hits) == 1:
                picked = hits[0]
        if not picked:
            names = ", ".join(str(spec["label"]) for spec in TRANSLATE_STYLES.values())
            await update.message.reply_text(f"Unknown style. Use: {names}")
            return
        _patch_settings(translateStyle=picked)
        spec = TRANSLATE_STYLES[picked]
        await update.message.reply_text(f"Caption style: {spec['label']}\n{spec['blurb']}")
        return
    spec = TRANSLATE_STYLES[current]
    await update.message.reply_text(
        f"Caption style: {spec['label']}\n{spec['blurb']}\nTap to switch:",
        reply_markup=_style_keyboard(current),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = load_settings()
    voice = Path(settings.get("voiceFile") or "ellie.wav").stem
    emotion = "on" if settings.get("emotionTags") else "off"
    trans = "on" if settings.get("translateCaptions") else "off"
    if await asyncio.to_thread(_chatterbox_up, settings):
        engine = "ready"
    else:
        engine = "not ready (starting or off)"
    style = _style_label(_current_style(settings))
    model = str(settings.get("model") or TURBO_MODEL)
    lang = str((settings.get("params") or {}).get("language") or "en")
    await update.message.reply_text(
        f"Engine: {engine}\nVoice: {voice}\nModel: {model}\nLanguage: {lang}\n"
        f"Emotion tags: {emotion}\n"
        f"Chinese captions: {trans}\nCaption style: {style}"
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data.startswith("voice:"):
        name = data.split(":", 1)[1]
        _patch_settings(voiceFile=name, voiceMode="clone")
        try:
            await q.edit_message_text(f"Voice: {Path(name).stem}")
        except Exception:
            if q.message:
                await q.message.reply_text(f"Voice: {Path(name).stem}")
        return
    if data == "emotion:on":
        _patch_settings(emotionTags=True)
        await q.edit_message_text("Emotion tags turned on.")
        return
    if data == "emotion:off":
        _patch_settings(emotionTags=False)
        await q.edit_message_text("Emotion tags turned off.")
        return
    if data == "tr:on":
        _patch_settings(translateCaptions=True)
        await q.edit_message_text("Chinese captions turned on.")
        return
    if data == "tr:off":
        _patch_settings(translateCaptions=False)
        await q.edit_message_text("Chinese captions turned off.")
        return
    if data.startswith("style:"):
        picked = normalize_translate_style(data.split(":", 1)[1])
        _patch_settings(translateStyle=picked)
        spec = TRANSLATE_STYLES[picked]
        try:
            await q.edit_message_text(
                f"Caption style: {spec['label']}\n{spec['blurb']}",
                reply_markup=_style_keyboard(picked),
            )
        except Exception:
            if q.message:
                await q.message.reply_text(f"Caption style: {spec['label']}\n{spec['blurb']}")
        return
    if data.startswith("zh:"):
        key = data.split(":", 1)[1]
        src = _cache.get(key, "")
        target = q.message
        if target is None or not hasattr(target, "reply_text"):
            return
        if not src:
            await target.reply_text("That line expired. Send it again.")
            return
        await _send_translation(target, src)


async def _reply_chunks(message, text: str) -> None:
    t = text or ""
    while t:
        chunk, t = t[:4096], t[4096:]
        await message.reply_text(chunk)


async def _send_translation(message, src: str) -> None:
    style = _current_style()
    result = await asyncio.to_thread(translate_dialogue, src, style)
    try:
        if result.text:
            await _reply_chunks(message, result.text)
            return
        hint = _translate_hint(result.error)
        if hint:
            await message.reply_text(hint)
    except TelegramError as e:
        print(f"translate send failed: {e}", flush=True)


async def _send_voice_file(message, content: bytes, markup) -> None:
    fd, tmp = tempfile.mkstemp(prefix="formant_", suffix=".ogg")
    os.close(fd)
    path = Path(tmp)
    try:
        path.write_bytes(content)
        with path.open("rb") as f:
            await message.reply_voice(voice=f, reply_markup=markup)
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


async def talk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = load_settings()
    want_emotion = bool(settings.get("emotionTags", False))
    captions = bool(settings.get("translateCaptions", False))
    raw = update.message.text or ""
    language = infer_tts_language(raw, "en")
    wanted_model = tts_model_for_language(language)
    multilingual = wanted_model == MULTILINGUAL_MODEL
    emotion = want_emotion and not multilingual
    text = clean_speech_text(raw, emotion)
    if not text:
        await update.message.reply_text("Nothing to speak.")
        return

    if not await _wait_engine(update.message):
        return

    params = settings.get("params") or {}
    voice_mode = settings.get("voiceMode") or "clone"
    voice_file = settings.get("voiceFile") or "ellie.wav"
    url = _box_url(settings) + "/tts"

    lock = _work_lock()
    queued = lock.locked()
    wait_msg = None
    if queued:
        wait_msg = await update.message.reply_text("Previous message is still processing. This one is queued.")

    async with lock:
        if wait_msg is not None:
            try:
                await wait_msg.delete()
            except Exception:
                pass

        # Captions stay BEFORE voice. Timeouts stay long (translate 90s+retry,
        # TTS 180s). Do not shorten; do not swap this order. Off = no caption.
        if captions:
            await _send_translation(update.message, raw)

        status = None
        need = _needed_model_type(wanted_model)
        loaded = await asyncio.to_thread(_loaded_model_type, settings)
        if loaded != need:
            swap_note = (
                "Switching to multilingual… Non-English voices are not as strong as English yet."
                if multilingual
                else "Switching back to Turbo…"
            )
            status = await update.message.reply_text(swap_note)
            err = await asyncio.to_thread(_switch_box_model, wanted_model, settings)
            if err:
                try:
                    await status.edit_text("Could not switch the voice model:\n" + err)
                except Exception:
                    await update.message.reply_text("Could not switch the voice model:\n" + err)
                return
        await asyncio.to_thread(_remember_tts_route, wanted_model, language)

        if emotion:
            if status is None:
                status = await update.message.reply_text("Adding emotion tags…")
            else:
                try:
                    await status.edit_text("Adding emotion tags…")
                except Exception:
                    status = await update.message.reply_text("Adding emotion tags…")
            prepared = await asyncio.to_thread(prepare_speech, raw, True)
            text = prepared.text or text
            print(f"emotion applied={prepared.tagged} error={prepared.error or '-'}", flush=True)
            hint = _emotion_hint(prepared.error)
            if hint:
                await update.message.reply_text(hint)
            try:
                await status.edit_text("Synthesizing…")
            except Exception:
                await update.message.reply_text("Synthesizing…")
        else:
            if status is None:
                await update.message.reply_text("Synthesizing…")
            else:
                try:
                    await status.edit_text("Synthesizing…")
                except Exception:
                    await update.message.reply_text("Synthesizing…")
        body = {
            "text": text,
            "voice_mode": voice_mode,
            "output_format": "opus",
            "split_text": bool(params.get("splitText", True)),
            "chunk_size": int(params.get("chunkSize") or 240),
            "temperature": params.get("temperature", 0.8),
            "exaggeration": params.get("exaggeration", 1.3),
            "cfg_weight": params.get("cfgWeight", 0.5),
            "speed_factor": params.get("speedFactor", 1),
            "seed": int(params.get("seed") or 0),
            "language": language,
        }
        if voice_mode == "clone":
            body["reference_audio_filename"] = voice_file
        else:
            body["predefined_voice_id"] = voice_file

        try:
            code, content, err = await asyncio.to_thread(_tts_post, url, body)
        except requests.RequestException as e:
            up = await asyncio.to_thread(_chatterbox_up)
            if up:
                await update.message.reply_text(f"Can't reach Chatterbox: {e}")
            else:
                await update.message.reply_text("Engine is still starting or disconnected. Try again in a moment.")
            return
        if code != 200:
            await update.message.reply_text("Chatterbox returned an error:\n" + err)
            return

        cache_id = _remember(raw)
        markup = None
        if not captions:
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Translate", callback_data=f"zh:{cache_id}")]]
            )
        try:
            await _send_voice_file(update.message, content, markup)
        except TelegramError as e:
            print(f"voice send failed: {e}", flush=True)
            try:
                await update.message.reply_text(f"Voice note didn't send: {e}")
            except TelegramError:
                pass


async def on_startup(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "about Formant"),
            BotCommand("voice", "pick a cloned voice"),
            BotCommand("emotion", "emotion tags on/off"),
            BotCommand("translate", "Chinese captions on/off"),
            BotCommand("style", "caption style"),
            BotCommand("status", "engine, voice, and switches"),
        ]
    )


def main() -> None:
    token = load_token()
    send_http = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=120.0,
        write_timeout=120.0,
        pool_timeout=20.0,
        media_write_timeout=120.0,
    )
    poll_http = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=20.0,
    )
    app = (
        Application.builder()
        .token(token)
        .request(send_http)
        .get_updates_request(poll_http)
        .post_init(on_startup)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("voice", cmd_voice))
    app.add_handler(CommandHandler("emotion", cmd_emotion))
    app.add_handler(CommandHandler("translate", cmd_translate))
    app.add_handler(CommandHandler("style", cmd_style))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, talk))
    print("Formant bot 1.6 ready", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()
