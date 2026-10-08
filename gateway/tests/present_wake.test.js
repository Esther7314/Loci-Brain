// ============================================================
// gateway/tests/present_wake.test.js — wake (present/wake.js) and DND (present/dnd.js)
//
// In-process, behind the network fence, on a fake clock: the real relay and the real
// present layer in front of a scripted fake upstream (chat answers, wake answers, HTTP
// errors, a wake that hangs) and a fake Loci (cue, poke with a dream, dream/wake). The
// heartbeat is not started; each test drives wake.tick() minute by minute itself.
// Temp directories only. Fixtures say 「ta」.
//
// What it holds the step to (blueprint §六 row 11):
//   · a fake-clock day: no wake inside DND, the day stops at daily_cap, held_cap null
//     means no limit, a shut gate writes one line per reason change
//   · paid failures back off ×2, ×4 and no further, count toward the cap, and one
//     success resets them; unpaid failures are not counted (connect still backs off)
//   · her request mid-wake aborts it, and nothing is written
//   · dry_run makes no upstream call
//   · the wake request's prefix is the last chat request byte for byte, tools included,
//     and each wake's request is the next one's prefix
//   · the letter never reaches the day store, held.json, the client or a status reply
//   · the same dream is handed over once; a failed wake leaves it for the next
//   · after a restart: no borrowed key, no wake; a start stamp keeps a restarted gateway
//     with a fixed key from waking at once
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
const { create_wake } = require("../present/wake.js");
const { create_settings } = require("../present/settings.js");
const { create_prompts, CARDS } = require("../present/prompts.js");
const { dnd_state, in_span, minute_of_day } = require("../present/dnd.js");

const ZONE = "Asia/Shanghai";
const KEY = "sk-wake-test-SENTINEL-5b2e";
const LETTER_MARK = "【自由时间";
const CARD_LINE = CARDS.wake.text.split("\n")[0];
const at = (local) => Date.parse(`${local}+08:00`);
const MIN = 60 * 1000;

const root = fs.mkdtempSync(path.join(os.tmpdir(), "loci-wake-"));
let n = 0;
const fresh_dir = () => { const d = path.join(root, `d${++n}`); fs.mkdirSync(d, { recursive: true }); return d; };

const is_wake = (body) => {
  const last = body?.messages?.[body.messages.length - 1];
  return typeof last?.content === "string" && last.content.startsWith(LETTER_MARK);
};

// ———— fake upstream: plan(body, { wake, i }) → { text, status, hang } ————
function start_upstream() {
  const got = [];
  const open = new Set();
  let plan = () => ({ text: "嗯。" });
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      if (req.method === "GET") {
        res.writeHead(200, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ object: "list", data: [] }));
      }
      const raw = Buffer.concat(chunks).toString("utf8");
      const body = JSON.parse(raw || "null");
      const wake = is_wake(body);
      got.push({ wake, body, raw, headers: { ...req.headers } });
      const p = plan(body, { wake, i: got.filter((g) => g.wake === wake).length }) || {};
      if (p.hang) { open.add(res); return; }
      if (p.status && p.status !== 200) {
        res.writeHead(p.status, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ error: { message: "scripted failure" } }));
      }
      const text = String(p.text ?? "");
      const events = [{ choices: [{ index: 0, delta: { role: "assistant" } }] }];
      for (let i = 0; i < text.length; i += 3) events.push({ choices: [{ index: 0, delta: { content: text.slice(i, i + 3) } }] });
      events.push({ choices: [{ index: 0, delta: {}, finish_reason: "stop" }] });
      events.push({ choices: [], usage: { prompt_tokens: 900, completion_tokens: 9, total_tokens: 909 } });
      res.writeHead(200, { "Content-Type": "text/event-stream" });
      res.end(events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("") + "data: [DONE]\n\n");
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({
    port: server.address().port,
    url: `http://127.0.0.1:${server.address().port}/v1`,
    got,
    wakes: () => got.filter((g) => g.wake),
    chats: () => got.filter((g) => !g.wake),
    plan(fn) { plan = fn; got.length = 0; },
    release() { for (const r of open) { try { r.destroy(); } catch { /* gone */ } } open.clear(); },
    async close() { this.release(); server.closeAllConnections?.(); await new Promise((r) => server.close(r)); },
  })));
}

// ———— fake Loci: cue (no cards), poke (a scripted dream / muse count), dream/wake booked,
//      and an MCP face that lists one tool (what own_turn offers for tools: "loci") ————
const LOCI_TOOLS = [{ name: "recall", description: "找回记忆", inputSchema: { type: "object", properties: { query: { type: "string" } } } }];
function start_loci() {
  const dream_wakes = [];
  const pokes = [];
  let poke_reply = { dreams: [], muse_pending: 0 };
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
          res.end(`event: message
data: ${JSON.stringify(obj)}

`);
        };
        if (body?.method === "initialize") return sse({ jsonrpc: "2.0", id: body.id, result: { protocolVersion: "2024-11-05", capabilities: {} } }, "s1");
        if (body?.method === "notifications/initialized") { res.writeHead(202); return res.end(); }
        if (body?.method === "tools/list") return sse({ jsonrpc: "2.0", id: body.id, result: { tools: LOCI_TOOLS } });
        res.writeHead(400); return res.end();
      }
      if (route === "/api/v2/cue") return json({ cards: [], text: "" });
      if (route.startsWith("/api/v2/cue/")) return json({ ok: true });
      if (route === "/api/loci/poke") { pokes.push(Date.now()); return json(poke_reply); }
      if (route === "/api/loci/dream/wake") { dream_wakes.push(Date.now()); return json({}); }
      res.writeHead(404); res.end();
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({
    port: server.address().port,
    url: `http://127.0.0.1:${server.address().port}/mcp`,
    dream_wakes, pokes,
    poke(reply) { poke_reply = reply; },
    reset() { dream_wakes.length = 0; pokes.length = 0; poke_reply = { dreams: [], muse_pending: 0 }; },
    async close() { server.closeAllConnections?.(); await new Promise((r) => server.close(r)); },
  })));
}

let up, loci;
const servers = [];
before(async () => {
  up = await start_upstream();
  loci = await start_loci();
  fence.allow(up.port, loci.port);
});
after(async () => {
  for (const s of servers) { s.closeAllConnections?.(); await new Promise((r) => s.close(r)); }
  await up.close();
  await loci.close();
  fs.rmSync(root, { recursive: true, force: true });
});

const WAKE_ON = { on: true, every_min: 60, dnd: { on: true, from: "23:00", to: "08:00" }, daily_cap: 16 };

/** The real present layer and the real relay, in this process, on a fake clock. */
async function boot({ wake = WAKE_ON, start = at("2026-10-07T10:00:00"), env = {}, data_root = fresh_dir(), clock = null } = {}) {
  fs.mkdirSync(path.join(data_root, "state"), { recursive: true });
  // poke delivery's idle gate stays shut on the chat path (its clock is the real one)
  fs.writeFileSync(path.join(data_root, "state", "poke-window.json"),
    JSON.stringify({ lastUserMessageTime: new Date().toISOString(), wakePending: false }));
  if (wake !== null) fs.writeFileSync(path.join(data_root, "present.json"), JSON.stringify({ wake }));
  const c = clock || create_fake_clock(start);
  const present = create_present({ env: { LOCI_TZ: ZONE, ...env }, data_root, clock: c, upstream: up.url,
                                   loci: loci.url, log: () => {} });
  const relay = create_relay({ upstream: up.url, present });
  const server = http.createServer((req, res) => relay(req, res, { start: Date.now() }));
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  servers.push(server);
  fence.allow(server.address().port);
  const url = `http://127.0.0.1:${server.address().port}/v1/chat/completions`;

  async function chat(messages, extra = {}) {
    const resp = await fetch(url, { method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${KEY}` },
      body: JSON.stringify({ model: "her-model", stream: true, messages, ...extra }) });
    const text = await resp.text();
    return { status: resp.status, text };
  }
  /** Her turn, and wait until its answer has become the thread's snapshot. */
  async function say(messages, extra = {}) {
    const before_at = present.threads.list().map((t) => t.last_sent?.at || 0);
    const r = await chat(messages, extra);
    assert.strictEqual(r.status, 200, r.text);
    await wait_for(() => present.threads.list().some((t) => t.last_sent && !before_at.includes(t.last_sent.at)
      && t.last_sent.reply));
    return r;
  }
  return { present, wake: present.wake, clock: c, data_root, chat, say };
}

const wait_for = async (cond, ms = 3000) => {
  const end = Date.now() + ms;
  while (!cond()) { if (Date.now() > end) throw new Error("timed out waiting"); await new Promise((r) => setTimeout(r, 5)); }
};

/** Tick once a minute until `until`; returns every tick that did something. */
async function run_until(g, until) {
  const done = [];
  while (g.clock.now() < until) {
    g.clock.advance(MIN);
    const r = await g.wake.tick();
    if (r.ran || r.result) done.push({ at: g.clock.now(), ...r });
  }
  return done;
}

const log_lines = (dir) => {
  const f = path.join(dir, "logs", "present.jsonl");
  return fs.existsSync(f) ? fs.readFileSync(f, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
};
const day_text = (dir) => {
  const d = path.join(dir, "host", "days");
  return fs.existsSync(d) ? fs.readdirSync(d).map((f) => fs.readFileSync(path.join(d, f), "utf8")).join("") : "";
};
const day_lines = (dir) => day_text(dir).trim().split("\n").filter(Boolean).map((l) => JSON.parse(l));
const held_items = (dir) => {
  const f = path.join(dir, "held.json");
  return fs.existsSync(f) ? JSON.parse(fs.readFileSync(f, "utf8")).items : [];
};
const local_hhmm = (ms) => new Date(ms + 8 * 3600 * 1000).toISOString().slice(11, 16);

const HELLO = [{ role: "system", content: "你是 ta 的伴。" }, { role: "user", content: "我去上课了，晚点聊。" }];

// ———————————————————————————————————————————————

test("dnd: by the minute, wrapping midnight, start inside and end outside; unreadable means do not wake", () => {
  const w = { dnd: { on: true, from: "23:00", to: "08:00" } };
  const q = (local) => dnd_state(w, at(local), ZONE);
  assert.strictEqual(q("2026-10-07T22:59:00").dnd, false);
  assert.deepStrictEqual(q("2026-10-07T23:00:00"), { dnd: true, reason: "dnd" }, "the start is inside");
  assert.strictEqual(q("2026-10-08T03:00:00").dnd, true, "across midnight");
  assert.strictEqual(q("2026-10-08T07:59:59").dnd, true);
  assert.strictEqual(q("2026-10-08T08:00:00").dnd, false, "the end is outside");
  const day = { dnd: { on: true, from: "13:30", to: "14:15" } };
  assert.strictEqual(dnd_state(day, at("2026-10-07T13:29:00"), ZONE).dnd, false);
  assert.strictEqual(dnd_state(day, at("2026-10-07T13:30:00"), ZONE).dnd, true, "minutes count, not whole hours");
  assert.strictEqual(dnd_state(day, at("2026-10-07T14:15:00"), ZONE).dnd, false);
  assert.deepStrictEqual(dnd_state({ dnd: { on: false, from: "00:00", to: "23:59" } }, at("2026-10-07T03:00:00"), ZONE),
    { dnd: false, reason: null });
  assert.deepStrictEqual(dnd_state(null, at("2026-10-07T12:00:00"), ZONE), { dnd: true, reason: "settings_unreadable" },
    "settings that cannot be read: do not wake (the opposite of Lento)");
  assert.deepStrictEqual(dnd_state({ dnd: { on: true, from: "25:00", to: "08:00" } }, at("2026-10-07T12:00:00"), ZONE),
    { dnd: true, reason: "bad_span" });
  assert.strictEqual(in_span(minute_of_day("09:00"), minute_of_day("09:00"), minute_of_day("09:00")), false, "an empty span");
});

test("a fake-clock day: none inside DND, stops at the cap, held_cap null is no limit, gate lines deduplicated", { timeout: 60000 }, async () => {
  up.plan((body, { wake }) => ({ text: wake ? "刚才想到你了。" : "好，去吧。" }));
  const g = await boot({ wake: { ...WAKE_ON, daily_cap: 5 } });
  await g.say(HELLO);

  const first_day = await run_until(g, at("2026-10-07T16:00:00"));
  assert.deepStrictEqual(first_day.map((d) => local_hhmm(d.at)), ["11:00", "12:00", "13:00", "14:00", "15:00"],
    "every_min after she went quiet, then every_min after each wake, five and no more");
  assert.ok(first_day.every((d) => d.result === "spoke"));
  const s = g.wake.status();
  assert.strictEqual(s.next_why, "cap");
  assert.strictEqual(s.next_why_words, "今天叫的次数到上限了，明天再叫");
  assert.strictEqual(s.next_at, "2026-10-08T08:00:00+08:00", "tomorrow, once DND is over");
  assert.deepStrictEqual(s.today, { woke: 5, spoke: 5, paid_failures: 0, dry_run: 0 });
  assert.strictEqual(s.held, 5, "held_cap null: no limit on what he says unanswered");
  assert.deepStrictEqual([s.last.result, s.last.result_words], ["spoke", "说了话"]);

  const night = await run_until(g, at("2026-10-08T12:00:00"));
  assert.deepStrictEqual(night.map((d) => local_hhmm(d.at)), ["08:00", "09:00", "10:00", "11:00", "12:00"]);
  for (const d of [...first_day, ...night]) {
    const minute = Number(local_hhmm(d.at).slice(0, 2)) * 60 + Number(local_hhmm(d.at).slice(3));
    assert.ok(!(minute >= 23 * 60 || minute < 8 * 60), `no wake inside DND (${local_hhmm(d.at)})`);
  }
  assert.strictEqual(up.wakes().length, 10, "every wake that ran went upstream once");

  // a shut gate wrote one line per change of reason, never two in a row with the same reason
  const gates = log_lines(g.data_root).filter((l) => l.event === "wake_gate");
  assert.ok(gates.length < 60, `${gates.length} gate lines for ${26 * 60} ticks`);
  const shut = [];
  for (const l of log_lines(g.data_root)) {
    if (l.event === "wake_gate") { assert.notStrictEqual(shut[shut.length - 1], l.blocked, "deduplicated"); shut.push(l.blocked); }
    if (l.event === "wake") shut.push(null);
  }
  assert.ok(gates.some((l) => l.blocked === "cap") && gates.some((l) => l.blocked === "dnd"));
  // what he said is a woke line in the day store and held for her, the letter is neither
  const woke = day_lines(g.data_root).filter((l) => l.woke);
  assert.strictEqual(woke.length, 10);
  assert.ok(woke.every((l) => l.role === "assistant" && l.text === "刚才想到你了。"));
  assert.deepStrictEqual(held_items(g.data_root).map((h) => h.line), woke.map((l) => l.id));
});

test("paid failures back off ×2, ×4 and no further, take places under the cap; one success resets", { timeout: 60000 }, async () => {
  let fails = 3;
  up.plan((body, { wake }) => (wake && fails-- > 0 ? { status: 500 } : { text: wake ? "" : "嗯，去吧。" }));
  const g = await boot({ wake: { ...WAKE_ON, dnd: { on: false, from: "23:00", to: "08:00" }, daily_cap: 6 } });
  await g.say(HELLO);

  const seen = await run_until(g, at("2026-10-07T11:30:00"));
  assert.deepStrictEqual(seen.map((d) => [local_hhmm(d.at), d.result]), [["11:00", "paid_failure"]]);
  let s = g.wake.status();
  assert.deepStrictEqual([s.next_why, s.next_at], ["backoff", "2026-10-07T13:00:00+08:00"], "×2 after one paid failure");
  assert.strictEqual(g.present.wake.health().failures_since_ok, 1);

  seen.push(...await run_until(g, at("2026-10-07T23:59:00")));
  assert.deepStrictEqual(seen.map((d) => [local_hhmm(d.at), d.result]), [
    ["11:00", "paid_failure"], ["13:00", "paid_failure"], ["17:00", "paid_failure"],   // ×2, ×4
    ["21:00", "silent"],                                                                // ×4 is the ceiling
    ["22:00", "silent"],                                                                // reset: every_min again
    ["23:00", "silent"],                                                                // 3 paid + 3 ran = the cap of 6
  ]);
  assert.strictEqual((await g.wake.tick()).why, "cap", "23:59: the day is used up");
  s = g.wake.status();
  assert.deepStrictEqual(s.today, { woke: 3, spoke: 0, paid_failures: 3, dry_run: 0 });
  assert.deepStrictEqual([s.next_why, s.next_at], [null, "2026-10-08T00:00:00+08:00"], "a new day, a new cap");
  assert.strictEqual(s.held, 0, "silence holds nothing");
  const h = g.present.wake.health();
  assert.deepStrictEqual([h.failures_since_ok, h.last_ok_at], [0, "2026-10-07T23:00:00+08:00"]);
  assert.strictEqual(day_lines(g.data_root).filter((l) => l.woke).length, 0, "a silent wake writes no line");
});

test("unpaid failures are not counted; an unreachable upstream still backs off", async () => {
  const dir = fresh_dir();
  const clock = create_fake_clock(at("2026-10-07T10:00:00"));
  fs.writeFileSync(path.join(dir, "present.json"), JSON.stringify({ wake: { ...WAKE_ON, dnd: { on: false, from: "23:00", to: "08:00" }, daily_cap: 2 } }));
  const thread = { id: "t_aaaaaa", last_at: clock.now(),
    last_sent: { at: 1, request: { model: "m", messages: [{ role: "user", content: "在吗" }] }, reply: "在。", wakes: [] } };
  const outcomes = [{ outcome: "unpaid", reason: "connect" }, { outcome: "unpaid", reason: "connect" },
                    { outcome: "unpaid", reason: "no_model" }, { outcome: "paid", reason: "http_500" }];
  const runs = [];
  const own_turn = {
    status: () => ({ key: "borrowed", running: null }), busy: () => false, abort: () => false,
    run: async (args) => { runs.push({ at: clock.now(), args }); return { text: "", silent: true, run: "r", ...outcomes.shift() }; },
  };
  const wake = create_wake({
    data_root: dir, clock, zone: ZONE, own_turn, log: () => {},
    threads: { list: () => [thread], get: () => thread, save: () => true },
    day_store: { append_new: () => { throw new Error("nothing should be written"); } },
    settings: create_settings({ file: path.join(dir, "present.json"), env: {} }),
    prompts: create_prompts({ file: path.join(dir, "prompts.json"), clock, zone: ZONE }),
    fetch_poke: async () => ({ dreams: [], muse_pending: 0 }),
  });
  const g = { clock, wake };
  const seen = await run_until(g, at("2026-10-07T22:00:00"));
  assert.deepStrictEqual(seen.map((d) => [local_hhmm(d.at), d.result, d.reason]), [
    ["11:00", "unpaid_failure", "connect"],
    ["13:00", "unpaid_failure", "connect"],    // backed off ×2
    ["17:00", "unpaid_failure", "no_model"],   // ×4
    ["21:00", "paid_failure", "http_500"],     // still ×4: no_model neither backs off further nor resets
  ]);
  const s = wake.status();
  assert.deepStrictEqual(s.today, { woke: 0, spoke: 0, paid_failures: 1, dry_run: 0 }, "only the paid one is counted");
  assert.deepStrictEqual([s.next_why, s.next_at], ["backoff", "2026-10-08T01:00:00+08:00"],
    "the cap of 2 is not reached by unpaid failures");
  assert.strictEqual(runs[0].args.kind, "wake");
});

test("prefix: the last chat request byte for byte, tools included; each wake is the next one's prefix; the letter stays private", { timeout: 30000 }, async () => {
  const tools = [{ type: "function", function: { name: "loci__recall", description: "找回记忆",
    parameters: { type: "object", properties: { query: { type: "string" } } } } }];
  let w = 0;
  up.plan((body, { wake }) => ({ text: wake ? `醒着的第${++w}句。` : "好呀，等你下课。" }));
  const g = await boot();
  await g.say(HELLO, { tools, temperature: 0.3, tool_choice: "auto" });
  const chat = up.chats()[0].body;

  await run_until(g, at("2026-10-07T12:00:00"));
  const [w1, w2] = up.wakes().map((x) => x.body);
  assert.ok(w1 && w2, "two wakes went upstream");

  const k = chat.messages.length;
  assert.strictEqual(JSON.stringify(w1.messages.slice(0, k)), JSON.stringify(chat.messages), "the chat request's messages, byte for byte");
  assert.strictEqual(JSON.stringify(w1.tools), JSON.stringify(chat.tools), "the same tools, byte for byte");
  assert.deepStrictEqual([w1.model, w1.temperature, w1.tool_choice], [chat.model, chat.temperature, chat.tool_choice]);
  assert.deepStrictEqual(w1.messages[k], { role: "assistant", content: "好呀，等你下课。" }, "then his reply");
  assert.strictEqual(w1.messages.length, k + 2);
  const letter = w1.messages[k + 1];
  assert.strictEqual(letter.role, "user");
  assert.ok(letter.content.startsWith(LETTER_MARK) && letter.content.includes(CARD_LINE), "the letter: the shell and the wake card");
  assert.ok(!/\d/.test(letter.content), "no number in the letter");

  assert.strictEqual(JSON.stringify(w2.messages.slice(0, w1.messages.length)), JSON.stringify(w1.messages),
    "the first wake's whole request is the second's prefix");
  assert.deepStrictEqual(w2.messages[w1.messages.length], { role: "assistant", content: "醒着的第1句。" });
  assert.strictEqual(w2.messages.length, w1.messages.length + 2);

  // the letter is in no day file, nothing held, no status; what he said is
  const days = day_text(g.data_root);
  assert.ok(!days.includes(CARD_LINE) && !days.includes(LETTER_MARK), "the letter never reaches the day store");
  assert.ok(days.includes("醒着的第1句。") && days.includes("醒着的第2句。"));
  const held = fs.readFileSync(path.join(g.data_root, "held.json"), "utf8");
  assert.ok(!held.includes(CARD_LINE));
  assert.deepStrictEqual(held_items(g.data_root).map((h) => h.text), ["醒着的第1句。", "醒着的第2句。"]);
  const shown = JSON.stringify([g.wake.status(), g.present.wake.health()]);
  for (const secret of [CARD_LINE, LETTER_MARK, "醒着的第", "好呀"]) assert.ok(!shown.includes(secret), `status carries no ${secret}`);
  assert.ok(!log_lines(g.data_root).some((l) => JSON.stringify(l).includes(CARD_LINE) || JSON.stringify(l).includes("醒着的")),
    "the log carries no words");

  // her next turn: nothing the client receives carries the letter, and the snapshot starts over
  const next = await g.say([...HELLO, { role: "assistant", content: "好呀，等你下课。" }, { role: "user", content: "下课啦。" }],
                           { tools, temperature: 0.3, tool_choice: "auto" });
  assert.ok(!next.text.includes(CARD_LINE) && !next.text.includes(LETTER_MARK), "no letter in client-bound bytes");
  assert.ok(!JSON.stringify(up.chats()[1].body).includes(LETTER_MARK), "nor in her turn upstream");
  const t = g.present.threads.list().find((x) => x.last_sent);
  assert.deepStrictEqual(t.last_sent.wakes, [], "a new turn of hers is a new snapshot");
});

test("she arrives mid-wake: aborted on the spot, nothing written, not a failure", { timeout: 30000 }, async () => {
  up.plan((body, { wake }) => (wake ? { hang: true } : { text: "嗯嗯。" }));
  const g = await boot();
  await g.say(HELLO);
  g.clock.set(at("2026-10-07T11:00:00"));
  const ticking = g.wake.tick();
  await wait_for(() => up.wakes().length === 1);
  assert.strictEqual(g.present.own_turn.status().running.kind, "wake");

  const back = await g.say([...HELLO, { role: "assistant", content: "嗯嗯。" }, { role: "user", content: "我回来了！" }]);
  assert.strictEqual(back.status, 200);
  const r = await ticking;
  assert.deepStrictEqual([r.ran, r.result], [true, "aborted"]);
  up.release();

  assert.strictEqual(day_lines(g.data_root).filter((l) => l.woke).length, 0, "no line");
  assert.deepStrictEqual(held_items(g.data_root), [], "nothing held");
  const s = g.wake.status();
  assert.deepStrictEqual(s.today, { woke: 0, spoke: 0, paid_failures: 0, dry_run: 0 }, "not counted");
  assert.deepStrictEqual([s.last.result, s.last.result_words], ["aborted", "ta 来了，停下了"]);
  assert.strictEqual(g.present.wake.health().failures_since_ok, 0, "not a failure");
  const own = log_lines(g.data_root).filter((l) => l.event === "own_turn" && l.phase === "result");
  assert.deepStrictEqual(own.map((l) => [l.outcome, l.reason]), [["aborted", "owner_arrived"]]);
});

test("dry_run: computes the wake, logs would_wake, never calls upstream", { timeout: 30000 }, async () => {
  up.plan(() => ({ text: "嗯。" }));
  const g = await boot({ wake: { ...WAKE_ON, dry_run: true } });
  await g.say(HELLO);
  const seen = await run_until(g, at("2026-10-07T12:30:00"));
  assert.deepStrictEqual(seen.map((d) => [local_hhmm(d.at), d.result]), [["11:00", "would_wake"], ["12:00", "would_wake"]]);
  assert.strictEqual(up.wakes().length, 0, "no upstream call");
  assert.strictEqual(up.chats().length, 1);
  const would = log_lines(g.data_root).filter((l) => l.event === "wake" && l.result === "would_wake");
  assert.strictEqual(would.length, 2);
  assert.strictEqual(would[0].prefix, 2);
  assert.strictEqual(would[0].messages, 4);
  const s = g.wake.status();
  assert.strictEqual(s.state, "dry_run");
  assert.deepStrictEqual(s.today, { woke: 0, spoke: 0, paid_failures: 0, dry_run: 2 });
  assert.deepStrictEqual(held_items(g.data_root), []);
  assert.strictEqual(day_lines(g.data_root).filter((l) => l.woke).length, 0);
});

test("a dream is handed over once, by id; a failed wake leaves it for the next; her next message sends dream/wake", { timeout: 30000 }, async () => {
  const DREAM = "梦见一片很安静的海，ta 在岸边捡贝壳";
  loci.reset();
  loci.poke({ dreams: [{ id: "dream-1", 层: "完整", 内容: DREAM }], muse_pending: 0 });
  let wakes = 0;
  up.plan((body, { wake }) => (wake ? (++wakes === 1 ? { status: 500 } : { text: "" }) : { text: "晚安。" }));
  const g = await boot({ wake: { ...WAKE_ON, dnd: { on: false, from: "23:00", to: "08:00" } } });
  await g.say(HELLO);
  await run_until(g, at("2026-10-07T14:30:00"));
  const letters = up.wakes().map((x) => x.body.messages[x.body.messages.length - 1].content);
  assert.strictEqual(letters.length, 3, "11:00 failed, 13:00 and 14:00 ran");
  assert.ok(letters[0].includes(DREAM), "the first wake carried the dream");
  assert.ok(letters[1].includes(DREAM), "it failed, so the next one carries it again");
  assert.ok(!letters[2].includes(DREAM), "handed over once: not again");
  assert.ok(letters.every((l) => !l.includes("muse()")), "no muse line when Loci has none waiting");
  const armed = JSON.parse(fs.readFileSync(path.join(g.data_root, "state", "poke-window.json"), "utf8"));
  assert.strictEqual(armed.wakePending, true, "armed for her next message");
  assert.strictEqual(loci.dream_wakes.length, 0);

  loci.poke({ dreams: [{ id: "dream-1", 层: "完整", 内容: DREAM }], muse_pending: 4 });
  await run_until(g, at("2026-10-07T15:59:00"));
  const last = up.wakes().map((x) => x.body.messages[x.body.messages.length - 1].content).pop();
  assert.ok(last.includes("muse()") && !last.includes(DREAM), "the muse line rides along; the dream does not");

  // the chat path: her idle gate is open (she was away for hours) and Loci still has that
  // dream, now as a fragment — her message sends dream/wake and hands nothing over again
  const state_file = path.join(g.data_root, "state", "poke-window.json");
  const open_idle_gate = () => {
    const st = JSON.parse(fs.readFileSync(state_file, "utf8"));
    fs.writeFileSync(state_file, JSON.stringify({ ...st, lastUserMessageTime: new Date(Date.now() - 5 * 3600 * 1000).toISOString() }));
  };
  assert.deepStrictEqual(JSON.parse(fs.readFileSync(state_file, "utf8")).dreamsDelivered, ["dream-1"]);
  loci.poke({ dreams: [{ id: "dream-1", 层: "碎片", 内容: "安静的海" }], muse_pending: 0 });
  open_idle_gate();
  const pokes_before = loci.pokes.length;
  const morning = [...HELLO, { role: "assistant", content: "晚安。" }, { role: "user", content: "早！" }];
  await g.say(morning);
  assert.strictEqual(loci.dream_wakes.length, 1, "her next message sent dream/wake");
  assert.strictEqual(loci.pokes.length, pokes_before + 1, "the idle gate was open: Loci was asked");
  const her_turn = JSON.stringify(up.chats().pop().body);
  assert.ok(!her_turn.includes("安静的海") && !her_turn.includes("[Loci poke]"), "that dream is not handed over again, in any layer");
  assert.deepStrictEqual(JSON.parse(fs.readFileSync(state_file, "utf8")).dreamsDelivered, ["dream-1"], "the record survives the chat path");

  // a dream no wake handed over still comes the ordinary way
  loci.poke({ dreams: [{ id: "dream-2", 层: "完整", 内容: "梦见一座会走路的灯塔" }], muse_pending: 0 });
  open_idle_gate();
  g.clock.advance(MIN);
  await g.say([...morning, { role: "assistant", content: "晚安。" }, { role: "user", content: "今天好冷。" }]);
  assert.ok(JSON.stringify(up.chats().pop().body).includes("梦见一座会走路的灯塔"), "another dream is delivered as before");
});

test("tools: a snapshot without tools gets Loci's own; one with tools keeps them (the prefix test)", { timeout: 30000 }, async () => {
  up.plan((body, { wake }) => ({ text: wake ? "" : "嗯。" }));
  const g = await boot();
  await g.say(HELLO);
  assert.strictEqual(up.chats()[0].body.tools, undefined, "her client sent no tools");
  await run_until(g, at("2026-10-07T11:00:00"));
  const w1 = up.wakes()[0].body;
  assert.deepStrictEqual(w1.tools, [{ type: "function", function: { name: "recall", description: "找回记忆",
    parameters: LOCI_TOOLS[0].inputSchema } }], "Loci's tools, as Loci lists them");
  const line = log_lines(g.data_root).find((l) => l.event === "wake" && l.result);
  assert.strictEqual(line.tools, "loci");
});

test("after a restart: no borrowed key, no wake; a start stamp keeps a restarted gateway from waking at once", { timeout: 30000 }, async () => {
  up.plan((body, { wake }) => (wake ? { hang: true } : { text: "嗯。" }));
  const dir = fresh_dir();
  const clock = create_fake_clock(at("2026-10-07T10:00:00"));
  const a = await boot({ data_root: dir, clock });
  await a.say(HELLO);
  clock.set(at("2026-10-07T11:00:00"));
  const hung = a.wake.tick();
  await wait_for(() => up.wakes().length === 1);

  // the process "restarts" while that run hangs: the borrowed key is gone with it
  const b = await boot({ data_root: dir, clock, wake: null });
  clock.set(at("2026-10-07T13:00:00"));
  assert.deepStrictEqual(await b.wake.tick(), { ran: false, why: "no_key" });
  const s = b.wake.status();
  assert.deepStrictEqual([s.next_why, s.next_at], ["no_key", null]);
  assert.match(s.next_why_words, /LOCI_UPSTREAM_KEY/);
  assert.strictEqual(up.wakes().length, 1, "nothing sent without a key");

  // a fixed key works across restarts, but the stamp of the run cut off at 11:00 still counts
  clock.set(at("2026-10-07T11:01:00"));
  const c = await boot({ data_root: dir, clock, wake: null, env: { LOCI_UPSTREAM_KEY: "sk-fixed-SENTINEL-77" } });
  assert.deepStrictEqual(await c.wake.tick(), { ran: false, why: "cooldown" });
  assert.strictEqual(c.wake.status().next_at, "2026-10-07T12:00:00+08:00", "every_min from the start stamp");
  a.present.own_turn.abort("test_teardown");
  await hung;
  up.release();
});

test("held_cap: at the cap the gate waits for her; settings that cannot be read do not wake, and say so once", { timeout: 30000 }, async () => {
  up.plan((body, { wake }) => ({ text: wake ? "你在忙吗？" : "去吧。" }));
  const g = await boot({ wake: { ...WAKE_ON, held_cap: 1 } });
  await g.say(HELLO);
  const seen = await run_until(g, at("2026-10-07T14:00:00"));
  assert.deepStrictEqual(seen.map((d) => local_hhmm(d.at)), ["11:00"], "one unanswered line, then wait");
  const s = g.wake.status();
  assert.deepStrictEqual([s.next_why, s.next_at, s.held], ["held_cap", null, 1]);

  fs.writeFileSync(path.join(g.data_root, "present.json"), "{ not json", "utf8");
  await run_until(g, at("2026-10-07T14:10:00"));
  const shut = log_lines(g.data_root).filter((l) => l.event === "wake_gate" && l.blocked === "settings_unreadable");
  assert.strictEqual(shut.length, 1, "ten ticks, one line");
  const s2 = g.wake.status();
  assert.deepStrictEqual([s2.state, s2.next_why], ["settings_unreadable", "settings_unreadable"]);
  assert.strictEqual(up.wakes().length, 1);
});

test("a dream handed over on the chat path is recorded, and a wake does not hand it over again", { timeout: 30000 }, async () => {
  const DREAM = "梦见和 ta 在雨里等一班很晚的公交";
  loci.reset();
  loci.poke({ dreams: [{ id: "dream-chat", 层: "完整", 内容: DREAM }], muse_pending: 0 });
  up.plan((body, { wake }) => ({ text: wake ? "" : "早呀。" }));
  const g = await boot();
  // she comes back after hours away: the idle gate is open, so her first line carries the dream
  const state_file = path.join(g.data_root, "state", "poke-window.json");
  fs.writeFileSync(state_file, JSON.stringify({ lastUserMessageTime: new Date(Date.now() - 5 * 3600 * 1000).toISOString(), wakePending: false }));
  await g.say(HELLO);
  assert.ok(JSON.stringify(up.chats()[0].body).includes(DREAM), "the chat path handed the dream over");
  assert.deepStrictEqual(JSON.parse(fs.readFileSync(state_file, "utf8")).dreamsDelivered, ["dream-chat"], "and recorded its id");

  await run_until(g, at("2026-10-07T11:00:00"));
  const wakes = up.wakes().map((x) => x.body.messages[x.body.messages.length - 1].content);
  assert.strictEqual(wakes.length, 1);
  assert.ok(wakes[0].startsWith(LETTER_MARK) && !wakes[0].includes(DREAM), "the wake does not repeat it");
  assert.strictEqual(JSON.stringify(wakes[0]).includes("梦"), false, "no dream line at all");
});
