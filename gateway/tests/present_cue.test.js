// ============================================================
// gateway/tests/present_cue.test.js — cards, overlays, the window and usage, through the
// real gateway
//
// The gateway runs as a black-box child process (start_gateway.js) with a fake upstream,
// a fake Loci, the network fence, a file-driven fake clock and LOCI_TZ pinned. A fake
// client sends its whole history every turn, the way a chat app does.
//
// What is being proved (construction steps 2 and 3):
//   · round N's card is still in round N+1, at the same position, with the same bytes
//   · /cue/delivered once per turn; a resend of the same turn gets the same card, no
//     second ask and no second delivered
//   · Loci unreachable: the request still goes through
//   · upstream receives client system + carry + the lines from the mark on; a mark the
//     history no longer holds is void and the history goes as sent
//   · the stream the client receives is upstream's, less the usage chunk it did not ask
//     for; the fill % is right, its window size says where it came from
//   · 🔴 a sentinel planted in a carry never reaches a byte bound for the client
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
const { to_said } = require("../present/threads.js");

const root = fs.mkdtempSync(path.join(os.tmpdir(), "loci-present-cue-"));
const ledger_path = path.join(root, "fence.jsonl");
const SYS = { role: "system", content: "你是一个温柔的伴侣。" };
const OUR_PORTS = new Set();
const gateways = [];
const u = (content) => ({ role: "user", content });
const a = (content) => ({ role: "assistant", content });

let fake_upstream, fake_loci, gateway, clock;
const plans = [];

function shut_poke_gate(dir) {
  fs.mkdirSync(path.join(dir, "state"), { recursive: true });
  fs.writeFileSync(path.join(dir, "state", "poke-window.json"),
    JSON.stringify({ lastUserMessageTime: new Date().toISOString(), wakePending: false }));
}

async function boot(dir, { prepare = null } = {}) {
  shut_poke_gate(dir);
  if (prepare) prepare(dir);
  const g = await start_gateway({
    端口: 0,
    上游地址: fake_upstream.地址,
    loci地址: fake_loci.地址,
    数据根: dir,
    相关超时毫秒: 1200,
    白名单端口: [fake_upstream.端口, fake_loci.端口],
    账本路径: ledger_path,
    extra_env: {
      LOCI_TZ: "Asia/Shanghai",
      LOCI_GATEWAY_TEST_CLOCK: clock.file,
      LOCI_GATEWAY_DAYS: path.join(dir, "library", "_hosts", "gateway"),
      LOCI_CUE_TIMEOUT_MS: "1200",
    },
  });
  gateways.push(g);
  OUR_PORTS.add(g.端口);
  fence.allow(g.端口);
  return g;
}

before(async () => {
  fake_upstream = await start_fake_upstream({ 端口: 0 });
  fake_loci = await start_fake_loci({ 端口: 0 });
  OUR_PORTS.add(fake_upstream.端口); OUR_PORTS.add(fake_loci.端口);
  fake_upstream.reply_with(() => plans.shift() || { text: "（没安排的回话）" });
  fake_upstream.set_models({ object: "list", data: [{ id: "big-model", context_length: 200000 }] });
  clock = create_file_clock(path.join(root, "clock.txt"), Date.parse("2026-10-07T12:41:05Z"));
  gateway = await boot(path.join(root, "main"));
});

after(async () => {
  for (const g of gateways) await g.停();
  if (fake_upstream) await fake_upstream.关();
  if (fake_loci) await fake_loci.关();
  if (process.env.留下现场) console.log(`[留下现场] ${root}`);
  else fs.rmSync(root, { recursive: true, force: true });
});

async function chat(history, { stream = true, to = gateway, model = "fake-model", extra = {} } = {}) {
  const resp = await fetch(`${to.地址}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "Bearer test-key" },
    body: JSON.stringify({ model, stream, messages: history, ...extra }),
  });
  const headers = {};
  resp.headers.forEach((v, k) => { headers[k] = v; });
  return { status: resp.status, text: await resp.text(), headers };
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

const asks = () => fake_loci.cue_requests("/api/v2/cue");
const delivered = () => fake_loci.cue_requests("/api/v2/cue/delivered");
const upstream_bodies = () => fake_upstream.收到.map((r) => r.体);
function thread_file(dir, id) { return JSON.parse(fs.readFileSync(path.join(dir, "threads", `${id}.json`), "utf8")); }
function threads_in(dir) {
  return fs.readdirSync(path.join(dir, "threads")).filter((n) => n.endsWith(".json")).map((n) => thread_file(dir, n.slice(0, -5)));
}

const A1 = "你好呀。今天过得怎么样？我一直在这儿等你来说话。";
const A2 = "考试辛苦了！先别想分数，今晚好好吃一顿，睡个好觉。";
const A3 = "那就好好歇一歇，明天的事明天再说，我陪着你。";

// ————————————————————————————————————————————————————————————

test("acceptance: round N's card is still there in round N+1, same place, same bytes; delivered once per turn", { timeout: 20000 }, async () => {
  const asks_before = asks().length;
  const h1 = [SYS, u("你好")];
  plans.push({ text: A1 });
  await chat(h1);
  const h2 = [...h1, a(A1), u("今天考试了")];
  plans.push({ text: A2 });
  await chat(h2);
  const h3 = [...h2, a(A2), u("考完了，好累")];
  plans.push({ text: A3 });
  await chat(h3);

  const mine = asks().slice(asks_before);
  assert.strictEqual(mine.length, 3, "one ask per turn");
  const window = mine[0].体.window;
  assert.match(window, /^t_[0-9a-f]{6}#w1$/);
  assert.ok(mine.every((r) => r.体.window === window), "one conversation, one window");
  assert.deepStrictEqual(mine.map((r) => r.体.text), ["你好", "今天考试了", "考完了，好累"]);
  const turns = mine.map((r) => r.体.turn);
  assert.ok(turns.every((t) => /^m_20261007_\d{4}$/.test(t)), JSON.stringify(turns));

  const [b1, b2, b3] = upstream_bodies().slice(-3);
  const card_at = (body) => body.messages.map((m, i) => (m.role === "system" && /〔相关记忆〕/.test(m.content) ? i : -1)).filter((i) => i >= 0);
  assert.deepStrictEqual(card_at(b1), [2], "the card sits right after her line");
  assert.deepStrictEqual(card_at(b2), [2, 5]);
  assert.deepStrictEqual(card_at(b3), [2, 5, 8]);
  assert.deepStrictEqual(b3.messages.map((m) => m.role), ["system", "user", "system", "assistant", "user", "system", "assistant", "user", "system"]);
  assert.ok(b3.messages[2].content.includes(turns[0]) && b3.messages[5].content.includes(turns[1]) && b3.messages[8].content.includes(turns[2]));

  // byte for byte: each request starts with the previous request's messages, as raw text on the wire
  const raws = fake_upstream.收到.slice(-3).map((r) => r.原文);
  for (const [prev, next] of [[b1, raws[1]], [b2, raws[2]]]) {
    const prefix = JSON.stringify(prev.messages).slice(0, -1);
    assert.ok(next.includes(`"messages":${prefix},`), "the next turn's upstream bytes carry the previous turn's prefix unchanged");
  }

  await until(() => delivered().filter((r) => turns.includes(r.体.turn)).length >= 3, "three delivered");
  await new Promise((r) => setTimeout(r, 150));
  const confirmed = delivered().filter((r) => turns.includes(r.体.turn));
  assert.deepStrictEqual(confirmed.map((r) => r.体), turns.map((turn) => ({ window, turn })), "exactly once per turn, in order");
});

test("a resend of the same turn gets the same card, no second ask and no second delivered", { timeout: 20000 }, async () => {
  clock.advance(10 * 60 * 1000);
  const h = [SYS, u("重发测试：这一句会发三次")];
  plans.push({ status: 500, error: "upstream fell over" });
  const failed = await chat(h);
  assert.strictEqual(failed.status, 500);
  const ask = asks().at(-1);
  assert.strictEqual(ask.体.text, "重发测试：这一句会发三次");
  const turn = ask.体.turn;
  await new Promise((r) => setTimeout(r, 150));
  assert.strictEqual(delivered().filter((r) => r.体.turn === turn).length, 0, "upstream refused: nothing was delivered");

  plans.push({ text: "这次接住了，你说的那句我听见了。" });
  const resent = await chat(h);
  assert.strictEqual(resent.status, 200);
  plans.push({ text: "换个说法再回一次：我听见了，真的。" });
  await chat(h);   // a regenerate of the same turn

  assert.strictEqual(asks().filter((r) => r.体.turn === turn).length, 1, "the same turn is asked once");
  const bodies = upstream_bodies().slice(-3);
  const cards = bodies.map((b) => JSON.stringify(b.messages.at(-1)));
  assert.ok(cards[0].includes(turn));
  assert.strictEqual(cards[1], cards[0]);
  assert.strictEqual(cards[2], cards[0]);
  await until(() => delivered().some((r) => r.体.turn === turn), "delivered after the resend went through");
  await new Promise((r) => setTimeout(r, 200));
  assert.strictEqual(delivered().filter((r) => r.体.turn === turn).length, 1, "delivered once, not once per request");
});

test("Loci unreachable: the request still goes through; the turn asks again next time", { timeout: 20000 }, async () => {
  clock.advance(10 * 60 * 1000);
  fake_loci.设模式("断连");
  try {
    const h = [SYS, u("Loci 不在的时候说的话")];
    plans.push({ text: "嗯，我在呢，你接着说。" });
    const r = await chat(h, { stream: false });
    assert.strictEqual(r.status, 200);
    assert.strictEqual(r.text, fake_upstream.sent.at(-1));
    assert.deepStrictEqual(upstream_bodies().at(-1).messages, h, "no card: the history as sent");
    fake_loci.设模式("正常");
    plans.push({ text: "这回再说一遍，我还在。" });
    await chat(h, { stream: false });   // same request again
    const last = upstream_bodies().at(-1).messages;
    assert.strictEqual(last.length, 3);
    assert.match(last[2].content, /〔相关记忆〕/, "the retry asked Loci again and got the card");
  } finally { fake_loci.设模式("正常"); }
});

test("usage: asked for on the client's behalf, the usage chunk stripped, every other byte as upstream sent it; fill % right", { timeout: 20000 }, async () => {
  clock.advance(10 * 60 * 1000);
  // wait for the background model-list fetch the first turn of this file set off
  const cache = path.join(root, "main", "context_windows.json");
  await until(() => fs.existsSync(cache) && JSON.parse(fs.readFileSync(cache, "utf8")).provider["big-model"], "the provider's model list");
  assert.ok(fake_upstream.models_requests.length >= 1);
  assert.strictEqual(fake_upstream.models_requests[0].头.authorization, "Bearer test-key", "asked with the client's own key");

  const h = [SYS, u("用量测试")];
  plans.push({ text: "收到用量测试。", usage: { prompt_tokens: 74210, completion_tokens: 9, total_tokens: 74219 } });
  const r = await chat(h, { model: "big-model" });
  const up = upstream_bodies().at(-1);
  assert.deepStrictEqual(up.stream_options, { include_usage: true });
  assert.notStrictEqual(r.text, fake_upstream.sent.at(-1), "upstream did send a usage chunk");
  assert.strictEqual(r.text, fake_upstream.without_usage_chunk.at(-1), "the client gets upstream's bytes, less that one chunk");

  const thread = await until(() => threads_in(path.join(root, "main")).find((t) => t.window?.usage?.prompt_tokens === 74210), "usage recorded");
  assert.deepStrictEqual(thread.window.usage.estimated, false);
  assert.strictEqual(thread.window.usage.context_tokens, 200000);
  assert.strictEqual(thread.window.usage.context_source, "provider");
  assert.strictEqual(thread.window.usage.fill_pct, Math.round(74210 * 1000 / 200000) / 10);
  assert.strictEqual(thread.window.usage.fill_pct, 37.1);
});

test("usage: a client that asked itself gets the whole stream; usage on a choice chunk stays; non-stream untouched", { timeout: 20000 }, async () => {
  plans.push({ text: "你自己要的用量。" });
  const own = await chat([SYS, u("我自己要用量")], { extra: { stream_options: { include_usage: true } } });
  assert.strictEqual(own.text, fake_upstream.sent.at(-1));

  plans.push({ text: "用量跟在最后一块里。", usage_in_last_choice: true });
  const in_choice = await chat([SYS, u("用量在最后一块")]);
  assert.strictEqual(in_choice.text, fake_upstream.sent.at(-1));

  plans.push({ text: "不流式的回话。" });
  const plain = await chat([SYS, u("不流式")], { stream: false });
  assert.strictEqual(plain.text, fake_upstream.sent.at(-1));
  assert.strictEqual(upstream_bodies().at(-1).stream_options, undefined, "nothing asked for on a non-streamed request");
});

test("usage: a provider that never reports it → the fill is estimated from the characters sent, and says so", { timeout: 20000 }, async () => {
  plans.push({ text: "这家不报用量。", usage: null });
  const r = await chat([SYS, u("不报用量的那家，模型是 deepseek")], { model: "deepseek-chat" });
  assert.strictEqual(r.text, fake_upstream.sent.at(-1), "no usage chunk came, nothing was taken out");
  const thread = await until(() => threads_in(path.join(root, "main")).find((t) => t.window?.usage?.model === "deepseek-chat"), "usage recorded");
  const usage = thread.window.usage;
  assert.strictEqual(usage.estimated, true);
  assert.ok(usage.prompt_tokens > 0);
  assert.strictEqual(usage.context_tokens, 128000);
  assert.strictEqual(usage.context_source, "table");
  assert.strictEqual(usage.fill_pct, Math.round(usage.prompt_tokens * 1000 / 128000) / 10);
});

// ———— the mark and the carry ————

const SENTINEL = "哨兵·CARRY-7f3a9e-千万别漏出去";
const T = "t_c0ffee";
const LINES = [
  { id: "m_20261007_0001", role: "user", text: "我们去年夏天去了海边" },
  { id: "m_20261007_0002", role: "assistant", text: "记得，那天的晚霞特别好看，你还捡了好多贝壳回来。" },
  { id: "m_20261007_0003", role: "user", text: "今年还想去" },
  { id: "m_20261007_0004", role: "assistant", text: "好啊，那就挑个不那么热的周末，我们再去一次海边。" },
];

function plant(dir) {
  const days = path.join(dir, "library", "_hosts", "gateway", "days");
  fs.mkdirSync(days, { recursive: true });
  fs.writeFileSync(path.join(days, "2026-10-07.jsonl"), LINES.map((l) => JSON.stringify({
    id: l.id, rev: 1, at: "2026-10-07T20:00:00+08:00", thread: T, role: l.role, text: l.text,
    attach: null, tools: null, woke: false, state: "live" })).join("\n") + "\n");
  fs.mkdirSync(path.join(dir, "threads"), { recursive: true });
  fs.writeFileSync(path.join(dir, "threads", `${T}.json`), JSON.stringify({
    id: T, created_at: 0, last_at: 0,
    branch: LINES.map((l) => ({ id: l.id, role: l.role, fp: to_said({ role: l.role, content: l.text }).fp })),
    window: { no: 2, name: `${T}#w2`, opened_at: 0, mark: "m_20261007_0003", carry: `〔前情〕${SENTINEL}`,
              overlays: [], usage: null, model: null },
  }));
}

const said = LINES.map((l) => ({ role: l.role, content: l.text }));

test("🔴 carry: upstream gets client system + carry + lines from the mark on; the sentinel never reaches the client", { timeout: 30000 }, async () => {
  clock.advance(10 * 60 * 1000);
  const dir = path.join(root, "carried");
  const g = await boot(dir, { prepare: plant });
  const client_bytes = [];

  const h = [SYS, ...said, u("那就下个月吧")];
  plans.push({ text: "下个月好，我去查查那边的天气。" });
  const streamed = await chat(h, { to: g });
  client_bytes.push(streamed.text, JSON.stringify(streamed.headers));
  const up = upstream_bodies().at(-1);
  const ask = asks().at(-1);
  assert.strictEqual(ask.体.window, `${T}#w2`, "the window Loci hears about is the planted one");
  assert.deepStrictEqual(up.messages.slice(0, 5), [SYS, { role: "system", content: `〔前情〕${SENTINEL}` }, said[2], said[3], u("那就下个月吧")]);
  assert.strictEqual(up.messages.length, 6);
  assert.match(up.messages[5].content, /〔相关记忆〕/);
  assert.strictEqual(streamed.text, fake_upstream.without_usage_chunk.at(-1));

  plans.push({ text: "不流式也一样，前情只给模型看。" });
  const h2 = [...h, a("下个月好，我去查查那边的天气。"), u("不流式再说一句")];
  const plain = await chat(h2, { to: g, stream: false });
  client_bytes.push(plain.text, JSON.stringify(plain.headers));
  assert.strictEqual(upstream_bodies().at(-1).messages[1].content, `〔前情〕${SENTINEL}`);
  assert.strictEqual(plain.text, fake_upstream.sent.at(-1));

  // the client trimmed its history past the mark: void for this request, sent as it is (no carry)
  plans.push({ text: "嗯，你接着说，我听着呢。" });
  const trimmed = [SYS, said[3], u("客户端把前面截掉了")];
  const t3 = await chat(trimmed, { to: g });
  client_bytes.push(t3.text);
  const up3 = upstream_bodies().at(-1).messages;
  assert.deepStrictEqual(up3.slice(0, 3), trimmed);
  assert.ok(!JSON.stringify(up3).includes(SENTINEL), "a void mark sends no carry");

  // the owner rewound to before the mark and edited: a fork with a window of its own, sent as it is
  plans.push({ text: "改成明年也行，不着急。" });
  const rewound = [SYS, said[0], said[1], u("今年不去了，明年再说")];
  const t4 = await chat(rewound, { to: g });
  client_bytes.push(t4.text);
  const up4 = upstream_bodies().at(-1).messages;
  assert.deepStrictEqual(up4.slice(0, 4), rewound);
  assert.ok(!JSON.stringify(up4).includes(SENTINEL));
  assert.notStrictEqual(asks().at(-1).体.window, `${T}#w2`, "the fork speaks for a window of its own");

  for (const bytes of client_bytes) assert.ok(!bytes.includes(SENTINEL) && !bytes.includes("CARRY-7f3a9e"), "the carry reached a byte bound for the client");
  const days = path.join(dir, "library", "_hosts", "gateway", "days", "2026-10-07.jsonl");
  await new Promise((r) => setTimeout(r, 200));
  assert.ok(!fs.readFileSync(days, "utf8").includes("CARRY-7f3a9e"), "the carry never lands in a day file");
  assert.ok(!g.全部输出().includes("CARRY-7f3a9e"), "nor on the console");
  assert.strictEqual(thread_file(dir, T).window.carry, `〔前情〕${SENTINEL}`, "the carry stays where it lives");
});

test("reconciliation: every outbound connection stayed inside this run", () => {
  const entries = fs.existsSync(ledger_path)
    ? fs.readFileSync(ledger_path, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
  assert.ok(entries.length > 0);
  assert.deepStrictEqual(entries.filter((e) => !OUR_PORTS.has(e.端口)), []);
  assert.ok(entries.every((e) => e.放行), "nothing had to be blocked");
  assert.deepStrictEqual(fake_loci.全程收到.filter((r) => r.路径 === "/mcp"), [], "the chat path no longer calls any MCP tool");
});
