// ============================================================
// gateway/近期记忆视图.js —— 从她自己的网关里整段抽出来的（2026-08-19）
//
// 🟡 **这一个是可选的**（她 8-19 定的：必选是 戳戳送达 + 相关记忆提醒，
//    这个和「压缩」是可选）。不接它，别的两样照常工作。
//
// 原件：lento-home/src/loci-bridge/近期记忆视图.js，跟另外两个模块同一条边界
// （零 import 宿主项目、只用 fs/path/全局 fetch）。抽出来一行逻辑没改，
// 只动了路径落点和标记。
// ============================================================
//
// 开工单 D:\lento\交接\2-记忆系统\开工单-Loci二改-2026-08-12.md 7.1：
//   昨天/最近做了什么，留原文、留氛围，**自动生成、不进库**——
//   「2-4 句话，保留关键事件和当时的感觉，不要流水账，不要丢掉情绪」，
//   留几句/留多少原文是配置项，gateway 现算现给，不做「压成一条进库的 gist」。
//
// 🔴 2026-08-18 统筹拍板（施工7 交活报告点名的坑之一，现在接了）：
//   · 消费点：**跟自动贴同节奏**——开窗第一轮算一次、贴在 `[Lento memory
//     context]` 后面的独立 system 段，同窗口内不重算；只给 DeepSeek/GLM 线
//     （src/gateway/server.js 的 handleChat，跟自动贴同一个边界，CC 线不碰）。
//   · 失败语义跟自动贴一致：Loci 炸了 → 这轮没有视图，记一行日志，不挡聊天；
//     有旧缓存就退回旧的。
//   · 视图**不落盘成记忆**——`贴一次` 的缓存文件只装"这个窗口现在贴的是什么"，
//     跟 Loci 的库完全无关，进程重启/换个 statePath 就等于从没存在过。
//
// 🔴 2026-08-18 当天又改了一版（她拍的）：**撤掉了 deepseek 摘要那半**。
//   理由是她的原话——「那 2-4 句要留『当时的感觉』，外包给不在场的模型写出来是
//   新闻稿」。摘要将来会由**收窗时当窗的那个模型自己写**（归另一张单，不是这单的活）。
//   → 这一版**只贴昨日原文摘录，没有摘要**。`生成摘要` 这个接口**留着**（收窗摘要
//   那单接进来时喂给这儿），但现在是**可选参数**：不传就走纯原文摘录模式，
//   不再当场炸——"摘要位空着，等收窗那单"。
//
// 跟 src/loci-bridge/自动贴.js 一样：只被 HTTP 调 Loci，零 import lento-home，
// 将来跟 Loci 一起开源（MCP 客户端那段刻意重复一份，理由写在 自动贴.js 里）。
// ============================================================

const fs = require("fs");
const path = require("path");

// 2026-08-19 抽出来时只改了这一处路径（跟另外两个模块同一个改法）。
const 数据根 = process.env.LOCI_GATEWAY_DATA || path.join(__dirname, "data");
const 默认地址 = process.env.LOCI_MCP || "http://127.0.0.1:18002/mcp";
const 默认状态档 = path.join(数据根, "state", "recent-memory-view-window.json");
const 默认日志档 = path.join(数据根, "logs", "memory-actions.jsonl");

// 🔴 诊断/验收都认这个字面量——跟 自动贴.js 的 MARKER 是姐妹标记，改了就抓不到了。
const MARKER = "[Loci recent memory view]";

function 读JSON(文件, 缺省 = {}) {
  try { return fs.existsSync(文件) ? JSON.parse(fs.readFileSync(文件, "utf8")) : 缺省; }
  catch { return 缺省; }
}
function 写JSON(文件, 值) {
  fs.mkdirSync(path.dirname(文件), { recursive: true });
  fs.writeFileSync(文件, `${JSON.stringify(值, null, 2)}\n`);
}
function 记一行(文件, 值) {
  fs.mkdirSync(path.dirname(文件), { recursive: true });
  fs.appendFileSync(文件, `${JSON.stringify(值)}\n`);
}

// 跟 自动贴.js 是故意重复的一份最小 MCP 客户端（同样的理由：这个文件要能整段
// 单独抽出去开源，不能反过来 require 同目录的兄弟文件、更不能 require Home）。
function 造客户端({ 地址 = 默认地址, 超时毫秒 = 10000 } = {}) {
  let 会话 = null;
  function 头(带会话) {
    const h = { "Content-Type": "application/json", "Accept": "application/json, text/event-stream" };
    if (带会话 && 会话) h["Mcp-Session-Id"] = 会话;
    return h;
  }
  function 挑一条(缓) {
    for (const 行 of 缓.split(/\r?\n/)) {
      if (!行.startsWith("data:")) continue;
      const 文 = 行.slice(5).trim();
      if (!文) continue;
      try { return JSON.parse(文); } catch { /* 还没收全 */ }
    }
    return null;
  }
  async function 喊一声(体, { 带会话 = true } = {}) {
    const 控 = new AbortController();
    const 闹钟 = setTimeout(() => 控.abort(), 超时毫秒);
    let 回;
    try { 回 = await fetch(地址, { method: "POST", headers: 头(带会话), body: JSON.stringify(体), signal: 控.signal }); }
    catch (错) { clearTimeout(闹钟); throw new Error(`连不上 Loci（${地址}）：${错?.message || 错}`); }
    const 新会话 = 回.headers.get("mcp-session-id");
    if (新会话) 会话 = 新会话;
    if (回.status === 202 || !回.body) { clearTimeout(闹钟); return null; }
    if (!回.ok) { clearTimeout(闹钟); throw new Error(`Loci 回了 HTTP ${回.status}`); }
    const 读 = 回.body.getReader();
    let 缓 = "";
    try {
      for (;;) {
        const { done, value } = await 读.read();
        if (done) break;
        缓 += Buffer.from(value).toString("utf8");
        const 一条 = 挑一条(缓);
        if (一条) return 一条;
      }
    } finally { clearTimeout(闹钟); 控.abort(); }
    throw new Error("Loci 没给回应");
  }
  let 握手中 = null;
  function 握一次() { if (!握手中) 握手中 = 握手().finally(() => { 握手中 = null; }); return 握手中; }
  async function 握手() {
    会话 = null;
    await 喊一声({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "lento-gateway", version: "1" } } }, { 带会话: false });
    if (!会话) throw new Error("Loci 没给 session id");
    await 喊一声({ jsonrpc: "2.0", method: "notifications/initialized" });
  }
  async function 调(工具, 参数 = {}) {
    if (!会话) await 握一次();
    const 发 = () => 喊一声({ jsonrpc: "2.0", id: Date.now() % 100000, method: "tools/call", params: { name: 工具, arguments: 参数 } });
    let 回 = await 发().catch(错 => ({ __炸了: 错 }));
    if (回?.__炸了 || 回?.error) { await 握一次(); 回 = await 发().catch(错 => ({ __炸了: 错 })); }
    if (回?.__炸了) throw 回.__炸了;
    if (回?.error) throw new Error(回.error.message || JSON.stringify(回.error));
    return (回?.result?.content || []).map(块 => 块.text || "").join("\n");
  }
  return { 调 };
}

/** 原文摘录：取 recall 渲染文本的前 N 个非空行——recall 默认视图是按时间新→旧排的，
 *  前几行就是最近的那些，跟"留一部分原文"的诉求对得上，不用另猜"哪部分"。 */
function 摘前N行(文本, N) {
  const 行 = String(文本 || "").split(/\r?\n/).filter(x => x.trim());
  return 行.slice(0, Math.max(0, N)).join("\n");
}

/**
 * 算一次「近期记忆视图」的核心：拿 recall(when=…) 的原始事件文本，
 * 切一份原文摘录。**不落盘、不缓存**——窗口级缓存是 `贴一次`（下面）的事，
 * 这个函数本身没有状态。
 *
 * @param when         recall 的时间范围，她的原话是「隔天贴昨日原文」→ 默认 "昨天"
 * @param 摘要句数      配置项，默认 4（她 8-15：「2-4 句话」）——**现在没人读它**，
 *                     `生成摘要` 不传就用不上，留着是因为收窗摘要那单接回来时要用
 * @param 保留原文行数  配置项，默认 20——"留一部分原文"要留多少行，让宿主自己定
 * @param 生成摘要      **可选**。(原文, 摘要句数) => Promise<string>。
 *                     不传 = 纯原文摘录模式（2026-08-18 她拍的当前口径：摘要要留
 *                     "当时的感觉"，外包给不在场的模型写是新闻稿，这半空着等
 *                     "收窗时当窗的模型自己写"那单接回来）。传了才会调用——
 *                     这个模块永远不内置调用任何真实 LLM，谁来调、调不调是宿主的事。
 * @throws 只有 Loci 炸了才抛（调不调 生成摘要 不影响这条）——调用方（`贴一次`）
 *         负责兜底、`贴一次` 之外的直接调用方自己接 try/catch。
 */
async function 算一次({
  when = "昨天",
  摘要句数 = 4,
  保留原文行数 = 20,
  生成摘要,
  地址 = 默认地址,
  超时毫秒 = 10000,
} = {}) {
  const 客户端 = 造客户端({ 地址, 超时毫秒 });
  const 原文 = await 客户端.调("recall", { when });
  const 原文摘录 = 摘前N行(原文, 保留原文行数);
  const 摘要 = typeof 生成摘要 === "function" ? await 生成摘要(原文, 摘要句数) : null;
  return { when, 摘要句数, 保留原文行数, 摘要, 原文摘录, 原始事件字数: 原文.length, 落盘: false };
}

function 插到最新user之前(messages, patch) {
  let 插入点 = messages.length;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i]?.role === "user") { 插入点 = i; break; }
  }
  let 插到 = 0;
  while (插到 < messages.length && messages[插到]?.role === "system" && 插到 < 插入点) 插到 += 1;
  messages.splice(插到, 0, patch);
}

function 建贴文(view) {
  // 摘要位现在多半是空的（她 8-18 拍的：这半等收窗那单）——空着就不写这一行，
  // 不写「没写出摘要」这类暗示失败的话，因为这不是失败，是当前口径本来就没有它。
  const 段 = [MARKER];
  if (view.摘要) 段.push(view.摘要.trim());
  if (view.原文摘录) {
    if (段.length > 1) 段.push("");
    段.push(`原文摘录（${view.when}）：`, view.原文摘录);
  }
  return 段.join("\n");
}

/**
 * 贴一次：**跟自动贴同一节奏**——`newWindow` 时真算一次（Loci + 生成摘要都要成功
 * 才算数），同窗口内后续轮次直接重贴缓存文本；一个字都不进库，缓存只在
 * `statePath` 这一个文件里、只管"这个窗口现在贴的是什么"。
 *
 * @param messages   这一轮要发给上游的消息数组（原地修改）
 * @param newWindow  跟 自动贴.js 的 `贴一次` 传一样的信号，判断"什么是新窗口"
 *                   不在这个模块里重新发明
 * @param statePath  跟自动贴分开一个文件（`recent-memory-view-window.json`），
 *                   两边各管各的，谁的窗口判断不巧对不上也不会互相拖累
 */
async function 贴一次({
  messages,
  requestId,
  now = new Date(),
  newWindow = false,
  statePath = 默认状态档,
  logPath = 默认日志档,
  when = "昨天",
  摘要句数 = 4,
  保留原文行数 = 20,
  生成摘要,
  地址 = 默认地址,
  超时毫秒 = 10000,
} = {}) {
  const 结果 = { patchInjected: false, computed: false, windowId: null, error: null };
  const 缓存 = 读JSON(statePath, {});
  let view = 缓存.view || null;
  let windowId = 缓存.windowId || null;

  if (newWindow || !view) {
    结果.computed = true;
    try {
      view = await 算一次({ when, 摘要句数, 保留原文行数, 生成摘要, 地址, 超时毫秒 });
      windowId = `window-${now.toISOString()}`;
      写JSON(statePath, { windowId, view, computedAt: now.toISOString() });
      记一行(logPath, { time: now.toISOString(), request_id: requestId, actor: "gateway/recent_memory_view", action: "recent_memory_view_computed", status: "ok", window_id: windowId, when, summary_chars: view.摘要 ? view.摘要.length : 0, raw_excerpt_chars: view.原文摘录 ? view.原文摘录.length : 0 });
    } catch (错) {
      结果.error = String(错?.message || 错);
      记一行(logPath, { time: now.toISOString(), request_id: requestId, actor: "gateway/recent_memory_view", action: "recent_memory_view_computed", status: "error", error: 结果.error, fallback_to_stale_cache: Boolean(缓存.view) });
      view = 缓存.view || null;   // 这轮算失败了：有旧缓存退回旧的，没有就这轮没有视图
    }
  }

  if (view) {
    插到最新user之前(Array.isArray(messages) ? messages : [], { role: "system", content: 建贴文(view) });
    结果.patchInjected = true;
  }
  结果.windowId = windowId;
  return 结果;
}

module.exports = { MARKER, 默认地址, 默认状态档, 默认日志档, 算一次, 贴一次, _internal: { 造客户端, 摘前N行, 建贴文 } };
