// ============================================================
// gateway/present/context_window.js — how many tokens the model in use can take
//
// The fill % of a window needs a denominator, and nothing in a chat request says what it
// is. The answer is taken from the first of these that knows, and the source travels
// with the number so the panel can say where it came from:
//   user      the owner typed it (`compress.context_tokens` in present.json; absent or
//             null = not set)
//   learned   the model hit the wall once and the gateway learned its real limit from
//             that (the wall-hit escape, wall.js, calls learn())
//   provider  the provider's own model list (`GET /v1/models`) reports it
//   table     the built-in table of common models below, matched by name
//   default   1,000,000 — big on purpose: a window set too small makes the gateway pack
//             early and lose words for nothing; one set too big hits the wall, and the
//             wall has a way out (wall.js)
// The user's number wins over everything because the owner knows their provider; a
// learned limit wins over the provider's list because it was measured on this very
// route (proxies and plans cut windows below the model's paper size).
//
// The provider's list is fetched in the background, with the client's own credential
// headers (held for that one request, never stored), when a model shows up that the
// cached list does not know — at most once per LIST_COOLDOWN_MS, never blocking a chat.
// The list and the learned limits are kept in <LOCI_GATEWAY_DATA>/context_windows.json.
// ============================================================

const fs = require("fs");
const path = require("path");

const DEFAULT_TOKENS = 1_000_000;
const LIST_COOLDOWN_MS = 6 * 60 * 60 * 1000;
const LIST_TIMEOUT_MS = 5000;
const MIN_TOKENS = 1024;

// Numeric fields providers use for the window in a /v1/models entry. `max_tokens` is
// deliberately absent: more often than not it is the output cap.
const PROVIDER_FIELDS = [
  "context_length", "context_window", "max_context_length", "max_model_len",
  "context_size", "max_input_tokens", "input_token_limit", "inputTokenLimit",
];

// ⚠️ A best guess, not a source of truth: what these model families offered through
//    their own APIs as far as this table's author knew. Providers resell models with
//    smaller windows and vendors change them; the provider's list and the owner's own
//    number both win over this table. First match wins, so narrower patterns come first.
const TABLE = [
  // a size spelled in the name ("moonshot-v1-32k", "gpt-3.5-turbo-16k") is taken at its word
  { match: /(?:^|[-_/])(\d{1,4})k(?:$|[-_/.])/i, tokens: (m) => Number(m[1]) * 1024 },
  { match: /claude/i, tokens: 200_000 },
  { match: /gpt-4\.1/i, tokens: 1_047_576 },
  { match: /gpt-5/i, tokens: 400_000 },
  { match: /gpt-4o|gpt-4-turbo|gpt-4-\d{4}-preview|chatgpt-4o/i, tokens: 128_000 },
  { match: /(?:^|[-_/])o[134](?:$|[-_/])/i, tokens: 200_000 },
  { match: /gpt-3\.5-turbo/i, tokens: 16_385 },
  { match: /gpt-4(?:$|-0613|-0314)/i, tokens: 8_192 },
  { match: /deepseek/i, tokens: 128_000 },
  { match: /gemini-1\.5-pro/i, tokens: 2_097_152 },
  { match: /gemini/i, tokens: 1_048_576 },
  { match: /glm-4-long/i, tokens: 1_000_000 },
  { match: /glm-4\.6/i, tokens: 200_000 },
  { match: /glm/i, tokens: 128_000 },
  { match: /kimi-k2-(?:0905|turbo|thinking)/i, tokens: 262_144 },
  { match: /kimi|moonshot/i, tokens: 131_072 },
  { match: /qwen-long/i, tokens: 10_000_000 },
  { match: /qwen-turbo/i, tokens: 1_000_000 },
  { match: /qwen-max/i, tokens: 32_768 },
  { match: /qwen3-coder/i, tokens: 262_144 },
  { match: /qwen/i, tokens: 131_072 },
];

function table_tokens(model) {
  const name = String(model || "");
  if (!name) return null;
  for (const row of TABLE) {
    const m = row.match.exec(name);
    if (!m) continue;
    const tokens = typeof row.tokens === "function" ? row.tokens(m) : row.tokens;
    if (Number.isFinite(tokens) && tokens >= MIN_TOKENS) return tokens;
  }
  return null;
}

/** The window an entry of a /v1/models list reports, if any. */
function entry_tokens(entry) {
  if (!entry || typeof entry !== "object") return null;
  const places = [entry, entry.top_provider, entry.meta, entry.metadata, entry.capabilities];
  for (const place of places) {
    if (!place || typeof place !== "object") continue;
    for (const field of PROVIDER_FIELDS) {
      const n = Number(place[field]);
      if (Number.isInteger(n) && n >= MIN_TOKENS) return n;
    }
  }
  return null;
}

/** { model id → tokens } from a /v1/models reply ({data: [...]}, {models: [...]} or a bare array). */
function parse_models_list(body) {
  const list = Array.isArray(body) ? body : Array.isArray(body?.data) ? body.data
    : Array.isArray(body?.models) ? body.models : [];
  const out = {};
  for (const entry of list) {
    const id = String(entry?.id || entry?.name || "").replace(/^models\//, "");
    const tokens = entry_tokens(entry);
    if (id && tokens) out[id] = tokens;
  }
  return out;
}

const FORWARDED_CREDENTIALS = ["authorization", "x-api-key", "api-key"];

/**
 * @param data_root      <LOCI_GATEWAY_DATA>; the cache lives at context_windows.json in it
 * @param upstream       upstream base URL (…/v1), for the background model-list fetch
 * @param read_settings  () → present.json as an object (missing keys are fine)
 * @param clock          { now() }
 * @param log            where a failed fetch is reported
 */
function create_context_windows({ data_root, upstream = "", read_settings = () => ({}), clock, log = () => {} }) {
  const file = path.join(data_root, "context_windows.json");
  let state = null;
  let fetching = false;

  function load() {
    if (state) return state;
    state = { provider: {}, learned: {}, list_fetched_at: 0 };
    try {
      const parsed = JSON.parse(fs.readFileSync(file, "utf8"));
      if (parsed && typeof parsed === "object") {
        state.provider = parsed.provider && typeof parsed.provider === "object" ? parsed.provider : {};
        state.learned = parsed.learned && typeof parsed.learned === "object" ? parsed.learned : {};
        state.list_fetched_at = Number(parsed.list_fetched_at) || 0;
      }
    } catch { /* no cache yet, or an unreadable one: start empty, the next fetch rewrites it */ }
    return state;
  }

  function save() {
    fs.mkdirSync(data_root, { recursive: true });
    const tmp = `${file}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(state), "utf8");
    fs.renameSync(tmp, file);
  }

  function user_tokens() {
    let settings = {};
    try { settings = read_settings() || {}; } catch { settings = {}; }
    const n = Number(settings?.compress?.context_tokens);
    return Number.isInteger(n) && n >= MIN_TOKENS ? n : null;
  }

  /** @returns { tokens, source: "user" | "learned" | "provider" | "table" | "default" } */
  function resolve(model) {
    const s = load();
    const name = String(model || "");
    const user = user_tokens();
    if (user) return { tokens: user, source: "user" };
    const learned = Number(s.learned[name]?.tokens);
    if (name && Number.isInteger(learned) && learned >= MIN_TOKENS) return { tokens: learned, source: "learned" };
    const provider = Number(s.provider[name]);
    if (name && Number.isInteger(provider) && provider >= MIN_TOKENS) return { tokens: provider, source: "provider" };
    const table = table_tokens(name);
    if (table) return { tokens: table, source: "table" };
    return { tokens: DEFAULT_TOKENS, source: "default" };
  }

  /** Take in a provider's model list (from the background fetch, or any other place that saw one). */
  function note_models_list(body) {
    const s = load();
    const found = parse_models_list(body);
    s.provider = { ...s.provider, ...found };
    s.list_fetched_at = clock.now();
    save();
    return Object.keys(found).length;
  }

  /**
   * The hook for the wall-hit escape (wall.js): the limit read out of a
   * context-length error, or else the last prompt_tokens that still went through.
   */
  function learn(model, tokens, how) {
    const name = String(model || "");
    const n = Math.floor(Number(tokens));
    if (!name || !Number.isFinite(n) || n < MIN_TOKENS) return false;
    const s = load();
    s.learned[name] = { tokens: n, how: String(how || ""), at: clock.now() };
    save();
    return true;
  }

  /**
   * Called as a chat request passes: when the provider's list does not know this model
   * and has not been asked lately, ask it in the background. Never awaited by the chat.
   */
  function ensure(model, headers = {}) {
    const s = load();
    const name = String(model || "");
    if (!name || !upstream || fetching) return false;
    if (s.provider[name]) return false;
    if (s.list_fetched_at && clock.now() - s.list_fetched_at < LIST_COOLDOWN_MS) return false;
    const auth = {};
    for (const key of FORWARDED_CREDENTIALS) if (headers[key]) auth[key] = headers[key];
    fetching = true;
    s.list_fetched_at = clock.now();   // a failed fetch also waits out the cooldown: no knocking every turn
    const url = `${upstream.replace(/\/v1$/, "")}/v1/models`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), LIST_TIMEOUT_MS);
    fetch(url, { method: "GET", headers: { Accept: "application/json", ...auth }, signal: controller.signal })
      .then(async (resp) => {
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        note_models_list(await resp.json());
      })
      .catch((err) => {
        log(`[gateway] present: the provider's model list could not be read (${err?.message || err}); the window size falls back to the table`);
        try { save(); } catch { /* the cooldown still holds in memory */ }
      })
      .finally(() => { clearTimeout(timer); fetching = false; });
    return true;
  }

  return { resolve, ensure, note_models_list, learn, file };
}

module.exports = { create_context_windows, table_tokens, parse_models_list, entry_tokens, DEFAULT_TOKENS, TABLE };
