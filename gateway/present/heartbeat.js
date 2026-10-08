// ============================================================
// gateway/present/heartbeat.js — the gateway's one and only timer
//
// Everything the present layer does on its own (the nightly report, wake, push retries,
// tidying up) hangs off this one beat instead of starting a timer of its own: a process
// with a timer per feature ends up with features waking each other at random.
//
// · One tick every 60 seconds. Tasks run **in order, one at a time**; a tick that comes
//   round while the previous one is still running is skipped, not queued.
// · A task that throws is logged and the next task still runs.
// · `unref`: the beat never keeps the process alive on its own.
// · `tick()` is exported so tests can drive the beat with a fake clock instead of
//   waiting on a real interval.
// ============================================================

const DEFAULT_INTERVAL_MS = 60 * 1000;

/**
 * @param tasks        [{ name, run: async () => void }], run in this order every tick
 * @param interval_ms  how often the beat fires
 * @param log          where a failed task is reported (console.error by default)
 */
function create_heartbeat({ tasks = [], interval_ms = DEFAULT_INTERVAL_MS, log = console.error } = {}) {
  let timer = null;
  let running = false;

  async function tick() {
    if (running) return { skipped: true };
    running = true;
    try {
      for (const task of tasks) {
        try { await task.run(); }
        catch (err) { log(`[gateway] heartbeat task ${task.name} failed: ${err?.message || err}`); }
      }
    } finally { running = false; }
    return { skipped: false };
  }

  return {
    start() {
      if (timer || tasks.length === 0) return;   // nothing to run: no timer at all
      timer = setInterval(() => { tick(); }, interval_ms);
      timer.unref();
    },
    stop() { if (timer) clearInterval(timer); timer = null; },
    tick,
  };
}

module.exports = { create_heartbeat, DEFAULT_INTERVAL_MS };
