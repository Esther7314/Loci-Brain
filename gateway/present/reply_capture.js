// ============================================================
// gateway/present/reply_capture.js — read the model's answer as it streams past
//
// The relay pipes the upstream body to the client untouched; this module attaches a
// second listener to the same stream and reassembles what the model said, so the day
// store can write the reply **once the stream has finished**.
//
// What counts as the reply: choice 0's text, plus the **names** of the tools it called
// (arguments are never collected). Reasoning fields are not part of what was said.
// The provider's `usage` rides along when the answer carried one (the window's fill %
// is computed from its prompt_tokens); it is not part of what was said either.
// What counts as finished:
//   · a streamed answer (text/event-stream) that reached `data: [DONE]` or a
//     finish_reason
//   · a plain JSON answer that parsed and carried choices[0].message
// Anything else (the stream broke, the client went away, an error body) produces no
// reply: half an answer is not written down as if it had been said.
// ============================================================

const { StringDecoder } = require("string_decoder");

const MAX_BYTES = 8 * 1024 * 1024;   // past this the answer is not recorded; the client still gets all of it

function names_of(tool_calls) {
  if (!Array.isArray(tool_calls)) return [];
  return tool_calls.map((tc) => String(tc?.function?.name || tc?.name || "")).filter(Boolean);
}

function content_text(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content.map((p) => (typeof p === "string" ? p : (p && typeof p.text === "string" ? p.text : ""))).join("");
  }
  return "";
}

/** One SSE stream's worth of chat.completion.chunk events, reassembled. */
function create_sse_reader() {
  let buffer = "";
  let text = "";
  const tool_names = [];   // by tool-call index; a name may arrive in pieces
  let finished = false;
  let usage = null;

  function take_event(payload) {
    if (payload === "[DONE]") { finished = true; return; }
    let chunk;
    try { chunk = JSON.parse(payload); } catch { return; }
    if (chunk?.usage && typeof chunk.usage === "object") usage = chunk.usage;
    for (const choice of chunk?.choices || []) {
      if ((choice.index ?? 0) !== 0) continue;
      const delta = choice.delta || choice.message || {};
      if (typeof delta.content === "string") text += delta.content;
      for (const tc of delta.tool_calls || []) {
        const i = Number.isInteger(tc.index) ? tc.index : tool_names.length;
        const piece = tc?.function?.name;
        if (piece) tool_names[i] = (tool_names[i] || "") + piece;
      }
      if (choice.finish_reason) finished = true;
    }
  }

  return {
    push(str) {
      buffer += str;
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl).replace(/\r$/, "");
        buffer = buffer.slice(nl + 1);
        if (line.startsWith("data:")) take_event(line.slice(5).trim());
      }
    },
    end() {
      if (buffer.startsWith("data:")) take_event(buffer.slice(5).trim());
      buffer = "";
      return finished ? { text, tools: tool_names.filter(Boolean), usage } : null;
    },
  };
}

function parse_json_reply(raw) {
  let body;
  try { body = JSON.parse(raw); } catch { return null; }
  const message = body?.choices?.find?.((c) => (c.index ?? 0) === 0)?.message;
  if (!message) return null;
  return { text: content_text(message.content), tools: names_of(message.tool_calls || (message.function_call ? [message.function_call] : [])),
           usage: body.usage && typeof body.usage === "object" ? body.usage : null };
}

/**
 * @param stream     the Readable the relay pipes to the client
 * @param headers    response headers (content-type decides SSE vs JSON)
 * @param on_reply   called once with { text, tools, usage } when a finished answer was read
 */
function capture_reply(stream, headers, on_reply) {
  const is_sse = /text\/event-stream/i.test(String(headers?.["content-type"] || ""));
  const decoder = new StringDecoder("utf8");
  const sse = is_sse ? create_sse_reader() : null;
  let raw = "";
  let bytes = 0;
  let overflow = false;

  stream.on("data", (chunk) => {
    if (overflow) return;
    bytes += chunk.length;
    if (bytes > MAX_BYTES) { overflow = true; return; }
    const str = decoder.write(chunk);
    if (sse) sse.push(str); else raw += str;
  });
  stream.on("end", () => {
    if (overflow) return;
    const tail = decoder.end();
    let reply;
    if (sse) { sse.push(tail); reply = sse.end(); } else reply = parse_json_reply(raw + tail);
    if (reply) on_reply(reply);
  });
}

module.exports = { capture_reply, create_sse_reader, parse_json_reply, names_of, content_text };
