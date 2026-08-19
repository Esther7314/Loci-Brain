// ============================================================
// gateway/自动贴.js —— 从她自己的网关里整段抽出来的（2026-08-19）
//
// 原件：lento-home/src/loci-bridge/自动贴.js。跟 戳戳送达.js 同一个模块家族、
// 同一条边界（**零 import 宿主项目**，只用 fs/path/全局 fetch），头上就写着
// 「这个文件将来要整段跟 Loci 一起开源出去」。抽出来**一行逻辑没改**，
// 只动了路径落点这一处。
//
// 一个文件两件事，gateway 每轮都要拍这两下：
//   A · 自动贴       开窗第一轮调一次 breath()，整段贴进 system（贴前缀区）
//   B · 相关记忆提醒  强档=关键词命中 / 弱档=本地判据；触发了才调一次 recall，
//                    **只报有几条，不报正文**（贴真尾巴 —— 位置跟 A 不一样）
//
// 🔴 B 为什么必须贴真尾巴：她要的位置是「最新 user 之后、整个 messages 的最末」——
//    离模型开口最近。所以 算相关记忆提醒() **自己不碰 messages**，
//    它只把 patch 算出来还给你，你在组完请求体之后用 贴到真尾巴() 推进去。
// ============================================================
//
// 开工单 D:\lento\交接\2-记忆系统\开工单-Loci二改-2026-08-12.md 第 4 节（4.1/4.2/4.3）。
//
// 她 8-17 深夜定死的两条边界，这个文件就是那两条边界画出来的形状：
//   ① 「自动贴单独成一个模块文件，不跟 gateway 现有文件混写」
//      —— gateway（src/gateway/server.js）只留一行接线，见那边的 diff。
//   ② 「联动的 gateway 代码不和 Home 的代码写在一起——独立成自己的模块……
//      零 import lento-home」（主单 7️⃣ 边界，点名「施工第 7 步照办」）
//      —— 这个文件不 require 任何 server/ 或 src/chat/ 下的东西。
//      跟 Home 那边 server/loci信.js 长得像（同样的 MCP 握手/调用手势）是**故意的
//      重复**，不是抽共用：这个文件将来要整段跟 Loci 一起开源出去，
//      Home 的东西不能夹在里面。
//
// Loci 只被 HTTP 调（streamable-http MCP，`http://127.0.0.1:18002/mcp`，免 token
// 家规），这个文件里一行 Loci 的代码都没有、也不碰。
//
// 两件事，一次调用里做完（gateway 每轮都要拍这两下）：
//   A · 自动贴 —— 开窗第一轮 HTTP 调一次 breath()，整段（睁眼六样一样不挑不裁）
//     贴进 system/上下文；同一窗口后续轮次不再重新调用 Loci，直接重贴缓存的那份
//     （消息数组本身不会替我们记住上一轮贴过什么 —— 3010 每轮送来的 messages
//     是从她的会话存档重放的，不含 gateway 这一层加过的补丁）。
//   B · 相关记忆提醒（4.1/4.2/4.3，施工7b 2026-08-18 从观察模式升级成真提醒）——
//     **触发才跑**：强档=关键词命中（沿用现有词表），弱档=本地判据（不是真分词
//     / 向量，见下面 弱档触发 的注释）过滤掉应声词/问候语之后还剩点实质内容；
//     两档都没中的轮次，这一轮**一次 recall 都不调**。触发了才拿她这句话当
//     query 问 Loci 的 recall，渲染分数（0~100，跟 Loci `RELEVANCE_FLOOR=35`
//     同一把尺）≥ `RELEVANCE_MIN_SCORE`（env，默认 50）的才计数；event/mind
//     分开数（recall 渲染里带 🧠 牌的算 mind）。有命中就算出一条 system 短行
//     「〔记忆提醒〕和这句有关：事件 N 条 · 认知 M 条」——**只报数量，一个字的
//     记忆正文都不许出现**；0 条什么都没有。每轮现算，不写 state、不缓存、
//     不累积（gateway 每请求重建 messages，天然不会把上一轮的提醒行带过来）。
//     🔴 施工7b 她追加一刀改了插入点：**贴在整个 messages 的真尾巴**（最新
//     user 之后），不是 A/C 那种「插到最新 user 之前」——离模型开口最近、
//     命中率最高。但**这个模块自己不做插入**：算相关记忆提醒 只把算好的
//     patch 放进返回值（记录.patch），真正 push 到 outgoingBody.messages
//     真尾巴的动作在 server.js 里、组完 outgoingBody 之后做——原因是 server.js
//     内部那条 messages 送上游前要过三关校验/重建（tail 重建 / 硬 400 校验 /
//     `moveSystemPatchesBeforeLatestUser`），全都假定「最新 user 之后只能是
//     合法 tool 续接」，直接插进去要么被吞要么整个请求 400。详见
//     算相关记忆提醒 函数文档注释里的 ①②③。
//
// 失败（Loci 没起/超时）两件事都不挡聊天：该失败的那一半安安静静地什么都不做，
// 调用方（gateway）该转发的话照转发。
// ============================================================

const fs = require("fs");
const path = require("path");

// 2026-08-19 抽出来时只改了这一处路径（跟 戳戳送达.js 同一个改法）。
const 数据根 = process.env.LOCI_GATEWAY_DATA || path.join(__dirname, "data");

// LOCI_MCP：跟 server/loci信.js 用的是同一个环境变量名，验收注入假 Loci 走它。
const 默认地址 = process.env.LOCI_MCP || "http://127.0.0.1:18002/mcp";
const 默认状态档 = path.join(数据根, "state", "auto-breath-window.json");
const 默认日志档 = path.join(数据根, "logs", "memory-actions.jsonl");

// 🔴 诊断代码（src/gateway/server.js 的 upstreamDebugRecord、
//    src/gateway/messages.js 的 isVolatile）按这个字面量认「这是贴的记忆」——
//    换了字符串，那两处的诊断会静默失明。一个字不能改。
const MARKER = "[Loci memory context]";

// 强档 = 这句话里出现了「明说要翻旧账」的词。
// 🔴 2026-08-19 挪出来可配：原来这份中文表是写死的，而弱档默认关着 ——
//    合起来的后果是**一个不说中文的人装上之后，这个功能一次都不会触发，
//    而且他不会收到任何提示**。跟「超时 5 秒所以从上线起就没工作过」是同一种失败：
//    悄悄地什么都不做。
// 怎么改（三选一，从近到远）：
//    RELEVANCE_STRONG_WORDS="remember,last time,earlier"   逗号分隔，覆盖整张表
//    gateway/强档词.json                                    一个 JSON 数组，同上
//    什么都不设 → 用底下这份中文默认
const 中文强档词 = [
  "我记得", "记得吗", "还记得", "上次", "之前", "以前", "那时候", "那天", "那次", "记不记得",
];

function 读强档词() {
  const 从env = String(process.env.RELEVANCE_STRONG_WORDS || "").trim();
  if (从env) {
    const 表 = 从env.split(",").map(w => w.trim()).filter(Boolean);
    if (表.length) return 表;
  }
  try {
    const 档 = path.join(__dirname, "强档词.json");
    if (fs.existsSync(档)) {
      const 表 = JSON.parse(fs.readFileSync(档, "utf8"));
      if (Array.isArray(表) && 表.length) return 表.map(String);
    }
  } catch { /* 配置坏了不许让转发挂掉：退回默认表 */ }
  return 中文强档词;
}

const 强档关键词 = 读强档词();

// 分数线：跟 Loci recall 渲染的 0~100 尺度直接比，不做 0~1 换算了（施工7 那版
// 换算成 0~1 纯粹是历史包袱）。env `RELEVANCE_MIN_SCORE` 覆盖——她原话「这个
// 分数好改」，所以必须是一处 env 就能调，不许散在别处。
const 默认最低分 = 50;

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

// ——— MCP streamable-http 最小客户端（跟 server/loci信.js 同一套手势，独立一份）———

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
    try {
      回 = await fetch(地址, { method: "POST", headers: 头(带会话), body: JSON.stringify(体), signal: 控.signal });
    } catch (错) {
      clearTimeout(闹钟);
      throw new Error(`连不上 Loci（${地址}）：${错?.message || 错}`);
    }
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
    } finally {
      clearTimeout(闹钟);
      控.abort();
    }
    throw new Error("Loci 没给回应");
  }

  let 握手中 = null;
  function 握一次() {
    if (!握手中) 握手中 = 握手().finally(() => { 握手中 = null; });
    return 握手中;
  }
  async function 握手() {
    会话 = null;
    await 喊一声({
      jsonrpc: "2.0", id: 1, method: "initialize",
      params: { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "lento-gateway", version: "1" } },
    }, { 带会话: false });
    if (!会话) throw new Error("Loci 没给 session id");
    await 喊一声({ jsonrpc: "2.0", method: "notifications/initialized" });
  }

  async function 调(工具, 参数 = {}) {
    if (!会话) await 握一次();
    const 发 = () => 喊一声({ jsonrpc: "2.0", id: Date.now() % 100000, method: "tools/call", params: { name: 工具, arguments: 参数 } });
    let 回 = await 发().catch(错 => ({ __炸了: 错 }));
    if (回?.__炸了 || 回?.error) {
      await 握一次();
      回 = await 发().catch(错 => ({ __炸了: 错 }));
    }
    if (回?.__炸了) throw 回.__炸了;
    if (回?.error) throw new Error(回.error.message || JSON.stringify(回.error));
    return (回?.result?.content || []).map(块 => 块.text || "").join("\n");
  }

  return { 调 };
}

// ——— A · 自动贴 ———

function 插到最新user之前(messages, patch) {
  let 插入点 = messages.length;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i]?.role === "user") { 插入点 = i; break; }
  }
  // 跟系统消息挤在一起放最前面（旧缓存 insertSystemPatch 的规矩：紧跟在已有的
  // system 消息之后），真正的顺序由 server.js 现成的
  // moveSystemPatchesBeforeLatestUser 再理一遍，这儿只要不插到最新 user 后面。
  let 插到 = 0;
  while (插到 < messages.length && messages[插到]?.role === "system" && 插到 < 插入点) 插到 += 1;
  messages.splice(插到, 0, patch);
}

function 建贴文(breathText, generatedAt) {
  return [
    MARKER,
    `本轮 breath 于 ${generatedAt}（gateway 主动 HTTP 调，非工具调用）生成，一样不挑不裁。`,
    "",
    breathText,
  ].join("\n");
}

/**
 * A：开窗第一轮调一次 breath()，整段贴进 messages；同窗口内后续轮次直接重贴缓存。
 *
 * @param messages    这一轮要发给上游的消息数组（**原地修改**，跟旧的
 *                    applyStartupBreathMemory 同一个约定，调用方不用接回赋值）
 * @param newWindow   这是不是新窗口第一轮 —— 由 gateway 自己已经算好的信号传进来
 *                    （clearNextArmed || automaticNewWindow），这个模块不重新判断
 *                    「什么算一个窗口」，那条判断只该有一处
 * @param statePath   缓存当前窗口 breath 文本的地方（进程重启也不用重新调 Loci）
 * @param logPath     跟旧缓存共用同一个 memory-actions.jsonl，不新开一份
 * @param 地址/超时毫秒 只给验收用来指到假 Loci；平时用默认值
 */
async function 贴一次({
  messages,
  requestId,
  now = new Date(),
  newWindow = false,
  statePath = 默认状态档,
  logPath = 默认日志档,
  地址 = 默认地址,
  超时毫秒 = 10000,
} = {}) {
  // cacheHit/cacheWritten 两个字段是为了兼容下游诊断（server.js 的 breathMeta /
  // pruneCompletedStartupBreathToolChains）读的老字段名，语义搬过来对齐：
  // cacheHit=这轮没重新调 Loci、贴的是缓存里的旧文本；cacheWritten=这轮真调了且成功。
  const 结果 = { patchInjected: false, calledLoci: false, cacheHit: false, cacheWritten: false, windowId: null, breathChars: 0, error: null };
  const 缓存 = 读JSON(statePath, {});
  let breathText = 缓存.breathText || "";
  let windowId = 缓存.windowId || null;

  if (newWindow || !breathText) {
    结果.calledLoci = true;
    try {
      const 客户端 = 造客户端({ 地址, 超时毫秒 });
      const 文 = await 客户端.调("breath", {});
      windowId = `window-${now.toISOString()}`;
      breathText = String(文 || "");
      写JSON(statePath, { windowId, breathText, fetchedAt: now.toISOString() });
      记一行(logPath, { time: now.toISOString(), request_id: requestId, actor: "gateway/auto_paste", action: "breath_fetch", status: "ok", window_id: windowId, result_chars: breathText.length });
      结果.cacheWritten = true;
    } catch (错) {
      结果.error = String(错?.message || 错);
      记一行(logPath, { time: now.toISOString(), request_id: requestId, actor: "gateway/auto_paste", action: "breath_fetch", status: "error", error: 结果.error, fallback_to_stale_cache: Boolean(breathText) });
      // 🔴 失败不挡聊天：有旧缓存就照旧贴旧的（总比什么都没有强），没有就这轮不贴，
      //    绝不能因为 Loci 没起/超时就把她的这句话拦下来。
    }
  }

  if (breathText) {
    const patch = { role: "system", content: 建贴文(breathText, 缓存.fetchedAt || now.toISOString()) };
    插到最新user之前(Array.isArray(messages) ? messages : [], patch);
    结果.patchInjected = true;
    结果.breathChars = breathText.length;
    // 这轮没重新调 Loci、纯粹重贴缓存里的旧文本 —— 语义上就是「命中缓存」。
    结果.cacheHit = !结果.cacheWritten;
  }
  结果.windowId = windowId;
  return 结果;
}

// ——— B · 相关记忆提醒（4.1/4.2/4.3，触发才跑，只报数量真注入）———

function latestUserText(messages) {
  for (let i = (messages || []).length - 1; i >= 0; i -= 1) {
    const m = messages[i];
    if (m?.role !== "user") continue;
    if (typeof m.content === "string") return m.content;
    if (Array.isArray(m.content)) return m.content.map(part => (typeof part?.text === "string" ? part.text : "")).join("");
    return "";
  }
  return "";
}

function 强档命中(文本) {
  // 小写比对：英文词表不能因为句首大写（"Remember when…"）就整条漏掉。
  // 中文没有大小写，toLowerCase 对它是恒等变换，所以这一行对我们零影响。
  const 低 = String(文本).toLowerCase();
  return 强档关键词.filter(词 => 低.includes(String(词).toLowerCase()));
}

// 弱档触发的完整短句停用表：应声词/问候语，一字不差命中就不算「有实质内容」。
// ⚠️ **这不是真正的「主体名词抽取」**（开工单 4.1 写的是「主体名词 + 本地向量」）——
// gateway 这层没有分词器也没有本地向量模型，中文又没有空格，简单正则切不出词。
// 这儿退而求其次：**过滤掉明显没主体的应声话**（她打个「嗯」「在吗」不该去问
// Loci），剩下的但凡有点实质内容的都放行去调 recall —— 真正的语义判断留给
// Loci 自己的 recall（本地向量、拿真分数）去做，这一层只管「值不值得问一句」。
// 这是已知的简化，交活报告里点名了，将来想做真的主体抽取可以在这个函数里换。
const 弱档停用句 = new Set([
  "在吗", "在么", "你好", "嗨", "hi", "hello", "早", "早安", "晚安",
  "谢谢", "谢谢你", "谢啦", "多谢",
  "好的", "好嘞", "好呀", "好", "行", "行吧", "ok", "okay",
  "嗯", "嗯嗯", "哦", "哦哦", "噢", "知道了", "收到",
  "没事", "没什么", "无事", "没有", "算了",
]);

function 弱档触发(文本) {
  const 净 = String(文本 || "").trim();
  if (!净) return false;
  // 去掉尾部的标点噪声（"在吗？" 也该算应声话），再对完整句做停用表匹配。
  const 去尾标点 = 净.replace(/[，。！？、,.!?~～…\s]+$/g, "");
  if (弱档停用句.has(去尾标点)) return false;
  // 有效字数：中文按字数、英文/数字按连续字母数字串的长度算——太短的不构成主体
  // （单字应声、两三个字的口头禅），三个字起才当「这句话像是在说点什么」。
  const 汉字数 = (去尾标点.match(/[一-鿿]/g) || []).length;
  const 词串长 = (去尾标点.match(/[A-Za-z]{2,}|[0-9]+/g) || []).reduce((n, w) => n + w.length, 0);
  return 汉字数 + 词串长 >= 3;
}

/**
 * 从 recall(query=…) 的**渲染文本**里挑出带分数的行，顺带认出哪条是 mind。
 *
 * ⚠️ **这是在读 Loci 的展示格式，不是结构化 API** —— Loci 只暴露了 MCP 的
 * `recall` 工具（返回的是给人看的一段文字），没有单独的「给我 JSON 分数」的口子，
 * 而这单不许碰 Loci 代码去加一个。格式抄的是
 * `ombre-brain-v2/buckets/_app/src/tools/recall/core.py` 里 `_render_search` 的
 * 默认视图（当前是「时间+分数默认，query 单独也一样，🧠 牌=mind、房间码撤了」）：
 *   `{score:5.1f}  [🧠]{摘要}  ({短id})  {MM-DD}`
 * 这一行的分数是 **0~100** 的尺度（Loci 那边 `RELEVANCE_FLOOR` 默认 35），
 * 跟这个模块的 `RELEVANCE_MIN_SCORE` 是同一把尺，不用换算。
 * **这条耦合是这份实现的已知坑**：Loci 改了这行的排版，这儿就抓不到分数/牌了，
 * 抓不到就当没有命中处理（不炸，只是这轮少提醒一句），交活报告里点名过。
 */
function 解析分数行(文本) {
  const 条目 = [];
  for (const 行 of String(文本 || "").split(/\r?\n/)) {
    const m = /^\s*([0-9]+(?:\.[0-9]+)?)\s{2,}.*?\(([0-9a-zA-Z]{4,})\)/.exec(行);
    if (!m) continue;
    条目.push({ id: m[2], score100: Number(m[1]), isMind: 行.includes("🧠") });
  }
  return 条目;
}

// 注入文案：**只报数量，不报内容**——开工单 4.2 的字面要求，也是最容易踩的坑
// （随手把摘要也带上就是「系统替我想起」，正是她 8-16 改掉的那半）。
// 正文以「〔记忆提醒〕」开头是**字面约定**：src/gateway/messages.js 的
// `moveSystemPatchesBeforeLatestUser` 认这个前缀，给它开了豁免（见那边的
// isReminderPinnedAfterLatestUser）——这个豁免目前是防御性的：现在的实现里
// 提醒行压根不经过那条内部 messages 流水线（见下面 算相关记忆提醒 的文档），
// 所以这个函数眼下碰不到它；万一以后有代码改道把它塞回那条内部流水线，这个
// 豁免能接住，不会悄悄被搬回 user 之前。改这句开头前缀要跟那边一起改。
function 建提醒行(event数, mind数) {
  return `〔记忆提醒〕和这句有关：事件 ${event数} 条 · 认知 ${mind数} 条`;
}

// 贴在整个 messages 的**真尾巴**（最新 user 之后）——她 2026-08-18 追加的一刀：
// 离模型开口最近、命中率最高，类似 hook 往 user prompt 后面追加上下文的姿势。
// 🔴 **这个函数不在这个模块里调**：算相关记忆提醒 只算、只把 patch 放进返回值
// 的 记录.patch，真正 push 的动作交给 server.js，在它自己拼完 outgoingBody
// （真正发给上游那份）之后再做。原因写在 算相关记忆提醒 的文档注释里——
// server.js 内部那条「messages」在送去上游之前要过三关校验/重建，全都假定
// 「最新 user 之后只能是合法的 tool 续接」，塞一条 system 进去要么被吞、要么
// 直接 400。真正安全的位置是**校验通过、组好 outgoingBody 之后**，不是内部
// 那条 messages 流水线的任何一站。留这个 helper 在这儿是给 server.js（还有测试）
// 复用同一个"push 到真尾巴"手势，不是自己在用。
function 贴到真尾巴(messages, patch) {
  if (Array.isArray(messages)) messages.push(patch);
}

/**
 * B：**触发才跑**。强档=关键词命中，弱档=本地判据（见 弱档触发 注释）过滤掉
 * 应声话之后还有实质内容——两档都没中，这一轮一次 recall 都不调，日志记一行
 * 「没触发」，函数直接返回。
 *
 * 触发了才调 Loci 的 recall(query=她这句话)，渲染分数 ≥ `最低分`（默认 50，
 * env `RELEVANCE_MIN_SCORE` 覆盖）的才计数，event/mind 分开数。有命中就把
 * `记录.patch = { role: "system", content: ... }` 放进返回值——**这个函数
 * 自己不碰 messages**，0 条 记录.patch 就是 undefined。
 *
 * 🔴 为什么不像 A/C 那样自己插：她要的位置是「最新 user 之后，整个 messages
 * 真尾巴」，但 server.js 内部那条 `messages`（给 rolling summary / 工具链
 * 校验用的那份）在真正发出去之前要经过三关，全都假定「最新 user 之后只能是
 * 合法的 tool 续接」：
 *   ① `restoreLatestRequestTail` 会按客户端原始请求重建最新 user 之后的尾巴，
 *      塞进去的东西不是客户端原文，会被**静默吞掉**（试过，真吞）；
 *   ② `validateMessageSequence`（server.js 收尾那次）看到最新 user 之后有条
 *      非 assistant-tool_calls 的消息，直接 **400**（`non_tool_message_after_
 *      latest_user`）——这个是硬拒绝，不是静默；
 *   ③ `moveSystemPatchesBeforeLatestUser` 会把它搬回 user 之前（这条她点名
 *      要查，messages.js 已经给「〔记忆提醒〕」开头的消息开了豁免）。
 * 光豁免③不够——①②不豁免的话，提醒行要么消失要么整个请求打不通。真正干净
 * 的做法是压根不让它进那条内部 messages：算相关记忆提醒 只把 patch 算出来，
 * server.js 在 ①②③ 全部跑完、组好 `outgoingBody`（就是真正发给 DeepSeek/GLM
 * 的那份 wire payload）之后，直接 push 到 `outgoingBody.messages` 的真尾巴——
 * 那份不再被这三道内部校验碰第二次，天然安全。
 *
 * 每轮现算现贴：不读不写任何 state 文件，命中与否只活在这一次调用的返回值里
 * ——下一轮请求带来的是全新的 messages 数组（gateway 每请求重建），这个函数
 * 自己也没有任何跨调用的内存状态，天然不会累积。
 * 失败（Loci 没起/超时/解析不出分数行）不挡聊天：catch 住、日志记一行、这轮
 * 不注入，函数正常返回。
 */
async function 算相关记忆提醒({
  messages,
  requestId,
  now = new Date(),
  logPath = 默认日志档,
  地址 = 默认地址,
  // 🔴 2026-08-19 从 5000 提到 12000。实测：956 条的库跑一次带 query 的 recall
  //    要 5~7 秒（向量 + BM25 一起跑），而超时卡在 5 秒 ——
  //    **于是这个功能从上线到今天一次都没成功过**：每次都 abort，
  //    命中数恒为 0、patch 恒为 null，而且失败只进日志、聊天照常，
  //    所以没有任何地方看得出来它没在工作。
  //    库越大越慢，这个数该跟着库走；env RELEVANCE_TIMEOUT_MS 可调。
  超时毫秒 = Number(process.env.RELEVANCE_TIMEOUT_MS || 12000),
  最低分 = Number(process.env.RELEVANCE_MIN_SCORE || 默认最低分),
} = {}) {
  const 她的话 = latestUserText(messages).trim();
  const 强命中 = 她的话 ? 强档命中(她的话) : [];
  // 🔴 2026-08-19 弱档改成**默认关**（env RELEVANCE_WEAK=1 打开）。
  //    不是因为它不准，是因为它太宽：判据是「过滤掉应声词之后但凡有三个字
  //    以上的实质内容就放行」——日常说话几乎每句都过。而一次 recall 要 5~7 秒，
  //    等于**每一轮都卡六秒**。强档（「上次 / 还记得 / 之前」这类词）触发得少，
  //    该等的时候才等。想全都要的人自己开。
  const 开弱档 = String(process.env.RELEVANCE_WEAK || "").trim() === "1";
  const 弱命中 = 开弱档 && Boolean(她的话) && 强命中.length === 0 && 弱档触发(她的话);
  const 触发 = 强命中.length > 0 || 弱命中;

  const 记录 = {
    time: now.toISOString(),
    request_id: requestId,
    actor: "gateway/relevance_reminder",
    action: "relevance_reminder_observed",
    triggered: 触发,
    trigger_kind: 强命中.length > 0 ? "strong" : (弱命中 ? "weak" : "none"),
    strong_matched_keywords: 强命中,
    min_score: 最低分,
    recall_called: false,
    event_count: 0,
    mind_count: 0,
    injected: false,
  };

  if (!她的话 || !触发) {
    记录.skipped = !她的话 ? "no_user_text" : "not_triggered";
    记一行(logPath, 记录);
    return 记录;
  }

  记录.recall_called = true; // 触发了就算发起过调用，成不成功是另一件事（error 字段管）
  try {
    const 客户端 = 造客户端({ 地址, 超时毫秒 });
    const 文 = await 客户端.调("recall", { query: 她的话.slice(0, 120) });
    const 过线的 = 解析分数行(文).filter(条 => 条.score100 >= 最低分);
    const mind条目 = 过线的.filter(条 => 条.isMind);
    const event条目 = 过线的.filter(条 => !条.isMind);
    记录.event_count = event条目.length;
    记录.mind_count = mind条目.length;
    // 日志留 id 方便查证据链，**不留正文**——跟注入文案同一条纪律。
    记录.matched_ids = 过线的.map(条 => 条.id);

    if (过线的.length > 0) {
      // 🔴 只算，不碰 messages——真正 push 的动作交给 server.js，在 outgoingBody
      // 组完之后做。理由见本函数上方文档注释的 ①②③。
      记录.patch = { role: "system", content: 建提醒行(event条目.length, mind条目.length) };
      记录.injected = true;
    }
  } catch (错) {
    记录.error = String(错?.message || 错);
  }

  记一行(logPath, 记录);
  return 记录;
}

module.exports = {
  MARKER,
  默认地址,
  默认状态档,
  默认日志档,
  强档关键词,
  默认最低分,
  贴一次,
  算相关记忆提醒,
  // 贴到真尾巴：server.js 组完 outgoingBody 之后，真正 push 记录.patch 用的是
  // 这个（不是 _internal——它是生产代码要用的手势，不只是测试）。
  贴到真尾巴,

// ── 英文别名（2026-08-19 她提的：「你就不怕别人不好改吗」）─────────────────────
// 🔴 **只是别名，指的是同一个函数**。文件内部照旧中文——`算相关记忆提醒` 一眼知道
//    它干嘛，改成 computeRelevanceReminder 还得在脑子里翻译一次，而且改内部纯属
//    给自己制造 bug。但**对外这几个名字是别人要亲手敲的**，一个不认识汉字的人
//    连自己粘的是哪个都不知道。名字是给读的人用的，谁读就照顾谁。
  // computeReminder({ messages, requestId, 地址, 最低分 }) → { patch, ... }
  computeReminder: 算相关记忆提醒,
  // appendToTail(messages, patch) —— 组完请求体之后的最后一步
  appendToTail: 贴到真尾巴,
  paste: 贴一次,
  MARKER_LINE: MARKER,
  DEFAULT_ADDRESS: 默认地址,
  DEFAULT_MIN_SCORE: 默认最低分,
  STRONG_WORDS: 强档关键词,

  _internal: { 造客户端, latestUserText, 强档命中, 弱档触发, 解析分数行, 建贴文, 建提醒行 },
};
