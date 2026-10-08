// ============================================================
// gateway/present/own_turn.js — the one path for every turn the gateway pays for itself
//
// Wake, the daily report and forced packing all go upstream through run(). Nothing here
// runs on its own: the callers (wake.js, day_close.js, pack.js, driven by the one heartbeat)
// decide when, this file decides how.
//
// Which key (the owner's ruling, blueprint §七.1):
//   · LOCI_UPSTREAM_KEY set → that key, as `Authorization: Bearer <key>`. Works across
//     restarts.
//   · otherwise the credential headers of the owner's latest chat request that upstream
//     accepted (relay.js calls remember_owner after a 2xx): `authorization`, `x-api-key`,
//     `api-key`, whichever she sent. A request that carried none still counts — her
//     upstream takes no key — and own turns then go out without one too.
//   · held in this process's memory only. Never written, never logged, never returned by
//     status(); an upstream error body that echoes it is redacted before it is logged.
//     After a restart there is no key until she has spoken once: run() answers
//     { outcome: "unpaid", reason: "no_key" } without sending anything. blocked() says
//     the same beforehand (no_key, or no_model when no model can be named), so a caller
//     on the beat can wait without preparing a turn that cannot go.
//
// Which model: the caller's `models`, else present.json `own.models`, else the model of
// her latest accepted request. The list is a fallback chain: when upstream refuses the
// model (model_refused), the same round is sent again with the next one, and the run
// stays on whichever model answered. What counts as "refused the model":
//   · 404 — the model (or the route for it) does not exist at this provider
//   · 400 / 403 / 422 whose error body names a model ("model_not_found", "does not have
//     access to model …", "model … does not support tools", or the model's own name) —
//     unless it is about the context being too long, which another model of the chain
//     does not cure and which has its own way out (pack.js and day_close.js drop the oldest
//     lines and ask again)
//   · not 401 (the key is wrong for every model), not 429 (rate or quota: the next model
//     on the same key mostly hits the same wall, and backoff is the heartbeat's job),
//     not 5xx (the provider is unwell, not the model)
//
// Tools (§七.7 — Loci's tools only, at most own.tool_rounds rounds):
//   · the caller says what to offer: `tools` as an array goes out as given (wake passes
//     the last upstream request's tools so the prefix stays byte-identical for the
//     cache); `tools: "loci"` builds the definitions from what Loci's MCP lists (the
//     daily report, a fresh window); omitted → no tools.
//   · a call is executed only when its name is one Loci's MCP lists (tools/list, asked
//     once per run, on the first call) — exactly, or as a client's prefixed form of it:
//     the name ends in a separator (`__ _ . - : /`) followed by exactly one Loci tool
//     name (`loci__recall`, `mcp_loci_recall` → `recall`); an exact name wins, and a
//     name two Loci tools could end it is not Loci's. Loci is called with the bare name,
//     through auto_attach.js's MCP client with
//     `x-loci-hook-token` (LOCI_HOOK_TOKEN) and `Loci-Turn: <turn>#<n>` — the turn is the
//     caller's `turn` or this run's id, n counts this run's Loci calls from 1. Loci keys
//     its writes by it (src/core/scope.py parse_turn), so a resent call is not written
//     twice; reads ignore it.
//   · any other tool gets a tool result saying it is not available now (NOT_HERE).
//   · a round = one upstream answer whose tool calls were executed. After the last
//     allowed round upstream is asked once more for its words; if that answer calls
//     tools again, they are not executed: the run stops there, `cut: true`, with what was
//     said.
//
// Stopping:
//   · abort(reason) cancels at once (the upstream request, a Loci call in flight).
//     Reason "owner_arrived" is her request arriving: outcome "aborted", not a failure,
//     and the caller writes nothing from it. A Loci tool call that already completed
//     cannot be taken back.
//   · her arrival stops only the kinds in ARRIVAL_ABORTS (wake: it speaks to her unasked,
//     and she is here now). A pack is not stopped by it — her turn waits for the pack
//     instead, so it goes with the new window (pack.js) — and neither is the report.
//     abort("owner_arrived") on any other kind is a no-op answering false; every other
//     reason (the alarm, a shutdown) stops whatever runs.
//   · the alarm (10 minutes) aborts too, and counts as a paid failure.
//   · one run at a time: a second caller gets { outcome: "busy" } at once.
//
// Money (§四): a failure is "paid" once any request of the run reached upstream — any
// HTTP reply, or a stream that broke — and "unpaid" when nothing did: no key, no model,
// cannot connect. Connection errors are unpaid only when their code says the request
// never left (refused, no such host, unreachable, connect timeout, TLS); any other
// transport error might have been read upstream, so it counts as paid.
//
// Bookkeeping: <LOCI_GATEWAY_DATA>/logs/present.jsonl gets a "started" line before the
// first request and a "result" line after; a run cut off by a restart is the "started"
// with no result. No-key and busy are not logged here: they send nothing, and the
// caller's gate keeps its own deduplicated line. Lines carry numbers, names and errors,
// never what was said.
//
// Silence: an answer that is empty, 【无话】 or "No response requested." — as the whole
// reply, never as part of one — comes back `silent: true`. This file only recognises
// them; nothing here (or in any prompt) teaches them.
//
// The summary block (stream_filter.js): every answer goes through the same scanner as the
// client's stream before its words are kept, so `text` and `said` are only ever what he
// said outside a 【窗口摘要】…【/窗口摘要】 block, and silence is judged on those words. The
// block comes back apart as `summary`: a wake folds the window with it (wake.js), a pack
// prefers it to the bare words (pack.js), a report ignores it. The request sent on after
// a tool round keeps the answer as he wrote it: it goes back to him, nowhere else.
// ============================================================

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { StringDecoder } = require("string_decoder");
const { _internal: { make_client } } = require("../auto_attach.js");
const { local_stamp } = require("./clock.js");
const { create_summary_scanner } = require("./stream_filter.js");

const DEFAULT_ALARM_MS = 10 * 60 * 1000;
const DEFAULT_TOOL_ROUNDS = 6;
const MCP_CALL_TIMEOUT_MS = 120 * 1000;
const CREDENTIAL_HEADERS = ["authorization", "x-api-key", "api-key"];
const ERROR_KEEP = 2000;

// What the model reads back as a tool result.
const NOT_HERE = "ta 不在，这个现在用不了。";
const LOCI_DOWN = "现在连不上 Loci，这个工具这一轮用不了。";
const BAD_ARGS = "参数不是一个 JSON 对象，没有执行。";
const loci_failed = (why) => `Loci 这次没办成：${why}`;

// Transport errors that mean the request never left this machine.
const UNSENT_CODES = new Set([
  "ECONNREFUSED", "ENOTFOUND", "EAI_AGAIN", "EHOSTUNREACH", "ENETUNREACH", "EADDRNOTAVAIL",
  "UND_ERR_CONNECT_TIMEOUT", "ERR_INVALID_URL",
  "CERT_HAS_EXPIRED", "DEPTH_ZERO_SELF_SIGNED_CERT", "SELF_SIGNED_CERT_IN_CHAIN",
  "UNABLE_TO_VERIFY_LEAF_SIGNATURE", "ERR_TLS_CERT_ALTNAME_INVALID",
]);

const TURN_ID = /^[^\s#]{1,200}$/;

// The run kinds her arriving request stops (see the header, Stopping).
const ARRIVAL_ABORTS = new Set(["wake"]);

function is_silence(text) {
  const t = String(text ?? "").trim();
  return t === "" || t === "【无话】" || /^no response requested\.?$/i.test(t);
}

// Clients present MCP tools under their own prefix (`loci__recall`, `mcp_loci_recall`).
const NAME_SEPARATORS = ["__", "_", ".", "-", ":", "/"];

/** The bare Loci tool name a called name stands for, or null (not Loci's, or ambiguous). */
function loci_name(called, listed) {
  if (listed.includes(called)) return called;
  const hits = listed.filter((t) => t && called.length > t.length && called.endsWith(t)
    && NAME_SEPARATORS.some((s) => called.slice(0, -t.length).endsWith(s)));
  return hits.length === 1 ? hits[0] : null;
}

function model_refused(status, body_text, model) {
  if (status === 404) return true;
  if (![400, 403, 422].includes(status)) return false;
  const t = String(body_text || "").toLowerCase();
  if (/context|too long|too many tokens|maximum.{0,40}tokens|token limit/.test(t)) return false;
  return /model/.test(t) || Boolean(model && t.includes(String(model).toLowerCase()));
}

function error_code(err) {
  return err?.cause?.code || err?.code || (err?.name === "TypeError" && /url/i.test(String(err?.message)) ? "ERR_INVALID_URL" : null);
}

function content_text(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map((p) => (typeof p === "string" ? p : (typeof p?.text === "string" ? p.text : ""))).join("");
  return "";
}

/** One streamed answer, reassembled: text, whole tool calls (id, name, arguments), usage. */
function create_answer_reader() {
  let buffer = "";
  let text = "";
  const calls = [];
  let finished = false;
  let usage = null;
  let error = null;

  function take(payload) {
    if (payload === "[DONE]") { finished = true; return; }
    let chunk;
    try { chunk = JSON.parse(payload); } catch { return; }
    if (chunk?.error) { error = typeof chunk.error === "string" ? chunk.error : (chunk.error.message || JSON.stringify(chunk.error)); return; }
    if (chunk?.usage && typeof chunk.usage === "object") usage = chunk.usage;
    for (const choice of chunk?.choices || []) {
      if ((choice.index ?? 0) !== 0) continue;
      const delta = choice.delta || choice.message || {};
      text += content_text(delta.content);
      for (const tc of delta.tool_calls || []) {
        const i = Number.isInteger(tc.index) ? tc.index : calls.length;
        const c = calls[i] || (calls[i] = { id: "", name: "", arguments: "" });
        if (tc.id) c.id = tc.id;
        if (tc.function?.name) c.name += tc.function.name;
        if (typeof tc.function?.arguments === "string") c.arguments += tc.function.arguments;
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
        if (line.startsWith("data:")) take(line.slice(5).trim());
      }
    },
    end() {
      if (buffer.startsWith("data:")) take(buffer.slice(5).trim());
      buffer = "";
      return { finished, error, text, calls: calls.filter((c) => c && c.name), usage };
    },
  };
}

function parse_json_answer(raw) {
  let body;
  try { body = JSON.parse(raw); } catch { return null; }
  const message = body?.choices?.find?.((c) => (c.index ?? 0) === 0)?.message;
  if (!message) return null;
  const calls = (message.tool_calls || []).map((tc) => ({
    id: String(tc.id || ""), name: String(tc.function?.name || ""),
    arguments: typeof tc.function?.arguments === "string" ? tc.function.arguments : JSON.stringify(tc.function?.arguments ?? {}),
  })).filter((c) => c.name);
  return { text: content_text(message.content), calls, usage: body.usage && typeof body.usage === "object" ? body.usage : null };
}

/**
 * One answer's words apart from its summary block (see the header):
 * { visible, summary: { text, closed } | null }.
 */
function split_answer(text) {
  const s = create_summary_scanner();
  const visible = s.push(text) + s.finish();
  const { summary, half } = s.result();
  if (summary) return { visible, summary: { text: summary, closed: true } };
  const half_text = half ? s.half_text() : "";
  return { visible, summary: half_text ? { text: half_text, closed: false } : null };
}

function add_usage(sum, u) {
  if (!u) return;
  for (const k of ["prompt_tokens", "completion_tokens", "total_tokens"]) {
    if (Number.isFinite(u[k])) sum[k] = (sum[k] || 0) + u[k];
  }
  if (Number.isFinite(u.prompt_tokens)) sum.last_prompt_tokens = u.prompt_tokens;
}

/**
 * @param env           LOCI_UPSTREAM_KEY (fixed key), LOCI_HOOK_TOKEN (Loci's door)
 * @param data_root     LOCI_GATEWAY_DATA; the log goes to <data_root>/logs/present.jsonl
 * @param upstream      upstream base URL; requests go where relay.js sends chat requests
 * @param loci_address  Loci's MCP endpoint (LOCI_MCP)
 * @param read_own      () → present.json `own` ({ tool_rounds, models })
 * @param mcp           an MCP client ({ list_tools, call_tool }); built from loci_address when omitted
 * @param alarm_ms      the overall alarm
 */
function create_own_turn({
  env = process.env, data_root, upstream, loci_address, read_own = () => ({}),
  clock = { now: () => Date.now() }, zone = undefined, log = console.error,
  mcp = null, alarm_ms = DEFAULT_ALARM_MS,
}) {
  const log_file = path.join(data_root, "logs", "present.jsonl");
  const target = String(upstream || "").replace(/\/+$/, "").replace(/\/v1$/, "") + "/v1/chat/completions";
  const hook_token = String(env.LOCI_HOOK_TOKEN || "").trim();
  const loci = mcp || make_client({ address: loci_address, timeout_ms: MCP_CALL_TIMEOUT_MS,
                                    headers: hook_token ? { "x-loci-hook-token": hook_token } : {} });

  // her latest accepted request: credential headers and model, in memory only
  let borrowed = null;
  let current = null;

  function remember_owner({ headers = {}, model = null } = {}) {
    const creds = {};
    for (const name of CREDENTIAL_HEADERS) {
      const v = headers[name];
      if (typeof v === "string" && v.trim()) creds[name] = v;
    }
    borrowed = { headers: creds, model: typeof model === "string" && model.trim() ? model.trim() : (borrowed?.model || null), at: clock.now() };
  }

  function fixed_key() { return String(env.LOCI_UPSTREAM_KEY || "").trim(); }

  function credential() {
    const fixed = fixed_key();
    if (fixed) return { source: "fixed", headers: { authorization: `Bearer ${fixed}` } };
    if (borrowed) return { source: "borrowed", headers: { ...borrowed.headers } };
    return null;
  }

  /** The model chain a run would try: the caller's, else own.models, else her latest model. */
  function chain_of(models) {
    const own = read_own() || {};
    const asked = Array.isArray(models) && models.length ? models
      : Array.isArray(own.models) && own.models.length ? own.models
      : [borrowed?.model];
    return [...new Set(asked.map((m) => String(m || "").trim()).filter(Boolean))];
  }

  /** Why run() would refuse before sending anything ("no_key" · "no_model"), or null. */
  function blocked(models = null) {
    if (!credential()) return "no_key";
    return chain_of(models).length ? null : "no_model";
  }

  /** Every secret this process holds, as it could appear in text. */
  function secrets() {
    const out = new Set();
    const add = (v) => { const s = String(v || "").trim(); if (s.length >= 4) out.add(s); };
    add(fixed_key());
    for (const v of Object.values(borrowed?.headers || {})) {
      add(v);
      const m = /^\s*\w+\s+(.+)$/.exec(v);
      if (m) add(m[1]);
    }
    return [...out].sort((a, b) => b.length - a.length);
  }
  function redact(text) {
    let s = String(text ?? "");
    for (const secret of secrets()) s = s.split(secret).join("••••");
    return s.length > ERROR_KEEP ? `${s.slice(0, ERROR_KEEP)}…` : s;
  }

  function write_log(entry) {
    try {
      fs.mkdirSync(path.dirname(log_file), { recursive: true });
      const at = zone ? local_stamp(clock.now(), zone).iso : new Date(clock.now()).toISOString();
      fs.appendFileSync(log_file, `${JSON.stringify({ at, event: "own_turn", ...entry })}\n`, "utf8");
    } catch (err) { log(`[gateway] present: own turn log not written: ${err?.message || err}`); }
  }

  function new_run_id(kind) {
    const stamp = new Date(clock.now()).toISOString().replace(/[-:]/g, "").replace(/\..*$/, "");
    return `own-${String(kind).replace(/[^\w-]/g, "") || "turn"}-${stamp}-${crypto.randomBytes(2).toString("hex")}`;
  }

  /** What the panel and health may see: no key, ever. */
  function status() {
    const cred = credential();
    return {
      busy: Boolean(current),
      running: current ? { run: current.run, kind: current.kind } : null,
      key: cred ? cred.source : null,
      borrowed_at: borrowed ? borrowed.at : null,
      owner_model: borrowed ? borrowed.model : null,
    };
  }

  function abort(reason = "owner_arrived") {
    if (!current) return false;
    if (reason === "owner_arrived" && !ARRIVAL_ABORTS.has(current.kind)) return false;
    if (!current.abort_reason) {
      current.abort_reason = String(reason || "aborted");
      current.controller.abort();
    }
    return true;
  }

  // ———— One upstream request ————

  async function ask_upstream({ cred, model, messages, tool_defs, extra, signal, run }) {
    const body = { ...extra, model, messages, stream: true,
                   stream_options: { ...(extra.stream_options || {}), include_usage: true } };
    if (tool_defs && tool_defs.length) body.tools = tool_defs;
    else { delete body.tools; delete body.tool_choice; }
    let resp;
    try {
      resp = await fetch(target, {
        method: "POST",
        headers: { ...cred.headers, "content-type": "application/json", accept: "text/event-stream, application/json" },
        body: JSON.stringify(body), signal,
      });
    } catch (err) {
      if (signal.aborted) return { kind: "aborted" };
      const code = error_code(err);
      const why = `${err?.message || err}${code ? ` (${code})` : ""}`;
      return UNSENT_CODES.has(code) ? { kind: "unsent", error: why } : { kind: "lost", error: why };
    }
    run.reached = true;
    if (!resp.ok) {
      let text = "";
      try { text = await resp.text(); } catch { if (signal.aborted) return { kind: "aborted" }; }
      if (model_refused(resp.status, text, model)) return { kind: "refused_model", status: resp.status, error: text };
      return { kind: "http", status: resp.status, error: text };
    }
    const is_sse = /text\/event-stream/i.test(resp.headers.get("content-type") || "");
    const decoder = new StringDecoder("utf8");
    const reader = is_sse ? create_answer_reader() : null;
    let raw = "";
    try {
      if (resp.body) {
        const r = resp.body.getReader();
        for (;;) {
          const { done, value } = await r.read();
          if (done) break;
          const str = decoder.write(Buffer.from(value));
          if (reader) reader.push(str); else raw += str;
        }
      }
    } catch (err) {
      if (signal.aborted) return { kind: "aborted" };
      return { kind: "broke", error: String(err?.message || err) };
    }
    if (signal.aborted) return { kind: "aborted" };
    const tail = decoder.end();
    if (reader) {
      reader.push(tail);
      const got = reader.end();
      if (got.error) return { kind: "broke", error: got.error };
      if (!got.finished) return { kind: "broke", error: "the stream ended before the answer finished" };
      return { kind: "ok", ...got };
    }
    const got = parse_json_answer(raw + tail);
    if (!got) return { kind: "bad_reply", error: (raw + tail).slice(0, ERROR_KEEP) };
    return { kind: "ok", ...got };
  }

  // ———— Loci's tools ————

  async function loci_tools(run, signal) {
    if (!run.loci_list) {
      run.loci_list = loci.list_tools({ signal }).then(
        (tools) => ({ ok: true, tools: tools.filter((t) => t && typeof t.name === "string") }),
        (err) => ({ ok: false, error: String(err?.message || err) }));
    }
    return run.loci_list;
  }

  async function run_tool(call, run, signal) {
    const listed = await loci_tools(run, signal);
    if (signal.aborted) return null;
    if (!listed.ok) { run.tools_refused.push(call.name); return LOCI_DOWN; }
    const bare = loci_name(call.name, listed.tools.map((t) => t.name));
    if (!bare) { run.tools_refused.push(call.name); return NOT_HERE; }
    let args;
    try { args = call.arguments.trim() ? JSON.parse(call.arguments) : {}; } catch { args = null; }
    if (!args || typeof args !== "object" || Array.isArray(args)) { run.tools_refused.push(call.name); return BAD_ARGS; }
    run.ordinal += 1;
    run.tools_used.push(bare);
    try {
      const text = await loci.call_tool(bare, args, { headers: { "Loci-Turn": `${run.turn}#${run.ordinal}` }, signal });
      return text;
    } catch (err) {
      if (signal.aborted) return null;
      return loci_failed(String(err?.message || err));
    }
  }

  // ———— The run ————

  /**
   * @param kind      "wake" | "report" | "pack" — written in the log, part of the run id
   * @param messages  the whole conversation to send (OpenAI chat format)
   * @param tools     an array of tool definitions to offer as given, "loci" for Loci's
   *                  own list, or null for none
   * @param models    the model chain for this run; defaults as the header says
   * @param turn      the Loci-Turn id for this run's writes; defaults to the run id
   * @param extra     other request fields to send as they are (temperature, …)
   * @returns see result() below
   */
  async function run({ kind = "turn", messages, tools = null, models = null, turn = null, extra = {} } = {}) {
    if (current) return result({ kind, outcome: "busy", reason: "busy" });
    if (!Array.isArray(messages) || messages.length === 0) throw new TypeError("own turn: messages must be a non-empty array");
    if (turn !== null && !TURN_ID.test(String(turn))) throw new TypeError("own turn: turn must be 1-200 characters with no whitespace or #");

    const cred = credential();
    if (!cred) return result({ kind, outcome: "unpaid", reason: "no_key" });
    const own = read_own() || {};
    const chain = chain_of(models);
    if (!chain.length) return result({ kind, outcome: "unpaid", reason: "no_model" });
    const max_rounds = Number.isInteger(own.tool_rounds) && own.tool_rounds > 0 ? own.tool_rounds : DEFAULT_TOOL_ROUNDS;

    const run_id = new_run_id(kind);
    const controller = new AbortController();
    const signal = controller.signal;
    current = { run: run_id, kind, controller, abort_reason: null };
    const me = current;
    const state = {
      run: run_id, turn: turn ? String(turn) : run_id, ordinal: 0, reached: false,
      tools_used: [], tools_refused: [], loci_list: null,
    };
    const started = clock.now();
    const alarm = setTimeout(() => abort("alarm"), alarm_ms);
    if (alarm.unref) alarm.unref();

    write_log({ phase: "started", run: run_id, kind, key: cred.source, models: chain, tool_rounds: max_rounds });

    const convo = messages.slice();
    const usage = {};
    const said = [];
    let summary = null;   // the run's newest summary block: { text, closed }
    let model_i = 0;
    let rounds = 0;
    let calls = 0;
    let out;

    const failure = (reason, error) => ({ outcome: state.reached ? "paid" : "unpaid", reason, error: redact(error) });

    try {
      let tool_defs = null;
      if (tools === "loci") {
        const listed = await loci_tools(state, signal);
        if (listed.ok) {
          tool_defs = listed.tools.map((t) => ({ type: "function", function: {
            name: t.name, description: String(t.description || ""),
            parameters: t.inputSchema && typeof t.inputSchema === "object" ? t.inputSchema : { type: "object", properties: {} } } }));
        }
      } else if (Array.isArray(tools)) tool_defs = tools;

      for (;;) {
        if (signal.aborted) { out = {}; break; }
        calls += 1;
        const got = await ask_upstream({ cred, model: chain[model_i], messages: convo, tool_defs, extra, signal, run: state });
        if (got.kind === "aborted") { out = {}; break; }
        if (got.kind === "refused_model" && model_i + 1 < chain.length) {
          log(`[gateway] present: own turn ${run_id}: upstream refused model ${chain[model_i]} (HTTP ${got.status}); trying ${chain[model_i + 1]}`);
          model_i += 1;
          continue;
        }
        if (got.kind === "refused_model" || got.kind === "http") { out = failure(`http_${got.status}`, got.error); break; }
        if (got.kind === "unsent") { out = failure("connect", got.error); break; }
        if (got.kind === "lost") { out = failure("connection_lost", got.error); break; }
        if (got.kind === "broke") { out = failure("stream_broke", got.error); break; }
        if (got.kind === "bad_reply") { out = failure("bad_reply", got.error); break; }

        add_usage(usage, got.usage);
        const split = split_answer(got.text);
        // a newer block replaces an older one, except a half one a closed one
        if (split.summary && (split.summary.closed || !summary?.closed)) summary = split.summary;
        if (split.visible) said.push(split.visible);
        if (!got.calls.length) { out = { outcome: "ok", reason: null, text: split.visible, cut: false }; break; }
        if (rounds >= max_rounds) {
          out = { outcome: "ok", reason: null, text: split.visible, cut: true, cut_tools: got.calls.map((c) => c.name) };
          break;
        }

        const tool_calls = got.calls.map((c, i) => ({ id: c.id || `call_${rounds + 1}_${i + 1}`, type: "function",
                                                      function: { name: c.name, arguments: c.arguments } }));
        convo.push({ role: "assistant", content: got.text || null, tool_calls });
        for (const tc of tool_calls) {
          const content = await run_tool({ name: tc.function.name, arguments: tc.function.arguments }, state, signal);
          if (signal.aborted) break;
          convo.push({ role: "tool", tool_call_id: tc.id, content: String(content ?? "") });
        }
        if (signal.aborted) { out = {}; break; }
        rounds += 1;
      }
    } catch (err) {
      out = signal.aborted ? {} : failure("internal", String(err?.stack || err));
    } finally {
      clearTimeout(alarm);
    }

    if (signal.aborted) {
      const reason = me.abort_reason || "aborted";
      // the alarm is a hung run: money was spent on it; anything else stopped on purpose
      out = reason === "alarm"
        ? { outcome: "paid", reason: "alarm", error: `no answer within ${Math.round(alarm_ms / 1000)} s` }
        : { outcome: "aborted", reason };
    }
    current = null;

    const done = result({
      kind, run: run_id, key: cred.source, model: chain[model_i], rounds, calls,
      tools_used: state.tools_used, tools_refused: state.tools_refused,
      said: said.join("\n\n"), usage, reached_upstream: state.reached, ms: clock.now() - started, ...out,
      summary: out.outcome === "ok" ? summary : null,
    });
    write_log({
      phase: "result", run: run_id, kind, outcome: done.outcome, reason: done.reason, model: done.model,
      rounds, calls, tools_used: done.tools_used, tools_refused: done.tools_refused, cut: done.cut,
      silent: done.outcome === "ok" ? done.silent : null, usage: done.usage, reached_upstream: done.reached_upstream,
      ms: done.ms, error: done.error,
    });
    return done;
  }

  /**
   * The shape every run() returns:
   *   ok              outcome === "ok"
   *   outcome         "ok" | "paid" | "unpaid" | "aborted" | "busy"
   *   reason          null when ok; no_key · no_model · busy · owner_arrived (or the abort
   *                   reason given) · alarm · connect · connection_lost · http_<status> ·
   *                   stream_broke · bad_reply · internal
   *   counted         a paid failure: what the caller counts and backs off on
   *   text            the last answer's words (on a cut, the words of the answer whose
   *                   tool calls were not run) — what a report or a wake keeps; never a
   *                   summary block (see the header)
   *   said            every answer's words in this run, in order, the same way
   *   silent          text is empty, 【无话】 or "No response requested."
   *   summary         the run's newest summary block, { text, closed }, or null.
   *                   closed: false = it never closed; only a pack may use its text
   *   cut             stopped at the tool-round limit
   *   tools_used      Loci tools executed, in order · tools_refused  names answered "not here"
   *   usage           summed prompt / completion / total tokens, plus last_prompt_tokens
   */
  function result(r) {
    const text = String(r.text ?? "");
    return {
      ok: r.outcome === "ok",
      outcome: r.outcome,
      reason: r.reason ?? null,
      counted: r.outcome === "paid",
      run: r.run ?? null,
      kind: r.kind ?? null,
      key: r.key ?? null,
      model: r.model ?? null,
      text,
      said: String(r.said ?? ""),
      silent: is_silence(text),
      summary: r.summary && typeof r.summary.text === "string" && r.summary.text
        ? { text: r.summary.text, closed: Boolean(r.summary.closed) } : null,
      cut: Boolean(r.cut),
      cut_tools: r.cut_tools || [],
      rounds: r.rounds ?? 0,
      calls: r.calls ?? 0,
      tools_used: r.tools_used || [],
      tools_refused: r.tools_refused || [],
      usage: r.usage || {},
      reached_upstream: Boolean(r.reached_upstream),
      error: r.error ?? null,
      ms: r.ms ?? 0,
    };
  }

  return { run, abort, remember_owner, status, blocked, busy: () => Boolean(current) };
}

module.exports = {
  create_own_turn, is_silence, model_refused, loci_name,
  NOT_HERE, LOCI_DOWN, DEFAULT_ALARM_MS, CREDENTIAL_HEADERS, ARRIVAL_ABORTS,
};
