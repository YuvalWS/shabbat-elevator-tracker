import { ApiClient, testServer } from "./api.js";
import { DEFAULT_API_BASE } from "./config.js";
import { LANGS, Translator, autoLang, loadStrings } from "./i18n.js";
import { nextArrival, position } from "./model.js";
import { Shaft } from "./shaft.js";
import { ScreenKeeper } from "./wake.js";

const SUPPORTED_API = 1;
const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- settings
const store = {
  get(k, d = null) { try { const v = localStorage.getItem("set." + k); return v === null ? d : v; } catch { return d; } },
  set(k, v) { try { if (v === null || v === undefined) localStorage.removeItem("set." + k); else localStorage.setItem("set." + k, v); } catch { /* private mode */ } },
};

function apiBaseFromUrl() {
  const p = new URLSearchParams(location.search).get("api");
  if (p !== null) { store.set("api", p); return p; }
  return null;
}
const apiBase = apiBaseFromUrl() ?? store.get("api") ?? DEFAULT_API_BASE;

// ?floor=3&lang=he&theme=dark&clock=12 preset the settings (e.g. for a wall tablet) and are saved
{
  const q = new URLSearchParams(location.search);
  const valid = { lang: ["auto", ...LANGS], theme: ["auto", "light", "dark"], clock: ["24", "12"], keepawake: ["1", "0"] };
  for (const k of Object.keys(valid)) if (valid[k].includes(q.get(k))) store.set(k, q.get(k));
  if (q.get("floor")) store.set("floor:" + apiBase, q.get("floor"));
}

const ui = {
  lang: store.get("lang", "auto"),
  theme: store.get("theme", "auto"),
  clock: store.get("clock", "24"),
  advanced: store.get("advanced", "0") === "1",
  keepawake: store.get("keepawake", "1") === "1", // on by default
  video: store.get("video", "1") === "1", // the fallback for when the browser cannot keep the screen on

  floor: store.get("floor:" + apiBase),
  picking: false, // floor picker reopened to change the floor
};

let tr = null;
let state = null;
let transport = "connecting";
let screenMode = "off";
let lastFloorsKey = "";
const shaft = new Shaft($("shaft"));
const keeper = new ScreenKeeper((m) => { screenMode = m; $("screen-mode").textContent = t("screen." + m); });

// ---------------------------------------------------------------- i18n
async function applyLanguage() {
  const lang = ui.lang === "auto" ? autoLang() : ui.lang;
  const strings = await loadStrings(lang);
  tr = new Translator(lang, strings);
  document.documentElement.lang = lang;
  document.documentElement.dir = tr.dir;
  for (const e of document.querySelectorAll("[data-i18n]")) e.textContent = tr.t(e.dataset.i18n);
  for (const e of document.querySelectorAll("[data-i18n-title]")) {
    e.title = tr.t(e.dataset.i18nTitle);
    e.setAttribute("aria-label", e.title);
  }
  updateThemeButton(); // titles that depend on the current state, set after the generic ones above
  updateFullscreenButton();
  $("btn-lang").textContent = lang.toUpperCase();
  $("btn-lang").title = tr.t("header.language");
  const sel = $("set-lang");
  sel.textContent = "";
  sel.add(new Option(tr.t("settings.language_auto"), "auto"));
  for (const l of LANGS) {
    const s = await loadStrings(l);
    sel.add(new Option(s["_meta.name"], l));
  }
  sel.value = ui.lang;
  lastFloorsKey = "";
  shaft.key = "";
  if (state) onState(state);
  render();
}

function t(k, v) { return tr ? tr.t(k, v) : k; }

// ---------------------------------------------------------------- theme & tier
function applyTheme() {
  if (ui.theme === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.dataset.theme = ui.theme;
  $("set-theme").value = ui.theme;
  updateThemeButton();
}

/** The theme being shown right now: the chosen one, or the system's when "automatic". */
function effectiveTheme() {
  if (ui.theme !== "auto") return ui.theme;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function updateThemeButton() {
  const b = $("btn-theme");
  if (!tr) return;
  b.title = t(effectiveTheme() === "dark" ? "header.theme_light" : "header.theme_dark");
  b.setAttribute("aria-label", b.title);
}

// ---------------------------------------------------------------- full screen
const fsElement = () => document.fullscreenElement || document.webkitFullscreenElement || null;

function updateFullscreenButton() {
  const b = $("btn-fullscreen");
  const on = !!fsElement();
  b.setAttribute("aria-pressed", String(on));
  if (!tr) return;
  b.title = t(on ? "header.fullscreen_exit" : "header.fullscreen");
  b.setAttribute("aria-label", b.title);
}

function toggleFullscreen() {
  const root = document.documentElement;
  if (fsElement()) (document.exitFullscreen || document.webkitExitFullscreen).call(document);
  else {
    const p = (root.requestFullscreen || root.webkitRequestFullscreen).call(root);
    if (p && p.catch) p.catch(() => { /* refused by the browser */ });
  }
}

function applyTier() {
  const w = window.innerWidth, h = window.innerHeight;
  const tier = w < 360 || h < 500 ? "xs" : w >= 900 && h >= 600 ? "lg" : "md";
  const orient = w > h ? "land" : "port";
  const root = document.documentElement.dataset;
  if (root.tier !== tier || root.orient !== orient) {
    root.tier = tier;
    root.orient = orient;
    render();
  }
}

// ---------------------------------------------------------------- formatting
function tz() { return state?.calendar?.timezone || undefined; }

function fmtTime(ms, withSeconds = true) {
  return new Intl.DateTimeFormat(tr?.lang || "en", {
    hour: "2-digit", minute: "2-digit", second: withSeconds ? "2-digit" : undefined,
    hour12: ui.clock === "12", timeZone: tz(),
  }).format(new Date(ms));
}

function fmtDayTime(ms) {
  return new Intl.DateTimeFormat(tr?.lang || "en", {
    weekday: "short", hour: "2-digit", minute: "2-digit", hour12: ui.clock === "12", timeZone: tz(),
  }).format(new Date(ms));
}

function fmtCountdown(sec) {
  sec = Math.max(0, Math.ceil(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const ss = String(s).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

// how long to keep showing the last known schedule while the server is unreachable
const STALE_HIDE_MS = 5 * 60 * 1000;

function fmtAgo(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 5) return t("ago.now");
  if (s < 90) return t("ago.seconds", { n: s });
  if (s < 5400) return t("ago.minutes", { n: Math.round(s / 60) });
  if (s < 172800) return t("ago.hours", { n: Math.round(s / 3600) });
  return t("ago.days", { n: Math.round(s / 86400) });
}

// ---------------------------------------------------------------- floors
function onRouteSet() {
  return new Set((state?.timeline?.stops || []).map((s) => s.floor));
}

function setFloor(f) {
  ui.floor = f;
  ui.picking = false;
  store.set("floor:" + apiBase, f);
  for (const c of document.querySelectorAll(".chip")) c.setAttribute("aria-checked", String(c.dataset.floor === f));
  shaft.key = "";
  render();
  if (f) $("btn-floor").focus({ preventScroll: true });
}

function openPicker(open) {
  ui.picking = open;
  render();
  if (open) document.querySelector('.chip[aria-checked="true"]')?.focus({ preventScroll: true });
}

/** The building name may be one string or a {lang: name} mapping. */
function buildingName(b) {
  const n = b?.name;
  if (!n || typeof n === "string") return n || "";
  return n[tr?.lang] || n.en || Object.values(n)[0] || "";
}

function buildFloors() {
  const floors = state.building.floors;
  const key = floors.join("|") + (tr?.lang || "");
  if (key === lastFloorsKey) return;
  lastFloorsKey = key;
  if (!ui.floor && state.building.default_floor) ui.floor = state.building.default_floor;
  if (ui.floor && !floors.includes(ui.floor)) ui.floor = null;
  const onRoute = onRouteSet();
  const chips = $("floor-chips");
  chips.textContent = "";
  for (const f of floors) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "chip" + (onRoute.has(f) ? "" : " skipped");
    b.dataset.floor = f; b.textContent = f;
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", String(f === ui.floor));
    b.setAttribute("aria-label", t("floor.name", { floor: f }));
    b.addEventListener("click", () => setFloor(f));
    chips.appendChild(b);
  }
}

// ---------------------------------------------------------------- state
function onState(s) {
  state = s;
  if (s.api_version !== SUPPORTED_API) {
    showMessage(t("status.server_version", { v: s.api_version }), "bad");
    return;
  }
  const name = buildingName(s.building);
  $("title").textContent = name || t("app.title");
  document.title = (name ? name + " · " : "") + t("app.title");
  $("version").textContent = t("footer.version", { v: s.version || "?" });
  buildFloors();
  renderShabbat();
  if (ui.advanced) renderDebug();
  render();
}

function showMessage(text, cls) {
  const m = $("message");
  m.hidden = !text;
  m.textContent = text || "";
  m.className = "message" + (cls ? " " + cls : "");
  $("next").classList.toggle("invalid", !!text);
}

function reasonText() {
  const p = state.prediction;
  if (p.reason === "outside_shabbat") {
    const ns = state.calendar?.next_span;
    return ns ? t("reason.outside_shabbat", { time: fmtDayTime(Date.parse(ns.window_start)) }) : t("reason.outside_shabbat_plain");
  }
  if (p.reason === "no_sync" && state.sync.state === "provisional") return t("reason.provisional");
  return t("reason." + (p.reason || "no_sync"));
}

function render() {
  if (!tr) return;
  if (!state) { showMessage(transport === "down" ? t("status.server_down") : t("app.loading"), transport === "down" ? "bad" : ""); updateDot(); return; }
  if (state.api_version !== SUPPORTED_API) return;
  const now = window.__api.now();
  const tl = state.timeline;
  const floors = state.building.floors;
  // server unreachable: the countdown runs on from the last state, marked as such, then is hidden
  const lostMs = transport === "down" ? Date.now() - (window.__api.lastContact || 0) : 0;
  const valid = state.prediction.valid && tl.anchor_time_ms != null && lostMs <= STALE_HIDE_MS;
  const pos = valid ? position(tl, floors, tl.anchor_time_ms, now) : null;

  // floor picker: big while no floor is chosen (or while changing it), otherwise a title + edit button
  const f = ui.floor;
  const picking = !f || ui.picking;
  $("floor-picker").hidden = !picking;
  $("floor-cancel").hidden = !f;
  $("next").hidden = picking;
  document.querySelector(".text-panel").classList.toggle("picking", picking);
  $("btn-floor").hidden = !f;
  renderNextTitle(f);
  const cd = $("countdown");
  if (!f) {
    showMessage("");
  } else if (!onRouteSet().has(f)) {
    showMessage(t("floor.not_on_route", { floor: f }), "warn");
  } else if (lostMs > STALE_HIDE_MS) {
    showMessage(t("reason.server_down", { ago: fmtAgo(lostMs) }), "bad");
  } else if (!valid) {
    showMessage(reasonText(), state.prediction.reason === "elevator_paused" ? "warn" : "");
  } else {
    const n = nextArrival(tl, tl.anchor_time_ms, f, now);
    showMessage("");
    if (n.here_until !== undefined) {
      cd.classList.add("here");
      $("countdown-value").textContent = t("next.here_now");
      $("next-at").textContent = t("next.doors_close_in", { countdown: fmtCountdown((n.here_until - now) / 1000) });
      $("next-then").textContent = t("next.then", { time: fmtTime(n.arrive_at) });
    } else {
      cd.classList.remove("here");
      $("countdown-value").textContent = fmtCountdown((n.arrive_at - now) / 1000);
      $("next-at").textContent = t("next.at", { time: fmtTime(n.arrive_at) });
      const after = nextArrival(tl, tl.anchor_time_ms, f, n.depart_at + 1);
      $("next-then").textContent = after ? t("next.then", { time: fmtTime(after.arrive_at) }) : "";
    }
    if (state.prediction.degraded) $("next-then").textContent = t("degraded." + state.prediction.degraded);
    if (lostMs) $("next-then").textContent = t("degraded.server_down", { ago: fmtAgo(lostMs) });
  }
  cd.classList.toggle("stale", !!lostMs);

  // status line
  const sy = state.sync;
  $("status-sync").textContent = sy.last_sync_at_ms
    ? t("status.synced_ago", { ago: fmtAgo(now - sy.last_sync_at_ms), time: fmtTime(sy.last_sync_at_ms) })
    : t("status.never_synced");
  const st = state.stream;
  const audio = $("status-audio");
  if (transport === "down") { audio.textContent = t("status.server_down"); audio.className = "bad"; }
  else if (st.state === "connected") { audio.textContent = t("status.audio_live"); audio.className = ""; }
  else if (st.state === "lost") {
    audio.textContent = t("status.audio_lost", { ago: fmtAgo(now - (st.last_audio_at_ms || Date.parse(st.since))) });
    audio.className = "bad";
  } else { audio.textContent = t("status.audio_connecting"); audio.className = "warn"; }

  // car line
  const car = $("car-line");
  if (pos && pos.kind === "stop") car.textContent = t("car.at_floor", { floor: pos.floor }) + " · " + t("doors." + pos.doors);
  else if (pos) car.textContent = t("car.moving", { from: pos.from, to: pos.to }) + " · " + t("direction." + pos.direction);
  else car.textContent = lostMs > STALE_HIDE_MS ? "" : t("state." + sy.state);

  renderRoute(pos);
  if (document.documentElement.dataset.tier === "lg") {
    shaft.build(floors, onRouteSet(), ui.floor, t("shaft.title"));
    shaft.update(pos, valid);
  }
  updateDot();
}

let nextTitleKey = "";
function renderNextTitle(f) {
  const key = (f || "") + (tr?.lang || "");
  if (key === nextTitleKey) return;
  nextTitleKey = key;
  const el = $("next-title");
  el.textContent = "";
  if (!f) return;
  // "Next arrival at floor {floor}" with the floor number emphasised
  const [before, after = ""] = t("next.title").split("{floor}");
  const num = document.createElement("bdi");
  num.className = "floor-num";
  num.textContent = f;
  el.append(before, num, after);
}

function updateDot() {
  const d = $("dot");
  let cls = "bad";
  if (transport !== "down" && state) {
    cls = state.stream.state === "connected" && state.sync.state === "locked" && state.sync.confirmed !== false ? "ok" : "warn";
  }
  d.className = "dot " + cls;
  d.title = t("transport." + transport);
}

let routeKey = "";
function renderRoute(pos) {
  const strip = $("route-strip");
  const stops = state.timeline.stops;
  if (!stops.length) return;
  const floors = state.building.floors;
  // start the strip at the lowest stop
  let i0 = 0;
  stops.forEach((s, i) => { if (floors.indexOf(s.floor) < floors.indexOf(stops[i0].floor)) i0 = i; });
  const order = stops.slice(i0).concat(stops.slice(0, i0));
  const key = order.map((s) => s.floor + s.visit).join(",") + ui.floor;
  if (key !== routeKey) {
    routeKey = key;
    strip.textContent = "";
    for (const s of order) {
      const d = document.createElement("span");
      d.className = "stop" + (s.floor === ui.floor ? " mine" : "");
      d.dataset.key = s.floor + "#" + s.visit;
      d.textContent = s.floor;
      strip.appendChild(d);
    }
  }
  let carKey = null, nextKey = null;
  if (pos) {
    if (pos.kind === "stop") carKey = pos.floor;
    else nextKey = pos.to;
  }
  for (const d of strip.children) {
    const f = d.dataset.key.split("#")[0];
    d.classList.toggle("car", carKey === f);
    d.classList.toggle("next-car", nextKey === f);
  }
}

function renderShabbat() {
  const box = $("shabbat-body");
  const cal = state.calendar;
  box.textContent = "";
  if (!cal || !cal.enabled) { box.textContent = t("shabbat.disabled"); return; }
  const span = cal.current_span || cal.next_span;
  const grid = document.createElement("div");
  grid.className = "shabbat-grid";
  const kv = (k, v, cls = "") => {
    const e = document.createElement("div"); e.className = "kv " + cls;
    const a = document.createElement("span"); a.className = "k"; a.textContent = k;
    const b = document.createElement("span"); b.className = "v"; b.textContent = v;
    e.append(a, b); grid.appendChild(e);
  };
  if (span) {
    kv(t("shabbat.candle_lighting"), fmtDayTime(Date.parse(span.start)), "main");
    kv(t("shabbat.havdalah"), fmtDayTime(Date.parse(span.end)), "main");
  }
  const ids = new Set();
  for (const d of span?.days || []) for (const id of d.ids) ids.add(id);
  for (const id of cal.today_ids || []) ids.add(id);
  ids.delete("shabbat"); // implied by the card
  const names = [...ids].filter((id) => tr.has("holiday." + id)).map((id) => t("holiday." + id));
  if (cal.parsha_ids?.length && tr.has("parsha." + cal.parsha_ids[0])) {
    kv(t("shabbat.title"), t("shabbat.parsha", { name: cal.parsha_ids.map((p) => t("parsha." + p)).join(" ") }));
  } else if (names.length) {
    kv(t("shabbat.title"), names.join(", "));
  }
  const hd = cal.hebrew_date;
  if (hd) {
    const heb = tr.lang === "he" && hd.day_he; // Hebrew numerals (from hdate) in the Hebrew UI
    kv("", t("shabbat.hebrew_date", { day: heb ? hd.day_he : hd.day, month: t("month." + hd.month_id),
                                       year: heb ? hd.year_he : hd.year }), "hdate");
  }
  box.appendChild(grid);
  const line = document.createElement("p");
  line.className = "now-line";
  const badge = document.createElement("span");
  badge.className = "badge" + (cal.now?.issur_melacha ? " on" : "");
  badge.textContent = cal.now?.issur_melacha ? t("shabbat.now_on") : t("shabbat.now_off");
  line.appendChild(badge);
  if (names.length && cal.parsha_ids?.length) line.append(" " + names.join(", "));
  if (span) {
    const w = document.createElement("small");
    w.textContent = " " + t("shabbat.elevator_window", { start: fmtDayTime(Date.parse(span.window_start)), end: fmtDayTime(Date.parse(span.window_end)) });
    line.appendChild(w);
  }
  box.appendChild(line);
}

// ---------------------------------------------------------------- debug
let debugTimer = null;
async function renderDebug() {
  const dbg = $("debug");
  dbg.hidden = !ui.advanced;
  if (!ui.advanced || !state) return;
  const c = window.__api.clock;
  const sy = state.sync, cy = state.cycle, st = state.stream;
  const rows = [
    [t("debug.sync_state"), `${sy.state} (${t("state." + sy.state)})`],
    [t("debug.source"), sy.source || "-"],
    [t("debug.anchor"), state.timeline.anchor_time_ms ? fmtTime(state.timeline.anchor_time_ms) : "-"],
    [t("debug.cycle"), `${cy.seconds} / ${cy.configured_seconds} s`],
    [t("debug.missed"), String(sy.missed)],
    [t("debug.pending"), sy.pending ?? "-"],
    [t("debug.next_expected"), sy.next_expected_at ? fmtTime(Date.parse(sy.next_expected_at)) : "-"],
    [t("debug.clock"), `${Math.round(c.offsetMs)} ms / ${c.rtt == null ? "-" : Math.round(c.rtt) + " ms"} / x${c.rate}`],
    [t("debug.transport"), t("transport." + transport)],
    [t("debug.stream"), `${st.state} · ${t("debug.reconnects")} ${st.reconnects_total}`],
    [t("debug.level"), st.level_dbfs == null ? "-" : st.level_dbfs + " dBFS"],
    [t("settings.screen"), t("screen." + screenMode)],
  ];
  const dl = $("debug-list");
  dl.textContent = "";
  for (const [k, v] of rows) {
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    dl.append(dt, dd);
  }
  $("debug-raw").textContent = JSON.stringify(state, null, 1);
  if (!debugTimer) {
    const refresh = async () => {
      if (!ui.advanced) return;
      try {
        const ev = await window.__api.fetchJson("/events?limit=10");
        $("debug-events").textContent = ev.events.map((e) => `${e.at_iso}  ${e.type}  ${JSON.stringify(Object.fromEntries(Object.entries(e).filter(([k]) => !["at", "at_iso", "type"].includes(k))))}`).join("\n");
      } catch { /* ignore */ }
      if (state?.debug_enabled) {
        try {
          const d = await window.__api.fetchJson("/debug");
          $("debug-candidates").textContent = `score ${d.score_db} dB\n` + d.candidates.map((x) => `${new Date(x.onset * 1000).toISOString()}  ${x.duration}s  +${x.peak_db}dB  ${x.verdict}`).join("\n");
        } catch { $("debug-candidates").textContent = t("debug.hidden"); }
      } else {
        $("debug-candidates").textContent = t("debug.hidden");
      }
    };
    refresh();
    debugTimer = setInterval(refresh, 5000);
  }
}

// ---------------------------------------------------------------- controls
function setupControls() {
  $("btn-floor").addEventListener("click", () => openPicker(true));
  $("floor-cancel").addEventListener("click", () => openPicker(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && ui.picking && ui.floor) openPicker(false); });
  // one tap = the other theme than the one on screen (also when "automatic" is showing it);
  // "automatic" is chosen in Settings
  $("btn-theme").addEventListener("click", () => {
    ui.theme = effectiveTheme() === "dark" ? "light" : "dark";
    store.set("theme", ui.theme);
    applyTheme();
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", updateThemeButton);
  if (document.fullscreenEnabled || document.webkitFullscreenEnabled) {
    $("btn-fullscreen").hidden = false; // e.g. not on iPhone Safari
    $("btn-fullscreen").addEventListener("click", toggleFullscreen);
    document.addEventListener("fullscreenchange", updateFullscreenButton);
    document.addEventListener("webkitfullscreenchange", updateFullscreenButton);
  }
  const menu = $("lang-menu");
  $("btn-lang").addEventListener("click", async (e) => {
    if (!menu.hidden) { menu.hidden = true; return; }
    menu.textContent = "";
    const opts = [["auto", t("settings.language_auto")]];
    for (const l of LANGS) opts.push([l, (await loadStrings(l))["_meta.name"]]);
    for (const [v, name] of opts) {
      const b = document.createElement("button");
      b.type = "button"; b.setAttribute("role", "menuitemradio"); b.textContent = name;
      b.setAttribute("aria-checked", String(ui.lang === v));
      b.addEventListener("click", () => { menu.hidden = true; setLang(v); });
      menu.appendChild(b);
    }
    const r = e.currentTarget.getBoundingClientRect();
    menu.style.top = r.bottom + 4 + "px";
    if (document.documentElement.dir === "rtl") { menu.style.left = r.left + "px"; menu.style.right = ""; }
    else { menu.style.right = (window.innerWidth - r.right) + "px"; menu.style.left = ""; }
    menu.hidden = false;
  });
  document.addEventListener("click", (e) => { if (!menu.hidden && !menu.contains(e.target) && e.target !== $("btn-lang")) menu.hidden = true; });

  const dlg = $("settings");
  $("btn-settings").addEventListener("click", () => {
    $("api-base").value = apiBase;
    $("set-clock").value = ui.clock;
    $("set-advanced").checked = ui.advanced;
    $("set-keepawake").checked = ui.keepawake;
    $("set-video").checked = ui.video;
    $("screen-hint").hidden = window.isSecureContext; // the warning is only for plain http://
    $("advanced").open = false; // server and debug stay folded until asked for
    $("screen-mode").textContent = t("screen." + screenMode);
    $("test-result").textContent = "";
    dlg.showModal();
  });
  $("set-lang").addEventListener("change", (e) => setLang(e.target.value));
  $("set-theme").addEventListener("change", (e) => { ui.theme = e.target.value; store.set("theme", ui.theme); applyTheme(); });
  $("set-clock").addEventListener("change", (e) => { ui.clock = e.target.value; store.set("clock", ui.clock); renderShabbat(); render(); });
  $("set-keepawake").addEventListener("change", (e) => {
    ui.keepawake = e.target.checked;
    store.set("keepawake", ui.keepawake ? "1" : "0");
    if (ui.keepawake) keeper.enable(); else keeper.disable();
  });
  $("set-video").addEventListener("change", (e) => {
    ui.video = e.target.checked;
    store.set("video", ui.video ? "1" : "0");
    keeper.setVideo(ui.video);
  });
  $("set-advanced").addEventListener("change", (e) => {
    ui.advanced = e.target.checked; store.set("advanced", ui.advanced ? "1" : "0");
    $("debug").hidden = !ui.advanced;
    if (ui.advanced) renderDebug();
  });
  $("btn-test").addEventListener("click", async () => {
    const out = $("test-result");
    out.textContent = "…";
    const base = $("api-base").value.trim() || location.origin;
    const r = await testServer(base);
    if (r.ok) {
      let name = "";
      try { name = buildingName((await (await fetch(base.replace(/\/+$/, "") + "/api/v1/state")).json()).building); } catch { /* ignore */ }
      out.textContent = t("settings.test_ok", { name, version: r.health.version });
    } else out.textContent = r.reason === "mixed_content" ? t("settings.test_mixed") : t("settings.test_fail");
  });
  // Everything else applies as soon as it is changed; the server address needs a reload.
  const saveAndClose = () => {
    const v = $("api-base").value.trim();
    if (v === apiBase) { dlg.close(); return; }
    store.set("api", v);
    const u = new URL(location.href);
    u.searchParams.delete("api");
    location.href = u.toString();
  };
  $("btn-save").addEventListener("click", saveAndClose);
  $("btn-x").addEventListener("click", saveAndClose);
  window.addEventListener("resize", applyTier);
}

async function setLang(v) {
  ui.lang = v;
  store.set("lang", v);
  await applyLanguage();
}

// ---------------------------------------------------------------- start
async function main() {
  applyTheme();
  applyTier();
  setupControls();
  await applyLanguage();
  const api = new ApiClient(apiBase, onState, (tp) => { transport = tp; render(); if (ui.advanced) renderDebug(); });
  window.__api = api;
  api.start();
  keeper.allowVideo = ui.video;
  if (ui.keepawake) keeper.enable();
  updateThemeButton();
  updateFullscreenButton();
  $("debug").hidden = !ui.advanced;
  setInterval(render, 250);
  setInterval(() => { if (state) renderShabbat(); }, 60000);
}

main();
