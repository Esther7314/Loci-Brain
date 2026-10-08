// ============================================================
// gateway/tests/present_recording.test.js — the day store through the real gateway
//
// The gateway runs as a black-box child process (start_gateway.js) with a fake upstream,
// a fake Loci, the network fence, a file-driven fake clock, LOCI_TZ pinned, and its day
// files pointed into this run's temp directory. A fake client plays a chat app: it sends
// the whole history every time, streams some answers and not others.
//
// What is being proved (construction step 1's acceptance, for a gateway with no tag —
// every chat request that comes in counts):
//   · 3 rounds + one resend + one regenerate → that day's file holds exactly 6 live
//     lines + 1 replaced (current version of each id)
//   · a second, unrelated conversation — opening with the very same words — goes into
//     its own thread
//   · requests to non-chat paths write nothing
//   · the client receives the upstream's bytes unchanged while the reply is being read
//   · with LOCI_GATEWAY_DAYS unset, day files stay inside LOCI_GATEWAY_DATA
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
const { fold_current } = require("../present/day_store.js");

const root = fs.mkdtempSync(path.join(os.tmpdir(), "loci-present-gateway-"));
const data_root = path.join(root, "data");
const host_dir = path.join(root, "library", "_hosts", "gateway");
const ledger_path = path.join(root, "fence.jsonl");
const DAY = "2026-10-07";
const day_file = path.join(host_dir, "days", `${DAY}.jsonl`);
const SYS = { role: "system", content: "你是一个温柔的伴侣。" };
const OUR_PORTS = new Set();
const gateways = [];

let fake_upstream, fake_loci, gateway, clock;
const plans = [];   // what the fake upstream answers next, in order

function shut_poke_gate(dir) {
  // Poke delivery rides in every chat request; with its idle gate shut it never knocks on Loci.
  fs.mkdirSync(path.join(dir, "state"), { recursive: true });
  fs.writeFileSync(path.join(dir, "state", "poke-window.json"),
    JSON.stringify({ lastUserMessageTime: new Date().toISOString(), wakePending: false }));
}

async function boot(dir, extra_env) {
  shut_poke_gate(dir);
  const g = await start_gateway({
    端口: 0,
    上游地址: fake_upstream.地址,
    loci地址: fake_loci.地址,
    数据根: dir,
    相关超时毫秒: 1200,
    白名单端口: [fake_upstream.端口, fake_loci.端口],
    账本路径: ledger_path,
    extra_env,
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
  clock = create_file_clock(path.join(root, "clock.txt"), Date.parse("2026-10-07T12:41:05Z"));
  gateway = await boot(data_root, {
    LOCI_TZ: "Asia/Shanghai",
    LOCI_GATEWAY_TEST_CLOCK: clock.file,
    LOCI_GATEWAY_DAYS: host_dir,
  });
});

after(async () => {
  for (const g of gateways) await g.停();
  if (fake_upstream) await fake_upstream.关();
  if (fake_loci) await fake_loci.关();
  if (process.env.留下现场) console.log(`[留下现场] ${root}`);
  else fs.rmSync(root, { recursive: true, force: true });
});

/** The client: posts the whole history, returns the status and the exact body text it got. */
async function chat(history, { stream = false, to = gateway, route = "/v1/chat/completions" } = {}) {
  const resp = await fetch(`${to.地址}${route}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "Bearer test-key" },
    body: JSON.stringify({ model: "fake-model", stream, messages: history }),
  });
  return { status: resp.status, text: await resp.text() };
}

function day_lines(file = day_file) {
  if (!fs.existsSync(file)) return [];
  return fs.readFileSync(file, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l));
}

/** The reply is written when the gateway sees the stream end, a moment after the client does. */
async function until(check, what, timeout_ms = 5000) {
  const deadline = Date.now() + timeout_ms;
  for (;;) {
    if (check()) return;
    if (Date.now() > deadline) throw new Error(`timed out waiting for: ${what}`);
    await new Promise((r) => setTimeout(r, 20));
  }
}
const has_text = (text, file) => () => day_lines(file).some((l) => l.text === text);

function snapshot(dir) {
  const out = {};
  const walk = (d) => {
    if (!fs.existsSync(d)) return;
    for (const name of fs.readdirSync(d)) {
      const p = path.join(d, name);
      const st = fs.statSync(p);
      if (st.isDirectory()) walk(p); else out[path.relative(root, p)] = st.size;
    }
  };
  walk(dir);
  return out;
}

const A1 = "你好呀。今天过得怎么样？我一直在这儿等你来说话。";
const A2 = "考试辛苦了！先别想分数，今晚好好吃一顿，睡个好觉。";
const A3 = "真的考完了！太好了，这一阵你一直绷着，现在可以松口气了。";
const A3_AGAIN = "考完啦！这段时间你真的很努力，我替你高兴，去奖励一下自己吧。";

// ————————————————————————————————————————————————————————————

test("acceptance: 3 rounds + a resend + a regenerate → 6 live lines + 1 replaced", { timeout: 20000 }, async () => {
  const h1 = [SYS, { role: "user", content: "你好" }];
  plans.push({ text: A1 });
  const r1 = await chat(h1, { stream: true });
  assert.strictEqual(r1.status, 200);
  assert.strictEqual(r1.text, fake_upstream.sent.at(-1), "the client gets the upstream's stream byte for byte");
  await until(has_text(A1), "round 1's reply");

  const h2 = [...h1, { role: "assistant", content: A1 }, { role: "user", content: "今天考试了" }];
  plans.push({ text: A2 });
  const r2 = await chat(h2);   // not streamed
  assert.strictEqual(r2.text, fake_upstream.sent.at(-1));
  await until(has_text(A2), "round 2's reply");

  const h3 = [...h2, { role: "assistant", content: A2 }, { role: "user", content: "考完了！" }];
  plans.push({ status: 500, error: "upstream fell over" });
  const failed = await chat(h3, { stream: true });
  assert.strictEqual(failed.status, 500);

  plans.push({ text: A3 });
  const resent = await chat(h3, { stream: true });   // the client sends the same request again
  assert.strictEqual(resent.status, 200);
  await until(has_text(A3), "the resend's reply");

  plans.push({ text: A3_AGAIN });
  const regenerated = await chat(h3, { stream: true });
  assert.strictEqual(regenerated.status, 200);
  await until(has_text(A3_AGAIN), "the regenerated reply");

  const current = fold_current(day_lines());
  const live = current.filter((l) => l.state === "live");
  const replaced = current.filter((l) => l.state === "replaced");
  assert.strictEqual(live.length, 6, JSON.stringify(current, null, 1));
  assert.strictEqual(replaced.length, 1);
  assert.deepStrictEqual(live.map((l) => [l.role, l.text]), [
    ["user", "你好"], ["assistant", A1], ["user", "今天考试了"], ["assistant", A2],
    ["user", "考完了！"], ["assistant", A3_AGAIN],
  ]);
  assert.strictEqual(replaced[0].text, A3);
  assert.deepStrictEqual(live.map((l) => l.id), [
    "m_20261007_0001", "m_20261007_0002", "m_20261007_0003", "m_20261007_0004", "m_20261007_0005", "m_20261007_0007",
  ]);
  assert.strictEqual(replaced[0].id, "m_20261007_0006");
  assert.ok(current.every((l) => l.at.startsWith("2026-10-07T20:41:05+08:00")), "stamped by the fake clock, in LOCI_TZ");
  assert.strictEqual(new Set(current.map((l) => l.thread)).size, 1, "one conversation, one thread");
  assert.ok(!fs.existsSync(path.join(data_root, "host")), "LOCI_GATEWAY_DAYS was set, so nothing lands in the default place");
});

test("a second, unrelated conversation opening with the same words gets its own thread", { timeout: 20000 }, async () => {
  const a_thread = day_lines()[0].thread;
  clock.advance(5 * 60 * 1000);   // past the resend window

  const B1 = "嗨，又见面啦。这次想从哪儿聊起？我都在。";
  const b1 = [SYS, { role: "user", content: "你好" }];
  plans.push({ text: B1 });
  await chat(b1, { stream: true });
  await until(has_text(B1), "chat B's first reply");

  const B2 = "好呀，随便聊聊最好了。你今天有没有遇到什么好玩的事？";
  plans.push({ text: B2 });
  await chat([...b1, { role: "assistant", content: B1 }, { role: "user", content: "随便聊聊" }]);
  await until(has_text(B2), "chat B's second reply");

  const current = fold_current(day_lines());
  const b_lines = current.slice(7);
  assert.deepStrictEqual(b_lines.map((l) => l.text), ["你好", B1, "随便聊聊", B2]);
  const b_thread = b_lines[0].thread;
  assert.notStrictEqual(b_thread, a_thread);
  assert.ok(b_lines.every((l) => l.thread === b_thread), "chat B stays on one thread");
  assert.ok(current.slice(0, 7).every((l) => l.thread === a_thread), "chat A was not touched");
  assert.ok(b_lines.every((l) => l.at.startsWith("2026-10-07T20:46:05+08:00")));
});

test("requests to non-chat paths write nothing", { timeout: 20000 }, async () => {
  const before_snapshot = { ...snapshot(host_dir), ...snapshot(path.join(data_root, "threads")) };
  const say = [SYS, { role: "user", content: "这句不该被记下" }];

  const embeddings = await fetch(`${gateway.地址}/v1/embeddings`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model: "e", input: "这句不该被记下" }),
  });
  assert.strictEqual(embeddings.status, 200);
  await embeddings.text();
  const models = await fetch(`${gateway.地址}/v1/models`);
  await models.text();
  const legacy = await fetch(`${gateway.地址}/v1/completions`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model: "c", prompt: "这句不该被记下" }),
  });
  await legacy.text();
  const off_api = await chat(say, { route: "/chat/completions" });   // chat-shaped, but not /v1/*
  assert.strictEqual(off_api.status, 404);
  const health = await fetch(`${gateway.地址}/health`);
  await health.text();

  await new Promise((r) => setTimeout(r, 200));   // give any stray write the time to land
  const after_snapshot = { ...snapshot(host_dir), ...snapshot(path.join(data_root, "threads")) };
  assert.deepStrictEqual(after_snapshot, before_snapshot);
  assert.ok(!day_lines().some((l) => l.text.includes("不该被记下")));
});

test("with LOCI_GATEWAY_DAYS unset, day files stay inside LOCI_GATEWAY_DATA", { timeout: 20000 }, async () => {
  const other_root = path.join(root, "data-default");
  const other = await boot(other_root, { LOCI_TZ: "Asia/Shanghai", LOCI_GATEWAY_TEST_CLOCK: clock.file });
  await until(() => other.全部输出().includes(`day files      ${path.join(other_root, "host", "days")}`),
    "the banner line saying where the day files go");
  const text = "默认位置的回话，落在网关自己的数据目录里，不碰任何库。";
  plans.push({ text });
  await chat([SYS, { role: "user", content: "默认位置" }], { to: other });
  const file = path.join(other_root, "host", "days", "2026-10-07.jsonl");
  await until(has_text(text, file), "the reply in the default place");
  assert.deepStrictEqual(day_lines(file).map((l) => l.text), ["默认位置", text]);
});

test("reconciliation: every outbound connection stayed inside this run", () => {
  const entries = fs.existsSync(ledger_path)
    ? fs.readFileSync(ledger_path, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
  const strays = entries.filter((e) => !OUR_PORTS.has(e.端口));
  assert.deepStrictEqual(strays, []);
  assert.ok(entries.every((e) => e.放行), "nothing had to be blocked");
});
