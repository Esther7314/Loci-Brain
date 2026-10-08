// ============================================================
// gateway/present/stream_filter.js — what the client gets back, and the usage it carries
//
// The fill % of a window is read from the provider's `usage.prompt_tokens`. A streamed
// answer only carries usage when the request asked for it
// (`stream_options.include_usage`), so the gateway asks on the client's behalf. When the
// client did not ask, the one chunk that exists only because of that — the usage chunk,
// `choices: []` plus `usage` — is taken out of what the client receives. Every other byte
// passes exactly as upstream sent it (a provider that also puts `"usage": null` on each
// chunk sends that to the client too: it is upstream's byte, and harmless).
// A provider that puts usage inside a chunk that also carries choices keeps that chunk:
// the choice is part of the answer.
//
// Later construction steps add to this file what else the client stream needs (the away
// prefix, holding back a summary block); for now it is the usage chunk and nothing more.
// ============================================================

const { Transform } = require("stream");

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

function is_usage_only(text) {
  const data = event_data(text);
  if (!data || data === "[DONE]") return false;
  let chunk;
  try { chunk = JSON.parse(data); } catch { return false; }
  if (!chunk || typeof chunk !== "object" || !chunk.usage || typeof chunk.usage !== "object") return false;
  return !Array.isArray(chunk.choices) || chunk.choices.length === 0;
}

/**
 * A pass-through for an SSE body that drops usage-only events. Works on bytes: an event
 * is everything up to and including the blank line that ends it, and a kept event is
 * written out as the exact bytes that came in.
 */
function create_usage_strip() {
  let pending = Buffer.alloc(0);
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
          const event = pending.subarray(start, i + 1);
          if (!is_usage_only(event.toString("utf8"))) this.push(event);
          start = i + 1;
        }
        line_start = i + 1;
      }
      pending = pending.subarray(start);
      done();
    },
    flush(done) {
      if (pending.length) this.push(pending);
      pending = Buffer.alloc(0);
      done();
    },
  });
}

module.exports = { with_usage, create_usage_strip, is_usage_only, event_data };
