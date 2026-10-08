// ============================================================
// gateway/tests/present_day_close.test.js — the night (present/day_close.js), the wake
// snapshot after a flip (wake.js rebase) and a pack that hits the wall (pack.js)
//
// In-process, behind the network fence, on a fake clock: the real relay and the real
// present layer in front of a scripted fake upstream (chat, report, wake and pack
// requests told apart by their shells) and a fake Loci (/api/v2/slices POST and GET with
// scripted failures, cue, poke, an MCP face listing recall and grow). The heartbeat is
// not started; each test drives day_close.tick() and waits for its job. Temp directories
// only. Fixtures say 「ta」.
//
// What it holds the step to (blueprint §六 row 10, adapted to §七.6):
//   · 04:31 → Loci receives the slices (speaker names, revisions, report_at from the second
//     night on) → reports/ holds the report → the next turn's upstream carry is the report;
//     the letter lists the pending slices by the letter's own line numbers
//   · three failed reports in a row → the fourth beat does not try; the whole reason is in
//     <day>.err.json and status says gave_up
//   · every_n: the hand-off happens every night, the report and the flip only on day N
//   · manual: no flip until 「现在写日报」, which writes and flips; a second press while it
//     runs is a 409
//   · hand-off failures: Loci down → a later beat, the letter says 没交上; 502 → three
//     tries; 400 → recorded, never resent; source_changed_while_slicing → resent at once
//     with the current revision, without the withdrawn line
//   · a missed window: one make-up after report.to for yesterday; report_ready holds wake
//     until it is written; her arrival does not abort it
//   · after any flip (day, self) the wake snapshot is the new window: the wake goes out
//     small and the next chat turn shares its prefix
//   · a pack whose own input is over the window drops its oldest lines and folds
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
const { create_packer, fit_newest, wall_of } = require("../present/pack.js");
const { CARDS } = require("../present/prompts.js");
const { OPEN, CLOSE } = require("../present/stream_filter.js");
const win = require("../present/window.js");

const ZONE = "Asia/Shanghai";
const KEY = "sk-night-test-7d1c";
const at = (local) => Date.parse(`${local}+08:00`);
const MIN = 60 * 1000;
const REPORT_MARK = "【写日报";
const LETTER_MARK = "【自由时间";
const PACK_MARK = "【换窗";
const OWNER = "小林";
const AI = "阿默";
const SYS = { role: "system", content: "你是 ta 的伴，说话轻一点。" };
const u = (content) => ({ role: "user", content });
const a = (content) => ({ role: "assistant", content });

const root = fs.mkdtempSync(path.join(os.tmpdir(), "loci-night-"));
let n = 0;
const fresh_dir = () => { const d = path.join(root, `d${++n}`); fs.mkdirSync(d, { recursive: true }); return d; };

function kind_of(body) {
  const last = body?.messages?.[body.messages.length - 1];
  const text = typeof last?.content === "string" ? last.content : "";
  if (text.startsWith(REPORT_MARK)) return "report";
  if (text.startsWith(LETTER_MARK)) return "wake";
  if (text.startsWith(PACK_MARK)) return "pack";
  return "chat";
}

// ———— fake upstream: plan(body, { kind, i }) → { text, status, error, delay_ms } ————
function start_upstream() {
  const got = [];
  let plan = () => ({ text: "嗯。" });
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", async () => {
      if (req.method === "GET") {
        res.writeHead(200, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ object: "list", data: [] }));
      }
      const body = JSON.parse(Buffer.concat(chunks).toString("utf8") || "null");
      const kind = kind_of(body);
      got.push({ kind, body });
      const p = plan(body, { kind, i: got.filter((g) => g.kind === kind).length }) || {};
      if (p.delay_ms) await new Promise((r) => setTimeout(r, p.delay_ms));
      if (p.status && p.status !== 200) {
        res.writeHead(p.status, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ error: { message: p.error || "scripted failure" } }));
      }
      const text = String(p.text ?? "");
      const events = [{ choices: [{ index: 0, delta: { role: "assistant" } }] }];
      for (let i = 0; i < text.length; i += 4) events.push({ choices: [{ index: 0, delta: { content: text.slice(i, i + 4) } }] });
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
    of: (kind) => got.filter((g) => g.kind === kind).map((g) => g.body),
    plan(fn) { plan = fn; got.length = 0; },
    async close() { server.closeAllConnections?.(); await new Promise((r) => server.close(r)); },
  })));
}

// ———— fake Loci: the slices routes (scripted), cue, poke, MCP ————
const LOCI_TOOLS = [
  { name: "recall", description: "找回记忆", inputSchema: { type: "object", properties: { query: { type: "string" } } } },
  { name: "grow", description: "写下", inputSchema: { type: "object", properties: {} } },
];
function start_loci() {
  const posts = [];       // POST /api/v2/slices bodies, in order
  const gets = [];
  const dropped = [];
  let pending = [];
  let plan = null;        // (body, i) → { status, json } | null (null: the default success)
  let down = false;
  let seq = 0;
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const route = req.url.split("?")[0];
      if (down && (route === "/mcp" || route.startsWith("/api/v2/"))) { req.socket.destroy(); return; }
      const body = JSON.parse(Buffer.concat(chunks).toString("utf8") || "null");
      const json = (status, obj) => { res.writeHead(status, { "Content-Type": "application/json" }); res.end(JSON.stringify(obj)); };
      if (route === "/api/v2/slices" && req.method === "POST") {
        posts.push({ body, headers: { ...req.headers } });
        const p = plan ? plan(body, posts.length) : null;
        if (p) return json(p.status, p.json);
        seq += 1;
        const lines = body.lines;
        const slices = [{ slice_id: `s_${seq}`, span: { first: lines[0].id, last: lines[lines.length - 1].id, count: lines.length },
                          gist: "ta 说起考试", guesses: [{ id: "abcd1234ef567890", short: "abcd1234", score: 0.8 }] }];
        pending.push({ batch_id: `b_${seq}`, source: body.source, day: body.day, revision: null, slices });
        return json(200, { batch_id: `b_${seq}`, day: body.day, source: body.source, slices, unsliced: 0, replaced: false });
      }
      if (route === "/api/v2/slices" && req.method === "GET") {
        gets.push(Date.now());
        return json(200, { pending: pending.reduce((k, b) => k + b.slices.length, 0), batches: pending.slice().reverse(), scope: "" });
      }
      if (route === "/api/v2/cue") return json(200, { cards: [], text: "" });
      if (route === "/api/v2/cue/dropped") { dropped.push(body); return json(200, { ok: true }); }
      if (route.startsWith("/api/v2/cue/")) return json(200, { ok: true });
      if (route === "/api/loci/poke") return json(200, { dreams: [], muse_pending: 0 });
      if (route === "/mcp") {
        const sse = (obj, session) => {
          res.writeHead(200, { "Content-Type": "text/event-stream", ...(session ? { "Mcp-Session-Id": session } : {}) });
          res.end(`event: message\ndata: ${JSON.stringify(obj)}\n\n`);
        };
        if (body?.method === "initialize") return sse({ jsonrpc: "2.0", id: body.id, result: { protocolVersion: "2024-11-05", capabilities: {} } }, "s1");
        if (body?.method === "notifications/initialized") { res.writeHead(202); return res.end(); }
        if (body?.method === "tools/list") return sse({ jsonrpc: "2.0", id: body.id, result: { tools: LOCI_TOOLS } });
        if (body?.method === "tools/call") return sse({ jsonrpc: "2.0", id: body.id, result: { content: [{ type: "text", text: "好的" }] } });
        res.writeHead(400); return res.end();
      }
      res.writeHead(404); res.end();
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({
    port: server.address().port,
    url: `http://127.0.0.1:${server.address().port}/mcp`,
    posts, gets, dropped,
    plan_slices(fn) { plan = fn; },
    set_down(v) { down = v; },
    reset() { posts.length = 0; gets.length = 0; dropped.length = 0; pending = []; plan = null; down = false; seq = 0; },
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

/** The real present layer and the real relay, in this process, on a fake clock. */
async function boot({ settings = {}, start = at("2026-10-07T20:00:00") } = {}) {
  loci.reset();
  const data_root = fresh_dir();
  fs.mkdirSync(path.join(data_root, "state"), { recursive: true });
  // poke delivery's idle gate stays shut on the chat path (its clock is the real one)
  fs.writeFileSync(path.join(data_root, "state", "poke-window.json"),
    JSON.stringify({ lastUserMessageTime: new Date().toISOString(), wakePending: false }));
  fs.writeFileSync(path.join(data_root, "present.json"), JSON.stringify({ compress: { keep_raw: 2 }, ...settings }));
  const clock = create_fake_clock(start);
  const present = create_present({ env: { LOCI_TZ: ZONE, LOCI_OWNER_NAME: OWNER, LOCI_AI_NAME: AI },
                                   data_root, clock, upstream: up.url, loci: loci.url, log: () => {} });
  const relay = create_relay({ upstream: up.url, present });
  const server = http.createServer((req, res) => relay(req, res, { start: Date.now() }));
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  servers.push(server);
  fence.allow(server.address().port);
  const url = `http://127.0.0.1:${server.address().port}/v1/chat/completions`;

  /** Her turn, and wait until its answer has become the thread's snapshot. */
  async function say(messages) {
    // the clock may not move between turns, so a new snapshot is told apart by its contents
    const mark = (t) => `${t.id}|${t.last_sent ? JSON.stringify(t.last_sent.request.messages) + t.last_sent.reply : ""}`;
    const before_marks = present.threads.list().map(mark);
    const resp = await fetch(url, { method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${KEY}` },
      body: JSON.stringify({ model: "her-model", stream: true, messages }) });
    const text = await resp.text();
    assert.strictEqual(resp.status, 200, text);
    await wait_for(() => present.threads.list().some((t) => t.last_sent && !before_marks.includes(mark(t))));
    return text;
  }
  /** One beat of the night, waited for. */
  async function beat(when) {
    if (when !== undefined) clock.set(when);
    const r = present.day_close.tick();
    await present.day_close.settle();
    return r;
  }
  const reports_dir = path.join(data_root, "host", "reports");
  const reports = () => (fs.existsSync(reports_dir) ? fs.readdirSync(reports_dir).sort() : []);
  const thread = () => present.threads.list().slice().sort((x, y) => y.last_at - x.last_at)[0];
  return { present, clock, data_root, say, beat, reports, reports_dir, thread };
}

const wait_for = async (cond, ms = 3000) => {
  const end = Date.now() + ms;
  while (!cond()) { if (Date.now() > end) throw new Error("timed out waiting"); await new Promise((r) => setTimeout(r, 5)); }
};
const log_lines = (dir) => {
  const f = path.join(dir, "logs", "present.jsonl");
  return fs.existsSync(f) ? fs.readFileSync(f, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
};

const DAY1 = [SYS, u("考完了！"), a("辛苦了，考得怎么样？"), u("还行吧，最后一题没写完。"), a("没写完也没关系，先歇会儿。"), u("嗯，我去洗澡。")];
const REPORT_1 = "昨天 ta 考完了试。ta 说「考完了！」，我先松了口气。";
// his replies in DAY1, in order, so the client's history is what upstream answered; then 「嗯。」
const REPLY = (i) => [DAY1[2].content, DAY1[4].content, "好，去吧。"][i - 1] ?? "嗯。";

async function day_one(g) {
  await g.say(DAY1.slice(0, 2));
  await g.say(DAY1.slice(0, 4));
  await g.say(DAY1);
}

// ———————————————————————————————————————————————

test("04:31: the slices reach Loci, reports/ has the report, the next turn's carry is the report; the wake goes out small", { timeout: 30000 }, async () => {
  up.plan((body, { kind, i }) => ({ text: kind === "report" ? REPORT_1 : kind === "wake" ? "" : REPLY(i) }));
  const g = await boot({ settings: { compress: { keep_raw: 2 }, wake: { on: true, every_min: 15, dnd: { on: false, from: "23:00", to: "08:00" } } } });
  await day_one(g);
  assert.strictEqual(g.present.day_close.report_ready(), true, "nothing is due before the window opens");

  // before the window: the occurrence before it (closing out the 6th) had nothing said
  assert.deepStrictEqual(await g.beat(at("2026-10-08T03:59:00")), { started: false, why: "nothing" });
  assert.strictEqual(loci.posts.length, 0, "no hand-off outside the window");
  g.clock.set(at("2026-10-08T04:31:00"));
  assert.strictEqual(g.present.day_close.report_ready(), false, "inside the window, yesterday's report is not written yet");
  const r = await g.beat();
  assert.deepStrictEqual([r.started, r.kind, r.handoff, r.report], [true, "auto", true, true]);

  // ① the hand-off: one batch, the thread's lines of the 7th, names as speakers, no report yet
  assert.strictEqual(loci.posts.length, 1);
  const batch = loci.posts[0].body;
  const t = g.thread();
  assert.deepStrictEqual(batch.source, { system: "gateway", instance: "gateway", container: `thread:${t.id}` });
  assert.strictEqual(batch.day, "2026-10-07");
  assert.strictEqual(batch.report_at, null, "no report has been written before");
  assert.strictEqual(batch.lines.length, 6);
  assert.deepStrictEqual(batch.lines.map((l) => l.speaker), [OWNER, AI, OWNER, AI, OWNER, AI]);
  assert.deepStrictEqual(batch.lines.map((l) => l.text).slice(0, 2), ["考完了！", "辛苦了，考得怎么样？"]);
  assert.ok(batch.lines.every((l) => l.revision === "r1" && /^2026-10-07T20:00:00\+08:00$/.test(l.at) && /^m_20261007_\d{4}$/.test(l.id)));

  // ② the report turn: her system prompt, the shell, the pending slices by line number, Loci's tools
  const [req] = up.of("report");
  assert.ok(req, "a report went upstream");
  assert.deepStrictEqual(req.messages[0], SYS, "he writes as himself");
  const letter = req.messages[1].content;
  assert.ok(letter.includes(CARDS.report.text.split("\n")[0]), "the report card is in the letter");
  assert.ok(letter.includes("· s_1　第 1–6 行：ta 说起考试　好像记过：abcd1234"), letter);
  assert.ok(letter.includes("(1) [10-07 20:00] ta：考完了！") && letter.includes("(6) [10-07 20:00] 你：好，去吧。"), letter);
  assert.ok(!letter.includes("没交上"));
  assert.deepStrictEqual(req.tools.map((x) => x.function.name), ["recall", "grow"]);
  assert.deepStrictEqual(g.reports(), ["2026-10-07.md"]);
  assert.strictEqual(fs.readFileSync(path.join(g.reports_dir, "2026-10-07.md"), "utf8"), `${REPORT_1}\n`);
  assert.strictEqual(g.present.day_close.report_ready(), true);

  // ③ the flip: window 2, carry = the report, mark on the last keep_raw lines, Loci told
  const w = g.thread().window;
  assert.strictEqual(w.no, 2);
  assert.deepStrictEqual(w.opened_by, { at: "2026-10-08T04:31:00+08:00", how: "day" });
  assert.deepStrictEqual(w.carry_parts, { report: REPORT_1, summary: null });
  await wait_for(() => loci.dropped.length > 0);
  assert.deepStrictEqual(loci.dropped.map((d) => [d.window, d.all]), [[`${t.id}#w1`, true]]);
  const s = g.present.report_status();
  assert.deepStrictEqual([s.state, s.day, s.at, s.text, s.gave_up, s.next_flip, s.next_flip_day],
    ["written", "2026-10-07", "2026-10-08T04:31:00+08:00", REPORT_1, false, "daily", "2026-10-08"]);
  const h = g.present.report_health();
  assert.deepStrictEqual([h.state, h.last_ok_seconds_ago, h.failures_since_ok, h.handoff.failures_since_ok], ["ok", 0, 0, 0]);

  // the wake snapshot moved to the new window: the wake carries the report, not the old lines
  const wake = await g.present.wake.tick();
  assert.strictEqual(wake.ran, true, JSON.stringify(wake));
  const [wreq] = up.of("wake");
  assert.deepStrictEqual(wreq.messages.slice(0, 3).map((m) => m.role), ["system", "system", "user"]);
  assert.strictEqual(wreq.messages[1].content, w.carry);
  assert.deepStrictEqual(wreq.messages.slice(2, 4), [u("嗯，我去洗澡。"), a("好，去吧。")], "only the kept tail, then his reply");
  assert.ok(!JSON.stringify(wreq.messages).includes("最后一题没写完"), "nothing before the mark");
  assert.strictEqual(wreq.messages.length, 5, "system, carry, her line, his reply, the letter");

  // her next turn (later that morning): the carry upstream is the report, and it shares the wake's prefix
  g.clock.set(at("2026-10-08T07:30:00"));
  await g.say([...DAY1, a("好，去吧。"), u("早上好，我醒了。")]);
  const chat = up.of("chat").at(-1);
  assert.ok(chat.messages[1].content.includes("〔上一份日报〕") && chat.messages[1].content.includes(REPORT_1));
  assert.strictEqual(JSON.stringify(chat.messages.slice(0, 4)), JSON.stringify(wreq.messages.slice(0, 4)),
    "the chat turn after the flip shares the wake's prefix");

  // the second night: the hand-off carries the last report's time
  up.plan((body, { kind }) => ({ text: kind === "report" ? "第二天的日报。" : "嗯。" }));
  await g.beat(at("2026-10-09T04:31:00"));
  assert.strictEqual(loci.posts.length, 2);
  assert.strictEqual(loci.posts[1].body.report_at, "2026-10-08T04:31:00+08:00");
  assert.deepStrictEqual(loci.posts[1].body.lines.map((l) => l.text), ["早上好，我醒了。", "嗯。"]);
  assert.strictEqual(loci.posts[1].body.day, "2026-10-08");
  assert.deepStrictEqual(g.reports(), ["2026-10-07.md", "2026-10-08.md"]);
  const second = up.of("report").at(-1).messages.at(-1).content;
  assert.ok(second.includes("早上好，我醒了。") && !second.includes("考完了！"), "the second report covers only what came after the first");
  // nothing from the report or the lines in the log
  assert.ok(!log_lines(g.data_root).some((l) => /考完了|日报。|松了口气/.test(JSON.stringify(l))));
});

test("three failed reports in a row: the fourth beat does not try; the whole reason is kept", { timeout: 30000 }, async () => {
  up.plan((body, { kind, i }) => (kind === "report" ? { status: 500, error: "upstream is unwell: a long reason that is kept whole" } : { text: REPLY(i) }));
  const g = await boot();
  await day_one(g);
  for (const m of [31, 32, 33]) {
    const r = await g.beat(at(`2026-10-08T04:${m}:00`));
    assert.strictEqual(r.started, true, `beat 04:${m}`);
  }
  assert.strictEqual(up.of("report").length, 3);
  const r4 = await g.beat(at("2026-10-08T04:34:00"));
  assert.deepStrictEqual(r4, { started: false, why: "nothing" }, "the fourth beat does not try");
  assert.strictEqual(up.of("report").length, 3);
  assert.deepStrictEqual(g.reports(), ["2026-10-07.err.json"]);
  const card = JSON.parse(fs.readFileSync(path.join(g.reports_dir, "2026-10-07.err.json"), "utf8"));
  assert.deepStrictEqual([card.reason, card.failures, card.gave_up], ["http_500", 3, true]);
  assert.ok(card.error.includes("a long reason that is kept whole"));
  const s = g.present.report_status();
  assert.deepStrictEqual([s.state, s.gave_up], ["failed", true]);
  assert.ok(s.error.includes("a long reason that is kept whole"));
  assert.strictEqual(g.thread().window.no, 1, "no flip without a report");
  assert.strictEqual(g.present.day_close.report_ready(), true, "a report that gave up does not hold wake all day");
  // after the window: no make-up for an occurrence that gave up
  assert.strictEqual((await g.beat(at("2026-10-08T10:00:00"))).started, false);
  // 「补一份」: once per press, even after it gave up
  up.plan((body, { kind }) => ({ text: kind === "report" ? "补的日报。" : "嗯。" }));
  const asked = g.present.report_now("missing");
  assert.deepStrictEqual([asked.status, asked.body.ok, asked.body.queued, asked.body.day], [200, true, true, "2026-10-07"]);
  assert.strictEqual(g.present.report_now("now").status, 409, "one is being written");
  await g.present.day_close.settle();
  assert.deepStrictEqual(g.reports(), ["2026-10-07.md"], "written, and the error card is gone");
  assert.strictEqual(g.thread().window.no, 2);
  assert.strictEqual(g.present.report_now("missing").status, 409, "nothing is missing any more");
});

test("every_n: the hand-off every night, the report and the flip only on day N", { timeout: 30000 }, async () => {
  up.plan((body, { kind }) => ({ text: kind === "report" ? `日报${up.of("report").length}。` : "嗯。" }));
  const g = await boot({ settings: { compress: { keep_raw: 2 }, report: { flip: "every_n", every_n: 2 } } });
  const h1 = [SYS, u("第一天。")];
  await g.say(h1);
  await g.beat(at("2026-10-08T04:31:00"));
  assert.deepStrictEqual([loci.posts.length, up.of("report").length, g.thread().window.no], [1, 1, 2],
    "the first night: nothing has flipped yet, so it is day N");

  const h2 = [...h1, a("嗯。"), u("第二天。")];
  g.clock.set(at("2026-10-08T20:00:00"));
  await g.say(h2);
  const r2 = await g.beat(at("2026-10-09T04:31:00"));
  assert.deepStrictEqual([r2.kind, r2.handoff, r2.report], ["handoff", true, false]);
  assert.deepStrictEqual([loci.posts.length, up.of("report").length, g.thread().window.no], [2, 1, 2],
    "the second night: handed over, no report, no flip");
  assert.strictEqual(loci.posts[1].body.day, "2026-10-08");
  let s = g.present.report_status();
  assert.deepStrictEqual([s.state, s.next_flip, s.next_flip_day], ["not_due", "every_n", "2026-10-09"]);
  assert.strictEqual(g.present.day_close.report_ready(), true, "nothing due holds no wake");

  g.clock.set(at("2026-10-09T20:00:00"));
  await g.say([...h2, a("嗯。"), u("第三天。")]);
  await g.beat(at("2026-10-10T04:31:00"));
  assert.deepStrictEqual([loci.posts.length, up.of("report").length, g.thread().window.no], [3, 2, 3], "day N: report and flip");
  const letter = up.of("report").at(-1).messages.at(-1).content;
  assert.ok(letter.includes("第二天。") && letter.includes("第三天。") && !letter.includes("第一天。"),
    "the report covers both days since the last one");
  assert.ok(letter.includes("· s_2") && letter.includes("· s_3"), "the queued slices of both nights are listed");
  assert.deepStrictEqual(g.reports(), ["2026-10-07.md", "2026-10-09.md"]);
  s = g.present.report_status();
  assert.deepStrictEqual([s.state, s.day, s.next_flip_day], ["written", "2026-10-09", "2026-10-11"]);
});

test("manual: the hand-off every night, no flip until 「现在写日报」, which writes and flips", { timeout: 30000 }, async () => {
  up.plan((body, { kind, i }) => ({ text: kind === "report" ? "ta 说换的时候写的日报。" : REPLY(i) }));
  const g = await boot({ settings: { compress: { keep_raw: 2 }, report: { flip: "manual" } } });
  await day_one(g);
  const r = await g.beat(at("2026-10-08T04:31:00"));
  assert.deepStrictEqual([r.kind, r.report], ["handoff", false]);
  assert.deepStrictEqual([loci.posts.length, up.of("report").length, g.thread().window.no], [1, 0, 1]);
  assert.strictEqual(g.present.report_status().state, "not_due");
  assert.strictEqual(g.present.report_status().next_flip_day, null);
  assert.strictEqual((await g.beat(at("2026-10-08T05:31:00"))).started, false, "nothing new to hand over");

  g.clock.set(at("2026-10-08T15:00:00"));
  assert.strictEqual(g.present.report_now("bogus").status, 400);
  const asked = g.present.report_now("now");
  assert.deepStrictEqual([asked.status, asked.body.day], [200, "2026-10-08"]);
  assert.strictEqual(g.present.report_now("now").status, 409, "a second press while it runs");
  await g.present.day_close.settle();
  assert.deepStrictEqual(g.reports(), ["2026-10-08.md"]);
  assert.strictEqual(g.thread().window.no, 2);
  assert.strictEqual(g.thread().window.opened_by.how, "day");
  await g.say([...DAY1, a("好，去吧。"), u("我回来了。")]);
  assert.ok(up.of("chat").at(-1).messages[1].content.includes("ta 说换的时候写的日报。"), "her next turn opens the new window");
  assert.strictEqual(g.present.report_status().state, "written");
});

test("hand-off failures: Loci down → a later beat and 没交上 in the letter; 502 three tries; 400 recorded, never resent", { timeout: 30000 }, async () => {
  up.plan((body, { kind, i }) => ({ text: kind === "report" ? "照写的日报。" : REPLY(((i - 1) % 3) + 1) }));
  let g = await boot();
  await day_one(g);
  loci.set_down(true);
  await g.beat(at("2026-10-08T04:31:00"));
  assert.strictEqual(loci.posts.length, 0);
  const letter = up.of("report").at(-1).messages.at(-1).content;
  assert.ok(letter.includes("今晚的切片没交上，这次先只写日报"), "the report is written anyway, saying so");
  assert.deepStrictEqual(g.reports(), ["2026-10-07.md"]);
  assert.ok(/Loci unreachable/.test(g.present.report_health().handoff.last_error));
  loci.set_down(false);
  await g.beat(at("2026-10-08T04:32:00"));
  assert.strictEqual(loci.posts.length, 1, "handed over on a later beat");
  assert.strictEqual(loci.posts[0].body.report_at, "2026-10-08T04:31:00+08:00");
  assert.strictEqual(up.of("report").length, 1, "the report is not written twice");
  assert.strictEqual(g.present.report_health().handoff.failures_since_ok, 0);

  // 502: three tries over three beats, then given up
  g = await boot();
  await day_one(g);
  loci.plan_slices(() => ({ status: 502, json: { error: "the side model failed, nothing was stored" } }));
  for (const m of [31, 32, 33, 34]) await g.beat(at(`2026-10-08T04:${m}:00`));
  assert.strictEqual(loci.posts.length, 3, "at most three tries");
  let h = g.present.report_health().handoff;
  assert.strictEqual(h.given_up_batches, 1);
  assert.ok(/HTTP 502/.test(h.last_error));

  // 400: the reason recorded, the batch never sent again
  g = await boot();
  await day_one(g);
  loci.plan_slices(() => ({ status: 400, json: { error: "lines[0]: not a valid source id" } }));
  await g.beat(at("2026-10-08T04:31:00"));
  await g.beat(at("2026-10-08T04:32:00"));
  assert.strictEqual(loci.posts.length, 1);
  h = g.present.report_health().handoff;
  assert.ok(h.last_error.includes("not a valid source id"));
  const books = JSON.parse(fs.readFileSync(path.join(g.data_root, "counters.json"), "utf8")).report.handoff;
  assert.deepStrictEqual([books.failed.length, books.failed[0].status, books.failed[0].given_up], [1, 400, true]);
});

test("source_changed_while_slicing: resent at once with the current revision, without the withdrawn line", { timeout: 30000 }, async () => {
  up.plan((body, { kind, i }) => ({ text: kind === "report" ? "日报。" : REPLY(i) }));
  const g = await boot();
  await day_one(g);
  const ids = g.thread().branch.map((e) => e.id);
  // Loci answers the first POST: one line was revised, another withdrawn, while it sliced;
  // the revision lands in the day store right then
  loci.plan_slices((body, i) => {
    if (i !== 1) return null;
    g.present.day_store.revise(ids[1], { text: "辛苦了！考得怎么样呀？" });
    return { status: 400, json: { error: "lines changed while slicing",
      note: "source_changed_while_slicing", lines: { [ids[1]]: "revised", [ids[3]]: "withdrawn" } } };
  });
  await g.beat(at("2026-10-08T04:31:00"));
  assert.deepStrictEqual(loci.posts[0].body.lines.find((l) => l.id === ids[1]).revision, "r1");
  assert.strictEqual(loci.posts.length, 2, "resent in the same beat");
  const second = loci.posts[1].body.lines;
  assert.ok(!second.some((l) => l.id === ids[3]), "the withdrawn line is left out");
  const revised = second.find((l) => l.id === ids[1]);
  assert.deepStrictEqual([revised.text, revised.revision], ["辛苦了！考得怎么样呀？", "r2"]);
  assert.strictEqual(g.present.report_health().handoff.failures_since_ok, 0);
});

test("a missed window: one make-up after report.to, for yesterday; it holds wake until written; her arrival does not abort it", { timeout: 30000 }, async () => {
  up.plan((body, { kind, i }) => (kind === "report" ? { text: "补写的日报。", delay_ms: 300 } : { text: REPLY(i) }));
  const g = await boot();
  await day_one(g);
  g.clock.set(at("2026-10-08T09:00:00"));   // the computer was off through the window
  assert.strictEqual(g.present.day_close.report_ready(), false, "yesterday's report holds the morning's first call");
  assert.strictEqual(g.present.report_status().state, "missing");
  const r = g.present.day_close.tick();
  assert.deepStrictEqual([r.started, r.kind, r.handoff], [true, "makeup", true]);
  await wait_for(() => up.of("report").length === 1);
  g.present.owner_arrived();   // she comes back while it is being written
  await g.present.day_close.settle();
  assert.deepStrictEqual(g.reports(), ["2026-10-07.md"], "her arrival did not abort the report");
  assert.strictEqual(g.present.day_close.report_ready(), true);
  assert.strictEqual(g.thread().window.no, 2);
  assert.deepStrictEqual(await g.beat(at("2026-10-08T12:00:00")), { started: false, why: "outside" }, "one make-up, no more");
  // the next day: the 8th had nothing said, so there is nothing to make up
  assert.deepStrictEqual(await g.beat(at("2026-10-09T10:00:00")), { started: false, why: "nothing" });
  assert.strictEqual(g.present.report_status().state, "quiet");
  assert.strictEqual(up.of("report").length, 1);
});

test("a self flip rebuilds the wake snapshot too: the wake carries his summary and the kept tail", { timeout: 30000 }, async () => {
  const SUMMARY = "ta 考完试了，在洗澡。";
  up.plan((body, { kind, i }) => (kind === "wake" ? { text: "" }
    : kind === "chat" && i === 3 ? { text: `好，去吧。\n${OPEN}${SUMMARY}${CLOSE}` } : { text: REPLY(i) }));
  const g = await boot({ settings: { compress: { keep_raw: 2 },
    report: { flip: "manual" }, wake: { on: true, every_min: 15, dnd: { on: false, from: "23:00", to: "08:00" } } } });
  await day_one(g);
  const t = g.thread();
  assert.strictEqual(t.window.no, 2, "he folded it himself");
  assert.strictEqual(t.window.opened_by.how, "self");
  const req = t.last_sent.request.messages;
  assert.deepStrictEqual(req[0], SYS);
  assert.strictEqual(req[1].content, t.window.carry);
  assert.ok(req[1].content.includes(SUMMARY));
  assert.deepStrictEqual(req.slice(2), [u("嗯，我去洗澡。")], "the client's messages from the new mark on, no overlays");
  assert.strictEqual(t.last_sent.reply.trim(), "好，去吧。", "his reply as the client saw it, the block taken out");
  g.clock.set(at("2026-10-07T21:00:00"));
  const r = await g.present.wake.tick();
  assert.strictEqual(r.ran, true, JSON.stringify(r));
  const w = up.of("wake")[0];
  assert.strictEqual(w.messages.length, 5, "system, carry, her line, his reply, the letter");
  assert.ok(!JSON.stringify(w.messages).includes("考完了！"));
});

test("a pack's flip (「现在压」) rebuilds the wake snapshot as well", { timeout: 30000 }, async () => {
  up.plan((body, { kind, i }) => ({ text: kind === "pack" ? "ta 考完了，去洗澡了。" : REPLY(i) }));
  const g = await boot({ settings: { compress: { keep_raw: 2 }, report: { flip: "manual" } } });
  await day_one(g);
  const asked = g.present.compress_now();
  assert.strictEqual(asked.status, 200);
  await g.present.packer.wait_for([g.thread().id], 5000);
  const t = g.thread();
  assert.deepStrictEqual([t.window.no, t.window.opened_by.how], [2, "manual"]);
  const req = t.last_sent.request.messages;
  assert.deepStrictEqual([req[0], req[1].content, req.slice(2)], [SYS, t.window.carry, [u("嗯，我去洗澡。")]]);
  assert.ok(req[1].content.includes("ta 考完了，去洗澡了。"));
});

// ———— the pack's wall (in-process, a scripted own turn) ————

function pack_rig({ answers, lines = 12 }) {
  const dir = fresh_dir();
  const clock = create_fake_clock(at("2026-10-07T21:00:00"));
  const branch = [];
  const store = new Map();
  for (let i = 1; i <= lines; i++) {
    const id = `m_20261007_${String(i).padStart(4, "0")}`;
    const role = i % 2 ? "user" : "assistant";
    branch.push({ id, role, fp: id });
    store.set(id, { id, role, text: `${role === "user" ? "ta" : "我"}说的第${i}句，${"字".repeat(300)}`, at: "2026-10-07T20:00:00+08:00" });
  }
  const thread = { id: "t_aaaaaa", branch, window: win.fresh_window("t_aaaaaa", clock.now()) };
  const runs = [];
  const own_turn = { run: async (args) => { runs.push(args); return answers.shift(); } };
  const packer = create_packer({
    threads: { get: () => thread, save: () => true }, day_store: { get: (id) => store.get(id) || null }, own_turn,
    prompts: { current: () => "卡" }, read_compress: () => ({ on: true, keep_raw: 2 }), data_root: dir, clock, zone: ZONE, log: () => {},
  });
  return { packer, runs, thread, dir };
}
const WALL = (limit, actual) => ({ ok: false, outcome: "paid", reason: "http_400", run: "r",
  error: JSON.stringify({ error: { code: "context_length_exceeded", message: `This model's maximum context length is ${limit} tokens. However, your messages resulted in ${actual} tokens.` } }) });
const OK = (text) => ({ ok: true, outcome: "ok", reason: null, text, said: text, run: "r", rounds: 0, tools_used: [] });

test("a pack over the window: the oldest lines are dropped until it fits, the pack folds, the cut is logged", { timeout: 20000 }, async () => {
  const rig = pack_rig({ answers: [WALL(1000, 2000), OK("收好了。")] });
  const r = await rig.packer.request("t_aaaaaa", "forced");
  assert.ok(r.started);
  await rig.packer.wait_for(["t_aaaaaa"], 5000);
  assert.strictEqual(rig.runs.length, 2, "sent again once it was cut");
  const first = rig.runs[0].messages.at(-1).content;
  const second = rig.runs[1].messages.at(-1).content;
  assert.ok(first.includes("说的第1句") && first.includes("说的第10句"));
  assert.ok(!second.includes("说的第1句，") && second.includes("说的第10句"), "the oldest went, the newest stayed");
  assert.ok(second.length < first.length * 0.6);
  assert.strictEqual(rig.thread.window.no, 2, "the window flipped");
  assert.strictEqual(rig.thread.window.carry_parts.summary, "收好了。");
  assert.strictEqual(rig.thread.window.mark, "m_20261007_0011", "the mark is where the plan put it");
  const logged = log_lines(rig.dir).filter((l) => l.event === "pack");
  const wall = logged.find((l) => l.outcome === "pack_wall");
  assert.ok(wall && wall.lines_before === 10 && wall.lines_after < 10 && wall.limit === 1000 && wall.actual === 2000);
  assert.strictEqual(logged.at(-1).outcome, "ok");
  assert.ok(logged.at(-1).left_out > 0);

  // no room at all: it fails as before, without a loop
  const tight = pack_rig({ answers: [WALL(10, 100000), OK("不该用到")], lines: 4 });
  tight.packer.request("t_aaaaaa", "forced");
  await tight.packer.wait_for(["t_aaaaaa"], 5000);
  assert.strictEqual(tight.runs.length, 1);
  assert.strictEqual(tight.thread.window.no, 1);

  // the pieces
  assert.strictEqual(wall_of(OK("x")), null);
  assert.strictEqual(wall_of({ ok: false, outcome: "paid", reason: "http_500", error: "context_length_exceeded" }), null, "a 5xx is no wall");
  assert.deepStrictEqual(fit_newest({ total: 1000, sizes: [100, 100, 100, 100], wall: { limit: null, actual: null } }), 0,
    "half of 1000 minus 600 of overhead leaves room for none");
  assert.strictEqual(fit_newest({ total: 1000, sizes: [200, 200, 200, 200], wall: { limit: null, actual: null } }), 1);
  assert.strictEqual(fit_newest({ total: 30, sizes: [10, 10, 10], wall: { limit: 1000, actual: 1001 } }), 2,
    "limit over refused size, with the margin: at least one goes");
});
