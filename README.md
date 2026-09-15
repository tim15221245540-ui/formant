# Formant

Windows desktop host for [Chatterbox TTS Server](https://github.com/devnen/Chatterbox-TTS-Server). Speak cloned voices from a local window or a Telegram bot, with optional emotion tags and Chinese captions.

Formant does **not** include Chatterbox, GPU drivers, or voice samples. You install Chatterbox yourself. Formant starts it, talks to it, and adds the UI / Telegram layer.

## What you need

- Windows
- Python 3.10 or newer
- An NVIDIA GPU with a working CUDA install (Chatterbox requirement)
- [Chatterbox TTS Server](https://github.com/devnen/Chatterbox-TTS-Server) cloned and able to run on its own
- Optional: a [DeepSeek](https://platform.deepseek.com/) (or SiliconFlow / Groq / xAI) API key for emotion tags and Chinese captions
- Optional: a [Telegram bot token](https://core.telegram.org/bots/tutorial) from [@BotFather](https://t.me/BotFather)

## Install

1. Install Chatterbox TTS Server and confirm `http://127.0.0.1:8004` works when you start it.
2. Download this repo (Code → Download ZIP, or `git clone`).
3. Double-click `Formant.bat`. It installs `pywebview`, `requests`, and `python-telegram-bot` if needed, then opens the window.

## First launch

A **Setup** button sits at the top right. On the first run it opens by itself.

1. **Chatterbox folder** — the directory that contains `server.py` (the Chatterbox repo root).
2. **API key** — paste your DeepSeek (or other provider) key if you want emotion tags or Chinese captions. Leave blank to skip.
3. **Telegram bot token** — paste only if you want the bot. Leave blank to skip.
4. Save, then click the waves on the home screen to start Chatterbox.

Keys are written to `%APPDATA%\Formant\` (`deepseek_key.txt`, `token.txt`, `config.json`). They never belong in the git folder. Do not commit them.

## Features

- Local Chatterbox start/stop (no extra Chatterbox browser window)
- Voice clone upload and pick
- Turbo emotion tags: `[laugh]`, `[sigh]`, `[gasp]`, and the rest of the official set
- Chinese captions in Spare / Academic / Editorial / Writerly styles
- Telegram bot: send text, get a voice note; English uses Turbo + tags; other languages switch to Multilingual
- YouTube clipper for training audio (needs `ffmpeg` and `yt-dlp` paths in Setup)

Non-English Multilingual voices are weaker than English Turbo. That is a model limit, not a Formant setting.

## Telegram

Turn **TG Bot** on in Formant after the token is saved.

| Command | What it does |
|---|---|
| `/start` | About Formant |
| `/voice` | Pick a cloned voice |
| `/emotion` | Emotion tags on/off (English / Turbo only) |
| `/translate` | Chinese captions on/off |
| `/style` | Caption style |
| `/status` | Engine, voice, model, language |

## License

Formant is MIT. Chatterbox TTS Server is MIT (devnen). The ResembleAI speech models have their own terms — read them on Hugging Face before you download weights.

## Privacy

API keys and the Telegram token stay on the machine that runs Formant. Emotion tagging and translation call the cloud provider you chose. Audio is synthesized locally by Chatterbox.
