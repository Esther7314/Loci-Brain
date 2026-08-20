// ============================================================
// gateway/poke_delivery.js —— 从她自己的网关里整段抽出来的（2026-08-19）
//
// 原件：lento-home/src/loci-bridge/戳戳送达.js。那个文件从第一天就是照着
// 「将来要跟 Loci 一起发出去」写的 —— 头一条边界就是**零 import 宿主项目**，
// 只用 fs / path / 全局 fetch，只走 Loci 的普通 REST 口。所以这次抽出来
// **一行逻辑都没改**，只动了两处：
//   ① 状态和日志的落点：原来在宿主项目根下的 data/，现在在这个网关自己目录下
//   ② 注入标记：[Lento poke] → [Loci poke]
//
// 它是**一个模块，不是一个服务**。同目录下的 server.js 是给它配的最小外壳
// （一个 OpenAI 兼容的反向代理，请求路过时调一次这儿的 `attach_once`）。
// 你要是已经有自己的网关，别用那个外壳，直接 require 这个文件就行。
// ============================================================
//
// 这个文件的两条判据（我们自己的开工单里定的，那份单子不在这个仓库里）：
//   **梦=交付，给内容**（系统递「你做了这样一个梦」+ 正文，我知道就行，不复述回她）；
//   **发呆=提醒，给一句**（没成团的 mind 攒久了 →「你该发呆了」，我自己去 muse）。
//
// [!] 2026-08-18 改过一次口径：
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
// 跟 auto_attach.js 同一个模块家族、同一条边界（她 8-17 深夜定死的）。
// ⚰️ 底下几处提到的 `近期记忆视图.js` **2026-08-19 整个撤了**（同目录已无此文件）——
//    留着这些引用是因为它们说的是边界怎么定的，不是在指路：
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
const data_root = process.env.LOCI_GATEWAY_DATA || path.join(__dirname, "data");

// 跟 auto_attach.js / 近期记忆视图.js 用同一个环境变量名（她的 MCP 地址）；
// Loci 的普通 REST 口挂在同一个进程、同一个端口，只是路径不是 /mcp——
// 从这同一个地址派生 REST 根，不另开一个环境变量（一处配置，两边都对）。
const DEFAULT_ADDRESS = process.env.LOCI_MCP || "http://127.0.0.1:18002/mcp";
const DEFAULT_STATE_PATH = path.join(data_root, "state", "poke-window.json");
const DEFAULT_LOG_PATH = path.join(data_root, "logs", "memory-actions.jsonl");
// = 3.5 小时，她 8-18 上午口径的出厂值；server.js 读 env POKE_IDLE_MINUTES 覆盖，
// 这儿的默认值只是这个模块自己被单独调用/测试时的兜底。
const DEFAULT_IDLE_MINUTES = 210;

// 诊断/测试认这个字面量 —— 跟 auto_attach.js 的 [Loci memory context] 是姐妹标记。
const MARKER = "[Loci poke]";

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

/** `http://host:port/mcp` → `http://host:port`。Loci 的 REST 只读/写口
 *  （/api/loci/poke、/api/loci/dream/wake 这类）跟 MCP 端点是同一个进程、
 *  同一个端口，只是根路径不同。 */
function httpBase(mcpUrl) {
  return String(mcpUrl || "").replace(/\/mcp\/?$/, "");
}

/** 纯 HTTP GET，不走 MCP 握手（这个口本来就是普通 REST，不是 MCP 工具）。 */
// 给 Loci 那四条 hook 路由带钥匙（2026-08-20）。
// 🔴 为什么要有它：那四条以前在「免检名单」里 —— 面板设了密码也拦不住，
//    **既能读到梦的正文，又能改状态**。现在改成「门锁了就要钥匙」，
//    桥得把钥匙带上，否则梦和戳戳会 401。
// ⚠️ 走请求头，**不走地址栏** —— 地址栏会被日志 / Referer / 浏览器历史带出去。
// ⚠️ 没配 `LOCI_HOOK_TOKEN` 也照常跑：Loci 那头只有**门锁着**的时候才要钥匙。
//    （所以不设密码的人一切照旧，什么都不用改。）
function request_headers() {
  const h = { Accept: "application/json" };
  const k = String(process.env.LOCI_HOOK_TOKEN || "").trim();
  if (k) h["x-loci-hook-token"] = k;
  return h;
}

async function fetch_poke(address, { timeout_ms = 8000 } = {}) {
  const url = `${httpBase(address)}/api/loci/poke`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout_ms);
  let resp;
  try {
    resp = await fetch(url, { method: "GET", headers: request_headers(), signal: controller.signal });
  } catch (err) {
    clearTimeout(timer);
    if (err?.name === "AbortError") throw new Error(`问 Loci 戳口超时（${url}）`);
    throw new Error(`连不上 Loci 戳口（${url}）：${err?.message || err}`);
  }
  clearTimeout(timer);
  if (!resp.ok) throw new Error(`Loci 戳口回了 HTTP ${resp.status}`);
  const body = await resp.json();
  if (!body || typeof body !== "object") throw new Error("Loci 戳口没给 JSON");
  return body;
}

/** 施工7d：降级信号——她回来发的第二条消息触发，POST 一次，幂等（Loci 那边
 *  没有活着的完整层就静默 200）。跟 fetch_poke 一样是纯 REST，不走 MCP 握手。 */
async function call_wake(address, { timeout_ms = 8000 } = {}) {
  const url = `${httpBase(address)}/api/loci/dream/wake`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout_ms);
  let resp;
  try {
    resp = await fetch(url, {
      method: "POST",
      headers: { ...request_headers(), "Content-Type": "application/json" },
      body: "{}",
      signal: controller.signal,
    });
  } catch (err) {
    clearTimeout(timer);
    if (err?.name === "AbortError") throw new Error(`调 Loci 降级口超时（${url}）`);
    throw new Error(`连不上 Loci 降级口（${url}）：${err?.message || err}`);
  }
  clearTimeout(timer);
  if (!resp.ok) throw new Error(`Loci 降级口回了 HTTP ${resp.status}`);
  return true;
}

/** build_patch_text：梦在前（交付，给全文——不管这段正文此刻是完整版还是已经降级的碎片/
 *  一句，Loci 吐什么就贴什么，这个模块不关心「层」，只关心 Loci 给没给内容）、
 *  发呆一句在后（提醒，绝不带团的内容）。哪样都没有就不该走到这儿——调用方在
 *  没货时压根不建这段。 */
function build_patch_text(poke) {
  const parts = [MARKER];
  if (poke.dream) {
    parts.push("〔梦〕昨夜织了一个梦：", String(poke.dream.内容 || "").trim());
  }
  if (poke.musePending > 0) {
    if (parts.length > 1) parts.push("");
    parts.push(`〔发呆〕没成团的想法攒了 ${poke.musePending} 团，该发呆了（muse()）`);
  }
  return parts.join("\n");
}

function insert_before_latest_user(messages, patch) {
  let latest_user_index = messages.length;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i]?.role === "user") { latest_user_index = i; break; }
  }
  let insert_at = 0;
  while (insert_at < messages.length && messages[insert_at]?.role === "system" && insert_at < latest_user_index) insert_at += 1;
  messages.splice(insert_at, 0, patch);
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
async function attach_once({
  messages,
  requestId,
  now = new Date(),
  newWindow = false,
  statePath = DEFAULT_STATE_PATH,
  logPath = DEFAULT_LOG_PATH,
  地址: address = DEFAULT_ADDRESS,
  超时毫秒: timeout_ms = 8000,
  闲时阈值分钟: idle_threshold_minutes = DEFAULT_IDLE_MINUTES,
} = {}) {
  const result = {
    patchInjected: false, calledLoci: false,
    hasDream: false, musePending: 0, error: null,
    idle: false, idleMinutes: null, wakeCalled: false, wakeError: null,
  };
  const state = read_json(statePath, {});

  // ---- 降级触发：跟这条请求够不够闲无关，只看"上一次是否已经武装" ----
  // 武装 = 上一条请求判过"够闲"（=那条消息是她回来的第一句），这条消息就是
  // 她回来之后的下一句——降级一次。武装/撤武装都要落state，所以先算出这条
  // 请求该不该撤武装，落盘的事跟下面闲时闸那段的写state合并成一次。
  let wakePending = state.wakePending === true;
  if (wakePending) {
    result.wakeCalled = true;
    try {
      await call_wake(address, { timeout_ms });
      log_line(logPath, {
        time: now.toISOString(), request_id: requestId, actor: "gateway/poke",
        action: "dream_wake", status: "ok",
      });
      wakePending = false;               // 降级成功，撤武装
    } catch (err) {
      result.wakeError = String(err?.message || err);
      result.wakeCalled = false;
      log_line(logPath, {
        time: now.toISOString(), request_id: requestId, actor: "gateway/poke",
        action: "dream_wake", status: "error", error: result.wakeError,
      });
      // 🔴 降级失败不挡聊天，也不假装成功——武装保留到下一条消息再试一次
      //    （Loci 那边 wake 是幂等的，多试几次没有副作用）。
    }
  }

  // ---- 闲时闸：距她上一条消息够不够久 ----
  const last_user_time = state.lastUserMessageTime ? new Date(state.lastUserMessageTime) : null;
  const minutes_since = last_user_time && !Number.isNaN(last_user_time.getTime())
    ? (now.getTime() - last_user_time.getTime()) / 60000
    : Infinity;                          // 没有历史记录：没法说她"刚"发过消息，闸默认开
  const idle_enough = minutes_since >= Number(idle_threshold_minutes);
  result.idle = idle_enough;
  result.idleMinutes = Number.isFinite(minutes_since) ? Math.round(minutes_since) : null;

  if (!idle_enough) {
    // 🔴 不够闲：一个字不注入，连 Loci 都不问（省调用）——聊天中绝不插嘴。
    write_json(statePath, { ...state, lastUserMessageTime: now.toISOString(), wakePending });
    return result;
  }

  // ---- 够闲：这条消息是她刚回来的第一句，真问一次 Loci ----
  result.calledLoci = true;
  let poke = null;
  try {
    const data = await fetch_poke(address, { timeout_ms });
    const dreams = Array.isArray(data.dreams) ? data.dreams : [];
    poke = { dream: dreams.length ? dreams[0] : null, musePending: Number(data.muse_pending) || 0 };
    log_line(logPath, {
      time: now.toISOString(), request_id: requestId, actor: "gateway/poke",
      action: "poke_fetch", status: "ok", idle_minutes: result.idleMinutes,
      has_dream: Boolean(poke.dream), muse_pending: poke.musePending,
    });
  } catch (err) {
    result.error = String(err?.message || err);
    log_line(logPath, {
      time: now.toISOString(), request_id: requestId, actor: "gateway/poke",
      action: "poke_fetch", status: "error", error: result.error,
      fallback_to_stale_cache: Boolean(state.poke),
    });
    // 失败不挡聊天：有上次成功的内容就照旧贴，没有就这轮不贴。
    poke = state.poke || null;
  }

  // 只要闲时闸这次开了，就武装等她下一句降级——就算这次没查到货（poke 为
  // null）也一样：wake 那边没有完整层会静默 200，多武装一次没有副作用。
  // （如果这条消息同时也是"武装武装"——上面 wakePending 那段刚触发过降级——
  // 就不重新武装，等真正下一次独立的空档再说。）
  if (!result.wakeCalled) wakePending = true;

  write_json(statePath, {
    poke, fetchedAt: now.toISOString(),
    lastUserMessageTime: now.toISOString(), wakePending,
  });

  if (poke && (poke.dream || poke.musePending > 0)) {
    const patch = { role: "system", content: build_patch_text(poke) };
    insert_before_latest_user(Array.isArray(messages) ? messages : [], patch);
    result.patchInjected = true;
    result.hasDream = Boolean(poke.dream);
    result.musePending = poke.musePending;
  }
  return result;
}

module.exports = {
  MARKER,
  DEFAULT_ADDRESS,
  DEFAULT_STATE_PATH,
  DEFAULT_LOG_PATH,
  DEFAULT_IDLE_MINUTES,
  attach_once,

// ── 过时别名，2026-08-20 之前叫这个名字。下个大版本删。────────────────────────
// 🔴 绑定改成英文是内部事，但这几个键是**别人 require() 之后要亲手敲的名字** ——
//    删掉的话，别人的代码会当场断在一个他打不出来的名字上。留一行成本为零。
  默认地址: DEFAULT_ADDRESS,
  默认状态档: DEFAULT_STATE_PATH,
  默认日志档: DEFAULT_LOG_PATH,
  默认闲时阈值分钟: DEFAULT_IDLE_MINUTES,

// ── 英文别名（2026-08-19 她提的：「你就不怕别人不好改吗」）─────────────────────
// 🔴 **只是别名，指的是同一个函数**。文件内部照旧中文——`算相关记忆提醒` 一眼知道
//    它干嘛，改成 computeRelevanceReminder 还得在脑子里翻译一次，而且改内部纯属
//    给自己制造 bug。但**对外这几个名字是别人要亲手敲的**，一个不认识汉字的人
//    连自己粘的是哪个都不知道。名字是给读的人用的，谁读就照顾谁。
  // paste({ messages, requestId, 地址, 闲时阈值分钟 }) —— 就地改 messages
  paste: attach_once,
  MARKER_LINE: MARKER,
  // DEFAULT_ADDRESS / DEFAULT_IDLE_MINUTES 现在就是正式名字了，导在上面。

  _internal: { httpBase, fetch_poke, call_wake, build_patch_text, insert_before_latest_user },
};
