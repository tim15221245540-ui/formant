(() => {
  const css = `
#formant-setup-btn {
  position: fixed; top: 1.15rem; right: 1.5rem; z-index: 80;
  height: 2.25rem; padding: 0 1rem; border-radius: 999px;
  border: 1px solid #11111124; background: #fff; color: #111;
  font: 500 13px/1 "Inter Tight", "Helvetica Neue", sans-serif;
  box-shadow: 0 10px 30px #10101412, 0 1px 2px #1010140a;
  cursor: pointer;
}
#formant-setup-btn.warn { border-color: #111; }
#formant-setup-mask {
  display: none; position: fixed; inset: 0; z-index: 90;
  background: #11111133;
}
#formant-setup-mask.open { display: block; }
#formant-setup-panel {
  position: absolute; top: 4.5rem; right: 1.5rem; width: min(28rem, calc(100vw - 3rem));
  max-height: calc(100vh - 6rem); overflow: auto;
  background: #fff; color: #111; border: 1px solid #11111124;
  border-radius: 1.25rem; padding: 1.25rem 1.35rem 1.4rem;
  box-shadow: 0 18px 50px #10101414, 0 2px 6px #1010140a;
  font: 400 14px/1.5 "Inter Tight", "Helvetica Neue", sans-serif;
}
#formant-setup-panel h2 { margin: 0; font-size: 1rem; font-weight: 600; letter-spacing: .02em; }
#formant-setup-panel p.hint { margin: .35rem 0 1rem; color: #8b8b90; font-size: 13px; }
#formant-setup-panel label { display: block; margin: .85rem 0 .3rem; font-size: 12px; font-weight: 500; }
#formant-setup-panel input, #formant-setup-panel select {
  width: 100%; height: 2.6rem; border-radius: 999px; border: 1px solid #11111124;
  background: #f6f6f7; padding: 0 1rem; outline: none; box-sizing: border-box;
}
#formant-setup-panel input:focus, #formant-setup-panel select:focus {
  box-shadow: 0 0 0 2px #3b82f64d;
}
#formant-setup-panel .row { display: flex; gap: .6rem; margin-top: 1.1rem; }
#formant-setup-panel button.save {
  flex: 1; height: 2.6rem; border-radius: 999px; border: 0; background: #111; color: #fff;
  font-weight: 500; cursor: pointer;
}
#formant-setup-panel button.ghost {
  height: 2.6rem; padding: 0 1rem; border-radius: 999px; border: 1px solid #11111124;
  background: #fff; cursor: pointer;
}
#formant-setup-status { margin-top: .75rem; font-size: 12px; color: #8b8b90; min-height: 1.2em; }
#formant-setup-status.ok { color: #16a34a; }
#formant-setup-status.err { color: #dc2626; }
#formant-setup-flags { display: flex; flex-wrap: wrap; gap: .4rem; margin-bottom: .4rem; }
#formant-setup-flags span {
  font-size: 11px; letter-spacing: .04em; border-radius: 999px;
  padding: .2rem .55rem; background: #f6f6f7; color: #8b8b90;
}
#formant-setup-flags span.on { background: #111; color: #fff; }
`;

  function el(html) {
    const t = document.createElement("template");
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  }

  async function load() {
    const r = await fetch("/formant/setup", { cache: "no-store" });
    if (!r.ok) throw new Error("Could not load setup");
    return r.json();
  }

  function flag(on, ok, off) {
    const s = document.createElement("span");
    s.className = on ? "on" : "";
    s.textContent = on ? ok : off;
    return s;
  }

  function mount() {
    if (document.getElementById("formant-setup-btn")) return;
    document.head.appendChild(el(`<style>${css}</style>`));
    const btn = el(`<button type="button" id="formant-setup-btn">Setup</button>`);
    const mask = el(`
      <div id="formant-setup-mask">
        <div id="formant-setup-panel" role="dialog" aria-label="Formant setup">
          <h2>Setup</h2>
          <p class="hint">Keys stay on this PC in %APPDATA%\\Formant. They are never sent to GitHub. The shared public Telegram bot is not open yet (no budget for a 24/7 server). Use your own BotFather token below.</p>
          <div id="formant-setup-flags"></div>
          <label>Shared bot hub URL (closed for now)</label>
          <input id="fs-hub" type="text" placeholder="Not open — no 24/7 server budget yet" spellcheck="false" />
          <p class="hint" id="fs-pair-note"></p>
          <label>Chatterbox folder</label>
          <input id="fs-box" type="text" placeholder="C:\\path\\to\\Chatterbox-TTS-Server-main" spellcheck="false" />
          <label>API provider</label>
          <select id="fs-provider">
            <option value="deepseek">DeepSeek</option>
            <option value="siliconflow">SiliconFlow</option>
            <option value="groq">Groq</option>
            <option value="xai">xAI</option>
          </select>
          <label>API key (emotion tags + Chinese captions)</label>
          <input id="fs-key" type="password" autocomplete="off" placeholder="Paste key — leave blank to keep the saved one" />
          <label>Telegram bot token (from @BotFather)</label>
          <input id="fs-token" type="password" autocomplete="off" placeholder="Leave blank to keep the saved token" />
          <label>ffmpeg (optional, YouTube clipper)</label>
          <input id="fs-ffmpeg" type="text" spellcheck="false" />
          <label>yt-dlp (optional, YouTube clipper)</label>
          <input id="fs-ytdlp" type="text" spellcheck="false" />
          <div class="row">
            <button type="button" class="ghost" id="fs-close">Close</button>
            <button type="button" class="ghost" id="fs-pair">Pair bot</button>
            <button type="button" class="save" id="fs-save">Save</button>
          </div>
          <div id="formant-setup-status"></div>
        </div>
      </div>`);
    document.body.appendChild(btn);
    document.body.appendChild(mask);

    const status = mask.querySelector("#formant-setup-status");
    const flags = mask.querySelector("#formant-setup-flags");

    function setStatus(text, kind) {
      status.textContent = text || "";
      status.className = kind || "";
    }

    function fill(data) {
      flags.innerHTML = "";
      flags.appendChild(flag(data.chatterbox_ready, "Chatterbox folder OK", "Chatterbox folder missing"));
      flags.appendChild(flag(data.has_api_key, "API key saved", "No API key"));
      flags.appendChild(flag(data.has_telegram_token, "Own bot token saved", "No own bot token"));
      const hub = data.hub || {};
      flags.appendChild(flag(!!hub.session, hub.paired ? "Hub paired" : (hub.code ? "Hub code ready" : "Hub not paired"), "Hub not paired"));
      mask.querySelector("#fs-hub").value = data.hub_url || hub.hub_url || "";
      const note = mask.querySelector("#fs-pair-note");
      if (hub.code && hub.bot_username) {
        note.textContent = `In Telegram send /link ${hub.code} to @${hub.bot_username}`;
      } else if (hub.code) {
        note.textContent = `In Telegram send /link ${hub.code} to the Formant bot`;
      } else if (hub.paired) {
        note.textContent = "Paired. Keep Formant open while you use the shared bot.";
      } else {
        note.textContent = "Shared hub is closed for now (no 24/7 server budget). Use your own bot token.";
      }
      mask.querySelector("#fs-box").value = data.chatterbox_dir || "";
      mask.querySelector("#fs-provider").value = data.emotion_provider || "deepseek";
      mask.querySelector("#fs-ffmpeg").value = data.ffmpeg || "";
      mask.querySelector("#fs-ytdlp").value = data.ytdlp || "";
      mask.querySelector("#fs-key").value = "";
      mask.querySelector("#fs-token").value = "";
      btn.classList.toggle("warn", !!data.needs_setup);
    }

    async function refresh() {
      const data = await load();
      fill(data);
      return data;
    }

    async function open(force) {
      try {
        const data = await refresh();
        if (force || data.needs_setup) mask.classList.add("open");
      } catch (e) {
        setStatus(String(e.message || e), "err");
        mask.classList.add("open");
      }
    }

    btn.addEventListener("click", () => open(true));
    mask.querySelector("#fs-close").addEventListener("click", () => mask.classList.remove("open"));
    mask.addEventListener("click", (e) => {
      if (e.target === mask) mask.classList.remove("open");
    });
    async function postSetup(extra) {
      setStatus("Saving…");
      const body = {
        chatterbox_dir: mask.querySelector("#fs-box").value.trim(),
        emotion_provider: mask.querySelector("#fs-provider").value,
        ffmpeg: mask.querySelector("#fs-ffmpeg").value.trim(),
        ytdlp: mask.querySelector("#fs-ytdlp").value.trim(),
        hub_url: mask.querySelector("#fs-hub").value.trim(),
      };
      const key = mask.querySelector("#fs-key").value.trim();
      const token = mask.querySelector("#fs-token").value.trim();
      if (key) body.api_key = key;
      if (token) body.telegram_token = token;
      Object.assign(body, extra || {});
      const r = await fetch("/formant/setup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || "Save failed");
      fill(data);
      return data;
    }

    mask.querySelector("#fs-pair").addEventListener("click", async () => {
      try {
        const data = await postSetup({ pair: true });
        const hub = data.hub || {};
        setStatus(
          hub.code
            ? `Paired code ${hub.code}. Send it to the bot as /link ${hub.code}`
            : "Hub saved.",
          hub.code ? "ok" : "err"
        );
      } catch (e) {
        setStatus(String(e.message || e), "err");
      }
    });

    mask.querySelector("#fs-save").addEventListener("click", async () => {
      try {
        const data = await postSetup();
        setStatus(
          data.chatterbox_ready
            ? "Saved. Click the waves to start Chatterbox."
            : "Saved. Chatterbox folder still does not contain server.py.",
          data.chatterbox_ready ? "ok" : "err"
        );
      } catch (e) {
        setStatus(String(e.message || e), "err");
      }
    });

    open(false);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
