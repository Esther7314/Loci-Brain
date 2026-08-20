// ============================================================
// gateway/tests/fake_loci.js —— 冒充记忆系统的那一头
//
// 🔴 **必须是假的。** 真的 Loci 在 18002（她的记忆库），跑测试绝不许碰它：
//    一来 recall 一次要几秒、结果还跟着库变，断言没法写死；
//    二来那是她的东西，测试不该有任何理由去敲它的门。
//    这个假货返回一份**写死的、可预测的**检索结果，断言就照着这份对。
//
// 它冒充两个面（真 Loci 这两个口挂在同一个端口上，网关也是照这个假设来的）：
//   · `POST /mcp`            MCP streamable-http，auto_attach.js 的 recall 走这儿
//   · `GET  /api/loci/poke`  普通 REST，poke_delivery.js 走这儿（这一单不测它，
//                            但它跟被测路径同一个请求里，所以也得记账 ——
//                            「谁都没漏出去」这句话要能拿账本证明）
//
// 四种脾气（设模式 切）：
//   正常   —— 老老实实返回那份写死的检索结果
//   五百   —— 握手正常，tools/call 回 HTTP 500（Loci 活着但坏了）
//   断连   —— 任何 /mcp 请求直接掐断连接（Loci 压根没起）
//   慢     —— tools/call 故意拖过网关的超时（就是那个「5 秒 bug」的现场）
//   换排版 —— 内容一样、排版换了（Loci 哪天改了 recall 的渲染就是这样）
//   空库   —— 查成功了，但一条相关的都没有（**新装的人第一天就是这个样子**）
// ============================================================

const http = require("node:http");

// ——— 写死的检索结果：抄 Loci recall 的渲染排版（auto_attach.js parse_score_line 认这个格式）———
// `{score:5.1f}  [🧠]{摘要}  ({短id})  {MM-DD}`，分数是 0~100 的尺度。
// 故意放一条 12.7 分的在里面：**它必须被分数线挡掉**，不然「过线才算」就是假的。
const RENDERED_TEXT = [
  "找到 4 条：",
  " 88.4  上次她把网关的超时从 5 秒提到 12 秒。  (aa11bb22)  08-19",
  " 71.0  今晚她连上了 Loci，第一次睁眼。  (cc33dd44)  08-03",
  " 63.2  🧠 她要的不是我少犯错，是我别装。  (ee55ff66)  08-05",
  " 12.7  一条不该过线的旧事。  (99aa88bb)  07-11",
  "另有 12 条在线下。",
].join("\n");

// 上面那份渲染文本按「≥50 分才算」应该得出的结论 —— 断言拿这几个数去对，
// 而不是在测试里另抄一遍魔法数字（抄两遍就会有一天对不上）。
const EXPECTED_PASSING_IDS = ["aa11bb22", "cc33dd44", "ee55ff66"];

// **同样的内容，换一种排版** —— 日期挪到前面、id 从圆括号换成方括号。
// Loci 那边哪天改一下 recall 的渲染就是这个样子。auto_attach.js 的 parse_score_line 是照着
// 旧排版写死的正则，换了就一条都认不出来 —— 用来把那个「静默失明」照出来。
const RELAYOUT_RENDERED_TEXT = [
  "找到 4 条：",
  " 08-19  88.4  上次她把网关的超时从 5 秒提到 12 秒。  [aa11bb22]",
  " 08-03  71.0  今晚她连上了 Loci，第一次睁眼。  [cc33dd44]",
  " 08-05  63.2  🧠 她要的不是我少犯错，是我别装。  [ee55ff66]",
  " 07-11  12.7  一条不该过线的旧事。  [99aa88bb]",
].join("\n");
const EXPECTED_EVENT_COUNT = 2;   // 88.4 / 71.0，没戴 🧠 牌
const EXPECTED_MIND_COUNT = 1;   // 63.2 戴了 🧠 牌
const BLOCKED_ID = "99aa88bb";
// 查成功了，但库里就是没有相关的东西 —— 这不是坏，这是新装的人的第一天。
// 它在日志里留下的痕迹跟「Loci 换了排版」**一模一样**（triggered、recall_called、
// 0 条、injected=false、没有 error），健康口分不出这两者，见测试第八节。
const EMPTY_RENDERED_TEXT = "找到 0 条。";

// 真正的记忆正文 —— **一个字都不许出现在贴回去的那行里**（「只报数量不报正文」）
const MEMORY_BODY_SAMPLES = ["上次她把网关的超时从 5 秒提到 12 秒。", "她要的不是我少犯错，是我别装。"];

async function start_fake_loci({ 端口: port }) {
  const received = [];        // 每一个 HTTP 请求都记一笔（每条测试开头清账）
  const tool_calls = [];    // 只记 tools/call：{ 工具, 参数 }
  // 全程账：**清账清不掉**。用来在最后对总账 ——「整套跑下来某条路一次都没出声」
  // 这种话，只有一份从头记到尾的账本才说得出口。
  const all_received = [];
  let mode = "正常";
  let slow_ms = 3000;
  const timers = new Set();

  function send_sse(res, obj, session) {
    const headers = { "Content-Type": "text/event-stream; charset=utf-8" };
    if (session) headers["Mcp-Session-Id"] = session;
    res.writeHead(200, headers);
    res.end(`event: message\ndata: ${JSON.stringify(obj)}\n\n`);
  }

  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      let body = null;
      try { body = raw ? JSON.parse(raw) : null; } catch { body = null; }
      const route = String(req.url || "").split("?")[0];
      const entry = { 方法: req.method, 路径: route, rpc方法: body?.method || null, 体: body, 模式: mode };
      received.push(entry);
      all_received.push(entry);

      // ——— 断连：Loci 压根没起，连接建了立刻断 ———
      if (mode === "断连" && route === "/mcp") { req.socket.destroy(); return; }

      // ——— MCP 面 ———
      if (req.method === "POST" && route === "/mcp") {
        const rpc = body?.method;
        if (rpc === "initialize") {
          return send_sse(res, {
            jsonrpc: "2.0", id: body.id,
            result: { protocolVersion: "2024-11-05", capabilities: {}, serverInfo: { name: "假loci", version: "0" } },
          // 🔴 会话 id 必须从头里给，不给的话客户端握手会自己判失败。
          //    而且**只能是 ASCII** —— HTTP 头的值是 latin-1，写成中文的话
          //    Node 会 ERR_INVALID_CHAR，握手直接崩（这儿踩过一次）。
          }, "fake-session-1");
        }
        if (rpc === "notifications/initialized") {
          res.writeHead(202); return res.end();     // 通知无 id，202 空身子（真 MCP 就这么回）
        }
        if (rpc === "tools/call") {
          tool_calls.push({ 工具: body?.params?.name, 参数: body?.params?.arguments || {} });
          if (mode === "五百") {
            res.writeHead(500, { "Content-Type": "text/plain" });
            return res.end("假 Loci 故意炸给你看");
          }
          const resp = {
            jsonrpc: "2.0", id: body.id,
            result: { content: [{ type: "text", text:
              mode === "换排版" ? RELAYOUT_RENDERED_TEXT
              : mode === "空库" ? EMPTY_RENDERED_TEXT
              : RENDERED_TEXT }] },
          };
          if (mode === "慢") {
            // 拖过网关的超时再回。回的时候对面多半已经 abort 了，写不进去很正常，
            // 所以整段包起来 —— 假货自己不许把测试进程搞崩。
            const timer = setTimeout(() => {
              timers.delete(timer);
              try { send_sse(res, resp); } catch { /* 对面早走了，正常 */ }
            }, slow_ms);
            if (timer.unref) timer.unref();   // 别让它拖着进程不退出
            timers.add(timer);
            return;
          }
          return send_sse(res, resp);
        }
        res.writeHead(400); return res.end();
      }

      // ——— REST 面：这一单里**它一次都不该被敲响**，敲了就是账本上的证据 ———
      if (route === "/api/loci/poke") {
        res.writeHead(200, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ dreams: [], muse_pending: 0 }));
      }
      if (route === "/api/loci/dream/wake") {
        res.writeHead(200, { "Content-Type": "application/json" });
        return res.end("{}");
      }

      res.writeHead(404); res.end();
    });
  });

  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", resolve);   // 只听回环
  });

  return {
    端口: port,
    地址: `http://127.0.0.1:${port}/mcp`,
    收到: received,
    工具调用: tool_calls,
    全程收到: all_received,
    渲染文本: RENDERED_TEXT,
    换了排版的渲染文本: RELAYOUT_RENDERED_TEXT,
    空库渲染文本: EMPTY_RENDERED_TEXT,
    应该过线的id: EXPECTED_PASSING_IDS, 应该的事件数: EXPECTED_EVENT_COUNT,
    应该的认知数: EXPECTED_MIND_COUNT, 挡在线下的id: BLOCKED_ID, 记忆正文样本: MEMORY_BODY_SAMPLES,
    设模式(new_mode, ms) { mode = new_mode; if (ms != null) slow_ms = ms; },
    清账() { received.length = 0; tool_calls.length = 0; },
    async 关() {
      for (const t of timers) clearTimeout(t);
      timers.clear();
      // 同 假上游：keep-alive 的连接不主动掐掉，close() 会挂在那儿
      server.closeAllConnections?.();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

module.exports = { start_fake_loci, 渲染文本: RENDERED_TEXT, 应该过线的id: EXPECTED_PASSING_IDS,
  应该的事件数: EXPECTED_EVENT_COUNT, 应该的认知数: EXPECTED_MIND_COUNT };
