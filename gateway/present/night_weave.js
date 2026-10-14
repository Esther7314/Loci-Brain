// ============================================================
// gateway/present/night_weave.js — the night's weave: the day report written, then Loci
// dreams
//
// Loci weaves at night, not at the morning's first breath: once the day report is
// written (day_close.js, `last_report.at_ms`) and DELAY_MS has passed, the beat asks
//     POST /api/loci/dream/weave
// once for that report. Loci sweeps what has expired and weaves if its pile is above the
// line and today's weave has not happened (core/_dream.maintain; breath's background beat
// shares the per-day count and runs one at a time with this). The weaver is Loci's own
// dream model: no paid turn of his is spent here.
//
// Delivery does not change: a dream woven tonight is handed whole by the next wake letter
// (wake.js, usually the first after DND, i.e. the morning) or with her next message
// (poke delivery), and her message after that degrades it (/api/loci/dream/wake).
//
// When:
//   · only for the latest report, and only within FRESH_MS of its writing: a gateway
//     started days after its last report does not dream about it now
//   · once per report: asked_for = the report's at_ms, on disk, so a restart does not ask
//     again
//   · Loci unreachable, a timeout, a non-2xx → the next beat tries again, at most TRIES
//     times for one report; then given up, the reason kept
// The weave can take a minute (a model call), so it runs in the background: the beat
// never waits on it, and one runs at a time.
//
// State: <LOCI_GATEWAY_DATA>/state/night-weave.json
//   {asked_for, at, woven, nightmare, verdict, tries, error}
// No dream text passes through here: Loci answers the id, never the words.
// ============================================================

const fs = require("fs");
const path = require("path");

const DELAY_MS = 30 * 60 * 1000;
const FRESH_MS = 12 * 60 * 60 * 1000;
const TRIES = 3;
const TIMEOUT_MS = 10 * 60 * 1000;   // Loci holds the request while its dream model writes

function read_json(file) {
  try { return JSON.parse(fs.readFileSync(file, "utf8")); } catch { return null; }
}

function write_json(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(value), "utf8");
  fs.renameSync(tmp, file);
}

/**
 * @param data_root    LOCI_GATEWAY_DATA
 * @param loci_base    Loci's REST root
 * @param hook_token   LOCI_HOOK_TOKEN, or ""
 * @param last_report  () → {at_ms} | null — day_close.js's last written report
 */
function create_night_weave({ data_root, loci_base, hook_token = "", last_report, clock, log = console.error,
                              delay_ms = DELAY_MS, timeout_ms = TIMEOUT_MS }) {
  const file = path.join(data_root, "state", "night-weave.json");
  let running = null;

  const load = () => {
    const v = read_json(file);
    return v && typeof v === "object" && !Array.isArray(v) ? v : {};
  };

  async function ask() {
    const headers = { Accept: "application/json", "Content-Type": "application/json" };
    if (hook_token) headers["x-loci-hook-token"] = hook_token;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout_ms);
    try {
      const resp = await fetch(`${loci_base}/api/loci/dream/weave`, {
        method: "POST", headers, body: "{}", signal: controller.signal,
      });
      const text = await resp.text();
      if (!resp.ok) return { ok: false, error: `Loci answered HTTP ${resp.status}` };
      let json = null;
      try { json = JSON.parse(text); } catch { return { ok: false, error: "Loci's answer is not JSON" }; }
      return { ok: true, json: json || {} };
    } catch (err) {
      return { ok: false, error: err?.name === "AbortError" ? `no answer within ${Math.round(timeout_ms / 1000)} s`
        : `${err?.cause?.code || err?.message || err}` };
    } finally { clearTimeout(timer); }
  }

  /** Is a weave due now, and for which report? */
  function due(now) {
    const r = last_report();
    const at = Number(r && r.at_ms);
    if (!Number.isFinite(at)) return null;
    if (now < at + delay_ms || now > at + FRESH_MS) return null;
    const st = load();
    if (st.asked_for === at) return null;
    if (st.for_report === at && Number(st.tries) >= TRIES) return null;
    return at;
  }

  async function run(at) {
    const before = load();
    const tries = (before.for_report === at ? Number(before.tries) || 0 : 0) + 1;
    const got = await ask();
    if (got.ok) {
      write_json(file, { asked_for: at, for_report: at, at: clock.now(), woven: got.json.woven ?? null,
                         nightmare: Boolean(got.json.nightmare), verdict: String(got.json.verdict || ""),
                         tries, error: null });
      return;
    }
    const gave_up = tries >= TRIES;
    write_json(file, { ...before, for_report: at, tries, error: got.error, ...(gave_up ? { gave_up_at: clock.now() } : {}) });
    log(`[gateway] night weave: ${got.error}${gave_up ? " — given up for this report" : " — trying again next beat"}`);
  }

  /** The beat: start a due weave in the background; never waits on it. */
  function tick() {
    if (running) return;
    const at = due(clock.now());
    if (at === null) return;
    running = run(at)
      .catch((err) => log(`[gateway] night weave failed: ${err?.message || err}`))
      .finally(() => { running = null; });
  }

  return { tick, status: load, settle: () => running || Promise.resolve() };
}

module.exports = { create_night_weave, DELAY_MS, FRESH_MS, TRIES };
