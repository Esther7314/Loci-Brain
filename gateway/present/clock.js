// ============================================================
// gateway/present/clock.js — what time it is, and what day it is where the owner lives
//
// Every present module asks this file for the time instead of calling Date.now() itself,
// so that one switch can move the whole layer's clock:
//   · normally: the system clock
//   · LOCI_GATEWAY_TEST_CLOCK=<file>: the time is whatever epoch-milliseconds number
//     that file holds, read fresh on every call. It exists for the black-box tests,
//     which run the gateway as a child process and have no other way to move its clock
//     (resend windows, day boundaries, and later the night report and wake hours).
//     A missing or unreadable file throws: a test clock that silently fell back to the
//     real time would make a time-dependent test pass or fail by the wall clock.
//
// The day a line belongs to is the owner's local day, not UTC: a day file is "what was
// said on the 7th" as the owner lived it. LOCI_TZ names the zone (IANA, e.g.
// Asia/Shanghai); unset means the zone of the machine the gateway runs on.
// ============================================================

const fs = require("fs");

function create_clock(env = process.env) {
  const file = env.LOCI_GATEWAY_TEST_CLOCK;
  if (!file) return { now: () => Date.now(), source: "system clock" };
  return {
    now() {
      const ms = Number(fs.readFileSync(file, "utf8").trim());
      if (!Number.isFinite(ms)) throw new Error(`LOCI_GATEWAY_TEST_CLOCK file does not hold a number: ${file}`);
      return ms;
    },
    source: `test clock (${file})`,
  };
}

/** Returns the zone to use: LOCI_TZ when it is a zone this Node knows, else the machine's. */
function resolve_zone(env = process.env) {
  const asked = String(env.LOCI_TZ || "").trim();
  const machine = Intl.DateTimeFormat().resolvedOptions().timeZone;
  if (!asked) return { zone: machine, note: null };
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: asked });
    return { zone: asked, note: null };
  } catch {
    return { zone: machine, note: `LOCI_TZ=${asked} is not a time zone this Node knows; using ${machine}` };
  }
}

const formatters = new Map();
function formatter_for(zone) {
  if (!formatters.has(zone)) {
    formatters.set(zone, new Intl.DateTimeFormat("en-US", {
      timeZone: zone, hourCycle: "h23",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    }));
  }
  return formatters.get(zone);
}

const pad = (n, w = 2) => String(n).padStart(w, "0");

/**
 * The owner's local calendar for one instant.
 * @returns { day: "2026-10-07", compact: "20261007", iso: "2026-10-07T20:41:05+08:00" }
 */
function local_stamp(ms, zone) {
  const parts = {};
  for (const p of formatter_for(zone).formatToParts(new Date(ms))) parts[p.type] = p.value;
  const y = Number(parts.year), mo = Number(parts.month), d = Number(parts.day);
  const h = Number(parts.hour), mi = Number(parts.minute), s = Number(parts.second);
  // The offset is whatever makes the local wall time and the instant agree.
  const whole_second = Math.floor(ms / 1000) * 1000;
  const offset_min = Math.round((Date.UTC(y, mo - 1, d, h, mi, s) - whole_second) / 60000);
  const sign = offset_min >= 0 ? "+" : "-";
  const abs = Math.abs(offset_min);
  const day = `${y}-${pad(mo)}-${pad(d)}`;
  return {
    day,
    compact: day.replace(/-/g, ""),
    iso: `${day}T${pad(h)}:${pad(mi)}:${pad(s)}${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`,
  };
}

module.exports = { create_clock, resolve_zone, local_stamp };
