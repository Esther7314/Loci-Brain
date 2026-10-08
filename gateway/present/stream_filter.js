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
// comes in:
//   · the opening marker only counts at the start of a line (the very start of the reply,
//     or right after a "\n"): the same characters in the middle of a sentence are text;
//   · the closing marker counts anywhere once a block is open;
//   · a streamed answer is read event by event (an event is complete bytes up to its
//     blank line, so a marker cut inside a multi-byte character is whole again by then),
//     and the scanner below carries its state from one event's text to the next, so a
//     marker split across events is still found. Only text that could still turn out to
//     be the start of the opening marker is held back; as soon as it cannot, it is
//     released, in the next event that carries text;
//   · a plain JSON answer is read whole and its message text scanned the same way;
//   · a block that never closes — the stream ended or was cut first, or the model just
//     stopped — is dropped from the output like a closed one, and never captured.
// An event the scanner did not change goes out as the bytes that came in; one it did
// change is written again with only its text replaced, and one left with no text and
// nothing else to say is not sent at all.
// What the gateway keeps of a block (present/index.js): a closed one becomes the next
// window's carry; a half one is never stored anywhere. The day store gets the text the
// client saw — split_summary() over the reply is the same scanner over the whole text,
// so the two agree by construction.
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

/**
 * Carries the block state across pieces of one reply's text. The output does not depend
 * on how the text is cut into pieces.
 *   push(text) → the part of the text that may be shown now
 *   finish()   → whatever was held back and turned out not to be a marker; a block still
 *                open is dropped and counted as half
 *   result()   → { summary: closed blocks' text joined (trimmed) or null, half: bool }
 */
function create_summary_scanner() {
  let inside = false;
  let line_start = true;
  let hold = "";
  let block = "";
  const closed = [];
  let half = false;

  function push(text) {
    let out = "";
    for (const c of String(text ?? "")) {
      if (inside) {
        block += c;
        if (block.endsWith(CLOSE)) {
          closed.push(block.slice(0, -CLOSE.length));
          block = "";
          inside = false;
          line_start = false;
        }
        continue;
      }
      if (hold || line_start) {
        const candidate = hold + c;
        if (OPEN.startsWith(candidate)) {
          if (candidate === OPEN) { inside = true; hold = ""; block = ""; }
          else hold = candidate;
          line_start = false;
          continue;
        }
        if (hold) { out += hold; hold = ""; }
      }
      out += c;
      line_start = c === "\n";
    }
    return out;
  }

  function finish() {
    const out = hold;
    hold = "";
    if (inside) { half = true; inside = false; block = ""; }
    line_start = true;
    return out;
  }

  function result() {
    const text = closed.map((s) => s.trim()).filter(Boolean).join("\n\n");
    return { summary: text || null, half };
  }

  return { push, finish, result, holding: () => inside || hold.length > 0 };
}

/** The whole reply at once: { visible, summary, half } — exactly what the stream filter shows and keeps. */
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

/** An event that only carries text for choice 0, shaped after the last chunk seen. */
function text_event(template, text) {
  const chunk = {};
  for (const k of ["id", "object", "created", "model", "system_fingerprint"]) if (template && k in template) chunk[k] = template[k];
  chunk.choices = [{ index: 0, delta: { content: text } }];
  return Buffer.from(`data: ${JSON.stringify(chunk)}\n\n`);
}

/**
 * A pass-through for an SSE body. Works on bytes: an event is everything up to and
 * including the blank line that ends it.
 * @param strip_usage  drop usage-only events (the client did not ask for usage)
 * @param summary      take summary blocks out of choice 0's text
 * @param scanner      create_summary_scanner() to use (lets the caller read result());
 *                     a fresh one when not given
 */
function create_sse_filter({ strip_usage = false, summary = true, scanner = null } = {}) {
  const scan = summary ? (scanner || create_summary_scanner()) : null;
  let pending = Buffer.alloc(0);
  let template = null;

  function take(event_buf, transform) {
    const event_text = event_buf.toString("utf8");
    if (strip_usage && is_usage_only(event_text)) return;
    if (!scan) { transform.push(event_buf); return; }
    const { data, chunk, broken } = parse_chunk(event_text);
    if (broken) {
      // An event that does not parse (most often the last one, cut off with the stream)
      // cannot be read by the client either; while a block is open, or when it may hold
      // a marker, it is not sent at all.
      if (scan.holding() || mentions_open(event_text)) return;
      transform.push(event_buf);
      return;
    }
    if (data === "[DONE]") {
      const tail = scan.finish();
      if (tail) transform.push(text_event(template, tail));
      transform.push(event_buf);
      return;
    }
    const choice = chunk && Array.isArray(chunk.choices) ? chunk.choices.find((c) => (c?.index ?? 0) === 0) : null;
    if (!choice) { transform.push(event_buf); return; }
    template = chunk;
    const delta = choice.delta && typeof choice.delta === "object" ? choice.delta : null;
    const had_text = delta && typeof delta.content === "string";
    let text = had_text ? scan.push(delta.content) : "";
    if (choice.finish_reason) text += scan.finish();
    if (had_text ? text === delta.content : !text) { transform.push(event_buf); return; }
    // the text changed: write the event again with only its text replaced
    const fresh = JSON.parse(JSON.stringify(chunk));
    const fc = fresh.choices.find((c) => (c?.index ?? 0) === 0);
    fc.delta = { ...(fc.delta || {}), content: text };
    const nothing_else = !text && fresh.choices.length === 1 && !fc.finish_reason && !fresh.usage
      && Object.keys(fc.delta).every((k) => k === "content");
    if (nothing_else) return;
    transform.push(Buffer.from(rewrite_event(event_text, fresh)));
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
      if (scan) {
        // the stream ended without [DONE] or a finish_reason: release what was held back
        // (it was not a marker); an open block is dropped
        const tail = scan.finish();
        if (tail) this.push(text_event(template, tail));
      }
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
 * A pass-through for a non-streamed answer: read whole, choice 0's message text scanned.
 * An answer that was not changed goes out as the bytes that came in. One that does not
 * parse (cut short) is sent as it came, up to any opening marker it may hold.
 */
function create_json_filter({ scanner = null } = {}) {
  const scan = scanner || create_summary_scanner();
  const parts = [];
  return new Transform({
    transform(chunk, _enc, done) { parts.push(Buffer.from(chunk)); done(); },
    flush(done) {
      const raw_buf = Buffer.concat(parts);
      const raw = raw_buf.toString("utf8");
      let body = null;
      try { body = JSON.parse(raw); } catch { body = null; }
      const message = body?.choices?.find?.((c) => (c?.index ?? 0) === 0)?.message;
      if (!message) {
        if (body || !mentions_open(raw)) this.push(raw_buf);
        else {
          const at = Math.min(...[raw.indexOf("【"), raw.search(/\\u3010/i)].filter((i) => i >= 0));
          this.push(Buffer.from(raw.slice(0, at)));
        }
        return done();
      }
      let changed = false;
      if (typeof message.content === "string") {
        const shown = scan.push(message.content) + scan.finish();
        if (shown !== message.content) { message.content = shown; changed = true; }
      } else if (Array.isArray(message.content)) {
        const texts = message.content.filter((p) => p && typeof p === "object" && typeof p.text === "string");
        texts.forEach((p, i) => {
          const shown = scan.push(p.text) + (i === texts.length - 1 ? scan.finish() : "");
          if (shown !== p.text) { p.text = shown; changed = true; }
        });
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
