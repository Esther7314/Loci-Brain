// ============================================================
// gateway/tests/gateway_injection.test.js — the first test suite for this gateway
//
// 🔴 **What this suite is here to protect**
// This gateway once shipped a bug: the timeout was hard-coded to 5 seconds while one
// recall takes 5~7 — so **it never once did its job from the day it went live**, and it
// stayed that way for days with nobody noticing. Its way of failing is **quietly doing
// nothing**: no error, no crash, the chat carries on, and the interface looks normal
// forever.
//
// So what this suite wants is **not** "nothing raised an error", it is a **positive
// signal**: send a sentence carrying a strong trigger word and the injection **really
// happened** — the messages upstream received really were changed, the hit count really
// is ≥ 1, and what was attached really came from that search result.
// "No error" and "it really happened" are two different things, and that bug lived in
// the gap between them.
//
// Every assertion lands on **the body the fake upstream received** — whether the gateway
// really changed the messages is something only upstream can see.
//
// 🔴 Nothing real is touched anywhere in here: the real Loci (18002), the real gateway
//    (3100) and the real services on 3000/3010 must not be knocked on once. Ports are
//    picked and verified on the spot (19xxx), with the network fence booking every
//    outbound connection as a backstop.
//
// Run (from the repository root):  node --test "gateway/tests/*.test.js"
//   ⚠️ Do not write `node --test gateway/tests` without the glob — Node 24 treats that
//      directory as a module and tries to require it, giving MODULE_NOT_FOUND, which
//      looks exactly like the tests failing.
// To keep the scene and read the logs (the temp directory is not deleted):
//   留下现场=1 node --test "gateway/tests/*.test.js"
//
// Node v24.15.0, about 3.7 seconds for the whole suite, 19 cases, all green.
//
// 📜 Three of these were deliberately written RED — they described how the gateway
//    should behave before it did, and each one named the fix in its own comment. All
//    three fixes have since landed, so they are green now, and the history is kept
//    rather than deleted because it is the argument for writing tests this way:
//      · `/健康`, the old health route, was forwarded upstream as ordinary traffic
//        → fixed by the `/v1/` prefix guard (server.js)
//      · the health verdict under-reported: one ancient success in the window covered up
//        a total outage happening right now
//        → fixed by counting failures since the last success
//      · a gzipped upstream response was truncated, because `content-encoding` was
//        stripped while `content-length` still described the compressed size
//        → fixed by dropping both headers together
//
// ⚠️ If you are reading a comment below that says an assertion "is red today", check
//    before believing it. Those sentences were true when written and outlived their
//    subject; that is precisely the failure mode this whole suite exists to catch, and
//    it happened to the suite itself.
//    (To try your own gateway: LOCI_GATEWAY_ENTRY=/your/server.js node --test "gateway/tests/*.test.js")
// ============================================================

const { test, before, after } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

const fence = require("./network_fence.js");     // fence this process too: the test must not knock on the wrong door either
const { start_fake_upstream } = require("./fake_upstream.js");
const { start_fake_loci } = require("./fake_loci.js");
const { start_gateway } = require("./start_gateway.js");

// ——— The scene: a temp directory, deleted when the run ends; 留下现场=1 keeps it ———
// 🔴 **The pid is not decoration.** `before()` wipes this directory on the way in, so
//    two overlapping runs delete each other's state — the idle gate then reads a store
//    that was never seeded, opens, and two extra requests go out. That was reproduced
//    on 2026-08-22 by running the suite twice at once: both processes failed on
//    「对账·第八节」 with exactly `GET /api/loci/poke` + `POST /api/loci/dream/wake`
//    surplus. It had been seen once in the wild before that and written off as
//    unreproducible.
const data_root = path.join(__dirname, `.跑测试留下的东西-${process.pid}`);
const log_path = path.join(data_root, "logs", "memory-actions.jsonl");
const child_ledger_path = path.join(data_root, "网关围栏账本.jsonl");

// How long the gateway waits for recall. Keeping it small is **deliberate**: the timeout
// case has to finish inside this number, while the fake Loci answers instantly in the
// normal cases, so a small timeout does not affect them.
const relevance_timeout_ms = 1200;
// How long the fake Loci's 慢 mode drags — it has to be **clearly larger** than the number above, or the scene of that bug cannot be reproduced
const fake_loci_slow_ms = 3000;

// 🔴 These ports belong to the real things. Not one of them may be touched in a test.
const FORBIDDEN_PORTS = [3000, 3010, 3100, 18002, 18003];

// Every port this run actually bound — the fakes, the main gateway, and each clean
// gateway. The end-of-run reconciliation checks outbound traffic against this instead of
// against a numeric range: "somewhere in 19xxx" was only ever a stand-in for "one of
// ours", and it stopped meaning that once the OS started handing out the numbers.
// Checking the real set is also strictly stronger — it would now catch a connection to
// some other process that happened to be sitting in the same range.
const OUR_PORTS = new Set();

let fake_upstream, fake_loci, gateway;
let gateway_port;

// ——— Ports are never chosen here any more — everything binds 0 and reports back what the OS
// gave it. What used to live at this spot was `is_port_free` + `pick_free_ports`: ask
// whether a number was free, bind it a moment later, and lose the race to whoever asked
// at the same time. Two runs at once cost all 19 tests, and a pid-derived lane only
// lowered the odds. See start_fake_upstream / start_fake_loci / start_gateway.
// ——— Small helpers ———

/** Send one sentence to the gateway and return the original messages, for a character-by-character "was it changed" comparison */
async function send_line(line, request_id) {
  const messages = [
    { role: "system", content: "你是小慢。" },
    { role: "user", content: line },
  ];
  const original = JSON.parse(JSON.stringify(messages));
  const resp = await fetch(`${gateway.地址}/v1/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      // This is how a log line is matched up, not "the last line is probably mine".
      // 🔴 The escaping is mandatory: an HTTP header value is a ByteString (latin-1),
      //    Chinese does not fit in one, and fetch throws a TypeError on the spot. The
      //    log lookup escapes the same way before comparing.
      "X-Request-Id": encodeURIComponent(request_id),
      Authorization: "Bearer fake-key-do-not-look",
    },
    body: JSON.stringify({ model: "假模型", messages: messages, temperature: 0.3 }),
  });
  const body = await resp.json().catch(() => null);
  return { 状态: resp.status, 体: body, 原样: original };
}

/** Pick this request's relevance-reminder record out of memory-actions.jsonl */
function find_notice_log(request_id) {
  if (!fs.existsSync(log_path)) return null;
  const wanted = encodeURIComponent(request_id);   // the same escaping used when it was sent
  const lines = fs.readFileSync(log_path, "utf8").split(/\r?\n/).filter(Boolean);
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    let rec; try { rec = JSON.parse(lines[i]); } catch { continue; }
    if (rec.actor === "gateway/relevance_reminder" && String(rec.request_id) === wanted) return rec;
  }
  return null;
}

/** What the tail of the gateway's console line said (milliseconds wiped, leaving only its verdict on this request) */
function console_verdict(delta) {
  const m = /→\s*\d{3}\s+\d+ms\s+(.*)$/m.exec(delta.trim());
  return m ? m[1].trim() : null;
}

/** The numbers inside the line pasted back: 事件 N 条 · 认知 M 条 */
function parse_notice_line(text) {
  const m = /^〔记忆提醒〕和这句有关：事件 (\d+) 条 · 认知 (\d+) 条$/.exec(String(text).trim());
  return m ? { 事件: Number(m[1]), 认知: Number(m[2]) } : null;
}

/** Both fence ledgers combined: this process's, plus the one the gateway child process wrote to file */
function all_outbound_records() {
  const child_records = fs.existsSync(child_ledger_path)
    ? fs.readFileSync(child_ledger_path, "utf8").split(/\r?\n/).filter(Boolean).map((raw_line) => JSON.parse(raw_line))
    : [];
  return [...fence.账本, ...child_records];
}

before(async () => {
  fs.rmSync(data_root, { recursive: true, force: true });
  fs.mkdirSync(path.join(data_root, "logs"), { recursive: true });
  fs.mkdirSync(path.join(data_root, "state"), { recursive: true });

  // 🔴 **Nobody picks a port any more — 0 means "OS, you pick".** This used to guess:
  //    ask whether 19100 was free, bind it a moment later, and hope nothing slipped into
  //    the gap. Two runs at once were told the same number was free; the loser died in
  //    here and took all 19 tests with it, which reads as "the gateway is broken".
  //    Each server now reports the port it really bound, and the order below is what
  //    makes it work: the fakes come up first, so their real ports are known by the time
  //    the gateway needs them for its fence allowlist.
  fake_upstream = await start_fake_upstream({ 端口: 0 });
  fake_loci = await start_fake_loci({ 端口: 0 });

  // 🔴 Preset poke delivery's state file: pretend somebody spoke **just now**.
  //    This suite does not test poke delivery, but it rides in the **same request** as
  //    the path under test. Without this file its idle gate computes "time since the last
  //    message = Infinity" → opens straight away → every request goes knocking on the
  //    fake Loci's /api/loci/poke, and the assertion "the fake Loci received 0 requests
  //    when nothing triggered" could never be made.
  //    Shut the gate and the path under test is clean (a later assertion checks
  //    specifically that the REST endpoint was never knocked on).
  fs.writeFileSync(
    path.join(data_root, "state", "poke-window.json"),
    JSON.stringify({ lastUserMessageTime: new Date().toISOString(), wakePending: false }, null, 2),
  );

  gateway = await start_gateway({
    端口: 0,
    上游地址: fake_upstream.地址,
    loci地址: fake_loci.地址,
    数据根: data_root,
    相关超时毫秒: relevance_timeout_ms,
    白名单端口: [fake_upstream.端口, fake_loci.端口],   // the gateway may only go out to these two places
    账本路径: child_ledger_path,
  });
  gateway_port = gateway.端口;
  for (const p of [fake_upstream.端口, fake_loci.端口, gateway_port]) OUR_PORTS.add(p);

  // Kept as an assertion rather than a filter: the OS hands out ephemeral ports, and
  // if one ever lands on something real (3000, 18002…) the fence would quietly let the
  // suite knock on a live service. Cheap to check, expensive to miss.
  for (const p of [fake_upstream.端口, fake_loci.端口, gateway_port]) {
    assert.ok(Number.isInteger(p) && p > 0, `端口没报回来：${p}`);
    assert.ok(!FORBIDDEN_PORTS.includes(p), `端口 ${p} 是真东西在用的`);
  }

  fence.allow(gateway_port);                     // the test process may only knock on the gateway's door
});

after(async () => {
  // Only clean up what we started: the gateway was spawned here (killed by pid), and both fakes live in this process
  if (gateway) await gateway.停();
  if (fake_upstream) await fake_upstream.关();
  if (fake_loci) await fake_loci.关();
  if (process.env.留下现场) console.log(`[留下现场] ${data_root}`);
  else fs.rmSync(data_root, { recursive: true, force: true });
});

// ============================================================
// One. The positive signal — the single most important case in here
// ============================================================

test("正向信号：带强档词的一句话过去，上游收到的 messages 真的被贴了东西", { timeout: 15000 }, async () => {
  fake_loci.设模式("正常");
  fake_upstream.清账(); fake_loci.清账();

  const line = "上次你说的那个超时的事，后来怎么样了？";
  const { 状态: status, 原样: original } = await send_line(line, "测试-正向");

  assert.strictEqual(status, 200, "网关得把上游的回应原样带回来");

  // ① Upstream really received this round (not "the gateway believes it forwarded")
  assert.strictEqual(fake_upstream.收到.length, 1, "假上游应该正好收到一次转发");
  const upstream_body = fake_upstream.最后一笔().体;

  // ② The messages **really were changed** — this is the assertion that stayed
  //    permanently false while that bug was alive. The array grew by one, and it grew at
  //    the **true tail** (after the latest user message, at the very end of the array).
  assert.strictEqual(upstream_body.messages.length, original.length + 1,
    "带强档词的一句话，上游收到的消息数应该比客户端发的多一条 —— 少了就说明注入压根没发生");
  const tail = upstream_body.messages[upstream_body.messages.length - 1];
  assert.strictEqual(tail.role, "system");
  assert.ok(String(tail.content).startsWith("〔记忆提醒〕"),
    `真尾巴那条应该是记忆提醒，实际是：${JSON.stringify(tail)}`);

  // ③ Not one character of the original messages was touched — injection may only add, never alter what was said
  assert.deepStrictEqual(upstream_body.messages.slice(0, original.length), original);
  assert.strictEqual(upstream_body.model, "假模型", "请求体的其它字段不该被网关碰");
  assert.strictEqual(upstream_body.temperature, 0.3);

  // ④ The gateway really went looking, using **that exact sentence**, through the **fake Loci**
  const calls = fake_loci.工具调用.filter((c) => c.工具 === "recall");
  assert.ok(calls.length >= 1, "触发了就该真的调一次 recall");
  assert.strictEqual(calls[0].参数.query, line, "拿去查的应该就是用户这句话原文");

  // ⑤ The log side lines up too (when something goes wrong this is the only place the truth shows, so it has to be accurate)
  const record = find_notice_log("测试-正向");
  assert.ok(record, "日志里得有这一次请求的记录");
  assert.strictEqual(record.triggered, true);
  assert.strictEqual(record.trigger_kind, "strong");
  assert.ok(record.strong_matched_keywords.includes("上次"), "命中的强档词应该是「上次」");
  assert.strictEqual(record.injected, true);
});

test("命中数：贴的不是空壳 —— 条数 ≥ 1，而且跟假 Loci 给的那份对得上", { timeout: 15000 }, async () => {
  fake_loci.设模式("正常");
  fake_upstream.清账(); fake_loci.清账();

  await send_line("还记得吗，那天夜里我们把弱档关掉了", "测试-命中数");

  const tail = fake_upstream.最后一笔().体.messages.at(-1);
  const counts = parse_notice_line(tail.content);
  assert.ok(counts, `提醒行的格式不对：${tail.content}`);

  // "An injection happened" is not "the injection had content" — pasting a line reading
  // 「事件 0 条 · 认知 0 条」 still counts as pasting. So pin it separately: the hit
  // count really is ≥ 1.
  assert.ok(counts.事件 + counts.认知 >= 1, "命中数必须 ≥ 1，贴个空壳等于没贴");

  // The numbers are not the gateway's invention, they are counted out of **the fake
  // Loci's search result**: 88.4 / 71.0 are two events and 63.2 is one mind entry (it
  // wears the 🧠 badge), all three clearing the floor; the 12.7 entry must be blocked by
  // it.
  assert.strictEqual(counts.事件, fake_loci.应该的事件数, "事件条数应该等于过线的非 🧠 行数");
  assert.strictEqual(counts.认知, fake_loci.应该的认知数, "认知条数应该等于过线的 🧠 行数");

  const record = find_notice_log("测试-命中数");
  assert.deepStrictEqual(record.matched_ids, fake_loci.应该过线的id,
    "日志里记下的命中 id 必须正好是过线的那几条 —— 这是「贴的东西来自这份检索结果」的证据链");
  assert.ok(!record.matched_ids.includes(fake_loci.挡在线下的id),
    "12.7 分那条在分数线以下，绝不该被算进去（不然分数线是摆设）");
  assert.strictEqual(record.min_score, 50, "分数线应该是显式配的那个 50");
});

test("只报数量不报正文：贴回去的那行里，一个字的记忆正文都不许出现", { timeout: 15000 }, async () => {
  fake_loci.设模式("正常");
  fake_upstream.清账(); fake_loci.清账();

  const { 原样: original } = await send_line("以前我们是怎么处理这种情况的", "测试-不报正文");

  // Confirm **something really was pasted** first — otherwise this case goes green for
  // free "because nothing was injected at all", which is precisely the kind of false
  // green this suite exists to prevent.
  const tail = fake_upstream.最后一笔().体.messages.at(-1);
  assert.strictEqual(fake_upstream.最后一笔().体.messages.length, original.length + 1, "得先真的贴上了，这条测试才有意义");
  assert.strictEqual(tail.role, "system");
  const text = String(tail.content);
  assert.ok(text.startsWith("〔记忆提醒〕"), `检查的应该是提醒那一行，实际：${text}`);
  // This is the discipline the gateway wrote on its own red line: give a count and you
  // are tapping someone on the shoulder; give a summary and the system has done the
  // remembering for them.
  // It also catches "pasted, but pasted the wrong thing" — swallowing the entire search
  // result into the context is its own kind of broken.
  for (const sentence of fake_loci.记忆正文样本) {
    assert.ok(!text.includes(sentence), `记忆正文漏进注入里了：${sentence}`);
  }
  for (const id of fake_loci.应该过线的id) {
    assert.ok(!text.includes(id), `记忆 id 漏进注入里了：${id}`);
  }
  assert.ok(!text.includes("🧠"), "🧠 牌是渲染格式，不该出现在贴给模型的那行里");
  assert.strictEqual(text.split("\n").length, 1, "提醒就该是一行");
});

// ============================================================
// Two. The path where nothing triggers
// ============================================================

test("不触发：一句什么都不沾的话过去 —— 一次都不查记忆，消息一个字不改", { timeout: 15000 }, async () => {
  fake_loci.设模式("正常");
  fake_upstream.清账(); fake_loci.清账();

  const { 状态: status, 原样: original } = await send_line("嗯", "测试-不触发");
  assert.strictEqual(status, 200);

  // ① The fake Loci **received no request at all** — not "no recall", but the door was
  //    never knocked on: no MCP handshake, and no REST /api/loci/poke either.
  assert.deepStrictEqual(fake_loci.收到, [],
    `不该触发的一句话居然去敲了 Loci：${JSON.stringify(fake_loci.收到)}`);
  assert.strictEqual(fake_loci.工具调用.length, 0);

  // ② **Not one character changed**: what upstream received is, character for character, what the client sent
  const upstream_body = fake_upstream.最后一笔().体;
  assert.deepStrictEqual(upstream_body.messages, original, "没触发就不该往 messages 里加任何东西");

  // ③ The log says clearly **why** nothing was looked up — not "looked and came back empty-handed"
  const record = find_notice_log("测试-不触发");
  assert.strictEqual(record.triggered, false);
  assert.strictEqual(record.trigger_kind, "none");
  assert.strictEqual(record.recall_called, false);
  assert.strictEqual(record.skipped, "not_triggered");
});

// ============================================================
// Three. When Loci is down — failure must not block the chat, but it must be visible
// ============================================================

test("Loci 回 500：网关照常转发，而且日志上看得出来它失败了", { timeout: 15000 }, async () => {
  fake_loci.设模式("五百");
  fake_upstream.清账(); fake_loci.清账();

  const { 状态: status, 体: body, 原样: original } = await send_line("上次那个 500 的事", "测试-五百");

  // ① The conversation must not be killed: forwarded as usual, response received as usual
  assert.strictEqual(status, 200, "Loci 挂了也不许把用户的对话弄死");
  assert.strictEqual(body.choices[0].message.content, "假上游收到了。");
  assert.strictEqual(fake_upstream.收到.length, 1, "上游必须照常收到这一轮");
  assert.deepStrictEqual(fake_upstream.最后一笔().体.messages, original, "查失败了就别贴，更不许贴半截");

  // ② But it **has to be visible** — the log carries a record with an error, and
  //    "triggered, looked, failed" can be told apart from "never triggered at all".
  const record = find_notice_log("测试-五百");
  assert.strictEqual(record.triggered, true, "触发过");
  assert.strictEqual(record.recall_called, true, "去查过");
  assert.strictEqual(record.injected, false, "没贴成");
  assert.ok(record.error && /500/.test(record.error), `error 里应该看得出是 500，实际：${record.error}`);

  // ③ It retried once internally (handshake → call → fail → handshake again → call).
  //    Pin that number: a failure costs **double**, not single — the timeout case below
  //    relies on this fact.
  assert.strictEqual(fake_loci.工具调用.length, 2, "调用失败时它会重试一次，一共两发");
});

test("Loci 连不上（连接被掐断）：一样照常转发，一样在日志里留痕", { timeout: 15000 }, async () => {
  fake_loci.设模式("断连");
  fake_upstream.清账(); fake_loci.清账();

  const { 状态: status, 原样: original } = await send_line("之前那份开工单还在吗", "测试-断连");

  assert.strictEqual(status, 200);
  assert.deepStrictEqual(fake_upstream.最后一笔().体.messages, original);

  const record = find_notice_log("测试-断连");
  assert.strictEqual(record.triggered, true);
  assert.strictEqual(record.injected, false);
  assert.ok(record.error && record.error.includes("连不上 Loci"),
    `error 应该说清是连不上，实际：${record.error}`);
});

// ============================================================
// Four. Timeout — the regression assertion for that bug
// ============================================================

test("超时：假 Loci 比网关的超时还慢 → 转发照常，而且这件事说得出口", { timeout: 20000 }, async () => {
  // Start with a "never triggered at all" sentence as the control, to compare against what the console says on the timeout run
  fake_loci.设模式("正常");
  fake_upstream.清账(); fake_loci.清账(); gateway.输出增量();
  await send_line("好的", "测试-超时对照");
  const control_verdict = console_verdict(await gateway.等增量());

  // Now make the fake Loci slower than the gateway's timeout — this is the scene of that
  // bug: a 5 second timeout against a 5~7 second recall, so every call aborted and the
  // hit count was permanently 0.
  fake_loci.设模式("慢", fake_loci_slow_ms);
  fake_upstream.清账(); fake_loci.清账();

  const t0 = Date.now();
  const { 状态: status, 体: body, 原样: original } = await send_line("上次那件事你还记得吗", "测试-超时");
  const elapsed = Date.now() - t0;
  const timeout_verdict = console_verdict(await gateway.等增量());

  // ① The conversation must not be stalled, and must not be killed
  assert.strictEqual(status, 200, "Loci 慢不许把用户的对话弄死");
  assert.strictEqual(body.choices[0].message.content, "假上游收到了。");
  assert.deepStrictEqual(fake_upstream.最后一笔().体.messages, original, "超时了就一个字都不该贴");

  // ② The timeout really took effect (rather than sitting through the fake Loci's whole
  //    3 seconds). The ceiling is computed as twice the timeout, because a failure means
  //    one retry — see the 2 pinned by the previous case.
  assert.ok(elapsed < relevance_timeout_ms * 2 + 1500,
    `等太久了：${elapsed}ms，超时设的是 ${relevance_timeout_ms}ms（重试一次也就 ${relevance_timeout_ms * 2}ms 上下）`);
  assert.strictEqual(fake_loci.工具调用.length, 2,
    "🔴 一次超时 = 两次 recall：它重试了一次，所以真实等待是 RELEVANCE_TIMEOUT_MS 的两倍");

  // ③ **It can be said out loud** — the log carries an explicit failure record, and
  //    "triggered, looked, timed out" is distinguishable from "never triggered at all".
  //    That bug survived for days precisely because there was nowhere to read this line.
  const record = find_notice_log("测试-超时");
  assert.ok(record, "超时也必须留下记录");
  assert.strictEqual(record.triggered, true, "触发过");
  assert.strictEqual(record.recall_called, true, "去查过");
  assert.strictEqual(record.injected, false, "没贴成");
  assert.strictEqual(record.event_count, 0);
  assert.strictEqual(record.mind_count, 0);
  assert.ok(record.error, "超时必须在日志里留下 error —— 没有这一行，它就是「安静地什么都不做」");

  // ④ A nail in the current behaviour (**not praise for it**): on the console, a timeout
  //    and a total non-trigger look **identical**. The gateway only ever says 「提醒无」,
  //    with not one character between the two cases — that is where the bug lived.
  //    The evidence can only be had from memory-actions.jsonl (the assertions in ③).
  //    The day the gateway makes this line tell them apart, this assertion goes red:
  //    delete it then, and be glad.
  assert.strictEqual(timeout_verdict, control_verdict,
    "现状：控制台分不出「超时」和「没触发」。这条钉的是现状，不是期望");
  assert.ok(/提醒无/.test(timeout_verdict || ""), `控制台判词长这样：${timeout_verdict}`);
});

// ============================================================
// Five. When it cannot make sense of what it read
//       (reader: this is **evidence of a pit**, not praise for it)
// ============================================================

test("坑｜Loci 换个排版：一条都数不出来，而且日志上跟「本来就没相关记忆」一模一样", { timeout: 15000 }, async () => {
  // auto_attach.js's parse_score_line reads Loci's **human-facing rendered text**, not a
  // structured API, with a regex hard-coded to "score + two spaces + … + (id in round
  // brackets)". The day Loci moves the date to the front and switches the id to square
  // brackets, not one character of content goes missing and the gateway recognises
  // nothing at all.
  //
  // 🔴 What this case pins is the **current behaviour**: in the log, this blindness looks
  //    exactly like "there really is nothing relevant today" (triggered=true,
  //    recall_called=true, 0 entries, injected=false, and **not even an error**). Which
  //    makes this failure mode harder to find than the 5 second bug — that one at least
  //    left an error in the log.
  //    The day the gateway learns to shout when it cannot parse a score line, this goes
  //    red; change it then.
  fake_loci.设模式("换排版");
  fake_upstream.清账(); fake_loci.清账();

  const { 状态: status, 原样: original } = await send_line("上次那个排版的事", "测试-换排版");
  assert.strictEqual(status, 200);

  // It really looked, and really got content back (the fake Loci returned successfully this time)
  assert.strictEqual(fake_loci.工具调用.length, 1, "这次调用是成功的，不该有重试");
  // But it counted nothing out, and pasted nothing
  assert.deepStrictEqual(fake_upstream.最后一笔().体.messages, original);

  const record = find_notice_log("测试-换排版");
  assert.strictEqual(record.triggered, true);
  assert.strictEqual(record.recall_called, true);
  assert.strictEqual(record.event_count, 0);
  assert.strictEqual(record.mind_count, 0);
  assert.strictEqual(record.injected, false);
  assert.strictEqual(record.error, undefined,
    "现状：解析失败连个 error 都不留 —— 这就是它比 5 秒 bug 更难发现的地方");
});

// ============================================================
// Six. Forwarding itself — 🔴 one case in this section is **red**, and it is a real bug
//      in the gateway, not a test left unfinished
// ============================================================

test("🔴 上游 gzip 的时候，回应会被截断（gateway/server.js 第 136~139 行）", { timeout: 15000 }, async () => {
  // How it happens:
  //   ① server.js deletes the client's accept-encoding (`delete headers["accept-encoding"]`)
  //      — the intent being "do not let upstream compress";
  //   ② but the forwarding uses Node's built-in fetch, and **undici puts one back
  //      itself**, `accept-encoding: gzip, deflate` (measured), so upstream gzips anyway;
  //   ③ fetch decompresses the body, while the content-length in resp.headers still
  //      describes the **compressed** size;
  //   ④ when the response headers are copied across, only content-encoding is skipped and
  //      **content-length is copied verbatim**.
  // Result: the client is told "107 bytes in total" while the real body is a thousand-odd
  // decompressed bytes — Node cuts it off at content-length, the client gets half a JSON
  // document, and parse blows up.
  //
  // Why it stays alive: chat calls are almost all stream:true, and a streamed response is
  // chunked with no content-length, so this path is bypassed. Non-streaming requests
  // (completions, embeddings, any synchronous SDK call) are the ones that get bitten.
  //
  // 📜 This assertion was written RED — it described how it should be, before it was.
  //    The fix landed: `content-length` is now dropped together with `content-encoding`
  //    where the response headers are copied, because the body has already been
  //    decompressed and its old length no longer describes it.
  //    (The other way to fix it would have been to stop decompressing at all and forward
  //    the original bytes untouched. Either works; only one was needed.)
  fake_upstream.清账(); fake_loci.清账();
  fake_upstream.设压缩(true);
  try {
    const resp = await fetch(`${gateway.地址}/v1/chat/completions`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Request-Id": "gzip" },
      body: JSON.stringify({ model: "假模型", messages: [{ role: "user", content: "嗯" }] }),
    });
    assert.strictEqual(resp.status, 200);
    const text = await resp.text();
    assert.strictEqual(text, fake_upstream.应该拿到的正文,
      `上游 gzip 的时候，客户端应该逐字拿到上游那份 JSON。实际拿到 ${Buffer.byteLength(text)} 字节的半截：${JSON.stringify(text.slice(-40))}`);
    JSON.parse(text);   // half a document would blow up right here too
  } finally {
    fake_upstream.设压缩(false);   // do not drag the following cases into the ditch
  }
});

// ============================================================
// Seven. Reconciliation — not one request leaked outside the fake environment
// ============================================================

test("对账：整套跑下来，出门的连接一条都没漏到假环境之外", { timeout: 15000 }, () => {
  const records = all_outbound_records();
  assert.ok(records.length > 0, "账本是空的，说明围栏根本没挂上 —— 那前面的「没漏」全是空话");

  const allowed_ports = new Set([fake_upstream.端口, fake_loci.端口, gateway_port]);
  const out_of_range = records.filter((rec) => !allowed_ports.has(Number(rec.端口)));
  assert.deepStrictEqual(out_of_range, [],
    `有连接打到假环境之外去了：${JSON.stringify(out_of_range)}`);

  // There should not be a single blocked entry — one would mean a path was never
  // rerouted and the fence caught it on our behalf.
  const blocked = records.filter((rec) => rec.放行 === false);
  assert.deepStrictEqual(blocked, [], `围栏拦下了这些：${JSON.stringify(blocked)}`);

  // Name them one by one: not one of the real things was knocked on
  for (const real_port of FORBIDDEN_PORTS) {
    assert.ok(!records.some((rec) => Number(rec.端口) === real_port), `敲到真的 ${real_port} 了`);
  }

  // The fake Loci's REST face (the poke-delivery path) was never knocked on **across the
  // whole run** — read from the whole-run ledger that 清账 cannot clear, which shows the
  // preset idle gate really did stay shut the entire time, and that the earlier "the fake
  // Loci received 0 requests" was not luck.
  const rest_hits = fake_loci.全程收到.filter((rec) => rec.路径.startsWith("/api/loci/"));
  assert.deepStrictEqual(rest_hits, [], "戳戳送达那条路不该在这一单里出声");

  // Across the whole run only one tool was ever called, recall — the gateway is not calling anything else behind our backs, and above all must never write a memory
  const other_tools = fake_loci.全程收到
    .filter((rec) => rec.rpc方法 === "tools/call")
    .map((rec) => rec.体?.params?.name)
    .filter((name) => name !== "recall");
  assert.deepStrictEqual(other_tools, [], `网关调了 recall 之外的工具：${other_tools.join(",")}`);

  console.log(`[对账] 出门 ${records.length} 次，端口只有：${[...new Set(records.map((rec) => rec.端口))].join(", ")}`);
});

// ============================================================
// Eight. `/health` — the read-only endpoint
//
// The **entire point** of its existing is "when something is wrong, you know at once".
// So the test here is not "it returns 200", it is "**is what it says true**": say broken
// when it is broken, and do not cry wolf when it is not.
//
// 📌 This endpoint was first called `/健康`, and **nobody could reach it** — the client
//    sends the escaped `/%E5%81%A5%E5%BA%B7` (curl on Windows sends `/%BD%A1%BF%B5`,
//    escaped per the local codepage), while the route is compared byte for byte and never
//    matches. Push the raw UTF-8 bytes down a bare socket instead and Node's HTTP parser
//    answers 400 (HPE_INVALID_URL) before the handler ever gets a turn.
//    Worse than "no response" is that it **fails quietly**: an unmatched route falls
//    through to the forwarding path, so the health probe gets **treated as ordinary
//    traffic and sent to the upstream model**.
//    It is named `/health` (ASCII) now, and the problem is gone at the root.
//    ⚠️ One assertion is left here watching the old path (the first case of this
//       section), with exactly one criterion: **it must not leak upstream.**
//
// ⚠️ This section deliberately comes after the reconciliation: the freshly started test
//    gateways use new ports, and putting it earlier would turn the existing
//    reconciliation red for "not recognising these ports" — which is not a leak, it is
//    not knowing the new ports exist. The existing ten cases are left untouched, so this
//    section runs its own reconciliation at the end (the last case of section eight).
// ============================================================

// This section starts its own clean gateway: **one log per test.**
// Why not share the gateway above: the health endpoint's verdict is inferred from the log
// **as a whole**, and the first ten cases have already left a pile of successful records
// in the main log — build on that and a verdict like "broken" can never come out (which
// is itself the under-reporting failure; see the 漏报 case). To judge whether it speaks
// accurately, the ground under it has to be clean.
let clean_gateway_count = 0;
async function start_clean_gateway(label, { make_data_root = true } = {}) {
  clean_gateway_count += 1;
  const port = 0;   // the OS picks; the real one comes back on gw.端口
  const root = path.join(__dirname, `.跑测试留下的东西-${label}-${process.pid}`);
  fs.rmSync(root, { recursive: true, force: true });
  if (make_data_root) {
    fs.mkdirSync(path.join(root, "logs"), { recursive: true });
    fs.mkdirSync(path.join(root, "state"), { recursive: true });
    // Same as the main gateway: hold poke delivery's idle gate shut. This section tests the health endpoint, not poking.
    fs.writeFileSync(path.join(root, "state", "poke-window.json"),
      JSON.stringify({ lastUserMessageTime: new Date().toISOString(), wakePending: false }));
  }
  const gw = await start_gateway({
    端口: port,
    上游地址: fake_upstream.地址,
    loci地址: fake_loci.地址,
    数据根: root,
    相关超时毫秒: relevance_timeout_ms,
    白名单端口: [fake_upstream.端口, fake_loci.端口],
    账本路径: child_ledger_path,               // shares the main ledger, reconciled together at the end
  });
  // ⚠️ **After**, not before: with the OS picking, there is no port to allow until the
  //    gateway has actually bound one. Allowing `port` up here let a literal 0 into the
  //    allowlist and the fence then blocked the test from knocking on its own gateway.
  fence.allow(gw.端口);                    // the test process needs to knock on it
  OUR_PORTS.add(gw.端口);
  return {
    网关: gw,
    端口: gw.端口,
    数据根: root,
    日志档: path.join(root, "logs", "memory-actions.jsonl"),
    async 收() {
      await gw.停();
      if (!process.env.留下现场) fs.rmSync(root, { recursive: true, force: true });
    },
  };
}

/** Send one sentence to a given gateway (the send_line used by the first ten cases targets the main gateway and is left alone) */
async function send_line_to(target_gw, line, request_id) {
  const resp = await fetch(`${target_gw.地址}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Request-Id": encodeURIComponent(request_id) },
    body: JSON.stringify({ model: "假模型", messages: [{ role: "user", content: line }] }),
  });
  await resp.text();
  return resp.status;
}

/** Knock on the health endpoint **the most ordinary way** — exactly how a client would, with no special posture */
async function hit_health(target_gw, route = "/health") {
  const resp = await fetch(`${target_gw.地址}${route}`, { method: "GET" });
  const raw = await resp.text();
  let body = null; try { body = JSON.parse(raw); } catch { /* not JSON, never mind */ }
  return { 状态: resp.status, 体: body, 原文: raw };
}
/** Is this the health endpoint's own answer, rather than upstream's after being forwarded there */
function is_health_answer(body) { return Boolean(body) && typeof body.verdict === "string"; }

/**
 * Read the health endpoint once, and **assert on the way that the door opens**.
 * 🔴 A hard assertion, not a skip: whether the door opens is itself something to
 *    protect — the day the route is changed back into something unreachable, this
 *    section should go **red**, not quietly skip past.
 */
async function read_health(target_gw) {
  const { 状态: status, 体: body, 原文: raw } = await hit_health(target_gw);
  assert.ok(is_health_answer(body),
    `GET /health should return the health endpoint's own JSON (the one carrying "verdict"). Status ${status}, got: ${raw.slice(0, 160)}`);
  return body;
}

/** Pick the relevance-reminder records out of the log by build_health()'s own rule — these are exactly what it reads */
function read_notice_records(log_file) {
  if (!fs.existsSync(log_file)) return [];
  return fs.readFileSync(log_file, "utf8").split(/\r?\n/).filter(Boolean)
    .map((raw_line) => { try { return JSON.parse(raw_line); } catch { return null; } })
    .filter((rec) => rec && rec.action === "relevance_reminder_observed");
}

const STRONG_LINES = ["上次那件事怎么样了", "还记得吗那天夜里", "以前我们是怎么弄的", "之前那份单子呢", "那时候你说过什么"];
const NEUTRAL_LINES = ["嗯", "好的", "哦哦"];

test("健康口｜/health 通，而且老路径 /健康 不许漏给上游", { timeout: 20000 }, async () => {
  // This case watches two things, and the second one is the crux.
  //
  // ① (new path) `/health` opens, and answers with the health endpoint's own JSON.
  //
  // ② (old path) `/健康` **must not be forwarded upstream as chat traffic**.
  //    There is exactly one criterion: **the fake upstream received nothing at all**.
  //    The old path may answer whatever it likes now (a 404, or some local reply), but as
  //    long as it can still leak upstream this case should be red — because in a real run
  //    the far end is DeepSeek: a probe meant to check "is it working" does not work, and
  //    posts itself to someone else's bill on the way out.
  //
  // Both spellings have to be tried: a client knocking on `/健康` has fetch escape it to
  // `/%E5%81%A5%E5%BA%B7`, and somebody may paste that escaped string straight out of old
  // documentation — both count as "the old path".
  const rig = await start_clean_gateway("路由");
  try {
    // ① the new path opens
    fake_upstream.清账(); fake_loci.清账();
    const health = await read_health(rig.网关);
    assert.ok(typeof health.log_path === "string", "the health endpoint should say which log it reads");
    assert.deepStrictEqual(fake_upstream.收到, [], "/health 是本地只读口，一个字都不该转发出去");

    // ② the old path must not leak
    for (const old_route of ["/健康", "/%E5%81%A5%E5%BA%B7"]) {
      fake_upstream.清账(); fake_loci.清账();
      const { 状态: status, 原文: raw } = await hit_health(rig.网关, old_route);
      const leaked = fake_upstream.收到.map((rec) => `${rec.方法} ${rec.路径}`);
      assert.deepStrictEqual(leaked, [],
        `老路径 ${old_route} 被当成普通流量转发给上游了：${JSON.stringify(leaked)}\n`
        + `  （真跑起来这就是发给 DeepSeek —— 一个健康探测跑到人家账单上去了）\n`
        + `  它自己回的是：${status} ${raw.slice(0, 80)}`);
    }
  } finally { await rig.收(); }
});

test("健康口｜正常在工作的时候，它说「在工作」", { timeout: 20000 }, async () => {
  const rig = await start_clean_gateway("在工作");
  try {
    fake_loci.设模式("正常"); fake_upstream.清账(); fake_loci.清账();
    for (let i = 0; i < 3; i += 1) await send_line_to(rig.网关, STRONG_LINES[i], `健康-在工作-${i}`);

    // —— Verifiable without going through the door: the raw material build_health() reads has to be right first ——
    const raw_records = read_notice_records(rig.日志档);
    assert.strictEqual(raw_records.length, 3);
    assert.strictEqual(raw_records.filter((r) => r.triggered).length, 3, "三句都带强档词，该三次都触发");
    assert.strictEqual(raw_records.filter((r) => r.injected).length, 3, "假 Loci 正常返回，该三次都贴上");

    const health = await read_health(rig.网关);

    assert.strictEqual(health.verdict, "working");
    assert.ok(health.attached >= 1, `attached should be >= 1, got ${health.attached}`);
    assert.ok(health.last_attach, "there should be a last_attach");
    assert.ok(health.last_attach.hits >= 1,
      `last_attach.hits should be >= 1, got ${health.last_attach.hits}`);
    assert.strictEqual(health.errors, 0);
  } finally { await rig.收(); }
});

test("健康口｜那个 bug 的形状（触发了但一次都没贴上）必须被认出来", { timeout: 20000 }, async () => {
  const rig = await start_clean_gateway("bug形状");
  try {
    // The fake Loci answers 500 throughout — the gateway forwards as usual, the chat
    // carries on, and the interface gives nothing away. This is exactly the shape of
    // "5 second timeout, never worked once since it shipped".
    fake_loci.设模式("五百"); fake_upstream.清账(); fake_loci.清账();
    for (let i = 0; i < 3; i += 1) await send_line_to(rig.网关, STRONG_LINES[i], `健康-坏了-${i}`);

    const raw_records = read_notice_records(rig.日志档);
    assert.strictEqual(raw_records.filter((r) => r.triggered).length, 3, "三次都该触发");
    assert.strictEqual(raw_records.filter((r) => r.injected).length, 0, "三次都该没贴上");
    assert.strictEqual(raw_records.filter((r) => r.error).length, 3, "三次都该留下 error");

    const health = await read_health(rig.网关);

    assert.ok(/🔴/.test(health.verdict),
      `this broken it must report red; verdict was: ${health.verdict}`);
    assert.strictEqual(health.attached, 0, "nothing was ever attached");
    assert.strictEqual(health.triggered, 3);
    assert.ok(health.last_error && health.last_error.what, "it must be able to say what went wrong last");
  } finally { await rig.收(); }
});

test("健康口｜一次都没触发 ≠ 坏了：不许报红", { timeout: 20000 }, async () => {
  const rig = await start_clean_gateway("没触发");
  try {
    fake_loci.设模式("正常"); fake_upstream.清账(); fake_loci.清账();
    for (let i = 0; i < 3; i += 1) await send_line_to(rig.网关, NEUTRAL_LINES[i], `健康-没触发-${i}`);

    const raw_records = read_notice_records(rig.日志档);
    assert.strictEqual(raw_records.length, 3);
    assert.strictEqual(raw_records.filter((r) => r.triggered).length, 0, "都是应声话，一次都不该触发");
    assert.strictEqual(fake_loci.收到.length, 0, "没触发就不该去敲 Loci");

    const health = await read_health(rig.网关);

    // 🔴 The most important one here: **a false alarm is worse than no alarm.** Nobody saying anything relevant is an ordinary day, not a fault.
    assert.ok(!/🔴/.test(health.verdict), `no trigger must not report red; got: ${health.verdict}`);
    assert.ok(health.verdict.includes("not triggered once"), `verdict was: ${health.verdict}`);
    assert.strictEqual(health.triggered, 0);
    assert.strictEqual(health.attached, 0);
  } finally { await rig.收(); }
});

test("健康口｜它是只读的：不写日志、不出门", { timeout: 20000 }, async () => {
  const rig = await start_clean_gateway("只读");
  try {
    fake_loci.设模式("正常"); fake_upstream.清账(); fake_loci.清账();
    await send_line_to(rig.网关, STRONG_LINES[0], "健康-只读-垫底");

    const health = await read_health(rig.网关);

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

test("健康口｜日志档还不存在的时候：200 + 说清「还没有任何一次记录」，不是 500", { timeout: 20000 }, async () => {
  // Point LOCI_GATEWAY_DATA at a directory that does not exist at all — that is what a fresh install, or a mistyped path, looks like.
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

// ——— Does it lie about itself ———
// The two cases below do not test whether the endpoint opens; they test **the criterion
// itself**. They run today (against the raw material build_health() reads), because that
// material already shows what the verdict is going to be.

test("撒谎·漏报｜陈年的成功会盖住今天的全面失效", { timeout: 25000 }, async () => {
  // 🔴 build_health() decides "working" on one condition: **any injected record anywhere
  //    in the window**. The window is "the last 200 log lines", not "the last stretch of
  //    time" — so a record that succeeded yesterday sits in the window forever, covers up
  //    today's total outage, and the verdict still says "working".
  //    Of the four possible verdicts this is **the most dangerous one**: it says all is
  //    well at exactly the moment something has gone wrong, and speaking up at that
  //    moment is the entire reason this endpoint exists.
  // 📌 The criterion should become "recently" rather than "ever". Either spelling does:
  //      · look only at the last K triggers (say 10) for a success;
  //      · or look at "how many failures since the last success" — go red past 3.
  //    All the data needed is already in hand (last_injected.time is computed already).
  const rig = await start_clean_gateway("漏报");
  try {
    fake_upstream.清账(); fake_loci.清账();
    fake_loci.设模式("正常");
    for (let i = 0; i < 2; i += 1) await send_line_to(rig.网关, STRONG_LINES[i], `健康-漏报-好-${i}`);
    fake_loci.设模式("五百");                       // from this moment on it is completely dead
    for (let i = 0; i < 4; i += 1) await send_line_to(rig.网关, STRONG_LINES[i], `健康-漏报-坏-${i}`);

    const raw_records = read_notice_records(rig.日志档);
    assert.strictEqual(raw_records.length, 6);
    // Right now: the last 4 rounds in a row all failed
    const last_four = raw_records.slice(-4);
    assert.ok(last_four.every((r) => r.triggered && !r.injected && r.error),
      "最近四轮应该是「触发了、没贴上、有 error」");
    // Meanwhile two ancient successes still lie in the window — those two are what make the verdict say "working"
    assert.strictEqual(raw_records.filter((r) => r.injected).length, 2,
      "窗口里还留着早先那两条成功记录 —— 漏报就是它们造成的");

    const health = await read_health(rig.网关);
    // 📜 Written RED: at the time, the health endpoint answered "working" here, and this
    //    line asserted the answer it *should* give. The fix landed — the verdict now
    //    counts failures since the last success — so it passes. What it guards is that
    //    four straight failures can never again be papered over by a success from days ago.
    assert.ok(/🔴/.test(health.verdict),
      `four straight failures and the health endpoint said "${health.verdict}" — an ancient success covered up today's outage`);
  } finally { await rig.收(); }
});

test("撒谎·误报｜库里本来就没有相关的东西，会被说成「它在安静地什么都不做」", { timeout: 25000 }, async () => {
  // The fake Loci looks up fine, there is simply **nothing relevant at all** (day one for
  // a fresh install, or a topic genuinely never discussed — both look like this). What
  // the log gets is: triggered, looked, 0 entries, nothing attached, **and no error**.
  // But build_health() only looks at "was anything attached", so it shouts 🔴 "it is
  // quietly doing nothing" — while it is in fact working perfectly.
  // **Ship this and somebody sees that red light on day one.**
  //
  // 📌 The material to tell them apart is already in hand: `出过错`. The criterion should
  //    be said in two sentences —
  //      errors > 0          → "🔴 it is broken" (that is the shape of the bug)
  //      errors = 0, 0 hits  → "looked, but nothing cleared the floor" (an empty library /
  //                            the floor set too high / Loci changed its render layout →
  //                            see the 换排版 case in section five)
  //    ⚠️ But say it plainly: **"the library has nothing" and "the parser has gone blind"
  //       look identical in the log** (both are triggered + recall_called + 0 entries +
  //       no error). Nobody can separate them, so this sentence can only say "nothing
  //       cleared the floor" and must not draw the conclusion on someone's behalf.
  const rig = await start_clean_gateway("误报");
  try {
    fake_upstream.清账(); fake_loci.清账();
    fake_loci.设模式("空库");
    for (let i = 0; i < 3; i += 1) await send_line_to(rig.网关, STRONG_LINES[i], `健康-误报-${i}`);

    const raw_records = read_notice_records(rig.日志档);
    assert.strictEqual(raw_records.length, 3);
    assert.ok(raw_records.every((r) => r.triggered && r.recall_called), "三次都真的去查了");
    assert.ok(raw_records.every((r) => !r.injected), "查到 0 条，所以一次都没贴");
    assert.ok(raw_records.every((r) => r.error === undefined),
      "🔴 关键：一个 error 都没有 —— 这不是坏，这是「确实没有相关的记忆」");

    // Once the door is fixed, this is where that false alarm would show up (for now, only pin the material and draw no conclusion for anyone)
    const health = await read_health(rig.网关);
    assert.strictEqual(health.errors, 0,
      "errors must be 0 — it is the only clue that tells 'nothing matched' apart from 'it is broken'");
  } finally { await rig.收(); }
});

test("对账·第八节：健康这一节新起的网关，也一条都没漏出去", { timeout: 15000 }, () => {
  // The existing reconciliation comes before this section and cannot see these new ports — so do one here.
  const records = all_outbound_records();
  const strangers = records.filter((rec) => !OUR_PORTS.has(Number(rec.端口)));
  assert.deepStrictEqual(strangers, [], `有连接打到我们没起过的端口上：${JSON.stringify(strangers)}`
    + `
本次起过的：${[...OUR_PORTS].sort((a, b) => a - b).join(", ")}`);
  const blocked = records.filter((rec) => rec.放行 === false);
  assert.deepStrictEqual(blocked, [], `围栏拦下了这些：${JSON.stringify(blocked)}`);
  for (const real_port of FORBIDDEN_PORTS) {
    assert.ok(!records.some((rec) => Number(rec.端口) === real_port), `敲到真的 ${real_port} 了`);
  }
  const rest_hits = fake_loci.全程收到.filter((rec) => rec.路径.startsWith("/api/loci/"));
  const timeline = fake_loci.全程收到.map((rec, i) => `${i}:${rec.rpc方法 || rec.路径}`).join(" ");
  assert.deepStrictEqual(rest_hits.map((rec) => `${rec.方法} ${rec.路径}`), [],
    `戳戳送达那条路整套跑下来都不该出声（闲时闸该被预置的 state 关死）。全程时间线：\n${timeline}`);
  console.log(`[对账·全程] 出门 ${records.length} 次，端口：${[...new Set(records.map((rec) => Number(rec.端口)))].sort().join(", ")}`);
});
