// ============================================================
// gateway/server.js —— Loci 的 mini gateway（2026-08-19）
//
// **它只干一件事**：你的客户端把聊天请求发给它，它转发给真正的模型，
// 转发之前顺手问一次 Loci「现在有没有该让 AI 知道的东西」——
// 有就在消息里插一行系统提示，没有就一个字不动。
//
// 🔴 三条边界（跟模块那份同源，别在改的时候丢掉）：
//   · **失败不挡聊天**。问 Loci 超时、Loci 没起、返回不是 JSON —— 全部照常转发，
//     只在日志里记一行。宁可这次没插上，也不能让人的对话卡住。
//   · **只读多、写极少**。它调 Loci 两个普通 REST 口：GET /api/loci/poke（纯读）、
//     POST /api/loci/dream/wake（降级信号，幂等）。⛔ 不碰 MCP 工具面。
//   · **不改内容，只加一行**。插进去的是一条 system 消息，说的是「有几条相关记忆」
//     这种**数量**，不是记忆正文 —— 要不要去看，判断权留给 AI 自己。
//
// 零依赖：只用 Node 自带的 http / fetch（Node 18+）。
//
// 跑：
//     LOCI_UPSTREAM=https://api.deepseek.com/v1 node gateway/server.js
// 然后把客户端的 base_url 指到 http://127.0.0.1:3100/v1 就行，
// API key 照常由客户端带（这儿只是原样转发，**不存也不看**）。
// ============================================================

const http = require("http");
const { Readable } = require("stream");
const 桥 = require("./戳戳送达.js");

const 端口 = Number(process.env.PORT || 3100);
// 真正的模型在哪。必须是 OpenAI 兼容的地址（末尾带不带 /v1 都认）。
const 上游 = (process.env.LOCI_UPSTREAM || "").replace(/\/+$/, "");
// Loci 在哪（跟模块用同一个环境变量，一处配置两边都对）
const LOCI = process.env.LOCI_MCP || 桥.默认地址;
const 闲时阈值分钟 = Number(process.env.POKE_IDLE_MINUTES || 桥.默认闲时阈值分钟);

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

/** 这个请求是不是「一次聊天」。只有聊天才值得问 Loci。 */
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

  let 注入 = null;
  if (是聊天(req, 体)) {
    try {
      // 🔴 就是这一下。贴一次() 会**就地**改 体.messages（插一条 system），
      //    也可能什么都不做（不够闲 / Loci 那边没东西）。
      注入 = await 桥.贴一次({
        messages: 体.messages,
        requestId: req.headers["x-request-id"] || String(起),
        地址: LOCI,
        闲时阈值分钟,
      });
    } catch (错) {
      // 失败不挡聊天 —— 这是这个网关最重要的一条性质
      注入 = { error: String(错?.message || 错) };
    }
  }

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

  // 日志一行。插没插、为什么没插，看这一行就够了。
  const 说 = 注入
    ? (注入.error ? `问 Loci 失败：${注入.error}`
      : 注入.patchInjected ? `插了一行（梦=${注入.hasDream} 发呆=${注入.musePending}）`
      : 注入.calledLoci ? "问过了，没东西可插"
      : `没问（不够闲，闲了 ${注入.idleMinutes ?? "?"} 分钟 < ${闲时阈值分钟}）`)
    : "不是聊天，直接转发";
  console.log(`[gateway] ${req.method} ${req.url} → ${回.status}  ${Date.now() - 起}ms  ${说}`);
});

服务.listen(端口, () => {
  console.log(`[gateway] 起来了 http://127.0.0.1:${端口}`);
  console.log(`[gateway] 上游      ${上游}`);
  console.log(`[gateway] Loci      ${桥._internal.httpBase(LOCI)}`);
  console.log(`[gateway] 闲时阈值   ${闲时阈值分钟} 分钟（她这么久没说话，下一句才问 Loci）`);
  console.log(`[gateway] 把客户端的 base_url 指到 http://127.0.0.1:${端口}/v1`);
});
