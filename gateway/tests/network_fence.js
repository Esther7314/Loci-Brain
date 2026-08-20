// ============================================================
// gateway/tests/network_fence.js — stop every outbound connection, and write each down
//
// Why something like this is needed: the red line here is
//   "every outbound network path must be confirmed, one by one, to land in the fake
//    environment".
// Green assertions **do not count** — if the gateway quietly connected to the real
// 18002, the assertions could still go green (that side happens to return a parseable
// result too) while the real memory library has already been knocked on. The lesson was
// paid for in blood: one run burned through to the real gateway.
//
// So the interception happens at the very bottom layer (net.Socket.prototype.connect,
// where Node's own fetch/undici ends up too):
//   · a port on the allowlist — let it through, but write it down
//   · anything else           — **physically cannot get out** (the socket is destroyed
//                               on the spot), and write that down too
// Reconcile against the ledger afterwards: it should hold nothing but the two 19xxx
// ports of the fake upstream and the fake Loci.
//
// It does not throw when it blocks: a throw surfaces from undici's internals as an
// uncaught exception and takes the whole gateway down, and then the process is gone
// before the ledger is finished. destroy is clean — the layer above sees an ordinary
// "cannot connect", while the ledger records in black and white where it wanted to go.
//
// Two ways to use it:
//   child process: node -r <this file> gateway/server.js, allowlist from env 围栏白名单端口
//   this process:  require it, then allow(port…)
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
  // the child process's ledger has to be readable from the test process, so it also lands in a file, one entry per line
  if (ledger_path) { try { fs.appendFileSync(ledger_path, `${JSON.stringify(entry)}\n`); } catch { /* a failed write must never affect the thing under test */ } }
}

// undici calls socket.connect([options, cb]) — the array form — so reading args[0].port
// directly gives undefined, and every connection would then be misjudged as "a port we
// do not recognise". Unwrap it first.
function parse_destination(args) {
  let head = args[0];
  if (Array.isArray(head)) head = head[0];
  // no host? fall back to path (unix sockets and named pipes have a path and no host)
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
