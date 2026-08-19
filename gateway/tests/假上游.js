// ============================================================
// gateway/tests/假上游.js —— 冒充「真正的模型」的那一头
//
// 为什么要它：**网关有没有真的改过消息，只有上游看得见。**
// 网关改的是它自己进程里那个 messages 数组，客户端这边看不到；
// 日志说「贴了」也只是网关自己在说自己。所以断言必须打在
// **上游收到的那个 body** 上 —— 那是唯一一份「真的送出去了」的证据。
//
// 它还兼职记账：每一个到达这儿的请求都记一笔（方法/路径/头/体），
// 跑完对账，确认网关一条请求都没漏到别处去。
// ============================================================

const http = require("node:http");
const zlib = require("node:zlib");

/**
 * @param 端口  外面挑好、确认过没被占用的高位端口（19xxx）
 */
async function 起假上游({ 端口 }) {
  const 收到 = [];
  // 压缩：真上游（DeepSeek / OpenAI / GLM）只要请求头里有 accept-encoding 就会 gzip。
  // 默认关着，只有专门测转发的那条会打开。
  let 要压缩 = false;

  const 服务 = http.createServer((req, res) => {
    const 块 = [];
    req.on("data", (c) => 块.push(c));
    req.on("end", () => {
      const 原文 = Buffer.concat(块).toString("utf8");
      let 体 = null;
      try { 体 = 原文 ? JSON.parse(原文) : null; } catch { 体 = null; }
      收到.push({
        方法: req.method,
        路径: req.url,
        头: { ...req.headers },
        体,
        原文,
        // 网关不该把 messages 弄丢或弄乱，所以整份原文也留着，出问题能逐字比
      });
      const 回 = {
        id: "假上游-固定回应",
        object: "chat.completion",
        choices: [{ index: 0, message: { role: "assistant", content: "假上游收到了。" }, finish_reason: "stop" }],
      };
      const 正文 = Buffer.from(JSON.stringify(回), "utf8");
      if (要压缩) {
        // 真上游就是这么回的：gzip 过的身子 + content-encoding + **压缩后**的 content-length
        const 压 = zlib.gzipSync(正文);
        res.writeHead(200, {
          "Content-Type": "application/json; charset=utf-8",
          "Content-Encoding": "gzip",
          "Content-Length": String(压.length),
        });
        return res.end(压);
      }
      res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
      res.end(正文);
    });
  });

  await new Promise((好, 坏) => {
    服务.once("error", 坏);
    // 只听 127.0.0.1：不给局域网留任何一个口子
    服务.listen(端口, "127.0.0.1", 好);
  });

  return {
    端口,
    地址: `http://127.0.0.1:${端口}/v1`,
    收到,
    清账() { 收到.length = 0; },
    最后一笔() { return 收到[收到.length - 1]; },
    设压缩(开) { 要压缩 = Boolean(开); },
    /** 上游那份回应正文的原样（没压缩时客户端应该逐字拿到这个） */
    应该拿到的正文: JSON.stringify({
      id: "假上游-固定回应",
      object: "chat.completion",
      choices: [{ index: 0, message: { role: "assistant", content: "假上游收到了。" }, finish_reason: "stop" }],
    }),
    async 关() {
      // closeAllConnections：网关那边是 keep-alive，光 close() 会一直等着那条连接
      // 自己断，测试就卡在收尾里不退出了。
      服务.closeAllConnections?.();
      await new Promise((好) => 服务.close(好));
    },
  };
}

module.exports = { 起假上游 };
