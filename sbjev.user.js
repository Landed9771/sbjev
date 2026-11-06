// ==UserScript==
// @name         sbjev - Jev SponsorBlock drafts
// @namespace    https://github.com/Landed9771/sbjev
// @version      1.8
// @description  Masthead button: ask the local sbjev server to draft SponsorBlock segments for this video, then load them into SponsorBlock as unsubmitted segments.
// @match        https://www.youtube.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @connect      127.0.0.1
// @connect      localhost
// @updateURL    https://raw.githubusercontent.com/Landed9771/sbjev/main/sbjev.user.js
// @downloadURL  https://raw.githubusercontent.com/Landed9771/sbjev/main/sbjev.user.js
// @run-at       document-idle
// ==/UserScript==

(function () {
  "use strict";

  // the local sbjev server; change it from the userscript manager's menu if you moved the port
  const gmGet = (k, d) => { try { return typeof GM_getValue === "function" ? GM_getValue(k, d) : d; } catch { return d; } };
  const SERVER = String(gmGet("server", "http://127.0.0.1:8765")).replace(/\/+$/, "");
  if (typeof GM_registerMenuCommand === "function") {
    GM_registerMenuCommand("Set sbjev server address", () => {
      const v = prompt("sbjev server address (matches \"port\" in config.json):", SERVER);
      if (v && /^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(v.trim())) {
        GM_setValue("server", v.trim());
        alert("Saved. Reload the page to use it.");
      } else if (v) {
        alert("Use http://127.0.0.1:PORT or http://localhost:PORT");
      }
    });
  }
  const BTN_ID = "sbjev-button";
  const results = new Map(); // videoId|categories -> server result, survives SPA navigation
  const ALL_CATS = ["sponsor", "selfpromo", "interaction", "intro", "outro", "preview", "filler", "poi_highlight"];
  const LABEL = { poi_highlight: "highlight" };
  const DEFAULT_CATS = ["sponsor", "selfpromo", "interaction", "intro", "outro"];
  const getCats = () => {
    try {
      const saved = JSON.parse(localStorage.getItem("sbjev-cats"));
      if (Array.isArray(saved) && saved.length) return saved.filter((c) => ALL_CATS.includes(c));
    } catch { /* storage blocked or corrupt */ }
    return DEFAULT_CATS;
  };
  const setCats = (cats) => { try { localStorage.setItem("sbjev-cats", JSON.stringify(cats)); } catch { } };
  // optional: don't look for categories SponsorBlock already has (cheaper; misses a second read)
  const getSkipKnown = () => { try { return localStorage.getItem("sbjev-skipknown") === "1"; } catch { return false; } };
  const setSkipKnown = (v) => { try { localStorage.setItem("sbjev-skipknown", v ? "1" : "0"); } catch { } };
  const key = (id) => id + "|" + getCats().join(",") + (getSkipKnown() ? "|skip" : "");

  const style = document.createElement("style");
  style.textContent = `
    #${BTN_ID} { color: rgb(136,136,136); background: rgb(32,32,32); border: 1px solid rgb(48,48,48);
      cursor: pointer; margin-left: 4px; padding: 0 8px; font: 12px Roboto, Arial, sans-serif; height: 24px;
      align-self: center; white-space: nowrap; flex: none; }
    #${BTN_ID}:hover { color: rgb(200,200,200); }
    #${BTN_ID}[data-busy] { color: rgb(0,212,0); cursor: progress; }
    #sbjev-panel { position: fixed; z-index: 9999; width: 420px; max-width: calc(100vw - 16px); box-sizing: border-box;
      background: rgb(24,24,24); color: rgb(220,220,220); border: 1px solid rgb(64,64,64); border-radius: 8px;
      padding: 12px 14px; font: 13px/1.45 Roboto, Arial, sans-serif; box-shadow: 0 8px 24px rgba(0,0,0,.5); }
    #sbjev-panel h4 { margin: 0 0 8px; font-size: 13px; font-weight: 500; color: #fff; }
    #sbjev-panel .seg { padding: 6px 0; border-top: 1px solid rgb(48,48,48); }
    #sbjev-panel .seg.old { opacity: .5; }
    #sbjev-panel .cat { display: inline-block; min-width: 80px; font-weight: 500; }
    #sbjev-panel .sponsor { color: #00d400; } #sbjev-panel .selfpromo { color: #ffff00; }
    #sbjev-panel .interaction { color: #cc00ff; } #sbjev-panel .intro { color: #00ffff; }
    #sbjev-panel .outro { color: #6b6bff; } #sbjev-panel .preview { color: #29a8ff; }
    #sbjev-panel .filler { color: #a36bff; } #sbjev-panel .poi_highlight { color: #ff4f9a; }
    #sbjev-panel .cats { display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 10px; padding-top: 8px;
      border-top: 1px solid rgb(48,48,48); font-size: 12px; }
    #sbjev-panel .cats label { display: flex; align-items: center; gap: 4px; cursor: pointer; }
    #sbjev-panel .cats input { margin: 0; accent-color: #3ea6ff; }
    #sbjev-panel .quote { color: rgb(150,150,150); font-size: 12px; }
    #sbjev-panel .note { color: rgb(200,160,90); font-size: 12px; }
    #sbjev-panel code { font: 12px Consolas, monospace; color: #fff; user-select: all; }
    #sbjev-panel .row { display: flex; gap: 8px; justify-content: flex-end; margin-top: 10px; }
    #sbjev-panel button { background: rgb(48,48,48); color: #fff; border: 0; border-radius: 16px;
      padding: 6px 14px; cursor: pointer; font: inherit; }
    #sbjev-panel button.go { background: #3ea6ff; color: #0f0f0f; font-weight: 500; }
    #sbjev-panel .muted { color: rgb(150,150,150); }
    #sbjev-panel .spend { margin-top: 4px; font-size: 12px; }
  `;
  document.head.appendChild(style);

  // YouTube enforces Trusted Types, so innerHTML throws; build everything with this.
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "onclick") el.addEventListener("click", v);
      else if (k === "class") el.className = v;
      else el.setAttribute(k, v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid);
    return el;
  }

  const videoId = () => (location.pathname === "/watch" ? new URLSearchParams(location.search).get("v") : null);
  const fmt = (t) => {
    t = Math.floor(t);
    const hh = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
    return (hh ? hh + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0");
  };

  function ensureButton() {
    if (document.getElementById(BTN_ID)) return;
    const center = document.querySelector("ytd-masthead #center");
    if (!center) return;
    const btn = h("button", { id: BTN_ID, title: "Draft SponsorBlock segments for this video with Jev", onclick: onClick }, "Jev SB");
    // sit next to the sb.ltn.fi button if it's there, otherwise before the search box
    const anchor = center.querySelector('[id^="sb.ltn.fi"]');
    if (anchor) anchor.after(btn);
    else center.prepend(btn);
  }

  const closePanel = () => document.getElementById("sbjev-panel")?.remove();

  function showPanel(...body) {
    closePanel();
    const r = document.getElementById(BTN_ID).getBoundingClientRect();
    const panel = h("div", { id: "sbjev-panel" }, h("h4", null, "Jev SponsorBlock draft"), ...body);
    panel.style.top = r.bottom + 8 + "px";
    panel.style.left = Math.max(8, Math.min(r.left, innerWidth - 428)) + "px";
    document.body.appendChild(panel);
  }

  const btnRow = (...buttons) => h("div", { class: "row" }, ...buttons);

  // category picker; the choice is remembered and applies to the next run / Re-run
  function catPicker() {
    const chosen = new Set(getCats());
    return h("div", { class: "cats" }, ALL_CATS.map((c) => {
      const box = h("input", { type: "checkbox" });
      box.checked = chosen.has(c);
      box.addEventListener("change", () => {
        box.checked ? chosen.add(c) : chosen.delete(c);
        if (chosen.size) setCats(ALL_CATS.filter((x) => chosen.has(x)));
        else box.checked = true, chosen.add(c); // keep at least one
      });
      return h("label", { class: c }, box, LABEL[c] || c);
    }), (() => {
      const box = h("input", { type: "checkbox" });
      box.checked = getSkipKnown();
      box.addEventListener("change", () => setSkipKnown(box.checked));
      return h("label", { title: "Cheaper: most videos already have their sponsor read on SponsorBlock. " +
                                 "The catch: a second read that nobody submitted won't be found." },
        box, "skip categories already on SponsorBlock");
    })());
  }
  const closeBtn = (label = "Close") => h("button", { onclick: closePanel }, label);

  function renderResult(res) {
    const fresh = res.segments.filter((s) => s.new);
    const summary = !res.segments.length
      ? "Jev found nothing to skip."
      : !fresh.length
        ? "Everything Jev found is already on SponsorBlock."
        : `${fresh.length} new segment${fresh.length > 1 ? "s" : ""}. Loading reloads the page and puts them in SponsorBlock's submit menu, unsubmitted.`;
    const rows = res.segments.map((s) =>
      h("div", { class: "seg" + (s.new ? "" : " old") },
        h("span", { class: "cat " + s.category }, LABEL[s.category] || s.category),
        s.category === "poi_highlight"
          ? `at ${fmt(s.segment[0])} `
          : `${fmt(s.segment[0])}–${s.segment[1] > (document.querySelector("video")?.duration || Infinity) ? "end" : fmt(s.segment[1])} `,
        h("span", { class: "muted" }, s.category === "poi_highlight"
          ? `(conf ${s.confidence.toFixed(2)})`
          : `(${(s.segment[1] - s.segment[0]).toFixed(0)}s, conf ${s.confidence.toFixed(2)})`),
        h("div", { class: "quote" }, `“${s.text.slice(0, 110)}”`),
        s.notes.length ? h("div", { class: "note" }, s.notes.join("; ")) : null));
    showPanel(
      h("div", { class: "muted" }, summary),
      spendLine(res),
      rows,
      catPicker(),
      btnRow(
        h("button", { onclick: () => run(res.id) }, "Run again"),
        closeBtn(),
        res.url ? h("button", { class: "go", onclick: () => loadIntoSponsorBlock(res) }, "Load into SponsorBlock") : null));
  }

  function loadIntoSponsorBlock(res) {
    const id = res.id;
    if (!res.url) return;
    // SponsorBlock only reads #segments= once per page load, so this has to be a real load.
    // The playback position is carried across in sessionStorage rather than a &t= in the
    // URL: a URL timestamp would win again on every later reload and lose your place.
    const target = new URL(res.url);
    const video = document.querySelector("video");
    if (video && videoId() === id) {
      try { sessionStorage.setItem("sbjev-resume", JSON.stringify({ id, t: video.currentTime })); } catch { }
    }
    const sameDoc = target.pathname + target.search === location.pathname + location.search;
    location.href = target.toString();
    if (sameDoc) location.reload();
  }

  // After a "Load into SponsorBlock" reload: seek back to where you were (once), then strip
  // #segments= from the address so a later reload doesn't re-import already-handled drafts.
  function afterImportLoad() {
    let resume = null;
    try {
      resume = JSON.parse(sessionStorage.getItem("sbjev-resume"));
      sessionStorage.removeItem("sbjev-resume");
    } catch { }
    const hasImport = location.hash.startsWith("#segments=");
    if (!hasImport && !resume) return;
    let seeked = !(resume && resume.id === videoId());
    let seenAt = 0;
    const started = Date.now();
    const timer = setInterval(() => {
      const video = document.querySelector("#movie_player video");
      const adShowing = document.querySelector("#movie_player.ad-showing");
      const ready = video && video.readyState >= 1 && !adShowing && video.duration > 0;
      if (ready && !seeked) {
        video.currentTime = Math.min(resume.t, video.duration - 1);
        seeked = true;
      }
      if (ready && !seenAt) seenAt = Date.now();
      // SponsorBlock reads the hash when the video loads; give it a few seconds, then clean up
      if (seenAt && Date.now() - seenAt > 4000 && location.hash.startsWith("#segments=")) {
        const url = new URL(location.href);
        url.hash = "";
        url.searchParams.delete("t"); // links made by older versions of this script carried one
        history.replaceState(history.state, "", url.toString());
      }
      if ((seeked && seenAt && Date.now() - seenAt > 4000) || Date.now() - started > 180000) clearInterval(timer); // ads can run a while
    }, 500);
  }

  // "$0.0004 this video · $0.0023 spent so far · $4.99 left"; money under a cent needs 4 decimals
  const usd = (x) => "$" + (x < 1 ? x.toFixed(4) : x.toFixed(2));
  function spendLine(res) {
    const parts = [];
    if (res.cost != null) parts.push(res.cost ? `${usd(res.cost)} this video` : "this video was free (cached)");
    if (res.account) parts.push(`${usd(res.account.spent)} spent so far`, `${usd(res.account.balance)} left`);
    return parts.length ? h("div", { class: "muted spend" }, parts.join(" · ")) : null;
  }

  // What to tell the user for each error kind the server reports (sbjev.py error_kind)
  const ADVICE = {
    bot_check: "YouTube's bot check blocked the caption download. If a VPN is on, turn it off and retry; " +
               "otherwise it usually clears within a few hours.",
    rate_limited: "YouTube is rate-limiting caption downloads from this connection. Wait a few minutes and retry.",
    no_captions: "This video has no captions, so there's nothing for Jev to read.",
    unavailable: "YouTube says this video is unavailable here (private, members-only or age-restricted).",
    credit: "Out of usable Jev credit. Jev needs paid credit: vercel.com → AI Gateway → Credits.",
    jev_busy: "Jev's servers were overloaded and kept refusing. Retry in a minute.",
    server: "Something broke inside the sbjev server. Details are in server.log in the sbjev folder.",
  };

  function showError(msg, res = {}, retryId = null) {
    const advice = res.kind && ADVICE[res.kind];
    showPanel(
      h("div", { class: "note" }, ...[advice || msg].flat()),
      advice && res.error ? h("div", { class: "muted", style: "font-size:11px;margin-top:4px" }, res.error) : null,
      spendLine({ account: res.account }), catPicker(),
      btnRow(retryId ? h("button", { class: "go", onclick: () => run(retryId) }, "Retry") : null, closeBtn()));
  }

  // GM_xmlhttpRequest as a promise: resolves {status, data}; rejects with "unreachable" / "timeout"
  function request(path, timeout) {
    return new Promise((resolve, reject) => {
      if (typeof GM_xmlhttpRequest !== "function") return reject(new Error("nogm"));
      GM_xmlhttpRequest({
        method: "GET", url: SERVER + path, timeout,
        onload(r) {
          let data = null;
          try { data = JSON.parse(r.responseText || "null"); } catch { /* not JSON */ }
          resolve({ status: r.status, data, text: (r.responseText || "").slice(0, 200) });
        },
        onerror() { reject(new Error("unreachable")); },
        ontimeout() { reject(new Error("timeout")); },
      });
    });
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // the background launcher restarts a crashed server within ~5 s, so give it a moment
  async function pingServer(onWait) {
    for (let i = 0; i < 4; i++) {
      try {
        const r = await request("/ping", 4000);
        if (r.status === 200 && r.data) return r.data;
      } catch (e) {
        if (e.message === "nogm") throw e;
      }
      if (i < 3) { onWait(i); await sleep(3000); }
    }
    return null;
  }

  // install.ps1 registers sbjev:// to launch start-server.vbs, so the page can start the server
  async function startServer(id) {
    const a = h("a", { href: "sbjev://start", style: "display:none" });
    document.body.appendChild(a);
    a.click();
    a.remove();
    showPanel(h("div", { class: "muted" }, "Starting the sbjev server…"), btnRow(closeBtn("Hide")));
    for (let i = 0; i < 10; i++) {
      await sleep(2000);
      try {
        const r = await request("/ping", 3000);
        if (r.status === 200 && r.data) return run(id);
      } catch { /* not up yet */ }
    }
    showError(["Still no answer from the server. If the browser asked about opening an sbjev link, allow it " +
      "and press Retry. Otherwise run install.ps1 in the sbjev folder (it registers the link), or check " +
      "server.log there."], {}, id);
  }

  async function run(id) {
    const btn = document.getElementById(BTN_ID);
    if (btn.dataset.busy) return;
    btn.dataset.busy = "1";
    const started = Date.now();
    const tick = setInterval(() => { btn.textContent = `Jev… ${Math.round((Date.now() - started) / 1000)}s`; }, 500);
    const done = () => { clearInterval(tick); delete btn.dataset.busy; btn.textContent = "Jev SB"; };
    const status = (text) => showPanel(h("div", { class: "muted" }, text), btnRow(closeBtn("Hide")));
    const k = key(id);
    try {
      status("Checking the sbjev server…");
      const ping = await pingServer(() => status("The sbjev server isn't answering yet; waiting for it to (re)start…"));
      if (!ping) {
        done();
        return showPanel(
          h("div", { class: "note" }, "The sbjev server isn't running. It normally starts at login."),
          h("div", { class: "muted", style: "font-size:12px;margin-top:4px" },
            "Start server asks Windows to open it (your browser may ask once whether to allow sbjev links). " +
            "If nothing happens, run install.ps1 from the sbjev folder, or double-click start-server.vbs there."),
          btnRow(h("button", { class: "go", onclick: () => startServer(id) }, "Start server"), closeBtn()));
      }
      status(ping.busy && ping.busy !== id
        ? `The server is busy with another video (${ping.busy_for}s so far); this one is queued after it.`
        : "Reading captions and asking Jev. A long video takes a minute or two.");

      let r;
      for (let attempt = 0; ; attempt++) {
        try {
          r = await request(`/run?v=${encodeURIComponent(id)}&c=${getCats().join(",")}&skip_known=${getSkipKnown() ? 1 : 0}`, 15 * 60 * 1000);
          break;
        } catch (e) {
          // connection dropped mid-run (server restarted): one retry is cheap, finished work is cached
          if (e.message === "unreachable" && attempt === 0) {
            status("Lost the connection to the server; retrying (finished steps are cached)…");
            if (await pingServer(() => {})) continue;
          }
          throw e;
        }
      }
      if (r.status !== 200 || !r.data) {
        const res = r.data || {};
        return showError(res.error || `The server answered HTTP ${r.status}: ${r.text || "(empty)"}`, res, id);
      }
      results.set(k, r.data);
      if (videoId() === id) renderResult(r.data);
    } catch (e) {
      if (e.message === "nogm") {
        showError("Your userscript manager didn't grant network access (GM_xmlhttpRequest). " +
          "Reinstall the script, and allow it to connect to 127.0.0.1 if asked.");
      } else if (e.message === "timeout") {
        showError("No answer after 15 minutes. The server may be stuck; check server.log.", {}, id);
      } else {
        showError("Lost the connection to the sbjev server and it didn't come back.", {}, id);
      }
    } finally {
      done();
    }
  }

  function onClick() {
    const id = videoId();
    if (!id) return showError("Open a video (a /watch page) first.");
    if (document.getElementById(BTN_ID).dataset.busy) return;
    if (document.getElementById("sbjev-panel")) return closePanel();
    if (results.has(key(id))) return renderResult(results.get(key(id)));
    run(id);
  }

  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePanel(); });
  document.addEventListener("yt-navigate-finish", () => { closePanel(); ensureButton(); });
  new MutationObserver(ensureButton).observe(document.documentElement, { childList: true, subtree: true });
  ensureButton();
  afterImportLoad();
})();
