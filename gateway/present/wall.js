// ============================================================
// gateway/present/wall.js — the way out when upstream says the context is too long
//
// A window size set too big (or a provider that cuts windows below the model's paper
// size) ends in upstream refusing the turn: "the context is too long". Without a way out
// of its own that turn fails, and so does every later one — the fill is never measured
// on a refused turn, so nothing below it ever moves (lesson ⑥; Lento once had a wall
// turn report no usage, the fill froze and the window never escaped). So:
//
//   ① recognise the refusal (WALL_PATTERNS below) — only on a 4xx other than 401 / 403 /
//      429 (a wrong key, a forbidden model, a rate or quota limit are not about length)
//   ② learn the real window for this model (context_window.js learn(), source `learned`):
//      the limit the error names, else the last prompt_tokens a turn on this model got
//      through with (the owner's ruling, blueprint §七.4, 10-08), else nothing new
//   ③ cut: drop the oldest raw lines after the mark — whole turns, each starting at a
//      line of hers, with the overlays and tool round trips inside them — until the
//      estimate is under the force line of that window. The client's system messages,
//      the carry and the current turn (her line onwards) are never dropped.
//   ④ resend once; 「撞墙」 goes to logs/present.jsonl (numbers only, never text)
//   ⑤ ask for a pack (pack.js, how "forced"), so the next turns stop hitting the wall —
//      unless compress.on is false: then every turn that hits it is cut again, but
//      nothing is folded behind the owner's back
//
// It never gets stuck:
//   · the cut never depends on the fill: every request that hits the wall is cut, so a
//     turn whose resend also failed does not stop the next turn from escaping;
//   · when the resend also fails, the window's fill is set from the estimate against the
//     learned size (marked estimated) instead of being left where it froze;
//   · the target is at most force_pct of what was refused, whatever the window says, so
//     every escape drops something even when the limit is unknown;
//   · estimates are only rough (fill.js), so the target is scaled by how far estimates
//     have been off on this model — the real token count the error names, else the last
//     turn's real prompt_tokens over its estimate — and every resend that hits the wall
//     again tightens the next escape on that model (SQUEEZE, held in memory).
//
// The recognised refusals (case-insensitive; a body matches if any of these is in it):
//   context_length_exceeded                           OpenAI's code (also DeepSeek,
//                                                     OpenRouter, most OpenAI-compatible)
//   "maximum context length is N tokens"              OpenAI / vLLM message
//   "reduce the length of the messages"               OpenAI message
//   "prompt is too long" ("…: A tokens > N maximum")  Anthropic
//   "input length and max_tokens exceed context limit: A + B > N"   Anthropic
//   "exceeds the context window" / "context window exceeded"        Responses-style, others
//   "context length … exceed" / "exceed … context length"           Ollama, llama.cpp, others
//   "input token count (A) exceeds the maximum number of tokens allowed (N)"   Gemini
//   "exceeded model token limit: N"                   Moonshot / Kimi
//   "too large for model with N maximum context length"   Mistral
//   "range of input length should be [1, N]"          Qwen / DashScope
//   "input is too long" / "prompt too long" / "too many tokens"     generic
//   上下文…超 / 超…上下文 / 输入…过长 / prompt 超长        Chinese providers (GLM and others)
// ============================================================

const fs = require("fs");
const path = require("path");
const { estimate_prompt, measure } = require("./fill.js");
const { local_stamp } = require("./clock.js");

const WALL_PATTERNS = [
  { name: "context_length_exceeded", re: /context_length_exceeded/i },
  { name: "maximum_context_length", re: /maximum context length is/i },
  { name: "reduce_length", re: /reduce the length of the messages/i },
  { name: "prompt_too_long", re: /prompt (?:is )?too long/i },
  { name: "exceed_context_limit", re: /exceeds? (?:the )?context limit/i },
  { name: "context_window", re: /exceeds? (?:the |its |this model's )?(?:maximum )?context window|context window (?:exceeded|is exceeded|overflow)/i },
  { name: "context_length", re: /context length.{0,60}exceed|exceed.{0,60}context length/i },
  { name: "max_tokens_allowed", re: /exceeds the maximum number of tokens allowed/i },
  { name: "token_limit", re: /exceeded model token limit/i },
  { name: "mistral", re: /too large for model with \d+ maximum context length/i },
  { name: "input_range", re: /range of input length should be/i },
  { name: "input_too_long", re: /input (?:is )?too long|too many (?:input )?tokens/i },
  { name: "chinese", re: /上下文.{0,12}(?:超|过长|太长)|超.{0,8}上下文|输入.{0,8}(?:过长|太长|超长)|prompt\s*超长/i },
];

const num = (s) => Number(String(s).replace(/[,_\s]/g, ""));

// Where the limit sits in each message shape, tried in order; the first that reads wins.
const LIMIT_READERS = [
  /maximum context length is\s*([\d,]+)\s*tokens/i,
  /context limit:\s*[\d,]+\s*\+\s*[\d,]+\s*>\s*([\d,]+)/i,
  /prompt is too long:\s*[\d,]+\s*tokens\s*>\s*([\d,]+)/i,
  /maximum number of tokens allowed\s*\(([\d,]+)\)/i,
  /exceeded model token limit:?\s*([\d,]+)/i,
  /too large for model with\s*([\d,]+)\s*maximum context length/i,
  /range of input length should be\s*\[\s*\d+\s*,\s*([\d,]+)\s*\]/i,
  /context (?:window|length|limit) (?:of|is)\s*([\d,]+)/i,
];

// Where the size of the refused request sits, when the message says it.
const ACTUAL_READERS = [
  /(?:your messages|you requested|resulted in)\D{0,20}([\d,]+)\s*tokens/i,
  /context limit:\s*([\d,]+)\s*\+/i,
  /prompt is too long:\s*([\d,]+)\s*tokens/i,
  /input token count\s*\(([\d,]+)\)/i,
  /prompt contains\s*([\d,]+)\s*tokens/i,
];

/**
 * Is this upstream answer the context-length wall?
 * @returns { hit: false } | { hit: true, pattern, limit: N | null, actual: N | null }
 */
function wall_error(status, text) {
  const s = Number(status);
  if (!(s >= 400 && s < 500) || s === 401 || s === 403 || s === 429) return { hit: false };
  const body = String(text || "");
  const found = WALL_PATTERNS.find((p) => p.re.test(body));
  if (!found) return { hit: false };
  const read = (readers) => {
    for (const re of readers) {
      const m = re.exec(body);
      if (m && Number.isFinite(num(m[1])) && num(m[1]) > 0) return num(m[1]);
    }
    return null;
  };
  return { hit: true, pattern: found.name, limit: read(LIMIT_READERS), actual: read(ACTUAL_READERS) };
}

/**
 * Drop the oldest turns between the kept head and the current turn until the estimate is
 * at most `target`. A turn starts at a user message; what sits before the first one in
 * the droppable stretch (an overlay placed before her line) goes with that first turn.
 * @param messages   the upstream messages as sent (not modified)
 * @param keep_head  how many leading messages never go (client system + carry)
 * @returns { messages, dropped, estimate }
 */
function cut_oldest(messages, { keep_head, target, tools = null }) {
  const est = (list) => estimate_prompt({ messages: list, tools });
  let last_user = -1;
  for (let i = messages.length - 1; i >= keep_head; i--) if (messages[i]?.role === "user") { last_user = i; break; }
  const head = messages.slice(0, keep_head);
  if (last_user <= keep_head) return { messages: messages.slice(), dropped: 0, estimate: est(messages) };
  const middle = messages.slice(keep_head, last_user);
  const current = messages.slice(last_user);
  // where a kept stretch may start: at a line of hers after the first one (an overlay
  // placed right before that line stays with it), or nothing kept at all
  const first_user = middle.findIndex((m) => m?.role === "user");
  const cuts = [];
  for (let i = first_user + 1; first_user >= 0 && i < middle.length; i++) {
    if (middle[i]?.role !== "user") continue;
    let c = i;
    while (c - 1 > first_user && middle[c - 1]?.role === "system") c--;
    cuts.push(c);
  }
  cuts.push(middle.length);
  let from = 0;
  let out = messages.slice();
  for (const c of cuts) {
    if (est(out) <= target) break;
    from = c;
    out = [...head, ...middle.slice(c), ...current];
  }
  return { messages: out, dropped: from, estimate: est(out) };
}

const SQUEEZE_STEP = 0.75;
const SQUEEZE_MIN = 0.2;
const SCALE_MIN = 0.25;
const SCALE_MAX = 4;

/**
 * @param windows        context_window.js instance (resolve, learn)
 * @param threads        threads.js instance (the fill is set on a wall whose resend failed)
 * @param read_compress  () → present.json compress section
 * @param request_pack   (thread_id) → asks pack.js for a pack, how "forced"
 * @param data_root      LOCI_GATEWAY_DATA; 「撞墙」 lines go to logs/present.jsonl
 */
function create_wall_escape({ windows, threads, read_compress, request_pack, data_root, clock, zone, log = console.error }) {
  const log_file = path.join(data_root, "logs", "present.jsonl");
  const last_ok = new Map();   // model → { prompt_tokens, ratio }: the last turn that went through with usage
  const squeeze = new Map();   // model → factor < 1 after resends that hit the wall again

  /** A turn went through: what it cost, and how far the estimate was off. */
  function note_usage(model, usage, estimate) {
    const real = Number(usage?.prompt_tokens);
    if (!model || !Number.isFinite(real) || real <= 0) return;
    const est = Number(estimate);
    const ratio = Number.isFinite(est) && est > 0 ? real / est : null;
    last_ok.set(String(model), { prompt_tokens: real, ratio });
  }

  function write_line(entry) {
    try {
      fs.mkdirSync(path.dirname(log_file), { recursive: true });
      const at = zone ? local_stamp(clock.now(), zone).iso : new Date(clock.now()).toISOString();
      fs.appendFileSync(log_file, `${JSON.stringify({ at, event: "撞墙", ...entry })}\n`, "utf8");
    } catch (err) { log(`[gateway] present: the wall line was not written: ${err?.message || err}`); }
  }

  /** A Response carrying bytes already read, so the relay can pipe it as if untouched. */
  function rebuilt(resp, bytes) {
    return new Response(bytes, { status: resp.status, statusText: resp.statusText, headers: resp.headers });
  }

  /**
   * @param ctx   prepare()'s ctx: thread, window, model, estimate, forward (the body sent), keep_head
   * @param resp  upstream's non-2xx Response (its body not read yet)
   * @param send  (body object) → Promise<Response>, the same request again with this body
   * @returns the Response the relay should go on with: the resend's, or the original rebuilt
   */
  async function escape(ctx, resp, send) {
    let bytes;
    try { bytes = Buffer.from(await resp.arrayBuffer()); }
    catch { bytes = Buffer.alloc(0); }   // the error body broke off: pass the status on with what is left
    try {
      const wall = wall_error(resp.status, bytes.toString("utf8"));
      if (!wall.hit || !ctx || !ctx.forward || !Array.isArray(ctx.forward.messages)) return rebuilt(resp, bytes);
      const model = ctx.model ? String(ctx.model) : "";
      const before = estimate_prompt(ctx.forward);

      // ② learn
      let learned = null;
      if (wall.limit && windows.learn(model, wall.limit, "error")) learned = { tokens: wall.limit, how: "error" };
      else {
        const ok = last_ok.get(model);
        if (ok && windows.learn(model, ok.prompt_tokens, "last_ok")) learned = { tokens: ok.prompt_tokens, how: "last_ok" };
      }
      const size = windows.resolve(model);

      // ③ cut to the force line
      const values = read_compress() || {};
      const force = Number(values.force_pct) > 0 ? Number(values.force_pct) / 100 : 0.85;
      const known_ratio = wall.actual && before > 0 ? wall.actual / before : last_ok.get(model)?.ratio;
      const scale = Math.min(SCALE_MAX, Math.max(SCALE_MIN, Number(known_ratio) || 1));
      const sq = squeeze.get(model) || 1;
      const target = Math.floor(Math.min(size.tokens * force / scale, before * force) * sq);
      const cut = cut_oldest(ctx.forward.messages, { keep_head: ctx.keep_head || 0, target, tools: ctx.forward.tools });
      const body = { ...ctx.forward, messages: cut.messages };

      // ④ resend once
      let again = null;
      let again_error = null;
      try { again = await send(body); } catch (err) { again_error = String(err?.message || err); }
      let outcome;
      let out;
      if (again && again.ok) {
        outcome = "ok";
        ctx.forward = body;
        ctx.estimate = cut.estimate;
        ctx.walled = true;
        out = again;
      } else if (again) {
        const again_bytes = Buffer.from(await again.arrayBuffer().catch(() => new ArrayBuffer(0)));
        const again_wall = wall_error(again.status, again_bytes.toString("utf8"));
        if (again_wall.hit) squeeze.set(model, Math.max(SQUEEZE_MIN, sq * SQUEEZE_STEP));
        outcome = again_wall.hit ? `wall_again_${again.status}` : `http_${again.status}`;
        out = rebuilt(again, again_bytes);
      } else {
        outcome = "unsent";
        out = rebuilt(resp, bytes);
      }

      // the fill must not freeze on a refused turn: estimate it against the size just learned
      if (outcome !== "ok") {
        const thread = threads.get(ctx.thread);
        if (thread && thread.window && thread.window.name === ctx.window) {
          thread.window.usage = measure({ usage: null, estimate: Math.round(before * scale), window: windows.resolve(model),
                                          model: model || null, at: clock.now() });
          threads.save(thread.id);
        }
      }

      // ⑤ a pack, so the next turns stop hitting it
      const packing = values.on === true;
      if (packing) {
        try { request_pack(ctx.thread); } catch (err) { log(`[gateway] present: asking for a pack after the wall failed: ${err?.message || err}`); }
      }

      write_line({
        thread: ctx.thread, window: ctx.window, model: model || null, status: resp.status, pattern: wall.pattern,
        limit: wall.limit, learned, size: size.tokens, size_source: size.source,
        estimate_before: before, estimate_after: cut.estimate, target, dropped: cut.dropped,
        resend: outcome, error: again_error, pack: packing,
      });
      console.log(`[gateway] present ${ctx.thread} 撞墙 (${wall.pattern}${wall.limit ? ` · limit ${wall.limit}` : ""}): `
        + `dropped ${cut.dropped} old messages, resend ${outcome}${packing ? " · pack asked" : ""}`);
      return out;
    } catch (err) {
      log(`[gateway] present: the wall escape failed: ${err?.message || err}`);
      return rebuilt(resp, bytes);
    }
  }

  return { escape, note_usage };
}

module.exports = { create_wall_escape, wall_error, cut_oldest, WALL_PATTERNS };
