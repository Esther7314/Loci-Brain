// ============================================================
// gateway/server.js —— Loci 的 mini gateway（2026-08-19）
//
// **它干的事**：你的客户端把聊天请求发给它，它转发给真正的模型，
// 转发之前拍两下 Loci，把该让 AI 知道的东西加进这一轮的消息里。
//
// 两下，位置不一样，别搞混：
//   · 戳戳送达（poke_delivery.js）   梦 / 发呆，贴在稳定前缀区
//   · 相关记忆提醒（auto_attach.js） **贴真尾巴** —— 最新 user 之后、整个 messages 的最末
//
// 🔴 提醒为什么要最后贴：位置得「离模型开口最近」。
//    所以 build_relevance_notice() 自己不碰 messages，只把 patch 算出来还给你，
//    等别的都插完、请求体组好了，最后一步才 attach_at_true_tail()。顺序反了位置就错了。
//
// ⛔ **这个外壳不替 AI 调 breath()。** auto_attach.js 里还有一个「开窗第一轮把 breath
//    整段贴进 system」的函数（`attach_once`），它是她自己那套网关的做法，这儿**故意不接**：
//    「开口之前先 breath()」是 **AI 自己该伸的那只手**，写在系统提示里
//    （docs/系统提示-中文.md）。网关替它贴进去，它就不再是"自己想起来要睁眼"，
//    而是"被人喂了一份摘要"——那是两种完全不同的东西。
//    你要是就想要网关代劳，那个函数在模块里现成的，自己接一行就是。
//
// 🔴 三条边界（跟两个模块同源，改的时候别丢）：
//   · **失败不挡聊天**。任何一下拍空了——超时、Loci 没起、返回不是 JSON——
//     都照常转发，只记一行日志。宁可这次没贴上，也不能让人的对话卡住。
//   · **只读多，写极少**。碰的是 breath / recall（读）和 poke / dream.wake
//     （只读口 + 幂等信号）。⛔ 不改任何记忆。
//   · **不报正文，只报有什么**。B 插进去的是「有几条相关记忆」这种**数量**，
//     判断权留给 AI 自己。
//
// 零依赖：只用 Node 自带的 http / fetch（Node 18+）。
//
// 跑：
//     LOCI_UPSTREAM=https://api.deepseek.com/v1 node gateway/server.js
// 然后把客户端的 base_url 指到 http://127.0.0.1:3100/v1。
// API key 照常由客户端带 —— 这一层原样转发，**不存也不看**。
// ============================================================

const http = require("http");
const path = require("path");
const { Readable } = require("stream");
const auto = require("./auto_attach.js");
const poke = require("./poke_delivery.js");

const port = Number(process.env.PORT || 3100);
const upstream = (process.env.LOCI_UPSTREAM || "").replace(/\/+$/, "");
const LOCI = process.env.LOCI_MCP || poke.DEFAULT_ADDRESS;
const idle_threshold_minutes = Number(process.env.POKE_IDLE_MINUTES || poke.DEFAULT_IDLE_MINUTES);
const min_score = Number(process.env.RELEVANCE_MIN_SCORE || auto.DEFAULT_MIN_SCORE);
const data_root = process.env.LOCI_GATEWAY_DATA || path.join(__dirname, "data");
// ⚰️ 2026-08-19：**近期记忆视图整个撤了**（她拍的）。
//    它做的是「隔天开窗，把 recall(when="昨天") 的原文摘录贴进去」，
//    而她要的「昨日记忆」根本不用去 Loci 翻 —— **就是收窗时压出来的那几句话**，
//    第二天带着它开窗就完了。多问 Loci 一次，换来的是同一件事的第二个做法。
//    她的原话：「昨日记忆我说的就是压缩说过的话 就好了 根本就不要 recall 昨天」。
//    思路留在 gateway/README.md 第四节（收窗压缩），代码不留 ——
//    文档里不提、代码里还活着，就是留着一条没入口的路。
const log_path = path.join(data_root, "logs", "memory-actions.jsonl");

if (!upstream) {
  console.error("没配 LOCI_UPSTREAM —— 我不知道该把请求转给谁。");
  console.error("例：LOCI_UPSTREAM=https://api.deepseek.com/v1 node gateway/server.js");
  process.exit(1);
}

function read_body(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

function is_chat(req, body) {
  return req.method === "POST"
    && /\/chat\/completions$/.test(req.url.split("?")[0])
    && body && Array.isArray(body.messages);
}

// ════════════════════════════════════════════════════════════════
// 「它在不在工作」—— 只读，从它自己已经在写的那份日志里推出来
// ════════════════════════════════════════════════════════════════
// 🔴 2026-08-20 加的，起因就是这个网关自己：**超时设成 5 秒，于是它从上线起
//    一次都没工作过，活了好几天没人发现**。它没崩、没报错、控制台上也看不出区别 ——
//    `说.push("提醒无")` 这三个字，超时、500、连不上、和「这句话本来就不该查」，
//    印出来一模一样。
// 📌 判据：**别问「进程在不在」，问「它最近一次真的干成活是什么时候」。**
//    前者只证明它站在那儿，后者才是它在工作的证据。
// ⚠️ 故意**不新建一套记账**：`memory-actions.jsonl` 已经把 injected / error /
//    triggered 一条条写下来了。多一套账就多一个「账本自己坏了而没人知道」的地方,
//    而那正是这一段要治的病。
const WINDOW_SIZE = 200;         // 看最近多少条**相关记忆检查**（不是多少行日志）
const MAX_READ_BYTES = 4 * 1024 * 1024;

function read_recent_records(n = WINDOW_SIZE) {
  const fs = require("fs");
  try {
    if (!fs.existsSync(log_path)) return null;   // null = 文件不在（跟「文件在但没记录」分开）
    // 🔴 **真的只读尾巴。** 第一版注释写着「只读尾巴」，实际是 readFileSync 整个文件
    //    读进内存再切最后 200 行 —— 日志长到几百兆时它照样慢、照样吃内存。
    //    注释跟代码不符比没有注释更坏：**下一个人会信它。**
    const size = fs.statSync(log_path).size;
    const start = Math.max(0, size - MAX_READ_BYTES);
    const fd = fs.openSync(log_path, "r");
    const buf = Buffer.alloc(Math.min(size, MAX_READ_BYTES));
    fs.readSync(fd, buf, 0, buf.length, start);
    fs.closeSync(fd);
    let text = buf.toString("utf8");
    if (start > 0) text = text.slice(text.indexOf("\n") + 1);   // 头一行多半被切断了，扔掉
    // 🔴 **先筛再切**，不是先切再筛。同一份 jsonl 里还有戳戳/做梦的记录 ——
    //    戳戳一吵就会把相关记忆那些挤出窗口，极端情况下健康口会说
    //    「还没有任何一次记录」，而其实上面全是。
    const records = [];
    for (const l of text.trimEnd().split("\n")) {
      let r = null;
      try { r = JSON.parse(l); } catch { continue; }
      if (r && r.action === "relevance_reminder_observed") records.push(r);
    }
    return records.slice(-n);
  } catch { return []; }
}

function build_health() {
  // 🔴 **一种形状，不管有没有日志。** 第一版在「文件不在」那支提前 return 了一个短对象，
  //    少了那几个计数字段 —— 读的人得应付两种形状，而这口子存在的意义就是「一眼看明白」。
  //    区分照样保留（日志档存在: false + 结论里说清楚），但字段一个不少。
  const read_records = read_recent_records();
  const log_exists = read_records !== null;
  const records = read_records || [];
  const seconds_ago = (t) => (t ? Math.round((Date.now() - new Date(t).getTime()) / 1000) : null);
  const number_or_raw = (v) => (v === undefined || v === null || v === "" ? "（没设，用默认）"
                            : (Number.isFinite(Number(v)) ? Number(v) : `⚠️ 这不是个数：${JSON.stringify(v)}`));

  const triggered_records = records.filter((r) => r.triggered);
  const injected_records = records.filter((r) => r.injected);
  const error_records = records.filter((r) => r.error);
  const last_injected = [...records].reverse().find((r) => r.injected) || null;
  const last_error = [...records].reverse().find((r) => r.error) || null;

  // 🔴 **最近一次成功「之后」又崩了几次** —— 第一版判的是「窗口里有没有成功过」，
  //    那是会撒谎的：窗口是最后 200 条，不是最近一段时间，
  //    **昨天的一次成功会一直待在窗口里，把今天的全面失效整个盖住**。
  //    实测过：2 轮正常 + 4 轮连崩，它照样说「在工作」—— 那正是它最该吭声的时刻。
  //    而且反过来想：当初那个「超时 5 秒」的 bug，只要早先有过任何一次成功，
  //    这个口照样看不出来。**一个会撒谎的监控比没有监控更坏。**
  const last_success_index = records.map((r) => !!r.injected).lastIndexOf(true);
  const errors_since_success = records.slice(last_success_index + 1).filter((r) => r.error).length;

  let verdict;
  if (!log_exists) {
    // 「文件不在」和「文件在但还没记录」是两回事：前者八成是 LOCI_GATEWAY_DATA 配歪了，
    // 健康口读的日志跟网关写的根本不是同一份 —— 那样它会永远说「还没有记录」，
    // 而网关其实一直在好好干活。**这两种情况必须分得开。**
    verdict = "还没有任何一次记录 —— 日志档还不存在（刚起来？还是 LOCI_GATEWAY_DATA 指错了？）";
  } else if (records.length === 0) {
    verdict = "还没有任何一次记录 —— 它可能刚起来，也可能从来没被调用过";
  } else if (triggered_records.length === 0) {
    // ⚠️ 不报红：没人说到相关的事，本来就该一次都不触发。
    //    但要提一句语言 —— 强档词表出厂是中文的，不说中文的人会永远停在这一句上，
    //    而这一句还安慰他「可能正常」。**那是漏报一整类用户。**
    verdict = "最近这些轮里一次都没触发（可能正常：没人说到相关的事）"
      + (records.length >= 30 ? "　⚠️ 攒了这么多轮一次都没触发，也可能是强档词表跟你说的语言对不上" : "");
  } else if (errors_since_success >= 3) {
    verdict = `🔴 它是刚坏的：最近一次真的贴上之后，又连着失败了 ${errors_since_success} 次`;
  } else if (injected_records.length > 0) {
    verdict = "在工作";
  } else if (error_records.length > 0 && triggered_records.length >= 2) {
    // 这才是那个 bug 的形状：**触发了、报错了、一次都没贴上**。
    // ⚠️ 要 ≥2 次才喊：README 自己写着「Loci 重启后第一次相关检查大概率超时」，
    //    冷启动那一次报红等于每次重启都狼来了。
    verdict = `🔴 触发了 ${triggered_records.length} 次，出错 ${error_records.length} 次，一次都没贴上 —— 它在安静地什么都不做`;
  } else if (error_records.length === 0) {
    // 🔴 **不报红。** 第一版这儿会喊 🔴，而它逮到的是一个完全正常的人：
    //    新装的、库里本来就没有相关的东西 —— 查得好好的、0 命中、一个错都没有。
    //    开源出去第一天就有人看见这个红。
    // ⚠️ 而这句话有天花板，得说出来：「库里真没有」和「解析瞎了」
    //    （Loci 换了渲染排版）在日志里**长得一模一样**，谁也分不开。
    //    所以只列可能性，**不下「一切正常」这个结论**。
    verdict = `触发了 ${triggered_records.length} 次，一条都没过线（没报错：可能库里确实没有 / 分数线太高 / Loci 改了渲染排版）`;
  } else {
    verdict = `触发了 ${triggered_records.length} 次还没贴上过，出错 ${error_records.length} 次 —— 次数还太少，再看看`;
  }

  return {
    结论: verdict,
    最近这些轮: records.length,
    触发过: triggered_records.length,
    真的贴上: injected_records.length,
    出过错: error_records.length,
    最近一次真的贴上之后又崩了: errors_since_success,
    最近一次真的贴上: last_injected ? { 几秒前: seconds_ago(last_injected.time), 命中: (last_injected.event_count || 0) + (last_injected.mind_count || 0) } : null,
    最近一次出错: last_error ? { 几秒前: seconds_ago(last_error.time), 是什么: String(last_error.error).slice(0, 200) } : null,
    // ⚠️ 不抄 auto_attach.js 里那个默认值（12000）—— 抄一份就多一处会漂的常量。
    //    「没设」本身就是要看见的信息。打错字也照实说，这口子存在的意义就是逮配歪了的东西。
    超时设的是: number_or_raw(process.env.RELEVANCE_TIMEOUT_MS),
    相关度最低分: number_or_raw(min_score),
    日志档: log_path,
    日志档存在: log_exists,
  };
}


const server = http.createServer(async (req, res) => {
  const start = Date.now();

  // 只读健康口。放在最前面：它不该被后面任何一步影响，也不该影响任何一步。
  // 🔴 2026-08-20：这条路由**一开始写的是 `/健康`，敲不响** —— 客户端送来的是
  //    转义过的 `/%E5%81%A5%E5%BA%B7`，而这儿是逐字节比的，永远对不上。
  //    后果比"没反应"更糟：**这个健康探测会被当成普通请求转发给上游模型。**
  //    一个用来看"它在不在工作"的口，自己不工作，还顺手把请求发出去了。
  //    ⚠️ 修法不是在这儿 decodeURIComponent —— 那只是把问题藏起来。
  //       **URL 里就不该有非 ASCII。** 改名 `/health`，问题从根上没有了。
  if (req.method === "GET" && req.url.split("?")[0] === "/health") {
    req.resume();   // GET 没身子，但别把没读走的字节留在 keep-alive 连接上顶歪下一个请求
    let payload = "{}";
    try { payload = JSON.stringify(build_health(), null, 2); }
    catch (err) { payload = JSON.stringify({ 结论: "健康口自己算不出来了", 错: String(err?.message || err) }); }
    const buf = Buffer.from(payload, "utf8");
    res.writeHead(200, { "Content-Type": "application/json; charset=utf-8", "Content-Length": buf.length });
    res.end(buf);
    return;
  }

  const raw = await read_body(req).catch(() => Buffer.alloc(0));
  let body = null;
  try { body = raw.length ? JSON.parse(raw.toString("utf8")) : null; } catch { body = null; }

  const notes = [];
  if (is_chat(req, body)) {
    const common = {
      messages: body.messages,
      requestId: String(req.headers["x-request-id"] || start),
      logPath: log_path,
      地址: LOCI,
    };

    // ---- D：戳戳送达。梦 / 发呆，贴在跟 A 同一处前缀。 ----
    try {
      const d = await poke.attach_once({
        ...common,
        statePath: path.join(data_root, "state", "poke-window.json"),
        闲时阈值分钟: idle_threshold_minutes,
      });
      notes.push(d.patchInjected ? `戳戳(梦=${d.hasDream} 发呆=${d.musePending})`
        : d.calledLoci ? "戳戳无" : "戳戳没问(不够闲)");
    } catch (err) { notes.push("戳戳炸:" + (err?.message || err)); }

    // ---- B：相关记忆提醒。**最后一步，贴真尾巴** ----
    // 触发才跑（强档=关键词命中，弱档=本地判据）。没触发一次 recall 都不调。
    try {
      const b = await auto.build_relevance_notice({ ...common, 最低分: min_score });
      if (b && b.patch) {
        auto.attach_at_true_tail(body.messages, b.patch);
        notes.push("提醒(贴了真尾巴)");
      } else notes.push("提醒无");
    } catch (err) { notes.push("提醒炸:" + (err?.message || err)); }
  } else notes.push("不是聊天，直接转发");

  // 🔴 **不像 API 的路径一律本地 404。**（2026-08-20，网关测试逮到的一整类）
  //    这一层是代理，它的默认行为是「什么都往上游转」—— 所以它**没有「不认识的地址」**
  //    这回事。写错一个路由，在普通服务器上是 404，在这儿是**照常转发出去**。
  //    实际后果：`/favicon.ico`、打错的 URL、扫描器的探针，
  //    全都带着客户端那把 Authorization 转给上游。
  //    起因是我加健康口时把路径写成了中文 `/健康`，客户端送来的是转义过的，
  //    比不上 → 掉进这条默认路 → **一个用来看「它在不在工作」的探测被发给了上游模型**。
  //    ⚠️ 治标的修法（在健康口那儿 decodeURIComponent）救不了：Windows 上 curl
  //       发的是按本地代码页转的 `%BD%A1%BF%B5`，decodeURIComponent 对它直接抛异常，
  //       还是漏。**治本是这一条：只放 API 路径出去。**
  const route = req.url.split("?")[0];
  if (!route.startsWith("/v1/")) {
    req.resume();
    const payload = Buffer.from(JSON.stringify({
      error: `这一层只转发 /v1/* 的请求，${route} 没往上游发。`,
      提示: "看它在不在工作：GET /health",
    }, null, 2), "utf8");
    res.writeHead(404, { "Content-Type": "application/json; charset=utf-8", "Content-Length": payload.length });
    res.end(payload);
    console.log(`[gateway] ${req.method} ${req.url} → 404（不是 /v1/*，没往上游发）`);
    return;
  }

  const forward_body = body ? Buffer.from(JSON.stringify(body)) : raw;
  const headers = { ...req.headers };
  delete headers.host; delete headers["content-length"]; delete headers["accept-encoding"];

  const target = upstream.replace(/\/v1$/, "") + req.url;
  let resp;
  try {
    resp = await fetch(target, {
      method: req.method,
      headers: headers,
      body: ["GET", "HEAD"].includes(req.method) ? undefined : forward_body,
    });
  } catch (err) {
    res.writeHead(502, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ error: "连不上上游：" + String(err?.message || err) }));
    console.error(`[gateway] ${req.method} ${req.url} → 上游连不上：${err?.message || err}`);
    return;
  }

  const resp_headers = {};
  // 🔴 `content-length` 必须跟 `content-encoding` 一起丢掉（2026-08-20 修，网关第一套测试逮到的）。
  //    上面第 119 行 delete 了 accept-encoding，本意是「别让上游压缩」——
  //    可 Node 自带的 fetch **自己又补了一个** `accept-encoding: gzip, deflate`，
  //    于是上游照样 gzip。fetch 把身子解压了，而 `content-length` 还是**压缩后**的数。
  //    只跳过 content-encoding 的话，客户端被告知「一共 N 字节」（压缩后的），
  //    实际身子是解压后的更长的那份 —— **按 N 一刀切断，拿到半截 JSON**。
  //    ⚠️ 为什么活到今天没被发现：聊天基本都 stream:true，流式回应是 chunked、
  //       没有 content-length，整条路绕开了。**非流式的调用才中招**
  //       （补全、embedding、SDK 的同步调用）—— 又是一个「平时看不出来」的失败。
  //    丢掉之后由 Node 自己按实际身子算长度 / 走 chunked，两边就对上了。
  resp.headers.forEach((v, k) => {
    if (k !== "content-encoding" && k !== "content-length") resp_headers[k] = v;
  });
  res.writeHead(resp.status, resp_headers);
  if (resp.body) Readable.fromWeb(resp.body).pipe(res);
  else res.end();

  console.log(`[gateway] ${req.method} ${req.url} → ${resp.status}  ${Date.now() - start}ms  ${notes.join(" · ")}`);
});

server.listen(port, () => {
  console.log(`[gateway] 起来了 http://127.0.0.1:${port}`);
  console.log(`[gateway] 上游        ${upstream}`);
  console.log(`[gateway] Loci        ${poke._internal.httpBase(LOCI)}`);
  console.log(`[gateway] 相关度最低分 ${min_score}  ·  闲时阈值 ${idle_threshold_minutes} 分钟`);
  // 🔴 2026-08-20 补：这一行以前**偏偏没印**，而那个「超时 5 秒 → 从上线起一次没工作过」
  //    的 bug 要是当初印在启动第一屏，**第一天就能看见**。
  //    📌 判据：能让人一眼看出「配歪了」的数，就该印在启动的第一屏。
  console.log(`[gateway] Loci 超时      ${process.env.RELEVANCE_TIMEOUT_MS || "（没设，用默认）"}`);
  console.log(`[gateway] 它在不在工作    GET http://127.0.0.1:${port}/health`);
  console.log(`[gateway] 把客户端的 base_url 指到 http://127.0.0.1:${port}/v1`);
});
