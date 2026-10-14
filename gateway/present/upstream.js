// ============================================================
// gateway/present/upstream.js — the upstream in force: where, with which key, a test, and
// the plain door Loci's side model goes through
//
// Where and which key live in settings.js (the `upstream` section of present.json, over
// the env LOCI_UPSTREAM and LOCI_UPSTREAM_KEY); this file reads them **per request**, so a
// change saved on the panel takes effect on the next request without a restart:
//   base()        the upstream URL in force ("" = none: no panel URL, no LOCI_UPSTREAM)
//   credential()  the headers that carry the key in force: a fixed key (panel, else env)
//                 as `Authorization: Bearer <key>`; else the credential headers of the
//                 client's latest accepted request (own_turn.js, memory only; {} when the
//                 client sent none); else null — no key yet
// A client's own chat request is not this file's: relay.js forwards it to base() with the
// key the client sent.
//
// test() — POST /present/upstream-test (present_api.js). It asks the upstream's
// GET <base>/v1/models with the key in force, the cheapest request that proves both the
// address and the key, and answers
//   { ok: true, status: 200, models: [ids…] }
//   { ok: false, status: <HTTP status>, error }      upstream answered, but not with a list
//   { ok: false, status: "no_upstream" | "no_key" | "error", error }   nothing was asked, or
//                                                    the request never got an answer
// within TEST_TIMEOUT_MS: Loci's proxy gives the gateway five seconds per answer
// (web/loci_present.py), and the test has to answer inside them. A list it reads is also
// handed to the window sizes (context_window.js), as the background fetch would.
//
// handle_side — /loci/upstream/*, mounted by server.js behind LOCI_GATEWAY_TOKEN (the key
// Loci already presents on /present/* and /loci/source). Loci's side model (tags, slices,
// summaries, dreams; Loci's dehydration api_format "gateway") calls the upstream in force
// through it with the key in force; only the model name is Loci's own. Two routes:
//   POST /loci/upstream/v1/chat/completions   →  POST <base>/v1/chat/completions
//   GET  /loci/upstream/v1/models             →  GET  <base>/v1/models
// A pass-through and nothing more: no present layer, no Loci tools, no window, no fill,
// no compress or wake bookkeeping, and the body goes upstream byte for byte. The headers
// that go upstream are content-type, accept and the key in force, nothing else (Loci's
// Bearer stays here). The side call is not the owner speaking: it neither lends its
// credential to the gateway's own turns nor stops a wake.
// What comes back:
//   2xx           upstream's body, piped as it came, with its content-type
//   other status  upstream's status and body, with every secret this gateway holds
//                 masked out of it
//   409           { error: { message, code: "no_upstream" | "no_key" } } — the gateway has
//                 nothing to send with; not a transient error, so Loci does not retry it
//   502           { error: { message, code: "upstream_unreachable" } }
//   404 · 415     another path, or a POST whose body is not JSON; nothing is sent
//
// 🔴 No key ever reaches an answer, a log line or an error: the view shows a masked
//    tail (settings.js), and upstream text passes redact() before it leaves.
// ============================================================

const { Readable } = require("stream");
const { send_json } = require("./http_io.js");
const { says_json } = require("../relay.js");

const SIDE_PREFIX = "/loci/upstream";
const SIDE_ROUTES = new Set(["POST /v1/chat/completions", "GET /v1/models"]);
const MAX_SIDE_BODY_BYTES = 16 * 1024 * 1024;
const TEST_TIMEOUT_MS = 4000;
const ERROR_KEEP = 2000;
const CREDENTIAL_HEADERS = ["authorization", "x-api-key", "api-key"];

const NO_KEY = "网关还没有 key：等 ta 先说一句，或者在网关上游里填一把固定的 key";
const NO_UPSTREAM = "网关还没有上游：在网关上游里填一个地址，或者启动时设 LOCI_UPSTREAM";

/** The key itself out of credential headers: a Bearer's token, else an api-key header; "" for none, null for no headers at all. */
function key_in(headers) {
  if (!headers) return null;
  const auth = String(headers.authorization || "").trim();
  if (auth) {
    const m = /^\w+\s+(.+)$/.exec(auth);
    return m ? m[1].trim() : auth;
  }
  return String(headers["x-api-key"] || headers["api-key"] || "").trim();
}

function models_of(body) {
  const list = Array.isArray(body?.data) ? body.data : Array.isArray(body?.models) ? body.models : null;
  if (!list) return null;
  return [...new Set(list.map((m) => String(m?.id || m?.name || "").replace(/^models\//, "")).filter(Boolean))].sort();
}

/** The message an OpenAI-style error body carries, else the body's own text, cut short. */
function message_of(text) {
  try {
    const e = JSON.parse(text)?.error;
    if (typeof e === "string" && e.trim()) return e.trim();
    if (e && typeof e.message === "string" && e.message.trim()) return e.message.trim();
  } catch { /* not JSON: the text itself */ }
  return String(text || "").trim().slice(0, 300);
}

function read_raw(req, limit) {
  return new Promise((resolve) => {
    const chunks = [];
    let size = 0;
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    req.on("data", (c) => {
      if (done) return;
      size += c.length;
      if (size > limit) { finish(null); req.resume(); return; }
      chunks.push(c);
    });
    req.on("end", () => finish(Buffer.concat(chunks)));
    req.on("error", () => finish(null));
  });
}

/**
 * @param env              LOCI_UPSTREAM_KEY: masked out of upstream text even while a panel key is in force
 * @param settings         settings.js instance (upstream_base, fixed_key)
 * @param own_turn         own_turn.js instance (client_credential: the client's remembered headers)
 * @param context_windows  context_window.js instance; a list the test reads goes to it (optional)
 * @param log              where a side call's line goes (one per call: method, path, status, time)
 * @param test_timeout_ms  how long test() waits for the upstream
 */
function create_upstream({ env = process.env, settings, own_turn, context_windows = null, log = console.log, test_timeout_ms = TEST_TIMEOUT_MS }) {
  function base() { return settings.upstream_base(); }

  function credential() {
    const fixed = settings.fixed_key();
    if (fixed) return { from: fixed.from, headers: { authorization: `Bearer ${fixed.key}` } };
    const client = own_turn.client_credential();
    return client ? { from: "client", headers: client } : null;
  }

  /** Every secret this gateway holds that could show up in upstream's text, longest first. */
  function secrets() {
    const out = new Set();
    const add = (v) => { const s = String(v || "").trim(); if (s.length >= 4) out.add(s); };
    const fixed = settings.fixed_key();
    if (fixed) add(fixed.key);
    add(String(env.LOCI_UPSTREAM_KEY || ""));
    for (const v of Object.values(own_turn.client_credential() || {})) { add(v); add(key_in({ authorization: v })); }
    return [...out].sort((a, b) => b.length - a.length);
  }
  function redact(text) {
    let s = String(text ?? "");
    for (const secret of secrets()) s = s.split(secret).join("••••");
    return s.length > ERROR_KEEP ? `${s.slice(0, ERROR_KEEP)}…` : s;
  }

  const target = (b, rest) => `${b.replace(/\/v1$/, "")}${rest}`;

  async function test() {
    const b = base();
    if (!b) return { ok: false, status: "no_upstream", error: NO_UPSTREAM };
    const cred = credential();
    if (!cred) return { ok: false, status: "no_key", error: NO_KEY };
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), test_timeout_ms);
    let resp;
    let text;
    try {
      resp = await fetch(target(b, "/v1/models"), {
        method: "GET", headers: { accept: "application/json", ...cred.headers }, signal: controller.signal,
      });
      text = await resp.text();
    } catch (err) {
      const why = controller.signal.aborted ? `超时：上游 ${test_timeout_ms / 1000} 秒内没回`
        : `连不上上游：${redact(err?.cause?.message || err?.message || err)}`;
      return { ok: false, status: "error", error: why };
    } finally { clearTimeout(timer); }
    if (resp.status === 401 || resp.status === 403) {
      return { ok: false, status: resp.status, error: `上游不认这把 key（HTTP ${resp.status}）：${redact(message_of(text))}` };
    }
    if (resp.status === 404) {
      return { ok: false, status: 404, error: "上游没有 /models 这个口（HTTP 404）：地址要写到 …/v1 那一层" };
    }
    if (!resp.ok) return { ok: false, status: resp.status, error: `上游回了 HTTP ${resp.status}：${redact(message_of(text))}` };
    let body = null;
    try { body = JSON.parse(text); } catch { body = null; }
    const models = models_of(body);
    if (!models) return { ok: false, status: resp.status, error: `上游回了 HTTP ${resp.status}，但读不出模型列表` };
    try { context_windows?.note_models_list(body); } catch { /* window sizes are a side benefit */ }
    return { ok: true, status: resp.status, models };
  }

  const side_error = (res, status, message, code) => send_json(res, status, { error: { message, code } });

  async function handle_side(req, res) {
    const started = Date.now();
    const [path_part, query = ""] = req.url.split(/\?(.*)/s);
    const rest = path_part.slice(SIDE_PREFIX.length);
    const line = (status, note = "") => log(`[gateway] side ${req.method} ${rest} → ${status}  ${Date.now() - started}ms${note ? `  ${note}` : ""}`);
    if (!SIDE_ROUTES.has(`${req.method} ${rest}`)) {
      req.resume();
      line(404);
      return side_error(res, 404, `副模型的口只有 POST ${SIDE_PREFIX}/v1/chat/completions 和 GET ${SIDE_PREFIX}/v1/models`, "not_here");
    }
    let raw = null;
    if (req.method === "POST") {
      if (!says_json(req)) {
        req.resume();
        line(415);
        return side_error(res, 415, "要带 Content-Type: application/json；这一条没往上游发", "not_json");
      }
      raw = await read_raw(req, MAX_SIDE_BODY_BYTES);
      if (raw === null) { line(413); return side_error(res, 413, `请求体超过 ${MAX_SIDE_BODY_BYTES} 字节，或者读不出来`, "too_big"); }
    } else req.resume();

    const b = base();
    if (!b) { line(409, "no upstream"); return side_error(res, 409, NO_UPSTREAM, "no_upstream"); }
    const cred = credential();
    if (!cred) { line(409, "no key"); return side_error(res, 409, NO_KEY, "no_key"); }

    const headers = { accept: String(req.headers.accept || "application/json") };
    if (raw) headers["content-type"] = String(req.headers["content-type"]);
    Object.assign(headers, cred.headers);
    // Loci gone (its own timeout) → the upstream request goes too
    const controller = new AbortController();
    res.on("close", () => { if (!res.writableEnded) controller.abort(); });
    let resp;
    try {
      resp = await fetch(target(b, rest) + (query ? `?${query}` : ""), {
        method: req.method, headers, body: raw || undefined, signal: controller.signal,
      });
    } catch (err) {
      if (controller.signal.aborted) { line("aborted"); return undefined; }
      line(502, `key from ${cred.from}`);
      return side_error(res, 502, `连不上上游：${redact(err?.cause?.message || err?.message || err)}`, "upstream_unreachable");
    }
    const type = resp.headers.get("content-type") || "application/json; charset=utf-8";
    if (!resp.ok) {
      let text = "";
      try { text = await resp.text(); } catch { /* an empty body says the status alone */ }
      const out = Buffer.from(redact(text), "utf8");
      res.writeHead(resp.status, { "Content-Type": type, "Content-Length": out.length, "Cache-Control": "no-store" });
      res.end(out);
      line(resp.status, `key from ${cred.from}`);
      return undefined;
    }
    // content-length goes with content-encoding: fetch hands the body decompressed (relay.js says why)
    res.writeHead(resp.status, { "Content-Type": type, "Cache-Control": "no-store" });
    if (!resp.body) { res.end(); line(resp.status, `key from ${cred.from}`); return undefined; }
    const stream = Readable.fromWeb(resp.body);
    stream.on("error", () => res.destroy());
    stream.on("end", () => line(resp.status, `key from ${cred.from}`));
    stream.pipe(res);
    return undefined;
  }

  return { base, credential, test, handle_side, redact };
}

module.exports = { create_upstream, key_in, SIDE_PREFIX, NO_KEY, NO_UPSTREAM, TEST_TIMEOUT_MS };
