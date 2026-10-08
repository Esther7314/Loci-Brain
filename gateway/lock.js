// ============================================================
// gateway/lock.js — one gateway per data directory
//
// Two gateways on one LOCI_GATEWAY_DATA would both keep the same ledger and both run the
// heartbeat: two wakes for every one, twice the money, two of him. So the first thing a
// gateway does with its data directory is take <LOCI_GATEWAY_DATA>/gateway.lock, and a
// gateway that cannot take it does not start.
//
// How, with nothing but Node's fs (the same on Windows and POSIX):
//   · take = create the file exclusively (open with "wx": the OS refuses if it exists,
//     so of two processes racing for it exactly one wins) and write this pid into it.
//   · the file is there → read the pid in it and ask the OS whether that process is
//     alive: process.kill(pid, 0) sends nothing, it only checks (on Windows Node opens
//     the process to check). Alive, or alive but not ours to signal (EPERM) → refuse.
//     Gone (ESRCH) → the lock is stale (a gateway that crashed, or was killed without a
//     chance to clean up) and is taken over.
//   · a lock with no pid in it yet is a gateway in the middle of writing it, if the file
//     is younger than FRESH_MS → refuse; older → stale.
//   · taking over is a rename to a name of our own, then a check that what we moved is
//     the stale lock we judged: if another gateway replaced it in between, its lock is put
//     back and we look again, so two gateways taking over the same stale lock cannot
//     both end up running.
//   · release = delete the file on exit, only while it still holds our pid. SIGINT and
//     SIGTERM are turned into a normal exit so that runs; a process killed outright
//     (TerminateProcess on Windows, SIGKILL) leaves the file behind with a dead pid, and
//     the next start takes it over.
//
// A pid the OS has since given to some unrelated process reads as alive, and the gateway
// refuses: the refusal names the lock file, so the owner can delete it by hand.
// ============================================================

const fs = require("fs");
const os = require("os");
const path = require("path");

const LOCK_NAME = "gateway.lock";
const FRESH_MS = 10 * 1000;
const ATTEMPTS = 5;

/** Is the process with this pid alive? (process.kill(pid, 0) checks without signalling.) */
function is_alive(pid) {
  try { process.kill(pid, 0); return true; }
  catch (err) { return err.code === "EPERM"; }
}

/** What is in the lock file now: { raw, pid (null when none is readable), fresh } or null (no file). */
function read_holder(file, now) {
  let raw, stat;
  try { raw = fs.readFileSync(file, "utf8"); stat = fs.statSync(file); }
  catch (err) { if (err.code === "ENOENT") return null; throw err; }
  const n = Number.parseInt(raw.trim(), 10);
  const pid = Number.isInteger(n) && n > 0 ? n : null;
  return { raw, pid, fresh: now - stat.mtimeMs < FRESH_MS };
}

function create_exclusive(file, content) {
  const fd = fs.openSync(file, "wx");
  try { fs.writeSync(fd, content); } finally { fs.closeSync(fd); }
}

/** Move a stale lock out of the way. true = it was the stale one and is gone. */
function take_aside(file, stale_raw, pid) {
  const aside = `${file}.stale-${pid}`;
  try { fs.renameSync(file, aside); }
  catch (err) { if (err.code === "ENOENT") return false; throw err; }
  const moved = fs.readFileSync(aside, "utf8");
  if (moved !== stale_raw) {
    // Another gateway took the stale lock over between our read and our rename: this is
    // its live lock. Put it back (exclusively: never over a lock someone made since).
    try { create_exclusive(file, moved); } catch { /* someone holds it again: we look again */ }
  }
  fs.rmSync(aside, { force: true });
  return moved === stale_raw;
}

/**
 * Take <dir>/gateway.lock for this process.
 * @param dir    LOCI_GATEWAY_DATA (created if missing)
 * @param pid    whose lock this is (this process)
 * @param alive  pid → boolean (is_alive; replaceable for tests)
 * @param now    () → ms
 * @returns { ok: true, file, took_over: pid | null } | { ok: false, file, pid: pid | null }
 */
function acquire_lock(dir, { pid = process.pid, alive = is_alive, now = Date.now } = {}) {
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, LOCK_NAME);
  let took_over = null;
  let holder = null;
  for (let attempt = 0; attempt < ATTEMPTS; attempt++) {
    try {
      create_exclusive(file, `${pid}${os.EOL}`);
      return { ok: true, file, took_over };
    } catch (err) {
      if (err.code !== "EEXIST") throw err;
    }
    holder = read_holder(file, now());
    if (holder === null) continue;                       // released in between: try again
    const held = holder.pid === null ? holder.fresh : holder.pid !== pid && alive(holder.pid);
    if (held) return { ok: false, file, pid: holder.pid };
    if (take_aside(file, holder.raw, pid) && holder.pid !== pid) took_over = holder.pid;
  }
  return { ok: false, file, pid: holder ? holder.pid : null };
}

/** The line a gateway that could not take the lock prints before it exits. */
function refusal_message(dir, got) {
  const who = got.pid ? `pid ${got.pid}` : "a gateway that is still starting";
  return [
    `Another gateway (${who}) is already running on LOCI_GATEWAY_DATA=${dir}.`,
    "Two gateways on one data directory would wake him twice and pay twice: this one does not start.",
    `Stop that one, or give this one a LOCI_GATEWAY_DATA of its own. If ${got.pid ? `pid ${got.pid}` : "no gateway"} is not a gateway, delete ${got.file}.`,
  ].join("\n");
}

/** Delete the lock on exit while it still holds our pid; SIGINT / SIGTERM exit normally so this runs. */
function release_on_exit(file, pid = process.pid) {
  const release = () => {
    try {
      const holder = read_holder(file, Date.now());
      if (holder && holder.pid === pid) fs.rmSync(file, { force: true });
    } catch { /* exiting anyway: a lock left behind is taken over as stale */ }
  };
  process.once("exit", release);
  for (const signal of ["SIGINT", "SIGTERM"]) {
    process.once(signal, () => process.exit(128 + (os.constants.signals[signal] || 0)));
  }
  return release;
}

module.exports = { acquire_lock, refusal_message, release_on_exit, is_alive, LOCK_NAME, FRESH_MS };
