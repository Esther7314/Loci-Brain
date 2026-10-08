// ============================================================
// gateway/tests/present_pack.test.js — the gateway folds a window itself (forced, manual)
// and the way out of the context-length wall, through the real gateway
//
// The gateway runs as a black-box child process (start_gateway.js) with a fake upstream,
// a fake Loci, the network fence and a file-driven fake clock. present.json plants a small
// window (10000 tokens), keep_raw 2 and the force line at 85; LOCI_PACK_WAIT_MS is 1500 so
// "her turn waits up to 90 s" can be watched in seconds. A pack request is told apart
// upstream by its shell (【换窗 …).
//
// What is being proved (construction step 5's acceptance):
//   · fill ≥ force line → a pack runs (own turn: her system prompt, the forced shell with
//     the card, keep_raw and exactly the lines before the kept tail, Loci's tools) → the
//     next turn's upstream body carries the new carry and the window number went up
//   · a pack in flight makes the next turn wait, then go with the new window; a pack that
//     takes longer than the wait → the turn goes as it is, and the pack still flips after
//   · the earlier summary is folded into the next pack; markers are stripped from a summary
//   · POST /present/compress → queued, then a flip with how "manual"; a second call while
//     it runs → 409
//   · upstream answers context_length_exceeded "maximum context length is 8192 tokens" →
//     cut once, the resend succeeds, one 「撞墙」 line, 8192 learned (source learned), the
//     fill after it uses 8192; a resend that fails too returns upstream's error and the
//     next turn still escapes; a failed pack backs off instead of retrying every turn
//   · 🔴 a sentinel in a pack's summary is never in client bytes, the day store, /present,
//     /health or the console
// Plus, in-process: the wall patterns, the cut, the summary cleaning, and her arrival
// aborting a wake but not a pack (own_turn.js).
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
const { CARDS } = require("../present/prompts.js");
const { OPEN, CLOSE } = require("../present/stream_filter.js");
const { estimate_prompt } = require("../present/fill.js");
const { wall_error, cut_oldest } = require("../present/wall.js");
const { clean_summary, plan_pack, format_line } = require("../present/pack.js");
const { create_own_turn } = require("../present/own_turn.js");

const root = fs.mkdtempSync(path.join(os.tmpdir(), "loci-present-pack-"));
const data_root = path.join(root, "data");
const days_dir = path.join(root, "library", "_hosts", "gateway", "days");
const ledger_path = path.join(root, "fence.jsonl");
const TOKEN = "gw-pack-token-90e1";
const SENTINEL = "哨兵·PACKED-3b7e91-千万别漏出去";
const SENTINEL_KEY = "3b7e91";
const SYS = { role: "system", content: "你是一个温柔的伴侣。" };
const OUR_PORTS = new Set();
const u = (content) => ({ role: "user", content });
const a = (content) => ({ role: "assistant", content });
const usage = (prompt_tokens) => ({ prompt_tokens, completion_tokens: 20, total_tokens: prompt_tokens + 20 });

let fake_upstream, fake_loci, gateway, clock;
const plans = [];        // chat turns, in order
const pack_plans = [];   // pack requests, in order
let wall_mode = "off";   // "off" | "limit" (over 8192 estimated → refused) | "always"
const client_bytes = [];

const is_pack = (body) => {
  const last = body?.messages?.at(-1);
  return last?.role === "user" && typeof last.content === "string" && last.content.startsWith("【换窗");
};
const WALL_MSG = (n) => `This model's maximum context length is 8192 tokens. However, your messages resulted in ${n} tokens. Please reduce the length of the messages.`;

before(async () => {
  fake_upstream = await start_fake_upstream({ 端口: 0 });
  fake_loci = await start_fake_loci({ 端口: 0 });
  OUR_PORTS.add(fake_upstream.端口); OUR_PORTS.add(fake_loci.端口);
  fake_upstream.reply_with((body) => {
    if (is_pack(body)) return pack_plans.shift() || { text: "（没安排的摘要）" };
    if (body?.model === "wall-model") {
      const n = estimate_prompt(body);
      if (wall_mode === "always" || (wall_mode === "limit" && n > 8192)) {
        return { status: 400, code: "context_length_exceeded", error: WALL_MSG(n) };
      }
      return { text: `墙那边的回话 ${n}${"回".repeat(1500)}`, usage: usage(n), piece: 400 };
    }
    return plans.shift() || { text: "（没安排的回话）" };
  });
  clock = create_file_clock(path.join(root, "clock.txt"), Date.parse("2026-10-07T12:00:00Z"));
  fs.mkdirSync(path.join(data_root, "state"), { recursive: true });
  fs.writeFileSync(path.join(data_root, "state", "poke-window.json"),
    JSON.stringify({ lastUserMessageTime: new Date().toISOString(), wakePending: false }));
  fs.writeFileSync(path.join(data_root, "present.json"), JSON.stringify({ compress: { context_tokens: 10000, keep_raw: 2, force_pct: 85 } }));
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
      LOCI_PACK_WAIT_MS: "1500",
    },
  });
  OUR_PORTS.add(gateway.端口);
  fence.allow(gateway.端口, fake_upstream.端口, fake_loci.端口);   // the in-process own turn at the end talks to the fakes
});

after(async () => {
  if (gateway) await gateway.停();
  if (fake_upstream) await fake_upstream.关();
  if (fake_loci) await fake_loci.关();
  if (process.env.留下现场) console.log(`[留下现场] ${root}`);
  else fs.rmSync(root, { recursive: true, force: true });
});

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

async function chat(history, { model = "fake-model" } = {}) {
  const resp = await fetch(`${gateway.地址}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "Bearer test-key" },
    body: JSON.stringify({ model, stream: true, messages: history }),
  });
  const text = await resp.text();
  client_bytes.push(`${text}\n${text_of_sse(text)}`);
  return { status: resp.status, text };
}

async function call(method, route, body) {
  const headers = { "Content-Type": "application/json" };
  if (route !== "/health") headers.Authorization = `Bearer ${TOKEN}`;
  const resp = await fetch(`${gateway.地址}${route}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await resp.text();
  client_bytes.push(text);
  return { status: resp.status, text, json: JSON.parse(text) };
}

async function until(check, what, timeout_ms = 6000) {
  const deadline = Date.now() + timeout_ms;
  for (;;) {
    const got = check();
    if (got) return got;
    if (Date.now() > deadline) throw new Error(`timed out waiting for: ${what}`);
    await new Promise((r) => setTimeout(r, 20));
  }
}

const thread = (id) => JSON.parse(fs.readFileSync(path.join(data_root, "threads", `${id}.json`), "utf8"));
const threads = () => fs.readdirSync(path.join(data_root, "threads")).filter((n) => n.endsWith(".json"))
  .map((n) => JSON.parse(fs.readFileSync(path.join(data_root, "threads", n), "utf8")));
const chats = () => fake_upstream.收到.filter((r) => r.体?.messages && !is_pack(r.体));
const packs = () => fake_upstream.收到.filter((r) => is_pack(r.体));
const asks = () => fake_loci.cue_requests("/api/v2/cue");
const present_log = () => {
  const f = path.join(data_root, "logs", "present.jsonl");
  return fs.existsSync(f) ? fs.readFileSync(f, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
};
const settled = (id, check, what, ms) => until(() => { const t = thread(id); return check(t) ? t : null; }, what, ms);

// ————————————————————————————————————————————————————————————

const R = ["你好呀，今天怎么样？", "考试辛苦了，先吃饭。", "那就早点睡，我陪着你。", "海边等你考完就去。", "嗯，我记着呢。", "晚安。"];
let T = null;
let history = [];
let summary1 = null;

test("fill ≥ force line → a pack (forced shell, the lines before the kept tail, Loci's tools) → the next turn carries the new carry", { timeout: 30000 }, async () => {
  history = [SYS, u("你好")];
  plans.push({ text: R[0], usage: usage(3000) });
  await chat(history);
  T = (await until(() => threads().find((t) => t.window?.usage?.prompt_tokens === 3000), "turn 1")).id;

  history = [...history, a(R[0]), u("今天考试了")];
  plans.push({ text: R[1], usage: usage(4000) });
  await chat(history);
  await settled(T, (t) => t.window.usage?.prompt_tokens === 4000, "turn 2");
  assert.strictEqual(packs().length, 0, "40%: no pack");

  // 90% ≥ 85: the gateway packs in the background, at once
  summary1 = `ta 今天考完试，很累。${SENTINEL}`;
  pack_plans.push({ text: `好的，摘要如下：${OPEN}${summary1}${CLOSE}`, usage: usage(2000) });
  history = [...history, a(R[1]), u("考完了，好累")];
  plans.push({ text: R[2], usage: usage(9000) });
  const r3 = await chat(history);
  assert.strictEqual(r3.status, 200);
  const t = await settled(T, (x) => x.window.no === 2, "the forced flip");

  const pack = packs()[0];
  const shell = pack.体.messages.at(-1).content;
  assert.deepStrictEqual(pack.体.messages[0], SYS, "his system prompt, so he writes as himself");
  assert.strictEqual(pack.体.messages.length, 2);
  assert.strictEqual(pack.体.model, "fake-model", "the model she used");
  assert.strictEqual(pack.头.authorization, "Bearer test-key", "her borrowed key");
  assert.deepStrictEqual(pack.体.tools.map((x) => x.function.name), ["recall", "grow"], "Loci's tools only");
  assert.ok(shell.includes(CARDS.compress.text), "the compress card in force");
  assert.ok(shell.includes("最近 2 条原话"), "keep_raw");
  assert.ok(shell.includes("这一步只调工具"));
  // exactly the lines before the kept tail: her first two lines and his first two replies
  for (const s of ["ta：你好", `你：${R[0]}`, "ta：今天考试了", `你：${R[1]}`]) assert.ok(shell.includes(s), s);
  assert.ok(!shell.includes("考完了，好累") && !shell.includes(R[2]), "the kept tail is not packed");
  assert.ok(!shell.includes("上一扇窗收尾时你写的摘要"), "no earlier summary in a first window");

  const w = t.window;
  assert.strictEqual(w.opened_by.how, "forced");
  assert.deepStrictEqual(w.carry_parts, { report: null, summary: summary1 }, "the block he wrapped it in, without the words around it");
  assert.ok(!w.carry.includes(OPEN) && !w.carry.includes(CLOSE));
  assert.strictEqual(t.branch.find((e) => e.id === w.mark).role, "user");
  assert.strictEqual(t.branch.length - t.branch.findIndex((e) => e.id === w.mark), 2, "keep_raw 2");
  assert.deepStrictEqual([w.usage, w.offered, w.overlays], [null, [], []]);
  await until(() => fake_loci.cue_requests("/api/v2/cue/dropped").find((d) => d.体.window === `${T}#w1`), "/cue/dropped");
  const pack_line = present_log().find((l) => l.event === "pack" && l.outcome === "ok");
  assert.deepStrictEqual([pack_line.how, pack_line.lines, pack_line.next], ["forced", 4, `${T}#w2`]);

  // the next turn: system + new carry + the kept lines + her new line (+ its card)
  history = [...history, a(R[2]), u("想去海边")];
  plans.push({ text: R[3], usage: usage(4000) });
  await chat(history);
  const up = chats().at(-1).体.messages;
  assert.strictEqual(up[0].content, SYS.content);
  assert.ok(up[1].content.includes(summary1), "the new carry goes upstream");
  assert.deepStrictEqual(up.slice(2, 5), [u("考完了，好累"), a(R[2]), u("想去海边")]);
  assert.strictEqual(asks().at(-1).体.window, `${T}#w2`, "Loci hears the new window");
  await settled(T, (x) => x.window.usage?.prompt_tokens === 4000, "turn 4");
});

test("a pack in flight: the next turn waits for it and goes with the new window; the earlier summary is folded in", { timeout: 30000 }, async () => {
  // turn 5 reaches 90%: a pack starts, and takes 600 ms
  const summary2 = `第二份摘要：海边。${SENTINEL}`;
  pack_plans.push({ text: summary2, delay_ms: 600 });
  history = [...history, a(R[3]), u("你帮我记着")];
  plans.push({ text: R[4], usage: usage(9000) });
  await chat(history);
  await until(() => packs().length === 2, "the second pack is in flight");
  assert.strictEqual(thread(T).window.no, 2, "not flipped yet");

  // her next turn arrives while it runs: it waits, then goes with window 3
  history = [...history, a(R[4]), u("还在吗")];
  plans.push({ text: "在呢。", usage: usage(2000) });
  const t0 = Date.now();
  const r = await chat(history);
  assert.strictEqual(r.status, 200);
  assert.ok(Date.now() - t0 >= 300, "the turn waited for the pack");
  const up = chats().at(-1).体.messages;
  assert.ok(up[1].content.includes(summary2), "it went with the new carry");
  assert.ok(!up[1].content.includes(summary1), "the old summary is replaced, not stacked");
  assert.strictEqual(asks().at(-1).体.window, `${T}#w3`);
  const shell = packs()[1].体.messages.at(-1).content;
  assert.ok(shell.includes(`上一扇窗收尾时你写的摘要`) && shell.includes(summary1), "the earlier summary goes into the pack");
  for (const s of ["ta：考完了，好累", `你：${R[2]}`]) assert.ok(shell.includes(s), s);
  assert.ok(!shell.includes("ta：你好"), "lines before the window's mark are not sent again");
  assert.match(gateway.全部输出(), /waited for the pack/);
  await settled(T, (x) => x.window.usage?.prompt_tokens === 2000, "turn 6");
});

test("a pack slower than the wait: the turn goes as it is, the pack still flips afterwards", { timeout: 30000 }, async () => {
  const before = thread(T).window;
  // a turn at 90% again, with a pack that takes 3 s (the wait is 1.5 s)
  history = [...history, a("在呢。"), u("讲个故事")];
  plans.push({ text: "从前有座山。", usage: usage(9000) });
  pack_plans.push({ text: `第三份摘要。${SENTINEL}`, delay_ms: 3000 });
  await chat(history);
  await until(() => packs().length === 3, "the third pack is in flight");

  history = [...history, a("从前有座山。"), u("然后呢")];
  plans.push({ text: "山里有座庙。", usage: usage(2000) });
  const t0 = Date.now();
  const r = await chat(history);
  const waited = Date.now() - t0;
  assert.strictEqual(r.status, 200);
  assert.ok(waited >= 1400 && waited < 2900, `waited ${waited} ms: the wait, not the pack`);
  const up = chats().at(-1).体.messages;
  assert.strictEqual(up[1].content, before.carry, "as it is: the window it had");
  assert.strictEqual(asks().at(-1).体.window, before.name);
  assert.match(gateway.全部输出(), /pack still running after 1500 ms: as it is/);
  const t = await settled(T, (x) => x.window.no === before.no + 1, "the late flip", 6000);
  assert.strictEqual(t.window.opened_by.how, "forced");
  assert.ok(t.window.carry.includes("第三份摘要"));
});

test("「现在压」: queued, then a flip with how manual; a second call while it runs → 409", { timeout: 30000 }, async () => {
  const before = thread(T).window;
  pack_plans.push({ text: `按了按钮的摘要。${SENTINEL}`, delay_ms: 800 });
  const first = await call("POST", "/present/compress", {});
  assert.strictEqual(first.status, 200, first.text);
  assert.deepStrictEqual(first.json, { ok: true, queued: true, thread: T });
  const second = await call("POST", "/present/compress", { thread: T });
  assert.strictEqual(second.status, 409);
  assert.strictEqual(second.json.ok, false);
  const t = await settled(T, (x) => x.window.no === before.no + 1, "the manual flip");
  assert.strictEqual(t.window.opened_by.how, "manual");
  const st = await call("GET", "/present");
  assert.strictEqual(st.json.status.compress.last.how, "manual");
  assert.strictEqual(st.json.status.compress.last.how_words, "你按的");
  const h = await call("GET", "/health");
  const c = h.json.present.compress;
  assert.strictEqual(c.state, "ok");
  assert.strictEqual(c.last_how, "manual");
  assert.strictEqual(c.failures_since_ok, 0);
  assert.strictEqual(typeof c.last_ok_seconds_ago, "number");
});

let W = null;
const long = (tag) => `${tag}${"长".repeat(1500)}`;
let wall_history = [];

/** Her next long line on the wall conversation; on a 200 his reply (as the client got it) joins the history. */
async function wall_turn(tag, { again = false } = {}) {
  if (!again) wall_history = [...wall_history, u(long(tag))];
  const r = await chat(wall_history, { model: "wall-model" });
  return { ...r, reply: r.status === 200 ? text_of_sse(r.text) : null };
}
const wall_reply = (r) => { wall_history = [...wall_history, a(r.reply)]; };

test("🔴 the wall: context_length_exceeded → cut once, the resend succeeds, one 「撞墙」 line, 8192 learned and used", { timeout: 30000 }, async () => {
  clock.advance(10 * 60 * 1000);
  const patched = await call("POST", "/present", { patch: { compress: { context_tokens: null } } });
  assert.strictEqual(patched.status, 200, patched.text);
  wall_mode = "limit";
  wall_history = [SYS];
  for (const tag of ["墙一", "墙二", "墙三"]) {
    const r = await wall_turn(tag);
    assert.strictEqual(r.status, 200, r.text);
    wall_reply(r);
    if (!W) W = (await until(() => threads().find((t) => t.window?.model === "wall-model"), "the wall conversation")).id;
    await settled(W, (t) => t.branch.at(-1)?.role === "assistant" && t.branch.length === wall_history.length - 1, `wall turn ${tag}`);
  }
  assert.ok(!present_log().some((l) => l.event === "撞墙"), "under the limit so far");

  // the next turn is over 8192: refused, cut, resent
  pack_plans.push({ status: 500, error: "pack refused" });   // the pack the wall asks for fails (paid): it must back off
  const before_n = chats().length;
  const r = await wall_turn("墙四");
  assert.strictEqual(r.status, 200, r.text);
  assert.match(r.reply, /^墙那边的回话 \d+回+$/, "the client gets the resend's answer");
  wall_reply(r);
  const sent = chats().slice(before_n);
  assert.strictEqual(sent.length, 2, "one refused request, one resend");
  const [refused, resent] = sent.map((s) => s.体.messages);
  assert.ok(estimate_prompt({ messages: refused }) > 8192);
  assert.ok(estimate_prompt({ messages: resent }) <= Math.floor(8192 * 0.85), "cut to under the force line");
  assert.deepStrictEqual(resent[0], SYS, "the client's system stays");
  assert.ok(resent.some((m) => m.content === long("墙四")), "her line stays");
  assert.ok(!resent.some((m) => m.content === long("墙一")), "the oldest line went first");
  assert.strictEqual(resent.find((m) => m.role !== "system").role, "user", "what is kept starts on a line of hers");

  const walls = present_log().filter((l) => l.event === "撞墙");
  assert.strictEqual(walls.length, 1);
  assert.deepStrictEqual([walls[0].limit, walls[0].learned, walls[0].resend, walls[0].pattern],
    [8192, { tokens: 8192, how: "error" }, "ok", "context_length_exceeded"]);
  const learned = JSON.parse(fs.readFileSync(path.join(data_root, "context_windows.json"), "utf8")).learned["wall-model"];
  assert.deepStrictEqual([learned.tokens, learned.how], [8192, "error"]);

  const t = await settled(W, (x) => x.branch.length === 8 && x.window.usage?.context_tokens === 8192, "the fill after the wall");
  assert.strictEqual(t.window.usage.context_source, "learned");
  const used = estimate_prompt({ messages: resent });
  assert.strictEqual(t.window.usage.prompt_tokens, used);
  assert.strictEqual(t.window.usage.fill_pct, Math.round((used * 1000) / 8192) / 10, "the fill % uses 8192");
  const st = await call("GET", "/present");
  assert.deepStrictEqual(st.json.status.compress.context, { tokens: 8192, source: "learned" });

  // the pack the wall asked for failed (paid): counted, and not retried on the next wall
  await until(() => present_log().some((l) => l.event === "pack" && l.thread === W && l.outcome === "paid"), "the failed pack");
  const h = await call("GET", "/health");
  assert.strictEqual(h.json.present.compress.failures_since_ok, 1);
  assert.strictEqual(h.json.present.compress.waiting_to_retry, 1);
});

test("never stuck: a resend that fails too returns upstream's error, and the next turn still escapes", { timeout: 30000 }, async () => {
  const packs_before = packs().length;
  wall_mode = "always";
  const r = await wall_turn("墙五");
  assert.strictEqual(r.status, 400, "upstream's error, as today");
  assert.match(r.text, /maximum context length is 8192 tokens/);
  const t = await settled(W, (x) => x.window.usage?.estimated === true, "the fill moved instead of freezing");
  assert.ok(t.window.usage.fill_pct > 85);
  const walls = present_log().filter((l) => l.event === "撞墙");
  assert.strictEqual(walls.length, 2);
  assert.match(walls[1].resend, /^wall_again_400$/);

  // the same turn again, upstream back to its real limit: it escapes
  wall_mode = "limit";
  const again = await wall_turn("墙五", { again: true });
  assert.strictEqual(again.status, 200, again.text);
  assert.strictEqual(present_log().filter((l) => l.event === "撞墙").at(-1).resend, "ok");
  assert.strictEqual(packs().length, packs_before, "the backed-off pack was not paid for again on every wall");
});

test("🔴 sentinel: 0 times in client bytes, the day store, /present, /health and the console", async () => {
  client_bytes.push((await call("GET", "/present")).text, (await call("GET", "/health")).text);
  assert.ok(client_bytes.length > 15);
  assert.deepStrictEqual(client_bytes.filter((b) => b.includes(SENTINEL_KEY)), []);
  for (const n of fs.readdirSync(days_dir)) {
    assert.ok(!fs.readFileSync(path.join(days_dir, n), "utf8").includes(SENTINEL_KEY), `day file ${n}`);
  }
  assert.ok(!gateway.全部输出().includes(SENTINEL_KEY), "the console");
  assert.ok(!fs.readFileSync(path.join(data_root, "logs", "present.jsonl"), "utf8").includes(SENTINEL_KEY), "the log");
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

// ———— in-process ————

test("wall patterns: the common shapes are recognised, the limit read when it is there; other errors are not walls", () => {
  const hit = (status, text) => wall_error(status, text);
  const cases = [
    [400, '{"error":{"message":"This model\'s maximum context length is 8192 tokens. However, your messages resulted in 9100 tokens.","code":"context_length_exceeded"}}', 8192, 9100],
    [400, '{"error":{"code":"context_length_exceeded","message":"Input too long"}}', null, null],
    [400, '{"type":"error","error":{"type":"invalid_request_error","message":"prompt is too long: 210345 tokens > 200000 maximum"}}', 200000, 210345],
    [400, "input length and `max_tokens` exceed context limit: 187254 + 20000 > 200000, decrease input length or `max_tokens` and try again", 200000, 187254],
    [400, "The input token count (1300000) exceeds the maximum number of tokens allowed (1048576).", 1048576, 1300000],
    [400, "Invalid request: Your request exceeded model token limit: 131072", 131072, null],
    [400, "Prompt contains 40000 tokens and 0 draft tokens, too large for model with 32768 maximum context length", 32768, 40000],
    [400, "Range of input length should be [1, 30720]", 30720, null],
    [413, "request exceeds the context window of this model", null, null],
    [400, "the input length exceeds the context length", null, null],
    [400, "prompt 超长，请缩短后重试", null, null],
    [400, "输入的上下文长度超过限制", null, null],
  ];
  for (const [status, text, limit, actual] of cases) {
    const got = hit(status, text);
    assert.strictEqual(got.hit, true, text);
    assert.strictEqual(got.limit, limit, text);
    assert.strictEqual(got.actual, actual, text);
  }
  for (const [status, text] of [
    [429, "Request too large for gpt-4o on tokens per min (TPM): Limit 30000, Requested 50000"],
    [401, "maximum context length is 8192 tokens"],
    [500, "context_length_exceeded"],
    [400, "The model `gone` does not exist"],
    [400, "max_tokens is too large: 9000. This model supports at most 4096 completion tokens"],
  ]) assert.strictEqual(hit(status, text).hit, false, text);
});

test("the cut: whole turns from the oldest, never the head or the current turn, starting on her line", () => {
  const big = (s) => s + "字".repeat(1000);
  const msgs = [
    { role: "system", content: "sys" }, { role: "system", content: "carry" },
    { role: "user", content: big("一") }, { role: "system", content: "card1" }, { role: "assistant", content: big("回一") },
    { role: "system", content: "poke2" }, { role: "user", content: big("二") }, { role: "assistant", content: big("回二") },
    { role: "user", content: big("三") }, { role: "system", content: "card3" },
  ];
  const names = (r) => r.messages.map((m) => m.content.replace(/字+$/, ""));
  const once = cut_oldest(msgs, { keep_head: 2, target: 3500 });
  assert.deepStrictEqual(names(once), ["sys", "carry", "poke2", "二", "回二", "三", "card3"], "the overlay before her line stays with it");
  assert.strictEqual(once.dropped, 3);
  const all = cut_oldest(msgs, { keep_head: 2, target: 10 });
  assert.deepStrictEqual(names(all), ["sys", "carry", "三", "card3"], "the head and the current turn always stay");
  const none = cut_oldest(msgs, { keep_head: 2, target: 1e9 });
  assert.strictEqual(none.dropped, 0);
});

test("pack pieces: markers stripped, the plan covers the mark to the kept tail, lines formatted", () => {
  assert.strictEqual(clean_summary(`  ${OPEN}要的${CLOSE}  `), "要的");
  assert.strictEqual(clean_summary("就是正文"), "就是正文");
  const branch = ["user", "assistant", "user", "assistant", "user", "assistant"].map((role, i) => ({ id: `m_${i}`, role }));
  assert.deepStrictEqual(plan_pack({ branch, window: { mark: null } }, 2), { mark: "m_4", ids: ["m_0", "m_1", "m_2", "m_3"] });
  assert.deepStrictEqual(plan_pack({ branch, window: { mark: "m_2" } }, 2), { mark: "m_4", ids: ["m_2", "m_3"] });
  assert.deepStrictEqual(plan_pack({ branch, window: { mark: "m_4" } }, 2).ids, [], "nothing between the mark and the tail");
  assert.deepStrictEqual(plan_pack({ branch, window: { mark: null } }, 3), { mark: "m_4", ids: ["m_0", "m_1", "m_2", "m_3"] }, "the tail starts on her line");
  assert.strictEqual(format_line({ role: "user", text: "考完了！", at: "2026-10-07T20:41:05+08:00", attach: ["image"] }), "[10-07 20:41] ta：考完了！ [image]");
  assert.strictEqual(format_line({ role: "assistant", text: "", at: "2026-10-07T20:42:00+08:00", tools: ["recall"] }), "[10-07 20:42] 你：（调了工具：recall）");
});

test("her arrival aborts a wake, never a pack: the pack runs on to its answer", { timeout: 20000 }, async () => {
  const dir = path.join(root, "own");
  fs.mkdirSync(dir, { recursive: true });
  const runner = create_own_turn({ env: {}, data_root: dir, upstream: fake_upstream.地址, loci_address: fake_loci.地址,
                                   read_own: () => ({ tool_rounds: 6, models: [] }), log: () => {} });
  runner.remember_owner({ headers: { authorization: "Bearer k-own" }, model: "fake-model" });
  const msg = [{ role: "user", content: "【换窗 · 测试】" }];
  pack_plans.push({ text: "慢慢写完的摘要", delay_ms: 300 });
  const p = runner.run({ kind: "pack", messages: msg });
  await new Promise((r) => setTimeout(r, 50));
  assert.strictEqual(runner.abort("owner_arrived"), false, "arrival does not stop a pack");
  const res = await p;
  assert.deepStrictEqual([res.outcome, res.text], ["ok", "慢慢写完的摘要"]);

  pack_plans.push({ text: "不该说完", delay_ms: 2000 });
  const w = runner.run({ kind: "wake", messages: msg });
  await new Promise((r) => setTimeout(r, 50));
  assert.strictEqual(runner.abort("owner_arrived"), true, "arrival stops a wake");
  assert.deepStrictEqual([(await w).outcome, (await w).reason], ["aborted", "owner_arrived"]);
  pack_plans.length = 0;
});
