// ============================================================
// gateway/present/stream_filter.js — what the client gets back, and what the gateway reads
// out of it on the way
//
// Two things are taken out of the answer before the client sees it; every other byte
// passes exactly as upstream sent it.
//
// ─── The usage chunk ───
// The fill % of a window is read from the provider's `usage.prompt_tokens`. A streamed
// answer only carries usage when the request asked for it
// (`stream_options.include_usage`), so the gateway asks on the client's behalf. When the
// client did not ask, the one chunk that exists only because of that — the usage chunk,
// `choices: []` plus `usage` — is taken out of what the client receives (a provider that
// also puts `"usage": null` on each chunk sends that to the client too: it is upstream's
// byte, and harmless). A provider that puts usage inside a chunk that also carries
// choices keeps that chunk: the choice is part of the answer.
//
// ─── The summary block ───
// When he folds the window himself (present/prompts.js compress_self_shell) he writes
//     【窗口摘要】…【/窗口摘要】
// inside his reply. 🔴 The client never sees a character of it, in any shape the answer
// comes in, whether or not he puts the markers where he was told:
//   · the opening marker counts wherever it stands: at a line start, after a sentence
//     (「好的。【窗口摘要】…」), after spaces or markdown (`**【窗口摘要】**`, `> 【窗口摘要】`).
//     Decoration standing alone before it on its line, and after the closing marker up
//     to the end of that line, goes with the block;
//   · the closing marker counts anywhere once a block is open;
//   · what is only a mention stays text: the marker right after an opening quote or
//     bracket (「【窗口摘要】」, `【窗口摘要】`) or right before a closing one, and a closed
//     "block" with no letter or digit inside (「用【窗口摘要】…【/窗口摘要】包起来」). The
//     words without their brackets are never a marker;
//   · every text field of every choice is scanned, each with its own scanner: the
//     answer's `content` and the thinking some providers stream beside it
//     (`reasoning_content`, `reasoning`), in a chunk's `delta` or in its `message`.
//     Only choice 0's `content` is the reply; a block anywhere else is taken out and
//     never kept;
//   · a streamed answer is read event by event (an event is complete bytes up to its
//     blank line, so a marker cut inside a multi-byte character is whole again by then),
//     and each scanner carries its state from one event's text to the next, so a marker
//     split across events is still found. Only text that could still turn out to belong
//     to a block is held back; as soon as it cannot, it is released, in the next event
//     that carries that field;
//   · a plain JSON answer is read whole and its message texts scanned the same way;
//   · a block that never closes — the stream ended or was cut first, or the model just
//     stopped — is dropped from the output like a closed one, and never captured.
// An event the scanners did not change goes out as the bytes that came in; one they did
// change is written again with only its texts replaced, and one left with no text and
// nothing else to say is not sent at all.
// What the gateway keeps of a block (present/index.js): a closed one becomes the next
// window's carry; a half one is never stored anywhere. The day store gets the text the
// client saw — split_summary() over the reply is the same scanner over the whole text,
// so the two agree by construction. The gateway's own turns (own_turn.js) put every
// answer through split_summary() too: a wake, a pack and a report get the visible words
// and the block apart, never one inside the other.
//
// ─── The prefix block ───
// One thing is added: on the turn that shows what he said while she was away
// (present/away.js), the block goes in front of the answer — create_sse_prefix /
// create_json_prefix, piped after the filters above, so the summary scanner never reads
// it and starts on the model's own first character exactly as it would without it.
//   · streamed: the first event the client gets is one more chunk carrying the block as
//     choice 0's text (role assistant), with the id, object, created, model and
//     system_fingerprint of upstream's first chunk; upstream's events follow unchanged.
//     Comment lines and events with no data that come before it pass first (they are
//     not chunks).
//   · plain JSON: the block is put in front of choice 0's message text (a tool-call
//     answer with no text gets the block as its text).
// ============================================================

const { Transform } = require("stream");

const OPEN = "【窗口摘要】";
const CLOSE = "【/窗口摘要】";

/** Ask for usage on a streamed request; returns { body, strip } — strip = the client did not ask itself. */
function with_usage(body) {
  if (!body || body.stream !== true) return { body, strip: false };
  if (body.stream_options && body.stream_options.include_usage === true) return { body, strip: false };
  return { body: { ...body, stream_options: { ...(body.stream_options || {}), include_usage: true } }, strip: true };
}

/** The data payload of one SSE event (its `data:` lines joined), or null when it has none. */
function event_data(text) {
  const parts = [];
  for (const raw of text.split("\n")) {
    const line = raw.replace(/\r$/, "");
    if (line.startsWith("data:")) parts.push(line.slice(5).replace(/^ /, ""));
  }
  return parts.length ? parts.join("\n") : null;
}

function parse_chunk(text) {
  const data = event_data(text);
  if (!data || data === "[DONE]") return { data, chunk: null };
  try {
    const chunk = JSON.parse(data);
    return { data, chunk: chunk && typeof chunk === "object" ? chunk : null };
  } catch { return { data, chunk: null, broken: true }; }
}

function is_usage_only(text) {
  const { chunk } = parse_chunk(text);
  if (!chunk || !chunk.usage || typeof chunk.usage !== "object") return false;
  return !Array.isArray(chunk.choices) || chunk.choices.length === 0;
}

// ———— The scanner: text in, text the client may see out ————

// Markdown and spacing that may stand around a marker on its own line.
const DECORATION = new Set([" ", "\t", "　", "*", "_", "~", ">", "#", "-"]);
// Right before the opening marker: the marker is being quoted, not used.
const QUOTE_OPEN = new Set(["「", "『", "“", "‘", "\"", "'", "`", "《", "〈", "（", "(", "【", "["]);
// Right after the opening marker: the same.
const QUOTE_CLOSE = new Set(["」", "』", "”", "’", "\"", "'", "`", "》", "〉", "）", ")", "】", "]"]);
const HAS_WORDS = /[\p{L}\p{N}]/u;

/** A block's text as it may be kept: trimmed, with the emphasis around it gone. */
function block_text(raw) {
  return String(raw ?? "").replace(/^[\s*_~]+|[\s*_~]+$/g, "");
}

/**
 * Carries the block state across pieces of one text field. The output does not depend
 * on how the text is cut into pieces.
 *   push(text)  → the part of the text that may be shown now
 *   finish()    → whatever was held back and turned out not to belong to a block; a block
 *                 still open is dropped and counted as half
 *   result()    → { summary: closed blocks' text joined or null, half: bool }
 *   half_text() → what the last block that never closed held, or "" — for a pack only,
 *                 whose whole answer is meant to be the summary (pack.js)
 */
function create_summary_scanner() {
  let inside = false;
  let head = true;         // nothing but decoration on this line so far
  let lead = "";           // that decoration, held back while it may belong to a marker
  let hold = "";           // what could still become the opening marker
  let prev = "\n";         // the character before `hold`
  let block = "";
  let block_lead = "";     // decoration taken with the block, given back if it was a mention
  let first = false;       // the next character is the first inside the block
  let after_close = false;
  let trail = "";          // decoration after a closing marker, dropped at the line's end
  const closed = [];
  let half = false;
  let half_raw = "";

  function push(text) {
    let out = "";
    for (const c of String(text ?? "")) {
      if (inside) {
        if (first) {
          first = false;
          if (QUOTE_CLOSE.has(c)) {   // 【窗口摘要】」 — a mention
            out += block_lead + OPEN + c;
            inside = false; head = false; prev = c;
            continue;
          }
        }
        block += c;
        if (block.endsWith(CLOSE)) {
          const raw = block.slice(0, -CLOSE.length);
          inside = false;
          head = false;
          block = "";
          if (!HAS_WORDS.test(raw)) {   // nothing written inside — a mention
            out += block_lead + OPEN + raw + CLOSE;
            prev = "】";
          } else {
            closed.push(raw);
            after_close = true;
            trail = "";
            prev = "】";
          }
        }
        continue;
      }
      if (after_close) {
        if (c !== "\n" && DECORATION.has(c)) { trail += c; continue; }
        if (c !== "\n") out += trail;
        trail = "";
        after_close = false;
      }
      // right after an opening quote (「【窗口摘要】) the marker is a mention: nothing is held
      if (hold || (c === OPEN[0] && !QUOTE_OPEN.has(prev))) {
        const candidate = hold + c;
        if (OPEN.startsWith(candidate)) {
          if (candidate !== OPEN) { hold = candidate; continue; }
          hold = "";
          inside = true;
          first = true;
          block = "";
          block_lead = lead;
          lead = "";
          continue;
        }
        // not the marker after all: what was held is text, and this character starts over
        out += lead + hold;
        lead = "";
        head = false;
        prev = hold[hold.length - 1];
        hold = "";
        if (c === OPEN[0] && !QUOTE_OPEN.has(prev)) { hold = c; continue; }
      }
      if (head && DECORATION.has(c)) { lead += c; prev = c; continue; }
      out += lead + c;
      lead = "";
      head = c === "\n";
      prev = c;
    }
    return out;
  }

  function finish() {
    let out = "";
    if (inside) {
      half = true;
      half_raw = block;
      inside = false; block = ""; block_lead = ""; first = false;
    } else out = lead + hold;
    lead = ""; hold = ""; trail = "";
    after_close = false;
    head = true;
    prev = "\n";
    return out;
  }

  function result() {
    const text = closed.map(block_text).filter(Boolean).join("\n\n");
    return { summary: text || null, half };
  }

  return {
    push, finish, result,
    half_text: () => block_text(half_raw),
    holding: () => inside || hold.length > 0 || lead.length > 0 || trail.length > 0,
  };
}

/** The whole text at once: { visible, summary, half } — exactly what the stream filter shows and keeps. */
function split_summary(text) {
  const s = create_summary_scanner();
  const visible = s.push(text) + s.finish();
  return { visible, ...s.result() };
}

/** Could these raw bytes hold an opening marker (literally or \u-escaped)? */
function mentions_open(raw) {
  return raw.includes("【") || /\\u3010/i.test(raw);
}

// ———— SSE ————

function eol_of(event_text) {
  return /\r\n\r\n$/.test(event_text) ? "\r\n" : "\n";
}

/** The same event with its data replaced by `chunk`; other lines (event:, id:, comments) kept. */
function rewrite_event(event_text, chunk) {
  const eol = eol_of(event_text);
  const kept = event_text.split(/\r?\n/).filter((l) => l !== "" && !l.startsWith("data:"));
  return [...kept, `data: ${JSON.stringify(chunk)}`].join(eol) + eol + eol;
}

// The text fields of a choice's `delta` / `message` the model writes into.
const TEXT_FIELDS = ["content", "reasoning_content", "reasoning", "refusal"];
const CHOICE_KEYS = new Set(["index", "delta", "message", "logprobs", "finish_reason"]);

/**
 * One scanner per choice and text field of one answer. Choice 0's `content` is the reply:
 * it uses `main` when one is given, so the caller can read what was captured.
 */
function create_scanners(main = null) {
  const all = new Map();
  return {
    get(index, field) {
      const key = `${index}|${field}`;
      if (!all.has(key)) all.set(key, index === 0 && field === "content" && main ? main : create_summary_scanner());
      return all.get(key);
    },
    /** [[index, field, scanner], …] for the given choice, or for every choice. */
    list(index = null) {
      const out = [];
      for (const [key, s] of all) {
        const [i, field] = key.split("|");
        if (index === null || Number(i) === index) out.push([Number(i), field, s]);
      }
      return out;
    },
    holding() { for (const s of all.values()) if (s.holding()) return true; return false; },
  };
}

/** Scan the text fields of one `delta` or `message` in place; true when any of them changed. */
function scan_box(box, index, scanners, { whole = false } = {}) {
  let changed = false;
  for (const field of TEXT_FIELDS) {
    const v = box[field];
    if (typeof v === "string") {
      const s = scanners.get(index, field);
      const shown = s.push(v) + (whole ? s.finish() : "");
      if (shown !== v) { box[field] = shown; changed = true; }
    } else if (Array.isArray(v)) {
      const parts = v.filter((p) => p && typeof p === "object" && typeof p.text === "string");
      if (!parts.length) continue;
      const s = scanners.get(index, field);
      parts.forEach((p, k) => {
        const shown = s.push(p.text) + (whole && k === parts.length - 1 ? s.finish() : "");
        if (shown !== p.text) { p.text = shown; changed = true; }
      });
    }
  }
  return changed;
}

/** Put released text at the end of a box's field. */
function append_text(box, field, text) {
  const v = box[field];
  if (Array.isArray(v)) v.push({ type: "text", text });
  else box[field] = (typeof v === "string" ? v : "") + text;
}

/** An event that only carries `field` text for one choice, shaped after the last chunk seen. */
function text_event(template, index, field, text) {
  const chunk = {};
  for (const k of ["id", "object", "created", "model", "system_fingerprint"]) if (template && k in template) chunk[k] = template[k];
  chunk.choices = [{ index, delta: { [field]: text } }];
  return Buffer.from(`data: ${JSON.stringify(chunk)}\n\n`);
}

/** A rewritten chunk that no longer says anything: no text, no finish, no usage, nothing else. */
function says_nothing(chunk) {
  const empty_box = (b) => b === undefined || b === null
    || (typeof b === "object" && Object.entries(b).every(([k, v]) => TEXT_FIELDS.includes(k)
      && (v === "" || v === null || (Array.isArray(v) && v.every((p) => p?.text === "")))));
  return !chunk.usage && chunk.choices.every((c) => c && typeof c === "object" && !c.finish_reason
    && Object.keys(c).every((k) => CHOICE_KEYS.has(k)) && !c.logprobs && empty_box(c.delta) && empty_box(c.message));
}

/**
 * A pass-through for an SSE body. Works on bytes: an event is everything up to and
 * including the blank line that ends it.
 * @param strip_usage  drop usage-only events (the client did not ask for usage)
 * @param summary      take summary blocks out of every choice's text fields
 * @param scanner      create_summary_scanner() to use for choice 0's content (lets the
 *                     caller read result()); a fresh one when not given
 */
function create_sse_filter({ strip_usage = false, summary = true, scanner = null } = {}) {
  const scanners = summary ? create_scanners(scanner) : null;
  let pending = Buffer.alloc(0);
  let template = null;

  /** Release what every scanner (of one choice, or all) still holds, as events of their own. */
  function release(transform, index = null) {
    for (const [i, field, s] of scanners.list(index)) {
      const tail = s.finish();
      if (tail) transform.push(text_event(template, i, field, tail));
    }
  }

  function take(event_buf, transform) {
    const event_text = event_buf.toString("utf8");
    if (strip_usage && is_usage_only(event_text)) return;
    if (!scanners) { transform.push(event_buf); return; }
    const { data, chunk, broken } = parse_chunk(event_text);
    if (broken) {
      // An event that does not parse (most often the last one, cut off with the stream)
      // cannot be read by the client either; while a block is open, or when it may hold
      // a marker, it is not sent at all.
      if (scanners.holding() || mentions_open(event_text)) return;
      transform.push(event_buf);
      return;
    }
    if (data === "[DONE]") {
      release(transform);
      transform.push(event_buf);
      return;
    }
    if (!chunk || !Array.isArray(chunk.choices) || !chunk.choices.length) { transform.push(event_buf); return; }
    template = chunk;
    // `chunk` is this event's own parse: changed in place, and written again only if it did
    let changed = false;
    for (const choice of chunk.choices) {
      if (!choice || typeof choice !== "object") continue;
      const index = Number.isInteger(choice.index) ? choice.index : 0;
      let here = false;
      for (const name of ["delta", "message"]) {
        const box = choice[name];
        if (box && typeof box === "object" && !Array.isArray(box)) here = scan_box(box, index, scanners) || here;
      }
      if (choice.finish_reason) {
        for (const [, field, s] of scanners.list(index)) {
          const tail = s.finish();
          if (!tail) continue;
          const box = choice.delta && typeof choice.delta === "object" ? choice.delta
            : choice.message && typeof choice.message === "object" ? choice.message : (choice.delta = {});
          append_text(box, field, tail);
          here = true;
        }
      }
      // token log-probabilities spell out the text: a choice whose text changed loses them
      if (here && choice.logprobs) choice.logprobs = null;
      changed = changed || here;
    }
    if (!changed) { transform.push(event_buf); return; }
    if (says_nothing(chunk)) return;
    transform.push(Buffer.from(rewrite_event(event_text, chunk)));
  }

  return new Transform({
    transform(chunk, _enc, done) {
      pending = pending.length ? Buffer.concat([pending, chunk]) : Buffer.from(chunk);
      let start = 0;
      let line_start = 0;
      for (let i = 0; i < pending.length; i++) {
        if (pending[i] !== 0x0a) continue;
        const line_end = i > line_start && pending[i - 1] === 0x0d ? i - 1 : i;
        if (line_end === line_start) {
          // a blank line: the event [start, i] is complete
          take(pending.subarray(start, i + 1), this);
          start = i + 1;
        }
        line_start = i + 1;
      }
      pending = pending.subarray(start);
      done();
    },
    flush(done) {
      if (pending.length) take(pending, this);
      pending = Buffer.alloc(0);
      // the stream ended without [DONE] or a finish_reason: release what was held back
      // (it was not a block); an open block is dropped
      if (scanners) release(this);
      done();
    },
  });
}

/** The usage strip alone (no summary scanning). */
function create_usage_strip() {
  return create_sse_filter({ strip_usage: true, summary: false });
}

// ———— Plain JSON ————

/**
 * A pass-through for a non-streamed answer: read whole, every choice's message text fields
 * scanned (choice 0's content with `scanner` when given). An answer that was not changed
 * goes out as the bytes that came in. One that does not parse (cut short) is sent as it
 * came, up to any opening marker it may hold.
 */
function create_json_filter({ scanner = null } = {}) {
  const scanners = create_scanners(scanner);
  const parts = [];
  return new Transform({
    transform(chunk, _enc, done) { parts.push(Buffer.from(chunk)); done(); },
    flush(done) {
      const raw_buf = Buffer.concat(parts);
      const raw = raw_buf.toString("utf8");
      let body = null;
      try { body = JSON.parse(raw); } catch { body = null; }
      if (!body) {
        if (!mentions_open(raw)) this.push(raw_buf);
        else {
          const at = Math.min(...[raw.indexOf("【"), raw.search(/\\u3010/i)].filter((i) => i >= 0));
          this.push(Buffer.from(raw.slice(0, at)));
        }
        return done();
      }
      let changed = false;
      for (const choice of Array.isArray(body.choices) ? body.choices : []) {
        const box = choice && typeof choice === "object" ? choice.message : null;
        if (!box || typeof box !== "object" || Array.isArray(box)) continue;
        const here = scan_box(box, Number.isInteger(choice.index) ? choice.index : 0, scanners, { whole: true });
        if (here && choice.logprobs) choice.logprobs = null;
        changed = changed || here;
      }
      this.push(changed ? Buffer.from(JSON.stringify(body)) : raw_buf);
      done();
    },
  });
}

// ———— The prefix block (present/away.js) ————

/** The chunk that carries the prefix, shaped after upstream's first chunk. */
function prefix_event(template, prefix) {
  const chunk = {};
  for (const k of ["id", "object", "created", "model", "system_fingerprint"]) if (template && k in template) chunk[k] = template[k];
  if (!chunk.object) chunk.object = "chat.completion.chunk";
  chunk.choices = [{ index: 0, delta: { role: "assistant", content: prefix }, finish_reason: null }];
  return Buffer.from(`data: ${JSON.stringify(chunk)}\n\n`);
}

/** An SSE pass-through that sends `prefix` as the first chunk; every upstream byte follows unchanged. */
function create_sse_prefix(prefix) {
  let pending = Buffer.alloc(0);
  let sent = false;

  function take(event_buf, transform) {
    if (!sent) {
      const { data, chunk } = parse_chunk(event_buf.toString("utf8"));
      if (data === null) { transform.push(event_buf); return; }
      transform.push(prefix_event(chunk, prefix));
      sent = true;
    }
    transform.push(event_buf);
  }

  return new Transform({
    transform(chunk, _enc, done) {
      if (sent) { this.push(chunk); return done(); }
      pending = pending.length ? Buffer.concat([pending, chunk]) : Buffer.from(chunk);
      let start = 0;
      let line_start = 0;
      for (let i = 0; i < pending.length && !sent; i++) {
        if (pending[i] !== 0x0a) continue;
        const line_end = i > line_start && pending[i - 1] === 0x0d ? i - 1 : i;
        if (line_end === line_start) {
          take(pending.subarray(start, i + 1), this);
          start = i + 1;
        }
        line_start = i + 1;
      }
      const rest = pending.subarray(start);
      pending = Buffer.alloc(0);
      if (sent) { if (rest.length) this.push(rest); } else pending = rest;
      done();
    },
    flush(done) {
      // the stream ended before a complete event: what came is passed on as it came
      if (pending.length) this.push(pending);
      pending = Buffer.alloc(0);
      done();
    },
  });
}

/** A plain-JSON pass-through that puts `prefix` in front of choice 0's message text. */
function create_json_prefix(prefix) {
  const parts = [];
  return new Transform({
    transform(chunk, _enc, done) { parts.push(Buffer.from(chunk)); done(); },
    flush(done) {
      const raw_buf = Buffer.concat(parts);
      let body = null;
      try { body = JSON.parse(raw_buf.toString("utf8")); } catch { body = null; }
      const message = body?.choices?.find?.((c) => (c?.index ?? 0) === 0)?.message;
      if (!message || typeof message !== "object") { this.push(raw_buf); return done(); }
      if (Array.isArray(message.content)) {
        const first = message.content.find((p) => p && typeof p === "object" && typeof p.text === "string");
        if (first) first.text = prefix + first.text;
        else message.content.unshift({ type: "text", text: prefix });
      } else message.content = prefix + (typeof message.content === "string" ? message.content : "");
      this.push(Buffer.from(JSON.stringify(body)));
      done();
    },
  });
}

module.exports = {
  create_sse_prefix, create_json_prefix,
  with_usage, create_usage_strip, create_sse_filter, create_json_filter,
  create_summary_scanner, split_summary, is_usage_only, event_data, OPEN, CLOSE,
};
