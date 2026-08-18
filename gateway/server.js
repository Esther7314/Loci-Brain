// ============================================================
// gateway/server.js —— Loci 的 mini gateway（2026-08-19）
//
// **它干的事**：你的客户端把聊天请求发给它，它转发给真正的模型，
// 转发之前拍两下 Loci，把该让 AI 知道的东西加进这一轮的消息里。
//
// 两下，位置不一样，别搞混：
//   · 戳戳送达（戳戳送达.js）   梦 / 发呆，贴在稳定前缀区
//   · 相关记忆提醒（自动贴.js） **贴真尾巴** —— 最新 user 之后、整个 messages 的最末
//
// 🔴 提醒为什么要最后贴：位置得「离模型开口最近」。
//    所以 算相关记忆提醒() 自己不碰 messages，只把 patch 算出来还给你，
//    等别的都插完、请求体组好了，最后一步才 贴到真尾巴()。顺序反了位置就错了。
//
// ⛔ **这个外壳不替 AI 调 breath()。** 自动贴.js 里还有一个「开窗第一轮把 breath
//    整段贴进 system」的函数（`贴一次`），它是她自己那套网关的做法，这儿**故意不接**：
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
const 自动贴 = require("./自动贴.js");
const 戳戳 = require("./戳戳送达.js");

const 端口 = Number(process.env.PORT || 3100);
const 上游 = (process.env.LOCI_UPSTREAM || "").replace(/\/+$/, "");
const LOCI = process.env.LOCI_MCP || 戳戳.默认地址;
const 闲时阈值分钟 = Number(process.env.POKE_IDLE_MINUTES || 戳戳.默认闲时阈值分钟);
const 最低分 = Number(process.env.RELEVANCE_MIN_SCORE || 自动贴.默认最低分);
const 数据根 = process.env.LOCI_GATEWAY_DATA || path.join(__dirname, "data");
const 日志档 = path.join(数据根, "logs", "memory-actions.jsonl");

if (!上游) {
  console.error("没配 LOCI_UPSTREAM —— 我不知道该把请求转给谁。");
  console.error("例：LOCI_UPSTREAM=https://api.deepseek.com/v1 node gateway/server.js");
  process.exit(1);
}

function 读body(req) {
  return new Promise((好, 坏) => {
    const 块 = [];
    req.on("data", (c) => 块.push(c));
    req.on("end", () => 好(Buffer.concat(块)));
    req.on("error", 坏);
  });
}

function 是聊天(req, 体) {
  return req.method === "POST"
    && /\/chat\/completions$/.test(req.url.split("?")[0])
    && 体 && Array.isArray(体.messages);
}

const 服务 = http.createServer(async (req, res) => {
  const 起 = Date.now();
  const 原始 = await 读body(req).catch(() => Buffer.alloc(0));
  let 体 = null;
  try { 体 = 原始.length ? JSON.parse(原始.toString("utf8")) : null; } catch { 体 = null; }

  const 说 = [];
  if (是聊天(req, 体)) {
    const 公共 = {
      messages: 体.messages,
      requestId: String(req.headers["x-request-id"] || 起),
      logPath: 日志档,
      地址: LOCI,
    };

    // ---- D：戳戳送达。梦 / 发呆，贴在跟 A 同一处前缀。 ----
    try {
      const d = await 戳戳.贴一次({
        ...公共,
        statePath: path.join(数据根, "state", "poke-window.json"),
        闲时阈值分钟,
      });
      说.push(d.patchInjected ? `戳戳(梦=${d.hasDream} 发呆=${d.musePending})`
        : d.calledLoci ? "戳戳无" : "戳戳没问(不够闲)");
    } catch (错) { 说.push("戳戳炸:" + (错?.message || 错)); }

    // ---- B：相关记忆提醒。**最后一步，贴真尾巴** ----
    // 触发才跑（强档=关键词命中，弱档=本地判据）。没触发一次 recall 都不调。
    try {
      const b = await 自动贴.算相关记忆提醒({ ...公共, 最低分 });
      if (b && b.patch) {
        自动贴.贴到真尾巴(体.messages, b.patch);
        说.push("提醒(贴了真尾巴)");
      } else 说.push("提醒无");
    } catch (错) { 说.push("提醒炸:" + (错?.message || 错)); }
  } else 说.push("不是聊天，直接转发");

  const 转发体 = 体 ? Buffer.from(JSON.stringify(体)) : 原始;
  const 头 = { ...req.headers };
  delete 头.host; delete 头["content-length"]; delete 头["accept-encoding"];

  const 目标 = 上游.replace(/\/v1$/, "") + req.url;
  let 回;
  try {
    回 = await fetch(目标, {
      method: req.method,
      headers: 头,
      body: ["GET", "HEAD"].includes(req.method) ? undefined : 转发体,
    });
  } catch (错) {
    res.writeHead(502, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ error: "连不上上游：" + String(错?.message || 错) }));
    console.error(`[gateway] ${req.method} ${req.url} → 上游连不上：${错?.message || 错}`);
    return;
  }

  const 回头 = {};
  回.headers.forEach((v, k) => { if (k !== "content-encoding") 回头[k] = v; });
  res.writeHead(回.status, 回头);
  if (回.body) Readable.fromWeb(回.body).pipe(res);
  else res.end();

  console.log(`[gateway] ${req.method} ${req.url} → ${回.status}  ${Date.now() - 起}ms  ${说.join(" · ")}`);
});

服务.listen(端口, () => {
  console.log(`[gateway] 起来了 http://127.0.0.1:${端口}`);
  console.log(`[gateway] 上游        ${上游}`);
  console.log(`[gateway] Loci        ${戳戳._internal.httpBase(LOCI)}`);
  console.log(`[gateway] 相关度最低分 ${最低分}  ·  闲时阈值 ${闲时阈值分钟} 分钟`);
  console.log(`[gateway] 把客户端的 base_url 指到 http://127.0.0.1:${端口}/v1`);
});
