// ============================================================
// gateway/tests/起网关.js —— 把 gateway/server.js 当**黑盒**起在一个子进程里
//
// 为什么起子进程、不 require 进来测：
//   server.js 的一堆配置（端口、上游、Loci 地址、数据落点）是在**模块加载那一刻**
//   从 env 读死的。require 进来就没法再改，也就测不出「网关按配置真的把请求
//   转到了这儿」这件事。黑盒起进程 = 测的是她真正会跑起来的那个东西。
//
// 这个文件还管两件生死攸关的事：
//   ① 环境**不继承**任何 RELEVANCE_* / LOCI_* —— 机器上要是恰好设了
//      RELEVANCE_WEAK=1 或者 LOCI_MCP 指着真的 18002，测试就会变成薛定谔的绿，
//      甚至真的去敲她的记忆库。这儿一律删掉再显式重设。
//   ② 起进程时挂上 网络围栏（-r），白名单只有两个假端口。
//
// 收摊：只掐**自己 spawn 的那个 pid**，绝不 taskkill 任何别的东西。
// ============================================================

const { spawn } = require("node:child_process");
const path = require("node:path");

// 默认就是仓库里那个 server.js。
// `LOCI_GATEWAY_ENTRY` 是给「拿一份改过的副本试一下」用的口子 —— 比如想看
// 「这个 bug 修好之后，那几条测试会不会过」，不用去动仓库里的文件：
//   LOCI_GATEWAY_ENTRY=/某处/server.js  node --test "gateway/tests/*.test.js"
const 网关入口 = process.env.LOCI_GATEWAY_ENTRY || path.join(__dirname, "..", "server.js");
const 围栏文件 = path.join(__dirname, "网络围栏.js");

/**
 * @param 端口          网关自己听哪儿（外面挑好的 19xxx）
 * @param 上游地址      假上游，形如 http://127.0.0.1:19xxx/v1
 * @param loci地址      假 Loci，形如 http://127.0.0.1:19xxx/mcp
 * @param 数据根        state/logs 落哪儿（测试自己的临时目录，别碰 gateway/data）
 * @param 相关超时毫秒  RELEVANCE_TIMEOUT_MS —— 那个 bug 就出在这个数上
 * @param 白名单端口    围栏放行的端口（只该有假上游 + 假 Loci）
 * @param 账本路径      围栏账本落哪儿
 */
async function 起网关({ 端口, 上游地址, loci地址, 数据根, 相关超时毫秒, 白名单端口, 账本路径 }) {
  const 环境 = { ...process.env };
  // 🔴 先把所有可能影响判断的都清干净，再显式给 —— 别让机器上的 env 说了算
  for (const 键 of Object.keys(环境)) {
    if (/^(RELEVANCE_|LOCI_|POKE_|PORT$)/.test(键)) delete 环境[键];
  }
  Object.assign(环境, {
    PORT: String(端口),
    LOCI_UPSTREAM: 上游地址,
    LOCI_MCP: loci地址,
    LOCI_GATEWAY_DATA: 数据根,
    RELEVANCE_MIN_SCORE: "50",              // 写死，不看机器上的默认
    RELEVANCE_TIMEOUT_MS: String(相关超时毫秒),
    POKE_IDLE_MINUTES: "210",               // 出厂值；戳戳的闸靠预置 state 关着
    围栏白名单端口: 白名单端口.join(","),
    围栏账本: 账本路径,
  });
  // RELEVANCE_WEAK 不设 = 弱档关着（默认）；RELEVANCE_STRONG_WORDS 不设 = 用出厂中文词表。
  // 这两条是**故意不设**的：测的就是拆箱默认状态下它到底工不工作。

  const 子 = spawn(process.execPath, ["-r", 围栏文件, 网关入口], {
    env: 环境,
    cwd: path.join(__dirname, ".."),
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });

  let 输出 = "";
  子.stdout.on("data", (d) => { 输出 += d.toString("utf8"); });
  子.stderr.on("data", (d) => { 输出 += d.toString("utf8"); });

  // 等它真的 listen 了再往下走 —— sleep 猜时间是不老实的做法，
  // 而且端口万一被占，这儿能立刻炸出来（而不是后面一堆莫名其妙的连不上）
  await new Promise((好, 坏) => {
    const 闹钟 = setTimeout(() => 坏(new Error(`网关 10 秒没起来。它说：\n${输出}`)), 10000);
    const 看 = () => {
      if (输出.includes("起来了")) { clearTimeout(闹钟); 好(); }
    };
    子.stdout.on("data", 看);
    子.on("exit", (码) => { clearTimeout(闹钟); 坏(new Error(`网关起来就退了（exit ${码}）。它说：\n${输出}`)); });
    看();
  });

  let 读到哪了 = 输出.length;

  return {
    pid: 子.pid,
    端口,
    地址: `http://127.0.0.1:${端口}`,
    全部输出() { return 输出; },
    /** 上次调用之后网关新说的话 —— 用来看某一次请求它在控制台上留下了什么 */
    输出增量() { const 新 = 输出.slice(读到哪了); 读到哪了 = 输出.length; return 新; },
    /**
     * 同上，但**等**那一行真的到了再返回。
     * 子进程的 stdout 是异步管道：客户端已经拿到回应了，那行日志可能还在路上。
     * 直接读增量会读到空的 —— 那样断言就变成在赌时序。
     */
    async 等增量(判定 = (文) => /→\s*\d{3}\s+\d+ms/.test(文), 超时毫秒 = 5000) {
      const 截止 = Date.now() + 超时毫秒;
      for (;;) {
        const 新 = 输出.slice(读到哪了);
        if (判定(新)) { 读到哪了 = 输出.length; return 新; }
        if (Date.now() > 截止) { 读到哪了 = 输出.length; throw new Error(`等网关那行日志等超时了，只等到：${JSON.stringify(新)}`); }
        await new Promise((好) => setTimeout(好, 20));
      }
    },
    async 停() {
      if (子.exitCode !== null || 子.signalCode !== null) return;
      const 退了 = new Promise((好) => 子.once("exit", 好));
      子.kill();                       // Windows 上就是 TerminateProcess，只对这一个 pid
      const 兜底 = setTimeout(() => { try { 子.kill("SIGKILL"); } catch { /* 已经没了 */ } }, 3000);
      await 退了;
      clearTimeout(兜底);
    },
  };
}

module.exports = { 起网关 };
