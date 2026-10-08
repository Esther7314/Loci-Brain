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
// Nothing in this section reads the private thread ledger's contents.
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
    report: not_built(),
    wake: present.wake.health(),
    compress: not_built(),
    push: not_built(),
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
