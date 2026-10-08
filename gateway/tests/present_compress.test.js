// ============================================================
// gateway/tests/present_compress.test.js — the reminders, the summary block and the
// window flip, through the real gateway
//
// The gateway runs as a black-box child process (start_gateway.js) with a fake upstream,
// a fake Loci, the network fence and a file-driven fake clock. present.json is planted
// with a small window (10000 tokens) and keep_raw 3, so a few turns reach the lines.
//
// What is being proved (construction step 4's acceptance):
//   · the weak line and the ask line are each offered once per window, at the true tail
//     (after her line and its card), with the compress card and keep_raw, and replayed in
//     place, same bytes, on later turns
//   · 🔴 a sentinel written inside 【窗口摘要】…【/窗口摘要】 appears 0 times in the
//     bytes the client receives — streamed, non-streamed, the block split across chunks,
//     the stream cut inside the block (connection dropped, and a clean early end) — and
//     never in the day store, a /present reply, /health or the console
//   · a closed block flips the window: carry = the summary, mark on the last keep_raw
//     lines (a line of hers), window number + 1, /cue/dropped {old, all}, status last =
//     self; the first turn after the flip offers no reminder
//   · a half block leaves the carry as it was and is stored nowhere
//   · compress.on false: nothing is offered
//
// Nothing outside os.tmpdir() is written; no real port is touched (reconciled at the end).
// ============================================================

const { test, before, after } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const fence = require("./network_fence.js");
const { start_fake_upstream } = require("./fake_upstream.js");
const { start_fake_loci } = require("./fake_loci.js");
const { start_gateway } = require("./start_gateway.js");
const { create_file_clock } = require("./fake_clock.js");
const { CARDS, compress_self_shell } = require("../present/prompts.js");
const { OPEN, CLOSE } = require("../present/stream_filter.js");

const root = fs.mkdtempSync(path.join(os.tmpdir(), "loci-present-compress-"));
const data_root = path.join(root, "data");
const days_dir = path.join(root, "library", "_hosts", "gateway", "days");
const ledger_path = path.join(root, "fence.jsonl");
const TOKEN = "gw-compress-token-41d2";
const SENTINEL = "哨兵·SUMMARY-5d0c2b-千万别漏出去";
const SENTINEL_KEY = "SUMMARY-5d0c2b";
const SYS = { role: "system", content: "你是一个温柔的伴侣。" };
const OUR_PORTS = new Set();
const u = (content) => ({ role: "user", content });
const a = (content) => ({ role: "assistant", content });
const usage = (prompt_tokens) => ({ prompt_tokens, completion_tokens: 20, total_tokens: prompt_tokens + 20 });

let fake_upstream, fake_loci, gateway, clock;
const plans = [];
const client_bytes = [];   // every byte any route of the gateway sent back, for the sentinel check

before(async () => {
  fake_upstream = await start_fake_upstream({ 端口: 0 });
  fake_loci = await start_fake_loci({ 端口: 0 });
  OUR_PORTS.add(fake_upstream.端口); OUR_PORTS.add(fake_loci.端口);
  fake_upstream.reply_with(() => plans.shift() || { text: "（没安排的回话）" });
  clock = create_file_clock(path.join(root, "clock.txt"), Date.parse("2026-10-07T12:00:00Z"));
  fs.mkdirSync(path.join(data_root, "state"), { recursive: true });
  fs.writeFileSync(path.join(data_root, "state", "poke-window.json"),
    JSON.stringify({ lastUserMessageTime: new Date().toISOString(), wakePending: false }));
  // force line at 99: these turns reach 97%, and the gateway's own packing is present_pack.test.js's
  fs.writeFileSync(path.join(data_root, "present.json"), JSON.stringify({ compress: { context_tokens: 10000, keep_raw: 3, force_pct: 99 } }));
  gateway = await start_gateway({
    端口: 0,
    上游地址: fake_upstream.地址,
    loci地址: fake_loci.地址,
    数据根: data_root,
    相关超时毫秒: 1200,
    白名单端口: [fake_upstream.端口, fake_loci.端口],
    账本路径: ledger_path,
    extra_env: {
      LOCI_TZ: "Asia/Shanghai",
      LOCI_GATEWAY_TEST_CLOCK: clock.file,
      LOCI_GATEWAY_DAYS: path.dirname(days_dir),
      LOCI_CUE_TIMEOUT_MS: "1200",
      LOCI_GATEWAY_TOKEN: TOKEN,
    },
  });
  OUR_PORTS.add(gateway.端口);
  fence.allow(gateway.端口);
});

after(async () => {
  if (gateway) await gateway.停();
  if (fake_upstream) await fake_upstream.关();
  if (fake_loci) await fake_loci.关();
  if (process.env.留下现场) console.log(`[留下现场] ${root}`);
  else fs.rmSync(root, { recursive: true, force: true });
});

/** One chat turn. The body is read chunk by chunk and kept even when the connection drops. */
async function chat(history, { stream = true } = {}) {
  const resp = await fetch(`${gateway.地址}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "Bearer test-key" },
    body: JSON.stringify({ model: "fake-model", stream, messages: history }),
  });
  const parts = [];
  let broke = false;
  try {
    const reader = resp.body.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parts.push(Buffer.from(value));
    }
  } catch { broke = true; }
  const text = Buffer.concat(parts).toString("utf8");
  const headers = {};
  resp.headers.forEach((v, k) => { headers[k] = v; });
  // A streamed answer carries the text in small pieces, so a sentinel in it is never one
  // run of bytes on the wire: the check reads the raw bytes and the text they reassemble to.
  const seen = stream ? `${text}\n${text_of_sse(text)}` : text;
  client_bytes.push(seen, JSON.stringify(headers));
  return { status: resp.status, text, seen, broke };
}

const leaked = (r) => r.seen.includes(SENTINEL_KEY) || r.seen.includes("5d0c2b");

async function call(method, route, body) {
  const headers = { "Content-Type": "application/json" };
  if (route !== "/health") headers.Authorization = `Bearer ${TOKEN}`;
  const resp = await fetch(`${gateway.地址}${route}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await resp.text();
  client_bytes.push(text);
  return { status: resp.status, text, json: JSON.parse(text) };
}

async function until(check, what, timeout_ms = 5000) {
  const deadline = Date.now() + timeout_ms;
  for (;;) {
    const got = check();
    if (got) return got;
    if (Date.now() > deadline) throw new Error(`timed out waiting for: ${what}`);
    await new Promise((r) => setTimeout(r, 20));
  }
}

/** The text a client reassembles from an SSE body. */
function text_of_sse(body) {
  let text = "";
  for (const block of body.split(/\n\n/)) {
    const line = block.split("\n").find((l) => l.startsWith("data:"));
    if (!line) continue;
    const data = line.slice(5).trim();
    if (data === "[DONE]") continue;
    let c;
    try { c = JSON.parse(data); } catch { continue; }
    for (const ch of c.choices || []) if (typeof ch.delta?.content === "string") text += ch.delta.content;
  }
  return text;
}

const threads = () => fs.readdirSync(path.join(data_root, "threads")).filter((n) => n.endsWith(".json"))
  .map((n) => JSON.parse(fs.readFileSync(path.join(data_root, "threads", n), "utf8")));
const thread = (id) => JSON.parse(fs.readFileSync(path.join(data_root, "threads", `${id}.json`), "utf8"));
const upstream_last = () => fake_upstream.收到.at(-1).体.messages;
const day_lines = () => fs.readdirSync(days_dir).flatMap((n) => fs.readFileSync(path.join(days_dir, n), "utf8")
  .split("\n").filter(Boolean).map((l) => JSON.parse(l)));
const asks = () => fake_loci.cue_requests("/api/v2/cue");
const dropped = () => fake_loci.cue_requests("/api/v2/cue/dropped");
const is_reminder = (m) => m.role === "system" && typeof m.content === "string" && m.content.startsWith("〔系统提醒");

const WEAK = compress_self_shell({ card: CARDS.compress.text, keep_raw: 3, line: "weak" });
const ASK = compress_self_shell({ card: CARDS.compress.text, keep_raw: 3, line: "ask" });

/** Send one turn and wait until its fill is recorded on the thread. */
async function turn(history, plan, opts = {}) {
  plans.push(plan);
  const r = await chat(history, opts);
  const id = asks().at(-1)?.体.turn;
  return { ...r, turn: id };
}

async function settled(thread_id, check, what) {
  return until(() => { const t = thread(thread_id); return check(t) ? t : null; }, what);
}

// ————————————————————————————————————————————————————————————

const R = [
  "你好呀，今天过得怎么样？我在这儿等你。",
  "考试辛苦了，先别想分数，好好吃一顿。",
  "那就早点睡，明天的事明天再说，我陪着你。",
  "嗯，我记得你说过想去海边，等考完我们就去。",
];
let T = null;   // the conversation the long test runs on
let history = [];

test("reminders: the weak line, then the ask line, each once, at the true tail, replayed in place", { timeout: 30000 }, async () => {
  history = [SYS, u("你好")];
  await turn(history, { text: R[0], usage: usage(5000) });
  T = await until(() => threads().find((t) => t.window?.usage?.prompt_tokens === 5000), "turn 1 usage");
  T = T.id;
  assert.ok(!upstream_last().some(is_reminder), "no fill yet: nothing offered");

  history = [...history, a(R[0]), u("今天考试了")];
  await turn(history, { text: R[1], usage: usage(7000) });
  await settled(T, (t) => t.window.usage?.prompt_tokens === 7000, "turn 2 usage");
  assert.ok(!upstream_last().some(is_reminder), "50% is under every line");

  // fill 70% ≥ weak 65 → the weak reminder, after her line and after its card
  history = [...history, a(R[1]), u("考完了，好累")];
  const t3 = await turn(history, { text: R[2], usage: usage(7200) });
  const up3 = upstream_last();
  assert.deepStrictEqual(up3.slice(-3).map((m) => m.role), ["user", "system", "system"]);
  assert.strictEqual(up3.at(-3).content, "考完了，好累");
  assert.match(up3.at(-2).content, /〔相关记忆〕/);
  assert.ok(up3.at(-2).content.includes(t3.turn), "the card is this turn's");
  assert.strictEqual(up3.at(-1).content, WEAK, "the weak reminder, the compress card and keep_raw in it, at the true tail");
  assert.strictEqual(up3.filter(is_reminder).length, 1);
  await settled(T, (t) => t.window.usage?.prompt_tokens === 7200, "turn 3 usage");
  assert.deepStrictEqual(thread(T).window.offered, ["weak:65"]);

  // fill 72%: the weak line is spent, the ask line not reached; the reminder stays in place
  history = [...history, a(R[2]), u("想去海边")];
  await turn(history, { text: R[3], usage: usage(8000) });
  const up4 = upstream_last();
  assert.strictEqual(up4.filter(is_reminder).length, 1, "not said twice");
  const prefix = JSON.stringify(up3).slice(0, -1);
  assert.ok(JSON.stringify(up4).startsWith(prefix), "turn 4 starts with turn 3's exact upstream messages, reminder included");
  await settled(T, (t) => t.window.usage?.prompt_tokens === 8000, "turn 4 usage");

  // the next turn (the next test) is at 80% ≥ ask 75
  history = [...history, a(R[3]), u("你帮我记着")];
});

let flip_turn = null;
let visible5 = null;

test("🔴 write → leave: the block never reaches the client (streamed, split across chunks); the window flips", { timeout: 30000 }, async () => {
  const reply = `好，我都记着。\n${OPEN}ta 今天考完试，很累，想去海边。${SENTINEL}${CLOSE}\n早点睡。`;
  visible5 = "好，我都记着。\n\n早点睡。";
  const r = await turn(history, { text: reply, usage: usage(8100) });
  flip_turn = r.turn;
  const up5 = upstream_last();
  assert.strictEqual(up5.at(-1).content, ASK, "the ask reminder at the true tail");
  assert.strictEqual(up5.filter(is_reminder).length, 2);
  assert.strictEqual(r.status, 200);
  assert.ok(!leaked(r), "the sentinel reached the client");
  assert.ok(!r.text.includes("窗口摘要"), "nor did the markers");
  assert.strictEqual(text_of_sse(r.text), visible5, "the client gets everything outside the block");
  assert.ok(r.text.endsWith("data: [DONE]\n\n"));

  const t = await settled(T, (x) => x.window.no === 2, "the flip");
  const w = t.window;
  assert.strictEqual(w.name, `${T}#w2`);
  assert.strictEqual(w.opened_by.how, "self");
  assert.strictEqual(w.opened_by.at, "2026-10-07T20:00:00+08:00");
  assert.ok(w.carry.includes(`ta 今天考完试，很累，想去海边。${SENTINEL}`), "the summary is the carry");
  assert.ok(!w.carry.includes(OPEN) && !w.carry.includes(CLOSE));
  assert.deepStrictEqual(w.carry_parts, { report: null, summary: `ta 今天考完试，很累，想去海边。${SENTINEL}` });
  assert.deepStrictEqual([w.offered, w.overlays, w.usage], [[], [], null], "fill and offered lines cleared (lesson ③)");
  // keep_raw 3 over 10 lines: the tail starts on a reply, so the mark moves on to her line
  const mark_at = t.branch.findIndex((e) => e.id === w.mark);
  assert.strictEqual(t.branch[mark_at].role, "user");
  assert.strictEqual(t.branch.length - mark_at, 2);
  assert.strictEqual(w.mark, flip_turn, "the mark is her last line");
  const drop = await until(() => dropped().find((d) => d.体.window === `${T}#w1`), "/cue/dropped");
  assert.deepStrictEqual(drop.体, { window: `${T}#w1`, all: true });

  const line = day_lines().find((l) => l.id === t.branch.at(-1).id);
  assert.strictEqual(line.role, "assistant");
  assert.strictEqual(line.text, visible5, "the day store keeps the text the client saw");
});

test("the first turn after the flip offers no reminder; upstream gets carry + the kept lines", { timeout: 30000 }, async () => {
  history = [...history, a(visible5), u("晚安")];
  const r = await turn(history, { text: "晚安，做个好梦。", usage: usage(9500) });
  const up = upstream_last();
  assert.ok(!up.some(is_reminder), "no reminder in the first turn of the new window");
  assert.strictEqual(up[0].content, SYS.content);
  assert.ok(up[1].content.includes(SENTINEL), "the carry goes upstream (and only there)");
  assert.deepStrictEqual(up.slice(2, 5), [u("你帮我记着"), a(visible5), u("晚安")]);
  assert.match(up[5].content, /〔相关记忆〕/);
  assert.strictEqual(up.length, 6);
  assert.strictEqual(asks().at(-1).体.window, `${T}#w2`);
  assert.ok(!leaked(r));
  await settled(T, (t) => t.window.usage?.prompt_tokens === 9500, "usage in the new window");

  // the new window can be reminded: its own lines start fresh
  history = [...history, a("晚安，做个好梦。"), u("还没睡着")];
  await turn(history, { text: "那我陪你聊一会儿。", usage: usage(9600) });
  assert.strictEqual(upstream_last().at(-1).content, ASK, "95%: the ask line, once, in window 2");
  await settled(T, (t) => t.window.usage?.prompt_tokens === 9600, "usage");
  history = [...history, a("那我陪你聊一会儿。")];
});

test("a half block (never closed) leaves the carry as it was and is stored nowhere", { timeout: 30000 }, async () => {
  const before_carry = thread(T).window.carry;
  history = [...history, u("讲个故事吧")];
  const r = await turn(history, { text: `好呀。\n${OPEN}写到一半${SENTINEL}就停了`, usage: usage(9700) });
  assert.ok(!leaked(r));
  assert.strictEqual(text_of_sse(r.text), "好呀。\n");
  const t = await settled(T, (x) => x.window.usage?.prompt_tokens === 9700, "usage");
  assert.strictEqual(t.window.no, 2, "no flip");
  assert.strictEqual(t.window.carry, before_carry, "the carry is unchanged");
  assert.ok(!t.window.summary_pending);
  assert.strictEqual(day_lines().find((l) => l.id === t.branch.at(-1).id).text, "好呀。\n");
  history = [...history, a("好呀。\n")];
});

test("a stream cut inside the block — dropped connection, or a clean early end — leaks nothing, stores nothing", { timeout: 30000 }, async () => {
  const before = thread(T);
  history = [...history, u("然后呢")];
  const text = `然后啊……\n${OPEN}断在这里${SENTINEL}后面没了`;
  const dropped_conn = await turn(history, { text, cut_inside: "SUMMARY-5d", cut: "destroy" });
  assert.ok(dropped_conn.broke, "upstream dropped the connection: so does the client's");
  const clean = await turn(history, { text, cut_inside: "SUMMARY-5d", cut: "end" });
  for (const r of [dropped_conn, clean]) {
    assert.ok(!leaked(r) && !r.seen.includes("SUMMARY") && !r.seen.includes("断在这里"), "the block reached the client");
    assert.ok(!r.seen.includes("窗口摘要"));
  }
  assert.strictEqual(text_of_sse(clean.text), "然后啊……\n", "what came before the block arrives");
  await new Promise((r) => setTimeout(r, 200));
  const after = thread(T);
  assert.strictEqual(after.window.carry, before.window.carry);
  assert.strictEqual(after.window.no, 2);
  assert.strictEqual(after.branch.at(-1).role, "user", "no reply was written for a cut answer");
  assert.ok(fake_upstream.sent.slice(-2).every((s) => text_of_sse(s).includes("SUMMARY-5d")), "upstream really did send the sentinel");
  history = history.slice(0, -1);
});

let T2 = null;

test("non-streamed: the block is taken out of the JSON answer and the window flips", { timeout: 30000 }, async () => {
  clock.advance(10 * 60 * 1000);
  const h = [SYS, u("不流式的一轮")];
  const r = await turn(h, { text: `好。\n${OPEN}不流式的摘要${SENTINEL}${CLOSE}\n嗯。`, usage: usage(3000) }, { stream: false });
  assert.strictEqual(r.status, 200);
  assert.ok(!leaked(r));
  const body = JSON.parse(r.text);
  assert.strictEqual(body.choices[0].message.content, "好。\n\n嗯。");
  assert.deepStrictEqual(body.usage, usage(3000), "the rest of the answer is as upstream sent it");
  const t = await until(() => threads().find((x) => x.window?.carry?.includes("不流式的摘要")), "the flip");
  T2 = t.id;
  assert.strictEqual(t.window.no, 2);
  assert.strictEqual(t.window.mark, t.branch[0].id, "keep_raw 3 over 2 lines: the whole conversation is kept");

  // a quoted marker is a mention, not a block: every byte as upstream sent it
  const mid = await turn([SYS, u("mid-line 测试")], { text: `我说的「${OPEN}」不是记号` }, { stream: false });
  assert.strictEqual(mid.text, fake_upstream.sent.at(-1));
  const mid_s = await turn([SYS, u("mid-line 流式")], { text: `我说的「${OPEN}」不是记号` });
  assert.strictEqual(mid_s.text, fake_upstream.without_usage_chunk.at(-1));
});

test("compress.on false: nothing is offered however full the window is", { timeout: 30000 }, async () => {
  clock.advance(60 * 1000);   // so this conversation is the one used last
  const off = await call("POST", "/present", { patch: { compress: { on: false } } });
  assert.strictEqual(off.status, 200, off.text);
  try {
    const h = [SYS, u("不流式的一轮"), a("好。\n\n嗯。"), u("满了也不提醒")];
    await turn(h, { text: "好的。", usage: usage(9900) }, { stream: false });
    await settled(T2, (t) => t.window.usage?.prompt_tokens === 9900, "usage");
    await turn([...h, a("好的。"), u("再来一句")], { text: "嗯。" }, { stream: false });
    assert.ok(!upstream_last().some(is_reminder), "99% and compress off: no reminder");
    assert.deepStrictEqual(thread(T2).window.offered, []);
  } finally {
    await call("POST", "/present", { patch: { compress: { on: true } } });
  }
});

test("/present status from the real window: fill, kept lines, last = self; time and way, never text", { timeout: 30000 }, async () => {
  const r = await call("GET", "/present");
  assert.strictEqual(r.status, 200);
  const c = r.json.status.compress;
  const t = thread(c.thread);
  assert.strictEqual(c.thread, T2, "the conversation used last");
  assert.strictEqual(c.state, "ok");
  assert.strictEqual(c.used_tokens, t.window.usage.prompt_tokens);
  assert.strictEqual(c.fill_pct, t.window.usage.fill_pct);
  assert.strictEqual(c.estimated, false);
  assert.deepStrictEqual(c.context, { tokens: 10000, source: "user" });
  const mark_at = t.branch.findIndex((e) => e.id === t.window.mark);
  assert.strictEqual(c.kept_raw, t.branch.length - mark_at);
  assert.deepStrictEqual(Object.keys(c.last).sort(), ["at", "how", "how_words"]);
  assert.strictEqual(c.last.how, "self");
  assert.strictEqual(c.last.how_words, "他自己压的");
  assert.match(c.last.at, /^2026-10-07T\d{2}:\d{2}:\d{2}\+08:00$/);
  const h = await call("GET", "/health");
  assert.strictEqual(h.status, 200);
});

test("🔴 sentinel: 0 times in every byte bound for a client, the day store and the console", () => {
  assert.ok(client_bytes.length > 20);
  const leaks = client_bytes.filter((b) => b.includes(SENTINEL_KEY) || b.includes("5d0c2b"));
  assert.deepStrictEqual(leaks, []);
  for (const n of fs.readdirSync(days_dir)) {
    assert.ok(!fs.readFileSync(path.join(days_dir, n), "utf8").includes("5d0c2b"), `day file ${n}`);
  }
  assert.ok(!gateway.全部输出().includes("5d0c2b"), "the console");
  assert.ok(fs.readFileSync(path.join(data_root, "threads", `${T}.json`), "utf8").includes(SENTINEL_KEY),
    "the carry does hold it, in the private ledger");
});

test("reconciliation: every outbound connection stayed inside this run", () => {
  const entries = fs.existsSync(ledger_path)
    ? fs.readFileSync(ledger_path, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
  assert.ok(entries.length > 0);
  assert.deepStrictEqual(entries.filter((e) => !OUR_PORTS.has(e.端口)), []);
  assert.ok(entries.every((e) => e.放行), "nothing had to be blocked");
});
