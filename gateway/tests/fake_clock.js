// ============================================================
// gateway/tests/fake_clock.js — a clock the test moves by hand
//
// Two shapes, one idea:
//   · in-process:  create_fake_clock(start) → { now(), set(ms), advance(ms) }, handed
//                  straight to present modules that take a `clock`
//   · child process: create_file_clock(file, start) writes the time into a file and
//                  the gateway is started with LOCI_GATEWAY_TEST_CLOCK=<file>
//                  (present/clock.js reads it on every call). Same three methods.
// Times are epoch milliseconds. Nothing here ever reads the real clock after creation,
// so a test that depends on "two minutes later" does not depend on how fast it ran.
// ============================================================

const fs = require("node:fs");

function create_fake_clock(start) {
  let t = Number(start);
  return {
    now: () => t,
    set(ms) { t = Number(ms); },
    advance(ms) { t += Number(ms); },
    source: "fake clock",
  };
}

function create_file_clock(file, start) {
  let t = Number(start);
  const write = () => fs.writeFileSync(file, String(t), "utf8");
  write();
  return {
    file,
    now: () => t,
    set(ms) { t = Number(ms); write(); },
    advance(ms) { t += Number(ms); write(); },
  };
}

module.exports = { create_fake_clock, create_file_clock };
