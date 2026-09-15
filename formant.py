# -*- coding: utf-8 -*-
"""Formant desktop host. Double-click Formant.bat."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from appdir import exclusive, resolve as appdata_dir
from emotion import (
    PROVIDERS,
    explain_emotion_error,
    has_api_key,
    infer_tts_language,
    prepare_speech,
    provider_id,
    save_api_key,
    tag_dialogue,
)

HERE = Path(__file__).resolve().parent
UI_DIR = HERE / "ui"
APPDATA = appdata_dir(HERE)
HOST = "127.0.0.1"
PORT = 8765
VERSION = "1.6"
CREATE_NO_WINDOW = 0x08000000

DEFAULT_CONFIG = {
    "chatterbox_dir": "",
    "bot_dir": "",
    "chatterbox_url": "http://127.0.0.1:8004",
    "ytdlp": "",
    "ffmpeg": "",
    "clip_dir": "",
    "emotion_provider": "deepseek",
    "emotion_model": "",
}

DEFAULT_SETTINGS = {
    "emotionTags": False,
    "translateCaptions": False,
    "translateStyle": "spare",
    "tgBot": False,
    "voiceMode": "clone",
    "voiceFile": "",
    "model": "chatterbox-turbo",
    "params": {
        "temperature": 0.8,
        "exaggeration": 1.3,
        "cfgWeight": 0.5,
        "speedFactor": 1,
        "seed": 2025,
        "language": "en",
        "outputFormat": "wav",
        "splitText": True,
        "chunkSize": 240,
    },
}


def app_paths() -> None:
    APPDATA.mkdir(parents=True, exist_ok=True)


def read_json(path: Path, fallback):
    if not path.exists():
        return json.loads(json.dumps(fallback))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return json.loads(json.dumps(fallback))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def config_path() -> Path:
    return APPDATA / "config.json"


def load_config() -> dict:
    return {**DEFAULT_CONFIG, **read_json(config_path(), {})}


def save_config(cfg: dict) -> None:
    write_json(config_path(), cfg)


def has_telegram_token() -> bool:
    for folder in (APPDATA, HERE):
        p = folder / "token.txt"
        if p.exists() and p.read_text(encoding="utf-8").strip():
            return True
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip())


def save_telegram_token(token: str) -> None:
    APPDATA.mkdir(parents=True, exist_ok=True)
    path = APPDATA / "token.txt"
    token = (token or "").strip()
    if token:
        path.write_text(token, encoding="utf-8")
    elif path.exists():
        path.unlink()


def chatterbox_ready(cfg: dict | None = None) -> bool:
    cfg = cfg or load_config()
    root = Path(str(cfg.get("chatterbox_dir") or "").strip())
    return bool(root) and (root / "server.py").is_file()


def setup_snapshot() -> dict:
    cfg = load_config()
    return {
        "chatterbox_dir": str(cfg.get("chatterbox_dir") or ""),
        "chatterbox_url": str(cfg.get("chatterbox_url") or "http://127.0.0.1:8004"),
        "chatterbox_ready": chatterbox_ready(cfg),
        "emotion_provider": provider_id(),
        "has_api_key": has_api_key(),
        "has_telegram_token": has_telegram_token(),
        "ytdlp": str(cfg.get("ytdlp") or ""),
        "ffmpeg": str(cfg.get("ffmpeg") or ""),
        "clip_dir": str(cfg.get("clip_dir") or ""),
        "needs_setup": not chatterbox_ready(cfg),
        "providers": list(PROVIDERS.keys()),
    }


def _load_settings_unlocked() -> dict:
    data = {**DEFAULT_SETTINGS, **read_json(APPDATA / "settings.json", {})}
    data.setdefault("params", dict(DEFAULT_SETTINGS["params"]))
    return data


def _save_settings_unlocked(data: dict) -> None:
    cfg = load_config()
    data["chatterboxUrl"] = cfg["chatterbox_url"]
    write_json(APPDATA / "settings.json", data)


def load_settings() -> dict:
    with exclusive(APPDATA / "settings.json"):
        return _load_settings_unlocked()


def save_settings(data: dict) -> None:
    with exclusive(APPDATA / "settings.json"):
        _save_settings_unlocked(data)


@contextmanager
def settings_txn():
    with exclusive(APPDATA / "settings.json"):
        data = _load_settings_unlocked()
        yield data
        _save_settings_unlocked(data)


def parse_multipart(raw: bytes, content_type: str) -> tuple[dict[str, str], dict[str, dict]]:
    """Split multipart/form-data into text fields and file parts."""
    fields: dict[str, str] = {}
    files: dict[str, dict] = {}
    m = re.search(r"boundary=([^;]+)", content_type or "", re.I)
    if not m or not raw:
        return fields, files
    boundary = m.group(1).strip().strip('"').encode("latin1")
    delim = b"--" + boundary
    for chunk in raw.split(delim):
        if not chunk or chunk.startswith(b"--"):
            continue
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        header_raw, sep, body = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers = header_raw.decode("latin1", errors="ignore")
        name_m = re.search(r'name="([^"]+)"', headers)
        if not name_m:
            continue
        name = name_m.group(1)
        fn_m = re.search(r'filename="([^"]+)"', headers)
        if fn_m:
            files[name] = {"filename": Path(fn_m.group(1)).name, "data": body}
        else:
            fields[name] = body.decode("utf-8", errors="replace")
    return fields, files


def migrate_token(bot_dir: Path) -> None:
    dest = APPDATA / "token.txt"
    if dest.exists() and dest.read_text(encoding="utf-8").strip():
        return
    for candidate in (bot_dir / "token.txt", bot_dir / "bot.py"):
        if not candidate.exists():
            continue
        text = candidate.read_text(encoding="utf-8", errors="ignore")
        if candidate.name == "token.txt":
            token = text.strip()
        else:
            m = re.search(r'TOKEN\s*=\s*"([^"]+)"', text)
            token = m.group(1) if m else ""
        if token:
            dest.write_text(token, encoding="utf-8")
            return


def box_request(url: str, data: bytes | None = None, headers: dict | None = None, timeout: float = 8):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    return urllib.request.urlopen(req, timeout=timeout)


def parse_timestamp(text: str) -> float:
    raw = (text or "").strip().replace("，", ":").replace(" ", "")
    if not raw:
        raise ValueError("Need a start and end time.")
    if re.fullmatch(r"\d+(\.\d+)?", raw):
        return float(raw)
    parts = raw.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError as e:
        raise ValueError(f"Bad time: {text}") from e
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    raise ValueError(f"Bad time: {text}")


def format_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds - hours * 3600 - minutes * 60
    if abs(secs - round(secs)) < 1e-6:
        tail = f"{int(round(secs)):02d}"
    else:
        tail = f"{secs:06.3f}".rstrip("0").rstrip(".")
    if hours:
        return f"{hours}:{minutes:02d}:{tail}"
    return f"{minutes}:{tail}"


def safe_clip_name(name: str) -> str:
    stem = Path(name or "clip").name
    stem = re.sub(r"\.(wav|mp3|ogg|m4a|webm)$", "", stem, flags=re.I)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "clip"
    return stem[:80]


def resolve_tool(cfg: dict, key: str, fallbacks: list[str]) -> Path:
    ordered = [str(cfg.get(key) or "").strip(), *fallbacks]
    for raw in ordered:
        if not raw:
            continue
        p = Path(raw)
        if p.is_file():
            return p
        if p.is_dir():
            exe = p / ("yt-dlp.exe" if key == "ytdlp" else "ffmpeg.exe")
            if exe.is_file():
                return exe
    raise FileNotFoundError(f"No {key} executable. Set it in config.json.")


def ffmpeg_works(exe: Path) -> bool:
    if not exe.is_file():
        return False
    flags = CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        r = subprocess.run(
            [str(exe), "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            creationflags=flags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def resolve_ffmpeg(cfg: dict) -> Path:
    home = Path.home()
    candidates = [
        str(cfg.get("ffmpeg") or "").strip(),
        str(home / r"AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"),
        r"D:\yt\ffmpeg.exe",
    ]
    gyan = home / r"AppData\Local\Microsoft\WinGet\Packages"
    if gyan.is_dir():
        for p in gyan.glob("Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe"):
            candidates.append(str(p))
    seen: set[str] = set()
    for raw in candidates:
        if not raw or raw.lower() in seen:
            continue
        seen.add(raw.lower())
        p = Path(raw)
        exe = p / "ffmpeg.exe" if p.is_dir() else p
        if ffmpeg_works(exe):
            try:
                return exe.resolve()
            except OSError:
                return exe
    raise FileNotFoundError(
        "No working ffmpeg. Install Gyan FFmpeg or set the ffmpeg path in Setup."
    )


def ffmpeg_location(exe: Path) -> str:
    return str(exe.parent)


def clip_env(ffmpeg: Path) -> dict:
    env = os.environ.copy()
    bindir = str(ffmpeg.parent)
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    return env


def clean_media_url(url: str) -> str:
    raw = (url or "").strip()
    parts = urllib.parse.urlsplit(raw)
    if not parts.query:
        return raw
    q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    q = [(k, v) for k, v in q if k.lower() not in {"t", "start", "time_continue"}]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(q), parts.fragment))


class Clipper:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = "idle"
        self.log = ""
        self.error = ""
        self.file = ""
        self.imported = ""
        self.folder = ""
        self.proc: subprocess.Popen | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "state": self.state,
                "log": self.log,
                "error": self.error,
                "file": self.file,
                "imported": self.imported,
                "folder": self.folder,
            }

    def _append(self, line: str) -> None:
        text = (line or "").rstrip()
        if not text:
            return
        with self.lock:
            self.log = (self.log + text + "\n")[-8000:]

    def start(self, url: str, start: str, end: str, filename: str, import_voice: bool) -> dict:
        start_s = parse_timestamp(start)
        end_s = parse_timestamp(end)
        if end_s <= start_s:
            raise ValueError("End must be after start.")
        if end_s - start_s > 600:
            raise ValueError("Clip is longer than 10 minutes.")
        url = (url or "").strip()
        if not re.search(r"https?://", url, re.I):
            raise ValueError("Paste a YouTube link.")
        with self.lock:
            if self.state == "running":
                raise RuntimeError("A clip is already running.")
            self.state = "running"
            self.log = ""
            self.error = ""
            self.file = ""
            self.imported = ""
            self.folder = ""
        threading.Thread(
            target=self._run,
            args=(url, start, end, filename, import_voice),
            daemon=True,
        ).start()
        return self.snapshot()

    def _run(self, url: str, start: str, end: str, filename: str, import_voice: bool) -> None:
        try:
            start_s = parse_timestamp(start)
            end_s = parse_timestamp(end)
            if end_s <= start_s:
                raise ValueError("End must be after start.")
            if end_s - start_s > 600:
                raise ValueError("Clip is longer than 10 minutes.")
            url = clean_media_url(url)
            if not re.search(r"https?://", url, re.I):
                raise ValueError("Paste a YouTube link.")
            cfg = load_config()
            ytdlp = resolve_tool(
                cfg,
                "ytdlp",
                [r"D:\yt\yt-dlp.exe"],
            )
            ffmpeg = resolve_ffmpeg(cfg)
            ff_loc = ffmpeg_location(ffmpeg)
            env = clip_env(ffmpeg)
            self._append(f"ffmpeg: {ffmpeg}")
            stem = safe_clip_name(filename)
            work = Path(cfg.get("clip_dir") or r"D:\yt")
            work.mkdir(parents=True, exist_ok=True)
            section = f"*{format_timestamp(start_s)}-{format_timestamp(end_s)}"
            out_template = str(work / f"{stem}.%(ext)s")
            cmd = [
                str(ytdlp),
                "--ffmpeg-location",
                ff_loc,
                "--extractor-args",
                "youtube:player_client=android",
                "-x",
                "--audio-format",
                "wav",
                "--force-keyframes-at-cuts",
                "--download-sections",
                section,
                "--no-playlist",
                "--force-overwrites",
                "--newline",
                "-o",
                out_template,
                url,
            ]
            self._append(" ".join(cmd))
            wav = self._exec(cmd, work, stem, env=env, raise_on_fail=False)
            if wav is None:
                self._append("Section cut failed, downloading then trimming with ffmpeg.")
                raw_stem = f"{stem}_full"
                raw_cmd = [
                    str(ytdlp),
                    "--ffmpeg-location",
                    ff_loc,
                    "--extractor-args",
                    "youtube:player_client=android",
                    "-x",
                    "--audio-format",
                    "wav",
                    "--no-playlist",
                    "--force-overwrites",
                    "--newline",
                    "-o",
                    str(work / f"{raw_stem}.%(ext)s"),
                    url,
                ]
                raw = self._exec(raw_cmd, work, raw_stem, env=env)
                if raw is None:
                    raise RuntimeError("yt-dlp did not produce a wav.")
                wav = work / f"{stem}.wav"
                trim = [
                    str(ffmpeg),
                    "-y",
                    "-ss",
                    format_timestamp(start_s),
                    "-to",
                    format_timestamp(end_s),
                    "-i",
                    str(raw),
                    "-acodec",
                    "pcm_s16le",
                    str(wav),
                ]
                self._append(" ".join(trim))
                self._exec(trim, work, stem, env=env, require_wav=False)
                if not wav.exists() or wav.stat().st_size < 1000:
                    raise RuntimeError("ffmpeg did not write a wav.")
                try:
                    raw.unlink()
                except OSError:
                    pass
            imported = ""
            if import_voice:
                try:
                    imported = engine.save_voice(wav.name, wav.read_bytes(), "clone", select=False)
                    self._append(f"Saved to reference_audio as {imported}")
                except Exception as e:
                    self._append(f"Wav exported, but could not copy to reference_audio: {e}")
            with self.lock:
                self.state = "done"
                self.file = str(wav)
                self.imported = imported
                self.folder = str(wav.parent)
                self.error = ""
        except Exception as e:
            with self.lock:
                self.state = "error"
                self.error = str(e)
            self._append(str(e))

    def _exec(
        self,
        cmd: list[str],
        work: Path,
        stem: str,
        require_wav: bool = True,
        env: dict | None = None,
        raise_on_fail: bool = True,
    ) -> Path | None:
        flags = CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(work),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._append(line)
        code = self.proc.wait()
        self.proc = None
        wav = self._find_wav(work, stem)
        if code != 0 and wav is None:
            if raise_on_fail:
                raise RuntimeError(f"Command failed ({code}).")
            return None
        if require_wav:
            return wav
        return wav

    def _find_wav(self, folder: Path, stem: str) -> Path | None:
        matches = []
        for p in folder.glob(stem + "*"):
            if p.is_file() and p.suffix.lower() == ".wav":
                matches.append(p)
        if not matches:
            return None
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return matches[0]

    def reveal(self) -> None:
        with self.lock:
            folder = self.folder or str(load_config().get("clip_dir") or r"D:\yt")
        path = Path(folder)
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(path))  # noqa: S606

    def audio_path(self) -> Path | None:
        with self.lock:
            file = self.file
        p = Path(file) if file else None
        if p and p.is_file():
            return p
        return None


clipper = Clipper()


class Engine:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = "idle"
        self.error = ""
        self.bot_on = False
        self.bot_proc: subprocess.Popen | None = None
        self.box_proc: subprocess.Popen | None = None

    def _box_python(self, root: Path) -> Path | None:
        embedded = root / "python_embedded" / "python.exe"
        if embedded.exists():
            return embedded
        venv = root / "venv" / "Scripts" / "python.exe"
        if venv.exists():
            return venv
        return None

    def cfg(self) -> dict:
        return load_config()

    def box_url(self) -> str:
        return self.cfg()["chatterbox_url"].rstrip("/")

    def chatterbox_up(self) -> bool:
        try:
            urllib.request.urlopen(self.box_url() + "/api/model-info", timeout=0.6)
            return True
        except Exception:
            return False

    def start_engine(self) -> None:
        with self.lock:
            if self.chatterbox_up():
                self.state = "live"
                self.error = ""
                return
            if self.state == "awakening":
                return
            self.state = "awakening"
            self.error = ""
        threading.Thread(target=self._boot, daemon=True).start()

    def _boot(self) -> None:
        cfg = self.cfg()
        root_s = str(cfg.get("chatterbox_dir") or "").strip()
        if not root_s:
            with self.lock:
                self.state = "idle"
                self.error = "Set your Chatterbox folder in Setup first."
            return
        root = Path(root_s)
        py = self._box_python(root)
        wrapper = HERE / "silent_box.py"
        server = root / "server.py"
        if py and wrapper.exists() and server.exists():
            try:
                APPDATA.mkdir(parents=True, exist_ok=True)
                log = open(APPDATA / "chatterbox.log", "ab")
                flags = CREATE_NO_WINDOW if os.name == "nt" else 0
                self.box_proc = subprocess.Popen(
                    [str(py), str(wrapper), str(root)],
                    cwd=str(root),
                    stdout=log,
                    stderr=log,
                    creationflags=flags,
                )
            except OSError as e:
                with self.lock:
                    self.state = "idle"
                    self.error = str(e)
                return
        else:
            bat = root / "start.bat"
            if not bat.exists():
                with self.lock:
                    self.state = "idle"
                    self.error = f"No Chatterbox server in {root}"
                return
            try:
                os.startfile(str(bat))
            except OSError as e:
                with self.lock:
                    self.state = "idle"
                    self.error = str(e)
                return
        deadline = time.time() + 240
        while time.time() < deadline:
            if self.chatterbox_up():
                with self.lock:
                    self.state = "live"
                    self.error = ""
                return
            if self.box_proc and self.box_proc.poll() is not None:
                with self.lock:
                    self.state = "idle"
                    self.error = "Chatterbox exited. See %APPDATA%\\Formant\\chatterbox.log"
                return
            time.sleep(1.2)
        with self.lock:
            self.state = "idle"
            self.error = "Chatterbox did not come up. See %APPDATA%\\Formant\\chatterbox.log"

    def stop_engine(self) -> None:
        self.stop_bot()
        cfg = self.cfg()
        url = urlparse(cfg["chatterbox_url"])
        port = url.port or 8004
        if os.name == "nt":
            for pid in pids_on_port(port):
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                )
        proc = None
        with self.lock:
            proc = self.box_proc
            self.box_proc = None
            self.state = "idle"
            self.error = ""
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def start_bot(self) -> None:
        self.start_engine()
        self.stop_bot()
        with settings_txn() as settings:
            settings["tgBot"] = True
        cfg = self.cfg()
        bot_dir = Path(cfg["bot_dir"])
        migrate_token(bot_dir)
        script = HERE / "bot_formant.py"
        flags = CREATE_NO_WINDOW if os.name == "nt" else 0
        log = open(APPDATA / "bot.log", "ab")
        self.bot_proc = subprocess.Popen(
            [sys.executable, str(script)],
            cwd=str(HERE),
            stdout=log,
            stderr=log,
            creationflags=flags,
        )
        with self.lock:
            self.bot_on = True

    def stop_bot(self) -> None:
        with settings_txn() as settings:
            settings["tgBot"] = False
        if self.bot_proc and self.bot_proc.poll() is None:
            self.bot_proc.terminate()
            try:
                self.bot_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.bot_proc.kill()
        self.bot_proc = None
        kill_bot_scripts()
        with self.lock:
            self.bot_on = False

    def box_json(self, path: str, timeout: float = 6):
        try:
            with urllib.request.urlopen(self.box_url() + path, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return None

    def list_voices(self) -> list[dict]:
        settings = load_settings()
        current = settings.get("voiceFile") or "ellie.wav"
        mode = settings.get("voiceMode") or "clone"
        voices: list[dict] = []

        refs = self.box_json("/get_reference_files")
        names = []
        if isinstance(refs, list):
            names = [str(x) for x in refs]
        elif isinstance(refs, dict):
            names = [str(x) for x in (refs.get("files") or refs.get("reference_files") or [])]
        folder = Path(self.cfg()["chatterbox_dir"]) / "reference_audio"
        if folder.exists():
            for p in sorted(folder.iterdir()):
                if p.suffix.lower() in {".wav", ".mp3", ".ogg", ".flac", ".m4a"} and p.name not in names:
                    names.append(p.name)
        if not names:
            names = ["ellie.wav"]
        for n in names:
            voices.append(
                {
                    "id": n,
                    "name": Path(n).stem,
                    "filename": n,
                    "kind": "clone",
                    "hasAudio": True,
                    "selected": mode == "clone" and n == current,
                }
            )

        predefined = self.box_json("/get_predefined_voices")
        pre_names = []
        if isinstance(predefined, list):
            pre_names = [str(x.get("filename") or x.get("name") or x) if isinstance(x, dict) else str(x) for x in predefined]
        elif isinstance(predefined, dict):
            files = predefined.get("voices") or predefined.get("files") or []
            pre_names = [str(x.get("filename") or x) if isinstance(x, dict) else str(x) for x in files]
        voices_dir = Path(self.cfg()["chatterbox_dir"]) / "voices"
        if voices_dir.exists():
            for p in sorted(voices_dir.iterdir()):
                if p.suffix.lower() in {".wav", ".mp3"} and p.name not in pre_names:
                    pre_names.append(p.name)
        if not pre_names:
            pre_names = ["Emily.wav", "Abigail.wav"]
        for n in pre_names:
            voices.append(
                {
                    "id": n,
                    "name": Path(n).stem,
                    "filename": n,
                    "kind": "predefined",
                    "hasAudio": True,
                    "selected": mode == "predefined" and n == current,
                }
            )
        return voices

    def save_voice(self, filename: str, data: bytes, kind: str, select: bool = True) -> str:
        safe = Path(filename).name
        if not re.search(r"\.(wav|mp3|ogg|flac|m4a)$", safe, re.I):
            raise ValueError("Need a wav or mp3 file.")
        cfg = self.cfg()
        folder = Path(cfg["chatterbox_dir"]) / ("reference_audio" if kind == "clone" else "voices")
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / safe
        dest.write_bytes(data)
        if select:
            mode = "clone" if kind == "clone" else "predefined"
            with settings_txn() as settings:
                settings["voiceFile"] = safe
                settings["voiceMode"] = mode
        if self.chatterbox_up():
            endpoint = "/upload_reference" if kind == "clone" else "/upload_predefined_voice"
            boundary = "----FormantBoundary"
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{safe}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
            try:
                box_request(
                    self.box_url() + endpoint,
                    data=body,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                    timeout=30,
                )
            except Exception:
                pass
        return safe

    def delete_voice(self, filename: str, kind: str) -> None:
        safe = Path(filename).name
        if not safe or safe in {".", ".."} or "/" in safe or "\\" in safe:
            raise ValueError("Need a voice filename.")
        kind = "clone" if kind == "clone" else "predefined"
        cfg = self.cfg()
        folder = Path(cfg["chatterbox_dir"]) / ("reference_audio" if kind == "clone" else "voices")
        dest = folder / safe
        if dest.is_file():
            dest.unlink()
        remaining = []
        if folder.exists():
            remaining = sorted(
                p.name
                for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in {".wav", ".mp3", ".ogg", ".flac", ".m4a"}
            )
        with settings_txn() as settings:
            if settings.get("voiceFile") == safe:
                fallback = remaining[0] if remaining else ("ellie.wav" if kind == "clone" else "Emily.wav")
                settings["voiceFile"] = fallback

    def set_model(self, model: str) -> None:
        wanted = str(model or "chatterbox-turbo").strip() or "chatterbox-turbo"
        previous = str(load_settings().get("model") or "chatterbox-turbo")
        with self.lock:
            self.state = "awakening"
            self.error = ""
        with settings_txn() as settings:
            settings["model"] = wanted
        if not self.chatterbox_up():
            with settings_txn() as settings:
                settings["model"] = previous
            with self.lock:
                self.state = "idle"
                self.error = "Chatterbox is not running."
            return
        try:
            self._box_set_repo(wanted)
            box_request(
                self.box_url() + "/restart_server",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                timeout=360,
            )
            with self.lock:
                self.state = "live"
                self.error = ""
        except Exception as e:
            with settings_txn() as settings:
                settings["model"] = previous
            try:
                if previous != wanted:
                    self._box_set_repo(previous)
                    box_request(
                        self.box_url() + "/restart_server",
                        data=b"{}",
                        headers={"Content-Type": "application/json"},
                        timeout=360,
                    )
            except Exception:
                pass
            live = self.chatterbox_up()
            with self.lock:
                self.state = "live" if live else "idle"
                self.error = f"Could not load {wanted}. Reverted to {previous}. {e}"

    def _box_set_repo(self, model: str) -> None:
        payload = json.dumps({"model": {"repo_id": model}}).encode("utf-8")
        box_request(
            self.box_url() + "/save_settings",
            data=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

    def recycle_box(self) -> bool:
        """Relaunch Chatterbox without stopping the Telegram bot. CUDA asserts need this."""
        cfg = self.cfg()
        url = urlparse(cfg["chatterbox_url"])
        port = url.port or 8004
        if os.name == "nt":
            for pid in pids_on_port(port):
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                )
        proc = None
        with self.lock:
            proc = self.box_proc
            self.box_proc = None
            self.state = "idle"
            self.error = ""
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.start_engine()
        deadline = time.time() + 240
        while time.time() < deadline:
            if self.chatterbox_up():
                info = self.box_json("/api/model-info")
                if isinstance(info, dict) and info.get("loaded"):
                    with self.lock:
                        self.state = "live"
                        self.error = ""
                    return True
            time.sleep(1.2)
        return False

    def tts(self, payload: dict) -> tuple[bytes, object]:
        settings = load_settings()
        params = settings.get("params") or {}
        multilingual = "multilingual" in str(settings.get("model") or "")
        use_emotion = bool(settings.get("emotionTags")) and not multilingual
        prepared = prepare_speech(str(payload.get("text") or ""), use_emotion)
        text = prepared.text
        language = str(payload.get("language", params.get("language", "en")) or "en")
        if multilingual:
            language = infer_tts_language(text, language)
        body = {
            "text": text,
            "temperature": payload.get("temperature", params.get("temperature", 0.8)),
            "exaggeration": payload.get("exaggeration", params.get("exaggeration", 1.3)),
            "cfg_weight": payload.get("cfg_weight", params.get("cfgWeight", 0.5)),
            "speed_factor": payload.get("speed_factor", params.get("speedFactor", 1)),
            "seed": payload.get("seed", params.get("seed", 0)),
            "language": language,
            "voice_mode": payload.get("voice_mode") or settings.get("voiceMode") or "clone",
            "split_text": payload.get("split_text", params.get("splitText", True)),
            "chunk_size": payload.get("chunk_size", params.get("chunkSize", 240)),
            "output_format": payload.get("output_format") or params.get("outputFormat") or "wav",
        }
        voice = (
            payload.get("reference_audio_filename")
            or payload.get("predefined_voice_id")
            or settings.get("voiceFile")
            or "ellie.wav"
        )
        if body["voice_mode"] == "clone":
            body["reference_audio_filename"] = voice
        else:
            body["predefined_voice_id"] = voice
        req = urllib.request.Request(
            self.box_url() + "/tts",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                audio = r.read()
            return audio, prepared
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")
            if (
                e.code == 500
                and "failed to synthesize" in detail.lower()
                and not payload.get("_retried")
            ):
                print("  TTS failed; recycling Chatterbox after CUDA/engine error", flush=True)
                if self.recycle_box():
                    payload = dict(payload)
                    payload["_retried"] = True
                    return self.tts(payload)
            raise RuntimeError(detail or str(e)) from e

    def snapshot(self) -> dict:
        settings = load_settings()
        live = self.chatterbox_up()
        info = self.box_json("/api/model-info") if live else None
        loaded = bool(isinstance(info, dict) and info.get("loaded"))
        type_to_model = {
            "turbo": "chatterbox-turbo",
            "multilingual": "chatterbox-multilingual",
            "original": "chatterbox",
        }
        actual = type_to_model.get(str((info or {}).get("type") or "")) if isinstance(info, dict) else ""
        with self.lock:
            if self.bot_proc is not None and self.bot_proc.poll() is not None:
                self.bot_on = False
                self.bot_proc = None
            state = self.state
            err = self.error
            bot = self.bot_on
        if live and state != "awakening":
            state = "live"
            if loaded:
                err = ""
        elif not live and state in ("live", "awakening"):
            state = "idle"
        saved = settings.get("model") or "chatterbox-turbo"
        return {
            "engine": state,
            "error": err,
            "tgBot": bot,
            "emotionTags": bool(settings.get("emotionTags")),
            "translateCaptions": bool(settings.get("translateCaptions")),
            "emotionReady": has_api_key(),
            "emotionProvider": provider_id(),
            "params": settings.get("params") or DEFAULT_SETTINGS["params"],
            "model": actual or saved,
            "modelLoaded": loaded,
            "voiceMode": settings.get("voiceMode") or "clone",
            "voiceFile": settings.get("voiceFile") or "ellie.wav",
            "voices": self.list_voices(),
            "config": self.cfg(),
            "setup": setup_snapshot(),
        }


def kill_bot_scripts() -> None:
    """Kill leftover Telegram bot processes so only one copy polls the token."""
    if os.name != "nt":
        return
    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.CommandLine -match 'bot_formant\\.py|tg-tts\\\\bot\\.py' } | "
                "Select-Object -ExpandProperty ProcessId",
            ],
            text=True,
            errors="replace",
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.CalledProcessError):
        return
    me = os.getpid()
    for pid in out.split():
        if not pid.isdigit():
            continue
        n = int(pid)
        if n == me:
            continue
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(n)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )


def pids_on_port(port: int) -> set[int]:
    pids: set[int] = set()
    try:
        out = subprocess.check_output(["netstat", "-ano", "-p", "tcp"], text=True, errors="ignore")
    except (OSError, subprocess.CalledProcessError):
        return pids
    for line in out.splitlines():
        if f":{port} " not in line and not line.rstrip().endswith(f":{port}"):
            continue
        if "LISTENING" not in line.upper():
            continue
        parts = line.split()
        try:
            pids.add(int(parts[-1]))
        except ValueError:
            continue
    return pids


engine = Engine()


def index_file() -> Path | None:
    for name in ("index.html", "desktop.html"):
        p = UI_DIR / name
        if p.exists():
            return p
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        try:
            print("  http", fmt % args, flush=True)
        except Exception:
            return

    def _json(self, code: int, payload) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _bytes(self, code: int, data: bytes, mime: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "X-Formant-Emotion, X-Formant-Emotion-Note")
        if extra:
            for key, value in extra.items():
                if value:
                    self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/formant/status":
            self._json(200, engine.snapshot())
            return
        if path == "/formant/setup":
            self._json(200, setup_snapshot())
            return
        if path == "/formant/voices":
            self._json(200, {"voices": engine.list_voices()})
            return
        if path == "/formant/clip/status":
            self._json(200, clipper.snapshot())
            return
        if path == "/formant/clip/audio":
            wav = clipper.audio_path()
            if not wav:
                self._json(404, {"error": "No clip yet."})
                return
            self._bytes(200, wav.read_bytes(), "audio/wav")
            return
        if path in ("/clip", "/clip.html"):
            page = UI_DIR / "clip.html"
            if page.exists():
                self._bytes(200, page.read_bytes(), "text/html; charset=utf-8")
                return
        self._static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/formant/engine/start":
                engine.start_engine()
                self._json(200, engine.snapshot())
                return
            if path == "/formant/engine/stop":
                engine.stop_engine()
                self._json(200, engine.snapshot())
                return
            if path == "/formant/bot/start":
                engine.start_bot()
                self._json(200, engine.snapshot())
                return
            if path == "/formant/bot/stop":
                engine.stop_bot()
                self._json(200, engine.snapshot())
                return
            if path == "/formant/emotion":
                body = self._read_json()
                with settings_txn() as settings:
                    settings["emotionTags"] = bool(body.get("on"))
                self._json(200, engine.snapshot())
                return
            if path == "/formant/emotion/preview":
                body = self._read_json()
                raw = str(body.get("text") or "")
                tagged = tag_dialogue(raw)
                self._json(
                    200,
                    {
                        "text": raw,
                        "tagged": tagged.text,
                        "applied": tagged.tagged,
                        "error": tagged.error,
                        "emotionReady": has_api_key(),
                        "emotionProvider": provider_id(),
                    },
                )
                return
            if path == "/formant/model":
                body = self._read_json()
                engine.set_model(str(body.get("model") or "chatterbox-turbo"))
                self._json(200, engine.snapshot())
                return
            if path == "/formant/state":
                body = self._read_json()
                with settings_txn() as settings:
                    if "emotionTags" in body:
                        settings["emotionTags"] = bool(body.get("emotionTags"))
                    if body.get("model"):
                        settings["model"] = str(body["model"])
                    if body.get("voiceMode"):
                        settings["voiceMode"] = str(body["voiceMode"])
                    if body.get("voiceFile"):
                        settings["voiceFile"] = Path(str(body["voiceFile"])).name
                    if isinstance(body.get("params"), dict):
                        params = dict(settings.get("params") or DEFAULT_SETTINGS["params"])
                        params.update(body["params"])
                        settings["params"] = params
                self._json(200, engine.snapshot())
                return
            if path == "/formant/params":
                body = self._read_json()
                with settings_txn() as settings:
                    params = dict(settings.get("params") or DEFAULT_SETTINGS["params"])
                    params.update(body)
                    settings["params"] = params
                self._json(200, engine.snapshot())
                return
            if path == "/formant/select-voice":
                body = self._read_json()
                with settings_txn() as settings:
                    settings["voiceFile"] = Path(str(body.get("filename") or "ellie.wav")).name
                    settings["voiceMode"] = body.get("voiceMode") or settings.get("voiceMode") or "clone"
                self._json(200, engine.snapshot())
                return
            if path == "/formant/config":
                body = self._read_json()
                cfg = load_config()
                for key in (
                    "chatterbox_dir",
                    "bot_dir",
                    "chatterbox_url",
                    "ytdlp",
                    "ffmpeg",
                    "clip_dir",
                    "emotion_provider",
                    "emotion_model",
                ):
                    if key in body and str(body[key]).strip():
                        cfg[key] = str(body[key]).strip()
                save_config(cfg)
                self._json(200, engine.snapshot())
                return
            if path == "/formant/setup":
                body = self._read_json()
                cfg = load_config()
                for key in (
                    "chatterbox_dir",
                    "chatterbox_url",
                    "ytdlp",
                    "ffmpeg",
                    "clip_dir",
                    "emotion_provider",
                    "emotion_model",
                ):
                    if key in body:
                        cfg[key] = str(body.get(key) or "").strip()
                save_config(cfg)
                if "api_key" in body:
                    save_api_key(cfg.get("emotion_provider") or "deepseek", str(body.get("api_key") or ""))
                if "telegram_token" in body:
                    save_telegram_token(str(body.get("telegram_token") or ""))
                self._json(200, setup_snapshot())
                return
            if path == "/formant/tts":
                body = self._read_json()
                text = str(body.get("text") or "").strip()
                if not text:
                    self._json(400, {"error": "Nothing to speak."})
                    return
                if not engine.chatterbox_up():
                    self._json(503, {"error": "Chatterbox is not running. Click the matrix to start it."})
                    return
                audio, prepared = engine.tts(body)
                mime = {
                    "mp3": "audio/mpeg",
                    "opus": "audio/ogg",
                    "wav": "audio/wav",
                }.get(str(body.get("output_format") or "wav"), "audio/wav")
                extra = None
                if prepared.error:
                    extra = {
                        "X-Formant-Emotion": prepared.error,
                        "X-Formant-Emotion-Note": explain_emotion_error(prepared.error, "en"),
                    }
                self._bytes(200, audio, mime, extra)
                return
            if path == "/formant/voices/delete":
                body = self._read_json()
                engine.delete_voice(str(body.get("filename") or ""), str(body.get("kind") or "clone"))
                self._json(200, engine.snapshot())
                return
            if path == "/formant/voices":
                self._upload_voice()
                return
            if path == "/formant/clip":
                body = self._read_json()
                try:
                    snap = clipper.start(
                        str(body.get("url") or ""),
                        str(body.get("start") or ""),
                        str(body.get("end") or ""),
                        str(body.get("filename") or "clip"),
                        bool(body.get("importVoice", True)),
                    )
                except ValueError as e:
                    self._json(400, {**clipper.snapshot(), "error": str(e)})
                    return
                except RuntimeError as e:
                    self._json(409, {**clipper.snapshot(), "error": str(e)})
                    return
                self._json(200, snap)
                return
            if path == "/formant/clip/reveal":
                clipper.reveal()
                self._json(200, clipper.snapshot())
                return
        except urllib.error.HTTPError as e:
            detail = e.read()[:400].decode("utf-8", errors="ignore")
            self._json(e.code, {"error": detail or str(e)})
            return
        except Exception as e:
            self._json(500, {"error": str(e)})
            return
        self._json(404, {"error": "not found"})

    def _upload_voice(self) -> None:
        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        filename = "voice.wav"
        kind = "clone"
        data = raw
        if "multipart/form-data" in ctype:
            fields, files = parse_multipart(raw, ctype)
            kind = (fields.get("kind") or kind).strip()
            uploaded = files.get("files") or (next(iter(files.values()), None) if files else None)
            if uploaded:
                filename = uploaded["filename"] or filename
                data = uploaded["data"]
        else:
            header_text = raw[:800].decode("latin1", errors="ignore")
            m = re.search(r'filename="([^"]+)"', header_text)
            if m:
                filename = Path(m.group(1)).name
        saved = engine.save_voice(filename, data, kind if kind in ("clone", "predefined") else "clone")
        self._json(200, {"ok": True, "filename": saved, "voices": engine.list_voices()})

    def _static(self, path: str) -> None:
        rel = path.lstrip("/") or ""
        root = UI_DIR.resolve()
        target = (root / rel).resolve() if rel else None
        page = index_file()
        if path in ("/", "/console", "/index.html") or (target and not target.exists()):
            if page and (path.startswith("/console") or path in ("/", "/index.html") or (rel and "." not in Path(rel).name)):
                self._bytes(200, page.read_bytes(), "text/html; charset=utf-8")
                return
        if not rel:
            if not page:
                self._bytes(200, b"<h1>Formant</h1><p>ui folder missing.</p>", "text/html; charset=utf-8")
                return
            self._bytes(200, page.read_bytes(), "text/html; charset=utf-8")
            return
        try:
            if target:
                target.relative_to(root)
        except ValueError:
            self._json(403, {"error": "forbidden"})
            return
        if not target or not target.exists() or not target.is_file():
            if page:
                self._bytes(200, page.read_bytes(), "text/html; charset=utf-8")
                return
            self._json(404, {"error": "missing " + rel})
            return
        suffix = target.suffix.lower()
        mime = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".woff2": "font/woff2",
        }.get(suffix) or (mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self._bytes(200, target.read_bytes(), mime)


def _ensure_webview() -> bool:
    try:
        import webview  # noqa: F401

        return True
    except ImportError:
        print("  Installing pywebview…", flush=True)
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "pywebview"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            import webview  # noqa: F401

            return True
        except Exception as e:
            print(f"  pywebview install failed: {e}", flush=True)
            return False


def open_window(url: str) -> None:
    if _ensure_webview():
        try:
            import webview  # type: ignore

            webview.create_window(
                "Formant",
                url,
                width=1440,
                height=900,
                min_size=(1100, 720),
                background_color="#F6F6F7",
            )
            webview.start()
            return
        except Exception as e:
            print(f"  window fallback to browser: {e}", flush=True)
    webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return


def free_port(port: int) -> None:
    if os.name != "nt":
        return
    for pid in pids_on_port(port):
        if pid == os.getpid():
            continue
        print(f"  Closing leftover Formant ({pid})", flush=True)
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        time.sleep(0.4)


def main() -> None:
    app_paths()
    print("=" * 60, flush=True)
    print(f"  Formant {VERSION}", flush=True)
    print("=" * 60, flush=True)
    print(f"  Folder : {HERE}", flush=True)
    page = index_file()
    if not page:
        print("  [ERROR] ui folder is empty. Unzip the whole Formant folder.", flush=True)
        input("  Press Enter to close...")
        sys.exit(1)
    save_settings(load_settings())
    bot_dir = str(load_config().get("bot_dir") or "").strip()
    if bot_dir:
        migrate_token(Path(bot_dir))
    free_port(PORT)
    try:
        httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print("  [ERROR] Close every old Formant window, then try again.", flush=True)
        input("  Press Enter to close...")
        sys.exit(1)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://{HOST}:{PORT}/"
    ready = False
    for _ in range(80):
        try:
            urllib.request.urlopen(url, timeout=0.25)
            ready = True
            break
        except Exception:
            time.sleep(0.05)
    print(f"  Window : {url}", flush=True)
    print("  Click the waves to start Chatterbox in the background.", flush=True)
    print("  Close this window to quit Formant and Chatterbox.", flush=True)
    print("=" * 60, flush=True)
    if not ready:
        print("  [WARN] opening anyway.", flush=True)
    try:
        open_window(url)
    finally:
        engine.stop_engine()
        httpd.shutdown()


if __name__ == "__main__":
    main()
