// ============================================================
// gateway/tests/present_night_weave.test.js — the report written + 30 min → Loci dreams
//
// In process: night_weave.js with a fake clock, a hand-set last report and a tiny Loci
// that answers /api/loci/dream/weave. What is being proved:
//   · not before report + 30 min; once at or after it; never again for the same report,
//     not even after a restart (the state is on disk)
//   · a newer report asks again; a report older than FRESH_MS is not dreamt about now
//   · a failure is tried again on a later beat, at most TRIES times, then given up
//   · the hook key rides in the header; the state keeps the id, never any text
// Nothing outside os.tmpdir() is written; the only port is the fake Loci's, on loopback.
// ============================================================

const { test, before, after } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const { create_night_weave, DELAY_MS, FRESH_MS, TRIES } = require("../present/night_weave.js");
const { create_fake_clock } = require("./fake_clock.js");

const root = fs.mkdtempSync(path.join(os.tmpdir(), "loci-night-weave-"));
const asked = [];   // { key }
let answer = { status: 200, body: { woven: "d00000000001", nightmare: false, verdict: "过线，织" } };
let server = null;
let base = "";

before(async () => {
  server = http.createServer((req, res) => {
    req.resume();
    req.on("end", () => {
      if (req.method !== "POST" || req.url !== "/api/loci/dream/weave") { res.writeHead(404); res.end(); return; }
      asked.push({ key: req.headers["x-loci-hook-token"] || "" });
      res.writeHead(answer.status, { "Content-Type": "application/json" });
      res.end(JSON.stringify(answer.body));
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  base = `http://127.0.0.1:${server.address().port}`;
});

after(() => {
  server.close();
  fs.rmSync(root, { recursive: true, force: true });
});

function setup(dir, report_at) {
  const clock = create_fake_clock(report_at);
  const report = { at_ms: report_at };
  const logs = [];
  const make = () => create_night_weave({ data_root: dir, loci_base: base, hook_token: "k-123",
                                          last_report: () => report, clock, log: (l) => logs.push(l) });
  return { clock, report, logs, make };
}

async function beat(nw) { nw.tick(); await nw.settle(); }

test("asks once, at report + 30 min, with the key; a restart does not ask again", async () => {
  asked.length = 0;
  answer = { status: 200, body: { woven: "d00000000001", nightmare: true, verdict: "过线，织" } };
  const t0 = Date.parse("2026-10-15T04:40:00+08:00");
  const s = setup(path.join(root, "once"), t0);
  const nw = s.make();
  s.clock.set(t0 + DELAY_MS - 60 * 1000);
  await beat(nw);
  assert.strictEqual(asked.length, 0, "asked before the half hour");
  s.clock.set(t0 + DELAY_MS);
  await beat(nw);
  assert.strictEqual(asked.length, 1);
  assert.strictEqual(asked[0].key, "k-123");
  const st = nw.status();
  assert.strictEqual(st.asked_for, t0);
  assert.strictEqual(st.woven, "d00000000001");
  assert.strictEqual(st.nightmare, true);
  s.clock.advance(5 * 60 * 1000);
  await beat(nw);
  await beat(s.make());   // a restart reads the state from disk
  assert.strictEqual(asked.length, 1, "asked twice for one report");
});

test("a newer report asks again; a stale one is not dreamt about now", async () => {
  asked.length = 0;
  answer = { status: 200, body: { woven: null, nightmare: false, verdict: "攒不到线，一夜无梦" } };
  const t0 = Date.parse("2026-10-15T04:40:00+08:00");
  const s = setup(path.join(root, "newer"), t0);
  const nw = s.make();
  s.clock.set(t0 + DELAY_MS);
  await beat(nw);
  assert.strictEqual(asked.length, 1);
  assert.strictEqual(nw.status().woven, null);
  const t1 = t0 + 24 * 60 * 60 * 1000;
  s.report.at_ms = t1;
  s.clock.set(t1 + DELAY_MS);
  await beat(nw);
  assert.strictEqual(asked.length, 2, "the next night's report did not ask");

  const stale = setup(path.join(root, "stale"), t0);
  stale.clock.set(t0 + FRESH_MS + 60 * 1000);
  await beat(stale.make());
  assert.strictEqual(asked.length, 2, "a report past FRESH_MS was dreamt about");
});

test("a failure is tried again on later beats, then given up", async () => {
  asked.length = 0;
  answer = { status: 500, body: { error: "boom" } };
  const t0 = Date.parse("2026-10-15T04:40:00+08:00");
  const s = setup(path.join(root, "fail"), t0);
  const nw = s.make();
  s.clock.set(t0 + DELAY_MS);
  for (let i = 0; i < TRIES + 2; i += 1) { await beat(nw); s.clock.advance(60 * 1000); }
  assert.strictEqual(asked.length, TRIES);
  const st = nw.status();
  assert.strictEqual(st.tries, TRIES);
  assert.ok(st.gave_up_at && /500/.test(st.error), JSON.stringify(st));
  assert.ok(s.logs.some((l) => /given up/.test(l)));
});

test("Loci unreachable: no throw, kept for the next beat", async () => {
  const t0 = Date.parse("2026-10-15T04:40:00+08:00");
  const clock = create_fake_clock(t0 + DELAY_MS);
  const dir = path.join(root, "down");
  const nw = create_night_weave({ data_root: dir, loci_base: "http://127.0.0.1:1", last_report: () => ({ at_ms: t0 }),
                                  clock, log: () => {} });
  await beat(nw);
  const st = nw.status();
  assert.strictEqual(st.asked_for, undefined);
  assert.strictEqual(st.tries, 1);
  assert.ok(st.error);
});
