// ============================================================
// gateway/tests/network_fence.js —— 拦住每一条「出门」的连接，逐条记账
//
// 为什么要这么一道东西：这一单的红线是
//   「每一条出门的网络路径都要逐条确认打到假环境」。
// 光看断言绿了**不算数** —— 网关要是偷偷连了真的 18002，
// 断言照样可能绿（那边恰好也返回了一份能解析的结果），
// 而她的记忆库已经被敲过了。血的教训是 8-15 那次烧到真 gateway。
//
// 所以在最底下那一层（net.Socket.prototype.connect，Node 自己的 fetch/undici
// 最终也走这儿）拦一道：
//   · 白名单里的端口 —— 放行，但记一笔
//   · 白名单外的     —— **物理上连不出去**（socket 当场 destroy），也记一笔
// 跑完拿账本对：账上只该有假上游和假 Loci 那两个 19xxx 的端口。
//
// 拦的时候不 throw：throw 会从 undici 内部抛成未捕获异常、把网关整个搞崩，
// 那样账本还没写完进程就没了。destroy 掉是干净的 —— 上层看见的是一次
// 普通的「连不上」，而账本上白纸黑字记着它想去哪儿。
//
// 用法两种：
//   子进程： node -r <这个文件> gateway/server.js，白名单走 env `围栏白名单端口`
//   本进程： require 进来之后 allow(端口...)
// ============================================================

const net = require("node:net");
const fs = require("node:fs");

const allowlist = new Set(
  String(process.env.围栏白名单端口 || "")
    .split(",").map((s) => s.trim()).filter(Boolean).map(Number),
);
const ledger_path = process.env.围栏账本 || "";
const ledger = [];

function allow(...ports) { for (const p of ports) allowlist.add(Number(p)); }

function record_attempt(entry) {
  ledger.push(entry);
  // 子进程里的账本要能被测试进程读到，所以还落一份文件（一行一笔）
  if (ledger_path) { try { fs.appendFileSync(ledger_path, `${JSON.stringify(entry)}\n`); } catch { /* 记账失败不许影响被测的东西 */ } }
}

// undici 调的是 socket.connect([options, cb]) 这种数组形态，直接读 参[0].port
// 会拿到 undefined —— 那样所有连接都会被误判成「不认识的端口」。先拆。
function parse_destination(args) {
  let head = args[0];
  if (Array.isArray(head)) head = head[0];
  // host 拿不到就退到 path（unix socket / 具名管道那种，没有 host 只有 path）
  if (head && typeof head === "object") return { 端口: Number(head.port), 主机: String(head.host ?? head.path ?? "") };
  return { 端口: Number(args[0]), 主机: String(args[1] ?? "") };
}

const orig_connect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function (...args) {
  const { 端口: port, 主机: host } = parse_destination(args);
  const allowed = allowlist.has(port);
  record_attempt({ 时间: new Date().toISOString(), pid: process.pid, 端口: port, 主机: host, 放行: allowed });
  if (!allowed) {
    const err = new Error(`[网络围栏] 拦下一条不该出门的连接：${host}:${port}（白名单只有 ${[...allowlist].join(",") || "空"}）`);
    process.nextTick(() => this.destroy(err));
    return this;
  }
  return orig_connect.apply(this, args);
};

module.exports = { allow, 账本: ledger, 白名单: allowlist };
