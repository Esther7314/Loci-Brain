// ============================================================
// gateway/poke_delivery.js — one self-contained module, lifted whole out of a private
// gateway. That file was written from day one to ship alongside Loci, and its first
// boundary is **zero imports from the host project**: only fs / path / the global
// fetch, and only Loci's ordinary REST endpoints. So the extraction changed **not one
// line of logic**, only two things:
//   ① where state and logs land: the host project's data/ became this gateway's own
//   ② the injection marker: [Lento poke] → [Loci poke]
//
// It is **a module, not a service**. The server.js beside it is the minimal shell built
// for it — an OpenAI-compatible reverse proxy that calls `attach_once` here as a
// request goes past. If you already have a gateway of your own, skip that shell and
// require this file directly.
// ============================================================
//
// Two rules this file exists to keep:
//   **A dream is a delivery, so it carries content** (the system hands over "you had a
//   dream like this" plus the text — knowing it is enough, there is no need to recite
//   it back);
//   **Muse is a reminder, so it carries one line** (unclustered minds have piled up →
//   "time to muse", and the musing itself is done by hand afterwards).
//
// [!] The wording changed once. The earlier "the full dream never touches disk" was
// replaced by a newer rule: the full version survives on disk **through the silent
// night**, and the poke endpoint can hand over the whole thing. This file grew two
// things because of that, both of them unique to it (the breath paste and the recent
// memory view are unaffected and still run off the newWindow signal):
//
//   ① **The idle gate** — do not poke at the start of every window; poke **only when
//      nobody has said anything for a long time** (default 210 minutes = 3.5 hours,
//      tunable through env `POKE_IDLE_MINUTES`, which server.js reads and passes in).
//      Not idle enough: **not one character is injected, and Loci's poke endpoint is
//      not even asked**. Never interrupt a conversation in progress — that began as the
//      red line for poking someone about musing, and dream delivery was pulled under
//      the same gate, because "hand over the full version" can now be triggered by
//      accident in the gap between two consecutive sentences and has to be just as
//      strict.
//      Idle enough: this message is the **first sentence** back out of the silence, so
//      really ask Loci once and inject whatever there is (the dream — possibly the full
//      version, possibly a fragment or a single line if it has already decayed; and the
//      muse reminder).
//
//   ② **The decay trigger** — once the idle gate has opened (= this was the first
//      sentence back), the next message is recorded as "the second sentence back": the
//      moment it arrives, call `POST /api/loci/dream/wake` once to decay the still
//      living "full" layer on Loci's side down to the fragment layer (the old lifecycle
//      of 30 minutes as a fragment / 60 minutes as one line starts counting from then).
//      Stay away and it never decays — the full version has no timeout, and this is its
//      only way to die. **Idempotent as a backstop**: Loci's wake endpoint silently
//      returns 200 when there is no full layer, so if this side's state ever drifts out
//      of sync with reality (a previous call succeeded but the state file was never
//      written, say), calling again is entirely harmless.
//      Arming and decaying **do not look at whether this message is itself idle** — as
//      long as the "we judged idle last time" armed flag is still up, this message
//      counts as "the next one back" and decays once, idle or not.
//
// ⚠️ **The `newWindow` signal is no longer what this module uses to decide whether to
//    ask Loci** — that decision is now **the idle gate and nothing else**. The parameter
//    stays (interface parity with the other paths, plus log diagnostics), but passing
//    `newWindow=true` cannot get around the idle gate: not idle enough still means not
//    one character injected.
//
// Same module family and the same boundary as auto_attach.js.
// ⚰️ The `近期记忆视图.js` named in a few places below **was withdrawn wholesale**
//    (there is no such file in this directory any more). Those references are left
//    standing because they say how the boundary was drawn, not because they point
//    anywhere:
//   zero imports from the host project (this file ships whole with Loci, and the host's
//   own code cannot be smuggled along) ·
//   Loci reached over HTTP only, and only on its ordinary REST endpoints
//   (`/api/loci/poke`, `/api/loci/dream/wake`) — not the MCP tool surface; the red line
//   of "ten MCP tools, not one added and not one removed" is untouched here ·
//   failure never blocks the chat · one log line.
//
// Position: **the same prefix region as the breath paste** — inserted before the latest
// user message, not pinned at the "true tail" the way the relevance reminder is. Within
// a window this content does not have to change with whatever the user just said, so
// there is no "must be closest to the moment the model speaks" reason for it, and
// therefore no need to work around restoreLatestRequestTail, the hard 400 validation, or
// moveSystemPatchesBeforeLatestUser (the full reasoning was spelled out in an earlier
// version of this paragraph; the judgement itself has not changed).
//
// Neither one to hand → not one character injected, not even the MARKER line.
// ============================================================

const fs = require("fs");
const path = require("path");

// The one path the extraction changed: it used to land in data/ under the host
// project's root, and now lands in data/ under **this gateway's own directory**
// (LOCI_GATEWAY_DATA can point it somewhere else).
const data_root = process.env.LOCI_GATEWAY_DATA || path.join(__dirname, "data");

// The same env var name auto_attach.js and 近期记忆视图.js use (where Loci's MCP
// endpoint is). Loci's ordinary REST endpoints hang off the same process on the same
// port, just under a path other than /mcp — so the REST root is derived from that one
// address instead of getting a second env var (configure once, both sides are right).
const DEFAULT_ADDRESS = process.env.LOCI_MCP || "http://127.0.0.1:18002/mcp";
const DEFAULT_STATE_PATH = path.join(data_root, "state", "poke-window.json");
const DEFAULT_LOG_PATH = path.join(data_root, "logs", "memory-actions.jsonl");
// = 3.5 hours, the factory value. server.js reads env POKE_IDLE_MINUTES and overrides
// it; the default here only covers this module being called or tested on its own.
const DEFAULT_IDLE_MINUTES = 210;

// Diagnostics and tests recognise this literal — sister marker to auto_attach.js's [Loci memory context].
const MARKER = "[Loci poke]";

function read_json(file, fallback = {}) {
  try { return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : fallback; }
  catch { return fallback; }
}
function write_json(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`);
}
function log_line(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.appendFileSync(file, `${JSON.stringify(value)}\n`);
}

/** `http://host:port/mcp` → `http://host:port`. Loci's REST endpoints
 *  (/api/loci/poke, /api/loci/dream/wake and the like) live in the same process on the
 *  same port as the MCP endpoint, just under a different root path. */
function httpBase(mcpUrl) {
  return String(mcpUrl || "").replace(/\/mcp\/?$/, "");
}

/** A plain HTTP GET, no MCP handshake (this endpoint is ordinary REST, not an MCP tool). */
// Carry the key for Loci's four hook routes.
// 🔴 Why this exists: those four used to be on the "no inspection" list — a password on
//    the panel did not stop them, and they could **both read a dream's text and change
//    state**. The rule is now "if the door is locked, bring the key", so the bridge has
//    to carry it or dreams and pokes come back 401.
// ⚠️ It travels in a request header, **never in the URL** — a URL leaks out through
//    logs, Referer, and browser history.
// ⚠️ It still runs with no `LOCI_HOOK_TOKEN` set: Loci only asks for the key when the
//    **door is locked**. (So for anyone who has not set a password, nothing changes.)
function request_headers() {
  const h = { Accept: "application/json" };
  const k = String(process.env.LOCI_HOOK_TOKEN || "").trim();
  if (k) h["x-loci-hook-token"] = k;
  return h;
}

async function fetch_poke(address, { timeout_ms = 8000 } = {}) {
  const url = `${httpBase(address)}/api/loci/poke`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout_ms);
  let resp;
  try {
    resp = await fetch(url, { method: "GET", headers: request_headers(), signal: controller.signal });
  } catch (err) {
    clearTimeout(timer);
    if (err?.name === "AbortError") throw new Error(`问 Loci 戳口超时（${url}）`);
    throw new Error(`连不上 Loci 戳口（${url}）：${err?.message || err}`);
  }
  clearTimeout(timer);
  if (!resp.ok) throw new Error(`Loci 戳口回了 HTTP ${resp.status}`);
  const body = await resp.json();
  if (!body || typeof body !== "object") throw new Error("Loci 戳口没给 JSON");
  return body;
}

/** The decay signal: triggered by the second message after the user comes back, one
 *  POST, idempotent (Loci silently returns 200 when there is no living full layer).
 *  Plain REST like fetch_poke, no MCP handshake. */
async function call_wake(address, { timeout_ms = 8000 } = {}) {
  const url = `${httpBase(address)}/api/loci/dream/wake`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout_ms);
  let resp;
  try {
    resp = await fetch(url, {
      method: "POST",
      headers: { ...request_headers(), "Content-Type": "application/json" },
      body: "{}",
      signal: controller.signal,
    });
  } catch (err) {
    clearTimeout(timer);
    if (err?.name === "AbortError") throw new Error(`调 Loci 降级口超时（${url}）`);
    throw new Error(`连不上 Loci 降级口（${url}）：${err?.message || err}`);
  }
  clearTimeout(timer);
  if (!resp.ok) throw new Error(`Loci 降级口回了 HTTP ${resp.status}`);
  return true;
}

/** build_patch_text: the dream first (a delivery, so the whole text — whether that text
 *  is currently the full version or an already-decayed fragment or single line, whatever
 *  Loci returns is what gets pasted; this module does not care about "layers", only
 *  about whether Loci handed over any content), then the one muse line (a reminder, and
 *  never the contents of a cluster). With neither in hand, control should never reach
 *  here — the caller does not build this block when there is nothing to deliver. */
function build_patch_text(poke) {
  const parts = [MARKER];
  if (poke.dream) {
    parts.push("〔梦〕昨夜织了一个梦：", String(poke.dream.内容 || "").trim());
  }
  if (poke.musePending > 0) {
    if (parts.length > 1) parts.push("");
    parts.push(`〔发呆〕有 ${poke.musePending} 团日子和想法还没整理，该发呆了（muse()）`);
  }
  return parts.join("\n");
}

function insert_before_latest_user(messages, patch) {
  let latest_user_index = messages.length;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i]?.role === "user") { latest_user_index = i; break; }
  }
  let insert_at = 0;
  while (insert_at < messages.length && messages[insert_at]?.role === "system" && insert_at < latest_user_index) insert_at += 1;
  messages.splice(insert_at, 0, patch);
}

/**
 * Poke delivery: whether to ask Loci and whether to inject both come down to **one gate
 * and one gate only — the idle gate**.
 *
 * @param messages          the messages array going upstream this round (modified in
 *                          place, the same contract the other paths use)
 * @param newWindow         the same signal the other paths get, but **this module no
 *                          longer uses it to decide whether to ask Loci** (it used to;
 *                          the idle gate replaced it). The parameter stays for interface
 *                          parity and log diagnostics, and plays no part in the logic.
 * @param statePath         state file: time of the last message + the decay armed flag +
 *                          whatever the last poke returned (the same file as the window
 *                          cache, which is explicitly allowed)
 * @param logPath           shares the one memory-actions.jsonl with the other paths
 * @param 闲时阈值分钟       how many minutes since the last message counts as "idle".
 *                          The gateway reads env `POKE_IDLE_MINUTES` and passes it in;
 *                          the default here only covers the module being called alone.
 */
async function attach_once({
  messages,
  requestId,
  now = new Date(),
  newWindow = false,
  statePath = DEFAULT_STATE_PATH,
  logPath = DEFAULT_LOG_PATH,
  地址: address = DEFAULT_ADDRESS,
  超时毫秒: timeout_ms = 8000,
  闲时阈值分钟: idle_threshold_minutes = DEFAULT_IDLE_MINUTES,
} = {}) {
  const result = {
    patchInjected: false, calledLoci: false,
    hasDream: false, musePending: 0, error: null,
    idle: false, idleMinutes: null, wakeCalled: false, wakeError: null,
  };
  const state = read_json(statePath, {});

  // ---- Decay trigger: nothing to do with how idle this request is; it looks only at
  //      "were we already armed last time" ----
  // Armed = the previous request judged "idle enough" (= that message was the first
  // sentence back), which makes this message the next one after the return — decay once.
  // Arming and disarming both have to reach the state file, so work out here whether
  // this request should disarm, and fold that write in with the idle-gate section below
  // so the file is written once.
  let wakePending = state.wakePending === true;
  if (wakePending) {
    result.wakeCalled = true;
    try {
      await call_wake(address, { timeout_ms });
      log_line(logPath, {
        time: now.toISOString(), request_id: requestId, actor: "gateway/poke",
        action: "dream_wake", status: "ok",
      });
      wakePending = false;               // decayed successfully, disarm
    } catch (err) {
      result.wakeError = String(err?.message || err);
      result.wakeCalled = false;
      log_line(logPath, {
        time: now.toISOString(), request_id: requestId, actor: "gateway/poke",
        action: "dream_wake", status: "error", error: result.wakeError,
      });
      // 🔴 A failed decay neither blocks the chat nor pretends to have worked — stay
      //    armed and try again on the next message (Loci's wake is idempotent, so a few
      //    extra attempts have no side effects).
    }
  }

  // ---- Idle gate: has it been long enough since the last message ----
  const last_user_time = state.lastUserMessageTime ? new Date(state.lastUserMessageTime) : null;
  const minutes_since = last_user_time && !Number.isNaN(last_user_time.getTime())
    ? (now.getTime() - last_user_time.getTime()) / 60000
    : Infinity;                          // no history: nothing says a message arrived "just now", so the gate defaults to open
  const idle_enough = minutes_since >= Number(idle_threshold_minutes);
  result.idle = idle_enough;
  result.idleMinutes = Number.isFinite(minutes_since) ? Math.round(minutes_since) : null;

  if (!idle_enough) {
    // 🔴 Not idle enough: not one character injected, and Loci is not even asked (saves the call) — never interrupt a conversation in progress.
    write_json(statePath, { ...state, lastUserMessageTime: now.toISOString(), wakePending });
    return result;
  }

  // ---- Idle enough: this message is the first sentence back, so really ask Loci once ----
  result.calledLoci = true;
  let poke = null;
  try {
    const data = await fetch_poke(address, { timeout_ms });
    const dreams = Array.isArray(data.dreams) ? data.dreams : [];
    poke = { dream: dreams.length ? dreams[0] : null, musePending: Number(data.muse_pending) || 0 };
    log_line(logPath, {
      time: now.toISOString(), request_id: requestId, actor: "gateway/poke",
      action: "poke_fetch", status: "ok", idle_minutes: result.idleMinutes,
      has_dream: Boolean(poke.dream), muse_pending: poke.musePending,
    });
  } catch (err) {
    result.error = String(err?.message || err);
    log_line(logPath, {
      time: now.toISOString(), request_id: requestId, actor: "gateway/poke",
      action: "poke_fetch", status: "error", error: result.error,
      fallback_to_stale_cache: Boolean(state.poke),
    });
    // Failure never blocks the chat: paste whatever the last successful poke returned, or skip this round if there is none.
    poke = state.poke || null;
  }

  // ---- `dreamsDelivered` in this state file: the ids of dreams already handed over,
  //      here or by a wake (present/wake.js reads and writes the same list). One of them
  //      is never handed over again, in any layer: this message only sends dream/wake
  //      for it (above). A dream handed over here goes onto the list. ----
  const dreams_delivered = Array.isArray(state.dreamsDelivered) ? state.dreamsDelivered.map(String) : [];
  if (poke?.dream && poke.dream.id != null && dreams_delivered.includes(String(poke.dream.id))) {
    poke = { ...poke, dream: null };
  }
  const delivering_dream = poke?.dream && poke.dream.id != null ? String(poke.dream.id) : null;
  const delivered_after = delivering_dream ? [...dreams_delivered, delivering_dream].slice(-32) : dreams_delivered;

  // If the idle gate opened at all, arm for the next sentence to decay — even when
  // nothing came back (poke is null): wake silently returns 200 with no full layer, so
  // arming one extra time has no side effects.
  // (If this same message also did the decaying — the wakePending block above just
  // fired — do not re-arm; wait for the next genuinely separate gap.)
  if (!result.wakeCalled) wakePending = true;

  write_json(statePath, {
    poke, fetchedAt: now.toISOString(),
    lastUserMessageTime: now.toISOString(), wakePending,
    ...(delivered_after.length ? { dreamsDelivered: delivered_after } : {}),
  });

  if (poke && (poke.dream || poke.musePending > 0)) {
    const patch = { role: "system", content: build_patch_text(poke) };
    insert_before_latest_user(Array.isArray(messages) ? messages : [], patch);
    result.patchInjected = true;
    result.hasDream = Boolean(poke.dream);
    result.musePending = poke.musePending;
  }
  return result;
}

module.exports = {
  MARKER,
  DEFAULT_ADDRESS,
  DEFAULT_STATE_PATH,
  DEFAULT_LOG_PATH,
  DEFAULT_IDLE_MINUTES,
  attach_once,

// ── Deprecated aliases: the names these used to have. Gone in the next major. ────────
// 🔴 Renaming the bindings to English is an internal matter, but these keys are **names
//    other people type by hand after require()** — drop them and their code breaks on
//    the spot, on a name they cannot type. One line each costs nothing.
  默认地址: DEFAULT_ADDRESS,
  默认状态档: DEFAULT_STATE_PATH,
  默认日志档: DEFAULT_LOG_PATH,
  默认闲时阈值分钟: DEFAULT_IDLE_MINUTES,

// ── Backward-compatible aliases ──────────────────────────────────────────────────────
// 🔴 **Aliases only: each one points at the same thing as its formal name.** The formal
//    names are English already, so new code should reach for those directly —
//    attach_once and MARKER. These spellings stay because they are already written into
//    code elsewhere, and breaking a name someone has typed costs them far more than one
//    line costs us.
  // paste({ messages, requestId, 地址, 闲时阈值分钟 }) — modifies messages in place
  paste: attach_once,
  MARKER_LINE: MARKER,
  // DEFAULT_ADDRESS / DEFAULT_IDLE_MINUTES are the formal names now, exported above.

  _internal: { httpBase, fetch_poke, call_wake, build_patch_text, insert_before_latest_user },
};
