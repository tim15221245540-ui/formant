# Formant

Windows desktop host for [Chatterbox TTS Server](https://github.com/devnen/Chatterbox-TTS-Server). Speak cloned voices from a local window or a Telegram bot, with optional emotion tags and Chinese captions.

Formant does **not** include Chatterbox, GPU drivers, or voice samples. You install Chatterbox yourself. Formant starts it, talks to it, and adds the UI / Telegram layer.

## What you need

- Windows
- Python 3.10 or newer
- An NVIDIA GPU with a working CUDA install (Chatterbox requirement)
- [Chatterbox TTS Server](https://github.com/devnen/Chatterbox-TTS-Server) cloned and able to run on its own
- Optional: a [DeepSeek](https://platform.deepseek.com/) (or SiliconFlow / Groq / xAI) API key for emotion tags and Chinese captions
- Optional: the shared Formant Telegram bot (recommended) **or** your own token from [@BotFather](https://t.me/BotFather)

## Install

1. Install Chatterbox TTS Server and confirm `http://127.0.0.1:8004` works when you start it.
2. Download this repo (Code → Download ZIP, or `git clone`).
3. Double-click `Formant.bat`. It installs `pywebview`, `requests`, and `python-telegram-bot` if needed, then opens the window.

## First launch

A **Setup** button sits at the top right. On the first run it opens by itself.

1. **Chatterbox folder** — the directory that contains `server.py` (the Chatterbox repo root).
2. **API key** — paste your DeepSeek (or other provider) key if you want emotion tags or Chinese captions. Leave blank to skip.
3. **Shared bot hub URL** — `http://HOST-PUBLIC-IP:8766` (ask the maintainer; it is their home PC).
4. Tap **Pair bot**, then in Telegram send `/link CODE` to the Formant bot.
5. **API key** — paste your DeepSeek (or other provider) key if you want emotion tags or Chinese captions.
6. Save, then click the waves to start Chatterbox.

You do **not** need your own BotFather token if you use the shared bot.

The Telegram relay runs on the maintainer's personal computer because there is **no budget for a 24/7 server yet**. If the bot does not answer, that PC is probably off. A hosted relay is planned when funding allows.

Keys are written to `%APPDATA%\Formant\` (`deepseek_key.txt`, `token.txt`, `config.json`). They never belong in the git folder. Do not commit them.

## Features

- Local Chatterbox start/stop (no extra Chatterbox browser window)
- Voice clone upload and pick
- Turbo emotion tags: `[laugh]`, `[sigh]`, `[gasp]`, and the rest of the official set
- Chinese captions in Spare / Academic / Editorial / Writerly styles
- Telegram bot: send text, get a voice note; English uses Turbo + tags; other languages switch to Multilingual
- YouTube clipper for training audio (needs `ffmpeg` and `yt-dlp` paths in Setup)

Non-English Multilingual voices are weaker than English Turbo. That is a model limit, not a Formant setting.

## Telegram (shared bot)

Keep Formant open on your PC. Messages you send to the shared bot are synthesized **here**, with **your** cloned voices and **your** API key.

| Command | What it does |
|---|---|
| `/start` | About Formant |
| `/link CODE` | Pair this Telegram account with your Formant window |
| `/unlink` | Stop using your PC for this chat |
| `/voice` `/emotion` `/translate` `/style` | Host only — guests set these in the Formant window |

Advanced: paste your own BotFather token in Setup if you want a private bot instead of the shared one.

## License

Formant is MIT. Chatterbox TTS Server is MIT (devnen). The ResembleAI speech models have their own terms — read them on Hugging Face before you download weights.

## Privacy

API keys and the Telegram token stay on the machine that runs Formant. Emotion tagging and translation call the cloud provider you chose. Audio is synthesized locally by Chatterbox.
