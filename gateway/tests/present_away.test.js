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
//   · every held line is an item with its local time, a blank line between items, then
//     the boundary line; lines held on another thread are prefixed in the thread she
//     speaks in; a plain JSON answer gets the prefix in front of its text
//   · wake.away_prefix off: no prefix; the words go upstream before her line on that turn
//     and every later one, confirmed once she goes on, through a regenerate, a fork, a
//     flip (the window and the wake snapshot keep them) and a pack (it reads them as his
//     line); switching it between the showing turn and the next neither loses nor
//     doubles them
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
const win = require("../present/window.js");
const { stretch_lines, format_line } = require("../present/pack.js");
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

/** The prefix block as the client sees it, from [HH:MM, words] pairs. */
const prefix_of = (...items) => items.map(([hm, text]) => `— 你不在的时候 · ${hm} —\n${text}`).join("\n\n") + "\n\n————\n\n";

// ———————————————————————————————————————————————

test("the prefix block: every item with its local time, a blank line between, then the boundary", () => {
  assert.strictEqual(prefix_block([{ at: "2026-10-07T14:05:00+08:00", text: " 想你了。\n" }], ZONE),
    "— 你不在的时候 · 14:05 —\n想你了。\n\n————\n\n", "one item has its time too");
  assert.strictEqual(prefix_block([
    { at: "2026-10-07T14:05:00+08:00", text: "想你了。\n\n第二段。" },
    { at: "2026-10-07T06:30:00Z", text: "晚霞很好看。" },
  ], ZONE), "— 你不在的时候 · 14:05 —\n想你了。\n\n第二段。\n\n— 你不在的时候 · 14:30 —\n晚霞很好看。\n\n————\n\n");
  assert.strictEqual(prefix_of(["14:05", "想你了。"]), "— 你不在的时候 · 14:05 —\n想你了。\n\n————\n\n", "the helper below says the same");
  assert.strictEqual(prefix_block([], ZONE), "");
});

test("the prefix in the stream: comments pass first, the chunk copies upstream's first chunk, every byte after as sent, however cut", async () => {
  const first = { id: "c1", object: "chat.completion.chunk", created: 5, model: "m", system_fingerprint: "fp", choices: [{ index: 0, delta: { role: "assistant" } }] };
  const body = Buffer.from(`: keep-alive\r\n\r\ndata: ${JSON.stringify(first)}\r\n\r\n`
    + `data: {"id":"c1","choices":[{"index":0,"delta":{"content":"嗯"}}]}\r\n\r\ndata: [DONE]\r\n\r\n`, "utf8");
  const prefix = prefix_of(["09:00", "在的。"]);
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
  const prefix = prefix_of(["11:00", X]);
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
  const prefix = prefix_of(["11:00", X]);

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
  assert.strictEqual(sse_text(await g.say(t3)), `${prefix_of(["11:20", "又想起一件事。"])}嗯，我在听。`);
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
  const prefix = prefix_of(["14:05", "下雨了，记得带伞。"], ["14:30", "雨停了。"]);
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

// ———— wake.away_prefix off, and switching it ————

const u = (content) => ({ role: "user", content });
const a = (content) => ({ role: "assistant", content });
const HER_BACK = "我回来了。";

/** present.json rewritten with the switch as `on` (and any other sections given). */
const set_switch = (g, on, more = {}) => fs.writeFileSync(path.join(g.data_root, "present.json"),
  JSON.stringify({ wake: { ...WAKE_ON, away_prefix: on }, push: { bark: "" }, ...more }));
/** The message right before her line `text` in a request, or null. */
const before_her = (body, text) => {
  const i = body.messages.findIndex((m) => m.role === "user" && m.content === text);
  return i > 0 ? body.messages[i - 1] : null;
};
/** How many messages of a request are exactly `text`. */
const count_of = (body, text) => body.messages.filter((m) => m.content === text).length;
const last_chat = () => up.chats().slice(-1)[0].body;
const away_overlays = (g) => g.present.threads.list().flatMap((t) => (t.window?.overlays || []).filter((o) => o.kind === "away"));

/** Boot with the switch as `on`; her line, a wake that says X at 11:00, then her 我回来了。 answered 回来啦。 */
async function shown_once(on) {
  const state = { answer: "晚安。" };
  up.plan((body, { wake }) => ({ text: wake ? X : state.answer }));
  const g = await boot({ wake: { ...WAKE_ON, away_prefix: on }, push: { bark: "" } });
  const t1 = [SYS, u("晚安。")];
  await g.say(t1);
  await run_until(g, at("2026-10-07T11:00:00"));
  assert.deepStrictEqual(held_doc(g.data_root).items.map((i) => i.text), [X]);
  g.clock.advance(10 * MIN);
  const t2 = [...t1, a("晚安。"), u(HER_BACK)];
  state.answer = "回来啦。";
  const text2 = sse_text(await g.say(t2));
  return { g, state, t2, text2 };
}

test("switch off: no prefix; his words go upstream before her line on that turn and every later one; confirmed once she goes on; regenerates keep them", { timeout: 30000 }, async () => {
  const { g, state, t2, text2 } = await shown_once(false);
  assert.strictEqual(text2, "回来啦。", "no prefix: the answer as upstream wrote it");
  assert.deepStrictEqual(before_her(last_chat(), HER_BACK), a(X), "X goes upstream as his, right before her line");
  const reply2 = day_lines(g.data_root).filter((l) => l.role === "assistant" && !l.woke).pop();
  assert.strictEqual(reply2.text, "回来啦。", "the day store keeps what the client saw");
  assert.deepStrictEqual(held_doc(g.data_root).items.map((i) => [i.text, i.shown.prefixed]), [[X, false]]);

  // a regenerate of that turn, past the resend window: still no prefix, X once, in its place
  g.clock.advance(3 * MIN);
  state.answer = "回来啦，累不累？";
  assert.strictEqual(sse_text(await g.say(t2)), "回来啦，累不累？");
  assert.deepStrictEqual(before_her(last_chat(), HER_BACK), a(X));
  assert.strictEqual(count_of(last_chat(), X), 1);
  assert.strictEqual(held_doc(g.data_root).items.length, 1, "not confirmed before she goes on");

  // she goes on: confirmed, out of held.json; X stays in front of 我回来了。
  g.clock.advance(MIN);
  state.answer = "好看，像橘子汽水一样，我还拍了一张给你看。";
  const t3 = [...t2, a("回来啦，累不累？"), u("晚霞好看吗？")];
  assert.strictEqual(sse_text(await g.say(t3)), "好看，像橘子汽水一样，我还拍了一张给你看。");
  const doc = held_doc(g.data_root);
  assert.deepStrictEqual(doc.items, [], "held.json is empty: no pile-up against held_cap");
  assert.deepStrictEqual(doc.given.flatMap((e) => e.items.map((i) => [i.text, i.prefixed])), [[X, false]]);
  assert.strictEqual(g.present.wake.status().held, 0);
  assert.deepStrictEqual(before_her(last_chat(), HER_BACK), a(X));
  assert.strictEqual(count_of(last_chat(), X), 1);
  assert.deepStrictEqual(away_overlays(g).map((o) => [o.text, o.lasting]), [[X, true]]);

  // and on the turn after that
  g.clock.advance(MIN);
  state.answer = "嗯嗯，今天的晚霞真的很少见，红了很久。";
  const t4 = [...t3, a("好看，像橘子汽水一样，我还拍了一张给你看。"), u("嗯。")];
  await g.say(t4);
  assert.deepStrictEqual(before_her(last_chat(), HER_BACK), a(X));
  assert.strictEqual(count_of(last_chat(), X), 1);

  // she rewinds to 我回来了。 and regenerates (a fork): X still there, once, no prefix
  g.clock.advance(MIN);
  state.answer = "又一版。";
  assert.strictEqual(sse_text(await g.say(t2)), "又一版。");
  assert.deepStrictEqual(before_her(last_chat(), HER_BACK), a(X));
  assert.strictEqual(count_of(last_chat(), X), 1);

  // she goes on from her latest line on the first branch: confirmed again, nothing left held
  g.clock.advance(MIN);
  state.answer = "睡吧。";
  await g.say([...t4, a("嗯嗯，今天的晚霞真的很少见，红了很久。"), u("困了。")]);
  assert.deepStrictEqual(held_doc(g.data_root).items, []);
  assert.deepStrictEqual(before_her(last_chat(), HER_BACK), a(X));
  assert.strictEqual(count_of(last_chat(), X), 1);
});

test("switch off: a flip keeps his words while her line is raw (window and wake snapshot); a pack that folds her line reads them as his", { timeout: 30000 }, async () => {
  const { g, state, t2 } = await shown_once(false);
  g.clock.advance(MIN);
  state.answer = "好看。";
  const t3 = [...t2, a("回来啦。"), u("晚霞好看吗？")];
  await g.say(t3);
  const th = g.present.threads.list().find((t) => t.window && t.window.overlays.some((o) => o.kind === "away"));
  const anchor = th.window.overlays.find((o) => o.kind === "away").anchor;
  assert.strictEqual(g.present.day_store.get(anchor).text, HER_BACK);

  // the stretch a pack reads: X as his line, said while she was away, right before her line
  const read = stretch_lines(th.branch.map((e) => e.id), th.window, (id) => g.present.day_store.get(id)).map(format_line);
  const at_her = read.findIndex((l) => l.endsWith(`ta：${HER_BACK}`));
  assert.strictEqual(read[at_her - 1], `[10-07 11:00] 你（ta 不在的时候说的）：${X}`);

  // a flip with her line still raw: the overlay comes along, and the wake snapshot has it
  win.open_next(th, { mark: anchor, carry: "〔前情〕ta 去上课了。" }, g.clock.now());
  g.present.threads.save(th.id);
  assert.deepStrictEqual(th.window.overlays.map((o) => [o.kind, o.text, o.lasting]), [["away", X, true]]);
  assert.ok(g.present.wake.rebase(th.id));
  const snap = g.present.threads.get(th.id).last_sent;
  assert.deepStrictEqual(before_her(snap.request, HER_BACK), a(X), "the snapshot holds X where the next turn will");
  for (const [i, id] of snap.layout.lines) assert.notStrictEqual(snap.request.messages[i].content, X, `layout line ${id} is a client message`);
  g.clock.advance(MIN);
  state.answer = "嗯。";
  const t4 = [...t3, a("好看。"), u("嗯。")];
  await g.say(t4);
  assert.deepStrictEqual(before_her(last_chat(), HER_BACK), a(X));
  assert.strictEqual(count_of(last_chat(), X), 1);
  assert.ok(last_chat().messages.some((m) => m.role === "system" && m.content === "〔前情〕ta 去上课了。"));
  assert.ok(snap.request.messages.every((m, i) => JSON.stringify(m) === JSON.stringify(last_chat().messages[i])),
    "the next turn starts with the rebuilt snapshot's exact messages");

  // 现在压 with one raw line kept: her line is folded, the pack's letter reads X as his
  set_switch(g, false, { compress: { keep_raw: 1 } });
  g.clock.advance(MIN);
  state.answer = "ta 回来了，我说过看见晚霞。";
  const sent_before = up.chats().length;
  assert.strictEqual(g.present.compress_now(th.id).body.queued, true);
  await wait_for(() => g.present.threads.get(th.id).window.no === 3);
  const pack = up.chats().slice(sent_before).map((c) => c.body).find((b) => String(b.messages.at(-1).content).includes("ta 不在的时候说的"));
  assert.ok(pack, "the pack went upstream");
  assert.ok(pack.messages.at(-1).content.includes(`你（ta 不在的时候说的）：${X}`));
  assert.deepStrictEqual(g.present.threads.get(th.id).window.overlays.filter((o) => o.kind === "away"), [],
    "its anchor folded away, the overlay is gone; the summary carries it now");
  assert.ok(g.present.threads.get(th.id).window.carry.includes("ta 回来了，我说过看见晚霞。"));
});

test("switching between the showing turn and the next: nothing lost, nothing doubled", { timeout: 40000 }, async () => {
  // shown with the prefix, switched off, the turn regenerated without it: the window keeps X
  {
    const { g, state, t2, text2 } = await shown_once(true);
    assert.strictEqual(text2, `${prefix_of(["11:00", X])}回来啦。`);
    set_switch(g, false);
    g.clock.advance(3 * MIN);
    state.answer = "重新来。";
    assert.strictEqual(sse_text(await g.say(t2)), "重新来。", "the same turn, shown the way the switch says now");
    g.clock.advance(MIN);
    state.answer = "好。";
    await g.say([...t2, a("重新来。"), u("嗯。")]);
    assert.deepStrictEqual(held_doc(g.data_root).items, []);
    assert.deepStrictEqual(before_her(last_chat(), HER_BACK), a(X));
    assert.strictEqual(count_of(last_chat(), X), 1);
    assert.ok(!last_chat().messages.some((m) => String(m.content).includes("你不在的时候")));
  }
  // shown without it, switched on before she goes on: no prefix after the fact, the window keeps X
  {
    const { g, state, t2 } = await shown_once(false);
    set_switch(g, true);
    g.clock.advance(MIN);
    state.answer = "好。";
    assert.strictEqual(sse_text(await g.say([...t2, a("回来啦。"), u("嗯。")])), "好。");
    assert.deepStrictEqual(held_doc(g.data_root).items, []);
    assert.deepStrictEqual(before_her(last_chat(), HER_BACK), a(X));
    assert.strictEqual(count_of(last_chat(), X), 1);
  }
  // shown with the prefix, switched off, and her history carries the prefixed reply: the history has X, the overlay goes
  {
    const { g, state, t2, text2 } = await shown_once(true);
    set_switch(g, false);
    g.clock.advance(MIN);
    state.answer = "好。";
    assert.strictEqual(sse_text(await g.say([...t2, a(text2), u("嗯。")])), "好。");
    assert.deepStrictEqual(held_doc(g.data_root).items, []);
    assert.strictEqual(count_of(last_chat(), X), 0, "X is only inside his prefixed reply");
    assert.ok(last_chat().messages.some((m) => m.role === "assistant" && m.content === text2));
    assert.deepStrictEqual(away_overlays(g), []);
  }
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
  assert.deepStrictEqual(r, { ok: false, status: "error", error: "超时：Bark 没回", endpoint_redacted: `${bark.base}/••••7Q` });
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
