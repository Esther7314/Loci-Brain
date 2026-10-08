// ============================================================
// gateway/tests/present_away.test.js — away (present/away.js) and push (present/push.js)
//
// In-process, behind the network fence, on a fake clock: the real relay and the real
// present layer in front of a scripted fake upstream (chat answers, streamed or plain,
// and wake answers), a fake Loci (cue with no cards, poke with nothing) and a fake Bark
// server that books every push and can be told to fail or hang. Temp directories only.
// Fixtures say 「ta」.
//
// What it holds the step to (blueprint §六 row 12, §二.1 away/push, §四 night):
//   · the wake said X → the next turn's first streamed chunk is the prefix, and upstream
//     gets X as his message right before her line; the day store keeps the reply the
//     client saw, prefix included
//   · the turn after that, the client history carries the prefix → no injection, and
//     held.json is empty
//   · a resend within two minutes and a regenerate of the prefixed turn carry the same
//     prefix again; a line held after that waits for her next new turn
//   · two or more held lines: one line each with its local time; lines held on another
//     thread are prefixed in the thread she speaks in; a plain JSON answer gets the
//     prefix in front of its text
//   · push: the full text by default (cut to fit a notification), only a notice with
//     push.text "notice"; a key or a
//     whole URL; nothing in DND; a failed push retried up to 3 times within 15 minutes,
//     then given up; a confirmed line not pushed again; 10 s timeout (shortened here);
//     the key never in a log, a book, an answer or an error
//   · POST /present/push-test answers Bark's outcome; /present and /health report the
//     last push
// ============================================================

const { test, before, after } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const fence = require("./network_fence.js");
const { create_fake_clock } = require("./fake_clock.js");

const { create_present } = require("../present/index.js");
const { create_relay } = require("../relay.js");
const { create_present_api } = require("../present/present_api.js");
const { create_push, endpoint_of, scrub, fit_body, BODY_MAX_BYTES } = require("../present/push.js");
const { prefix_block } = require("../present/away.js");
const { create_sse_prefix, create_json_prefix } = require("../present/stream_filter.js");
const { create_settings } = require("../present/settings.js");

const ZONE = "Asia/Shanghai";
const KEY = "sk-away-test-SENTINEL-41ac";
const BARK_KEY = "BarkKeySENTINEL7Q";
const LETTER_MARK = "【自由时间";
const at = (local) => Date.parse(`${local}+08:00`);
const MIN = 60 * 1000;

const root = fs.mkdtempSync(path.join(os.tmpdir(), "loci-away-"));
let n = 0;
const fresh_dir = () => { const d = path.join(root, `d${++n}`); fs.mkdirSync(d, { recursive: true }); return d; };

const is_wake = (body) => {
  const last = body?.messages?.[body.messages.length - 1];
  return typeof last?.content === "string" && last.content.startsWith(LETTER_MARK);
};

// ———— fake upstream: plan(body, { wake }) → { text } ————
function start_upstream() {
  const got = [];
  let plan = () => ({ text: "嗯。" });
  let seq = 0;
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      if (req.method === "GET") {
        res.writeHead(200, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ object: "list", data: [] }));
      }
      const body = JSON.parse(Buffer.concat(chunks).toString("utf8") || "null");
      const wake = is_wake(body);
      got.push({ wake, body });
      const text = String((plan(body, { wake }) || {}).text ?? "");
      const id = `chatcmpl-up-${++seq}`;
      const base = { id, object: "chat.completion.chunk", created: 1760000000 + seq, model: "fake-model", system_fingerprint: "fp_up" };
      if (body.stream !== true) {
        res.writeHead(200, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ id, object: "chat.completion", created: base.created, model: "fake-model",
          choices: [{ index: 0, message: { role: "assistant", content: text }, finish_reason: "stop" }],
          usage: { prompt_tokens: 900, completion_tokens: 9, total_tokens: 909 } }));
      }
      const events = [{ ...base, choices: [{ index: 0, delta: { role: "assistant", content: "" }, finish_reason: null }] }];
      for (let i = 0; i < text.length; i += 3) events.push({ ...base, choices: [{ index: 0, delta: { content: text.slice(i, i + 3) }, finish_reason: null }] });
      events.push({ ...base, choices: [{ index: 0, delta: {}, finish_reason: "stop" }] });
      events.push({ ...base, choices: [], usage: { prompt_tokens: 900, completion_tokens: 9, total_tokens: 909 } });
      res.writeHead(200, { "Content-Type": "text/event-stream" });
      res.end(events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("") + "data: [DONE]\n\n");
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({
    port: server.address().port,
    url: `http://127.0.0.1:${server.address().port}/v1`,
    wakes: () => got.filter((g) => g.wake),
    chats: () => got.filter((g) => !g.wake),
    plan(fn) { plan = fn; got.length = 0; },
    async close() { server.closeAllConnections?.(); await new Promise((r) => server.close(r)); },
  })));
}

// ———— fake Loci: cue with no cards, poke with nothing, an MCP face with one tool ————
function start_loci() {
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const route = req.url.split("?")[0];
      const json = (obj) => { res.writeHead(200, { "Content-Type": "application/json" }); res.end(JSON.stringify(obj)); };
      if (route === "/mcp") {
        const body = JSON.parse(Buffer.concat(chunks).toString("utf8") || "null");
        const sse = (obj, session) => {
          res.writeHead(200, { "Content-Type": "text/event-stream", ...(session ? { "Mcp-Session-Id": session } : {}) });
          res.end(`event: message\ndata: ${JSON.stringify(obj)}\n\n`);
        };
        if (body?.method === "initialize") return sse({ jsonrpc: "2.0", id: body.id, result: { protocolVersion: "2024-11-05", capabilities: {} } }, "s1");
        if (body?.method === "notifications/initialized") { res.writeHead(202); return res.end(); }
        if (body?.method === "tools/list") return sse({ jsonrpc: "2.0", id: body.id, result: { tools: [{ name: "recall", description: "找回记忆", inputSchema: { type: "object" } }] } });
        res.writeHead(400); return res.end();
      }
      if (route === "/api/v2/cue") return json({ cards: [], text: "" });
      if (route.startsWith("/api/v2/cue/")) return json({ ok: true });
      if (route === "/api/loci/poke") return json({ dreams: [], muse_pending: 0 });
      if (route === "/api/loci/dream/wake") return json({});
      res.writeHead(404); res.end();
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({
    port: server.address().port,
    url: `http://127.0.0.1:${server.address().port}/mcp`,
    async close() { server.closeAllConnections?.(); await new Promise((r) => server.close(r)); },
  })));
}

// ———— fake Bark: books every push; answer() → { status, message } | { hang: true } ————
function start_bark() {
  const got = [];
  const open = new Set();
  let answer = () => ({ status: 200 });
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      let body = null;
      try { body = JSON.parse(Buffer.concat(chunks).toString("utf8")); } catch { body = null; }
      got.push({ method: req.method, path: req.url, body });
      const a = answer(got.length) || {};
      if (a.hang) { open.add(res); return; }
      res.writeHead(a.status || 200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ code: a.status || 200, message: a.message || "success", timestamp: 1 }));
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({
    port: server.address().port,
    base: `http://127.0.0.1:${server.address().port}`,
    url: `http://127.0.0.1:${server.address().port}/${BARK_KEY}`,
    got,
    answer(fn) { answer = fn; got.length = 0; },
    release() { for (const r of open) { try { r.destroy(); } catch { /* gone */ } } open.clear(); },
    async close() { this.release(); server.closeAllConnections?.(); await new Promise((r) => server.close(r)); },
  })));
}

let up, loci, bark;
const servers = [];
before(async () => {
  up = await start_upstream();
  loci = await start_loci();
  bark = await start_bark();
  fence.allow(up.port, loci.port, bark.port);
});
after(async () => {
  for (const s of servers) { s.closeAllConnections?.(); await new Promise((r) => s.close(r)); }
  await up.close();
  await loci.close();
  await bark.close();
  fs.rmSync(root, { recursive: true, force: true });
});

const WAKE_ON = { on: true, every_min: 60, dnd: { on: false, from: "23:00", to: "08:00" }, daily_cap: 16 };

/** The real present layer, the real relay and the real /present routes, in this process, on a fake clock. */
async function boot({ wake = WAKE_ON, push = { bark: bark.url }, start = at("2026-10-07T10:00:00"), held = null } = {}) {
  const data_root = fresh_dir();
  fs.mkdirSync(path.join(data_root, "state"), { recursive: true });
  fs.writeFileSync(path.join(data_root, "state", "poke-window.json"),
    JSON.stringify({ lastUserMessageTime: new Date().toISOString(), wakePending: false }));
  fs.writeFileSync(path.join(data_root, "present.json"), JSON.stringify({ wake, push }));
  if (held) fs.writeFileSync(path.join(data_root, "held.json"), JSON.stringify(held));
  const clock = create_fake_clock(start);
  const present = create_present({ env: { LOCI_TZ: ZONE }, data_root, clock, upstream: up.url, loci: loci.url, log: () => {} });
  const relay = create_relay({ upstream: up.url, present });
  const api = create_present_api(present);
  const server = http.createServer((req, res) => (req.url.startsWith("/present") ? api(req, res) : relay(req, res, { start: Date.now() })));
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  servers.push(server);
  fence.allow(server.address().port);
  const origin = `http://127.0.0.1:${server.address().port}`;

  async function chat(messages, extra = {}) {
    const resp = await fetch(`${origin}/v1/chat/completions`, { method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${KEY}` },
      body: JSON.stringify({ model: "her-model", stream: true, messages, ...extra }) });
    const text = await resp.text();
    assert.strictEqual(resp.status, 200, text);
    return text;
  }
  /** Her turn; waits until its reply is written (the snapshot moved on). */
  async function say(messages, extra = {}) {
    const before_at = present.threads.list().map((t) => t.last_sent?.at || 0);
    const text = await chat(messages, extra);
    await wait_for(() => present.threads.list().some((t) => t.last_sent && !before_at.includes(t.last_sent.at)));
    return text;
  }
  async function call(method, route, body) {
    const resp = await fetch(`${origin}${route}`, { method, headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body) });
    const text = await resp.text();
    return { status: resp.status, text, json: JSON.parse(text) };
  }
  return { present, clock, data_root, chat, say, call };
}

const wait_for = async (cond, ms = 3000) => {
  const end = Date.now() + ms;
  while (!cond()) { if (Date.now() > end) throw new Error("timed out waiting"); await new Promise((r) => setTimeout(r, 5)); }
};

async function run_until(g, until) {
  while (g.clock.now() < until) { g.clock.advance(MIN); await g.present.wake.tick(); }
}

/** The data payloads of an SSE body, parsed. */
const sse_chunks = (text) => text.split("\n\n").map((e) => e.split("\n").find((l) => l.startsWith("data: ")))
  .filter(Boolean).map((l) => l.slice(6)).filter((d) => d !== "[DONE]").map((d) => JSON.parse(d));
const sse_text = (text) => sse_chunks(text).map((c) => c.choices?.[0]?.delta?.content || "").join("");
const read = (dir, name) => { const f = path.join(dir, name); return fs.existsSync(f) ? fs.readFileSync(f, "utf8") : ""; };
const held_doc = (dir) => JSON.parse(read(dir, "held.json") || '{"items":[]}');
const day_lines = (dir) => {
  const d = path.join(dir, "host", "days");
  return fs.existsSync(d) ? fs.readdirSync(d).flatMap((f) => fs.readFileSync(path.join(d, f), "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l))) : [];
};
const last_assistant_before_her = (body) => body.messages[body.messages.length - 2];

const SYS = { role: "system", content: "你是 ta 的伴。" };
const X = "刚才路过窗口，看见晚霞了，想跟 ta 说一声。";

// ———————————————————————————————————————————————

test("prefix lines: one held line bare, two or more one per line with the local time", () => {
  assert.strictEqual(prefix_block([{ at: "2026-10-07T14:05:00+08:00", text: " 想你了。\n" }], ZONE),
    "（你不在的时候我说过：想你了。）\n\n");
  assert.strictEqual(prefix_block([
    { at: "2026-10-07T14:05:00+08:00", text: "想你了。" },
    { at: "2026-10-07T06:30:00Z", text: "晚霞很好看。" },
  ], ZONE), "（你不在的时候我说过 · 14:05：想你了。）\n（你不在的时候我说过 · 14:30：晚霞很好看。）\n\n");
  assert.strictEqual(prefix_block([], ZONE), "");
});

test("the prefix in the stream: comments pass first, the chunk copies upstream's first chunk, every byte after as sent, however cut", async () => {
  const first = { id: "c1", object: "chat.completion.chunk", created: 5, model: "m", system_fingerprint: "fp", choices: [{ index: 0, delta: { role: "assistant" } }] };
  const body = Buffer.from(`: keep-alive\r\n\r\ndata: ${JSON.stringify(first)}\r\n\r\n`
    + `data: {"id":"c1","choices":[{"index":0,"delta":{"content":"嗯"}}]}\r\n\r\ndata: [DONE]\r\n\r\n`, "utf8");
  const prefix = "（你不在的时候我说过：在的。）\n\n";
  const chunk = { id: "c1", object: "chat.completion.chunk", created: 5, model: "m", system_fingerprint: "fp",
                  choices: [{ index: 0, delta: { role: "assistant", content: prefix }, finish_reason: null }] };
  const want = ": keep-alive\r\n\r\n" + `data: ${JSON.stringify(chunk)}\n\n` + body.toString("utf8").slice(": keep-alive\r\n\r\n".length);
  for (const size of [1, 2, 7, 40, body.length]) {
    const f = create_sse_prefix(prefix);
    const out = [];
    f.on("data", (c) => out.push(c));
    const ended = new Promise((r) => f.on("end", r));
    for (let i = 0; i < body.length; i += size) f.write(body.subarray(i, i + size));
    f.end();
    await ended;
    assert.strictEqual(Buffer.concat(out).toString("utf8"), want, `cut every ${size} bytes`);
  }
  // a plain tool-call answer with no text gets the prefix as its text
  const j = create_json_prefix(prefix);
  const out = [];
  j.on("data", (c) => out.push(c));
  const ended = new Promise((r) => j.on("end", r));
  j.end(JSON.stringify({ choices: [{ index: 0, message: { role: "assistant", content: null, tool_calls: [{ id: "t" }] } }] }));
  await ended;
  assert.strictEqual(JSON.parse(Buffer.concat(out).toString("utf8")).choices[0].message.content, prefix);
});

test("the wake said X: pushed in full; next turn starts with the prefix and upstream has X before her line; then confirmed", { timeout: 30000 }, async () => {
  bark.answer(() => ({ status: 200 }));
  up.plan((body, { wake }) => {
    if (wake) return { text: X };
    const last = body.messages[body.messages.length - 1].content;
    return { text: last === "我去上课了。" ? "好，去吧。" : last === "我回来了。" ? "回来啦，课怎么样？" : "好看，像橘子汽水。" };
  });
  const g = await boot();
  const t1 = [SYS, { role: "user", content: "我去上课了。" }];
  await g.say(t1);
  await run_until(g, at("2026-10-07T11:00:00"));
  assert.strictEqual(up.wakes().length, 1, "one wake went upstream");
  assert.deepStrictEqual(held_doc(g.data_root).items.map((i) => i.text), [X]);

  // the push: one POST of { title, body } with the full text, to the URL as configured
  await wait_for(() => bark.got.length === 1);
  assert.deepStrictEqual(bark.got[0], { method: "POST", path: `/${BARK_KEY}`, body: { title: "Loci", body: X } });
  await wait_for(() => g.present.push.status().last !== null);
  assert.deepStrictEqual(g.present.push.status(), { state: "on", last: { at: "2026-10-07T11:00:00+08:00", ok: true, status: 200 }, retrying: 0 });

  // her next turn
  g.clock.advance(20 * MIN);
  const t2 = [...t1, { role: "assistant", content: "好，去吧。" }, { role: "user", content: "我回来了。" }];
  const text2 = await g.say(t2);
  const chunks = sse_chunks(text2);
  const prefix = `（你不在的时候我说过：${X}）\n\n`;
  assert.deepStrictEqual(chunks[0].choices, [{ index: 0, delta: { role: "assistant", content: prefix }, finish_reason: null }],
    "the first chunk the client gets is the prefix");
  const first_up = chunks[1];
  for (const k of ["id", "object", "created", "model", "system_fingerprint"]) assert.strictEqual(chunks[0][k], first_up[k], `${k} as upstream's first chunk`);
  assert.strictEqual(sse_text(text2), `${prefix}回来啦，课怎么样？`);
  const sent2 = up.chats()[1].body;
  assert.deepStrictEqual(sent2.messages[sent2.messages.length - 1], { role: "user", content: "我回来了。" });
  assert.deepStrictEqual(last_assistant_before_her(sent2), { role: "assistant", content: X }, "X goes upstream as his, right before her line");
  // the day store keeps what the client saw
  const reply2 = day_lines(g.data_root).filter((l) => l.role === "assistant" && !l.woke).pop();
  assert.strictEqual(reply2.text, `${prefix}回来啦，课怎么样？`);
  assert.strictEqual(held_doc(g.data_root).items.length, 1, "not confirmed until the client's history shows it");

  // the turn after: the history carries the prefixed reply → confirmed, nothing injected
  g.clock.advance(5 * MIN);
  const t3 = [...t2, { role: "assistant", content: `${prefix}回来啦，课怎么样？` }, { role: "user", content: "晚霞好看吗？" }];
  const text3 = await g.say(t3);
  assert.ok(!sse_text(text3).includes("你不在的时候"), "no prefix on the turn after");
  assert.strictEqual(sse_text(text3), "好看，像橘子汽水。");
  const sent3 = up.chats()[2].body;
  assert.ok(!sent3.messages.some((m) => m.role === "assistant" && m.content === X), "X is no longer injected anywhere");
  assert.strictEqual(sent3.messages.length, t3.length, "the client's history and nothing else (no cards here)");
  assert.deepStrictEqual(held_doc(g.data_root).items, [], "held.json is empty");
  assert.strictEqual(g.present.wake.status().held, 0);
  const t = g.present.threads.list().find((x) => x.window);
  assert.ok(!t.window.overlays.some((o) => o.kind === "away"), "the away overlay is gone");
  // the threads module saw the prefixed reply as the same line, not an edit of it
  assert.ok(!day_lines(g.data_root).some((l) => l.rev > 1), "no revision: the stored reply is what the client sends back");

  // the key and the words never reach the log
  const log = read(g.data_root, "logs/present.jsonl");
  assert.ok(log.includes('"event":"push"'));
  for (const secret of [BARK_KEY, X, KEY]) assert.ok(!log.includes(secret), `the log carries no ${secret}`);
  assert.ok(!read(g.data_root, "counters.json").includes(BARK_KEY));
});

test("resend within 2 minutes and regenerate carry the same prefix; a line held after that waits for the next turn", { timeout: 30000 }, async () => {
  bark.answer(() => ({ status: 200 }));
  let answer = "第一版回话。";
  up.plan((body, { wake }) => ({ text: wake ? X : answer }));
  const g = await boot({ push: { bark: "" } });
  const t1 = [SYS, { role: "user", content: "晚安。" }];
  answer = "晚安。";
  await g.say(t1);
  await run_until(g, at("2026-10-07T11:00:00"));
  const prefix = `（你不在的时候我说过：${X}）\n\n`;

  g.clock.advance(10 * MIN);
  const t2 = [...t1, { role: "assistant", content: "晚安。" }, { role: "user", content: "早。" }];
  answer = "第一版回话。";
  assert.strictEqual(sse_text(await g.say(t2)), `${prefix}第一版回话。`);

  // the same request again within two minutes: the same turn, the same prefix
  g.clock.advance(MIN);
  answer = "重发的回话。";
  assert.strictEqual(sse_text(await g.say(t2)), `${prefix}重发的回话。`);

  // a line held after the prefix was shown (planted: a second wake)
  const doc = held_doc(g.data_root);
  doc.items.push({ at: "2026-10-07T11:20:00+08:00", line: "m_20261007_9999", thread: doc.items[0].thread, text: "又想起一件事。" });
  fs.writeFileSync(path.join(g.data_root, "held.json"), JSON.stringify(doc));

  // past the resend window, a regenerate of the same turn: the same prefix, the second line not in it
  g.clock.advance(3 * MIN);
  answer = "重新生成的回话。";
  const regen = await g.say(t2);
  assert.strictEqual(sse_text(regen), `${prefix}重新生成的回话。`);
  const sent = up.chats().slice(-1)[0].body;
  assert.deepStrictEqual(last_assistant_before_her(sent), { role: "assistant", content: X });
  assert.ok(!sent.messages.some((m) => m.content === "又想起一件事。"), "the later line waits for a new turn");
  const replies = day_lines(g.data_root).filter((l) => l.role === "assistant" && !l.woke && l.text.startsWith(prefix));
  const live = new Map();
  for (const l of replies) live.set(l.id, l);
  assert.deepStrictEqual([...live.values()].filter((l) => l.state === "live").map((l) => l.text), [`${prefix}重新生成的回话。`],
    "the regenerated reply is the live one, prefix included");

  // her next turn shows the prefixed reply: X confirmed, the later line prefixed alone
  g.clock.advance(MIN);
  answer = "嗯，我在听。";
  const t3 = [...t2, { role: "assistant", content: `${prefix}重新生成的回话。` }, { role: "user", content: "什么事？" }];
  assert.strictEqual(sse_text(await g.say(t3)), "（你不在的时候我说过：又想起一件事。）\n\n嗯，我在听。");
  assert.deepStrictEqual(held_doc(g.data_root).items.map((i) => i.text), ["又想起一件事。"]);
  const sent3 = up.chats().slice(-1)[0].body;
  assert.deepStrictEqual(last_assistant_before_her(sent3), { role: "assistant", content: "又想起一件事。" });
  assert.strictEqual(sent3.messages.filter((m) => m.role === "assistant" && m.content === X).length, 0);

  // she rewinds and regenerates the turn that showed X: X is shown there again
  g.clock.advance(MIN);
  answer = "再来一版。";
  assert.strictEqual(sse_text(await g.say(t2)), `${prefix}再来一版。`);
});

test("two held lines from another thread: each with its time, in this thread; a plain JSON answer gets the prefix in its text", { timeout: 30000 }, async () => {
  up.plan(() => ({ text: "我也想你。" }));
  const held = { items: [
    { at: "2026-10-07T14:05:00+08:00", line: "m_20261007_0101", thread: "t_0ther1", text: "下雨了，记得带伞。" },
    { at: "2026-10-07T14:30:00+08:00", line: "m_20261007_0102", thread: "t_0ther1", text: "雨停了。" },
  ] };
  const g = await boot({ held, push: { bark: "" }, start: at("2026-10-07T15:00:00") });
  const resp = await g.chat([SYS, { role: "user", content: "我到家了。" }], { stream: false });
  const body = JSON.parse(resp);
  const prefix = "（你不在的时候我说过 · 14:05：下雨了，记得带伞。）\n（你不在的时候我说过 · 14:30：雨停了。）\n\n";
  assert.strictEqual(body.choices[0].message.content, `${prefix}我也想你。`);
  const sent = up.chats()[0].body;
  assert.deepStrictEqual(sent.messages.slice(-3), [
    { role: "assistant", content: "下雨了，记得带伞。" },
    { role: "assistant", content: "雨停了。" },
    { role: "user", content: "我到家了。" },
  ], "both lines as his, in the order he said them, before her line");
  await wait_for(() => day_lines(g.data_root).some((l) => l.role === "assistant"));
  const reply = day_lines(g.data_root).find((l) => l.role === "assistant");
  assert.strictEqual(reply.text, `${prefix}我也想你。`);
  assert.notStrictEqual(reply.thread, "t_0ther1", "shown in the conversation she speaks in");

  // the next turn confirms both
  await g.chat([SYS, { role: "user", content: "我到家了。" }, { role: "assistant", content: `${prefix}我也想你。` },
                { role: "user", content: "晚饭吃什么？" }], { stream: false });
  assert.deepStrictEqual(held_doc(g.data_root).items, []);
});

// ———— push alone: create_push on a fake clock, against the fake Bark server ————

function push_rig({ push, wake = { on: false, dnd: { on: true, from: "23:00", to: "08:00" } }, start = at("2026-10-07T10:00:00"), items = [], bark_base, timeout_ms } = {}) {
  const dir = fresh_dir();
  fs.writeFileSync(path.join(dir, "present.json"), JSON.stringify({ wake, push }));
  fs.writeFileSync(path.join(dir, "held.json"), JSON.stringify({ items }));
  const clock = create_fake_clock(start);
  const settings = create_settings({ file: path.join(dir, "present.json"), env: {} });
  const p = create_push({ data_root: dir, settings, clock, zone: ZONE, log: () => {}, bark_base, timeout_ms });
  return { dir, clock, push: p };
}
const ITEM = { at: "2026-10-07T10:00:00+08:00", line: "m_20261007_0007", thread: "t_aaaaaa", text: "想跟 ta 说晚霞的事。" };

async function beats(rig, until) {
  while (rig.clock.now() < until) { rig.clock.advance(MIN); await rig.push.beat(); }
}

test("push: notice mode sends only the notice; a bare key goes to the Bark base", { timeout: 20000 }, async () => {
  bark.answer(() => ({ status: 200 }));
  const rig = push_rig({ push: { bark: BARK_KEY, text: "notice" }, bark_base: bark.base, items: [ITEM] });
  const r = await rig.push.on_spoke(ITEM);
  assert.deepStrictEqual(r, { ok: true, status: 200, endpoint_redacted: `${bark.base}/••••7Q` });
  assert.deepStrictEqual(bark.got, [{ method: "POST", path: `/${BARK_KEY}`, body: { title: "Loci", body: "ta 说了句话" } }]);
  assert.strictEqual(endpoint_of("abcd1234"), "https://api.day.app/abcd1234", "Bark's own server by default");
  assert.strictEqual(endpoint_of("https://bark.example/k1"), "https://bark.example/k1", "a whole URL as it is");
  // a full text too long for a notification is cut on a character boundary, marked
  const long = "晚".repeat(2000);
  const cut = fit_body(long);
  assert.ok(Buffer.byteLength(cut, "utf8") <= BODY_MAX_BYTES && cut.endsWith("……") && cut.startsWith("晚晚"));
  assert.strictEqual(fit_body("短的。"), "短的。");
});

test("push: nothing in DND; a push the night held back is given up after 15 minutes", { timeout: 20000 }, async () => {
  bark.answer(() => ({ status: 200 }));
  const rig = push_rig({ push: { bark: bark.url }, start: at("2026-10-07T23:30:00"), items: [ITEM] });
  assert.strictEqual(await rig.push.on_spoke(ITEM), null);
  await beats(rig, at("2026-10-07T23:50:00"));
  assert.strictEqual(bark.got.length, 0, "no push inside DND");
  assert.strictEqual(rig.push.status().retrying, 0, "given up");
  const log = read(rig.dir, "logs/present.jsonl");
  assert.ok(log.includes('"status":"skipped_dnd"') && log.includes('"status":"given_up"'));
  assert.deepStrictEqual(held_doc(rig.dir).items.map((i) => i.text), [ITEM.text], "the words stay held");
});

test("push: a failure retries 3 times within 15 minutes then gives up; one success ends it; a confirmed line is dropped", { timeout: 30000 }, async () => {
  // always failing: the first try and three retries, then given up
  bark.answer(() => ({ status: 500, message: `failed to push to ${BARK_KEY}` }));
  let rig = push_rig({ push: { bark: bark.url }, items: [ITEM] });
  const first = await rig.push.on_spoke(ITEM);
  assert.deepStrictEqual([first.ok, first.status], [false, 500]);
  assert.ok(!first.error.includes(BARK_KEY), "the key is masked out of Bark's message");
  const times = [];
  const orig = bark.got.length;
  while (rig.clock.now() < at("2026-10-07T10:30:00")) {
    rig.clock.advance(MIN);
    const before_n = bark.got.length;
    await rig.push.beat();
    if (bark.got.length > before_n) times.push(new Date(rig.clock.now() + 8 * 3600000).toISOString().slice(11, 16));
  }
  assert.strictEqual(orig, 1);
  assert.deepStrictEqual(times, ["10:02", "10:06", "10:12"], "retries at +2, +6 and +12 minutes");
  assert.strictEqual(bark.got.length, 4, "1 + 3 and no more");
  assert.deepStrictEqual(rig.push.health(), { state: "on", last_ok_at: null, last_ok_seconds_ago: null, failures_since_ok: 4,
                                              last_failure_status: 500, retrying: 0 });
  assert.ok(read(rig.dir, "logs/present.jsonl").includes('"status":"given_up"'));
  for (const f of ["logs/present.jsonl", "counters.json"]) assert.ok(!read(rig.dir, f).includes(BARK_KEY), `no key in ${f}`);

  // two failures, then it goes through
  bark.answer((i) => (i <= 2 ? { status: 503 } : { status: 200 }));
  rig = push_rig({ push: { bark: bark.url }, items: [ITEM] });
  await rig.push.on_spoke(ITEM);
  await beats(rig, at("2026-10-07T10:20:00"));
  assert.strictEqual(bark.got.length, 3);
  assert.deepStrictEqual([rig.push.status().retrying, rig.push.health().failures_since_ok], [0, 0]);
  assert.strictEqual(rig.push.health().last_ok_at, "2026-10-07T10:06:00+08:00");

  // she saw it before the retry: not pushed again
  bark.answer(() => ({ status: 500 }));
  rig = push_rig({ push: { bark: bark.url }, items: [ITEM] });
  await rig.push.on_spoke(ITEM);
  fs.writeFileSync(path.join(rig.dir, "held.json"), JSON.stringify({ items: [] }));
  await beats(rig, at("2026-10-07T10:20:00"));
  assert.strictEqual(bark.got.length, 1, "only the first try");
  assert.strictEqual(rig.push.status().retrying, 0);
});

test("push: a Bark that hangs times out, said as Lento's bark.js says it; errors never carry the key", { timeout: 20000 }, async () => {
  bark.answer(() => ({ hang: true }));
  const rig = push_rig({ push: { bark: bark.url }, items: [ITEM], timeout_ms: 150 });
  const r = await rig.push.test();
  bark.release();
  assert.deepStrictEqual(r, { ok: false, status: "error", error: "timeout", endpoint_redacted: `${bark.base}/••••7Q` });
  assert.strictEqual(scrub(`connect to ${bark.url}/x?token=abcdef failed`, `${bark.url}?token=abcdef`).includes(BARK_KEY), false);
  assert.ok(!scrub(`bad token abcdef`, `${bark.url}?token=abcdef`).includes("abcdef"));
});

test("POST /present/push-test answers Bark's outcome; /present and /health show the last push, never the key", { timeout: 20000 }, async () => {
  bark.answer(() => ({ status: 200 }));
  const g = await boot({ start: at("2026-10-07T09:00:00") });
  const r = await g.call("POST", "/present/push-test", {});
  assert.strictEqual(r.status, 200);
  assert.deepStrictEqual(r.json, { ok: true, status: 200, endpoint_redacted: `${bark.base}/••••7Q` });
  assert.deepStrictEqual(bark.got[0].body, { title: "Loci", body: "这是一条试推。收到就说明推送通了。" });
  g.clock.advance(90 * 1000);
  const s = await g.call("GET", "/present");
  assert.deepStrictEqual(s.json.status.push, { state: "on", last: { at: "2026-10-07T09:00:00+08:00", ok: true, status: 200 }, retrying: 0 });
  const { build_present_health } = require("../present/health_section.js");
  const h = build_present_health({ present: g.present, doors: { token_set: true, bind: "127.0.0.1", passphrase_required: false } });
  assert.deepStrictEqual(h.push, { state: "on", last_ok_at: "2026-10-07T09:00:00+08:00", last_ok_seconds_ago: 90,
                                   failures_since_ok: 0, last_failure_status: null, retrying: 0 });
  for (const text of [r.text, s.text, JSON.stringify(h)]) assert.ok(!text.includes(BARK_KEY), "the key is never answered");

  // Bark refusing: ok false with its status
  bark.answer(() => ({ status: 400, message: `device ${BARK_KEY} not found` }));
  const bad = await g.call("POST", "/present/push-test", {});
  assert.deepStrictEqual([bad.status, bad.json.ok, bad.json.status], [200, false, 400]);
  assert.ok(!bad.text.includes(BARK_KEY));
});

test("reconciliation: every outbound connection stayed inside this run", () => {
  const ours = new Set([up.port, loci.port, bark.port, ...servers.map((s) => s.address()?.port).filter(Boolean)]);
  const strays = fence.账本.filter((e) => !ours.has(e.端口));
  assert.deepStrictEqual(strays, []);
});
