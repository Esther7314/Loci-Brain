// ============================================================
// gateway/auto_attach.js — one self-contained module, lifted whole out of a private
// gateway. Same module family as poke_delivery.js, same boundary: **zero imports from
// the host project**, only fs / path / the global fetch. It was written from day one to
// be open-sourced alongside Loci, which is why the extraction changed **not one line of
// logic** — only where the paths land.
//
// One file, two jobs, and the gateway pokes both of them every round:
//   · breath paste        first turn of a window: call breath() once and paste the
//                         whole thing into the system prompt (the prefix region)
//   · relevance reminder  strong = keyword hit / weak = local heuristic; calls recall
//                         once, and only when triggered. **Reports how many, never
//                         what** — pinned at the true tail, a different position from
//                         the breath paste.
//
// 🔴 Why the reminder has to sit at the true tail: the position it needs is "after the
//    latest user message, at the very end of the whole messages array" — as close as
//    possible to the moment the model speaks. So build_relevance_notice() **never
//    touches messages itself**; it computes the patch, hands it back, and you push it
//    in with attach_at_true_tail() once the request body is assembled.
// ============================================================
//
// Two boundaries shaped this file, and the shape is the whole point:
//   ① The breath paste is **its own module file**, never mixed into the gateway's
//      existing files — the gateway keeps one line of wiring and nothing more.
//   ② The gateway-side code does not live together with the host application's code.
//      It is a standalone module with **zero imports from the host project**: nothing
//      under the host's server/ or chat/ trees is required from here.
//      It looks a lot like the host's own MCP client (the same handshake and call
//      gestures), and that duplication is **deliberate**, not something to factor out:
//      this file ships with Loci, and the host's code cannot come along for the ride.
//
// Loci is only ever reached over HTTP (streamable-http MCP,
// `http://127.0.0.1:18002/mcp`, no token, by house rule). There is not one line of
// Loci's own code in this file, and it touches none.
//
// Two jobs, both done in a single call (the gateway pokes both every round):
//   · Breath paste — on the first turn of a window, one HTTP call to breath(), and the
//     whole thing (the full waking screen breath() returns, nothing picked over, nothing
//     trimmed) goes into the system prompt / context. Later turns in the same window do
//     not call Loci again, they just re-paste the cached copy. (The messages array will
//     not remember what was pasted last round for us — the messages arriving each round
//     are replayed from the conversation archive and carry none of this layer's
//     patches.)
//   · Relevance reminder — **only runs when triggered**: strong = keyword hit (reusing
//     the existing word list), weak = a local heuristic (not real tokenisation, not
//     vectors — see the comment on weak_triggered) that filters out interjections and
//     greetings and asks whether anything of substance is left. On a round where
//     neither fires, **not a single recall is called**. When one does fire, the user's
//     sentence becomes the query for Loci's recall, and only entries whose rendered
//     score (0~100, the same ruler as Loci's own `RELEVANCE_FLOOR=35`) is
//     ≥ `RELEVANCE_MIN_SCORE` (env, default 50) are counted; event and mind are counted
//     separately (a 🧠 badge in the recall render means mind). If anything hits, one
//     short system line is computed — 「〔记忆提醒〕和这句有关：事件 N 条 · 认知 M 条」
//     — **counts only, not one character of memory text is allowed through**; on 0 hits
//     there is nothing at all. Recomputed every round: no state written, nothing
//     cached, nothing accumulated (the gateway rebuilds messages per request, so last
//     round's reminder line can never carry over by itself).
//     🔴 The insertion point is **the true tail of the whole messages array** (after
//     the latest user message), not "before the latest user message" the way the breath
//     paste goes in — closest to the moment the model speaks, highest hit rate. But
//     **this module does not do the inserting**: build_relevance_notice only puts the
//     computed patch into its return value (record.patch), and the actual push onto
//     outgoingBody.messages happens in server.js, once the outgoing body is assembled.
//     The reason: the messages array inside server.js has to clear three
//     validation/rebuild stages before it goes upstream (tail rebuild / hard 400
//     validation / `moveSystemPatchesBeforeLatestUser`), and every one of them assumes
//     that whatever follows the latest user message can only be a legal tool
//     continuation. Insert there and the line is either swallowed or the whole request
//     400s. Details in ①②③ of build_relevance_notice's own doc comment.
//
// Failure (Loci not running, a timeout) blocks the chat in neither job: the half that
// failed quietly does nothing, and the caller (the gateway) forwards exactly as it
// would have.
// ============================================================

const fs = require("fs");
const path = require("path");

// The one thing the extraction changed: this path (poke_delivery.js changed the same way).
const data_root = process.env.LOCI_GATEWAY_DATA || path.join(__dirname, "data");

// LOCI_MCP: the same env var name the host's own MCP client reads; acceptance tests
// point it at a fake Loci.
const DEFAULT_ADDRESS = process.env.LOCI_MCP || "http://127.0.0.1:18002/mcp";
const DEFAULT_STATE_PATH = path.join(data_root, "state", "auto-breath-window.json");
const DEFAULT_LOG_PATH = path.join(data_root, "logs", "memory-actions.jsonl");

// 🔴 Diagnostic code on the host side (upstreamDebugRecord in its gateway server,
//    isVolatile in its messages module) recognises "this is pasted memory" by this
//    exact literal — change the string and both go silently blind. Not one character.
const MARKER = "[Loci memory context]";

// Strong trigger = the sentence contains a word that **says outright** it is digging
// up the past.
// 🔴 Configurable on purpose: the defaults are Chinese and the weak trigger is off by
//    default, so without this **anyone who does not speak Chinese would install this and
//    never see it fire once, with nothing to tell them why** — quietly doing nothing.
//    The config file name is ASCII for the same reason: it is a file that person has to
//    create by hand.
// How to change it (pick one, nearest first):
//    RELEVANCE_STRONG_WORDS="remember,last time,earlier"   comma separated, replaces the whole list
//    gateway/strong_words.json                              a JSON array, same effect
//    set nothing → the Chinese defaults below
const CHINESE_STRONG_WORDS = [
  "我记得", "记得吗", "还记得", "上次", "之前", "以前", "那时候", "那天", "那次", "记不记得",
];

function read_strong_words() {
  const from_env = String(process.env.RELEVANCE_STRONG_WORDS || "").trim();
  if (from_env) {
    const words = from_env.split(",").map(w => w.trim()).filter(Boolean);
    if (words.length) return words;
  }
  try {
    const config_file = path.join(__dirname, "strong_words.json");
    if (fs.existsSync(config_file)) {
      const words = JSON.parse(fs.readFileSync(config_file, "utf8"));
      if (Array.isArray(words) && words.length) return words.map(String);
    }
  } catch { /* a broken config must never break forwarding: fall back to the defaults */ }
  return CHINESE_STRONG_WORDS;
}

const STRONG_WORDS = read_strong_words();

// Score floor: compared directly against the 0~100 scale of Loci's recall render, with
// no 0~1 conversion (an earlier version converted to 0~1 out of pure historical
// baggage). Overridden by env `RELEVANCE_MIN_SCORE` — this number has to stay easy to
// change, which means one env var and nowhere else.
const DEFAULT_MIN_SCORE = 50;

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

// ——— Minimal MCP streamable-http client (same gestures as the host's own, kept separate) ———

function make_client({ address = DEFAULT_ADDRESS, timeout_ms = 10000 } = {}) {
  let session = null;

  function build_headers(with_session) {
    const h = { "Content-Type": "application/json", "Accept": "application/json, text/event-stream" };
    if (with_session && session) h["Mcp-Session-Id"] = session;
    return h;
  }

  function pick_frame(pending) {
    for (const line of pending.split(/\r?\n/)) {
      if (!line.startsWith("data:")) continue;
      const text = line.slice(5).trim();
      if (!text) continue;
      try { return JSON.parse(text); } catch { /* frame not complete yet */ }
    }
    return null;
  }

  async function rpc_once(payload, { with_session = true } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout_ms);
    let resp;
    try {
      resp = await fetch(address, { method: "POST", headers: build_headers(with_session), body: JSON.stringify(payload), signal: controller.signal });
    } catch (err) {
      clearTimeout(timer);
      throw new Error(`连不上 Loci（${address}）：${err?.message || err}`);
    }
    const new_session = resp.headers.get("mcp-session-id");
    if (new_session) session = new_session;
    if (resp.status === 202 || !resp.body) { clearTimeout(timer); return null; }
    if (!resp.ok) { clearTimeout(timer); throw new Error(`Loci 回了 HTTP ${resp.status}`); }
    const reader = resp.body.getReader();
    let pending = "";
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        pending += Buffer.from(value).toString("utf8");
        const frame = pick_frame(pending);
        if (frame) return frame;
      }
    } finally {
      clearTimeout(timer);
      controller.abort();
    }
    throw new Error("Loci 没给回应");
  }

  let handshaking = null;
  function handshake_once() {
    if (!handshaking) handshaking = handshake().finally(() => { handshaking = null; });
    return handshaking;
  }
  async function handshake() {
    session = null;
    await rpc_once({
      jsonrpc: "2.0", id: 1, method: "initialize",
      params: { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "lento-gateway", version: "1" } },
    }, { with_session: false });
    if (!session) throw new Error("Loci 没给 session id");
    await rpc_once({ jsonrpc: "2.0", method: "notifications/initialized" });
  }

  async function call_tool(tool, args = {}) {
    if (!session) await handshake_once();
    const send = () => rpc_once({ jsonrpc: "2.0", id: Date.now() % 100000, method: "tools/call", params: { name: tool, arguments: args } });
    let resp = await send().catch(err => ({ __炸了: err }));
    if (resp?.__炸了 || resp?.error) {
      await handshake_once();
      resp = await send().catch(err => ({ __炸了: err }));
    }
    if (resp?.__炸了) throw resp.__炸了;
    if (resp?.error) throw new Error(resp.error.message || JSON.stringify(resp.error));
    return (resp?.result?.content || []).map(block => block.text || "").join("\n");
  }

  return { call_tool };
}

// ——— Breath paste ———

function insert_before_latest_user(messages, patch) {
  let latest_user_index = messages.length;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i]?.role === "user") { latest_user_index = i; break; }
  }
  // Sit up front alongside the system messages (the old cache's insertSystemPatch rule:
  // right after whatever system messages already exist). The real ordering is sorted out
  // again by the host's moveSystemPatchesBeforeLatestUser; all that matters here is not
  // landing after the latest user message.
  let insert_at = 0;
  while (insert_at < messages.length && messages[insert_at]?.role === "system" && insert_at < latest_user_index) insert_at += 1;
  messages.splice(insert_at, 0, patch);
}

function build_patch_text(breathText, generatedAt) {
  return [
    MARKER,
    `本轮 breath 于 ${generatedAt}（gateway 主动 HTTP 调，非工具调用）生成，一样不挑不裁。`,
    "",
    breathText,
  ].join("\n");
}

/**
 * Breath paste: on the first turn of a window, call breath() once and paste the whole
 * thing into messages; later turns in the same window just re-paste the cache.
 *
 * @param messages    the messages array going upstream this round (**modified in
 *                    place**, the same contract the old applyStartupBreathMemory had —
 *                    the caller does not assign the result back)
 * @param newWindow   whether this is the first turn of a new window. The gateway has
 *                    already worked that signal out and passes it in; this module does
 *                    not re-decide "what counts as a window", because that decision
 *                    belongs in exactly one place
 * @param statePath   where the current window's breath text is cached (so a process
 *                    restart does not mean calling Loci again)
 * @param logPath     shares the one memory-actions.jsonl with the old cache; no second
 *                    log file
 * @param 地址/超时毫秒 only for acceptance tests pointing at a fake Loci; the defaults
 *                    are what runs normally
 */
async function attach_once({
  messages,
  requestId,
  now = new Date(),
  newWindow = false,
  statePath = DEFAULT_STATE_PATH,
  logPath = DEFAULT_LOG_PATH,
  地址: address = DEFAULT_ADDRESS,
  超时毫秒: timeout_ms = 10000,
} = {}) {
  // cacheHit / cacheWritten exist to keep the old field names that downstream
  // diagnostics read (breathMeta / pruneCompletedStartupBreathToolChains on the host
  // side), with the meanings carried across unchanged: cacheHit = this round did not
  // call Loci again and pasted the old cached text; cacheWritten = this round really
  // called, and succeeded.
  const result = { patchInjected: false, calledLoci: false, cacheHit: false, cacheWritten: false, windowId: null, breathChars: 0, error: null };
  const cache = read_json(statePath, {});
  let breathText = cache.breathText || "";
  let windowId = cache.windowId || null;

  if (newWindow || !breathText) {
    result.calledLoci = true;
    try {
      const client = make_client({ address, timeout_ms });
      const text = await client.call_tool("breath", {});
      windowId = `window-${now.toISOString()}`;
      breathText = String(text || "");
      write_json(statePath, { windowId, breathText, fetchedAt: now.toISOString() });
      log_line(logPath, { time: now.toISOString(), request_id: requestId, actor: "gateway/auto_paste", action: "breath_fetch", status: "ok", window_id: windowId, result_chars: breathText.length });
      result.cacheWritten = true;
    } catch (err) {
      result.error = String(err?.message || err);
      log_line(logPath, { time: now.toISOString(), request_id: requestId, actor: "gateway/auto_paste", action: "breath_fetch", status: "error", error: result.error, fallback_to_stale_cache: Boolean(breathText) });
      // 🔴 A failure never blocks the chat: if there is an old cache, paste the old
      //    text (better than nothing); if there is none, skip this round. Loci being
      //    down or slow must never hold up what the user just said.
    }
  }

  if (breathText) {
    const patch = { role: "system", content: build_patch_text(breathText, cache.fetchedAt || now.toISOString()) };
    insert_before_latest_user(Array.isArray(messages) ? messages : [], patch);
    result.patchInjected = true;
    result.breathChars = breathText.length;
    // No fresh call to Loci this round, only a re-paste of the old cached text — which is exactly what "cache hit" means.
    result.cacheHit = !result.cacheWritten;
  }
  result.windowId = windowId;
  return result;
}

// ——— Relevance reminder (only runs when triggered; injects counts, never text) ———

function latestUserText(messages) {
  for (let i = (messages || []).length - 1; i >= 0; i -= 1) {
    const m = messages[i];
    if (m?.role !== "user") continue;
    if (typeof m.content === "string") return m.content;
    if (Array.isArray(m.content)) return m.content.map(part => (typeof part?.text === "string" ? part.text : "")).join("");
    return "";
  }
  return "";
}

function strong_hits(text) {
  // Compare lowercased: an English word list must not miss a whole entry just because
  // the sentence opens with a capital ("Remember when…"). Chinese has no case, so
  // toLowerCase is the identity there and this line costs us nothing.
  const lowered = String(text).toLowerCase();
  return STRONG_WORDS.filter(word => lowered.includes(String(word).toLowerCase()));
}

// Stop list of whole short utterances for the weak trigger: interjections and
// greetings. An exact match means "no substance here".
// ⚠️ **This is not real subject-noun extraction** — the design called for subject nouns
// plus local vectors, and this layer has neither a tokeniser nor a local vector model,
// while Chinese has no spaces for a simple regex to split on.
// So it settles for less: **filter out the obviously subject-less noise** (a bare
// 「嗯」 or 「在吗」 is no reason to go ask Loci) and let anything with a bit of
// substance through to recall. The real semantic judgement is left to Loci's own recall
// (local vectors, real scores); this layer only decides whether the question is worth
// asking at all.
// A known simplification, called out in the handover; swap this function out when
// someone wants real subject extraction.
const WEAK_STOPWORDS = new Set([
  "在吗", "在么", "你好", "嗨", "hi", "hello", "早", "早安", "晚安",
  "谢谢", "谢谢你", "谢啦", "多谢",
  "好的", "好嘞", "好呀", "好", "行", "行吧", "ok", "okay",
  "嗯", "嗯嗯", "哦", "哦哦", "噢", "知道了", "收到",
  "没事", "没什么", "无事", "没有", "算了",
]);

function weak_triggered(text) {
  const trimmed = String(text || "").trim();
  if (!trimmed) return false;
  // Strip trailing punctuation noise (「在吗？」 is just as much an interjection),
  // then match the whole utterance against the stop list.
  const stripped = trimmed.replace(/[，。！？、,.!?~～…\s]+$/g, "");
  if (WEAK_STOPWORDS.has(stripped)) return false;
  // Effective length: Han characters counted one by one, English and digits by the
  // length of each run of letters or numbers. Too short is not a subject (a
  // one-character grunt, a two- or three-character verbal tic); three and up counts as
  // "this sentence looks like it is saying something".
  const han_char_count = (stripped.match(/[一-鿿]/g) || []).length;
  const word_char_count = (stripped.match(/[A-Za-z]{2,}|[0-9]+/g) || []).reduce((n, w) => n + w.length, 0);
  return han_char_count + word_char_count >= 3;
}

/**
 * Pull the scored lines out of the **rendered text** of recall(query=…), and notice
 * along the way which ones are mind entries.
 *
 * ⚠️ **This reads Loci's display format, not a structured API.** Loci exposes only the
 * MCP `recall` tool, which returns a block of prose meant for a human; there is no
 * separate "give me the scores as JSON" endpoint, and adding one to Loci was out of
 * scope here. The format is copied from the default view of `_render_search` in
 * `src/tools/recall/core.py` (currently "time + score by default, the same for a bare
 * query, 🧠 badge = mind, room codes withdrawn"):
 *   `{score:5.1f}  [🧠]{摘要}  ({短id})  {MM-DD}`
 * The score on that line is on a **0~100** scale (Loci's own `RELEVANCE_FLOOR` defaults
 * to 35), the same ruler as this module's `RELEVANCE_MIN_SCORE`, so nothing needs
 * converting.
 * **This coupling is the known pit of this implementation**: change the layout of that
 * line on Loci's side and the score and the badge stop being picked up here, and
 * anything not picked up is treated as no hit at all (nothing blows up — the round is
 * simply one reminder poorer). Called out in the handover.
 */
function parse_score_line(text) {
  const entries = [];
  for (const line of String(text || "").split(/\r?\n/)) {
    const m = /^\s*([0-9]+(?:\.[0-9]+)?)\s{2,}.*?\(([0-9a-zA-Z]{4,})\)/.exec(line);
    if (!m) continue;
    entries.push({ id: m[2], score100: Number(m[1]), isMind: line.includes("🧠") });
  }
  return entries;
}

// The injected wording: **counts only, never content.** That was the literal
// requirement, and it is the easiest pit to fall into — casually carrying the summaries
// along turns it into "the system remembered for me", which is precisely the half that
// was cut out.
// Starting the text with 「〔记忆提醒〕」 is a **literal contract**: the host's
// `moveSystemPatchesBeforeLatestUser` recognises this prefix and grants it an exemption
// (see isReminderPinnedAfterLatestUser over there). That exemption is defensive for
// now: in the current implementation the reminder line never goes through that internal
// messages pipeline at all (see build_relevance_notice's doc comment below), so this
// function cannot reach it today. If some later change routes it back into that
// pipeline, the exemption catches it instead of quietly moving it back before the user
// message. Change this prefix and you change it over there too.
function build_notice_line(event_count, mind_count) {
  return `〔记忆提醒〕和这句有关：事件 ${event_count} 条 · 认知 ${mind_count} 条`;
}

// Pin it at the **true tail** of the whole messages array (after the latest user
// message): closest to the moment the model speaks, highest hit rate — the same gesture
// a hook uses when it appends context after the user prompt.
// 🔴 **This function is not called from inside this module.** build_relevance_notice
// only computes, and only puts the patch into record.patch in its return value; the
// actual push is left to server.js, after it has assembled outgoingBody (the copy that
// really goes upstream). The reason is in build_relevance_notice's doc comment: the
// internal "messages" inside server.js has to clear three validation/rebuild stages
// before it goes out, and all three assume that whatever follows the latest user
// message can only be a legal tool continuation — slip a system message in there and it
// is either swallowed or answered with a 400. The only genuinely safe position is
// **after validation passes and outgoingBody is assembled**, not any stop along that
// internal messages pipeline. This helper lives here so server.js (and the tests) can
// reuse the same "push to the true tail" gesture; the module does not use it itself.
function attach_at_true_tail(messages, patch) {
  if (Array.isArray(messages)) messages.push(patch);
}

/**
 * Relevance reminder: **only runs when triggered.** Strong = keyword hit; weak = a local
 * heuristic (see the comment on weak_triggered) still finding something of substance
 * after the interjections are filtered out. If neither fires, this round calls recall
 * zero times, writes one "not triggered" log line, and returns.
 *
 * When it does fire, it calls Loci's recall with the user's sentence as the query and
 * counts only entries whose rendered score is ≥ `最低分` (default 50, overridden by env
 * `RELEVANCE_MIN_SCORE`), keeping event and mind counts apart. On a hit it puts
 * `record.patch = { role: "system", content: ... }` into the return value —
 * **this function never touches messages itself** — and on 0 hits record.patch is simply
 * undefined.
 *
 * 🔴 Why it does not insert itself the way the breath paste does: the position it needs
 * is "after the latest user message, at the true tail of the whole messages array", but
 * the internal `messages` inside server.js (the copy used for the rolling summary and
 * for tool-chain validation) has to clear three stages before it actually goes out, and
 * all three assume that whatever follows the latest user message can only be a legal
 * tool continuation:
 *   ① `restoreLatestRequestTail` rebuilds everything after the latest user message from
 *      the client's original request, so anything slipped in that is not the client's
 *      own text is **silently swallowed** (tried it; it really is);
 *   ② `validateMessageSequence` (the pass at the end of server.js) sees a
 *      non-assistant-tool_calls message after the latest user message and answers a hard
 *      **400** (`non_tool_message_after_latest_user`) — a refusal, not a silence;
 *   ③ `moveSystemPatchesBeforeLatestUser` moves it back to before the user message (the
 *      host's messages module already exempts anything starting with 「〔记忆提醒〕」).
 * Exempting ③ alone is not enough — without exemptions for ①② the reminder line either
 * vanishes or the whole request fails to go through. The genuinely clean answer is never
 * to let it into that internal messages array at all: build_relevance_notice only
 * computes the patch, and server.js pushes it onto the true tail of `outgoingBody` (the
 * actual wire payload going to DeepSeek/GLM) after ①②③ have all run. That copy is never
 * touched by those three internal checks a second time, so it is safe by construction.
 *
 * Computed and attached fresh every round: no state file is read or written, and whether
 * anything hit lives only in this one call's return value. The next request brings a
 * brand new messages array (the gateway rebuilds per request) and this function holds no
 * memory across calls either, so nothing can accumulate.
 * Failure (Loci down, a timeout, no parseable score line) never blocks the chat: it is
 * caught, one log line is written, nothing is injected this round, and the function
 * returns normally.
 */
async function build_relevance_notice({
  messages,
  requestId,
  now = new Date(),
  logPath = DEFAULT_LOG_PATH,
  地址: address = DEFAULT_ADDRESS,
  // 🔴 Raised from 5000 to 12000. Measured: one recall with a query against a library
  //    of 956 entries takes 5~7 seconds (vectors and BM25 both run), while the timeout
  //    sat at 5 seconds — **so this feature never once succeeded between the day it
  //    shipped and the day that was found**. Every call aborted, the hit count was
  //    permanently 0 and the patch permanently null, and because the failure only went
  //    to the log while the chat carried on as usual, there was nowhere at all to see
  //    that it was not working.
  //    Bigger libraries are slower, so this number should follow the library;
  //    env RELEVANCE_TIMEOUT_MS tunes it.
  超时毫秒: timeout_ms = Number(process.env.RELEVANCE_TIMEOUT_MS || 12000),
  最低分: min_score = Number(process.env.RELEVANCE_MIN_SCORE || DEFAULT_MIN_SCORE),
} = {}) {
  const user_text = latestUserText(messages).trim();
  const strong_matched = user_text ? strong_hits(user_text) : [];
  // 🔴 The weak trigger is **off by default** (env RELEVANCE_WEAK=1 turns it on).
  //    Not because it is inaccurate — because it is far too wide: the rule is "let
  //    anything through that still has three characters of substance once the
  //    interjections are filtered out", and almost every sentence of ordinary speech
  //    clears that. At 5~7 seconds per recall, that means **six seconds of stall every
  //    single round**. The strong trigger (words like 「上次」「还记得」「之前」) fires
  //    rarely, so the waiting happens when waiting is worth it. Anyone who wants both
  //    can turn it on.
  const weak_enabled = String(process.env.RELEVANCE_WEAK || "").trim() === "1";
  const weak_matched = weak_enabled && Boolean(user_text) && strong_matched.length === 0 && weak_triggered(user_text);
  const triggered = strong_matched.length > 0 || weak_matched;

  const record = {
    time: now.toISOString(),
    request_id: requestId,
    actor: "gateway/relevance_reminder",
    action: "relevance_reminder_observed",
    triggered: triggered,
    trigger_kind: strong_matched.length > 0 ? "strong" : (weak_matched ? "weak" : "none"),
    strong_matched_keywords: strong_matched,
    min_score: min_score,
    recall_called: false,
    event_count: 0,
    mind_count: 0,
    injected: false,
  };

  if (!user_text || !triggered) {
    record.skipped = !user_text ? "no_user_text" : "not_triggered";
    log_line(logPath, record);
    return record;
  }

  record.recall_called = true; // triggered counts as a call attempted; whether it succeeded is a separate matter (the error field)
  try {
    const client = make_client({ address, timeout_ms });
    const text = await client.call_tool("recall", { query: user_text.slice(0, 120) });
    const passed = parse_score_line(text).filter(entry => entry.score100 >= min_score);
    const mind_entries = passed.filter(entry => entry.isMind);
    const event_entries = passed.filter(entry => !entry.isMind);
    record.event_count = event_entries.length;
    record.mind_count = mind_entries.length;
    // The log keeps ids so the evidence chain can be followed, **never the text** — the same discipline as the injected line.
    record.matched_ids = passed.map(entry => entry.id);

    if (passed.length > 0) {
      // 🔴 Compute only, never touch messages — the actual push is left to server.js,
      // once outgoingBody is assembled. Reasons in ①②③ of the doc comment above.
      record.patch = { role: "system", content: build_notice_line(event_entries.length, mind_entries.length) };
      record.injected = true;
    }
  } catch (err) {
    record.error = String(err?.message || err);
  }

  log_line(logPath, record);
  return record;
}

module.exports = {
  MARKER,
  DEFAULT_ADDRESS,
  DEFAULT_STATE_PATH,
  DEFAULT_LOG_PATH,
  STRONG_WORDS,
  DEFAULT_MIN_SCORE,
  attach_once,
  build_relevance_notice,
  // attach_at_true_tail: this is what server.js uses to actually push record.patch once
  // outgoingBody is assembled (not _internal — it is a gesture production code needs,
  // not just the tests).
  attach_at_true_tail,

// ── Deprecated aliases: the names these used to have. Gone in the next major. ────────
// 🔴 Renaming the bindings to English is an internal matter, but these keys are **names
//    other people type by hand after require()** — drop them and their code breaks on
//    the spot, on a name they cannot type. One line each costs nothing.
  默认地址: DEFAULT_ADDRESS,
  默认状态档: DEFAULT_STATE_PATH,
  默认日志档: DEFAULT_LOG_PATH,
  强档关键词: STRONG_WORDS,
  默认最低分: DEFAULT_MIN_SCORE,

// ── Backward-compatible aliases ──────────────────────────────────────────────────────
// 🔴 **Aliases only: each one points at the same function as its formal name.** The
//    formal names are English already, so new code should use those directly —
//    build_relevance_notice and attach_at_true_tail. These shorter spellings stay
//    because they are already written into code elsewhere, and breaking a name someone
//    has typed costs them far more than one line costs us.
  // computeReminder({ messages, requestId, 地址, 最低分 }) → { patch, ... }
  computeReminder: build_relevance_notice,
  // appendToTail(messages, patch) — the last step, once the request body is assembled
  appendToTail: attach_at_true_tail,
  paste: attach_once,
  MARKER_LINE: MARKER,
  // DEFAULT_ADDRESS / DEFAULT_MIN_SCORE / STRONG_WORDS are the formal names now, exported above.

  _internal: { make_client, latestUserText, strong_hits, weak_triggered, parse_score_line, build_patch_text, build_notice_line },
};
