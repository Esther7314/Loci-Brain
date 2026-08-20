// ============================================================
// gateway/tests/gateway_injection.test.js —— 这个网关的第一套测试
//
// 🔴 **这一套要守的是什么**
// 这个网关上出过一个 bug：超时写死 5 秒，而一次 recall 要 5~7 秒 ——
// 于是它**从上线起一次都没工作过**，活了好几天没人发现。
// 因为它的失败方式是「**安静地什么都不做**」：不报错、不崩、聊天照常，
// 界面上永远是正常的。
//
// 所以这套测试要的**不是**「没报错」，是**正向信号**：
//   带强档词的一句话过去 → 注入**真的发生了**（上游收到的 messages 真的被改了、
//   命中数真的 ≥ 1、贴的内容真的来自那份检索结果）。
// 「没报错」和「真的发生了」是两回事，那个 bug 就活在这两者之间。
//
// 断言全部打在**假上游收到的 body** 上 —— 网关有没有真的改过消息，只有上游看得见。
//
// 🔴 全程不碰真的东西：真 Loci（18002）、真网关（3100）、她的 3000/3010
//    一个都不许敲。端口现挑现验（19xxx），出门的连接有 网络围栏 逐条记账兜底。
//
// 跑（在仓库根下）：  node --test "gateway/tests/*.test.js"
//   ⚠️ 别写成 `node --test gateway/tests`（不带 glob）—— Node 24 会把那个目录
//      当成一个模块去 require，报 MODULE_NOT_FOUND，看着像测试挂了。
// 想留现场看日志（临时目录不删）：  留下现场=1 node --test "gateway/tests/*.test.js"
//
// 本机验过：Node v24.15.0，整套 3.7 秒，19 条。
// 🔴 其中**两条是红的，红的是网关不是测试**（都在第八节，各自的注释里写了怎么修）：
//    · 老路径 /健康 还会被当成普通流量转发给上游
//    · 健康口的「在工作」会漏报：陈年的成功盖得住今天的全面失效
//    这两条都拿改过的副本验过：按注释里的改法改完，19 条全绿。
//    （想拿自己的改法试：LOCI_GATEWAY_ENTRY=/你的/server.js node --test "gateway/tests/*.test.js"）
// ============================================================

const { test, before, after } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

const fence = require("./network_fence.js");     // 本进程也上围栏：测试自己也不许敲错门
const { start_fake_upstream } = require("./fake_upstream.js");
const { start_fake_loci } = require("./fake_loci.js");
const { start_gateway } = require("./start_gateway.js");

// ——— 现场（临时目录，跑完删掉；留下现场=1 就留着）———
const data_root = path.join(__dirname, ".跑测试留下的东西");
const log_path = path.join(data_root, "logs", "memory-actions.jsonl");
const child_ledger_path = path.join(data_root, "网关围栏账本.jsonl");

// 网关等 recall 多久。给得小是**故意的**：超时那条测试要在这个数之内跑完，
// 而正常那几条测试的假 Loci 是秒回的，小超时不影响它们。
const relevance_timeout_ms = 1200;
// 假 Loci「慢」模式拖多久 —— 必须**明显大于**上面那个数，才复现得出那个 bug 的现场
const fake_loci_slow_ms = 3000;

// 🔴 这几个端口是真的东西在用，测试里一个都不许碰
const FORBIDDEN_PORTS = [3000, 3010, 3100, 18002, 18003];

let fake_upstream, fake_loci, gateway;
let gateway_port;

// ——— 挑端口：挑之前先真的 listen 一下，确认没被占用 ———
function is_port_free(port) {
  return new Promise((resolve) => {
    const probe = net.createServer();
    probe.once("error", () => resolve(false));
    // 不指定 host = 跟网关自己 listen 的方式一样（所有网卡），这样探得最严
    probe.once("listening", () => probe.close(() => resolve(true)));
    probe.listen(port);
  });
}
async function pick_free_ports(count, from = 19100, to = 19899) {
  const picked = [];
  for (let p = from; p <= to && picked.length < count; p += 1) {
    if (FORBIDDEN_PORTS.includes(p)) continue;
    if (await is_port_free(p)) picked.push(p);
  }
  if (picked.length < count) throw new Error(`19xxx 段里没挑够 ${count} 个空端口`);
  return picked;
}

// ——— 小工具 ———

/** 往网关发一句话，返回上游那份原始 messages（用来逐字比对「改没改」） */
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
      // 日志靠这个对上号，不靠「最后一行大概是我这条」。
      // 🔴 转义是必须的：HTTP 头的值是 ByteString（latin-1），中文塞不进去，
      //    fetch 会当场 TypeError。查日志那头照同样的方式转义再比。
      "X-Request-Id": encodeURIComponent(request_id),
      Authorization: "Bearer fake-key-do-not-look",
    },
    body: JSON.stringify({ model: "假模型", messages: messages, temperature: 0.3 }),
  });
  const body = await resp.json().catch(() => null);
  return { 状态: resp.status, 体: body, 原样: original };
}

/** 从 memory-actions.jsonl 里挑出这一次请求的那条「相关记忆提醒」记录 */
function find_notice_log(request_id) {
  if (!fs.existsSync(log_path)) return null;
  const wanted = encodeURIComponent(request_id);   // 跟发出去时同一个转义
  const lines = fs.readFileSync(log_path, "utf8").split(/\r?\n/).filter(Boolean);
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    let rec; try { rec = JSON.parse(lines[i]); } catch { continue; }
    if (rec.actor === "gateway/relevance_reminder" && String(rec.request_id) === wanted) return rec;
  }
  return null;
}

/** 网关控制台那一行末尾的「说了什么」（把毫秒抹掉，只留它对这次请求的判词） */
function console_verdict(delta) {
  const m = /→\s*\d{3}\s+\d+ms\s+(.*)$/m.exec(delta.trim());
  return m ? m[1].trim() : null;
}

/** 贴回去那一行里的数字：事件 N 条 · 认知 M 条 */
function parse_notice_line(text) {
  const m = /^〔记忆提醒〕和这句有关：事件 (\d+) 条 · 认知 (\d+) 条$/.exec(String(text).trim());
  return m ? { 事件: Number(m[1]), 认知: Number(m[2]) } : null;
}

/** 围栏账本（本进程的 + 网关子进程落在文件里的那份）合起来 */
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

  const [upstream_port, loci_port, gw_port] = await pick_free_ports(3);
  gateway_port = gw_port;
  for (const p of [upstream_port, loci_port, gateway_port]) {
    assert.ok(p >= 19100 && p <= 19899, `端口 ${p} 跑出 19xxx 段了`);
    assert.ok(!FORBIDDEN_PORTS.includes(p), `端口 ${p} 是真东西在用的`);
  }

  fake_upstream = await start_fake_upstream({ 端口: upstream_port });
  fake_loci = await start_fake_loci({ 端口: loci_port });

  // 🔴 预置戳戳送达的状态文件：假装她「刚刚才说过话」。
  //    这一单不测戳戳送达，但它跟被测路径在**同一个请求**里。没有这个文件的话
  //    它的闲时闸判「距上次 = Infinity」→ 直接开 → 每一次请求都会去敲假 Loci 的
  //    /api/loci/poke，「不触发时假 Loci 收到 0 个请求」这条断言就永远做不成。
  //    把闸关上，被测的那条路才是干净的（后面还有一条断言专门查 REST 口没被敲过）。
  fs.writeFileSync(
    path.join(data_root, "state", "poke-window.json"),
    JSON.stringify({ lastUserMessageTime: new Date().toISOString(), wakePending: false }, null, 2),
  );

  gateway = await start_gateway({
    端口: gateway_port,
    上游地址: fake_upstream.地址,
    loci地址: fake_loci.地址,
    数据根: data_root,
    相关超时毫秒: relevance_timeout_ms,
    白名单端口: [upstream_port, loci_port],      // 网关只准往这两个地方出门
    账本路径: child_ledger_path,
  });

  fence.allow(gateway_port);                     // 测试进程只准敲网关这一个门
});

after(async () => {
  // 只收自己起的东西：网关是自己 spawn 的（按 pid 掐），两个假服务在本进程里
  if (gateway) await gateway.停();
  if (fake_upstream) await fake_upstream.关();
  if (fake_loci) await fake_loci.关();
  if (!process.env.留下现场) fs.rmSync(data_root, { recursive: true, force: true });
});

// ============================================================
// 一、正向信号 —— 这一条最重要
// ============================================================

test("正向信号：带强档词的一句话过去，上游收到的 messages 真的被贴了东西", { timeout: 15000 }, async () => {
  fake_loci.设模式("正常");
  fake_upstream.清账(); fake_loci.清账();

  const line = "上次你说的那个超时的事，后来怎么样了？";
  const { 状态: status, 原样: original } = await send_line(line, "测试-正向");

  assert.strictEqual(status, 200, "网关得把上游的回应原样带回来");

  // ① 上游真的收到了这一轮（不是「网关自己觉得转发了」）
  assert.strictEqual(fake_upstream.收到.length, 1, "假上游应该正好收到一次转发");
  const upstream_body = fake_upstream.最后一笔().体;

  // ② 消息**真的被改了** —— 这就是那个 bug 活着的时候永远为假的那条断言。
  //    数组长了一条，而且长在**真尾巴**（最新 user 之后、整个 messages 的最末）。
  assert.strictEqual(upstream_body.messages.length, original.length + 1,
    "带强档词的一句话，上游收到的消息数应该比客户端发的多一条 —— 少了就说明注入压根没发生");
  const tail = upstream_body.messages[upstream_body.messages.length - 1];
  assert.strictEqual(tail.role, "system");
  assert.ok(String(tail.content).startsWith("〔记忆提醒〕"),
    `真尾巴那条应该是记忆提醒，实际是：${JSON.stringify(tail)}`);

  // ③ 原来的消息一个字没被动过（注入只许「加」，不许改她说的话）
  assert.deepStrictEqual(upstream_body.messages.slice(0, original.length), original);
  assert.strictEqual(upstream_body.model, "假模型", "请求体的其它字段不该被网关碰");
  assert.strictEqual(upstream_body.temperature, 0.3);

  // ④ 网关真的去查了记忆，查的是**她这句话**，走的是**假 Loci**
  const calls = fake_loci.工具调用.filter((c) => c.工具 === "recall");
  assert.ok(calls.length >= 1, "触发了就该真的调一次 recall");
  assert.strictEqual(calls[0].参数.query, line, "拿去查的应该就是她这句话原文");

  // ⑤ 日志这一头也对得上（出事的时候只有这儿看得见真相，所以它必须准）
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

  // 「注入发生了」不等于「注入有内容」—— 贴一行「事件 0 条 · 认知 0 条」也算贴了。
  // 所以单独钉一条：命中数真的 ≥ 1。
  assert.ok(counts.事件 + counts.认知 >= 1, "命中数必须 ≥ 1，贴个空壳等于没贴");

  // 数字不是网关自己编的，是从**假 Loci 那份检索结果**里数出来的：
  // 88.4 / 71.0 两条事件 + 63.2 一条认知（戴 🧠 牌）过线；12.7 那条必须被分数线挡掉。
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

  // 先确认**真的贴了** —— 不然这条测试会「因为压根没注入」而白白变绿，
  // 那正是这一单最要防的那种假绿。
  const tail = fake_upstream.最后一笔().体.messages.at(-1);
  assert.strictEqual(fake_upstream.最后一笔().体.messages.length, original.length + 1, "得先真的贴上了，这条测试才有意义");
  assert.strictEqual(tail.role, "system");
  const text = String(tail.content);
  assert.ok(text.startsWith("〔记忆提醒〕"), `检查的应该是提醒那一行，实际：${text}`);
  // 这条是网关自己写在红线上的纪律：给数量=拍他一下，给摘要=系统替他想起来了。
  // 顺手把「贴了但没贴对东西」也挡住了 —— 整段检索结果被囫囵贴进去也是一种坏。
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
// 二、不触发的那条路
// ============================================================

test("不触发：一句什么都不沾的话过去 —— 一次都不查记忆，消息一个字不改", { timeout: 15000 }, async () => {
  fake_loci.设模式("正常");
  fake_upstream.清账(); fake_loci.清账();

  const { 状态: status, 原样: original } = await send_line("嗯", "测试-不触发");
  assert.strictEqual(status, 200);

  // ① 假 Loci **一个请求都没收到**（不是「没收到 recall」，是整个门都没被敲过：
  //    MCP 握手没有、REST 的 /api/loci/poke 也没有）
  assert.deepStrictEqual(fake_loci.收到, [],
    `不该触发的一句话居然去敲了 Loci：${JSON.stringify(fake_loci.收到)}`);
  assert.strictEqual(fake_loci.工具调用.length, 0);

  // ② 消息**一个字不改**：上游收到的跟客户端发出去的逐字相同
  const upstream_body = fake_upstream.最后一笔().体;
  assert.deepStrictEqual(upstream_body.messages, original, "没触发就不该往 messages 里加任何东西");

  // ③ 日志上说得清「为什么没查」—— 不是「查了但空手而归」
  const record = find_notice_log("测试-不触发");
  assert.strictEqual(record.triggered, false);
  assert.strictEqual(record.trigger_kind, "none");
  assert.strictEqual(record.recall_called, false);
  assert.strictEqual(record.skipped, "not_triggered");
});

// ============================================================
// 三、Loci 挂掉的时候 —— 失败不许挡聊天，但这件事得能看出来
// ============================================================

test("Loci 回 500：网关照常转发，而且日志上看得出来它失败了", { timeout: 15000 }, async () => {
  fake_loci.设模式("五百");
  fake_upstream.清账(); fake_loci.清账();

  const { 状态: status, 体: body, 原样: original } = await send_line("上次那个 500 的事", "测试-五百");

  // ① 她的对话不许被弄死：照常转发、照常拿到回应
  assert.strictEqual(status, 200, "Loci 挂了也不许把用户的对话弄死");
  assert.strictEqual(body.choices[0].message.content, "假上游收到了。");
  assert.strictEqual(fake_upstream.收到.length, 1, "上游必须照常收到这一轮");
  assert.deepStrictEqual(fake_upstream.最后一笔().体.messages, original, "查失败了就别贴，更不许贴半截");

  // ② 但这件事**得能看出来** —— 日志里有一条带 error 的记录，
  //    而且能分清「触发了、去查了、失败了」和「压根没触发」。
  const record = find_notice_log("测试-五百");
  assert.strictEqual(record.triggered, true, "触发过");
  assert.strictEqual(record.recall_called, true, "去查过");
  assert.strictEqual(record.injected, false, "没贴成");
  assert.ok(record.error && /500/.test(record.error), `error 里应该看得出是 500，实际：${record.error}`);

  // ③ 它内部重试了一次（握手 → 调 → 失败 → 重新握手 → 再调）。
  //    钉住这个次数：失败的代价是**两倍**，不是一倍 —— 超时那条测试要用到这个事实。
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
// 四、超时 —— 那个 bug 的回归断言
// ============================================================

test("超时：假 Loci 比网关的超时还慢 → 转发照常，而且这件事说得出口", { timeout: 20000 }, async () => {
  // 先拿一句「压根没触发」的做对照组，等会儿要跟超时那次比控制台说了什么
  fake_loci.设模式("正常");
  fake_upstream.清账(); fake_loci.清账(); gateway.输出增量();
  await send_line("好的", "测试-超时对照");
  const control_verdict = console_verdict(await gateway.等增量());

  // 现在让假 Loci 慢过网关的超时 —— 这就是那个 bug 的现场：
  // 超时 5 秒、recall 要 5~7 秒，于是它每次都 abort，命中数恒为 0。
  fake_loci.设模式("慢", fake_loci_slow_ms);
  fake_upstream.清账(); fake_loci.清账();

  const t0 = Date.now();
  const { 状态: status, 体: body, 原样: original } = await send_line("上次那件事你还记得吗", "测试-超时");
  const elapsed = Date.now() - t0;
  const timeout_verdict = console_verdict(await gateway.等增量());

  // ① 她的对话不许被卡死，也不许被弄死
  assert.strictEqual(status, 200, "Loci 慢不许把用户的对话弄死");
  assert.strictEqual(body.choices[0].message.content, "假上游收到了。");
  assert.deepStrictEqual(fake_upstream.最后一笔().体.messages, original, "超时了就一个字都不该贴");

  // ② 超时真的生效了（不是傻等假 Loci 那 3 秒）。
  //    上限拿「两倍超时」算，因为它失败会重试一次 —— 见上一条测试钉住的那个 2。
  assert.ok(elapsed < relevance_timeout_ms * 2 + 1500,
    `等太久了：${elapsed}ms，超时设的是 ${relevance_timeout_ms}ms（重试一次也就 ${relevance_timeout_ms * 2}ms 上下）`);
  assert.strictEqual(fake_loci.工具调用.length, 2,
    "🔴 一次超时 = 两次 recall：它重试了一次，所以真实等待是 RELEVANCE_TIMEOUT_MS 的两倍");

  // ③ **这件事说得出口** —— 日志里有一条明确的失败记录，
  //    分得清「触发了、去查了、超时了」跟「压根没触发」。
  //    那个 bug 能活好几天，就是因为当时没人有地方看这一行。
  const record = find_notice_log("测试-超时");
  assert.ok(record, "超时也必须留下记录");
  assert.strictEqual(record.triggered, true, "触发过");
  assert.strictEqual(record.recall_called, true, "去查过");
  assert.strictEqual(record.injected, false, "没贴成");
  assert.strictEqual(record.event_count, 0);
  assert.strictEqual(record.mind_count, 0);
  assert.ok(record.error, "超时必须在日志里留下 error —— 没有这一行，它就是「安静地什么都不做」");

  // ④ 现状钉子（**不是在夸它**）：控制台上那一行，超时跟压根没触发**一模一样**。
  //    网关只会说「提醒无」，两种情形一个字都不差 —— 那个 bug 就活在这儿。
  //    判据只能去 memory-actions.jsonl 里拿（③ 那几条）。
  //    哪天网关把这行改成能分辨的，这条断言会红：那时候删掉它，然后高兴一下。
  assert.strictEqual(timeout_verdict, control_verdict,
    "现状：控制台分不出「超时」和「没触发」。这条钉的是现状，不是期望");
  assert.ok(/提醒无/.test(timeout_verdict || ""), `控制台判词长这样：${timeout_verdict}`);
});

// ============================================================
// 五、读到的东西看不懂的时候（读者：这条是**坑的证据**，不是在夸它）
// ============================================================

test("坑｜Loci 换个排版：一条都数不出来，而且日志上跟「本来就没相关记忆」一模一样", { timeout: 15000 }, async () => {
  // auto_attach.js 的 parse_score_line 读的是 Loci **给人看的渲染文本**（不是结构化 API），
  // 正则写死了「分数 + 两个空格 + …… + (圆括号里的 id)」。
  // Loci 那边哪天把日期挪到前面、id 换成方括号 —— 内容一个字没少，网关一条也认不出来。
  //
  // 🔴 这条测试钉的是**现状**：这种失明在日志里长得跟「今天确实没有相关记忆」
  //    一模一样（triggered=true、recall_called=true、0 条、injected=false、
  //    **连 error 都没有**）。也就是说：这个失败模式比那个 5 秒 bug 还难发现 ——
  //    5 秒 bug 至少还在日志里留了个 error。
  //    要是哪天网关学会了「解析不出分数行就吼一声」，这条会红，那时候改它。
  fake_loci.设模式("换排版");
  fake_upstream.清账(); fake_loci.清账();

  const { 状态: status, 原样: original } = await send_line("上次那个排版的事", "测试-换排版");
  assert.strictEqual(status, 200);

  // 真的去查了、也真的拿到了内容（假 Loci 这次是成功返回的）
  assert.strictEqual(fake_loci.工具调用.length, 1, "这次调用是成功的，不该有重试");
  // 但一条都没数出来，什么都没贴
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
// 六、转发本身 —— 🔴 这一节里有一条是**红的**，是网关真有 bug，不是我没写完
// ============================================================

test("🔴 上游 gzip 的时候，回应会被截断（gateway/server.js 第 136~139 行）", { timeout: 15000 }, async () => {
  // 怎么发生的：
  //   ① server.js 第 119 行把客户端的 accept-encoding 删了 —— 本意是「别让上游压缩」；
  //   ② 但转发用的是 Node 自带 fetch，**undici 自己又加了一个**
  //      `accept-encoding: gzip, deflate`（实测如此），于是上游照样 gzip；
  //   ③ fetch 把身子解压了，可 回.headers 里的 content-length 还是**压缩后**的数；
  //   ④ 第 137 行抄回应头时只跳过了 content-encoding，**content-length 照抄**。
  // 结果：客户端被告知「一共 107 字节」，实际身子是解压后的一千多字节 ——
  // Node 按 content-length 一刀切断，客户端拿到半截 JSON，parse 直接炸。
  //
  // 为什么它能活着：聊天基本都是 stream:true，流式回应是 chunked、没有 content-length，
  // 这条路绕开了。非流式的请求（补全、embedding、任何 SDK 的同步调用）就中招。
  //
  // 这条断言写的是**应该怎样**，所以现在是红的。修法二选一（都在 server.js 第 137 行）：
  //   · 抄回应头时把 content-length 跟 content-encoding 一起跳过（身子已经解压了，
  //     长度不该再声明），或者
  //   · 别删 content-encoding，改成把原始压缩字节原样转发。
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
    JSON.parse(text);   // 拿到半截的话这儿也会炸
  } finally {
    fake_upstream.设压缩(false);   // 别把后面的测试带沟里
  }
});

// ============================================================
// 七、对账 —— 一条请求都没漏到假环境之外
// ============================================================

test("对账：整套跑下来，出门的连接一条都没漏到假环境之外", { timeout: 15000 }, () => {
  const records = all_outbound_records();
  assert.ok(records.length > 0, "账本是空的，说明围栏根本没挂上 —— 那前面的「没漏」全是空话");

  const allowed_ports = new Set([fake_upstream.端口, fake_loci.端口, gateway_port]);
  const out_of_range = records.filter((rec) => !allowed_ports.has(Number(rec.端口)));
  assert.deepStrictEqual(out_of_range, [],
    `有连接打到假环境之外去了：${JSON.stringify(out_of_range)}`);

  // 被拦下的一条都不该有（有的话说明有条路我没改道，围栏替我挡下来了）
  const blocked = records.filter((rec) => rec.放行 === false);
  assert.deepStrictEqual(blocked, [], `围栏拦下了这些：${JSON.stringify(blocked)}`);

  // 逐个点名：真的东西一个都没被敲过
  for (const real_port of FORBIDDEN_PORTS) {
    assert.ok(!records.some((rec) => Number(rec.端口) === real_port), `敲到真的 ${real_port} 了`);
  }

  // 假 Loci 的 REST 面（戳戳送达那条路）**整套跑下来**一次都没被敲响 ——
  // 用的是清账清不掉的那本全程账，说明预置的闲时闸真的一直关着，
  // 前面那条「假 Loci 收到 0 个请求」不是运气好。
  const rest_hits = fake_loci.全程收到.filter((rec) => rec.路径.startsWith("/api/loci/"));
  assert.deepStrictEqual(rest_hits, [], "戳戳送达那条路不该在这一单里出声");

  // 全程只有 recall 这一个工具被调过 —— 网关没在背地里调别的（尤其不许写记忆）
  const other_tools = fake_loci.全程收到
    .filter((rec) => rec.rpc方法 === "tools/call")
    .map((rec) => rec.体?.params?.name)
    .filter((name) => name !== "recall");
  assert.deepStrictEqual(other_tools, [], `网关调了 recall 之外的工具：${other_tools.join(",")}`);

  console.log(`[对账] 出门 ${records.length} 次，端口只有：${[...new Set(records.map((rec) => rec.端口))].join(", ")}`);
});

// ============================================================
// 八、`/health` —— 2026-08-20 新加的只读口
//
// 它存在的**全部意义**是「错了能立刻知道」。所以这一节的判据不是「它返回 200」，
// 是「**它说的话是不是真的**」：坏了要说坏，没坏别喊狼来了。
//
// 📌 这个口一开始叫 `/健康`，**谁也敲不响** —— 客户端送来的是转义过的
//    `/%E5%81%A5%E5%BA%B7`（Windows 上的 curl 更是按本地代码页转成 `/%BD%A1%BF%B5`），
//    而路由是逐字节比的，永远对不上；生 socket 送原样 UTF-8 字节的话，Node 的
//    HTTP 解析器直接 400（HPE_INVALID_URL），handler 根本轮不到跑。
//    比"没反应"更糟的是它**悄悄地错**：没匹配上就掉进转发那条路，
//    这个健康探测会被**当成普通流量转发给上游模型**。
//    现在改名 `/health`（ASCII），根上就没有这个问题了。
//    ⚠️ 老路径那条路留了一条断言在这儿看着（本节第一条），判据只有一个：
//       **不许漏到上游去。**
//
// ⚠️ 这一节故意排在「对账」后面：新起的测试网关用的是新端口，排在前面会让
//    已有那条对账因为「不认识这些端口」而红 —— 那不是漏，是它不知道有新端口。
//    我不动已有的十条，所以这一节自己在末尾补一次对账（第八节最后一条）。
// ============================================================

// 这一节自己起干净网关：**每条测试一份自己的日志**。
// 为什么不共用前面那个网关：健康口的结论是从日志**整段**推出来的，前面十条测试
// 已经在主日志里留下了一堆成功记录 —— 拿它做底，「坏了」这种结论永远出不来
// （这本身就是漏报，见「漏报」那条）。要判它说得准不准，底必须是干净的。
let clean_gateway_count = 0;
async function start_clean_gateway(label, { make_data_root = true } = {}) {
  clean_gateway_count += 1;
  const [port] = await pick_free_ports(1, 19300 + clean_gateway_count * 4);
  const root = path.join(__dirname, `.跑测试留下的东西-${label}`);
  fs.rmSync(root, { recursive: true, force: true });
  if (make_data_root) {
    fs.mkdirSync(path.join(root, "logs"), { recursive: true });
    fs.mkdirSync(path.join(root, "state"), { recursive: true });
    // 跟主网关一样把戳戳的闲时闸关死：这一节测的是健康口，不是戳戳
    fs.writeFileSync(path.join(root, "state", "poke-window.json"),
      JSON.stringify({ lastUserMessageTime: new Date().toISOString(), wakePending: false }));
  }
  fence.allow(port);                      // 测试进程要敲它
  const gw = await start_gateway({
    端口: port,
    上游地址: fake_upstream.地址,
    loci地址: fake_loci.地址,
    数据根: root,
    相关超时毫秒: relevance_timeout_ms,
    白名单端口: [fake_upstream.端口, fake_loci.端口],
    账本路径: child_ledger_path,               // 共用主账本，末尾一起对
  });
  return {
    网关: gw,
    端口: port,
    数据根: root,
    日志档: path.join(root, "logs", "memory-actions.jsonl"),
    async 收() {
      await gw.停();
      if (!process.env.留下现场) fs.rmSync(root, { recursive: true, force: true });
    },
  };
}

/** 往指定网关发一句话（前面十条用的 send_line 打的是主网关，那条不动） */
async function send_line_to(target_gw, line, request_id) {
  const resp = await fetch(`${target_gw.地址}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Request-Id": encodeURIComponent(request_id) },
    body: JSON.stringify({ model: "假模型", messages: [{ role: "user", content: line }] }),
  });
  await resp.text();
  return resp.status;
}

/** 用**最普通的方式**敲健康口 —— 客户端怎么敲，这儿就怎么敲，不搞特殊姿势 */
async function hit_health(target_gw, route = "/health") {
  const resp = await fetch(`${target_gw.地址}${route}`, { method: "GET" });
  const raw = await resp.text();
  let body = null; try { body = JSON.parse(raw); } catch { /* 不是 JSON 就算了 */ }
  return { 状态: resp.status, 体: body, 原文: raw };
}
/** 拿到的是不是健康口自己的回答（不是被转发给上游之后上游的那份） */
function is_health_answer(body) { return Boolean(body) && typeof body.结论 === "string"; }

/**
 * 读一次健康口，顺带**断言这门是通的**。
 * 🔴 这儿是硬断言不是 skip：门通不通本身就是要守的东西 ——
 *    哪天路由又被改成敲不响，这一节该**红**，不该悄悄地跳过去。
 */
async function read_health(target_gw) {
  const { 状态: status, 体: body, 原文: raw } = await hit_health(target_gw);
  assert.ok(is_health_answer(body),
    `GET /health 应该拿到健康口自己的 JSON（带「结论」那份）。实际状态 ${status}，拿到：${raw.slice(0, 160)}`);
  return body;
}

/** 按 build_health() 自己那套口径把日志里的「相关记忆提醒」挑出来（它读的就是这些） */
function read_notice_records(log_file) {
  if (!fs.existsSync(log_file)) return [];
  return fs.readFileSync(log_file, "utf8").split(/\r?\n/).filter(Boolean)
    .map((raw_line) => { try { return JSON.parse(raw_line); } catch { return null; } })
    .filter((rec) => rec && rec.action === "relevance_reminder_observed");
}

const STRONG_LINES = ["上次那件事怎么样了", "还记得吗那天夜里", "以前我们是怎么弄的", "之前那份单子呢", "那时候你说过什么"];
const NEUTRAL_LINES = ["嗯", "好的", "哦哦"];

test("健康口｜/health 通，而且老路径 /健康 不许漏给上游", { timeout: 20000 }, async () => {
  // 这条盯两件事，第二件才是要害。
  //
  // ①（新路径）`/health` 敲得响，回的是健康口自己那份 JSON。
  //
  // ②（老路径）`/健康` **不许被当成聊天流量转发给上游**。
  //    判据只有一个：**假上游一条都没收到**。老路径现在回什么都行（404 也好、
  //    本地随便答一句也罢），但只要它还会漏到上游，这条就该红 ——
  //    因为真跑起来那头是 DeepSeek：一个用来看"它在不在工作"的探测，
  //    自己不工作，还把探测发到人家账单上去了。
  //
  // 两种写法都要试：客户端敲 `/健康` 时 fetch 会转义成 `/%E5%81%A5%E5%BA%B7`，
  // 而有人可能直接照着旧文档粘那串转义后的路径 —— 两条都是"老路径"。
  const rig = await start_clean_gateway("路由");
  try {
    // ① 新路径通
    fake_upstream.清账(); fake_loci.清账();
    const health = await read_health(rig.网关);
    assert.ok(typeof health.日志档 === "string", "健康口该报出它读的是哪份日志");
    assert.deepStrictEqual(fake_upstream.收到, [], "/health 是本地只读口，一个字都不该转发出去");

    // ② 老路径不许漏
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

    // —— 不用进门就能验的：build_health() 读的那份原料，得先是对的 ——
    const raw_records = read_notice_records(rig.日志档);
    assert.strictEqual(raw_records.length, 3);
    assert.strictEqual(raw_records.filter((r) => r.triggered).length, 3, "三句都带强档词，该三次都触发");
    assert.strictEqual(raw_records.filter((r) => r.injected).length, 3, "假 Loci 正常返回，该三次都贴上");

    const health = await read_health(rig.网关);

    assert.strictEqual(health.结论, "在工作");
    assert.ok(health.真的贴上 >= 1, `真的贴上应该 ≥ 1，实际 ${health.真的贴上}`);
    assert.ok(health.最近一次真的贴上, "该有「最近一次真的贴上」");
    assert.ok(health.最近一次真的贴上.命中 >= 1,
      `最近一次真的贴上的命中数应该 ≥ 1，实际 ${health.最近一次真的贴上.命中}`);
    assert.strictEqual(health.出过错, 0);
  } finally { await rig.收(); }
});

test("健康口｜那个 bug 的形状（触发了但一次都没贴上）必须被认出来", { timeout: 20000 }, async () => {
  const rig = await start_clean_gateway("bug形状");
  try {
    // 假 Loci 一直 500 —— 网关照常转发、聊天照常，界面上什么都看不出来。
    // 这正是那个「超时 5 秒、从上线起一次没工作过」的形状。
    fake_loci.设模式("五百"); fake_upstream.清账(); fake_loci.清账();
    for (let i = 0; i < 3; i += 1) await send_line_to(rig.网关, STRONG_LINES[i], `健康-坏了-${i}`);

    const raw_records = read_notice_records(rig.日志档);
    assert.strictEqual(raw_records.filter((r) => r.triggered).length, 3, "三次都该触发");
    assert.strictEqual(raw_records.filter((r) => r.injected).length, 0, "三次都该没贴上");
    assert.strictEqual(raw_records.filter((r) => r.error).length, 3, "三次都该留下 error");

    const health = await read_health(rig.网关);

    assert.ok(/🔴/.test(health.结论),
      `坏成这样必须报红，实际结论是：${health.结论}`);
    assert.strictEqual(health.真的贴上, 0, "一次都没贴上");
    assert.strictEqual(health.触发过, 3);
    assert.ok(health.最近一次出错 && health.最近一次出错.是什么, "得说得出上次错在哪儿");
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

    // 🔴 这条最要紧：**误报比不报更坏**。没人说到相关的事是日常，不是故障。
    assert.ok(!/🔴/.test(health.结论), `没触发不该报红，实际：${health.结论}`);
    assert.ok(health.结论.includes("一次都没触发"), `实际结论：${health.结论}`);
    assert.strictEqual(health.触发过, 0);
    assert.strictEqual(health.真的贴上, 0);
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
  // LOCI_GATEWAY_DATA 指到一个压根不存在的目录 —— 刚装完、或者路径配歪了，就是这样。
  const rig = await start_clean_gateway("无日志", { make_data_root: false });
  try {
    fake_upstream.清账(); fake_loci.清账();
    const { 状态: status, 体: body } = await hit_health(rig.网关);
    assert.strictEqual(status, 200, "日志不在不是错误，不该 500");

    const health = is_health_answer(body) ? body : await read_health(rig.网关);

    assert.ok(health.结论.includes("还没有任何一次记录"), `实际结论：${health.结论}`);
    assert.strictEqual(health.最近这些轮, 0);
    assert.ok(!/🔴/.test(health.结论), "没有日志不等于坏了，不许报红");
  } finally { await rig.收(); }
});

// ——— 它自己会不会撒谎 ———
// 下面两条不测「口通不通」，测的是**判据本身**。它们今天就跑得起来（验的是
// build_health() 读的那份原料），因为原料里已经能看出结论会是什么。

test("撒谎·漏报｜陈年的成功会盖住今天的全面失效", { timeout: 25000 }, async () => {
  // 🔴 build_health() 判「在工作」的条件是：**窗口里有过任意一条 injected**。
  //    窗口是「日志最后 200 行」，不是「最近多久」—— 所以昨天成功过的记录
  //    会一直待在窗口里，把今天的全面失效盖住，结论照说「在工作」。
  //    这是这四种取值里**最危险的一种**：它恰好在真出事的时候说没事，
  //    而这个口存在的全部意义就是出事的时候吭一声。
  // 📌 判据该改成「最近这一段」而不是「有史以来」，两种写法都够用：
  //      · 只看最近 K 次触发（比如 10 次）里有没有成功过；
  //      · 或者看「距最近一次成功之后，又失败了多少次」——超过 3 次就报红。
  //    需要的数据都已经在手上了（贴过.time 已经算出来了）。
  const rig = await start_clean_gateway("漏报");
  try {
    fake_upstream.清账(); fake_loci.清账();
    fake_loci.设模式("正常");
    for (let i = 0; i < 2; i += 1) await send_line_to(rig.网关, STRONG_LINES[i], `健康-漏报-好-${i}`);
    fake_loci.设模式("五百");                       // 从这一刻起它彻底不工作了
    for (let i = 0; i < 4; i += 1) await send_line_to(rig.网关, STRONG_LINES[i], `健康-漏报-坏-${i}`);

    const raw_records = read_notice_records(rig.日志档);
    assert.strictEqual(raw_records.length, 6);
    // 现在这一刻：最近连着 4 轮全崩
    const last_four = raw_records.slice(-4);
    assert.ok(last_four.every((r) => r.triggered && !r.injected && r.error),
      "最近四轮应该是「触发了、没贴上、有 error」");
    // 而窗口里还躺着两条陈年的成功 —— 就是这两条让结论说「在工作」
    assert.strictEqual(raw_records.filter((r) => r.injected).length, 2,
      "窗口里还留着早先那两条成功记录 —— 漏报就是它们造成的");

    const health = await read_health(rig.网关);
    // 门修好之后这条会**红**，红的是判据不是测试：它这时候必须报红。
    assert.ok(/🔴/.test(health.结论),
      `最近四轮全崩，健康口却说「${health.结论}」—— 陈年的成功把今天的失效盖住了`);
  } finally { await rig.收(); }
});

test("撒谎·误报｜库里本来就没有相关的东西，会被说成「它在安静地什么都不做」", { timeout: 25000 }, async () => {
  // 假 Loci 查得好好的，就是**一条相关的都没有**（新装的人第一天、或者话题确实
  // 没聊过，都是这样）。日志里留下的是：触发了、查了、0 条、没贴上、**没有 error**。
  // 而 build_health() 只看「贴上了没有」，于是喊 🔴「它在安静地什么都不做」——
  // 可它明明工作得好好的。**开源出去第一天就会有人看见这个红。**
  //
  // 📌 手上其实有分得开的料：`出过错`。判据该分两句话说 ——
  //      错了 > 0        → 「🔴 在坏」（这才是那个 bug 的形状）
  //      错了 = 0、0 命中 → 「查了，但一条都没过线」（可能是库空 / 分数线太高 /
  //                          Loci 改了渲染排版 → 见第五节那条「换排版」）
  //    ⚠️ 但要说清：**「库里没有」和「解析瞎了」这两件事，日志里长得一模一样**
  //       （都是 triggered + recall_called + 0 条 + 没有 error）。
  //       谁也分不开，所以这句话只能说「一条都没过线」，不能替人下结论。
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

    // 门修好之后，这儿就是那句误报会出现的地方（现在只把料钉住，不替人定结论）
    const health = await read_health(rig.网关);
    assert.strictEqual(health.出过错, 0, "出过错必须是 0 —— 这是分辨「没命中」和「坏了」的唯一线索");
  } finally { await rig.收(); }
});

test("对账·第八节：健康这一节新起的网关，也一条都没漏出去", { timeout: 15000 }, () => {
  // 已有那条对账排在这一节前面，管不到这几个新端口 —— 这儿自己补一次。
  const records = all_outbound_records();
  const out_of_range = records.filter((rec) => Number(rec.端口) < 19100 || Number(rec.端口) > 19899);
  assert.deepStrictEqual(out_of_range, [], `有连接打到 19xxx 之外去了：${JSON.stringify(out_of_range)}`);
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
