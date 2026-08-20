// ============================================================
// gateway/auto_attach.js —— 从她自己的网关里整段抽出来的（2026-08-19）
//
// 原件：lento-home/src/loci-bridge/自动贴.js。跟 poke_delivery.js 同一个模块家族、
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
//    离模型开口最近。所以 build_relevance_notice() **自己不碰 messages**，
//    它只把 patch 算出来还给你，你在组完请求体之后用 attach_at_true_tail() 推进去。
// ============================================================
//
// 这一层的设计判据（我们自己的开工单第 4 节，那份单子不在这个仓库里）。
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
//     / 向量，见下面 weak_triggered 的注释）过滤掉应声词/问候语之后还剩点实质内容；
//     两档都没中的轮次，这一轮**一次 recall 都不调**。触发了才拿她这句话当
//     query 问 Loci 的 recall，渲染分数（0~100，跟 Loci `RELEVANCE_FLOOR=35`
//     同一把尺）≥ `RELEVANCE_MIN_SCORE`（env，默认 50）的才计数；event/mind
//     分开数（recall 渲染里带 🧠 牌的算 mind）。有命中就算出一条 system 短行
//     「〔记忆提醒〕和这句有关：事件 N 条 · 认知 M 条」——**只报数量，一个字的
//     记忆正文都不许出现**；0 条什么都没有。每轮现算，不写 state、不缓存、
//     不累积（gateway 每请求重建 messages，天然不会把上一轮的提醒行带过来）。
//     🔴 施工7b 她追加一刀改了插入点：**贴在整个 messages 的真尾巴**（最新
//     user 之后），不是 A/C 那种「插到最新 user 之前」——离模型开口最近、
//     命中率最高。但**这个模块自己不做插入**：build_relevance_notice 只把算好的
//     patch 放进返回值（记录.patch），真正 push 到 outgoingBody.messages
//     真尾巴的动作在 server.js 里、组完 outgoingBody 之后做——原因是 server.js
//     内部那条 messages 送上游前要过三关校验/重建（tail 重建 / 硬 400 校验 /
//     `moveSystemPatchesBeforeLatestUser`），全都假定「最新 user 之后只能是
//     合法 tool 续接」，直接插进去要么被吞要么整个请求 400。详见
//     build_relevance_notice 函数文档注释里的 ①②③。
//
// 失败（Loci 没起/超时）两件事都不挡聊天：该失败的那一半安安静静地什么都不做，
// 调用方（gateway）该转发的话照转发。
// ============================================================

const fs = require("fs");
const path = require("path");

// 2026-08-19 抽出来时只改了这一处路径（跟 poke_delivery.js 同一个改法）。
const data_root = process.env.LOCI_GATEWAY_DATA || path.join(__dirname, "data");

// LOCI_MCP：跟 server/loci信.js 用的是同一个环境变量名，验收注入假 Loci 走它。
const DEFAULT_ADDRESS = process.env.LOCI_MCP || "http://127.0.0.1:18002/mcp";
const DEFAULT_STATE_PATH = path.join(data_root, "state", "auto-breath-window.json");
const DEFAULT_LOG_PATH = path.join(data_root, "logs", "memory-actions.jsonl");

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
const CHINESE_STRONG_WORDS = [
  "我记得", "记得吗", "还记得", "上次", "之前", "以前", "那时候", "那天", "那次", "记不记得",
];

function read_strong_words() {
  const from_env = String(process.env.RELEVANCE_STRONG_WORDS || "").trim();
  if (from_env) {
    const words = from_env.split(",").map(w => w.trim()).filter(Boolean);
    if (words.length) return words;
  }
  try {
    const config_file = path.join(__dirname, "强档词.json");
    if (fs.existsSync(config_file)) {
      const words = JSON.parse(fs.readFileSync(config_file, "utf8"));
      if (Array.isArray(words) && words.length) return words.map(String);
    }
  } catch { /* 配置坏了不许让转发挂掉：退回默认表 */ }
  return CHINESE_STRONG_WORDS;
}

const STRONG_WORDS = read_strong_words();

// 分数线：跟 Loci recall 渲染的 0~100 尺度直接比，不做 0~1 换算了（施工7 那版
// 换算成 0~1 纯粹是历史包袱）。env `RELEVANCE_MIN_SCORE` 覆盖——她原话「这个
// 分数好改」，所以必须是一处 env 就能调，不许散在别处。
const DEFAULT_MIN_SCORE = 50;

function read_json(file, fallback = {}) {
  try { return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : fallback; }
  catch { return fallback; }
}
function write_json(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`);
}
function log_line(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.appendFileSync(file, `${JSON.stringify(value)}\n`);
}

// ——— MCP streamable-http 最小客户端（跟 server/loci信.js 同一套手势，独立一份）———

function make_client({ address = DEFAULT_ADDRESS, timeout_ms = 10000 } = {}) {
  let session = null;

  function build_headers(with_session) {
    const h = { "Content-Type": "application/json", "Accept": "application/json, text/event-stream" };
    if (with_session && session) h["Mcp-Session-Id"] = session;
    return h;
  }

  function pick_frame(pending) {
    for (const line of pending.split(/\r?\n/)) {
      if (!line.startsWith("data:")) continue;
      const text = line.slice(5).trim();
      if (!text) continue;
      try { return JSON.parse(text); } catch { /* 还没收全 */ }
    }
    return null;
  }

  async function rpc_once(payload, { with_session = true } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout_ms);
    let resp;
    try {
      resp = await fetch(address, { method: "POST", headers: build_headers(with_session), body: JSON.stringify(payload), signal: controller.signal });
    } catch (err) {
      clearTimeout(timer);
      throw new Error(`连不上 Loci（${address}）：${err?.message || err}`);
    }
    const new_session = resp.headers.get("mcp-session-id");
    if (new_session) session = new_session;
    if (resp.status === 202 || !resp.body) { clearTimeout(timer); return null; }
    if (!resp.ok) { clearTimeout(timer); throw new Error(`Loci 回了 HTTP ${resp.status}`); }
    const reader = resp.body.getReader();
    let pending = "";
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        pending += Buffer.from(value).toString("utf8");
        const frame = pick_frame(pending);
        if (frame) return frame;
      }
    } finally {
      clearTimeout(timer);
      controller.abort();
    }
    throw new Error("Loci 没给回应");
  }

  let handshaking = null;
  function handshake_once() {
    if (!handshaking) handshaking = handshake().finally(() => { handshaking = null; });
    return handshaking;
  }
  async function handshake() {
    session = null;
    await rpc_once({
      jsonrpc: "2.0", id: 1, method: "initialize",
      params: { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "lento-gateway", version: "1" } },
    }, { with_session: false });
    if (!session) throw new Error("Loci 没给 session id");
    await rpc_once({ jsonrpc: "2.0", method: "notifications/initialized" });
  }

  async function call_tool(tool, args = {}) {
    if (!session) await handshake_once();
    const send = () => rpc_once({ jsonrpc: "2.0", id: Date.now() % 100000, method: "tools/call", params: { name: tool, arguments: args } });
    let resp = await send().catch(err => ({ __炸了: err }));
    if (resp?.__炸了 || resp?.error) {
      await handshake_once();
      resp = await send().catch(err => ({ __炸了: err }));
    }
    if (resp?.__炸了) throw resp.__炸了;
    if (resp?.error) throw new Error(resp.error.message || JSON.stringify(resp.error));
    return (resp?.result?.content || []).map(block => block.text || "").join("\n");
  }

  return { call_tool };
}

// ——— A · 自动贴 ———

function insert_before_latest_user(messages, patch) {
  let latest_user_index = messages.length;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i]?.role === "user") { latest_user_index = i; break; }
  }
  // 跟系统消息挤在一起放最前面（旧缓存 insertSystemPatch 的规矩：紧跟在已有的
  // system 消息之后），真正的顺序由 server.js 现成的
  // moveSystemPatchesBeforeLatestUser 再理一遍，这儿只要不插到最新 user 后面。
  let insert_at = 0;
  while (insert_at < messages.length && messages[insert_at]?.role === "system" && insert_at < latest_user_index) insert_at += 1;
  messages.splice(insert_at, 0, patch);
}

function build_patch_text(breathText, generatedAt) {
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
async function attach_once({
  messages,
  requestId,
  now = new Date(),
  newWindow = false,
  statePath = DEFAULT_STATE_PATH,
  logPath = DEFAULT_LOG_PATH,
  地址: address = DEFAULT_ADDRESS,
  超时毫秒: timeout_ms = 10000,
} = {}) {
  // cacheHit/cacheWritten 两个字段是为了兼容下游诊断（server.js 的 breathMeta /
  // pruneCompletedStartupBreathToolChains）读的老字段名，语义搬过来对齐：
  // cacheHit=这轮没重新调 Loci、贴的是缓存里的旧文本；cacheWritten=这轮真调了且成功。
  const result = { patchInjected: false, calledLoci: false, cacheHit: false, cacheWritten: false, windowId: null, breathChars: 0, error: null };
  const cache = read_json(statePath, {});
  let breathText = cache.breathText || "";
  let windowId = cache.windowId || null;

  if (newWindow || !breathText) {
    result.calledLoci = true;
    try {
      const client = make_client({ address, timeout_ms });
      const text = await client.call_tool("breath", {});
      windowId = `window-${now.toISOString()}`;
      breathText = String(text || "");
      write_json(statePath, { windowId, breathText, fetchedAt: now.toISOString() });
      log_line(logPath, { time: now.toISOString(), request_id: requestId, actor: "gateway/auto_paste", action: "breath_fetch", status: "ok", window_id: windowId, result_chars: breathText.length });
      result.cacheWritten = true;
    } catch (err) {
      result.error = String(err?.message || err);
      log_line(logPath, { time: now.toISOString(), request_id: requestId, actor: "gateway/auto_paste", action: "breath_fetch", status: "error", error: result.error, fallback_to_stale_cache: Boolean(breathText) });
      // 🔴 失败不挡聊天：有旧缓存就照旧贴旧的（总比什么都没有强），没有就这轮不贴，
      //    绝不能因为 Loci 没起/超时就把她的这句话拦下来。
    }
  }

  if (breathText) {
    const patch = { role: "system", content: build_patch_text(breathText, cache.fetchedAt || now.toISOString()) };
    insert_before_latest_user(Array.isArray(messages) ? messages : [], patch);
    result.patchInjected = true;
    result.breathChars = breathText.length;
    // 这轮没重新调 Loci、纯粹重贴缓存里的旧文本 —— 语义上就是「命中缓存」。
    result.cacheHit = !result.cacheWritten;
  }
  result.windowId = windowId;
  return result;
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

function strong_hits(text) {
  // 小写比对：英文词表不能因为句首大写（"Remember when…"）就整条漏掉。
  // 中文没有大小写，toLowerCase 对它是恒等变换，所以这一行对我们零影响。
  const lowered = String(text).toLowerCase();
  return STRONG_WORDS.filter(word => lowered.includes(String(word).toLowerCase()));
}

// 弱档触发的完整短句停用表：应声词/问候语，一字不差命中就不算「有实质内容」。
// ⚠️ **这不是真正的「主体名词抽取」**（开工单 4.1 写的是「主体名词 + 本地向量」）——
// gateway 这层没有分词器也没有本地向量模型，中文又没有空格，简单正则切不出词。
// 这儿退而求其次：**过滤掉明显没主体的应声话**（她打个「嗯」「在吗」不该去问
// Loci），剩下的但凡有点实质内容的都放行去调 recall —— 真正的语义判断留给
// Loci 自己的 recall（本地向量、拿真分数）去做，这一层只管「值不值得问一句」。
// 这是已知的简化，交活报告里点名了，将来想做真的主体抽取可以在这个函数里换。
const WEAK_STOPWORDS = new Set([
  "在吗", "在么", "你好", "嗨", "hi", "hello", "早", "早安", "晚安",
  "谢谢", "谢谢你", "谢啦", "多谢",
  "好的", "好嘞", "好呀", "好", "行", "行吧", "ok", "okay",
  "嗯", "嗯嗯", "哦", "哦哦", "噢", "知道了", "收到",
  "没事", "没什么", "无事", "没有", "算了",
]);

function weak_triggered(text) {
  const trimmed = String(text || "").trim();
  if (!trimmed) return false;
  // 去掉尾部的标点噪声（"在吗？" 也该算应声话），再对完整句做停用表匹配。
  const stripped = trimmed.replace(/[，。！？、,.!?~～…\s]+$/g, "");
  if (WEAK_STOPWORDS.has(stripped)) return false;
  // 有效字数：中文按字数、英文/数字按连续字母数字串的长度算——太短的不构成主体
  // （单字应声、两三个字的口头禅），三个字起才当「这句话像是在说点什么」。
  const han_char_count = (stripped.match(/[一-鿿]/g) || []).length;
  const word_char_count = (stripped.match(/[A-Za-z]{2,}|[0-9]+/g) || []).reduce((n, w) => n + w.length, 0);
  return han_char_count + word_char_count >= 3;
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
function parse_score_line(text) {
  const entries = [];
  for (const line of String(text || "").split(/\r?\n/)) {
    const m = /^\s*([0-9]+(?:\.[0-9]+)?)\s{2,}.*?\(([0-9a-zA-Z]{4,})\)/.exec(line);
    if (!m) continue;
    entries.push({ id: m[2], score100: Number(m[1]), isMind: line.includes("🧠") });
  }
  return entries;
}

// 注入文案：**只报数量，不报内容**——开工单 4.2 的字面要求，也是最容易踩的坑
// （随手把摘要也带上就是「系统替我想起」，正是她 8-16 改掉的那半）。
// 正文以「〔记忆提醒〕」开头是**字面约定**：src/gateway/messages.js 的
// `moveSystemPatchesBeforeLatestUser` 认这个前缀，给它开了豁免（见那边的
// isReminderPinnedAfterLatestUser）——这个豁免目前是防御性的：现在的实现里
// 提醒行压根不经过那条内部 messages 流水线（见下面 build_relevance_notice 的文档），
// 所以这个函数眼下碰不到它；万一以后有代码改道把它塞回那条内部流水线，这个
// 豁免能接住，不会悄悄被搬回 user 之前。改这句开头前缀要跟那边一起改。
function build_notice_line(event_count, mind_count) {
  return `〔记忆提醒〕和这句有关：事件 ${event_count} 条 · 认知 ${mind_count} 条`;
}

// 贴在整个 messages 的**真尾巴**（最新 user 之后）——她 2026-08-18 追加的一刀：
// 离模型开口最近、命中率最高，类似 hook 往 user prompt 后面追加上下文的姿势。
// 🔴 **这个函数不在这个模块里调**：build_relevance_notice 只算、只把 patch 放进返回值
// 的 记录.patch，真正 push 的动作交给 server.js，在它自己拼完 outgoingBody
// （真正发给上游那份）之后再做。原因写在 build_relevance_notice 的文档注释里——
// server.js 内部那条「messages」在送去上游之前要过三关校验/重建，全都假定
// 「最新 user 之后只能是合法的 tool 续接」，塞一条 system 进去要么被吞、要么
// 直接 400。真正安全的位置是**校验通过、组好 outgoingBody 之后**，不是内部
// 那条 messages 流水线的任何一站。留这个 helper 在这儿是给 server.js（还有测试）
// 复用同一个"push 到真尾巴"手势，不是自己在用。
function attach_at_true_tail(messages, patch) {
  if (Array.isArray(messages)) messages.push(patch);
}

/**
 * B：**触发才跑**。强档=关键词命中，弱档=本地判据（见 weak_triggered 注释）过滤掉
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
 * 的做法是压根不让它进那条内部 messages：build_relevance_notice 只把 patch 算出来，
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
async function build_relevance_notice({
  messages,
  requestId,
  now = new Date(),
  logPath = DEFAULT_LOG_PATH,
  地址: address = DEFAULT_ADDRESS,
  // 🔴 2026-08-19 从 5000 提到 12000。实测：956 条的库跑一次带 query 的 recall
  //    要 5~7 秒（向量 + BM25 一起跑），而超时卡在 5 秒 ——
  //    **于是这个功能从上线到今天一次都没成功过**：每次都 abort，
  //    命中数恒为 0、patch 恒为 null，而且失败只进日志、聊天照常，
  //    所以没有任何地方看得出来它没在工作。
  //    库越大越慢，这个数该跟着库走；env RELEVANCE_TIMEOUT_MS 可调。
  超时毫秒: timeout_ms = Number(process.env.RELEVANCE_TIMEOUT_MS || 12000),
  最低分: min_score = Number(process.env.RELEVANCE_MIN_SCORE || DEFAULT_MIN_SCORE),
} = {}) {
  const user_text = latestUserText(messages).trim();
  const strong_matched = user_text ? strong_hits(user_text) : [];
  // 🔴 2026-08-19 弱档改成**默认关**（env RELEVANCE_WEAK=1 打开）。
  //    不是因为它不准，是因为它太宽：判据是「过滤掉应声词之后但凡有三个字
  //    以上的实质内容就放行」——日常说话几乎每句都过。而一次 recall 要 5~7 秒，
  //    等于**每一轮都卡六秒**。强档（「上次 / 还记得 / 之前」这类词）触发得少，
  //    该等的时候才等。想全都要的人自己开。
  const weak_enabled = String(process.env.RELEVANCE_WEAK || "").trim() === "1";
  const weak_matched = weak_enabled && Boolean(user_text) && strong_matched.length === 0 && weak_triggered(user_text);
  const triggered = strong_matched.length > 0 || weak_matched;

  const record = {
    time: now.toISOString(),
    request_id: requestId,
    actor: "gateway/relevance_reminder",
    action: "relevance_reminder_observed",
    triggered: triggered,
    trigger_kind: strong_matched.length > 0 ? "strong" : (weak_matched ? "weak" : "none"),
    strong_matched_keywords: strong_matched,
    min_score: min_score,
    recall_called: false,
    event_count: 0,
    mind_count: 0,
    injected: false,
  };

  if (!user_text || !triggered) {
    record.skipped = !user_text ? "no_user_text" : "not_triggered";
    log_line(logPath, record);
    return record;
  }

  record.recall_called = true; // 触发了就算发起过调用，成不成功是另一件事（error 字段管）
  try {
    const client = make_client({ address, timeout_ms });
    const text = await client.call_tool("recall", { query: user_text.slice(0, 120) });
    const passed = parse_score_line(text).filter(entry => entry.score100 >= min_score);
    const mind_entries = passed.filter(entry => entry.isMind);
    const event_entries = passed.filter(entry => !entry.isMind);
    record.event_count = event_entries.length;
    record.mind_count = mind_entries.length;
    // 日志留 id 方便查证据链，**不留正文**——跟注入文案同一条纪律。
    record.matched_ids = passed.map(entry => entry.id);

    if (passed.length > 0) {
      // 🔴 只算，不碰 messages——真正 push 的动作交给 server.js，在 outgoingBody
      // 组完之后做。理由见本函数上方文档注释的 ①②③。
      record.patch = { role: "system", content: build_notice_line(event_entries.length, mind_entries.length) };
      record.injected = true;
    }
  } catch (err) {
    record.error = String(err?.message || err);
  }

  log_line(logPath, record);
  return record;
}

module.exports = {
  MARKER,
  DEFAULT_ADDRESS,
  DEFAULT_STATE_PATH,
  DEFAULT_LOG_PATH,
  STRONG_WORDS,
  DEFAULT_MIN_SCORE,
  attach_once,
  build_relevance_notice,
  // attach_at_true_tail：server.js 组完 outgoingBody 之后，真正 push 记录.patch 用的是
  // 这个（不是 _internal——它是生产代码要用的手势，不只是测试）。
  attach_at_true_tail,

// ── 过时别名，2026-08-20 之前叫这个名字。下个大版本删。────────────────────────
// 🔴 绑定改成英文是内部事，但这几个键是**别人 require() 之后要亲手敲的名字** ——
//    删掉的话，别人的代码会当场断在一个他打不出来的名字上。留一行成本为零。
  默认地址: DEFAULT_ADDRESS,
  默认状态档: DEFAULT_STATE_PATH,
  默认日志档: DEFAULT_LOG_PATH,
  强档关键词: STRONG_WORDS,
  默认最低分: DEFAULT_MIN_SCORE,

// ── 英文别名（2026-08-19 她提的：「你就不怕别人不好改吗」）─────────────────────
// 🔴 **只是别名，指的是同一个函数**。文件内部照旧中文——`算相关记忆提醒` 一眼知道
//    它干嘛，改成 computeRelevanceReminder 还得在脑子里翻译一次，而且改内部纯属
//    给自己制造 bug。但**对外这几个名字是别人要亲手敲的**，一个不认识汉字的人
//    连自己粘的是哪个都不知道。名字是给读的人用的，谁读就照顾谁。
  // computeReminder({ messages, requestId, 地址, 最低分 }) → { patch, ... }
  computeReminder: build_relevance_notice,
  // appendToTail(messages, patch) —— 组完请求体之后的最后一步
  appendToTail: attach_at_true_tail,
  paste: attach_once,
  MARKER_LINE: MARKER,
  // DEFAULT_ADDRESS / DEFAULT_MIN_SCORE / STRONG_WORDS 现在就是正式名字了，导在上面。

  _internal: { make_client, latestUserText, strong_hits, weak_triggered, parse_score_line, build_patch_text, build_notice_line },
};
