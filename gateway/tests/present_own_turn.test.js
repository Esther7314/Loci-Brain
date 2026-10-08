// ============================================================
// gateway/tests/present_own_turn.test.js — the gateway's own paid turns (own_turn.js)
//
// In-process, behind the network fence: a scripted fake upstream (each request answered
// by a plan: text, tool calls, an HTTP error, a stream that stops halfway or hangs) and a
// fake Loci MCP that lists two tools and books every call with its headers. Temp
// directories only. The relay's credential hook is checked with the real relay and a
// stand-in present layer.
// ============================================================

const { test, before, after } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const fence = require("./network_fence.js");

const { create_own_turn, is_silence, model_refused, loci_name, NOT_HERE } = require("../present/own_turn.js");
const { create_relay } = require("../relay.js");

const KEY = "sk-own-turn-SENTINEL-7f3a9c";
const FIXED = "sk-fixed-SENTINEL-41d2";
const HOOK = "hook-SENTINEL-88aa";

const root = fs.mkdtempSync(path.join(os.tmpdir(), "loci-own-turn-"));
let n = 0;
const fresh_dir = () => { const d = path.join(root, `d${++n}`); fs.mkdirSync(d, { recursive: true }); return d; };

// ———— fake upstream: plan(body, i) → { text, calls: [[name, args]], status, error, half, hang, json, usage } ————
function start_upstream() {
  const got = [];
  let plan = () => ({ text: "嗯。" });
  const open = new Set();
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const body = JSON.parse(Buffer.concat(chunks).toString("utf8") || "null");
      got.push({ headers: { ...req.headers }, body });
      const p = plan(body, got.length) || {};
      if (p.hang) { open.add(res); return; }
      if (p.status && p.status !== 200) {
        res.writeHead(p.status, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ error: { message: p.error || "nope" } }));
      }
      const calls = p.calls || [];
      const usage = p.usage || { prompt_tokens: 100, completion_tokens: 10, total_tokens: 110 };
      if (p.json) {
        const message = { role: "assistant", content: p.text ?? "" };
        if (calls.length) message.tool_calls = calls.map(([name, args], i) => ({ id: `c${i}`, type: "function", function: { name, arguments: JSON.stringify(args || {}) } }));
        res.writeHead(200, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ choices: [{ index: 0, message, finish_reason: calls.length ? "tool_calls" : "stop" }], usage }));
      }
      const events = [{ choices: [{ index: 0, delta: { role: "assistant" } }] }];
      const text = String(p.text ?? "");
      for (let i = 0; i < text.length; i += 2) events.push({ choices: [{ index: 0, delta: { content: text.slice(i, i + 2) } }] });
      calls.forEach(([name, args], i) => {
        const a = JSON.stringify(args || {});
        // the name and the arguments arrive in pieces, as real providers send them
        events.push({ choices: [{ index: 0, delta: { tool_calls: [{ index: i, id: `call_${got.length}_${i}`, type: "function", function: { name, arguments: a.slice(0, 3) } }] } }] });
        events.push({ choices: [{ index: 0, delta: { tool_calls: [{ index: i, function: { arguments: a.slice(3) } }] } }] });
      });
      events.push({ choices: [{ index: 0, delta: {}, finish_reason: calls.length ? "tool_calls" : "stop" }] });
      const line = (e) => `data: ${JSON.stringify(e)}\n\n`;
      res.writeHead(200, { "Content-Type": "text/event-stream" });
      if (p.half) {
        // half the answer, then nothing: the run is cut off mid-stream
        res.write(events.slice(0, 2).map(line).join(""));
        open.add(res);
        if (p.half === "break") setTimeout(() => res.socket.destroy(), 20);
        return;
      }
      res.end(events.map(line).join("") + line({ choices: [], usage }) + "data: [DONE]\n\n");
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({
    port: server.address().port,
    url: `http://127.0.0.1:${server.address().port}/v1`,
    got,
    plan(fn) { plan = fn; got.length = 0; },
    async close() { for (const r of open) { try { r.destroy(); } catch { /* gone */ } } server.closeAllConnections?.(); await new Promise((r) => server.close(r)); },
  })));
}

// ———— fake Loci MCP: lists recall and grow, books every request's headers ————
function start_loci() {
  const calls = [];
  const lists = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const body = JSON.parse(Buffer.concat(chunks).toString("utf8") || "null");
      const sse = (obj, session) => {
        const h = { "Content-Type": "text/event-stream" };
        if (session) h["Mcp-Session-Id"] = session;
        res.writeHead(200, h);
        res.end(`event: message\ndata: ${JSON.stringify(obj)}\n\n`);
      };
      if (body?.method === "initialize") return sse({ jsonrpc: "2.0", id: body.id, result: { protocolVersion: "2024-11-05", capabilities: {} } }, "s1");
      if (body?.method === "notifications/initialized") { res.writeHead(202); return res.end(); }
      if (body?.method === "tools/list") {
        lists.push({ headers: { ...req.headers } });
        return sse({ jsonrpc: "2.0", id: body.id, result: { tools: [
          { name: "recall", description: "找回记忆", inputSchema: { type: "object", properties: { query: { type: "string" } } } },
          { name: "grow", description: "写下", inputSchema: { type: "object", properties: {} } },
        ] } });
      }
      if (body?.method === "tools/call") {
        calls.push({ name: body.params.name, args: body.params.arguments, headers: { ...req.headers } });
        return sse({ jsonrpc: "2.0", id: body.id, result: { content: [{ type: "text", text: `〔${body.params.name} 回来了〕` }] } });
      }
      res.writeHead(400); res.end();
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({
    port: server.address().port,
    url: `http://127.0.0.1:${server.address().port}/mcp`,
    calls, lists,
    clear() { calls.length = 0; lists.length = 0; },
    async close() { server.closeAllConnections?.(); await new Promise((r) => server.close(r)); },
  })));
}

let up, loci;
const logged = [];
before(async () => {
  up = await start_upstream();
  loci = await start_loci();
  fence.allow(up.port, loci.port);
});
after(async () => {
  await up.close();
  await loci.close();
  fs.rmSync(root, { recursive: true, force: true });
});

function runner({ env = {}, own = {}, alarm_ms, data_root = fresh_dir() } = {}) {
  loci.clear();
  const r = create_own_turn({
    env: { LOCI_HOOK_TOKEN: HOOK, ...env }, data_root, upstream: up.url, loci_address: loci.url,
    read_own: () => ({ tool_rounds: 6, models: [], ...own }), log: (s) => logged.push(s),
    ...(alarm_ms ? { alarm_ms } : {}),
  });
  r.data_root = data_root;
  return r;
}
const lines = (r) => {
  const f = path.join(r.data_root, "logs", "present.jsonl");
  return fs.existsSync(f) ? fs.readFileSync(f, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
};
const MSG = [{ role: "system", content: "唤醒信" }, { role: "user", content: "（ta 不在）" }];
const wait_for = async (cond, ms = 2000) => {
  const end = Date.now() + ms;
  while (!cond()) { if (Date.now() > end) throw new Error("timed out waiting"); await new Promise((r) => setTimeout(r, 5)); }
};

test("no key: a typed no_key, unpaid, nothing sent, nothing logged", async () => {
  up.plan(() => ({ text: "不该到这儿" }));
  const r = runner();
  const res = await r.run({ kind: "wake", messages: MSG });
  assert.deepStrictEqual([res.ok, res.outcome, res.reason, res.counted], [false, "unpaid", "no_key", false]);
  assert.strictEqual(up.got.length, 0);
  assert.deepStrictEqual(lines(r), []);
  assert.strictEqual(r.status().key, null);
});

test("the borrowed key and model: her latest accepted request's credential and model; a fixed key wins", async () => {
  up.plan(() => ({ text: "在的。" }));
  const r = runner();
  r.remember_owner({ headers: { authorization: `Bearer ${KEY}`, "x-other": "drop me" }, model: "her-model" });
  const res = await r.run({ kind: "wake", messages: MSG });
  assert.strictEqual(res.outcome, "ok");
  assert.strictEqual(res.text, "在的。");
  assert.strictEqual(res.key, "borrowed");
  assert.strictEqual(up.got[0].headers.authorization, `Bearer ${KEY}`);
  assert.strictEqual(up.got[0].headers["x-other"], undefined, "only credential headers are borrowed");
  assert.strictEqual(up.got[0].body.model, "her-model");
  assert.strictEqual(up.got[0].body.stream, true);
  assert.strictEqual(up.got[0].body.stream_options.include_usage, true);
  assert.strictEqual(up.got[0].body.tools, undefined, "no tools unless the caller offers them");
  assert.deepStrictEqual(res.usage, { prompt_tokens: 100, completion_tokens: 10, total_tokens: 110, last_prompt_tokens: 100 });
  assert.deepStrictEqual(r.status(), { busy: false, running: null, key: "borrowed", borrowed_at: r.status().borrowed_at, owner_model: "her-model" });

  up.plan(() => ({ text: "在的。" }));
  const f = runner({ env: { LOCI_UPSTREAM_KEY: FIXED }, own: { models: ["own-a"] } });
  f.remember_owner({ headers: { authorization: `Bearer ${KEY}` }, model: "her-model" });
  const res2 = await f.run({ kind: "report", messages: MSG });
  assert.strictEqual(res2.key, "fixed");
  assert.strictEqual(up.got[0].headers.authorization, `Bearer ${FIXED}`);
  assert.strictEqual(up.got[0].body.model, "own-a", "own.models comes before her model");

  const none = runner({ env: { LOCI_UPSTREAM_KEY: FIXED } });
  const res3 = await none.run({ kind: "wake", messages: MSG });
  assert.deepStrictEqual([res3.outcome, res3.reason], ["unpaid", "no_model"], "a fixed key with no model anywhere sends nothing");
});

test("a request that carried no key still lends: her upstream takes none, and own turns go without one", async () => {
  up.plan(() => ({ text: "好。" }));
  const r = runner();
  r.remember_owner({ headers: {}, model: "local-model" });
  const res = await r.run({ kind: "wake", messages: MSG });
  assert.strictEqual(res.outcome, "ok");
  assert.strictEqual(up.got[0].headers.authorization, undefined);
});

test("tool calls 7 times in a row: 6 rounds run on Loci, the 7th answer is cut, what was said comes back", async () => {
  up.plan((body, i) => ({ text: `第${i}次`, calls: [["recall", { query: `q${i}` }]] }));
  const r = runner();
  r.remember_owner({ headers: { authorization: `Bearer ${KEY}` }, model: "m" });
  const res = await r.run({ kind: "wake", messages: MSG, tools: "loci", turn: "wake-0042" });
  assert.strictEqual(res.outcome, "ok");
  assert.strictEqual(res.cut, true);
  assert.strictEqual(res.rounds, 6);
  assert.strictEqual(res.calls, 7);
  assert.strictEqual(up.got.length, 7, "no 8th request");
  assert.strictEqual(loci.calls.length, 6, "the 7th answer's tool call is not executed");
  assert.deepStrictEqual(res.cut_tools, ["recall"]);
  assert.strictEqual(res.text, "第7次");
  assert.strictEqual(res.said.split("\n\n").length, 7);
  assert.deepStrictEqual(res.tools_used, Array(6).fill("recall"));
  assert.deepStrictEqual(loci.calls.map((c) => c.headers["loci-turn"]), [1, 2, 3, 4, 5, 6].map((k) => `wake-0042#${k}`));
  assert.ok(loci.calls.every((c) => c.headers["x-loci-hook-token"] === HOOK));
  assert.ok(loci.lists.length === 1 && loci.lists[0].headers["x-loci-hook-token"] === HOOK, "Loci's tools are listed once per run");
  assert.deepStrictEqual(loci.calls[0].args, { query: "q1" }, "arguments arrive whole even when streamed in pieces");
  assert.deepStrictEqual(up.got[0].body.tools.map((t) => t.function.name), ["recall", "grow"], "tools: \"loci\" offers what Loci lists");
  // the 7th request carries the six rounds: assistant tool_calls and their results, in order
  const last = up.got[6].body.messages;
  assert.strictEqual(last.filter((m) => m.role === "tool").length, 6);
  assert.strictEqual(last.at(-1).content, "〔recall 回来了〕");
  assert.strictEqual(last.at(-1).tool_call_id, last.at(-2).tool_calls[0].id);
  assert.strictEqual(res.usage.prompt_tokens, 700);
  const [started, result] = lines(r);
  assert.deepStrictEqual([started.phase, result.phase, result.outcome, result.cut, result.rounds], ["started", "result", "ok", true, 6]);
});

test("a tool that is not Loci's gets 「ta 不在」; Loci's in the same answer runs; offered tools go out as given", async () => {
  const offered = [{ type: "function", function: { name: "web_search", parameters: { type: "object" } } },
                   { type: "function", function: { name: "recall", parameters: { type: "object" } } }];
  up.plan((body, i) => (i === 1 ? { text: "", calls: [["web_search", { q: "x" }], ["recall", { query: "ta" }]] } : { text: "想你了。" }));
  const r = runner();
  r.remember_owner({ headers: { authorization: `Bearer ${KEY}` }, model: "m" });
  const res = await r.run({ kind: "wake", messages: MSG, tools: offered });
  assert.strictEqual(res.outcome, "ok");
  assert.strictEqual(res.text, "想你了。");
  assert.strictEqual(res.cut, false);
  assert.deepStrictEqual(res.tools_used, ["recall"]);
  assert.deepStrictEqual(res.tools_refused, ["web_search"]);
  assert.deepStrictEqual(up.got[0].body.tools, offered, "the caller's tools, byte for byte");
  const results = up.got[1].body.messages.filter((m) => m.role === "tool").map((m) => m.content);
  assert.deepStrictEqual(results, [NOT_HERE, "〔recall 回来了〕"]);
  assert.strictEqual(NOT_HERE, "ta 不在，这个现在用不了。");
  assert.deepStrictEqual(loci.calls.map((c) => c.name), ["recall"], "web_search never reached Loci");
});

test("a client's prefixed Loci name runs on Loci under the bare name; exact wins; ambiguous or foreign stays 「ta 不在」", async () => {
  const listed = ["recall", "grow", "re_grow"];
  for (const name of ["loci__recall", "mcp_loci_recall", "loci.recall", "loci-recall", "loci:recall", "loci/recall"]) {
    assert.strictEqual(loci_name(name, listed), "recall", name);
  }
  assert.strictEqual(loci_name("re_grow", listed), "re_grow", "an exact name wins over its own suffix");
  assert.strictEqual(loci_name("x_re_grow", listed), null, "ends in two Loci names: ambiguous");
  assert.strictEqual(loci_name("myrecall", listed), null, "no separator before the name");
  assert.strictEqual(loci_name("recall_x", listed), null);
  assert.strictEqual(loci_name("web_search", listed), null);

  up.plan((body, i) => (i === 1 ? { text: "", calls: [["mcp_loci_recall", { query: "ta" }], ["other__search", {}]] } : { text: "好。" }));
  const r = runner();
  r.remember_owner({ headers: { authorization: `Bearer ${KEY}` }, model: "m" });
  const res = await r.run({ kind: "wake", messages: MSG });
  assert.deepStrictEqual(loci.calls.map((c) => c.name), ["recall"], "Loci is called with the bare name");
  assert.deepStrictEqual(res.tools_used, ["recall"]);
  assert.deepStrictEqual(res.tools_refused, ["other__search"]);
  const results = up.got[1].body.messages.filter((m) => m.role === "tool").map((m) => m.content);
  assert.deepStrictEqual(results, ["〔recall 回来了〕", NOT_HERE]);
});

test("abort when she arrives, mid-stream: at once, not a failure, and no result line claims success", async () => {
  up.plan(() => ({ text: "我在想要不要跟你说一件事", half: true }));
  const r = runner();
  r.remember_owner({ headers: { authorization: `Bearer ${KEY}` }, model: "m" });
  const p = r.run({ kind: "wake", messages: MSG });
  await wait_for(() => up.got.length === 1);
  await new Promise((res) => setTimeout(res, 30));   // the first half is on its way
  assert.strictEqual(r.busy(), true);
  const t0 = Date.now();
  assert.strictEqual(r.abort("owner_arrived"), true);
  const res = await p;
  assert.ok(Date.now() - t0 < 1000, "the abort does not wait for upstream");
  assert.deepStrictEqual([res.ok, res.outcome, res.reason, res.counted, res.text], [false, "aborted", "owner_arrived", false, ""]);
  assert.strictEqual(r.busy(), false);
  const log = lines(r);
  assert.deepStrictEqual(log.map((l) => l.phase), ["started", "result"]);
  assert.ok(!log.some((l) => l.outcome === "ok"), "nothing claims success");
  assert.strictEqual(log[1].outcome, "aborted");
  assert.strictEqual(r.abort(), false, "nothing to abort any more");
});

test("one at a time: a second caller is busy; the alarm aborts a hung run as a paid failure", async () => {
  up.plan(() => ({ hang: true }));
  const r = runner({ alarm_ms: 150 });
  r.remember_owner({ headers: { authorization: `Bearer ${KEY}` }, model: "m" });
  const p = r.run({ kind: "wake", messages: MSG });
  const second = await r.run({ kind: "report", messages: MSG });
  assert.deepStrictEqual([second.outcome, second.reason], ["busy", "busy"]);
  const res = await p;
  assert.deepStrictEqual([res.outcome, res.reason, res.counted], ["paid", "alarm", true]);
  assert.strictEqual(lines(r).at(-1).reason, "alarm");
});

test("model chain: a refused model falls to the next; context-too-long, 429 and 500 do not", async () => {
  up.plan((body) => (body.model === "gone" ? { status: 404, error: "The model `gone` does not exist" }
    : body.model === "locked" ? { status: 403, error: "You do not have access to model locked" }
    : { text: `我是 ${body.model}` }));
  const r = runner({ own: { models: ["gone", "locked", "good"] } });
  r.remember_owner({ headers: { authorization: `Bearer ${KEY}` }, model: "her" });
  const res = await r.run({ kind: "wake", messages: MSG });
  assert.strictEqual(res.outcome, "ok");
  assert.strictEqual(res.model, "good");
  assert.deepStrictEqual(up.got.map((g) => g.body.model), ["gone", "locked", "good"]);

  up.plan(() => ({ status: 404, error: "no such model" }));
  const all = await r.run({ kind: "wake", messages: MSG });
  assert.deepStrictEqual([all.outcome, all.reason, up.got.length], ["paid", "http_404", 3], "every model refused: a paid failure");

  for (const [status, error] of [[400, "This model's maximum context length is 8192 tokens"], [429, "Rate limit reached for model gone"], [500, "model server error"]]) {
    up.plan(() => ({ status, error }));
    const one = await r.run({ kind: "wake", messages: MSG });
    assert.deepStrictEqual([one.outcome, one.reason, up.got.length], ["paid", `http_${status}`, 1], `HTTP ${status} is not a model refusal`);
  }
  assert.strictEqual(model_refused(400, '{"error":{"code":"model_not_found"}}', "x"), true);
  assert.strictEqual(model_refused(401, "invalid model key", "x"), false);
});

test("paid or unpaid: cannot connect is unpaid; an HTTP error or a broken stream is paid", async () => {
  // a port nobody listens on (allowed through the fence, so the refusal is the OS's own)
  const probe = http.createServer();
  await new Promise((r) => probe.listen(0, "127.0.0.1", r));
  const dead = probe.address().port;
  await new Promise((r) => probe.close(r));
  fence.allow(dead);
  const r0 = create_own_turn({ env: {}, data_root: fresh_dir(), upstream: `http://127.0.0.1:${dead}/v1`,
                                loci_address: loci.url, log: () => {} });
  r0.remember_owner({ headers: { authorization: `Bearer ${KEY}` }, model: "m" });
  const unsent = await r0.run({ kind: "wake", messages: MSG });
  assert.deepStrictEqual([unsent.outcome, unsent.reason, unsent.counted, unsent.reached_upstream], ["unpaid", "connect", false, false]);

  const r = runner();
  r.remember_owner({ headers: { authorization: `Bearer ${KEY}` }, model: "m" });
  up.plan(() => ({ status: 500, error: "boom" }));
  const http_err = await r.run({ kind: "wake", messages: MSG });
  assert.deepStrictEqual([http_err.outcome, http_err.reason, http_err.counted], ["paid", "http_500", true]);

  up.plan(() => ({ text: "说到一半", half: "break" }));
  const broke = await r.run({ kind: "wake", messages: MSG });
  assert.deepStrictEqual([broke.outcome, broke.reason, broke.counted], ["paid", "stream_broke", true]);

  up.plan(() => ({ text: "非流式也认", json: true }));
  const plain = await r.run({ kind: "wake", messages: MSG });
  assert.deepStrictEqual([plain.outcome, plain.text], ["ok", "非流式也认"], "a provider that ignores stream:true is read as JSON");
});

test("silence: empty, 【无话】 or \"No response requested.\" as the whole reply only", async () => {
  assert.ok(is_silence(""));
  assert.ok(is_silence("  \n"));
  assert.ok(is_silence("【无话】"));
  assert.ok(is_silence(" No response requested. "));
  assert.ok(!is_silence("我想说【无话】这个词挺好玩"));
  assert.ok(!is_silence("No response requested. 但其实我有话说"));
  up.plan(() => ({ text: "【无话】" }));
  const r = runner();
  r.remember_owner({ headers: { authorization: `Bearer ${KEY}` }, model: "m" });
  const res = await r.run({ kind: "wake", messages: MSG });
  assert.deepStrictEqual([res.outcome, res.silent], ["ok", true]);
  assert.strictEqual(lines(r).at(-1).silent, true);
});

test("🔴 a summary block never reaches text or said; it comes back apart, and only the next round upstream sees it", async () => {
  const S = "哨兵·OWN-TURN-SUMMARY-3a0e";
  const OPEN = "【窗口摘要】";
  const CLOSE = "【/窗口摘要】";
  up.plan((body, i) => (i === 1
    ? { text: `先查一下。${OPEN}早的${S}${CLOSE}`, calls: [["recall", { query: "q" }]] }
    : { text: `好的。\n**${OPEN}**\n晚的${S}\n**${CLOSE}**\n` }));
  const r = runner();
  r.remember_owner({ headers: { authorization: `Bearer ${KEY}` }, model: "m" });
  const res = await r.run({ kind: "wake", messages: MSG, tools: "loci" });
  assert.strictEqual(res.outcome, "ok");
  assert.strictEqual(res.text, "好的。\n\n");
  assert.strictEqual(res.said, "先查一下。\n\n好的。\n\n");
  assert.deepStrictEqual(res.summary, { text: `晚的${S}`, closed: true }, "the newest block");
  assert.strictEqual(res.silent, false);
  assert.ok(up.got[1].body.messages.some((m) => m.role === "assistant" && String(m.content).includes(`早的${S}`)),
    "the tool round goes back to him as he wrote it");
  assert.ok(!JSON.stringify(lines(r)).includes(S));

  // a block and nothing else is silence; a block that never closes comes back marked so
  up.plan(() => ({ text: `${OPEN}只收${S}${CLOSE}` }));
  const only = await r.run({ kind: "wake", messages: MSG });
  assert.deepStrictEqual([only.text, only.silent, only.summary], ["", true, { text: `只收${S}`, closed: true }]);
  up.plan(() => ({ text: `嗯。${OPEN}半截${S}` }));
  const half = await r.run({ kind: "pack", messages: MSG });
  assert.deepStrictEqual([half.text, half.said, half.summary], ["嗯。", "嗯。", { text: `半截${S}`, closed: false }]);
});

test("the key never reaches the disk or the log, even when upstream echoes it back", async () => {
  const dir = fresh_dir();
  const r = runner({ data_root: dir });
  r.remember_owner({ headers: { authorization: `Bearer ${KEY}`, "x-api-key": KEY }, model: "m" });
  up.plan((body, i) => (i === 1 ? { status: 401, error: `Incorrect API key provided: ${KEY}` } : { text: "x" }));
  const res = await r.run({ kind: "wake", messages: MSG });
  assert.strictEqual(res.reason, "http_401");
  assert.ok(!res.error.includes(KEY) && res.error.includes("••••"), "the echoed key is redacted");
  const f = runner({ data_root: dir, env: { LOCI_UPSTREAM_KEY: FIXED } });
  up.plan(() => ({ status: 403, error: `bad key ${FIXED}` }));
  f.remember_owner({ headers: {}, model: "m" });
  await f.run({ kind: "wake", messages: MSG });
  up.plan(() => ({ text: "好", calls: [] }));
  await r.run({ kind: "wake", messages: MSG });

  const walk = (d) => fs.readdirSync(d, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? walk(path.join(d, e.name)) : [path.join(d, e.name)]));
  const files = walk(dir);
  assert.ok(files.some((p) => p.endsWith("present.jsonl")), "there is a log to search");
  for (const p of files) {
    const text = fs.readFileSync(p, "utf8");
    assert.ok(!text.includes(KEY) && !text.includes(FIXED), `${p} holds a key`);
  }
  for (const s of logged) assert.ok(!s.includes(KEY) && !s.includes(FIXED), "a console line holds a key");
  assert.ok(!JSON.stringify(r.status()).includes(KEY), "status() never carries the key");
});

test("relay: an accepted chat request lends its credential and model; a refused one does not", async () => {
  const seen = [];
  const present = {
    prepare: async () => ({ body: null, ctx: null, notes: [] }),
    on_response: () => null,
    remember_owner: (x) => seen.push({ auth: x.headers.authorization, model: x.model }),
  };
  const handle = create_relay({ upstream: up.url, present });
  const gw = http.createServer((req, res) => handle(req, res, { start: Date.now() }));
  await new Promise((r) => gw.listen(0, "127.0.0.1", r));
  fence.allow(gw.address().port);
  const quiet = console.log;
  console.log = () => {};
  try {
    const send = (auth) => fetch(`http://127.0.0.1:${gw.address().port}/v1/chat/completions`, {
      method: "POST", headers: { "content-type": "application/json", authorization: auth },
      body: JSON.stringify({ model: "her-model", messages: [{ role: "user", content: "在吗" }] }),
    }).then((x) => x.text());
    up.plan(() => ({ text: "在", json: true }));
    await send(`Bearer ${KEY}`);
    up.plan(() => ({ status: 401, error: "bad key" }));
    await send("Bearer wrong-key");
  } finally {
    console.log = quiet;
    gw.closeAllConnections?.();
    await new Promise((r) => gw.close(r));
  }
  assert.deepStrictEqual(seen, [{ auth: `Bearer ${KEY}`, model: "her-model" }]);
});
