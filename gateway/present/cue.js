// ============================================================
// gateway/present/cue.js — the three cue routes on Loci's side, as the gateway uses them
//
//   POST /api/v2/cue            {text, window, turn} → {cards: [{card, kind, …, text}], text}
//                               asked once per turn with the owner's line; `text` is the
//                               cards to paste right after that line, one per line
//   POST /api/v2/cue/delivered  {window, turn} — every card of that turn's answer is in the
//                               model's input now (said once upstream accepted the request)
//   POST /api/v2/cue/dropped    {window, all: true} / {window, turns} / {window, cards} —
//                               cards that left the input (a window flip, index.js)
// `window` = "<thread>#w<window no>", `turn` = the owner's line id. Loci answers the same
// turn with the same cards, so a resend can always ask again; the window state keeps the
// first answer and replays it instead (present/window.js).
//
// The routes are Loci's bridge routes: the gateway carries LOCI_HOOK_TOKEN in the
// `x-loci-hook-token` header (never in the URL) when it is set, exactly as poke delivery
// does. Loci only asks for it once its door is locked.
//
// A failure never blocks the chat: an unreachable Loci, a timeout (LOCI_CUE_TIMEOUT_MS,
// default 3000 — matching is code only on Loci's side, so a slow answer means something
// is wrong), a non-2xx or a reply that is not JSON all come back as { ok: false, error }
// and the turn is forwarded without a card. Each ask writes one `cue_observed` line to
// memory-actions.jsonl, which is what /health reads to say whether this works.
// ============================================================

const fs = require("fs");
const path = require("path");

const DEFAULT_TIMEOUT_MS = 3000;

function http_base(address) {
  return String(address || "").replace(/\/mcp\/?$/, "").replace(/\/+$/, "");
}

function log_line(file, value) {
  if (!file) return;
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.appendFileSync(file, `${JSON.stringify(value)}\n`);
  } catch { /* a log that cannot be written never costs the chat anything */ }
}

/**
 * @param address     Loci's MCP address (…/mcp); the REST root is derived from it
 * @param hook_token  LOCI_HOOK_TOKEN, or "" when Loci's door is not locked
 * @param timeout_ms  how long one call may take
 * @param log_path    memory-actions.jsonl
 */
function create_cue({ address, hook_token = "", timeout_ms = DEFAULT_TIMEOUT_MS, log_path = null }) {
  const base = http_base(address);

  async function post(route, body) {
    const headers = { "Content-Type": "application/json", Accept: "application/json" };
    if (hook_token) headers["x-loci-hook-token"] = hook_token;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout_ms);
    const url = `${base}${route}`;
    try {
      const resp = await fetch(url, { method: "POST", headers, body: JSON.stringify(body), signal: controller.signal });
      const raw = await resp.text();
      if (!resp.ok) throw new Error(`Loci ${route} 回了 HTTP ${resp.status}`);
      let parsed;
      try { parsed = JSON.parse(raw); } catch { throw new Error(`Loci ${route} 没给 JSON`); }
      if (!parsed || typeof parsed !== "object") throw new Error(`Loci ${route} 没给 JSON 对象`);
      return parsed;
    } catch (err) {
      if (err?.name === "AbortError") throw new Error(`问 Loci ${route} 超时（${timeout_ms}ms）`);
      if (/^Loci /.test(err?.message || "")) throw err;
      throw new Error(`连不上 Loci ${route}（${url}）：${err?.cause?.code || err?.message || err}`);
    } finally { clearTimeout(timer); }
  }

  /** @returns { ok, cards: [keys], text, error, ms } */
  async function ask({ text, window, turn, request_id = null }) {
    const started = Date.now();
    const record = {
      time: new Date().toISOString(), request_id, actor: "gateway/cue", action: "cue_observed",
      window, turn, asked: true, cards: 0, injected: false,
    };
    try {
      const reply = await post("/api/v2/cue", { text: String(text ?? ""), window, turn });
      const cards = Array.isArray(reply.cards) ? reply.cards.map((c) => String(c?.card || "")).filter(Boolean) : [];
      const card_text = typeof reply.text === "string" ? reply.text : "";
      record.cards = cards.length;
      record.injected = Boolean(card_text.trim());
      record.card_keys = cards;   // keys only, never the text: the log is not a second copy of the memory
      record.ms = Date.now() - started;
      log_line(log_path, record);
      return { ok: true, cards, text: card_text, error: null, ms: record.ms };
    } catch (err) {
      record.error = String(err?.message || err);
      record.ms = Date.now() - started;
      log_line(log_path, record);
      return { ok: false, cards: [], text: "", error: record.error, ms: record.ms };
    }
  }

  async function delivered({ window, turn }) {
    try {
      await post("/api/v2/cue/delivered", { window, turn });
      log_line(log_path, { time: new Date().toISOString(), actor: "gateway/cue", action: "cue_delivered", window, turn, status: "ok" });
      return { ok: true, error: null };
    } catch (err) {
      const error = String(err?.message || err);
      log_line(log_path, { time: new Date().toISOString(), actor: "gateway/cue", action: "cue_delivered", window, turn, status: "error", error });
      return { ok: false, error };
    }
  }

  /** For the window flip (index.js): `{all: true}`, `{turns}` or `{cards}`. */
  async function dropped({ window, ...which }) {
    try {
      await post("/api/v2/cue/dropped", { window, ...which });
      log_line(log_path, { time: new Date().toISOString(), actor: "gateway/cue", action: "cue_dropped", window, status: "ok" });
      return { ok: true, error: null };
    } catch (err) {
      const error = String(err?.message || err);
      log_line(log_path, { time: new Date().toISOString(), actor: "gateway/cue", action: "cue_dropped", window, status: "error", error });
      return { ok: false, error };
    }
  }

  return { ask, delivered, dropped, timeout_ms, base };
}

module.exports = { create_cue, DEFAULT_TIMEOUT_MS, http_base };
