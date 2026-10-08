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
// What it reads: the `cue_observed` line present/cue.js writes for every time a turn asks
// Loci for its cards — asked, answered or failed (`error`), and whether any card came back.
// ⚠️ Deliberately **not a second set of books**: `memory-actions.jsonl` already writes
//    that down one line at a time. A second ledger is one more
//    place where the books themselves can rot without anyone knowing — and that is the
//    exact disease this section exists to treat.
const WINDOW_SIZE = 200;         // how many recent **cue asks** to look at, not log lines
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
    //    jsonl also holds poke / dream / delivered records, and one noisy poke would push
    //    the cue records out of the window; in the extreme the health endpoint says
    //    "no records at all yet" while the lines above it are nothing but records.
    const records = [];
    for (const l of text.trimEnd().split("\n")) {
      let r = null;
      try { r = JSON.parse(l); } catch { continue; }
      if (r && r.action === "cue_observed") records.push(r);
    }
    return records.slice(-n);
  } catch { return []; }
}

function build_health({ log_path, cue_timeout_ms = null, present_health = null }) {
  // 🔴 **One shape, log or no log.** Every field is there on every branch: the reader
  //    copes with one shape, and the "no file" case is still told apart (log_exists:
  //    false, and the verdict spells it out).
  const read_records = read_recent_records(log_path);
  const log_exists = read_records !== null;
  const records = read_records || [];
  const seconds_ago = (t) => (t ? Math.round((Date.now() - new Date(t).getTime()) / 1000) : null);

  const answered_records = records.filter((r) => !r.error);
  const attached_records = records.filter((r) => r.injected);
  const error_records = records.filter((r) => r.error);
  const last_attached = [...records].reverse().find((r) => r.injected) || null;
  const last_error = [...records].reverse().find((r) => r.error) || null;

  // 🔴 **How many failures came AFTER the last answer.** Not "was there ever an answer
  //    in the window": the window is the last 200 asks, not the last stretch of time, so
  //    one answer from yesterday would sit in it forever and cover up today's outage.
  //    Success here is "Loci answered", cards or not — an answer with no card is Loci
  //    working on a turn nothing in the library matched.
  const last_answer_index = records.map((r) => !r.error).lastIndexOf(true);
  const errors_since_answer = records.slice(last_answer_index + 1).filter((r) => r.error).length;

  let verdict;
  if (!log_exists) {
    // "No file" and "file exists but has no records yet" are two different things.
    // The first usually means LOCI_GATEWAY_DATA points somewhere else, so the log the
    // health endpoint reads is not the log the gateway writes — and then it will say
    // "no records yet" forever while the gateway is working perfectly.
    verdict = "no records at all yet — the log file does not exist "
      + "(just started? or is LOCI_GATEWAY_DATA pointing somewhere else?)";
  } else if (records.length === 0) {
    verdict = "no records at all yet — it may have just started, or may never have been called";
  } else if (errors_since_answer >= 3) {
    verdict = `🔴 broken recently: since Loci last answered, asking it for cards has failed ${errors_since_answer} times in a row`;
  } else if (answered_records.length === 0 && error_records.length >= 2) {
    // The shape of a gateway that never once worked: every ask failed, the chat carried
    // on, nothing on screen says so. ⚠️ Only at ≥2: one failure right after Loci starts
    // is a cold start, not a fault.
    verdict = `🔴 asked ${records.length} times, failed ${error_records.length} times, Loci never answered once — it is quietly doing nothing`;
  } else if (attached_records.length > 0) {
    verdict = "working";
  } else if (answered_records.length > 0) {
    // ⚠️ Not an alarm: Loci answered and nothing matched. A fresh library, or lines that
    //    touch nothing it holds, look exactly like this. Say what it is, conclude nothing.
    verdict = `Loci answered ${answered_records.length} times with no card (not a fault: nothing in the library matched these lines)`;
  } else {
    verdict = `asked ${records.length} times, ${error_records.length} failed, no answer yet — too few rounds to tell, look again later`;
  }

  return {
    verdict,
    rounds_examined: records.length,
    asked: records.length,
    answered: answered_records.length,
    attached: attached_records.length,
    errors: error_records.length,
    errors_since_last_answer: errors_since_answer,
    last_attach: last_attached ? { seconds_ago: seconds_ago(last_attached.time), cards: last_attached.cards || 0 } : null,
    last_error: last_error ? { seconds_ago: seconds_ago(last_error.time), what: String(last_error.error).slice(0, 200) } : null,
    // the timeout actually in force (LOCI_CUE_TIMEOUT_MS or the default): a number that
    // shows a misconfiguration at a glance
    cue_timeout_ms,
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
