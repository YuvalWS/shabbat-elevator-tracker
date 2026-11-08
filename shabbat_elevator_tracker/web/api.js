// REST + Server-Sent Events client with clock-offset tracking and polling fallback.

const API = "/api/v1";
const SILENCE_MS = 40000; // pings come every 15 s

export class ApiClient {
  constructor(base, onState, onTransport) {
    this.base = (base || "").replace(/\/+$/, "");
    this.onState = onState;
    this.onTransport = onTransport;
    this.transport = "connecting"; // sse | poll | down | connecting
    this.lastState = null;
    this.clock = { serverMs: Date.now(), perf: performance.now(), rate: 1, rtt: null, offsetMs: 0 };
    this.sseFailures = 0;
    this.es = null;
    this.pollTimer = null;
    this.retrySseTimer = null;
    this.lastContact = 0;
    this.stopped = false;
  }

  url(path) {
    return this.base + API + path;
  }

  now() {
    const c = this.clock;
    return c.serverMs + (performance.now() - c.perf) * c.rate;
  }

  _setTransport(t) {
    if (t !== this.transport) {
      this.transport = t;
      this.onTransport && this.onTransport(t);
    }
  }

  _ingest(state, rttMs) {
    const half = rttMs != null ? rttMs / 2 : 0;
    const rate = Number(state.clock_rate) || 1;
    // keep the sample with the smallest RTT among recent fetches; SSE samples rebase cheaply
    const c = this.clock;
    const serverNow = state.server_time_ms + half * rate;
    if (rttMs == null || c.rtt == null || rttMs <= c.rtt * 1.5 || rate !== c.rate) {
      this.clock = { serverMs: serverNow, perf: performance.now(), rate,
                     rtt: rttMs != null ? rttMs : c.rtt, offsetMs: serverNow - Date.now() };
    }
    this.lastState = state;
    this.lastContact = Date.now();
    this.onState(state);
  }

  async fetchState() {
    const t0 = performance.now();
    const r = await fetch(this.url("/state"), { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const s = await r.json();
    this._ingest(s, performance.now() - t0);
    return s;
  }

  async fetchJson(path) {
    const r = await fetch(this.url(path), { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }

  start() {
    this.stopped = false;
    this.fetchState().catch(() => this._setTransport("down"));
    this._startSse();
    // periodic RTT refresh
    this.rttTimer = setInterval(() => {
      if (this.transport === "sse") this.fetchState().catch(() => {});
    }, 300000);
    // A server that dies without closing the connection (power cut, frozen container) leaves the
    // browser waiting forever with no error. The server pings every 15 s, so 40 s of silence means
    // the stream is dead: say so, and start over (SSE, with polling until it opens).
    this.watchdog = setInterval(() => {
      if (this.stopped || this.transport !== "sse" || Date.now() - this.lastContact < SILENCE_MS) return;
      if (this.es) this.es.close();
      this._setTransport("down");
      this.sseFailures = 0;
      this._startSse();
      this._startPolling();
    }, 5000);
  }

  stop() {
    this.stopped = true;
    if (this.es) this.es.close();
    clearInterval(this.pollTimer);
    clearTimeout(this.retrySseTimer);
    clearInterval(this.rttTimer);
    clearInterval(this.watchdog);
  }

  _startSse() {
    if (this.stopped || typeof EventSource === "undefined") return this._startPolling();
    if (this.es) this.es.close();
    let opened = false;
    const es = new EventSource(this.url("/stream"));
    this.es = es;
    es.addEventListener("open", () => {
      opened = true;
      this.lastContact = Date.now();
      this.sseFailures = 0;
      clearInterval(this.pollTimer);
      this.pollTimer = null;
      this._setTransport("sse");
    });
    es.addEventListener("state", (ev) => {
      try {
        this._ingest(JSON.parse(ev.data), null);
        this._setTransport("sse");
      } catch (e) { /* ignore malformed */ }
    });
    es.addEventListener("ping", () => { this.lastContact = Date.now(); });
    es.addEventListener("error", () => {
      if (!opened) this.sseFailures += 1;
      opened = false;
      if (this.sseFailures >= 3) {
        es.close();
        this._startPolling();
        clearTimeout(this.retrySseTimer);
        this.retrySseTimer = setTimeout(() => { this.sseFailures = 0; this._startSse(); }, 300000);
      } else if (Date.now() - this.lastContact > 20000) {
        this._setTransport("down");
      }
    });
  }

  _startPolling() {
    if (this.pollTimer) return;
    const poll = () => this.fetchState().then(() => this._setTransport("poll")).catch(() => this._setTransport("down"));
    poll();
    this.pollTimer = setInterval(poll, 30000);
  }
}

export async function testServer(base) {
  const url = (base || "").replace(/\/+$/, "") + API + "/health";
  try {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) return { ok: false, reason: "http", status: r.status };
    const h = await r.json();
    return { ok: true, health: h };
  } catch (e) {
    const mixed = location.protocol === "https:" && url.startsWith("http:");
    return { ok: false, reason: mixed ? "mixed_content" : "network" };
  }
}
