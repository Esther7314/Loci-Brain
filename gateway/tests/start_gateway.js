// ============================================================
// gateway/tests/start_gateway.js — run gateway/server.js as a **black box** in a child
// process
//
// Why a child process instead of requiring it in:
//   a whole pile of server.js's configuration (port, upstream, Loci address, where data
//   lands) is read out of env **at module load time**. Require it in and none of that
//   can be changed afterwards, which means there is no way to test "the gateway really
//   did route the request here, following its configuration". A black-box process is a
//   test of the thing that actually runs.
//
// This file also handles two life-or-death details:
//   ① The environment **inherits nothing** matching RELEVANCE_* / LOCI_* — if the
//      machine happens to have RELEVANCE_WEAK=1 set, or LOCI_MCP pointing at the real
//      18002, the tests turn into Schrödinger's green and may go knocking on the real
//      memory library. Every one of them is deleted here and set again explicitly.
//   ② The network fence is attached at spawn time (-r), with an allowlist of exactly the
//      two fake ports.
//
// Teardown: kill **only the pid it spawned itself**, never taskkill anything else.
// ============================================================

const { spawn } = require("node:child_process");
const path = require("node:path");

// Defaults to the server.js in the repository.
// `LOCI_GATEWAY_ENTRY` is the hatch for "try it with a modified copy" — for instance to
// see whether those tests would pass once this bug is fixed, without touching the file
// in the repository:
//   LOCI_GATEWAY_ENTRY=/somewhere/server.js  node --test "gateway/tests/*.test.js"
const GATEWAY_ENTRY = process.env.LOCI_GATEWAY_ENTRY || path.join(__dirname, "..", "server.js");
const FENCE_FILE = path.join(__dirname, "network_fence.js");

/**
 * @param 端口          where the gateway itself listens (a 19xxx picked by the caller)
 * @param 上游地址      the fake upstream, of the form http://127.0.0.1:19xxx/v1
 * @param loci地址      the fake Loci, of the form http://127.0.0.1:19xxx/mcp
 * @param 数据根        where state/logs land (the test's own temp directory — do not
 *                      touch gateway/data)
 * @param 相关超时毫秒  RELEVANCE_TIMEOUT_MS — the number that bug lived on
 * @param 白名单端口    ports the fence lets through (only the fake upstream + fake Loci)
 * @param 账本路径      where the fence's ledger lands
 */
async function start_gateway({ 端口: port, 上游地址: upstream_url, loci地址: loci_url, 数据根: data_root,
                               相关超时毫秒: relevance_timeout_ms, 白名单端口: allowed_ports, 账本路径: ledger_path }) {
  const env = { ...process.env };
  // 🔴 Wipe everything that could sway the outcome first, then set it explicitly — the machine's env does not get a vote
  for (const key of Object.keys(env)) {
    if (/^(RELEVANCE_|LOCI_|POKE_|PORT$)/.test(key)) delete env[key];
  }
  Object.assign(env, {
    PORT: String(port),
    LOCI_UPSTREAM: upstream_url,
    LOCI_MCP: loci_url,
    LOCI_GATEWAY_DATA: data_root,
    RELEVANCE_MIN_SCORE: "50",              // pinned, never the machine's default
    RELEVANCE_TIMEOUT_MS: String(relevance_timeout_ms),
    POKE_IDLE_MINUTES: "210",               // factory value; the poke gate is held shut by the preset state file
    围栏白名单端口: allowed_ports.join(","),
    围栏账本: ledger_path,
  });
  // RELEVANCE_WEAK unset = the weak trigger is off (the default); RELEVANCE_STRONG_WORDS
  // unset = the factory Chinese word list. Both are **deliberately left unset**: what is
  // under test is whether it works at all straight out of the box.

  const child = spawn(process.execPath, ["-r", FENCE_FILE, GATEWAY_ENTRY], {
    env: env,
    cwd: path.join(__dirname, ".."),
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });

  let output = "";
  child.stdout.on("data", (d) => { output += d.toString("utf8"); });
  child.stderr.on("data", (d) => { output += d.toString("utf8"); });

  // Wait until it has really listened before going on — sleeping for a guessed duration
  // is a dishonest way to do this, and if the port turns out to be taken, this blows up
  // right here instead of as a pile of baffling connection failures further down.
  let bound_port = port;
  await new Promise((resolve, reject) => {
    // filled in from the banner below; falls back to what we asked for
    const timer = setTimeout(() => reject(new Error(`网关 10 秒没起来。它说：\n${output}`)), 10000);
    const check_ready = () => {
      // Matches the banner's first line in server.js. These two are coupled: change the
      // banner without changing this and every gateway test waits until it times out,
      // which reads as "the gateway is broken" rather than "the string moved".
      // The banner carries the port it really bound — the only way to learn it when we
      // asked for 0. Same coupling as before (change the banner, change this), one step
      // deeper: we now read a number out of it instead of just spotting the line.
      const marker = "[gateway] up on http://127.0.0.1:";
      const at = output.indexOf(marker);
      if (at >= 0) {
        bound_port = parseInt(output.slice(at + marker.length), 10);
        clearTimeout(timer);
        resolve();
      }
    };
    child.stdout.on("data", check_ready);
    child.on("exit", (code) => { clearTimeout(timer); reject(new Error(`网关起来就退了（exit ${code}）。它说：\n${output}`)); });
    check_ready();
  });

  let read_offset = output.length;

  return {
    pid: child.pid,
    端口: bound_port,
    地址: `http://127.0.0.1:${bound_port}`,
    全部输出() { return output; },
    /** Whatever the gateway has said since the last call — used to see what one request left on the console */
    输出增量() { const fresh = output.slice(read_offset); read_offset = output.length; return fresh; },
    /**
     * The same, but **waits** until that line has really arrived before returning.
     * A child process's stdout is an async pipe: the client already has its response
     * while that log line may still be in flight. Reading the delta directly would read
     * an empty string — and then the assertion is just a bet on timing.
     */
    async 等增量(predicate = (text) => /→\s*\d{3}\s+\d+ms/.test(text), timeout_ms = 5000) {
      const deadline = Date.now() + timeout_ms;
      for (;;) {
        const fresh = output.slice(read_offset);
        if (predicate(fresh)) { read_offset = output.length; return fresh; }
        if (Date.now() > deadline) { read_offset = output.length; throw new Error(`等网关那行日志等超时了，只等到：${JSON.stringify(fresh)}`); }
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
    },
    async 停() {
      if (child.exitCode !== null || child.signalCode !== null) return;
      const exited = new Promise((resolve) => child.once("exit", resolve));
      child.kill();                       // on Windows this is TerminateProcess, and only for this one pid
      const fallback_timer = setTimeout(() => { try { child.kill("SIGKILL"); } catch { /* already gone */ } }, 3000);
      await exited;
      clearTimeout(fallback_timer);
    },
  };
}

module.exports = { start_gateway };
