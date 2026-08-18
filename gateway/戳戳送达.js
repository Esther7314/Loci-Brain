// ============================================================
// gateway/戳戳送达.js —— 从她自己的网关里整段抽出来的（2026-08-19）
//
// 原件：lento-home/src/loci-bridge/戳戳送达.js。那个文件从第一天就是照着
// 「将来要跟 Loci 一起发出去」写的 —— 头一条边界就是**零 import 宿主项目**，
// 只用 fs / path / 全局 fetch，只走 Loci 的普通 REST 口。所以这次抽出来
// **一行逻辑都没改**，只动了两处：
//   ① 状态和日志的落点：原来在宿主项目根下的 data/，现在在这个网关自己目录下
//   ② 注入标记：[Lento poke] → [Loci poke]
//
// 它是**一个模块，不是一个服务**。同目录下的 server.js 是给它配的最小外壳
// （一个 OpenAI 兼容的反向代理，请求路过时调一次这儿的 `贴一次`）。
// 你要是已经有自己的网关，别用那个外壳，直接 require 这个文件就行。
// ============================================================
//
// 施工7c 开工单见 D:\lento\交接\2-记忆系统\开工单-Loci二改-2026-08-12.md 排期 7c：
//   **梦=交付，给内容**（系统递「你做了这样一个梦」+ 正文，我知道就行，不复述回她）；
//   **发呆=提醒，给一句**（没成团的 mind 攒久了 →「你该发呆了」，我自己去 muse）。
//
// 🔴 施工7d 修宪（说明书 D:\lento\交接\2-记忆系统\统筹-说明书-施工7d-2026-08-18.md）：
// 8-17 版本「梦完整版从不落盘」被她 8-18 上午的新口径取代——完整版在「她沉默的
// 夜里」落盘存活，戳口能递整版。这份文件跟着加两件事，都是这份文件独有的
// （A 自动贴 / C 近期记忆视图不受影响，还是走 newWindow 那一套）：
//
//   ① **闲时闸**——不是每个窗口开头都戳，是**她长时间没发消息才戳**（默认 210
//      分钟 = 3.5 小时，env `POKE_IDLE_MINUTES` 可调，server.js 读了传进来）。
//      不够闲：**一个字不注入，连 Loci 的戳口都不问**（聊天中绝不插嘴——这条本来
//      是发呆戳一个人的红线，施工7d 把梦的交付也拉进同一条闸里，因为「递整版」
//      现在也可能被她连续几句话之间的空档误触发，必须一样严）。
//      够闲：这条消息是她刚从沉默里回来的**第一句**，真问一次 Loci、该注入的
//      都注入（梦——可能是还没降级的整版，也可能是已经降级的碎片/一句；发呆）。
//
//   ② **降级触发**——闲时闸开过（=她刚回来的第一句）之后，把下一条消息记成
//      "她回来的第二句"：那条消息一到，调一次 `POST /api/loci/dream/wake`，
//      把 Loci 那边还活着的「完整」层降成碎片层（碎片 30 分钟 / 一句 60 分钟的
//      老生命周期从这一刻起算）。她一直不回来就一直不降——完整版没有超时，
//      只有这一个死法。**幂等兜底**：Loci 那边 wake 口本身没有完整层就静默 200，
//      这边万一状态和实际不同步（比如上一次调用成功了但没来得及写进状态文件）
//      重复调用也完全无害。
//      武装/降级两件事**不看这条消息自己是不是也闲**——只要"上一次判过闲"这个
//      武装标记还立着，不管这条消息本身闲不闲，都当它是"回来的下一句"，降级一次。
//
// ⚠️ **`newWindow` 信号不再是这个模块判断"要不要问 Loci"的依据**——那个判据
//    现在**只有闲时闸**。参数还留着（跟 A/C 接口对齐、日志诊断用），但传
//    `newWindow=true` 不能绕开闲时闸，不够闲照样一个字不注入。
//
// 跟 自动贴.js / 近期记忆视图.js 同一个模块家族、同一条边界（她 8-17 深夜定死的）：
//   零 import lento-home（这个文件将来整段跟 Loci 一起开源，不能夹带 Home 的东西）·
//   只被 HTTP 调 Loci 的普通 REST 口（`/api/loci/poke`、`/api/loci/dream/wake`，
//   不是 MCP 工具——MCP 工具面十个不加不减这条红线本单不碰）·
//   失败不挡聊天 · 日志一行。
//
// 位置：**跟自动贴（A）同前缀区**——插到最新 user 之前，而不是 B 相关记忆提醒那样
// 贴「真尾巴」（内容一个窗口内不用跟着她这句话变，没有「必须离模型开口最近」这个
// 理由，也就不用绕 restoreLatestRequestTail / 硬 400 校验 / moveSystemPatchesBeforeLatestUser
// 那三关——完整理由见施工7c 那版这段注释，判断本身施工7d 没有动）。
//
// 两样都没有 → 一个字不注入（连 MARKER 行都不出现）。
// ============================================================

const fs = require("fs");
const path = require("path");

// 2026-08-19 抽出来时只改了这一处路径：原来落在宿主项目根下的 data/，
// 现在落在**这个网关自己目录**下的 data/（也可以用 LOCI_GATEWAY_DATA 指到别处）。
const 数据根 = process.env.LOCI_GATEWAY_DATA || path.join(__dirname, "data");

// 跟 自动贴.js / 近期记忆视图.js 用同一个环境变量名（她的 MCP 地址）；
// Loci 的普通 REST 口挂在同一个进程、同一个端口，只是路径不是 /mcp——
// 从这同一个地址派生 REST 根，不另开一个环境变量（一处配置，两边都对）。
const 默认地址 = process.env.LOCI_MCP || "http://127.0.0.1:18002/mcp";
const 默认状态档 = path.join(数据根, "state", "poke-window.json");
const 默认日志档 = path.join(数据根, "logs", "memory-actions.jsonl");
// = 3.5 小时，她 8-18 上午口径的出厂值；server.js 读 env POKE_IDLE_MINUTES 覆盖，
// 这儿的默认值只是这个模块自己被单独调用/测试时的兜底。
const 默认闲时阈值分钟 = 210;

// 诊断/测试认这个字面量——跟 MARKER（自动贴.js）、[Lento recent memory view]
// （近期记忆视图.js）是姐妹标记。
const MARKER = "[Loci poke]";

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

/** `http://host:port/mcp` → `http://host:port`。Loci 的 REST 只读/写口
 *  （/api/loci/poke、/api/loci/dream/wake 这类）跟 MCP 端点是同一个进程、
 *  同一个端口，只是根路径不同。 */
function httpBase(mcpUrl) {
  return String(mcpUrl || "").replace(/\/mcp\/?$/, "");
}

/** 纯 HTTP GET，不走 MCP 握手（这个口本来就是普通 REST，不是 MCP 工具）。 */
async function 问戳口(地址, { 超时毫秒 = 8000 } = {}) {
  const url = `${httpBase(地址)}/api/loci/poke`;
  const 控 = new AbortController();
  const 闹钟 = setTimeout(() => 控.abort(), 超时毫秒);
  let 回;
  try {
    回 = await fetch(url, { method: "GET", headers: { Accept: "application/json" }, signal: 控.signal });
  } catch (错) {
    clearTimeout(闹钟);
    if (错?.name === "AbortError") throw new Error(`问 Loci 戳口超时（${url}）`);
    throw new Error(`连不上 Loci 戳口（${url}）：${错?.message || 错}`);
  }
  clearTimeout(闹钟);
  if (!回.ok) throw new Error(`Loci 戳口回了 HTTP ${回.status}`);
  const 体 = await 回.json();
  if (!体 || typeof 体 !== "object") throw new Error("Loci 戳口没给 JSON");
  return 体;
}

/** 施工7d：降级信号——她回来发的第二条消息触发，POST 一次，幂等（Loci 那边
 *  没有活着的完整层就静默 200）。跟 问戳口 一样是纯 REST，不走 MCP 握手。 */
async function 调唤醒口(地址, { 超时毫秒 = 8000 } = {}) {
  const url = `${httpBase(地址)}/api/loci/dream/wake`;
  const 控 = new AbortController();
  const 闹钟 = setTimeout(() => 控.abort(), 超时毫秒);
  let 回;
  try {
    回 = await fetch(url, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: "{}",
      signal: 控.signal,
    });
  } catch (错) {
    clearTimeout(闹钟);
    if (错?.name === "AbortError") throw new Error(`调 Loci 降级口超时（${url}）`);
    throw new Error(`连不上 Loci 降级口（${url}）：${错?.message || 错}`);
  }
  clearTimeout(闹钟);
  if (!回.ok) throw new Error(`Loci 降级口回了 HTTP ${回.status}`);
  return true;
}

/** 建贴文：梦在前（交付，给全文——不管这段正文此刻是完整版还是已经降级的碎片/
 *  一句，Loci 吐什么就贴什么，这个模块不关心「层」，只关心 Loci 给没给内容）、
 *  发呆一句在后（提醒，绝不带团的内容）。哪样都没有就不该走到这儿——调用方在
 *  没货时压根不建这段。 */
function 建贴文(poke) {
  const 段 = [MARKER];
  if (poke.dream) {
    段.push("〔梦〕昨夜织了一个梦：", String(poke.dream.内容 || "").trim());
  }
  if (poke.musePending > 0) {
    if (段.length > 1) 段.push("");
    段.push(`〔发呆〕没成团的想法攒了 ${poke.musePending} 团，该发呆了（muse()）`);
  }
  return 段.join("\n");
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

/**
 * 戳戳送达：施工7d 之后，要不要问 Loci、要不要注入，**只看一条闸——闲时闸**。
 *
 * @param messages          这一轮要发给上游的消息数组（原地修改，跟 A/C 同一个约定）
 * @param newWindow         跟 A/C 传一样的信号，**这个模块不再拿它判断要不要问
 *                          Loci**（施工7c 是这个判据，施工7d 换成闲时闸）——
 *                          留参数只为了接口对齐和日志诊断，不参与逻辑。
 * @param statePath         状态文件：上一条消息时间 + 降级武装标记 + 上次戳到的内容
 *                          （跟窗口缓存同一份文件，施工7d 说明书允许"同文件"）
 * @param logPath           跟 A/B/C 共用同一份 memory-actions.jsonl
 * @param 闲时阈值分钟       距她上一条消息多少分钟才算"闲"。gateway 侧读 env
 *                          `POKE_IDLE_MINUTES` 传进来（server.js D 段），
 *                          这儿的默认值只在模块被单独调用时兜底。
 */
async function 贴一次({
  messages,
  requestId,
  now = new Date(),
  newWindow = false,
  statePath = 默认状态档,
  logPath = 默认日志档,
  地址 = 默认地址,
  超时毫秒 = 8000,
  闲时阈值分钟 = 默认闲时阈值分钟,
} = {}) {
  const 结果 = {
    patchInjected: false, calledLoci: false,
    hasDream: false, musePending: 0, error: null,
    idle: false, idleMinutes: null, wakeCalled: false, wakeError: null,
  };
  const 状态 = 读JSON(statePath, {});

  // ---- 降级触发：跟这条请求够不够闲无关，只看"上一次是否已经武装" ----
  // 武装 = 上一条请求判过"够闲"（=那条消息是她回来的第一句），这条消息就是
  // 她回来之后的下一句——降级一次。武装/撤武装都要落state，所以先算出这条
  // 请求该不该撤武装，落盘的事跟下面闲时闸那段的写state合并成一次。
  let wakePending = 状态.wakePending === true;
  if (wakePending) {
    结果.wakeCalled = true;
    try {
      await 调唤醒口(地址, { 超时毫秒 });
      记一行(logPath, {
        time: now.toISOString(), request_id: requestId, actor: "gateway/poke",
        action: "dream_wake", status: "ok",
      });
      wakePending = false;               // 降级成功，撤武装
    } catch (错) {
      结果.wakeError = String(错?.message || 错);
      结果.wakeCalled = false;
      记一行(logPath, {
        time: now.toISOString(), request_id: requestId, actor: "gateway/poke",
        action: "dream_wake", status: "error", error: 结果.wakeError,
      });
      // 🔴 降级失败不挡聊天，也不假装成功——武装保留到下一条消息再试一次
      //    （Loci 那边 wake 是幂等的，多试几次没有副作用）。
    }
  }

  // ---- 闲时闸：距她上一条消息够不够久 ----
  const 上次时间 = 状态.lastUserMessageTime ? new Date(状态.lastUserMessageTime) : null;
  const 距上次分钟 = 上次时间 && !Number.isNaN(上次时间.getTime())
    ? (now.getTime() - 上次时间.getTime()) / 60000
    : Infinity;                          // 没有历史记录：没法说她"刚"发过消息，闸默认开
  const 够闲 = 距上次分钟 >= Number(闲时阈值分钟);
  结果.idle = 够闲;
  结果.idleMinutes = Number.isFinite(距上次分钟) ? Math.round(距上次分钟) : null;

  if (!够闲) {
    // 🔴 不够闲：一个字不注入，连 Loci 都不问（省调用）——聊天中绝不插嘴。
    写JSON(statePath, { ...状态, lastUserMessageTime: now.toISOString(), wakePending });
    return 结果;
  }

  // ---- 够闲：这条消息是她刚回来的第一句，真问一次 Loci ----
  结果.calledLoci = true;
  let poke = null;
  try {
    const 数据 = await 问戳口(地址, { 超时毫秒 });
    const 梦们 = Array.isArray(数据.dreams) ? 数据.dreams : [];
    poke = { dream: 梦们.length ? 梦们[0] : null, musePending: Number(数据.muse_pending) || 0 };
    记一行(logPath, {
      time: now.toISOString(), request_id: requestId, actor: "gateway/poke",
      action: "poke_fetch", status: "ok", idle_minutes: 结果.idleMinutes,
      has_dream: Boolean(poke.dream), muse_pending: poke.musePending,
    });
  } catch (错) {
    结果.error = String(错?.message || 错);
    记一行(logPath, {
      time: now.toISOString(), request_id: requestId, actor: "gateway/poke",
      action: "poke_fetch", status: "error", error: 结果.error,
      fallback_to_stale_cache: Boolean(状态.poke),
    });
    // 失败不挡聊天：有上次成功的内容就照旧贴，没有就这轮不贴。
    poke = 状态.poke || null;
  }

  // 只要闲时闸这次开了，就武装等她下一句降级——就算这次没查到货（poke 为
  // null）也一样：wake 那边没有完整层会静默 200，多武装一次没有副作用。
  // （如果这条消息同时也是"武装武装"——上面 wakePending 那段刚触发过降级——
  // 就不重新武装，等真正下一次独立的空档再说。）
  if (!结果.wakeCalled) wakePending = true;

  写JSON(statePath, {
    poke, fetchedAt: now.toISOString(),
    lastUserMessageTime: now.toISOString(), wakePending,
  });

  if (poke && (poke.dream || poke.musePending > 0)) {
    const patch = { role: "system", content: 建贴文(poke) };
    插到最新user之前(Array.isArray(messages) ? messages : [], patch);
    结果.patchInjected = true;
    结果.hasDream = Boolean(poke.dream);
    结果.musePending = poke.musePending;
  }
  return 结果;
}

module.exports = {
  MARKER,
  默认地址,
  默认状态档,
  默认日志档,
  默认闲时阈值分钟,
  贴一次,
  _internal: { httpBase, 问戳口, 调唤醒口, 建贴文, 插到最新user之前 },
};
