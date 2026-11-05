/* =============================================================================
   Resilia shared page engine (used by every page)

   - Builds the header (logo, navigation, site picker, LIVE clock, sound) and
     the sunrise-to-sunset heat arc.
   - Fetches live data (scores + alert/action log) every 3 s (1 s at demo speed)
     and hands it to the page via Resilia.init({ onData }).
   - Shared behaviour: alert sound, supervisor actions, toast messages,
     view-only mode for the public link, and the live/summer weather switch.
   ============================================================================= */
(function () {
  "use strict";

  const PAGES = [
    { key: "control", label: "Control room", href: "/", ready: true },
    { key: "agent", label: "AI agent", href: "/ai-agent.html", ready: false },
    { key: "insights", label: "Insights", href: "/insights.html", ready: false },
    { key: "about", label: "About", href: "/about.html", ready: false },
  ];
  const SITES = ["Dubai", "Abu Dhabi", "Sharjah", "Al Ain"];
  const POLL_MS = 3000;
  const STALE_SECONDS = 20;
  const REDUCE_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const R = (window.Resilia = {
    state: {
      scores: null, log: [], speed: 1, site: "all",
      viewOnly: false, shareUrl: null,
      actualMode: null, pending: null,
    },
    REDUCE_MOTION,
    SITES,
  });

  // ---------------------------------------------------------------- helpers
  R.esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  R.hhmm = (iso) => (iso && iso.length >= 16 ? iso.slice(11, 16) : "--:--");   // "14:32"
  R.hhmmss = (iso) => (iso && iso.length >= 19 ? iso.slice(11, 19) : "--:--:--");
  // Log entries carry the simulated time when known; fall back to real time.
  R.entryTime = (e) => R.hhmm(e.sim_time || e.time);
  R.secondsSince = (localIso) => Math.max(0, Math.round((Date.now() - new Date(localIso).getTime()) / 1000));
  R.ago = (secs) => (secs < 60 ? `${secs} s ago` : `${Math.round(secs / 60)} min ago`);
  R.cap = (s) => String(s || "").replace(/(^|\s)\S/g, (c) => c.toUpperCase());
  const WORDS = ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten"];
  R.numberWord = (n) => WORDS[n] || String(n);

  R.STATUS = {
    act: { label: "Act now", cls: "st-act", rank: 0 },
    rising: { label: "Rising", cls: "st-rising", rank: 1 },
    steady: { label: "Steady", cls: "st-steady", rank: 2 },
  };
  R.statusHTML = (key) => {
    const s = R.STATUS[key] || R.STATUS.steady;
    return `<span class="status ${s.cls}"><i></i>${s.label}</span>`;
  };

  // Workers for the selected site (or all).
  R.workers = () => {
    const all = (R.state.scores && R.state.scores.workers) || [];
    return R.state.site === "all" ? all : all.filter((w) => w.site === R.state.site);
  };

  // Count numbers up/down smoothly instead of jumping.
  R.tween = (el, to, decimals = 0) => {
    if (!el) return;
    const from = parseFloat(el.dataset.v);
    el.dataset.v = to;
    if (REDUCE_MOTION || isNaN(from) || from === to) { el.textContent = Number(to).toFixed(decimals); return; }
    cancelAnimationFrame(el._raf);
    const start = performance.now();
    const step = (now) => {
      const k = Math.max(0, Math.min(1, (now - start) / 600));  // clamp: frames can be stamped early
      el.textContent = (from + (to - from) * (1 - Math.pow(1 - k, 3))).toFixed(decimals);
      if (k < 1) el._raf = requestAnimationFrame(step);
    };
    el._raf = requestAnimationFrame(step);
  };

  // Redraw part of the page without swallowing clicks or losing keyboard focus.
  let pointerIsDown = false;
  document.addEventListener("pointerdown", () => { pointerIsDown = true; }, true);
  document.addEventListener("pointerup", () => { pointerIsDown = false; }, true);
  document.addEventListener("pointercancel", () => { pointerIsDown = false; }, true);
  R.isPointerDown = () => pointerIsDown;
  R.setHTML = (el, html) => {
    if (!el || pointerIsDown || el._lastHTML === html) return;
    const focusKey = el.contains(document.activeElement) ? document.activeElement.dataset.key : null;
    el.innerHTML = html;
    el._lastHTML = html;
    if (focusKey) el.querySelector(`[data-key="${focusKey}"]`)?.focus();
  };

  let toastTimer = null;
  R.toast = (text) => {
    let el = document.getElementById("rs-toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "rs-toast"; el.className = "toast"; el.setAttribute("role", "status");
      document.body.appendChild(el);
    }
    el.textContent = text; el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
  };

  // ------------------------------------------------------- alerts & actions
  R.ACTION_LABELS = {
    dispatch_water: "Dispatch water",
    shade_break: "Schedule shade break",
    buzz: "Buzz again",
    acknowledge: "Acknowledge",
    on_my_way: "On my way",
    send_first_aider: "Send first-aider",
    call_999: "Call 999",
  };

  R.alerts = () => (R.state.scores && R.state.scores.alerts) || [];

  // An alert is handled by any action logged for it. If a "predicted" alert
  // later ESCALATED to actual RED, only actions after that count.
  R.handledAction = (alert) => R.state.log.find((e) => e.type === "action" && e.alert_id === alert.alert_id &&
    (!alert.escalated_at || e.time >= alert.escalated_at)) || null;
  R.isHandled = (alert) => !!R.handledAction(alert);
  R.lastActionFor = (workerId, filter) => R.state.log.find((e) => e.type === "action" &&
    e.worker_id === workerId && (!filter || filter(e)));
  R.actionsFor = (workerId) => R.state.log.filter((e) => e.worker_id === workerId);

  // Response time in real seconds since the latest alarm for this alert.
  R.responseSeconds = (e) => {
    const raised = R.state.log.find((x) => ["alert", "alert_escalated", "sos"].includes(x.type) &&
      x.alert_id === e.alert_id && x.time <= e.time);
    return raised ? Math.max(0, Math.round((new Date(e.time) - new Date(raised.time)) / 1000)) : null;
  };

  const inFlight = new Set();
  let recentLocal = [];   // actions just clicked, kept until the server's log has them
  R.inFlight = (action, worker) => inFlight.has(`${action}:${worker}`);

  R.postAction = async (worker, action, alertId) => {
    if (R.state.viewOnly) return null;
    const key = `${action}:${worker}`;
    if (inFlight.has(key)) return null;
    inFlight.add(key);
    R.emit();
    try {
      const res = await fetch("/api/actions", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ worker_id: worker, action, alert_id: alertId || null }),
      });
      const entry = await res.json();
      if (!res.ok) throw new Error(entry.error || "failed");
      R.state.log.unshift(entry);
      recentLocal.push({ entry, until: Date.now() + 10000 });
      if (action === "call_999") {
        R.toast("Logged. Dial 999 now: Resilia doesn't place the call itself.");
      } else {
        R.toast(`✓ ${entry.label}: ${worker}`);
      }
      return entry;
    } catch (err) {
      R.toast(`Couldn't log the action: ${err.message}`);
      return null;
    } finally {
      inFlight.delete(key);
      R.emit();
    }
  };

  // Any button with data-action / data-worker (and optional data-alert) posts an action.
  document.addEventListener("click", (event) => {
    const btn = event.target.closest("button[data-action]");
    if (!btn || btn.disabled) return;
    R.postAction(btn.dataset.worker, btn.dataset.action, btn.dataset.alert || null);
  });

  R.actionButton = (action, workerId, alertId, cls = "btn btn-outline", label) => {
    if (R.state.viewOnly) return "";
    const key = `${action}:${workerId}:${cls}`;
    return `<button type="button" class="${cls}" data-action="${action}" data-worker="${R.esc(workerId)}"
      data-alert="${R.esc(alertId || "")}" data-key="${R.esc(key)}"${R.inFlight(action, workerId) ? " disabled" : ""}>
      ${R.esc(label || R.ACTION_LABELS[action])}</button>`;
  };

  function mergeLog(serverEntries) {
    const now = Date.now();
    const same = (a, b) => a.time === b.time && a.worker_id === b.worker_id && a.action === b.action;
    recentLocal = recentLocal.filter((l) => l.until > now && !serverEntries.some((s) => same(s, l.entry)));
    return [...recentLocal.map((l) => l.entry), ...serverEntries].sort((a, b) => b.time.localeCompare(a.time));
  }

  // ------------------------------------------------------------------ sound
  // Browsers block sound until the user clicks the page once.
  let soundWanted = true;
  try { soundWanted = localStorage.getItem("hs-sound") !== "off"; } catch {}
  let audio = null;
  let lockedAtPress = false;
  R.soundReady = () => soundWanted && audio && audio.state === "running";
  R.soundBlocked = () => soundWanted && !R.soundReady();

  function unlockAudio() {
    lockedAtPress = soundWanted && !R.soundReady();
    if (!soundWanted) return;
    try {
      audio ||= new (window.AudioContext || window.webkitAudioContext)();
      audio.resume().then(updateSoundButton);
    } catch {}
  }
  document.addEventListener("pointerdown", unlockAudio, { capture: true });
  document.addEventListener("keydown", unlockAudio, { capture: true });

  // Gentle chime: a pure (sine) tone that fades in softly and rings out slowly,
  // through a filter that takes the edge off. No harsh or high-pitched beeps.
  function chime(freq, start, volume) {
    const osc = audio.createOscillator();
    const gain = audio.createGain();
    const soften = audio.createBiquadFilter();
    osc.type = "sine";
    osc.frequency.value = freq;
    soften.type = "lowpass";
    soften.frequency.value = 1800;
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(volume, start + 0.04);   // soft start
    gain.gain.exponentialRampToValueAtTime(0.0001, start + 1.2);    // slow fade
    osc.connect(soften).connect(gain).connect(audio.destination);
    osc.start(start);
    osc.stop(start + 1.3);
  }

  // Three calm sounds, still easy to tell apart (all below ~700 Hz, played once):
  //   predicted RED: one soft note
  //   RED:           two notes, rising
  //   SOS:           three notes, rising (the most noticeable, still calm)
  // Reminders for unhandled alerts play at a lower volume.
  R.playAlert = (kind, reminder = false) => {
    if (!R.soundReady()) return;
    const t = audio.currentTime + 0.05;
    const v = reminder ? 0.6 : 1;
    if (kind === "sos") [392, 523, 659].forEach((f, i) => chime(f, t + i * 0.28, 0.13 * v));
    else if (kind === "red") [440, 587].forEach((f, i) => chime(f, t + i * 0.3, 0.11 * v));
    else chime(523, t, 0.08 * v);
  };

  const heard = new Map();
  let lastReminder = Date.now();
  const REMINDER_MS = 60000;
  const RANK = { sos: 3, red: 2, predicted_red: 1 };
  function soundForAlerts() {
    let toPlay = null;
    for (const a of R.alerts()) {
      if (R.isHandled(a)) { heard.set(a.alert_id, a.kind); continue; }
      const h = heard.get(a.alert_id);
      if (h !== a.kind && !(h === "red" && a.kind !== "sos")) {
        heard.set(a.alert_id, a.kind);
        if (!toPlay || RANK[a.kind] > RANK[toPlay]) toPlay = a.kind;
      }
    }
    // Reminder for urgent alerts nobody has handled: once a minute, quieter.
    const urgent = R.alerts().find((a) => (a.kind === "sos" || a.kind === "red") && !R.isHandled(a));
    if (toPlay) { R.playAlert(toPlay); lastReminder = Date.now(); }
    else if (urgent && Date.now() - lastReminder > REMINDER_MS) { R.playAlert(urgent.kind, true); lastReminder = Date.now(); }
  }

  function updateSoundButton() {
    const btn = document.getElementById("rs-sound");
    if (!btn) return;
    const locked = soundWanted && !R.soundReady();
    btn.textContent = !soundWanted ? "🔕 Sound off" : locked ? "🔔 Tap for sound" : "🔔 Sound on";
    btn.classList.toggle("locked", locked);
    btn.setAttribute("aria-pressed", String(soundWanted));
  }
  function onSoundClick() {
    if (lockedAtPress || (soundWanted && !R.soundReady())) {
      lockedAtPress = false;
      unlockAudio();
      setTimeout(() => R.playAlert("predicted_red"), 100);
    } else {
      soundWanted = !soundWanted;
      try { localStorage.setItem("hs-sound", soundWanted ? "on" : "off"); } catch {}
      if (soundWanted) { unlockAudio(); setTimeout(() => R.playAlert("predicted_red"), 100); }
    }
    updateSoundButton();
    R.emit();
  }

  // --------------------------------------------------------------- header
  const LOGO = `<svg width="42" height="42" viewBox="0 0 42 42" aria-hidden="true">
      <circle cx="21" cy="21" r="19" fill="none" stroke="#f5a53c" stroke-width="2.2" opacity=".85"/>
      <circle cx="21" cy="21" r="12.5" fill="#f5a53c" opacity=".22"/>
      <circle cx="21" cy="21" r="8.5" fill="url(#rs-logo-g)"/>
      <defs><radialGradient id="rs-logo-g" cx=".4" cy=".38"><stop offset="0" stop-color="#ffd18a"/>
      <stop offset=".6" stop-color="#f58a3c"/><stop offset="1" stop-color="#e2553e"/></radialGradient></defs></svg>`;

  function buildHeader(pageKey) {
    const header = document.getElementById("rs-header");
    if (!header) return;
    header.className = "rs-header";
    const nav = PAGES.map((p) => p.ready
      ? `<a href="${p.href}"${p.key === pageKey ? ' aria-current="page"' : ""}>${p.label}</a>`
      : `<span class="soon" title="Coming next">${p.label}</span>`).join("");
    const siteOpts = [`<option value="all">All sites</option>`, ...SITES.map((s) => `<option value="${s}">${s}</option>`)].join("");
    header.innerHTML = `
      <div class="hdr-top">
        <a class="brand" href="/" aria-label="Resilia home">${LOGO}<span class="brand-name">Resilia</span></a>
        <nav class="nav" aria-label="Main">${nav}</nav>
        <div class="hdr-right">
          <a class="hpill alerts" id="rs-alerts" href="/" hidden></a>
          <span class="hpill demo" id="rs-demo" hidden></span>
          <span class="hpill view" id="rs-view" hidden title="You're on the public link: you can watch, but actions are taken in the control room">👁 View only</span>
          <button type="button" class="hpill share" id="rs-share" hidden title="Public view-only link for teammates. Click to copy."></button>
          <label class="site-sel">Site <select id="rs-site">${siteOpts}</select></label>
          <span class="hpill live" id="rs-live" role="status"><i></i><span id="rs-live-text">Connecting</span></span>
          <button type="button" class="hpill sound" id="rs-sound">🔔 Sound</button>
        </div>
      </div>
      <div class="arc-wrap">
        <div class="arc-box" id="rs-arc-box">
          <svg viewBox="0 0 1000 140" preserveAspectRatio="none" aria-hidden="true">
            <defs><linearGradient id="rs-peak-g" x1="0" x2="1"><stop offset="0" stop-color="#f5a53c"/><stop offset="1" stop-color="#f07a3a"/></linearGradient></defs>
            <path d="M10 136 Q500 -96 990 136" fill="none" stroke="rgba(255,255,255,.45)" stroke-width="2.2"
                  stroke-dasharray="2 7" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
            <polyline id="rs-peak" fill="none" stroke="url(#rs-peak-g)" stroke-width="5" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
          </svg>
          <div class="arc-sun" id="rs-sun" hidden></div>
        </div>
        <div class="arc-labels">
          <span id="rs-sunrise">Sunrise --:--</span>
          <span class="arc-mid" id="rs-arc-mid">&nbsp;</span>
          <span id="rs-sunset">Sunset --:--</span>
        </div>
      </div>`;

    const sel = document.getElementById("rs-site");
    try { R.state.site = localStorage.getItem("rs-site") || "all"; } catch {}
    if (!["all", ...SITES].includes(R.state.site)) R.state.site = "all";
    sel.value = R.state.site;
    sel.addEventListener("change", () => {
      R.state.site = sel.value;
      try { localStorage.setItem("rs-site", sel.value); } catch {}
      R.emit();
    });
    document.getElementById("rs-sound").addEventListener("click", onSoundClick);
    document.getElementById("rs-share").addEventListener("click", async () => {
      if (!R.state.shareUrl) return;
      try { await navigator.clipboard.writeText(R.state.shareUrl); R.toast("✓ Public link copied. Paste it in your team chat."); }
      catch { R.toast(R.state.shareUrl); }
    });
    updateSoundButton();
  }

  // Heat arc: dotted path from sunrise to sunset, peak-heat stretch highlighted,
  // the sun placed at the current (simulated) time of day.
  const PEAK = [12, 15.5];   // peak heat window, hours
  const toHours = (hhmm) => { const [h, m] = hhmm.split(":").map(Number); return h + m / 60; };
  const arcPoint = (t) => {  // quadratic curve: P0 (10,136), C (500,-96), P2 (990,136)
    const u = 1 - t;
    return [u * u * 10 + 2 * u * t * 500 + t * t * 990, u * u * 136 + 2 * u * t * -96 + t * t * 136];
  };
  function updateArc() {
    const s = R.state.scores;
    if (!s || !s.sun || !document.getElementById("rs-arc-box")) return;
    const rise = toHours(s.sun.sunrise), set = toHours(s.sun.sunset);
    const now = toHours(R.hhmm(s.sim_time));
    const t = (h) => (h - rise) / (set - rise);
    const pts = [];
    for (let i = 0; i <= 24; i++) {
      const tt = t(PEAK[0]) + (t(PEAK[1]) - t(PEAK[0])) * (i / 24);
      pts.push(arcPoint(Math.max(0, Math.min(1, tt))).map((v) => v.toFixed(1)).join(","));
    }
    document.getElementById("rs-peak").setAttribute("points", pts.join(" "));
    const sun = document.getElementById("rs-sun");
    const tn = t(now);
    const night = tn < 0 || tn > 1;
    const [x, y] = arcPoint(Math.max(0, Math.min(1, tn)));
    sun.hidden = false;
    sun.classList.toggle("night", night);
    sun.style.left = `${x / 10}%`;
    sun.style.top = `${(y / 140) * 100}%`;
    document.getElementById("rs-sunrise").textContent = `Sunrise ${s.sun.sunrise}`;
    document.getElementById("rs-sunset").textContent = `Sunset ${s.sun.sunset}`;

    const sites = Object.entries(s.sites || {});
    const hottest = sites.sort((a, b) => b[1].wbgt_c - a[1].wbgt_c)[0];
    const inPeak = now >= PEAK[0] && now <= PEAK[1];
    const nowText = `Now ${R.hhmm(s.sim_time)}`;
    const hotText = hottest ? ` · hottest site ${R.esc(hottest[0])} ${hottest[1].wbgt_c.toFixed(1)}° WBGT` : "";
    R.setHTML(document.getElementById("rs-arc-mid"),
      `<b>Peak heat 12:00–15:30</b> · ${inPeak ? "<b>in peak heat now</b> · " : ""}${nowText}${hotText}`);
  }

  function updateHeaderPills(pageKey) {
    const s = R.state.scores;
    const demo = document.getElementById("rs-demo");
    demo.hidden = R.state.speed <= 1;
    demo.textContent = `⏩ ${R.state.speed}× demo`;
    document.getElementById("rs-view").hidden = !R.state.viewOnly;
    const share = document.getElementById("rs-share");
    share.hidden = !R.state.shareUrl;
    if (R.state.shareUrl) R.setHTML(share, `🌐 <span>${R.esc(R.state.shareUrl.replace("https://", ""))}</span>`);
    // On pages other than the control room, show an alert chip linking back to it.
    const chip = document.getElementById("rs-alerts");
    const open = R.alerts().filter((a) => !R.isHandled(a));
    chip.hidden = pageKey === "control" || !open.length;
    if (open.length) chip.textContent = `⚠ ${open.length} open alert${open.length > 1 ? "s" : ""}`;
    if (s) updateArc();
  }

  function setLive(state, text) {
    const pill = document.getElementById("rs-live");
    if (!pill) return;
    pill.classList.toggle("is-live", state === "live");
    pill.classList.toggle("is-warn", state === "warn");
    document.getElementById("rs-live-text").textContent = text;
  }

  // Eyebrow / status line shared by pages: date · data source · warnings.
  R.statusText = "";
  R.statusWarn = false;
  function setStatus(text, warn = false) {
    R.statusText = text; R.statusWarn = warn;
  }

  // ------------------------------------------------------- weather switch
  // The switch shows the mode the engine ACTUALLY used. After a click we wait
  // until the engine confirms that exact change (settings version).
  const SWITCH_TIMEOUT_MS = 45000;
  function showMode(mode) {
    document.querySelectorAll(".rs-switch button[data-mode]").forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
  }
  document.addEventListener("click", async (event) => {
    const btn = event.target.closest(".rs-switch button[data-mode]");
    if (!btn || R.state.viewOnly) return;
    const mode = btn.dataset.mode;
    const st = R.state;
    if (st.pending ? st.pending.mode === mode : st.actualMode === mode) return;
    const request = { mode, version: Infinity, since: Date.now() };
    st.pending = request;
    showMode(mode);
    setLive("warn", "Switching…");
    try {
      const res = await fetch("/api/mode", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ weather_mode: mode }) });
      if (!res.ok) throw new Error("server said no");
      const saved = await res.json();
      if (st.pending === request) st.pending.version = saved.version;
    } catch {
      if (st.pending === request) st.pending = null;
      showMode(st.actualMode);
      R.toast("Couldn't change the weather mode: server not reachable");
    }
  });

  // -------------------------------------------------------------- polling
  let onData = () => {};
  let pageKey = "";
  R.emit = () => { try { onData(R.state); } catch (err) { console.error(err); } updateHeaderPills(pageKey); };

  async function loadAccess() {
    try {
      const access = await (await fetch("/api/access", { cache: "no-store" })).json();
      const was = R.state.viewOnly;
      R.state.viewOnly = !!access.view_only;
      R.state.shareUrl = access.share_url || null;
      document.body.classList.toggle("view-only", R.state.viewOnly);
      if (was !== R.state.viewOnly) R.emit();
    } catch {}
  }

  async function refresh() {
    const st = R.state;
    try {
      const [res, logRes] = await Promise.all([
        fetch("/api/scores", { cache: "no-store" }),
        fetch("/api/log", { cache: "no-store" }).catch(() => null),
      ]);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "No data");
      if (logRes && logRes.ok) st.log = mergeLog((await logRes.json()).entries || []);
      st.scores = data;
      st.speed = data.speed || 1;

      st.actualMode = data.weather_mode;
      let timedOut = false;
      if (st.pending && (data.settings_version ?? 0) >= st.pending.version) st.pending = null;
      else if (st.pending && Date.now() - st.pending.since > SWITCH_TIMEOUT_MS) { st.pending = null; timedOut = true; }
      showMode(st.pending ? st.pending.mode : st.actualMode);

      const age = R.secondsSince(data.generated_at);
      const failed = data.live_weather_failed || [];
      const clock = `${R.hhmm(data.sim_time)} GST`;
      if (st.pending) { setLive("warn", "Switching…"); setStatus("Switching weather source…"); }
      else if (timedOut) { setLive("off", "No confirmation"); setStatus("The risk engine didn't confirm the switch. Is it running?", true); }
      else if (age > STALE_SECONDS) { setLive("off", `Paused · ${clock}`); setStatus(`Data is ${age} s old. Is the risk engine running?`, true); }
      else {
        setLive("live", `Live · ${clock}${st.speed > 1 ? " (demo time)" : ""}`);
        if (data.weather_mode === "live" && failed.length) {
          setStatus(`Live weather unavailable for ${failed.join(", ")}: using simulated values there`, true);
        } else {
          setStatus(data.weather_mode === "offline" ? "Summer scenario" : "Live weather from Open-Meteo");
        }
      }
      R.emit();
      soundForAlerts();
    } catch (err) {
      setLive("off", "Offline");
      setStatus("Waiting for the risk engine… " + err.message, true);
      R.emit();
    }
  }

  async function loop() {
    await refresh();
    setTimeout(loop, R.state.speed > 1 ? 1000 : POLL_MS);
  }

  R.init = (opts) => {
    pageKey = opts.page;
    onData = opts.onData || (() => {});
    buildHeader(pageKey);
    loadAccess().then(loop);
    setInterval(loadAccess, 10000);
  };
})();
