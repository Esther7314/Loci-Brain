// ============================================================
// gateway/tests/lock.test.js — one gateway per LOCI_GATEWAY_DATA (gateway.lock)
//
// What is being proved:
//   · black box: a second gateway on the data directory of a running one exits non-zero
//     before it listens, and says which directory and which pid hold it
//   · black box: a lock left behind by a process that is gone is taken over, and the
//     lock then holds the new gateway's pid
//   · lock.js itself: a lock with no pid that is still fresh is held, an old one is stale;
//     a process that exits normally removes its lock, and only its own
// ============================================================

const { test, before, after } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");

const fence = require("./network_fence.js");
const { start_fake_upstream } = require("./fake_upstream.js");
const { start_fake_loci } = require("./fake_loci.js");
const { start_gateway } = require("./start_gateway.js");
const { acquire_lock, refusal_message, is_alive, LOCK_NAME, FRESH_MS } = require("../lock.js");

const root = fs.mkdtempSync(path.join(os.tmpdir(), "loci-lock-"));
const ledger_path = path.join(root, "fence.jsonl");
const gateways = [];
let fake_upstream, fake_loci;

before(async () => {
  fake_upstream = await start_fake_upstream({ 端口: 0 });
  fake_loci = await start_fake_loci({ 端口: 0 });
});

after(async () => {
  for (const g of gateways) await g.停();
  if (fake_upstream) await fake_upstream.关();
  if (fake_loci) await fake_loci.关();
  fs.rmSync(root, { recursive: true, force: true });
});

function data_dir(name) {
  const dir = path.join(root, name);
  fs.mkdirSync(path.join(dir, "state"), { recursive: true });
  fs.writeFileSync(path.join(dir, "state", "poke-window.json"),
    JSON.stringify({ lastUserMessageTime: new Date().toISOString(), wakePending: false }));
  return dir;
}

async function boot(dir) {
  const g = await start_gateway({
    端口: 0, 上游地址: fake_upstream.地址, loci地址: fake_loci.地址, 数据根: dir, 相关超时毫秒: 1200,
    白名单端口: [fake_upstream.端口, fake_loci.端口], 账本路径: ledger_path,
  });
  gateways.push(g);
  fence.allow(g.端口);
  return g;
}

/** A pid that was alive a moment ago and is not any more. */
async function dead_pid() {
  const child = spawn(process.execPath, ["-e", ""], { stdio: "ignore", windowsHide: true });
  const pid = child.pid;
  await new Promise((resolve) => child.once("exit", resolve));
  assert.ok(!is_alive(pid), `pid ${pid} should be gone`);
  return pid;
}

const lock_pid = (dir) => Number.parseInt(fs.readFileSync(path.join(dir, LOCK_NAME), "utf8"), 10);

test("black box: a second gateway on the same data directory refuses to start and names the dir and the pid", { timeout: 30000 }, async () => {
  const dir = data_dir("shared");
  const first = await boot(dir);
  assert.strictEqual(lock_pid(dir), first.pid, "the running gateway's pid is in the lock");

  await assert.rejects(boot(dir), (err) => {
    assert.match(err.message, /exit [1-9]\d*/, "exits non-zero");
    assert.ok(err.message.includes(`LOCI_GATEWAY_DATA=${dir}`), `names the data dir:\n${err.message}`);
    assert.ok(err.message.includes(`pid ${first.pid}`), `names the other pid:\n${err.message}`);
    assert.ok(!err.message.includes("[gateway] up on"), "it never listened");
    return true;
  });
  assert.strictEqual(lock_pid(dir), first.pid, "the refused gateway did not touch the first one's lock");

  // the first one is untouched and still answers
  const health = await fetch(`${first.地址}/health`);
  assert.strictEqual(health.status, 200);
});

test("black box: a stale lock (its pid is gone) is taken over", { timeout: 30000 }, async () => {
  const dir = data_dir("stale");
  const gone = await dead_pid();
  fs.writeFileSync(path.join(dir, LOCK_NAME), `${gone}\n`);

  const g = await boot(dir);
  assert.strictEqual(lock_pid(dir), g.pid, "the lock now holds the new gateway's pid");
  // the banner says so (the lines after the first arrive on their own time)
  await g.等增量((text) => new RegExp(`taken over from pid ${gone}\\b`).test(text));
});

test("lock.js: a lock with no pid yet is held while fresh, stale once old", () => {
  const dir = path.join(root, "empty");
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, LOCK_NAME);
  fs.writeFileSync(file, "");
  const t0 = fs.statSync(file).mtimeMs;

  const held = acquire_lock(dir, { pid: 4242, now: () => t0 + 1000 });
  assert.deepStrictEqual(held, { ok: false, file, pid: null });
  assert.match(refusal_message(dir, held), /still starting/);

  const taken = acquire_lock(dir, { pid: 4242, now: () => t0 + FRESH_MS + 1000 });
  assert.deepStrictEqual(taken, { ok: true, file, took_over: null });
  assert.strictEqual(lock_pid(dir), 4242);
});

test("lock.js: a live holder is refused; the pid check is what decides", () => {
  const dir = path.join(root, "alive");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, LOCK_NAME), "777\n");
  assert.deepStrictEqual(acquire_lock(dir, { pid: 4242, alive: () => true }),
    { ok: false, file: path.join(dir, LOCK_NAME), pid: 777 });
  assert.deepStrictEqual(acquire_lock(dir, { pid: 4242, alive: () => false }),
    { ok: true, file: path.join(dir, LOCK_NAME), took_over: 777 });
  assert.deepStrictEqual(fs.readdirSync(dir), [LOCK_NAME], "nothing left aside");
});

test("lock.js: a process that exits normally removes its lock, and leaves someone else's alone", () => {
  const lock_js = path.join(__dirname, "..", "lock.js");
  const run = (dir, script) => spawnSync(process.execPath, ["-e", script], {
    env: { ...process.env, LOCK_JS: lock_js, LOCK_DIR: dir }, windowsHide: true, encoding: "utf8" });

  const own = path.join(root, "clean-exit");
  const r1 = run(own, "const l = require(process.env.LOCK_JS); const got = l.acquire_lock(process.env.LOCK_DIR);"
    + " if (!got.ok) process.exit(3); l.release_on_exit(got.file);");
  assert.strictEqual(r1.status, 0, r1.stderr);
  assert.ok(!fs.existsSync(path.join(own, LOCK_NAME)), "the lock is gone after a clean exit");

  const other = path.join(root, "not-mine");
  const r2 = run(other, "const l = require(process.env.LOCK_JS); const got = l.acquire_lock(process.env.LOCK_DIR);"
    + " if (!got.ok) process.exit(3); l.release_on_exit(got.file);"
    + " require('fs').writeFileSync(got.file, '999999\\n');");
  assert.strictEqual(r2.status, 0, r2.stderr);
  assert.strictEqual(lock_pid(other), 999999, "a lock that no longer holds our pid is not ours to delete");
});
