// ============================================================
// gateway/tests/假loci.js —— 冒充记忆系统的那一头
//
// 🔴 **必须是假的。** 真的 Loci 在 18002（她的记忆库），跑测试绝不许碰它：
//    一来 recall 一次要几秒、结果还跟着库变，断言没法写死；
//    二来那是她的东西，测试不该有任何理由去敲它的门。
//    这个假货返回一份**写死的、可预测的**检索结果，断言就照着这份对。
//
// 它冒充两个面（真 Loci 这两个口挂在同一个端口上，网关也是照这个假设来的）：
//   · `POST /mcp`            MCP streamable-http，自动贴.js 的 recall 走这儿
//   · `GET  /api/loci/poke`  普通 REST，戳戳送达.js 走这儿（这一单不测它，
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

// ——— 写死的检索结果：抄 Loci recall 的渲染排版（自动贴.js 解析分数行 认这个格式）———
// `{score:5.1f}  [🧠]{摘要}  ({短id})  {MM-DD}`，分数是 0~100 的尺度。
// 故意放一条 12.7 分的在里面：**它必须被分数线挡掉**，不然「过线才算」就是假的。
const 渲染文本 = [
  "找到 4 条：",
  " 88.4  上次她把网关的超时从 5 秒提到 12 秒。  (aa11bb22)  08-19",
  " 71.0  今晚她连上了 Loci，第一次睁眼。  (cc33dd44)  08-03",
  " 63.2  🧠 她要的不是我少犯错，是我别装。  (ee55ff66)  08-05",
  " 12.7  一条不该过线的旧事。  (99aa88bb)  07-11",
  "另有 12 条在线下。",
].join("\n");

// 上面那份渲染文本按「≥50 分才算」应该得出的结论 —— 断言拿这几个数去对，
// 而不是在测试里另抄一遍魔法数字（抄两遍就会有一天对不上）。
const 应该过线的id = ["aa11bb22", "cc33dd44", "ee55ff66"];

// **同样的内容，换一种排版** —— 日期挪到前面、id 从圆括号换成方括号。
// Loci 那边哪天改一下 recall 的渲染就是这个样子。自动贴.js 的 解析分数行 是照着
// 旧排版写死的正则，换了就一条都认不出来 —— 用来把那个「静默失明」照出来。
const 换了排版的渲染文本 = [
  "找到 4 条：",
  " 08-19  88.4  上次她把网关的超时从 5 秒提到 12 秒。  [aa11bb22]",
  " 08-03  71.0  今晚她连上了 Loci，第一次睁眼。  [cc33dd44]",
  " 08-05  63.2  🧠 她要的不是我少犯错，是我别装。  [ee55ff66]",
  " 07-11  12.7  一条不该过线的旧事。  [99aa88bb]",
].join("\n");
const 应该的事件数 = 2;   // 88.4 / 71.0，没戴 🧠 牌
const 应该的认知数 = 1;   // 63.2 戴了 🧠 牌
const 挡在线下的id = "99aa88bb";
// 查成功了，但库里就是没有相关的东西 —— 这不是坏，这是新装的人的第一天。
// 它在日志里留下的痕迹跟「Loci 换了排版」**一模一样**（triggered、recall_called、
// 0 条、injected=false、没有 error），健康口分不出这两者，见测试第八节。
const 空库渲染文本 = "找到 0 条。";

// 真正的记忆正文 —— **一个字都不许出现在贴回去的那行里**（「只报数量不报正文」）
const 记忆正文样本 = ["上次她把网关的超时从 5 秒提到 12 秒。", "她要的不是我少犯错，是我别装。"];

async function 起假loci({ 端口 }) {
  const 收到 = [];        // 每一个 HTTP 请求都记一笔（每条测试开头清账）
  const 工具调用 = [];    // 只记 tools/call：{ 工具, 参数 }
  // 全程账：**清账清不掉**。用来在最后对总账 ——「整套跑下来某条路一次都没出声」
  // 这种话，只有一份从头记到尾的账本才说得出口。
  const 全程收到 = [];
  let 模式 = "正常";
  let 慢多久毫秒 = 3000;
  const 定时器们 = new Set();

  function 送SSE(res, 对象, 会话) {
    const 头 = { "Content-Type": "text/event-stream; charset=utf-8" };
    if (会话) 头["Mcp-Session-Id"] = 会话;
    res.writeHead(200, 头);
    res.end(`event: message\ndata: ${JSON.stringify(对象)}\n\n`);
  }

  const 服务 = http.createServer((req, res) => {
    const 块 = [];
    req.on("data", (c) => 块.push(c));
    req.on("end", () => {
      const 原文 = Buffer.concat(块).toString("utf8");
      let 体 = null;
      try { 体 = 原文 ? JSON.parse(原文) : null; } catch { 体 = null; }
      const 路径 = String(req.url || "").split("?")[0];
      const 一笔 = { 方法: req.method, 路径, rpc方法: 体?.method || null, 体, 模式 };
      收到.push(一笔);
      全程收到.push(一笔);

      // ——— 断连：Loci 压根没起，连接建了立刻断 ———
      if (模式 === "断连" && 路径 === "/mcp") { req.socket.destroy(); return; }

      // ——— MCP 面 ———
      if (req.method === "POST" && 路径 === "/mcp") {
        const rpc = 体?.method;
        if (rpc === "initialize") {
          return 送SSE(res, {
            jsonrpc: "2.0", id: 体.id,
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
          工具调用.push({ 工具: 体?.params?.name, 参数: 体?.params?.arguments || {} });
          if (模式 === "五百") {
            res.writeHead(500, { "Content-Type": "text/plain" });
            return res.end("假 Loci 故意炸给你看");
          }
          const 回 = {
            jsonrpc: "2.0", id: 体.id,
            result: { content: [{ type: "text", text:
              模式 === "换排版" ? 换了排版的渲染文本
              : 模式 === "空库" ? 空库渲染文本
              : 渲染文本 }] },
          };
          if (模式 === "慢") {
            // 拖过网关的超时再回。回的时候对面多半已经 abort 了，写不进去很正常，
            // 所以整段包起来 —— 假货自己不许把测试进程搞崩。
            const 闹钟 = setTimeout(() => {
              定时器们.delete(闹钟);
              try { 送SSE(res, 回); } catch { /* 对面早走了，正常 */ }
            }, 慢多久毫秒);
            if (闹钟.unref) 闹钟.unref();   // 别让它拖着进程不退出
            定时器们.add(闹钟);
            return;
          }
          return 送SSE(res, 回);
        }
        res.writeHead(400); return res.end();
      }

      // ——— REST 面：这一单里**它一次都不该被敲响**，敲了就是账本上的证据 ———
      if (路径 === "/api/loci/poke") {
        res.writeHead(200, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ dreams: [], muse_pending: 0 }));
      }
      if (路径 === "/api/loci/dream/wake") {
        res.writeHead(200, { "Content-Type": "application/json" });
        return res.end("{}");
      }

      res.writeHead(404); res.end();
    });
  });

  await new Promise((好, 坏) => {
    服务.once("error", 坏);
    服务.listen(端口, "127.0.0.1", 好);   // 只听回环
  });

  return {
    端口,
    地址: `http://127.0.0.1:${端口}/mcp`,
    收到,
    工具调用,
    全程收到,
    渲染文本,
    换了排版的渲染文本,
    空库渲染文本,
    应该过线的id, 应该的事件数, 应该的认知数, 挡在线下的id, 记忆正文样本,
    设模式(新模式, 毫秒) { 模式 = 新模式; if (毫秒 != null) 慢多久毫秒 = 毫秒; },
    清账() { 收到.length = 0; 工具调用.length = 0; },
    async 关() {
      for (const t of 定时器们) clearTimeout(t);
      定时器们.clear();
      // 同 假上游：keep-alive 的连接不主动掐掉，close() 会挂在那儿
      服务.closeAllConnections?.();
      await new Promise((好) => 服务.close(好));
    },
  };
}

module.exports = { 起假loci, 渲染文本, 应该过线的id, 应该的事件数, 应该的认知数 };
