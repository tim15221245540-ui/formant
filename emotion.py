# -*- coding: utf-8 -*-
"""Insert official Chatterbox Turbo tags via a cloud LLM (default: DeepSeek)."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from appdir import resolve as appdata_dir

HERE = Path(__file__).resolve().parent
APPDATA = appdata_dir(HERE)

ALLOWED_TAGS = (
    "[laugh]",
    "[chuckle]",
    "[sigh]",
    "[gasp]",
    "[cough]",
    "[clear throat]",
    "[sniff]",
    "[groan]",
    "[shush]",
)
ALLOWED_SET = {t.lower() for t in ALLOWED_TAGS}
TAG_RE = re.compile(r"\[[^\[\]]+\]")

PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-flash",
        "env": "DEEPSEEK_API_KEY",
        "file": "deepseek_key.txt",
        "thinking_off": True,
    },
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "deepseek-ai/DeepSeek-V4-Flash",
        "env": "SILICONFLOW_API_KEY",
        "file": "siliconflow_key.txt",
        "thinking_off": False,
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "env": "GROQ_API_KEY",
        "file": "groq_key.txt",
        "thinking_off": False,
    },
    "xai": {
        "base_url": "https://api.x.ai/v1",
        "model": "grok-4.3",
        "env": "XAI_API_KEY",
        "file": "xai_key.txt",
        "thinking_off": False,
    },
}

SYSTEM_PROMPT = """You insert Chatterbox Turbo emotion tags into the user's text for TTS.

Allowed tags, copy exactly:
[laugh] [chuckle] [sigh] [gasp] [cough] [clear throat] [sniff] [groan] [shush]

The input is a full scene: narration, actions, and spoken lines. Keep all of it. Do not extract dialogue. Do not start at the first spoken line if anything comes before it.

Your only job: decide where those tags belong from the meaning and situation, then insert them.

Be generous. Insert a tag at every emotional turn, reaction, breath, or shift in tone — in narration as well as speech. Short lines can still take a tag. Prefer more fitting tags over leaving a stretch of the scene flat.

Hard rules:
- Copy the original text EXACTLY, including the opening narration before anyone speaks.
- Do not add, delete, paraphrase, translate, or rearrange any word, punctuation, asterisk, or parenthesis.
- The ONLY allowed change is inserting the tags above.
- The input usually has no (laughs) or *sighs*. Infer from context anyway.
- Return only the tagged text. No markdown, no explanation, no extra wrapping quotes around the whole reply. Keep every quote that was already in the input.
- Never use any other [bracket] tag.
"""

_TRANSLATE_BASE = """你是中文译者。把用户发来的英文对白译成中文。

硬性规则（所有风格共用）：
- 情节、人物、尺度原样保留，不删、不改成含蓄，也不要用网文套话加料。
- 只返回译文。不要引号包裹全文，不要注释，不要英文原文。
"""

_REWRITE_PROMPT = """你是中文译者。把用户发来的英文对白用中文重写。

硬性规则：
- 情节、人物、尺度原样保留，不删、不改成含蓄，也不要用网文套话加料。
- 只返回中文。不要引号包裹全文，不要注释，不要英文原文。
"""

_EDITORIAL_POLISH = """你是一位资深中文小说编辑。请校对和润色用户发来的中文段落。

任务包括：
- 修正所有语法
- 识别并修改表达不通顺、读起来别扭的句子
- 确保用词准确，消除歧义
- 情节、人物、尺度原样保留，不删、不改成含蓄，也不要加料

直接给出修改后的版本。不要注释，不要对照，不要说明改了什么。
"""

_WRITERLY_POLISH = """你现在是一位文学作家。请将这段段落变得更加生动、更具有画面感。

- 专注于替换平淡的动词和形容词，使用更具表现力的词汇
- 适当使用比喻、拟人、排比等修辞手法
- 创造更强的节奏感和韵律感
- 这是再创作：允许改写措辞和意象。不要把人物、情节走向或尺度改成另一回事，也不要改成含蓄

直接给出改写后的版本。不要注释，不要对照，不要说明改了什么。
"""

TRANSLATE_STYLES = {
    "spare": {
        "label": "Spare",
        "blurb": "Tight written Chinese. Few adverbials.",
        "prompt": _TRANSLATE_BASE
        + """
风格：克制书面。
- 文艺、书面，允许适度艺术化，不要说明书式的英译中，不要机翻腔。
- 少用「地」字状语，避免「轻轻地走过来」「缓缓地开口」「充满情欲地看着」这种堆砌。
- 用动作、名词、短句推进，不要旁白解释情绪。
""",
    },
    "academic": {
        "label": "Academic",
        "blurb": "Idiomatic Chinese, not English syntax. Cut dummy 的/而/并/且, split long modifiers, prefer active voice.",
        "prompt": _TRANSLATE_BASE
        + """
风格：Academic。去翻译腔，按汉语意合来写。
- 别把英文形合搬进中文：少用「的 / 而 / 并 / 且」硬接
- 长定语拆开，不要 A的B的C
- 被动尽量改主动
- 抽象名词别当主语（「他收入的减少改变了……」→「他收入少了，日子也变了」）
- 连接词能省就省
""",
    },
    "editorial": {
        "label": "Editorial",
        "blurb": "Plain Chinese rewrite, then a senior fiction editor fixes grammar and awkward lines. Sense stays put.",
        "prompt": _REWRITE_PROMPT,
        "polish": _EDITORIAL_POLISH,
        "polish_temperature": 0.35,
    },
    "writerly": {
        "label": "Writerly",
        "blurb": "Plain Chinese rewrite, then a literary pass for stronger verbs, images, and rhythm. This style changes the original — the special case.",
        "prompt": _REWRITE_PROMPT,
        "polish": _WRITERLY_POLISH,
        "polish_temperature": 0.85,
    },
}
DEFAULT_TRANSLATE_STYLE = "spare"
TRANSLATE_PROMPT = TRANSLATE_STYLES[DEFAULT_TRANSLATE_STYLE]["prompt"]


def normalize_translate_style(name: str | None) -> str:
    key = str(name or "").strip().lower()
    return key if key in TRANSLATE_STYLES else DEFAULT_TRANSLATE_STYLE


def translate_style_prompt(style: str | None = None) -> str:
    return TRANSLATE_STYLES[normalize_translate_style(style)]["prompt"]

MODERATION_RE = re.compile(
    r"(content exists risk|content.?filter|content.?policy|against my guidelines|"
    r"as an ai|i (cannot|can't) (assist|help|comply)|i'?m unable to|"
    r"作为人工智能|无法协助|不能协助|内容.?违规|触发.?审查)",
    re.I,
)


@dataclass
class TagResult:
    text: str
    tagged: bool
    error: str = ""  # "", missing_key, balance, quota, moderation, failed, no_tags


@dataclass
class TranslateResult:
    text: str
    error: str = ""  # "", missing_key, balance, quota, moderation, failed


def _read_json(path: Path, fallback):
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(fallback)
    return {**fallback, **data} if isinstance(data, dict) else dict(fallback)


def load_emotion_config() -> dict:
    cfg = {"emotion_provider": "deepseek", "emotion_model": ""}
    cfg = {**cfg, **_read_json(HERE / "config.json", {})}
    cfg = {**cfg, **_read_json(APPDATA / "config.json", {})}
    name = str(cfg.get("emotion_provider") or "deepseek").strip().lower()
    if name not in PROVIDERS:
        name = "deepseek"
    spec = PROVIDERS[name]
    model = str(cfg.get("emotion_model") or "").strip() or spec["model"]
    return {"name": name, **spec, "model": model}


def provider_id() -> str:
    return load_emotion_config()["name"]


def _key_from_file(name: str) -> str:
    for folder in (APPDATA, HERE):
        p = folder / name
        if p.exists():
            t = p.read_text(encoding="utf-8").strip()
            if t:
                return t
    return ""


def load_api_key(cfg: dict | None = None) -> str:
    cfg = cfg or load_emotion_config()
    env = os.environ.get(cfg["env"], "").strip()
    if env:
        return env
    return _key_from_file(cfg["file"])


def save_api_key(provider: str, key: str) -> None:
    name = str(provider or "deepseek").strip().lower()
    if name not in PROVIDERS:
        name = "deepseek"
    APPDATA.mkdir(parents=True, exist_ok=True)
    path = APPDATA / PROVIDERS[name]["file"]
    key = (key or "").strip()
    if key:
        path.write_text(key, encoding="utf-8")
    elif path.exists():
        path.unlink()


def has_api_key() -> bool:
    return bool(load_api_key())


_HAN_RE = re.compile(r"[\u3400-\u9fff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_HANG_RE = re.compile(r"[\uac00-\ud7af]")
_CYR_RE = re.compile(r"[\u0400-\u04ff]")
_ARAB_RE = re.compile(r"[\u0600-\u06ff]")
_HEB_RE = re.compile(r"[\u0590-\u05ff]")
_EL_RE = re.compile(r"[\u0370-\u03ff]")
_DEV_RE = re.compile(r"[\u0900-\u097f]")
_LATIN_RE = re.compile(r"[A-Za-zÀ-ÿĀ-ž]")

TURBO_MODEL = "chatterbox-turbo"
MULTILINGUAL_MODEL = "chatterbox-multilingual"


def _script_ratio(pattern: re.Pattern, text: str) -> float:
    if not text:
        return 0.0
    return len(pattern.findall(text)) / max(len(text), 1)


def infer_tts_language(text: str, fallback: str = "en") -> str:
    """Guess Chatterbox language_id. English → en; anything else → a multilingual code."""
    t = text or ""
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"\[[^\]]+\]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return "en"

    if _script_ratio(_HANG_RE, t) > 0.12:
        return "ko"
    if _script_ratio(_KANA_RE, t) > 0.06:
        return "ja"
    if _script_ratio(_HAN_RE, t) > 0.15:
        return "zh"
    if _script_ratio(_CYR_RE, t) > 0.15:
        return "ru"
    if _script_ratio(_ARAB_RE, t) > 0.15:
        return "ar"
    if _script_ratio(_HEB_RE, t) > 0.15:
        return "he"
    if _script_ratio(_EL_RE, t) > 0.15:
        return "el"
    if _script_ratio(_DEV_RE, t) > 0.15:
        return "hi"

    words = set(re.findall(r"[A-Za-zÀ-ÿ']+", t.lower()))
    word_hits = {
        "en": len(words & {"the", "and", "you", "that", "this", "with", "have", "what", "your"}),
        "fr": len(words & {"les", "une", "des", "est", "que", "pour", "dans", "bonjour", "ça", "avec"}),
        "es": len(words & {"hola", "que", "qué", "los", "las", "una", "por", "como", "está", "gracias"}),
        "de": len(words & {"und", "der", "die", "das", "ich", "nicht", "ein", "ist", "sie"}),
        "it": len(words & {"ciao", "che", "una", "per", "sono", "non", "grazie"}),
        "pt": len(words & {"não", "uma", "para", "você", "está", "obrigado"}),
    }
    best_words = max(word_hits, key=lambda k: word_hits[k])
    if best_words != "en" and word_hits[best_words] >= 1 and word_hits[best_words] >= word_hits["en"]:
        return best_words
    if word_hits["en"] >= 2:
        return "en"

    marks = {
        "de": len(re.findall(r"[ÄÖÜäöüß]", t)),
        "es": len(re.findall(r"[Ññ¿¡]", t)),
        "pt": len(re.findall(r"[ÃÕãõ]", t)),
        "fr": len(re.findall(r"[ÀÂÆÇÈÉÊËÎÏÔŒÙÛÜŸàâæçèéêëîïôœùûüÿ]", t)),
        "it": len(re.findall(r"[ÌÒÙìòù]", t)),
        "sv": len(re.findall(r"[Åå]", t)),
        "da": len(re.findall(r"[ÆØæø]", t)),
        "tr": len(re.findall(r"[ĞğİıŞş]", t)),
        "pl": len(re.findall(r"[ĄĆĘŁŃŚŹŻąćęłńśźż]", t)),
    }
    if "¿" in t or "¡" in t:
        return "es"
    if "ß" in t:
        return "de"
    best_mark = max(marks, key=lambda k: marks[k])
    if marks[best_mark] >= 2:
        return best_mark
    if marks[best_mark] >= 1 and word_hits["en"] == 0:
        return best_mark

    if _LATIN_RE.search(t):
        return "en"
    fb = str(fallback or "en").strip().lower()
    return fb if fb else "en"


def tts_model_for_language(lang: str) -> str:
    return TURBO_MODEL if str(lang or "en").strip().lower() == "en" else MULTILINGUAL_MODEL


def clean_speech_text(text: str, emotion_tags: bool = False) -> str:
    t = text or ""
    if not emotion_tags:
        t = re.sub(r"\([^)]*\)", " ", t)
        t = t.replace("*", "")
    return re.sub(r"\s+", " ", t).strip()


def prepare_speech(raw: str, emotion_tags: bool) -> TagResult:
    """Text for TTS. Emotion on: tag the original; off: strip *markup* and (asides)."""
    raw = (raw or "").strip()
    if not raw:
        return TagResult(text="", tagged=False, error="")
    if not emotion_tags:
        return TagResult(text=clean_speech_text(raw, False), tagged=False, error="")
    tagged = tag_dialogue(raw)
    if tagged.tagged:
        return tagged
    return TagResult(text=clean_speech_text(raw, False), tagged=False, error=tagged.error)


def explain_emotion_error(error: str, lang: str = "zh") -> str:
    if not error:
        return ""
    cfg = load_emotion_config()
    name = cfg["name"]
    keyfile = cfg["file"]
    if lang == "en":
        notes = {
            "missing_key": f"Emotion tags need an API key ({keyfile}). Spoken without tags.",
            "balance": f"{name} balance is empty. Spoken without tags.",
            "quota": "Emotion tag quota is used up. Spoken without tags.",
            "moderation": f"{name} blocked this line. Spoken without tags.",
            "failed": f"{name} failed to tag this line. Spoken without tags.",
            "no_tags": "No emotion tags were inserted. Spoken without tags.",
        }
    else:
        notes = {
            "missing_key": f"情绪标签需要 API key。把 {name} 的 key 放到 %APPDATA%\\Formant\\{keyfile}。这次按普通朗读。",
            "balance": f"{name} 余额不足，请充值。这次按普通朗读。",
            "quota": "情绪标签额度用完了。这次按原文合成。",
            "moderation": f"{name} 审查拦住了这条。这次按原文合成。",
            "failed": f"{name} 报错。这次按原文合成。",
            "no_tags": "这次没插上情绪标签，按普通朗读。",
        }
    return notes.get(error, notes["failed"])


def explain_translate_error(error: str, lang: str = "zh") -> str:
    if not error:
        return ""
    cfg = load_emotion_config()
    name = cfg["name"]
    keyfile = cfg["file"]
    if lang == "en":
        notes = {
            "missing_key": f"Translation needs an API key ({keyfile}).",
            "balance": f"{name} balance is empty. No Chinese caption this time.",
            "quota": "Translation quota is used up. No Chinese caption this time.",
            "moderation": "This caption was blocked.",
            "failed": "Caption was not generated. The voice is unchanged.",
        }
    else:
        notes = {
            "missing_key": f"译文需要 API key。把 {name} 的 key 放到 %APPDATA%\\Formant\\{keyfile}。",
            "balance": f"{name} 余额不足，这条不附中文。",
            "quota": "译文额度用完了，这条不附中文。",
            "moderation": "这条译文被拦住了。",
            "failed": "译文没生成出来，语音不受影响。",
        }
    return notes.get(error, notes["failed"])


def _has_allowed_tag(text: str) -> bool:
    return any(m.group(0).lower() in ALLOWED_SET for m in TAG_RE.finditer(text or ""))


def _strip_unknown_tags(text: str) -> str:
    def keep(m: re.Match) -> str:
        token = m.group(0)
        return token if token.lower() in ALLOWED_SET else ""

    return TAG_RE.sub(keep, text or "")


def _unwrap(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[^\n]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t)
        t = t.strip()
    if (t.startswith('"') and t.endswith('"') and t.count('"') == 2) or (
        t.startswith("'") and t.endswith("'") and t.count("'") == 2
    ):
        t = t[1:-1].strip()
    return t


def _classify_http(code: int, body: str) -> str:
    low = (body or "").lower()
    if MODERATION_RE.search(body or "") or "content exists risk" in low:
        return "moderation"
    if code in (401, 403) or "authentication" in low or "invalid api" in low:
        return "missing_key"
    if code == 402 or "insufficient" in low or "balance" in low or "arrears" in low or "额度" in body or "余额" in body:
        return "balance"
    if code == 429 or "rate" in low or "quota" in low:
        return "quota"
    return "failed"


def _post_chat(
    cfg: dict,
    key: str,
    user_text: str,
    thinking_off: bool,
    system: str,
    temperature: float = 0.3,
    timeout: float = 40,
) -> tuple[int, dict | str]:
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload: dict = {
        "model": cfg["model"],
        "temperature": temperature,
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
    }
    if thinking_off:
        payload["thinking"] = {"type": "disabled"}
    raw = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")
        if thinking_off and e.code == 400:
            return _post_chat(cfg, key, user_text, False, system, temperature, timeout)
        try:
            return e.code, json.loads(detail)
        except json.JSONDecodeError:
            return e.code, detail
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, str(e)


def _finish_reason(data: dict) -> str:
    try:
        return str(data["choices"][0].get("finish_reason") or "")
    except (KeyError, IndexError, TypeError):
        return ""


def _choice_text(data: dict) -> str:
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in ("text", "output_text"):
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(content or "")


def tag_dialogue(text: str) -> TagResult:
    """Insert official tags via the cloud model. No local rewrite checks."""
    raw = (text or "").strip()
    if not raw:
        return TagResult(text="", tagged=False, error="")

    cfg = load_emotion_config()
    key = load_api_key(cfg)
    if not key:
        return TagResult(text=raw, tagged=False, error="missing_key")

    code, data = _post_chat(
        cfg, key, raw, bool(cfg.get("thinking_off")), SYSTEM_PROMPT, 0.3
    )
    if (
        code == 404
        and cfg["name"] == "deepseek"
        and cfg["model"] == "deepseek-v4-flash"
    ):
        cfg = {**cfg, "model": "deepseek-chat"}
        code, data = _post_chat(cfg, key, raw, False, SYSTEM_PROMPT, 0.3)
    if code != 200:
        body = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        err = _classify_http(code, body)
        print(f"  emotion tag {cfg['name']} http {code} {err}", flush=True)
        return TagResult(text=raw, tagged=False, error=err)

    if not isinstance(data, dict):
        return TagResult(text=raw, tagged=False, error="failed")
    if _finish_reason(data) == "content_filter":
        return TagResult(text=raw, tagged=False, error="moderation")

    out = _unwrap(_choice_text(data))
    if not out:
        print("  emotion tag empty content", flush=True)
        return TagResult(text=raw, tagged=False, error="failed")
    if MODERATION_RE.search(out) and not _has_allowed_tag(out):
        print(f"  emotion tag moderation: {out[:160]}", flush=True)
        return TagResult(text=raw, tagged=False, error="moderation")

    out = _strip_unknown_tags(out) or raw
    tagged = _has_allowed_tag(out)
    print(f"  emotion tagged={tagged}: {out[:240]}", flush=True)
    if not tagged:
        return TagResult(text=raw, tagged=False, error="no_tags")
    return TagResult(text=out, tagged=True, error="")


def _translate_once(
    cfg: dict,
    key: str,
    user_text: str,
    system: str,
    temperature: float,
    stage: str,
) -> TranslateResult:
    thinking = bool(cfg.get("thinking_off"))
    code, data = _post_chat(
        cfg, key, user_text, thinking, system, temperature, 90
    )
    if code == 0:
        print(f"  {stage} retry after: {data}", flush=True)
        code, data = _post_chat(
            cfg, key, user_text, thinking, system, temperature, 90
        )
    if (
        code == 404
        and cfg["name"] == "deepseek"
        and cfg["model"] == "deepseek-v4-flash"
    ):
        cfg["model"] = "deepseek-chat"
        code, data = _post_chat(cfg, key, user_text, False, system, temperature, 90)
    if code != 200:
        body = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        err = _classify_http(code, body)
        print(f"  {stage} {cfg['name']} http {code} {err}: {body[:300]}", flush=True)
        return TranslateResult(text="", error=err)

    if not isinstance(data, dict):
        return TranslateResult(text="", error="failed")
    if _finish_reason(data) == "content_filter":
        print(f"  {stage} content_filter", flush=True)
        return TranslateResult(text="", error="moderation")

    out = _unwrap(_choice_text(data))
    if not out:
        print(f"  {stage} empty content", flush=True)
        return TranslateResult(text="", error="failed")
    if MODERATION_RE.search(out):
        print(f"  {stage} moderation: {out[:160]}", flush=True)
        return TranslateResult(text="", error="moderation")
    print(f"  {stage} ok: {out[:240]}", flush=True)
    return TranslateResult(text=out, error="")


def translate_dialogue(text: str, style: str | None = None) -> TranslateResult:
    """Chinese caption in the chosen style. On any failure, return empty text."""
    raw = (text or "").strip()
    if not raw:
        return TranslateResult(text="", error="")

    cfg = dict(load_emotion_config())
    key = load_api_key(cfg)
    if not key:
        return TranslateResult(text="", error="missing_key")

    spec = TRANSLATE_STYLES[normalize_translate_style(style)]
    draft = _translate_once(
        cfg, key, raw, spec["prompt"], float(spec.get("temperature", 0.7)), "translate"
    )
    polish = spec.get("polish")
    if not polish or draft.error or not draft.text:
        return draft
    return _translate_once(
        cfg,
        key,
        draft.text,
        str(polish),
        float(spec.get("polish_temperature", 0.7)),
        "polish",
    )
