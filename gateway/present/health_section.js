// ============================================================
// gateway/present/health_section.js — the `present` section of GET /health
//
// Same question as the rest of /health: not "is the process up" but **"when did each
// thing last actually succeed"** — the last day report, wake, compression and push, how
// long ago, how many failures since, how many things he said are waiting for her.
// Parts that are not built yet answer `state: "not_built"` with nulls: a monitor that
// fills a gap with a guess is worse than one that says it does not know.
//
// What is built and measured here:
//   · recording — when the newest line was written to the day store
//   · settings  — whether present.json reads (ok · defaults · invalid · unreadable)
//   · doors     — whether /present/* and /loci/source can be entered at all
//   · wake      — when a wake last ran to an answer, failures since, how many things he
//                 said are held for her (wake.js health())
//   · compress  — when a window was last folded, any way (he did it, a pack, the panel's
//                 button), and how many paid pack failures since (present/index.js
//                 compress_health: times, ways and counts)
//   · report    — when a day report was last written, failures since, whether the latest
//                 one gave up, and when the nightly hand-off to Loci last went through
//                 (day_close.js health(): times, counts and the last error, never the text)
//   · push      — when a Bark push last went through, failures since, retries in flight
//                 (push.js health(); never the key or a text)
// Nothing in this section reads the carry, an overlay or anything else that was said.
// ============================================================

const fs = require("fs");
const path = require("path");

const not_built = (extra = {}) => ({ state: "not_built", last_ok_seconds_ago: null, failures_since_ok: null, ...extra });

function newest_line_at(day_store) {
  let names = [];
  try { names = fs.readdirSync(day_store.dir).filter((n) => /^\d{4}-\d{2}-\d{2}\.jsonl$/.test(n)).sort(); }
  catch { return null; }
  for (let i = names.length - 1; i >= 0; i--) {
    let newest = null;
    for (const line of day_store.read_day(path.basename(names[i], ".jsonl"))) {
      const t = Date.parse(line.at);
      if (Number.isFinite(t) && (newest === null || t > newest.t)) newest = { t, at: line.at };
    }
    if (newest) return newest;
  }
  return null;
}

function compress_section(present, now) {
  if (typeof present.compress_health !== "function") return not_built();
  const c = present.compress_health();
  return {
    state: c.last_ok_at ? "ok" : "never",
    last_ok_at: c.last_ok_at,
    last_ok_seconds_ago: c.last_ok_ms === null ? null : Math.max(0, Math.round((now - c.last_ok_ms) / 1000)),
    last_how: c.last_how,
    failures_since_ok: c.failures_since_ok,
    last_failure_reason: c.last_failure_reason,
    running: c.running,
    waiting_to_retry: c.waiting_to_retry,
  };
}

/**
 * @param present  present/index.js instance
 * @param doors    { token_set, bind, passphrase_required }
 */
function build_present_health({ present, doors }) {
  const now = present.clock.now();
  const newest = newest_line_at(present.day_store);
  const loaded = present.settings.load();
  const door = doors.token_set ? "open" : "closed: LOCI_GATEWAY_TOKEN is not set";
  return {
    recording: newest
      ? { last_line_at: newest.at, seconds_ago: Math.max(0, Math.round((now - newest.t) / 1000)) }
      : { last_line_at: null, seconds_ago: null },
    report: typeof present.report_health === "function" ? present.report_health() : not_built(),
    wake: present.wake.health(),
    compress: compress_section(present, now),
    push: present.push ? present.push.health() : not_built(),
    settings: { state: loaded.state, errors: loaded.errors },
    doors: {
      present_api: door,
      loci_source: door,
      bind: doors.bind,
      passphrase_required: doors.passphrase_required,
    },
  };
}

module.exports = { build_present_health };
