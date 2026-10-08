// ============================================================
// gateway/tests/gateway_injection.test.js — the first test suite for this gateway
//
// 🔴 **What this suite is here to protect**
// This gateway once shipped a bug: the timeout was hard-coded to 5 seconds while one
// lookup took 5~7 — so **it never once did its job from the day it went live**, and it
// stayed that way for days with nobody noticing. Its way of failing is **quietly doing
// nothing**: no error, no crash, the chat carries on, and the interface looks normal
// forever.
//
// So what this suite wants is **not** "nothing raised an error", it is a **positive
// signal**: send a line and the card Loci handed back **really is** in what upstream
// received — at the right place, with Loci's own words — and Loci really was asked with
// that line. "No error" and "it really happened" are two different things, and that bug
// lived in the gap between them.
//
// Every assertion lands on **the body the fake upstream received** — whether the gateway
// really changed the messages is something only upstream can see.
//
// The memory path under test is the cue (present/cue.js): every new line of the owner's
// asks Loci's `/api/v2/cue` once, the card goes right after her line, and
// `/cue/delivered` follows once upstream took the turn. The replay of cards across turns,
// the window and usage are covered in present_cue.test.js; this suite is the boundary:
// Loci working, Loci broken in each way it can break, and /health telling them apart.
//
// 🔴 Nothing real is touched anywhere in here: the real Loci (18002), the real gateway
//    (3100) and the real services on 3000/3010 must not be knocked on once. Every server
//    binds a port the OS picks, with the network fence booking every outbound connection
//    as a backstop.
//
// Run (from gateway/):  node --test
//   or from the repository root:  node --test "gateway/tests/*.test.js"
//   ⚠️ Do not write `node --test gateway/tests` without the glob — Node 24 treats that
//      directory as a module and tries to require it, giving MODULE_NOT_FOUND, which
//      looks exactly like the tests failing.
// To keep the scene and read the logs (the temp directory is not deleted):
//   留下现场=1 node --test "gateway/tests/*.test.js"
//    (To try your own gateway: LOCI_GATEWAY_ENTRY=/your/server.js node --test "gateway/tests/*.test.js")
// ============================================================

const { test, before, after } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const fence = require("./network_fence.js");     // fence this process too: the test must not knock on the wrong door either
const { start_fake_upstream } = require("./fake_upstream.js");
const { start_fake_loci } = require("./fake_loci.js");
const { start_gateway } = require("./start_gateway.js");

// ——— The scene: a directory deleted when the run ends; 留下现场=1 keeps it ———
// 🔴 **The pid is not decoration.** `before()` wipes this directory on the way in, so
//    two overlapping runs would delete each other's state — the poke idle gate then reads
//    a store that was never seeded, opens, and extra requests go out to Loci.
const data_root = path.join(__dirname, `.跑测试留下的东西-${process.pid}`);
const log_path = path.join(data_root, "logs", "memory-actions.jsonl");
const child_ledger_path = path.join(data_root, "网关围栏账本.jsonl");

// How long the gateway waits for the cue. Small on purpose: the timeout case has to
// finish inside it, while the fake Loci answers at once in every other case.
const cue_timeout_ms = 1200;
// How long the fake Loci's 慢 mode drags — clearly larger than the number above, or the scene of that bug cannot be reproduced
const fake_loci_slow_ms = 3000;

// 🔴 These ports belong to the real things. Not one of them may be touched in a test.
const FORBIDDEN_PORTS = [3000, 3010, 3100, 18002, 18003];

// Every port this run actually bound — the fakes, the main gateway, each clean gateway.
// The reconciliation checks outbound traffic against exactly this set.
const OUR_PORTS = new Set();

let fake_upstream, fake_loci, gateway;
let gateway_port;

function seed_poke_gate(root) {
  // 🔴 Pretend somebody spoke **just now**: poke delivery rides in the same request as the
  //    cue, and with no state its idle gate opens on the first line and knocks on
  //    /api/loci/poke. This suite tests the cue; the gate stays shut (asserted at the end).
  fs.mkdirSync(path.join(root, "state"), { recursive: true });
  fs.writeFileSync(path.join(root, "state", "poke-window.json"),
    JSON.stringify({ lastUserMessageTime: new Date().toISOString(), wakePending: false }, null, 2));
}

/** Send one line to a gateway and return the original messages, for a character-by-character "was it changed" comparison */
async function send_line(line, request_id, target = gateway) {
  const messages = [
    { role: "system", content: "你是小慢。" },
    { role: "user", content: line },
  ];
  const original = JSON.parse(JSON.stringify(messages));
  const resp = await fetch(`${target.地址}/v1/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      // This is how a log line is matched up, not "the last line is probably mine".
      // 🔴 The escaping is mandatory: an HTTP header value is a ByteString (latin-1),
      //    Chinese does not fit in one, and fetch throws a TypeError on the spot.
      "X-Request-Id": encodeURIComponent(request_id),
      Authorization: "Bearer fake-key-do-not-look",
    },
    body: JSON.stringify({ model: "假模型", messages: messages, temperature: 0.3 }),
  });
  const body = await resp.json().catch(() => null);
  return { 状态: resp.status, 体: body, 原样: original };
}

/** This request's cue record in memory-actions.jsonl */
function find_cue_log(request_id, file = log_path) {
  if (!fs.existsSync(file)) return null;
  const wanted = encodeURIComponent(request_id);
  const lines = fs.readFileSync(file, "utf8").split(/\r?\n/).filter(Boolean);
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    let rec; try { rec = JSON.parse(lines[i]); } catch { continue; }
    if (rec.action === "cue_observed" && String(rec.request_id) === wanted) return rec;
  }
  return null;
}

/** What the tail of the gateway's console line said (milliseconds wiped, leaving only its verdict on this request) */
function console_verdict(delta) {
  const m = /→\s*\d{3}\s+\d+ms\s+(.*)$/m.exec(delta.trim());
  return m ? m[1].trim() : null;
}

/** Both fence ledgers combined: this process's, plus the one the gateway child processes wrote to file */
function all_outbound_records() {
  const child_records = fs.existsSync(child_ledger_path)
    ? fs.readFileSync(child_ledger_path, "utf8").split(/\r?\n/).filter(Boolean).map((raw_line) => JSON.parse(raw_line))
    : [];
  return [...fence.账本, ...child_records];
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

async function boot_gateway(root) {
  return start_gateway({
    端口: 0,
    上游地址: fake_upstream.地址,
    loci地址: fake_loci.地址,
    数据根: root,
    相关超时毫秒: cue_timeout_ms,
    白名单端口: [fake_upstream.端口, fake_loci.端口],   // the gateway may only go out to these two places
    账本路径: child_ledger_path,
    extra_env: { LOCI_CUE_TIMEOUT_MS: String(cue_timeout_ms) },
  });
}

before(async () => {
  fs.rmSync(data_root, { recursive: true, force: true });
  fs.mkdirSync(path.join(data_root, "logs"), { recursive: true });
  // The fakes come up first, so their real ports are known by the time the gateway needs them for its fence allowlist.
  fake_upstream = await start_fake_upstream({ 端口: 0 });
  fake_loci = await start_fake_loci({ 端口: 0 });
  seed_poke_gate(data_root);
  gateway = await boot_gateway(data_root);
  gateway_port = gateway.端口;
  for (const p of [fake_upstream.端口, fake_loci.端口, gateway_port]) OUR_PORTS.add(p);
  // The OS hands out ephemeral ports; if one ever lands on something real the fence would let the suite knock on it.
  for (const p of [fake_upstream.端口, fake_loci.端口, gateway_port]) {
    assert.ok(Number.isInteger(p) && p > 0, `端口没报回来：${p}`);
    assert.ok(!FORBIDDEN_PORTS.includes(p), `端口 ${p} 是真东西在用的`);
  }
  fence.allow(gateway_port);                     // the test process may only knock on the gateway's door
});

after(async () => {
  if (gateway) await gateway.停();
  if (fake_upstream) await fake_upstream.关();
  if (fake_loci) await fake_loci.关();
  if (process.env.留下现场) console.log(`[留下现场] ${data_root}`);
  else fs.rmSync(data_root, { recursive: true, force: true });
});

const CARD = "〔相关记忆〕上次把网关的超时从 5 秒提到了 12 秒（三天前）";

// ============================================================
// One. The positive signal — the single most important case in here
// ============================================================

test("positive signal: the card Loci gave sits right after her line in what upstream received", { timeout: 15000 }, async () => {
  fake_loci.设模式("正常");
  fake_loci.cue_with(() => ({ cards: [{ card: "e9@v1", kind: "memory", id: "e9", short: "e9", why: "超时", text: CARD }], text: CARD }));
  fake_upstream.清账(); fake_loci.清账();
  try {
    const line = "上次你说的那个超时的事，后来怎么样了？";
    const { 状态: status, 原样: original } = await send_line(line, "测试-正向");
    assert.strictEqual(status, 200, "网关得把上游的回应原样带回来");

    // ① Upstream really received this round (not "the gateway believes it forwarded")
    assert.strictEqual(fake_upstream.收到.length, 1, "假上游应该正好收到一次转发");
    const upstream_body = fake_upstream.最后一笔().体;

    // ② The messages **really were changed**: one more, right after her line, in Loci's own words
    assert.strictEqual(upstream_body.messages.length, original.length + 1,
      "上游收到的消息数应该比客户端发的多一条 —— 少了就说明卡片压根没贴上");
    assert.deepStrictEqual(upstream_body.messages.at(-1), { role: "system", content: CARD });

    // ③ Not one character of the original messages was touched — only added to, never altered
    assert.deepStrictEqual(upstream_body.messages.slice(0, original.length), original);
    assert.strictEqual(upstream_body.model, "假模型", "请求体的其它字段不该被网关碰");
    assert.strictEqual(upstream_body.temperature, 0.3);
    assert.strictEqual(upstream_body.stream_options, undefined, "a non-streamed request is not asked for usage");

    // ④ Loci really was asked, with **that exact line**, through the **fake Loci**
    const asks = fake_loci.收到.filter((r) => r.路径 === "/api/v2/cue");
    assert.strictEqual(asks.length, 1, "one line, one ask");
    assert.strictEqual(asks[0].体.text, line, "拿去问的应该就是用户这句话原文");
    assert.match(asks[0].体.window, /^t_[0-9a-f]{6}#w1$/);
    assert.match(asks[0].体.turn, /^m_\d{8}_\d{4}$/);

    // ⑤ Delivered, once, for that window and turn — after upstream took it
    await until(() => fake_loci.收到.some((r) => r.路径 === "/api/v2/cue/delivered"), "the delivered acknowledgement");
    const confirmations = fake_loci.收到.filter((r) => r.路径 === "/api/v2/cue/delivered");
    assert.deepStrictEqual(confirmations.map((r) => r.体), [{ window: asks[0].体.window, turn: asks[0].体.turn }]);

    // ⑥ The log side lines up too (when something goes wrong this is the only place the truth shows)
    const record = find_cue_log("测试-正向");
    assert.ok(record, "日志里得有这一次请求的记录");
    assert.strictEqual(record.injected, true);
    assert.strictEqual(record.cards, 1);
    assert.deepStrictEqual(record.card_keys, ["e9@v1"]);
    assert.ok(!JSON.stringify(record).includes("超时从 5 秒"), "the log keeps card keys, never card text");
    assert.strictEqual(record.error, undefined);
  } finally { fake_loci.cue_with(null); }
});

test("no card: Loci answers nothing → upstream gets exactly what the client sent, nothing to confirm", { timeout: 15000 }, async () => {
  fake_loci.设模式("空库");
  fake_upstream.清账(); fake_loci.清账();
  try {
    const { 状态: status, 原样: original } = await send_line("嗯，今天天气不错", "测试-无卡");
    assert.strictEqual(status, 200);
    assert.deepStrictEqual(fake_upstream.最后一笔().体.messages, original, "没有卡就不该往 messages 里加任何东西");
    assert.strictEqual(fake_loci.收到.filter((r) => r.路径 === "/api/v2/cue").length, 1, "asked once all the same");
    await new Promise((r) => setTimeout(r, 150));
    assert.strictEqual(fake_loci.收到.filter((r) => r.路径 === "/api/v2/cue/delivered").length, 0, "no card, nothing delivered");
    const record = find_cue_log("测试-无卡");
    assert.strictEqual(record.injected, false);
    assert.strictEqual(record.cards, 0);
    assert.strictEqual(record.error, undefined, "an empty answer is an answer, not a failure");
  } finally { fake_loci.设模式("正常"); }
});

// ============================================================
// Two. When Loci is down — failure must not block the chat, but it must be visible
// ============================================================

test("Loci answers 500: forwarded as usual, and the log shows it failed", { timeout: 15000 }, async () => {
  fake_loci.设模式("五百");
  fake_upstream.清账(); fake_loci.清账();
  try {
    const { 状态: status, 体: body, 原样: original } = await send_line("上次那个 500 的事", "测试-五百");
    assert.strictEqual(status, 200, "Loci 挂了也不许把用户的对话弄死");
    assert.strictEqual(body.choices[0].message.content, "假上游收到了。");
    assert.strictEqual(fake_upstream.收到.length, 1, "上游必须照常收到这一轮");
    assert.deepStrictEqual(fake_upstream.最后一笔().体.messages, original, "问失败了就别贴，更不许贴半截");
    const record = find_cue_log("测试-五百");
    assert.strictEqual(record.injected, false, "没贴成");
    assert.ok(record.error && /500/.test(record.error), `error 里应该看得出是 500，实际：${record.error}`);
    assert.strictEqual(fake_loci.收到.filter((r) => r.路径 === "/api/v2/cue").length, 1, "one ask, no retry inside the turn");
  } finally { fake_loci.设模式("正常"); }
});

test("Loci unreachable (connection cut): forwarded as usual, and logged", { timeout: 15000 }, async () => {
  fake_loci.设模式("断连");
  fake_upstream.清账(); fake_loci.清账();
  try {
    const { 状态: status, 原样: original } = await send_line("之前那份开工单还在吗", "测试-断连");
    assert.strictEqual(status, 200);
    assert.deepStrictEqual(fake_upstream.最后一笔().体.messages, original);
    const record = find_cue_log("测试-断连");
    assert.strictEqual(record.injected, false);
    assert.ok(record.error && record.error.includes("连不上 Loci"), `error 应该说清是连不上，实际：${record.error}`);
  } finally { fake_loci.设模式("正常"); }
});

// ============================================================
// Three. Timeout — the regression assertion for that bug
// ============================================================

test("timeout: Loci slower than the cue timeout → forwarded in time, and the console says so", { timeout: 20000 }, async () => {
  fake_loci.设模式("正常");
  fake_upstream.清账(); fake_loci.清账(); gateway.输出增量();
  await send_line("好的，那就这样吧", "测试-超时对照");
  const control_verdict = console_verdict(await gateway.等增量());

  fake_loci.设模式("慢", fake_loci_slow_ms);
  fake_upstream.清账(); fake_loci.清账();
  try {
    const t0 = Date.now();
    const { 状态: status, 体: body, 原样: original } = await send_line("上次那件事你还记得吗", "测试-超时");
    const elapsed = Date.now() - t0;
    const timeout_verdict = console_verdict(await gateway.等增量());

    // ① The conversation is neither stalled nor killed
    assert.strictEqual(status, 200, "Loci 慢不许把用户的对话弄死");
    assert.strictEqual(body.choices[0].message.content, "假上游收到了。");
    assert.deepStrictEqual(fake_upstream.最后一笔().体.messages, original, "超时了就一个字都不该贴");

    // ② The timeout really took effect, rather than sitting through the fake Loci's whole 3 seconds
    assert.ok(elapsed < cue_timeout_ms + 1500, `等太久了：${elapsed}ms，超时设的是 ${cue_timeout_ms}ms`);

    // ③ **It can be said out loud** — in the log and on the console
    const record = find_cue_log("测试-超时");
    assert.ok(record, "超时也必须留下记录");
    assert.strictEqual(record.injected, false);
    assert.ok(record.error && /超时/.test(record.error), "超时必须在日志里留下 error —— 没有这一行，它就是「安静地什么都不做」");
    assert.ok(/卡炸/.test(timeout_verdict || ""), `the console line must say the cue failed; it said: ${timeout_verdict}`);
    assert.notStrictEqual(timeout_verdict, control_verdict, "a timeout and a working turn must not read the same on the console");
  } finally { fake_loci.设模式("正常"); }
});

// ============================================================
// Four. Forwarding itself
// ============================================================

test("a gzipped upstream answer reaches the client whole", { timeout: 15000 }, async () => {
  // Node's fetch asks upstream for gzip on its own and decompresses the body, while the
  // upstream content-length still describes the **compressed** size; the relay drops
  // content-length together with content-encoding, so the client gets the whole JSON
  // document. A non-streamed answer is the one that would be cut off.
  fake_loci.设模式("正常");
  fake_upstream.清账(); fake_loci.清账();
  fake_upstream.设压缩(true);
  try {
    const resp = await fetch(`${gateway.地址}/v1/chat/completions`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Request-Id": "gzip" },
      body: JSON.stringify({ model: "假模型", messages: [{ role: "user", content: "压缩的那一次" }] }),
    });
    assert.strictEqual(resp.status, 200);
    const text = await resp.text();
    assert.strictEqual(text, fake_upstream.应该拿到的正文,
      `上游 gzip 的时候，客户端应该逐字拿到上游那份 JSON。实际拿到 ${Buffer.byteLength(text)} 字节：${JSON.stringify(text.slice(-40))}`);
    JSON.parse(text);
  } finally {
    fake_upstream.设压缩(false);
  }
});

// ============================================================
// Five. Reconciliation — not one request leaked outside the fake environment
// ============================================================

test("reconciliation: across the run, no outbound connection left the fake environment", { timeout: 15000 }, () => {
  const records = all_outbound_records();
  assert.ok(records.length > 0, "账本是空的，说明围栏根本没挂上 —— 那前面的「没漏」全是空话");
  const allowed_ports = new Set([fake_upstream.端口, fake_loci.端口, gateway_port]);
  assert.deepStrictEqual(records.filter((rec) => !allowed_ports.has(Number(rec.端口))), [], "有连接打到假环境之外去了");
  assert.deepStrictEqual(records.filter((rec) => rec.放行 === false), [], "围栏拦下了东西");
  for (const real_port of FORBIDDEN_PORTS) {
    assert.ok(!records.some((rec) => Number(rec.端口) === real_port), `敲到真的 ${real_port} 了`);
  }
  // The poke path stayed silent the whole run (its gate was seeded shut), and the chat
  // path calls no MCP tool at all — above all it never writes a memory.
  assert.deepStrictEqual(fake_loci.全程收到.filter((rec) => rec.路径.startsWith("/api/loci/")), [], "戳戳送达那条路不该在这一单里出声");
  assert.deepStrictEqual(fake_loci.全程收到.filter((rec) => rec.路径 === "/mcp"), [], "the chat path called an MCP tool");
  const routes = new Set(fake_loci.全程收到.map((rec) => rec.路径));
  assert.ok([...routes].every((r) => r.startsWith("/api/v2/cue")), `Loci was asked something other than the cue: ${[...routes].join(", ")}`);
});

// ============================================================
// Six. `/health` — the read-only endpoint
//
// The **entire point** of it is "when something is wrong, you know at once". So the test
// here is not "it returns 200", it is "**is what it says true**": say broken when it is
// broken, and do not cry wolf when it is not.
//
// 📌 It is `/health` (ASCII): a non-ASCII route arrives percent-escaped (and escaped per
//    the local codepage by curl on Windows), never matches, and falls through to the
//    forward. The first case keeps watching the old spelling with one criterion: **it
//    must not leak upstream.**
//
// Each case starts its own clean gateway — one log per case: the verdict is inferred from
// the log as a whole, and the cases above left their records in the main one.
// This section comes after the main reconciliation and runs its own at the end.
// ============================================================

async function start_clean_gateway(label, { make_data_root = true } = {}) {
  const root = path.join(__dirname, `.跑测试留下的东西-${label}-${process.pid}`);
  fs.rmSync(root, { recursive: true, force: true });
  if (make_data_root) {
    fs.mkdirSync(path.join(root, "logs"), { recursive: true });
    seed_poke_gate(root);
  }
  const gw = await boot_gateway(root);
  fence.allow(gw.端口);                    // after binding: there is no port to allow before that
  OUR_PORTS.add(gw.端口);
  return {
    网关: gw,
    日志档: path.join(root, "logs", "memory-actions.jsonl"),
    async 收() {
      await gw.停();
      if (!process.env.留下现场) fs.rmSync(root, { recursive: true, force: true });
    },
  };
}

/** Knock on the health endpoint **the most ordinary way** — exactly how a client would */
async function hit_health(target_gw, route = "/health") {
  const resp = await fetch(`${target_gw.地址}${route}`, { method: "GET" });
  const raw = await resp.text();
  let body = null; try { body = JSON.parse(raw); } catch { /* not JSON, never mind */ }
  return { 状态: resp.status, 体: body, 原文: raw };
}
function is_health_answer(body) { return Boolean(body) && typeof body.verdict === "string"; }

/** Read the health endpoint once, asserting on the way that the door opens (red, not a skip, the day it stops). */
async function read_health(target_gw) {
  const { 状态: status, 体: body, 原文: raw } = await hit_health(target_gw);
  assert.ok(is_health_answer(body),
    `GET /health should return the health endpoint's own JSON (the one carrying "verdict"). Status ${status}, got: ${raw.slice(0, 160)}`);
  return body;
}

/** The cue records by build_health()'s own rule — exactly what it reads */
function read_cue_records(log_file) {
  if (!fs.existsSync(log_file)) return [];
  return fs.readFileSync(log_file, "utf8").split(/\r?\n/).filter(Boolean)
    .map((raw_line) => { try { return JSON.parse(raw_line); } catch { return null; } })
    .filter((rec) => rec && rec.action === "cue_observed");
}

let line_seq = 0;
/** A fresh line each time: the same opening line twice within two minutes is a resend, which does not ask again. */
const next_line = (label) => `${label}：第 ${++line_seq} 句话`;

test("health: /health opens, and the old path /健康 never leaks upstream", { timeout: 20000 }, async () => {
  const rig = await start_clean_gateway("路由");
  try {
    fake_upstream.清账(); fake_loci.清账();
    const health = await read_health(rig.网关);
    assert.ok(typeof health.log_path === "string", "the health endpoint should say which log it reads");
    assert.strictEqual(health.cue_timeout_ms, cue_timeout_ms, "the timeout in force is on the first screen");
    assert.deepStrictEqual(fake_upstream.收到, [], "/health 是本地只读口，一个字都不该转发出去");

    for (const old_route of ["/健康", "/%E5%81%A5%E5%BA%B7"]) {
      fake_upstream.清账(); fake_loci.清账();
      const { 状态: status, 原文: raw } = await hit_health(rig.网关, old_route);
      const leaked = fake_upstream.收到.map((rec) => `${rec.方法} ${rec.路径}`);
      assert.deepStrictEqual(leaked, [],
        `老路径 ${old_route} 被当成普通流量转发给上游了：${JSON.stringify(leaked)}（它自己回的是：${status} ${raw.slice(0, 80)}）`);
    }
  } finally { await rig.收(); }
});

test("health: when it is working, it says \"working\"", { timeout: 20000 }, async () => {
  const rig = await start_clean_gateway("在工作");
  try {
    fake_loci.设模式("正常"); fake_upstream.清账(); fake_loci.清账();
    for (let i = 0; i < 3; i += 1) await send_line(next_line("在工作"), `健康-在工作-${i}`, rig.网关);

    const raw_records = read_cue_records(rig.日志档);
    assert.strictEqual(raw_records.length, 3);
    assert.strictEqual(raw_records.filter((r) => r.injected).length, 3, "假 Loci 正常返回，该三次都贴上");

    const health = await read_health(rig.网关);
    assert.strictEqual(health.verdict, "working");
    assert.strictEqual(health.attached, 3);
    assert.ok(health.last_attach && health.last_attach.cards >= 1, JSON.stringify(health.last_attach));
    assert.strictEqual(health.errors, 0);
  } finally { await rig.收(); }
});

test("health: the shape of that bug (asked every time, never answered) is called out", { timeout: 20000 }, async () => {
  const rig = await start_clean_gateway("bug形状");
  try {
    // Loci answers 500 throughout — the gateway forwards as usual, the chat carries on,
    // and the interface gives nothing away. Exactly the shape of "never worked once".
    fake_loci.设模式("五百"); fake_upstream.清账(); fake_loci.清账();
    for (let i = 0; i < 2; i += 1) await send_line(next_line("坏了"), `健康-坏了-${i}`, rig.网关);

    const raw_records = read_cue_records(rig.日志档);
    assert.strictEqual(raw_records.filter((r) => r.error).length, 2, "两次都该留下 error");

    const health = await read_health(rig.网关);
    assert.ok(/🔴/.test(health.verdict), `this broken it must report red; verdict was: ${health.verdict}`);
    assert.strictEqual(health.attached, 0);
    assert.strictEqual(health.answered, 0);
    assert.strictEqual(health.asked, 2);
    assert.ok(health.last_error && health.last_error.what, "it must be able to say what went wrong last");
  } finally { fake_loci.设模式("正常"); await rig.收(); }
});

test("health: Loci answering with no card is not broken — it must not report red", { timeout: 20000 }, async () => {
  // A fresh library, or lines that touch nothing it holds: asked, answered, no card, no
  // error. Report that as broken and somebody sees a red light on day one.
  const rig = await start_clean_gateway("没卡");
  try {
    fake_loci.设模式("空库"); fake_upstream.清账(); fake_loci.清账();
    for (let i = 0; i < 3; i += 1) await send_line(next_line("没卡"), `健康-没卡-${i}`, rig.网关);

    const raw_records = read_cue_records(rig.日志档);
    assert.strictEqual(raw_records.length, 3);
    assert.ok(raw_records.every((r) => !r.injected && r.error === undefined));

    const health = await read_health(rig.网关);
    assert.ok(!/🔴/.test(health.verdict), `no card must not report red; got: ${health.verdict}`);
    assert.ok(health.verdict.includes("no card"), `verdict was: ${health.verdict}`);
    assert.strictEqual(health.answered, 3);
    assert.strictEqual(health.attached, 0);
    assert.strictEqual(health.errors, 0, "errors must be 0 — it is the clue that tells 'nothing matched' apart from 'it is broken'");
  } finally { fake_loci.设模式("正常"); await rig.收(); }
});

test("health: it is read-only — writes no log, goes nowhere", { timeout: 20000 }, async () => {
  const rig = await start_clean_gateway("只读");
  try {
    fake_loci.设模式("正常"); fake_upstream.清账(); fake_loci.清账();
    await send_line(next_line("只读"), "健康-只读-垫底", rig.网关);
    await read_health(rig.网关);
    await new Promise((r) => setTimeout(r, 200));   // the delivered acknowledgement of the line above lands first

    const log_size = () => fs.statSync(rig.日志档).size;
    const ledger_size = () => (fs.existsSync(child_ledger_path)
      ? fs.readFileSync(child_ledger_path, "utf8").split(/\r?\n/).filter(Boolean).length : 0);
    const log_size_before = log_size(), ledger_size_before = ledger_size();
    fake_upstream.清账(); fake_loci.清账();

    for (let i = 0; i < 3; i += 1) {
      const { 体: body } = await hit_health(rig.网关);
      assert.ok(is_health_answer(body), "连打三次都该稳定回同一个口");
    }
    assert.strictEqual(log_size(), log_size_before, "健康口只读：日志档一个字节都不该多");
    assert.strictEqual(ledger_size(), ledger_size_before, "健康口一次都不该出门（网关侧围栏账本没长）");
    assert.strictEqual(fake_upstream.收到.length, 0, "不该把健康检查转发给上游");
    assert.strictEqual(fake_loci.收到.length, 0, "不该为了答健康去问 Loci");
  } finally { await rig.收(); }
});

test("health: no log file yet → 200 and \"no records at all yet\", not a 500", { timeout: 20000 }, async () => {
  // LOCI_GATEWAY_DATA pointing at a directory that does not exist — a fresh install, or a mistyped path.
  const rig = await start_clean_gateway("无日志", { make_data_root: false });
  try {
    fake_upstream.清账(); fake_loci.清账();
    const { 状态: status, 体: body } = await hit_health(rig.网关);
    assert.strictEqual(status, 200, "日志不在不是错误，不该 500");
    const health = is_health_answer(body) ? body : await read_health(rig.网关);
    assert.ok(health.verdict.includes("no records at all yet"), `verdict was: ${health.verdict}`);
    assert.strictEqual(health.rounds_examined, 0);
    assert.ok(!/🔴/.test(health.verdict), "no log is not the same as broken — it must not report red");
  } finally { await rig.收(); }
});

test("health: an old success does not cover up an outage happening now", { timeout: 25000 }, async () => {
  // The window is the last 200 asks, not the last stretch of time: the verdict counts the
  // failures since Loci last answered, so two answers from earlier cannot paper over four
  // failures in a row now.
  const rig = await start_clean_gateway("漏报");
  try {
    fake_upstream.清账(); fake_loci.清账();
    fake_loci.设模式("正常");
    for (let i = 0; i < 2; i += 1) await send_line(next_line("漏报-好"), `健康-漏报-好-${i}`, rig.网关);
    fake_loci.设模式("五百");
    for (let i = 0; i < 4; i += 1) await send_line(next_line("漏报-坏"), `健康-漏报-坏-${i}`, rig.网关);

    const raw_records = read_cue_records(rig.日志档);
    assert.strictEqual(raw_records.length, 6);
    assert.ok(raw_records.slice(-4).every((r) => !r.injected && r.error), "最近四轮应该是「问了、没贴上、有 error」");
    assert.strictEqual(raw_records.filter((r) => r.injected).length, 2, "窗口里还留着早先那两条成功记录");

    const health = await read_health(rig.网关);
    assert.ok(/🔴/.test(health.verdict), `four straight failures and the health endpoint said "${health.verdict}"`);
    assert.strictEqual(health.errors_since_last_answer, 4);
  } finally { fake_loci.设模式("正常"); await rig.收(); }
});

test("reconciliation, health section: the gateways started here leaked nothing either", { timeout: 15000 }, () => {
  const records = all_outbound_records();
  const strangers = records.filter((rec) => !OUR_PORTS.has(Number(rec.端口)));
  assert.deepStrictEqual(strangers, [], `有连接打到我们没起过的端口上：${JSON.stringify(strangers)}`
    + `\n本次起过的：${[...OUR_PORTS].sort((a, b) => a - b).join(", ")}`);
  assert.deepStrictEqual(records.filter((rec) => rec.放行 === false), [], "围栏拦下了东西");
  for (const real_port of FORBIDDEN_PORTS) {
    assert.ok(!records.some((rec) => Number(rec.端口) === real_port), `敲到真的 ${real_port} 了`);
  }
  const rest_hits = fake_loci.全程收到.filter((rec) => rec.路径.startsWith("/api/loci/"));
  assert.deepStrictEqual(rest_hits.map((rec) => `${rec.方法} ${rec.路径}`), [],
    "戳戳送达那条路整套跑下来都不该出声（闲时闸该被预置的 state 关死）");
});
