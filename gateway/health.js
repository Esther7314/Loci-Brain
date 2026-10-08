// ============================================================
// gateway/health.js — GET /health, the read-only "is it actually working?" endpoint
//
// Mounted by server.js ahead of everything else. It reads the log the relay already
// writes and computes a verdict; it never writes, and it never goes out to the network.
// The `present` field is the present layer's own answer (present/health_section.js).
// ============================================================

// ════════════════════════════════════════════════════════════════
// "Is it actually working?" — read-only, inferred from the log it already writes
// ════════════════════════════════════════════════════════════════
// 🔴 Added because of this very gateway: **the timeout was set to 5 seconds, so from
//    the day it shipped it never once did its job, and it stayed that way for days
//    with nobody noticing.** It did not crash, it did not raise, and the console gave
//    nothing away — `notes.push("提醒无")` prints exactly the same for a timeout, a
//    500, a refused connection, and "this sentence was never meant to trigger a
//    lookup" alike.
// 📌 The test: **do not ask "is the process up", ask "when did it last actually get
//    something done".** The first only proves it is standing there; the second is the
//    evidence that it is working.
// ⚠️ Deliberately **not a second set of books**: `memory-actions.jsonl` already writes
//    injected / error / triggered down one line at a time. A second ledger is one more
//    place where the books themselves can rot without anyone knowing — and that is the
//    exact disease this section exists to treat.
const WINDOW_SIZE = 200;         // how many recent **relevance checks** to look at, not log lines
const MAX_READ_BYTES = 4 * 1024 * 1024;

function read_recent_records(log_path, n = WINDOW_SIZE) {
  const fs = require("fs");
  try {
    if (!fs.existsSync(log_path)) return null;   // null = no file (kept apart from "file exists, no records")
    // 🔴 **Actually read only the tail.** The first version's comment said "reads
    //    only the tail" while the code readFileSync'd the entire file into memory and
    //    then sliced off the last 200 lines — at a few hundred megabytes of log it is
    //    just as slow and just as hungry. A comment that disagrees with its code is
    //    worse than no comment at all: **the next person will believe it.**
    const size = fs.statSync(log_path).size;
    const start = Math.max(0, size - MAX_READ_BYTES);
    const fd = fs.openSync(log_path, "r");
    const buf = Buffer.alloc(Math.min(size, MAX_READ_BYTES));
    fs.readSync(fd, buf, 0, buf.length, start);
    fs.closeSync(fd);
    let text = buf.toString("utf8");
    if (start > 0) text = text.slice(text.indexOf("\n") + 1);   // the first line is probably cut in half, drop it
    // 🔴 **Filter first, then slice** — not slice first and filter after. The same
    //    jsonl also holds poke / dream records, and one noisy poke would push the
    //    relevance records out of the window; in the extreme the health endpoint says
    //    "no records at all yet" while the lines above it are nothing but records.
    const records = [];
    for (const l of text.trimEnd().split("\n")) {
      let r = null;
      try { r = JSON.parse(l); } catch { continue; }
      if (r && r.action === "relevance_reminder_observed") records.push(r);
    }
    return records.slice(-n);
  } catch { return []; }
}

function build_health({ log_path, min_score, present_health = null }) {
  // 🔴 **One shape, log or no log.** The first version returned early on the "no
  //    file" branch with a short object that was missing the count fields — the reader
  //    then had to cope with two shapes, and the entire point of this endpoint is that
  //    you understand it at a glance. The distinction is still made (日志档存在: false,
  //    plus 结论 spelling it out), but not one field is dropped.
  const read_records = read_recent_records(log_path);
  const log_exists = read_records !== null;
  const records = read_records || [];
  const seconds_ago = (t) => (t ? Math.round((Date.now() - new Date(t).getTime()) / 1000) : null);
  const number_or_raw = (v) => (v === undefined || v === null || v === "" ? "（没设，用默认）"
                            : (Number.isFinite(Number(v)) ? Number(v) : `⚠️ 这不是个数：${JSON.stringify(v)}`));

  const triggered_records = records.filter((r) => r.triggered);
  const injected_records = records.filter((r) => r.injected);
  const error_records = records.filter((r) => r.error);
  const last_injected = [...records].reverse().find((r) => r.injected) || null;
  const last_error = [...records].reverse().find((r) => r.error) || null;

  // 🔴 **How many failures came AFTER the last success.** The first version asked
  //    "was there ever a success in the window", and that answer lies: the window is
  //    the last 200 records, not the last stretch of time, so **a single success from
  //    yesterday sits in the window forever, covering up today's total outage**.
  //    Measured it: 2 healthy rounds followed by 4 straight failures, and it still
  //    said "working" — precisely the moment it most needed to speak up.
  //    Turn it around, too: the original "5 second timeout" bug would have stayed
  //    invisible here as long as anything at all had succeeded earlier.
  //    **A monitor that lies is worse than no monitor.**
  const last_success_index = records.map((r) => !!r.injected).lastIndexOf(true);
  const errors_since_success = records.slice(last_success_index + 1).filter((r) => r.error).length;

  let verdict;
  if (!log_exists) {
    // "No file" and "file exists but has no records yet" are two different things.
    // The first usually means LOCI_GATEWAY_DATA points somewhere else, so the log the
    // health endpoint reads is not the log the gateway writes — and then it will say
    // "no records yet" forever while the gateway is working perfectly.
    // **These two cases have to stay distinguishable.**
    verdict = "no records at all yet — the log file does not exist "
      + "(just started? or is LOCI_GATEWAY_DATA pointing somewhere else?)";
  } else if (records.length === 0) {
    verdict = "no records at all yet — it may have just started, or may never have been called";
  } else if (triggered_records.length === 0) {
    // ⚠️ Not an alarm: if nobody said anything relevant, zero triggers is correct.
    //    But say something about language — the strong-trigger word list ships in
    //    Chinese, so anyone who does not speak Chinese would sit on this one verdict
    //    forever while it reassures them that things are "probably fine".
    //    **That misses an entire class of users.**
    verdict = "not triggered once in these rounds (may be fine: nobody said anything relevant)"
      + (records.length >= 30 ? "   ⚠️ this many rounds with no trigger at all may also mean the strong-trigger word list does not match the language being spoken" : "");
  } else if (errors_since_success >= 3) {
    verdict = `🔴 broken recently: since the last real attach it has failed ${errors_since_success} times in a row`;
  } else if (injected_records.length > 0) {
    verdict = "working";
  } else if (error_records.length > 0 && triggered_records.length >= 2) {
    // This is the shape of that bug: **triggered, errored, never once attached.**
    // ⚠️ Only shout at ≥2: the README itself says the first relevance check after a
    //    Loci restart will very likely time out, and going red on that cold start
    //    means crying wolf on every single restart.
    verdict = `🔴 triggered ${triggered_records.length} times, errored ${error_records.length} times, attached nothing at all — it is quietly doing nothing`;
  } else if (error_records.length === 0) {
    // 🔴 **No alarm here.** The first version shouted 🔴 on this branch, and what it
    //    caught was a perfectly healthy install: fresh setup, nothing relevant in the
    //    library yet — the lookup ran fine, 0 hits, not one error. Ship that and
    //    somebody sees a red light on day one.
    // ⚠️ This verdict has a ceiling, and the ceiling has to be said out loud: "the
    //    library really has nothing" and "the parser has gone blind" (Loci changed its
    //    render layout) **look identical in the log**; nobody can tell them apart.
    //    So list the possibilities and **never conclude "everything is fine"**.
    verdict = `triggered ${triggered_records.length} times, nothing cleared the floor (no errors: the library may genuinely hold nothing / the score floor may be too high / Loci may have changed its render layout)`;
  } else {
    verdict = `triggered ${triggered_records.length} times with nothing attached yet, ${error_records.length} errors — too few rounds to tell, look again later`;
  }

  return {
    verdict,
    rounds_examined: records.length,
    triggered: triggered_records.length,
    attached: injected_records.length,
    errors: error_records.length,
    errors_since_last_attach: errors_since_success,
    last_attach: last_injected ? { seconds_ago: seconds_ago(last_injected.time), hits: (last_injected.event_count || 0) + (last_injected.mind_count || 0) } : null,
    last_error: last_error ? { seconds_ago: seconds_ago(last_error.time), what: String(last_error.error).slice(0, 200) } : null,
    // ⚠️ Do not copy auto_attach.js's default (12000) — a copied constant is one more
    //    constant that can drift. "Not set" is itself information worth seeing, and a
    //    typo is reported as the typo it is: catching a misconfiguration is exactly
    //    what this endpoint is for.
    timeout_setting: number_or_raw(process.env.RELEVANCE_TIMEOUT_MS),
    score_floor: number_or_raw(min_score),
    log_path,
    log_exists,
    // The present layer answers the same question for its own parts (present/health_section.js).
    // It is computed apart, so a present failure costs only this one field.
    present: present_section(present_health),
  };
}

function present_section(present_health) {
  if (!present_health) return null;
  try { return present_health(); }
  catch (err) { return { error: `the present section could not compute itself: ${err?.message || err}` }; }
}


// The route path is `/health`, matched exactly by the mount table in server.js.
// `opts.present_health`, when given, is a function returning the present section.
// 🔴 This route **was originally spelled `/健康`, and nobody could reach it** — the
//    client sends the percent-escaped `/%E5%81%A5%E5%BA%B7` while the comparison
//    here is byte-for-byte, so it never matched.
//    The consequence is worse than "no response": **the health probe was treated as
//    an ordinary request and forwarded to the upstream model.** An endpoint whose
//    only job is to say whether it is working did not work, and shipped the probe
//    off upstream on its way out.
//    ⚠️ The fix is not a decodeURIComponent here — that only hides the problem.
//       **A URL should not contain non-ASCII in the first place.** Renaming it
//       `/health` removes the problem at the root.
function handle_health(req, res, opts) {
  req.resume();   // a GET has no body, but do not leave unread bytes on a keep-alive connection to derail the next request
  let payload = "{}";
  try { payload = JSON.stringify(build_health(opts), null, 2); }
  catch (err) { payload = JSON.stringify({ verdict: "the health endpoint could not compute itself", error: String(err?.message || err) }); }
  const buf = Buffer.from(payload, "utf8");
  res.writeHead(200, { "Content-Type": "application/json; charset=utf-8", "Content-Length": buf.length });
  res.end(buf);
}

module.exports = { handle_health, build_health };
