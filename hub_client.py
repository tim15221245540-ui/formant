# -*- coding: utf-8 -*-
"""Talk to the maintainer's Telegram hub. TTS stays on this PC."""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from appdir import resolve as appdata_dir
from emotion import (
    infer_tts_language,
    prepare_speech,
    tts_model_for_language,
)

HERE = Path(__file__).resolve().parent
APPDATA = appdata_dir(HERE)
SESSION_FILE = APPDATA / "hub_session.json"


def _post(url: str, payload: dict, timeout: float = 40) -> dict:
    raw = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data if isinstance(data, dict) else {}


def _load_session() -> dict:
    if not SESSION_FILE.exists():
        return {}
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _save_session(data: dict) -> None:
    APPDATA.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def hub_status() -> dict:
    data = _load_session()
    return {
        "hub_url": str(data.get("hub_url") or ""),
        "session": bool(data.get("session")),
        "code": str(data.get("code") or ""),
        "bot_username": str(data.get("bot_username") or ""),
        "paired": not bool(data.get("code")) and bool(data.get("session")),
    }


def pair(hub_url: str) -> dict:
    base = hub_url.rstrip("/")
    info = _post(base + "/register", {}, timeout=15)
    session = str(info.get("session") or "")
    if not session:
        raise RuntimeError(str(info.get("error") or "Hub did not return a session."))
    out = {
        "hub_url": base,
        "session": session,
        "code": str(info.get("code") or ""),
        "bot_username": str(info.get("bot_username") or ""),
    }
    _save_session(out)
    return out


def _speak(text: str) -> tuple[bytes, str]:
    from emotion import explain_emotion_error, explain_translate_error, translate_dialogue
    from formant import engine, load_settings

    settings = load_settings()
    caption = ""
    if settings.get("translateCaptions"):
        translated = translate_dialogue(text, settings.get("translateStyle"))
        caption = translated.text or ""
        if not caption and translated.error:
            caption = explain_translate_error(translated.error, "en") or translated.error
    language = infer_tts_language(text, "en")
    wanted = tts_model_for_language(language)
    try:
        engine.set_model(wanted)
    except Exception as e:
        print(f"hub client set_model: {e}", flush=True)
    audio, prepared = engine.tts(
        {
            "text": text,
            "language": language,
            "output_format": "opus",
        }
    )
    if getattr(prepared, "error", "") and not caption:
        caption = explain_emotion_error(prepared.error, "en")
    return audio, caption


def run(stop: dict | None = None) -> None:
    stop = stop if stop is not None else {"off": False}
    while not stop.get("off"):
        data = _load_session()
        base = str(data.get("hub_url") or "").rstrip("/")
        session = str(data.get("session") or "")
        if not base or not session:
            time.sleep(2)
            continue
        try:
            payload = _post(base + "/wait", {"session": session}, timeout=40)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                print("hub session expired; pair again", flush=True)
                leftover = {"hub_url": base, "session": "", "code": "", "bot_username": data.get("bot_username") or ""}
                _save_session(leftover)
            else:
                print(f"hub wait http {e.code}", flush=True)
            time.sleep(2)
            continue
        except Exception as e:
            print(f"hub wait: {e}", flush=True)
            time.sleep(2)
            continue
        for job in payload.get("jobs") or []:
            job_id = str(job.get("id") or "")
            text = str(job.get("text") or "")
            result: dict = {"session": session, "job_id": job_id, "ok": False, "error": "", "audio_b64": "", "caption": ""}
            try:
                audio, caption = _speak(text)
                result["ok"] = True
                result["audio_b64"] = base64.b64encode(audio).decode("ascii")
                result["caption"] = caption
            except Exception as e:
                result["error"] = str(e)
                print(f"hub job failed: {e}", flush=True)
            try:
                _post(base + "/result", result, timeout=60)
            except Exception as e:
                print(f"hub result post: {e}", flush=True)
