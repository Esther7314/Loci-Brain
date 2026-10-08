// ============================================================
// gateway/present/fill.js — how full the window is
//
// The fill is the input of the last turn that went through, over the model's window:
//   used   `usage.prompt_tokens` from the provider when its answer carried usage;
//          otherwise an estimate from the characters that were sent, marked
//          `estimated: true` (the panel says 「估的」)
//   window present/context_window.js, with where the number came from
// It is measured on the last turn, so the next turn still adds the owner's line, the reply
// and any tool round trips on top — the lines that act on it (compress.js, pack.js)
// leave room for that.
// ============================================================

const CJK = /[⺀-鿿가-힯豈-﫿＀-￯]/g;

function text_of(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map((p) => (typeof p === "string" ? p : typeof p?.text === "string" ? p.text : "")).join("\n");
  return "";
}

/** Rough token count of a string: one per CJK character, one per four of anything else. */
function estimate_text(text) {
  const s = String(text || "");
  const cjk = (s.match(CJK) || []).length;
  return cjk + Math.ceil((s.length - cjk) / 4);
}

/** Rough prompt size of a request body: every message's text and tool calls, plus the tool definitions. */
function estimate_prompt(body) {
  let total = 0;
  for (const m of body?.messages || []) {
    total += 4 + estimate_text(text_of(m?.content));
    if (Array.isArray(m?.tool_calls)) total += estimate_text(JSON.stringify(m.tool_calls));
  }
  if (Array.isArray(body?.tools) && body.tools.length) total += estimate_text(JSON.stringify(body.tools));
  return total;
}

function fill_pct(used, tokens) {
  return tokens > 0 ? Math.round((used * 1000) / tokens) / 10 : null;
}

/**
 * @param usage     the provider's usage object, or null
 * @param estimate  estimate_prompt() of what was sent, used when usage has no prompt_tokens
 * @param window    { tokens, source } from context_window.resolve()
 * @returns the record kept as window.usage
 */
function measure({ usage, estimate, window, model, at }) {
  const reported = Number(usage?.prompt_tokens);
  const has = Number.isFinite(reported) && reported >= 0;
  const used = has ? reported : Math.max(0, Math.round(Number(estimate) || 0));
  return {
    prompt_tokens: used,
    estimated: !has,
    context_tokens: window.tokens,
    context_source: window.source,
    fill_pct: fill_pct(used, window.tokens),
    model: model || null,
    at,
  };
}

module.exports = { estimate_text, estimate_prompt, fill_pct, measure };
