/* Replay Desk UI */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = async (url, body) => {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(url, opt);
  const j = await r.json().catch(() => ({}));
  if (r.status === 404 && url.startsWith("/api/") && !j.error)
    throw new Error("Replay Desk needs a restart to finish updating: right-click the red tray icon by the clock, choose Quit, then open it again");
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
};
const enc = (id) => id.split("/").map(encodeURIComponent).join("/");
function fmt(t, frac = false) {
  t = Math.max(0, t || 0);
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  const ss = frac ? s.toFixed(2).padStart(5, "0") : String(Math.floor(s)).padStart(2, "0");
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + ss;
}
const fmtSize = (b) => b > 1e9 ? (b / 1e9).toFixed(2) + " GB" : (b / 1e6).toFixed(0) + " MB";
const fmtDate = (t) => new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 7000 : 4000);
}

/* ------------------------------------------------------------ navigation */
function show(view) {
  $$(".tabs button").forEach(b => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach(v => v.classList.toggle("active", v.id === "view-" + view));
  if (view === "library") loadLibrary();
  if (view !== "editor" && !$("#video").paused) $("#video").pause();
  if (view !== "compile") cpStop();
  if (view === "compile") renderCompile();
  if (view !== "library") setSelectMode(false);
}
$$(".tabs button").forEach(b => b.onclick = () => show(b.dataset.view));
document.addEventListener("click", e => { const g = e.target.closest("[data-goto]"); if (g) show(g.dataset.goto); });

/* ================================================================ CAPTURE */
let settings = {}, lastEvent = 0, statusState = {};

async function loadSettings() {
  settings = await api("/api/settings");
  let dev = { outputs: [], inputs: [], monitors: [] };
  try { dev = await api("/api/devices"); } catch {}
  const fill = (sel, list, cur) => {
    sel.innerHTML = '<option value="">Windows default</option>' + list.map(n => `<option>${n.replace(/</g, "&lt;")}</option>`).join("");
    if (cur && !list.includes(cur)) sel.insertAdjacentHTML("beforeend", `<option>${cur}</option>`);
  };
  fill($("#selGame"), dev.outputs, settings.game_device);
  fill($("#selMic"), dev.inputs, settings.mic_device);
  $("#selMonitor").innerHTML = (dev.monitors || [{ index: 0, label: "Display 1" }]).map(m => `<option value="${m.index}">${m.label}</option>`).join("");
  const f = $("#settingsForm");
  for (const el of f.elements) {
    if (!el.name || !(el.name in settings)) continue;
    if (el.type === "checkbox") el.checked = !!settings[el.name];
    else el.value = settings[el.name];
  }
  updateSettingLabels();
  $("#kbReplay").textContent = settings.hotkey_replay;
  $("#kbRecord").textContent = settings.hotkey_record;
  $("#replayLen").textContent = fmt(settings.buffer_seconds);
  $("#statEnc").textContent = { nvenc: "NVENC", amf: "AMF", x264: "x264 CPU" }[settings.encoder] || settings.encoder;
}
function updateSettingLabels() {
  const f = $("#settingsForm");
  $("#micVolVal").textContent = Math.round(f.mic_volume.value * 100) + "%";
  const off = +f.audio_offset_ms.value;
  $("#offVal").textContent = (off > 0 ? "+" : "") + off + " ms";
}
let saveTimer;
function collectSettings() {
  const out = {};
  for (const el of $("#settingsForm").elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") out[el.name] = el.checked;
    else if (el.type === "range" || ["monitor", "fps", "buffer_seconds"].includes(el.name)) out[el.name] = +el.value;
    else out[el.name] = el.value;
  }
  return out;
}
function queueSave(delay = 400) {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      const old = settings;
      settings = await api("/api/settings", collectSettings());
      $("#settingsSaved").textContent = "Saved ✓";
      setTimeout(() => $("#settingsSaved").textContent = "", 1500);
      $("#kbReplay").textContent = settings.hotkey_replay;
      $("#kbRecord").textContent = settings.hotkey_record;
      $("#replayLen").textContent = fmt(settings.buffer_seconds);
      $("#statEnc").textContent = { nvenc: "NVENC", amf: "AMF", x264: "x264 CPU" }[settings.encoder];
      const capKeys = ["monitor", "fps", "quality", "encoder", "game_audio", "game_device", "mic_enabled", "mic_device", "mic_volume", "audio_offset_ms", "output_dir"];
      if (capKeys.some(k => old[k] !== settings[k]) && statusState.buffer_running) toast("Restarting capture with new settings…");
    } catch (e) { toast("Couldn't save settings: " + e.message, "error"); }
  }, delay);
}
$("#settingsForm").addEventListener("input", e => { updateSettingLabels(); if (!e.target.classList.contains("hotkey")) queueSave(e.target.type === "range" ? 700 : 300); });
$("#settingsForm").addEventListener("submit", e => e.preventDefault());

/* hotkey capture fields */
const KEYNAME = (e) => {
  const k = e.key;
  if (/^F\d{1,2}$/.test(k)) return k;
  if (/^[a-z0-9]$/i.test(k)) return k.toUpperCase();
  const map = { Home: "Home", End: "End", Insert: "Insert", Delete: "Delete", PageUp: "PageUp", PageDown: "PageDown", Pause: "Pause", ScrollLock: "ScrollLock", " ": "Space" };
  if (e.code && e.code.startsWith("Numpad") && /\d/.test(e.code.slice(6))) return "Num" + e.code.slice(6);
  return map[k] || null;
};
$$(".hotkey").forEach(inp => {
  inp.addEventListener("focus", () => { inp.classList.add("listening"); inp.dataset.old = inp.value; inp.value = "Press keys…"; });
  inp.addEventListener("blur", () => { inp.classList.remove("listening"); if (inp.value === "Press keys…") inp.value = inp.dataset.old; });
  inp.addEventListener("keydown", e => {
    e.preventDefault();
    if (e.key === "Escape") { inp.value = inp.dataset.old; inp.blur(); return; }
    const k = KEYNAME(e);
    if (!k) return;
    const parts = [];
    if (e.ctrlKey) parts.push("Ctrl");
    if (e.altKey) parts.push("Alt");
    if (e.shiftKey) parts.push("Shift");
    parts.push(k);
    if (parts.length === 1 && !/^F\d+$/.test(k) && !k.startsWith("Num") && !["Pause", "ScrollLock", "Insert", "Home", "End", "PageUp", "PageDown"].includes(k)) {
      toast("Use a modifier (Ctrl/Alt/Shift) with letter keys so games don't trigger it by accident", "error");
      return;
    }
    inp.value = parts.join("+");
    inp.dataset.old = inp.value;
    inp.blur();
    queueSave(0);
  });
});

$("#btnReplay").onclick = () => api("/api/replay/save", {});
$("#btnOverlay").onclick = () => api("/api/overlay/toggle", {}).catch(e => toast(e.message, "error"));
$("#btnRecord").onclick = () => api("/api/record/toggle", {});
$("#btnBuffer").onclick = () => api("/api/buffer/" + (statusState.buffer_running ? "stop" : "start"), {});

const recent = [];
function renderRecent() {
  const el = $("#recentList");
  if (!recent.length) return;
  el.innerHTML = "";
  recent.slice(0, 4).forEach(r => {
    const d = document.createElement("div");
    d.className = "recent";
    d.innerHTML = `<img src="/thumb/${enc(r.id)}?t=${r.t}" onerror="this.style.visibility='hidden'"><div><div class="r-name"></div><div class="r-meta">${r.folder.slice(0, -1)} · ${new Date(r.t * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</div></div><button class="btn small">Edit</button>`;
    d.querySelector(".r-name").textContent = r.name;
    d.onclick = () => openEditor(r.id);
    el.appendChild(d);
  });
}
async function seedRecent() {
  try {
    const lib = await api("/api/library");
    lib.items.filter(i => i.folder !== "Clips").slice(0, 6).reverse().forEach(i => recent.unshift({ id: i.id, name: i.name, folder: i.folder, t: i.mtime }));
    renderRecent();
  } catch {}
}

function handleEvent(ev) {
  if (ev.kind === "saved" && ev.path) {
    const parts = ev.path.replace(/\\/g, "/").split("/");
    const name = parts.pop(), folder = parts.pop();
    recent.unshift({ id: folder + "/" + name, name: name.replace(/\.mp4$/, ""), folder, t: ev.t });
    renderRecent();
    toast(ev.msg);
    if (folder === "Replays") { $("#btnReplay").classList.remove("flash"); void $("#btnReplay").offsetWidth; $("#btnReplay").classList.add("flash"); }
    if ($("#view-library").classList.contains("active")) loadLibrary();
  } else if (ev.kind === "error") toast(ev.msg, "error");
  else if (ev.kind === "recording") toast(ev.msg, "rec");
  else if (ev.kind === "update") toast(ev.msg);
  else if (ev.kind === "recording_stopped") toast(ev.msg, "rec");
}

async function poll() {
  try {
    const s = await api("/api/status?since=" + lastEvent);
    statusState = s;
    s.events.forEach(ev => { lastEvent = Math.max(lastEvent, ev.id); handleEvent(ev); });
    renderStatus(s);
  } catch {
    $("#liveText").textContent = "App not responding";
    $("#livePill").className = "live err";
  }
  setTimeout(poll, 400);
}
let updDismissed = null;
function renderUpdate(u) {
  if (!u) return;
  $("#verText").textContent = "Version " + u.current + (u.installed_build ? "" : " (source)");
  const show = u.available && updDismissed !== u.version;
  $("#updBanner").classList.toggle("hidden", !show);
  if (!show) return;
  $("#updTitle").textContent = `Replay Desk ${u.version} is available`;
  if ($("#updNotes").dataset.v !== u.version) { $("#updNotes").textContent = u.notes || "Improvements and fixes."; $("#updNotes").dataset.v = u.version; }
  $("#updProg").classList.toggle("hidden", !u.downloading);
  $("#updFill").style.width = (u.progress * 100).toFixed(0) + "%";
  $("#updStatus").textContent = u.downloading ? `Downloading… ${Math.round(u.progress * 100)}%` : (u.error && !u.checking ? u.error : (u.ready && settings.auto_update ? "Installs next time the app opens" : ""));
  $("#updNow").disabled = u.downloading;
  $("#updLater").classList.toggle("hidden", u.downloading);
}
$("#updNotesBtn").onclick = () => $("#updNotes").classList.toggle("hidden");
$("#updLater").onclick = () => { updDismissed = statusState.update && statusState.update.version; $("#updBanner").classList.add("hidden"); };
$("#updNow").onclick = async () => {
  try { await api("/api/update/install", {}); $("#updStatus").textContent = "Starting download…"; }
  catch (e) { toast(e.message, "error"); }
};
$("#btnCheckUpd").onclick = async () => {
  $("#btnCheckUpd").textContent = "Checking…";
  try {
    const u = await api("/api/update/check", {});
    updDismissed = null;
    if (u.available) toast(`Replay Desk ${u.version} is available`);
    else if (u.error) toast("Couldn't check for updates: " + u.error, "error");
    else toast("You're on the latest version (" + u.current + ")");
  } finally { $("#btnCheckUpd").textContent = "Check for updates"; }
};

function renderStatus(s) {
  renderUpdate(s.update);
  const pill = $("#livePill");
  if (s.recording) { pill.className = "live rec"; $("#liveText").textContent = "REC " + fmt(s.recording_seconds); }
  else if (s.buffer_running) { pill.className = "live on"; $("#liveText").textContent = "Buffer on · " + fmt(s.buffer_seconds); }
  else if (s.error) { pill.className = "live err"; $("#liveText").textContent = "Capture stopped"; }
  else { pill.className = "live"; $("#liveText").textContent = "Buffer off"; }

  const st = $("#bufState");
  st.textContent = s.buffer_running ? "Running" : (s.error ? "Stopped" : "Off");
  st.className = "big-status " + (s.buffer_running ? "on" : "off");
  $("#btnBuffer").textContent = s.buffer_running ? "Stop buffer" : "Start buffer";
  $("#bufFill").style.width = Math.min(100, 100 * s.buffer_seconds / s.buffer_target) + "%";
  $("#bufText").textContent = fmt(s.buffer_seconds) + " / " + fmt(s.buffer_target);
  $("#statFps").textContent = s.buffer_running ? s.fps.toFixed(0) + " fps" : "— fps";
  $("#statDrop").textContent = s.dropped;

  const am = $("#audioMeters");
  const keys = Object.keys(s.audio || {});
  if (!s.audio_lib) am.innerHTML = '<div class="hint">Audio capture is unavailable — reinstall Replay Desk.</div>';
  else if (!keys.length) am.innerHTML = s.buffer_running ? '<div class="hint">Audio recording is turned off.</div>' : "";
  else {
    if (am.dataset.keys !== keys.join()) {
      am.innerHTML = keys.map(k => `<div class="am" data-k="${k}"><span>${k === "game" ? "Game audio" : "Mic"}</span><div class="bar"><div></div></div><span class="st"></span></div>`).join("");
      am.dataset.keys = keys.join();
    }
    keys.forEach(k => {
      const a = s.audio[k], row = am.querySelector(`[data-k="${k}"]`);
      row.querySelector(".bar div").style.width = Math.min(100, Math.sqrt(a.level) * 100) + "%";
      const stEl = row.querySelector(".st");
      stEl.textContent = a.ok ? `${(a.rate / 1000).toFixed(1)} kHz · ${a.channels}ch` : (a.error || "error");
      stEl.title = a.error || "";
      stEl.classList.toggle("bad", !a.ok);
    });
  }
  const eb = $("#errBox");
  if (s.error) { eb.textContent = s.error; eb.classList.remove("hidden"); } else eb.classList.add("hidden");

  const rb = $("#btnRecord");
  rb.classList.toggle("active", !!s.recording);
  $("#recTitle").textContent = s.recording ? "Stop recording" : "Start recording";
  $("#recTime").textContent = s.recording ? fmt(s.recording_seconds) : "Full session, any length";

  for (const [field, combo] of [["hotkey_replay", settings.hotkey_replay], ["hotkey_record", settings.hotkey_record], ["hotkey_overlay", settings.hotkey_overlay]]) {
    const el = $(`.hk-status[data-for="${field}"]`);
    const v = (s.hotkeys || {})[combo];
    if (!el) continue;
    if (!v) { el.textContent = ""; continue; }
    el.textContent = v === "ok" ? "✓ Active" : "⚠ " + v;
    el.classList.toggle("bad", v !== "ok");
  }
}

/* ================================================================ LIBRARY */
let libItems = [], libFilter = "all";
async function loadLibrary() {
  try {
    const r = await api("/api/library");
    libItems = r.items;
    renderLibrary();
  } catch (e) { toast("Couldn't load library: " + e.message, "error"); }
}
function renderLibrary() {
  const q = $("#libSearch").value.toLowerCase();
  const items = libItems.filter(i => (libFilter === "all" || i.folder === libFilter) && (!q || i.name.toLowerCase().includes(q)));
  $("#libCount").textContent = items.length + " video" + (items.length === 1 ? "" : "s");
  const g = $("#libGrid");
  if (!items.length) { g.innerHTML = '<div class="empty" style="grid-column:1/-1">Nothing here yet. Save a replay with ' + (settings.hotkey_replay || "the hotkey") + " while you play.</div>"; return; }
  g.innerHTML = "";
  g.classList.toggle("selecting", selecting);
  for (const it of items) {
    const c = document.createElement("div");
    const pickIdx = cp.items.findIndex(x => x.id === it.id);
    c.className = "vcard" + (selecting && pickIdx >= 0 ? " picked" : "");
    c.innerHTML = `
      <div class="vthumb" style="background-image:url('/thumb/${enc(it.id)}?m=${it.mtime}')">
        <span class="kind ${it.folder}">${it.folder.slice(0, -1)}</span><span class="dur mono">${fmt(it.duration)}</span></div>
      <div class="vbody"><div class="vname"></div>
        <div class="vmeta"><span>${fmtDate(it.mtime)}</span><span>${fmtSize(it.size)}</span>${it.height ? `<span>${it.height}p</span>` : ""}</div>
        <div class="vactions"><button class="btn" data-a="edit">Edit</button><button class="btn" data-a="rename">Rename</button><button class="btn" data-a="reveal">Folder</button><button class="btn del" data-a="delete">Delete</button></div>
      </div>`;
    c.querySelector(".vname").textContent = it.name;
    c.querySelector(".vname").title = it.name;
    if (selecting) {
      c.querySelector(".vthumb").insertAdjacentHTML("beforeend", `<span class="pick-num">${pickIdx >= 0 ? pickIdx + 1 : ""}</span>`);
      c.onclick = () => togglePick(it);
      g.appendChild(c);
      continue;
    }
    c.onclick = async (e) => {
      const a = e.target.dataset.a;
      if (!a || a === "edit") return openEditor(it.id);
      e.stopPropagation();
      if (a === "reveal") api("/api/file/reveal", { f: it.id });
      if (a === "rename") {
        const n = prompt("New name", it.name);
        if (n && n !== it.name) { try { await api("/api/file/rename", { f: it.id, name: n }); loadLibrary(); } catch (err) { toast(err.message, "error"); } }
      }
      if (a === "delete" && confirm(`Delete "${it.name}"? It goes to the Recycle Bin.`)) {
        await api("/api/file/delete", { f: it.id });
        loadLibrary();
      }
    };
    g.appendChild(c);
  }
}
$$("#libFilter button").forEach(b => b.onclick = () => { libFilter = b.dataset.f; $$("#libFilter button").forEach(x => x.classList.toggle("active", x === b)); renderLibrary(); });
$("#libSearch").oninput = renderLibrary;
$("#btnOpenFolder").onclick = () => api("/api/file/reveal", {});

/* ---- pick clips for a compilation ---- */
let selecting = false;
function setSelectMode(on) {
  selecting = on;
  $("#selectBar").classList.toggle("hidden", !on);
  $("#selectHint").classList.toggle("hidden", !on);
  $("#btnSelectMode").classList.toggle("hidden", on);
  if ($("#view-library").classList.contains("active")) renderLibrary();
  updateSelectBar();
}
function togglePick(it) {
  const i = cp.items.findIndex(x => x.id === it.id);
  if (i >= 0) cp.items.splice(i, 1);
  else cp.items.push({ id: it.id, name: it.name, duration: it.duration, mtime: it.mtime, height: it.height });
  renderLibrary();
  updateSelectBar();
}
function updateSelectBar() {
  const n = cp.items.length;
  $("#selCount").textContent = n + " selected";
  $("#selDur").textContent = n ? fmt(cp.items.reduce((a, x) => a + x.duration, 0)) + " total" : "";
  $("#selBuild").disabled = n < 2;
  $("#selBuild").title = n < 2 ? "Pick at least two videos" : "";
}
$("#btnSelectMode").onclick = () => { if (libFilter === "all" && libItems.some(i => i.folder === "Clips")) { libFilter = "Clips"; $$("#libFilter button").forEach(x => x.classList.toggle("active", x.dataset.f === "Clips")); } setSelectMode(true); };
$("#selCancel").onclick = () => setSelectMode(false);
$("#selClear").onclick = () => { cp.items = []; renderLibrary(); updateSelectBar(); };
$("#selBuild").onclick = () => { setSelectMode(false); show("compile"); };

/* ================================================================= EDITOR */
const video = $("#video");
const ed = { id: null, info: null, dur: 0, fps: 60, segs: [], selSeg: -1, sel: [0, 0], zoom: 1, previewing: false, previewIdx: 0, job: null };

async function openEditor(id) {
  $("#tabEditor").disabled = false;
  show("editor");
  ed.id = id;
  const info = await api("/api/info?f=" + encodeURIComponent(id));
  ed.info = info;
  ed.dur = info.duration || 0;
  ed.fps = info.fps || 60;
  ed.segs = [];
  ed.selSeg = -1;
  ed.sel = [0, ed.dur];
  ed.zoom = 1;
  $("#zoom").value = 1;
  stopPreview();
  const name = id.split("/").pop().replace(/\.mp4$/, "");
  $("#edName").textContent = name;
  $("#edMeta").textContent = [info.width && `${info.width}×${info.height}`, info.fps && `${Math.round(info.fps)} fps`, fmt(ed.dur)].filter(Boolean).join(" · ");
  $("#exName").value = name + "_clip";
  $("#tcDur").textContent = fmt(ed.dur, true);
  video.src = "/media/" + enc(id);
  video.playbackRate = +$("#rate").value;
  $("#strip").style.backgroundImage = "none";
  const strip = new Image();
  strip.onload = () => $("#strip").style.backgroundImage = `url('${strip.src}')`;
  strip.src = "/api/filmstrip?count=60&f=" + encodeURIComponent(id);
  renderAudioCtl();
  layoutTimeline();
  renderSegs();
  $("#exProg").classList.add("hidden");
}

function renderAudioCtl() {
  const el = $("#audioCtl");
  const tracks = ed.info.audio_tracks || [];
  let rows;
  if (tracks.length >= 3) rows = [{ i: 1, label: "Game" }, { i: 2, label: "Mic" }];
  else if (tracks.length >= 1) rows = [{ i: 0, label: "Volume" }];
  else { el.innerHTML = '<div class="hint">This video has no audio.</div>'; return; }
  el.innerHTML = rows.map(r => `<div class="vol" data-i="${r.i}"><span>${r.label}</span><input type="range" min="0" max="2" step="0.05" value="1"><span class="mono">100%</span></div>`).join("");
  $$(".vol", el).forEach(row => {
    const inp = row.querySelector("input"), out = row.querySelector(".mono");
    inp.oninput = () => { out.textContent = inp.value == 0 ? "Muted" : Math.round(inp.value * 100) + "%"; row.classList.toggle("muted", inp.value == 0); };
    row.querySelector("span").onclick = () => { inp.value = inp.value == 0 ? 1 : 0; inp.oninput(); };
    row.querySelector("span").style.cursor = "pointer";
    row.querySelector("span").title = "Click to mute / unmute";
  });
}
function volumes() {
  const v = {};
  $$("#audioCtl .vol").forEach(r => v[r.dataset.i] = +r.querySelector("input").value);
  return v;
}

/* ---- timeline geometry ---- */
const tlScroll = $("#timelineScroll"), tlInner = $("#tlInner");
function tlWidth() { return tlScroll.clientWidth * ed.zoom; }
function t2x(t) { return ed.dur ? (t / ed.dur) * tlWidth() : 0; }
function x2t(x) { return Math.max(0, Math.min(ed.dur, (x / tlWidth()) * ed.dur)); }
function eventT(e) { const r = tlInner.getBoundingClientRect(); return x2t(e.clientX - r.left); }

function layoutTimeline() {
  tlInner.style.width = tlWidth() + "px";
  // ruler
  const ruler = $("#ruler");
  const pxPerSec = tlWidth() / Math.max(0.01, ed.dur);
  const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
  const step = steps.find(s => s * pxPerSec >= 80) || 3600;
  let html = "";
  for (let t = 0; t <= ed.dur; t += step) html += `<div class="tick" style="left:${t2x(t)}px">${fmt(t, step < 1)}</div>`;
  ruler.innerHTML = html;
  renderSelection();
  renderSegs(false);
  renderPlayhead();
}
function renderSelection() {
  const [a, b] = ed.sel;
  const el = $("#selection");
  el.style.left = t2x(a) + "px";
  el.style.width = Math.max(2, t2x(b) - t2x(a)) + "px";
  $("#selInfo").textContent = `Selection ${fmt(a, true)} → ${fmt(b, true)}  (${fmt(b - a, true)})`;
}
function renderPlayhead() {
  $("#playhead").style.left = t2x(video.currentTime) + "px";
  $("#tcNow").textContent = fmt(video.currentTime, true);
}
function renderSegs(list = true) {
  const layer = $("#segLayer");
  layer.innerHTML = "";
  ed.segs.forEach((s, i) => {
    const shade = document.createElement("div");
    shade.className = "seg-shade";
    shade.style.left = t2x(s[0]) + "px";
    shade.style.width = Math.max(2, t2x(s[1]) - t2x(s[0])) + "px";
    layer.appendChild(shade);
    const d = document.createElement("div");
    d.className = "seg" + (i === ed.selSeg ? " sel" : "");
    d.style.left = t2x(s[0]) + "px";
    d.style.width = Math.max(4, t2x(s[1]) - t2x(s[0])) + "px";
    d.innerHTML = `<div class="eh l"></div>${i + 1}<div class="eh r"></div>`;
    d.addEventListener("mousedown", e => {
      e.stopPropagation();
      const edge = e.target.classList.contains("l") ? 0 : e.target.classList.contains("r") ? 1 : -1;
      ed.selSeg = i;
      renderSegs();
      if (edge < 0) { seek(s[0]); return; }
      dragEdge(e, t => {
        if (edge === 0) s[0] = Math.min(t, s[1] - 0.1); else s[1] = Math.max(t, s[0] + 0.1);
        seek(s[edge]);
        renderSegs();
      });
    });
    layer.appendChild(d);
  });
  if (!list) return;
  const ul = $("#segList");
  ul.innerHTML = "";
  $("#segEmpty").classList.toggle("hidden", ed.segs.length > 0);
  ed.segs.forEach((s, i) => {
    const r = document.createElement("div");
    r.className = "seg-item" + (i === ed.selSeg ? " sel" : "");
    r.innerHTML = `<div class="n">${i + 1}</div><div class="t"><b class="mono">${fmt(s[0], true)} → ${fmt(s[1], true)}</b><span>${fmt(s[1] - s[0], true)} long</span></div>
      <div class="acts"><button data-a="up" title="Move up">▲</button><button data-a="down" title="Move down">▼</button><button data-a="del" title="Remove">✕</button></div>`;
    r.onclick = (e) => {
      const a = e.target.dataset.a;
      if (a === "del") { ed.segs.splice(i, 1); ed.selSeg = -1; }
      else if (a === "up" && i > 0) { [ed.segs[i - 1], ed.segs[i]] = [ed.segs[i], ed.segs[i - 1]]; ed.selSeg = i - 1; }
      else if (a === "down" && i < ed.segs.length - 1) { [ed.segs[i + 1], ed.segs[i]] = [ed.segs[i], ed.segs[i + 1]]; ed.selSeg = i + 1; }
      else if (!a) { ed.selSeg = i; seek(s[0]); }
      renderSegs();
    };
    ul.appendChild(r);
  });
  const total = ed.segs.reduce((a, s) => a + s[1] - s[0], 0);
  $("#segTotal").textContent = ed.segs.length ? `${ed.segs.length} · ${fmt(total, true)}` : "";
}

function dragEdge(downEvent, onMove) {
  const move = e => onMove(eventT(e));
  const up = () => { window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); };
  window.addEventListener("mousemove", move);
  window.addEventListener("mouseup", up);
}

/* scrub & selection handles */
tlInner.addEventListener("mousedown", e => {
  if (e.button !== 0) return;
  const h = e.target.closest(".selection .h");
  if (h) {
    const left = h.classList.contains("l");
    dragEdge(e, t => {
      if (left) ed.sel[0] = Math.min(t, ed.sel[1] - 0.05); else ed.sel[1] = Math.max(t, ed.sel[0] + 0.05);
      seek(left ? ed.sel[0] : ed.sel[1]);
      renderSelection();
    });
    return;
  }
  stopPreview();
  ed.selSeg = -1;
  renderSegs();
  seek(eventT(e));
  dragEdge(e, t => seek(t));
});
tlScroll.addEventListener("wheel", e => {
  if (!e.ctrlKey && Math.abs(e.deltaY) > Math.abs(e.deltaX) && ed.zoom > 1) { tlScroll.scrollLeft += e.deltaY; e.preventDefault(); }
  else if (e.ctrlKey) { e.preventDefault(); setZoom(ed.zoom * (e.deltaY < 0 ? 1.25 : 0.8)); }
}, { passive: false });
function setZoom(z) {
  const center = video.currentTime;
  ed.zoom = Math.max(1, Math.min(60, z));
  $("#zoom").value = ed.zoom;
  layoutTimeline();
  tlScroll.scrollLeft = t2x(center) - tlScroll.clientWidth / 2;
}
$("#zoom").oninput = e => setZoom(+e.target.value);
window.addEventListener("resize", () => ed.id && layoutTimeline());

function seek(t) {
  t = Math.max(0, Math.min(ed.dur, t));
  video.currentTime = t;
  renderPlayhead();
  // keep playhead visible when zoomed
  const x = t2x(t);
  if (x < tlScroll.scrollLeft + 20 || x > tlScroll.scrollLeft + tlScroll.clientWidth - 20) tlScroll.scrollLeft = x - tlScroll.clientWidth / 3;
}

/* ---- transport ---- */
function togglePlay() { if (video.paused) { video.play(); } else { video.pause(); stopPreview(); } }
$("#bPlay").onclick = togglePlay;
$("#bBack").onclick = () => seek(video.currentTime - 5);
$("#bFwd").onclick = () => seek(video.currentTime + 5);
$("#rate").onchange = e => video.playbackRate = +e.target.value;
function edges() {
  const pts = new Set([0, ed.dur, ed.sel[0], ed.sel[1]]);
  ed.segs.forEach(s => { pts.add(s[0]); pts.add(s[1]); });
  return [...pts].sort((a, b) => a - b);
}
$("#bPrevSeg").onclick = () => { const t = video.currentTime - 0.05; seek([...edges()].reverse().find(p => p < t) ?? 0); };
$("#bNextSeg").onclick = () => { const t = video.currentTime + 0.05; seek(edges().find(p => p > t) ?? ed.dur); };
video.addEventListener("play", () => $("#bPlay").textContent = "❚❚");
video.addEventListener("pause", () => $("#bPlay").textContent = "▶");

function setIn() { const t = video.currentTime; ed.sel = [t, Math.max(t + 0.05, ed.sel[1] > t ? ed.sel[1] : ed.dur)]; renderSelection(); }
function setOut() { const t = video.currentTime; ed.sel = [Math.min(ed.sel[0] < t ? ed.sel[0] : 0, t - 0.05), t]; ed.sel[0] = Math.max(0, ed.sel[0]); renderSelection(); }
function addSeg() {
  const [a, b] = ed.sel;
  if (b - a < 0.1) return toast("Selection is too short", "error");
  ed.segs.push([a, b]);
  ed.segs.sort((x, y) => x[0] - y[0]);
  ed.selSeg = ed.segs.findIndex(s => s[0] === a && s[1] === b);
  renderSegs();
  // next selection starts where this one ended
  ed.sel = [b, Math.min(ed.dur, b + Math.min(10, ed.dur - b))];
  if (ed.sel[1] - ed.sel[0] < 0.1) ed.sel = [0, ed.dur];
  renderSelection();
}
function splitSeg() {
  const t = video.currentTime;
  const i = ed.segs.findIndex(s => t > s[0] + 0.05 && t < s[1] - 0.05);
  if (i < 0) return toast("Put the playhead inside a segment to split it", "error");
  const [a, b] = ed.segs[i];
  ed.segs.splice(i, 1, [a, t], [t, b]);
  ed.selSeg = i + 1;
  renderSegs();
}
$("#bIn").onclick = setIn;
$("#bOut").onclick = setOut;
$("#bAdd").onclick = addSeg;
$("#bSplit").onclick = splitSeg;

/* ---- clip preview: play kept segments back-to-back ---- */
function clipSegs() { return ed.segs.length ? ed.segs : [ed.sel]; }
function startPreview() {
  ed.previewing = true;
  ed.previewIdx = 0;
  $("#clipBadge").classList.remove("hidden");
  $("#bPreview").textContent = "■ Stop preview";
  video.currentTime = clipSegs()[0][0];
  video.play();
}
function stopPreview() {
  if (!ed.previewing) return;
  ed.previewing = false;
  $("#clipBadge").classList.add("hidden");
  $("#bPreview").textContent = "▶ Preview clip";
}
$("#bPreview").onclick = () => { if (ed.previewing) { stopPreview(); video.pause(); } else startPreview(); };
function tick() {
  if (ed.id && $("#view-editor").classList.contains("active")) {
    if (ed.previewing && !video.paused) {
      const segs = clipSegs();
      const cur = segs[ed.previewIdx];
      if (cur && video.currentTime >= cur[1] - 0.01) {
        ed.previewIdx++;
        if (ed.previewIdx >= segs.length) { video.pause(); stopPreview(); }
        else video.currentTime = segs[ed.previewIdx][0];
      }
    }
    renderPlayhead();
  }
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);

/* ---- keyboard ---- */
document.addEventListener("keydown", e => {
  if (!$("#view-editor").classList.contains("active") || !ed.id) return;
  if (e.target.matches("input, select, textarea") && e.target.type !== "range") return;
  const f = 1 / (ed.fps || 60);
  const k = e.key;
  const handled = {
    " ": togglePlay, i: setIn, I: setIn, o: setOut, O: setOut, a: addSeg, A: addSeg, s: splitSeg, S: splitSeg,
    p: () => $("#bPreview").click(), P: () => $("#bPreview").click(),
    ArrowLeft: () => seek(video.currentTime - (e.shiftKey ? 5 : 1)), ArrowRight: () => seek(video.currentTime + (e.shiftKey ? 5 : 1)),
    ",": () => { video.pause(); seek(video.currentTime - f); }, ".": () => { video.pause(); seek(video.currentTime + f); },
    j: () => seek(video.currentTime - 5), l: () => seek(video.currentTime + 5),
    Home: () => seek(0), End: () => seek(ed.dur),
    Delete: () => { if (ed.selSeg >= 0) { ed.segs.splice(ed.selSeg, 1); ed.selSeg = -1; renderSegs(); } },
    Backspace: () => { if (ed.selSeg >= 0) { ed.segs.splice(ed.selSeg, 1); ed.selSeg = -1; renderSegs(); } },
  }[k];
  if (handled) { e.preventDefault(); handled(); }
});

/* ---- export ---- */
$("#bExport").onclick = async () => {
  const segs = clipSegs().map(s => [+s[0].toFixed(3), +s[1].toFixed(3)]);
  if (segs.length === 1 && segs[0][0] <= 0.01 && segs[0][1] >= ed.dur - 0.01 && !confirm("No segments or In/Out set — export the whole video?")) return;
  try {
    const r = await api("/api/export", { f: ed.id, segments: segs, volumes: volumes(), name: $("#exName").value,
      quality: $("#exQuality").value, resolution: $("#exRes").value || null });
    ed.job = r.job;
    $("#exProg").classList.remove("hidden");
    $("#exFill").style.width = "0";
    $("#exText").textContent = "Exporting…";
    $("#exText").className = "";
    $("#exCancel").classList.remove("hidden");
    $("#bExport").disabled = true;
    pollExport();
  } catch (e) { toast("Export failed: " + e.message, "error"); }
};
async function pollExport() {
  if (!ed.job) return;
  const j = await api("/api/export/" + ed.job).catch(() => null);
  if (!j) return setTimeout(pollExport, 600);
  $("#exFill").style.width = (j.progress * 100).toFixed(1) + "%";
  if (j.state === "running") { $("#exText").textContent = `Exporting… ${Math.round(j.progress * 100)}%`; return setTimeout(pollExport, 400); }
  $("#bExport").disabled = false;
  $("#exCancel").classList.add("hidden");
  ed.job = null;
  if (j.state === "done") {
    $("#exText").innerHTML = `<span class="ok">✓ Saved to Clips</span> · <button class="link" id="exReveal">Show in folder</button> · <button class="link" id="exOpen">Open</button>`;
    $("#exReveal").onclick = () => api("/api/file/reveal", { f: j.out });
    $("#exOpen").onclick = () => openEditor(j.out);
    toast("Clip exported: " + j.out.split("/").pop());
  } else if (j.state === "error") { $("#exText").textContent = "Failed: " + j.error; toast("Export failed: " + j.error, "error"); }
  else $("#exText").textContent = "Cancelled";
}
$("#exCancel").onclick = () => ed.job && api(`/api/export/${ed.job}/cancel`, {});

/* ================================================================ COMPILE */
const cp = { items: [], idx: 0, playing: false, transition: "cut", job: null, dragFrom: -1 };
const cpVideo = $("#cpVideo");

function cpTotal() {
  const sum = cp.items.reduce((a, x) => a + x.duration, 0);
  const t = +$("#cpTdur").value;
  return cp.transition === "xfade" ? Math.max(0, sum - t * Math.max(0, cp.items.length - 1)) : sum;
}
function renderCompile() {
  const has = cp.items.length > 0;
  $("#cpEmpty").classList.toggle("hidden", has);
  cp.idx = Math.min(cp.idx, Math.max(0, cp.items.length - 1));
  if (!$("#cpName").value) $("#cpName").value = "Compilation_" + new Date().toISOString().slice(0, 10);
  // order list
  const list = $("#cpList");
  list.innerHTML = has ? "" : '<div class="empty small">Add clips from the Library, then drag them into order here.</div>';
  cp.items.forEach((it, i) => {
    const r = document.createElement("div");
    r.className = "cp-item" + (i === cp.idx ? " cur" : "");
    r.draggable = true;
    r.innerHTML = `<span class="grip">⋮⋮</span><span class="n">${i + 1}</span><img src="/thumb/${enc(it.id)}?m=${it.mtime}">
      <div class="t"><b></b><span>${it.id.split("/")[0].slice(0, -1)} · ${fmt(it.duration)}${it.height ? " · " + it.height + "p" : ""}</span></div>
      <div class="acts"><button data-a="up" title="Move up">▲</button><button data-a="down" title="Move down">▼</button><button data-a="del" title="Remove">✕</button></div>`;
    r.querySelector("b").textContent = it.name;
    r.onclick = e => {
      const a = e.target.dataset.a;
      if (a === "del") cp.items.splice(i, 1);
      else if (a === "up" && i > 0) [cp.items[i - 1], cp.items[i]] = [cp.items[i], cp.items[i - 1]];
      else if (a === "down" && i < cp.items.length - 1) [cp.items[i + 1], cp.items[i]] = [cp.items[i], cp.items[i + 1]];
      else if (!a) { cpLoad(i, false); return; }
      cpStop(); renderCompile();
    };
    r.addEventListener("dragstart", e => { cp.dragFrom = i; r.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; });
    r.addEventListener("dragend", () => { r.classList.remove("dragging"); $$(".cp-item.over").forEach(x => x.classList.remove("over")); });
    r.addEventListener("dragover", e => { e.preventDefault(); $$(".cp-item.over").forEach(x => x.classList.remove("over")); r.classList.add("over"); });
    r.addEventListener("drop", e => {
      e.preventDefault();
      const from = cp.dragFrom;
      if (from < 0 || from === i) return;
      const [moved] = cp.items.splice(from, 1);
      cp.items.splice(i, 0, moved);
      cp.dragFrom = -1;
      cpStop(); renderCompile();
    });
    list.appendChild(r);
  });
  // proportional strip
  const total = cp.items.reduce((a, x) => a + x.duration, 0) || 1;
  $("#cpTrack").innerHTML = cp.items.map((it, i) => `<div class="blk${i === cp.idx ? " cur" : ""}" style="flex:${it.duration / total};background-image:url('/thumb/${enc(it.id)}?m=${it.mtime}')"><span>${i + 1}</span></div>`).join("") + '<div class="ph" id="cpPh"></div>';
  $("#cpTotal").textContent = fmt(cpTotal());
  $("#cpTdurRow").classList.toggle("hidden", cp.transition === "cut");
  $("#cpTdurVal").textContent = (+$("#cpTdur").value).toFixed(1) + " s";
  const res = $("#cpRes").value;
  const maxH = Math.max(0, ...cp.items.map(x => x.height || 0));
  $("#cpSummary").textContent = has ? `${cp.items.length} clips · ${fmt(cpTotal())} · ${res ? res + "p" : (maxH ? maxH + "p" : "original size")}. Clips with a different size get black bars. Saved to Compilations.` : "";
  $("#cpExport").disabled = cp.items.length < 2 || !!cp.job;
  if (has && (!cpVideo.src || cpVideo.dataset.id !== cp.items[cp.idx].id)) cpLoad(cp.idx, false);
  if (!has) { cpVideo.removeAttribute("src"); cpVideo.dataset.id = ""; cpVideo.load(); }
}
function cpLoad(i, play) {
  cp.idx = i;
  const it = cp.items[i];
  cpVideo.dataset.id = it.id;
  cpVideo.src = "/media/" + enc(it.id);
  $("#cpBadge").textContent = `Clip ${i + 1} of ${cp.items.length}`;
  $("#cpBadge").classList.remove("hidden");
  $$(".cp-item").forEach((r, k) => r.classList.toggle("cur", k === i));
  $$("#cpTrack .blk").forEach((b, k) => b.classList.toggle("cur", k === i));
  if (play) cpVideo.play().catch(() => {});
}
function cpStop() { cp.playing = false; if (!cpVideo.paused) cpVideo.pause(); }
$("#cpPlay").onclick = () => {
  if (!cp.items.length) return;
  if (cp.playing) { cpStop(); return; }
  cp.playing = true;
  if (cpVideo.ended || (cp.idx === cp.items.length - 1 && cpVideo.currentTime >= cpVideo.duration - 0.05)) cpLoad(0, true);
  else cpVideo.play();
};
cpVideo.addEventListener("ended", () => {
  if (cp.playing && cp.idx < cp.items.length - 1) cpLoad(cp.idx + 1, true);
  else cp.playing = false;
});
cpVideo.addEventListener("play", () => { cp.playing = true; $("#cpPlay").textContent = "❚❚"; });
cpVideo.addEventListener("pause", () => { if (!cpVideo.ended) cp.playing = false; $("#cpPlay").textContent = "▶"; });
function cpOffset(i) { return cp.items.slice(0, i).reduce((a, x) => a + x.duration, 0); }
cpVideo.addEventListener("timeupdate", () => {
  const t = cpOffset(cp.idx) + cpVideo.currentTime;
  $("#cpNow").textContent = fmt(t);
  const total = cp.items.reduce((a, x) => a + x.duration, 0) || 1;
  const ph = $("#cpPh");
  if (ph) ph.style.left = (100 * t / total) + "%";
});
$("#cpTrack").addEventListener("click", e => {
  if (!cp.items.length) return;
  const r = $("#cpTrack").getBoundingClientRect();
  const total = cp.items.reduce((a, x) => a + x.duration, 0);
  let t = (e.clientX - r.left) / r.width * total, i = 0;
  while (i < cp.items.length - 1 && t > cp.items[i].duration) { t -= cp.items[i].duration; i++; }
  const wasPlaying = cp.playing;
  if (i !== cp.idx) cpLoad(i, wasPlaying);
  const seekTo = () => { cpVideo.currentTime = Math.max(0, t); };
  if (cpVideo.readyState >= 1) seekTo(); else cpVideo.addEventListener("loadedmetadata", seekTo, { once: true });
});
$$("#cpTrans button").forEach(b => b.onclick = () => { cp.transition = b.dataset.t; $$("#cpTrans button").forEach(x => x.classList.toggle("active", x === b)); renderCompile(); });
$("#cpTdur").oninput = renderCompile;
$("#cpRes").onchange = renderCompile;
const goPick = () => { show("library"); setSelectMode(true); };
$("#cpAdd").onclick = goPick;
$("#cpPick").onclick = goPick;

$("#cpExport").onclick = async () => {
  try {
    const r = await api("/api/compile", { files: cp.items.map(x => x.id), name: $("#cpName").value, quality: $("#cpQuality").value,
      resolution: $("#cpRes").value || null, transition: cp.transition, transition_seconds: +$("#cpTdur").value });
    cp.job = r.job;
    $("#cpProg").classList.remove("hidden");
    $("#cpFill").style.width = "0";
    $("#cpText").textContent = "Exporting…";
    $("#cpCancel").classList.remove("hidden");
    $("#cpExport").disabled = true;
    pollCompile();
  } catch (e) { toast("Compile failed: " + e.message, "error"); }
};
async function pollCompile() {
  if (!cp.job) return;
  const j = await api("/api/export/" + cp.job).catch(() => null);
  if (!j) return setTimeout(pollCompile, 600);
  $("#cpFill").style.width = (j.progress * 100).toFixed(1) + "%";
  if (j.state === "running") { $("#cpText").textContent = `Exporting… ${Math.round(j.progress * 100)}%`; return setTimeout(pollCompile, 400); }
  cp.job = null;
  $("#cpExport").disabled = cp.items.length < 2;
  $("#cpCancel").classList.add("hidden");
  if (j.state === "done") {
    $("#cpText").innerHTML = `<span class="ok">✓ Saved to Compilations</span> · <button class="link" id="cpReveal">Show in folder</button> · <button class="link" id="cpOpen">Open in editor</button>`;
    $("#cpReveal").onclick = () => api("/api/file/reveal", { f: j.out });
    $("#cpOpen").onclick = () => openEditor(j.out);
    toast("Compilation exported: " + j.out.split("/").pop());
  } else if (j.state === "error") { $("#cpText").textContent = "Failed: " + j.error; toast("Compile failed: " + j.error, "error"); }
  else $("#cpText").textContent = "Cancelled";
}
$("#cpCancel").onclick = () => cp.job && api(`/api/export/${cp.job}/cancel`, {});

/* ------------------------------------------------------------------ boot */
loadSettings().then(seedRecent);
poll();
