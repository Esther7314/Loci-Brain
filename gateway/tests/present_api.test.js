// ============================================================
// gateway/tests/present_api.test.js — /present/* and the present section of /health,
// through the real gateway
//
// The gateway runs as a black-box child process (start_gateway.js) with a fake upstream,
// a fake Loci, the network fence and a fake clock; its data directory is this run's
// temp directory. Before it starts, a thread ledger is planted holding a sentinel string
// where the private state lives (carry, overlay, the last request sent upstream).
//
// What is being proved (construction step 6's acceptance):
//   · no Bearer, or the wrong one → 401, and nothing is written
//   · LOCI_GATEWAY_TOKEN unset → /present/* closed (404, connected:false); /loci/source 503,
//     so Loci reads UNAVAILABLE and lets the memory's own body stand in
//   · GET carries host + connected:true, every setting with its default, honest
//     placeholders for the status that is not built
//   · a bad patch → 400 naming the field, and present.json is not touched
//   · the Bark code is never echoed back (bark_set + a masked tail)
//   · present.json that cannot be read → defaults shown, wake reads as "do not wake",
//     a POST refused (409) rather than written over
//   · prompt cards: defaults, a rewrite, a reset, the same shape as Loci's
//   · push-test with no Bark code says so (skipped_config_missing); report says why it will not
//     queue (a bad kind 400, nothing missing 409) — the report itself is proved in present_day_close.test.js
//   · the sentinel appears in no reply of /present/*, /loci/source or /health
// ============================================================

const { test, before, after } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const fence = require("./network_fence.js");
const { start_fake_upstream } = require("./fake_upstream.js");
const { start_fake_loci } = require("./fake_loci.js");
const { start_gateway } = require("./start_gateway.js");
const { create_file_clock } = require("./fake_clock.js");
const { CARDS } = require("../present/prompts.js");

const root = fs.mkdtempSync(path.join(os.tmpdir(), "loci-present-api-"));
const data_root = path.join(root, "data");
const host_dir = path.join(root, "library", "_hosts", "desk");
const ledger_path = path.join(root, "fence.jsonl");
const settings_file = path.join(data_root, "present.json");
const prompts_file = path.join(data_root, "prompts.json");
const TOKEN = "gw-test-token-0f3a9c";
const SENTINEL = "SENTINEL-CARRY-青柠汽水-7c1e";
const THREAD = "t_5e771e";
const OUR_PORTS = new Set();
const gateways = [];
const replies = [];   // every body any gateway route answered, for the sentinel check

let fake_upstream, fake_loci, gateway, clock;

function shut_poke_gate(dir) {
  fs.mkdirSync(path.join(dir, "state"), { recursive: true });
  fs.writeFileSync(path.join(dir, "state", "poke-window.json"),
    JSON.stringify({ lastUserMessageTime: new Date().toISOString(), wakePending: false }));
}

function plant(dir) {
  // The private state, as later steps will keep it: the sentinel sits in every field
  // that must never leave the ledger.
  fs.mkdirSync(path.join(dir, "threads"), { recursive: true });
  fs.writeFileSync(path.join(dir, "threads", `${THREAD}.json`), JSON.stringify({
    id: THREAD, created_at: 1, last_at: Date.parse("2026-10-07T12:00:00Z"),
    branch: [{ id: "m_20261007_0001", role: "user", fp: "0000000000000000" }],
    window: 3, mark: "m_20261007_0001",
    carry: `【窗口摘要】${SENTINEL}【/窗口摘要】`,
    overlay: [{ after: "m_20261007_0001", text: SENTINEL }],
    last_sent: { messages: [{ role: "system", content: SENTINEL }] },
  }));
  fs.mkdirSync(path.join(host_dir, "days"), { recursive: true });
  fs.writeFileSync(path.join(host_dir, "days", "2026-10-07.jsonl"), `${JSON.stringify({
    id: "m_20261007_0001", rev: 1, at: "2026-10-07T20:00:00+08:00", thread: THREAD,
    role: "user", text: "今天的第一句", attach: null, tools: null, woke: false, state: "live",
  })}\n`);
}

async function boot(dir, extra_env) {
  shut_poke_gate(dir);
  const g = await start_gateway({
    端口: 0,
    上游地址: fake_upstream.地址,
    loci地址: fake_loci.地址,
    数据根: dir,
    相关超时毫秒: 1200,
    白名单端口: [fake_upstream.端口, fake_loci.端口],
    账本路径: ledger_path,
    extra_env: { LOCI_TZ: "Asia/Shanghai", LOCI_GATEWAY_TEST_CLOCK: clock.file, ...extra_env },
  });
  gateways.push(g);
  OUR_PORTS.add(g.端口);
  fence.allow(g.端口);
  return g;
}

before(async () => {
  fake_upstream = await start_fake_upstream({ 端口: 0 });
  fake_loci = await start_fake_loci({ 端口: 0 });
  OUR_PORTS.add(fake_upstream.端口); OUR_PORTS.add(fake_loci.端口);
  clock = create_file_clock(path.join(root, "clock.txt"), Date.parse("2026-10-07T12:30:00Z"));
  plant(data_root);
  gateway = await boot(data_root, {
    LOCI_GATEWAY_TOKEN: TOKEN,
    LOCI_GATEWAY_NAME: "desk",
    LOCI_GATEWAY_DAYS: host_dir,
    LOCI_UPSTREAM_KEY: "sk-fixed-upstream-key",
  });
});

after(async () => {
  for (const g of gateways) await g.停();
  if (fake_upstream) await fake_upstream.关();
  if (fake_loci) await fake_loci.关();
  if (process.env.留下现场) console.log(`[留下现场] ${root}`);
  else fs.rmSync(root, { recursive: true, force: true });
});

// Every error text here reaches the panel as it is, so it must be Chinese.
const CHINESE = /[\u4e00-\u9fff]/;

/** One call to the gateway; returns { status, text, json }. `auth: null` sends no Authorization. */
async function call(method, route, { body, auth = TOKEN, to = gateway } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (auth !== null) headers.Authorization = `Bearer ${auth}`;
  const resp = await fetch(`${to.地址}${route}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await resp.text();
  replies.push(text);
  let json = null;
  try { json = JSON.parse(text); } catch { /* not JSON: json stays null */ }
  return { status: resp.status, text, json, headers: resp.headers };
}

const file_bytes = (f) => (fs.existsSync(f) ? fs.readFileSync(f, "utf8") : null);

const DEFAULT_VIEW = {
  compress: { on: true, context_tokens: null, ask_pct: 75, keep_raw: 40, weak_pct: [65], force_pct: 85 },
  report: { from: "04:00", to: "08:00", quiet_min: 30, flip: "daily", every_n: 2 },
  wake: {
    on: false, every_min: 60, dnd: { on: true, from: "23:00", to: "08:00" },
    segments: { mode: "all", list: [] }, held_cap: null, daily_cap: 16, dry_run: false, allow_short: false,
  },
  own: { tool_rounds: 6, models: [], fixed_key_set: true },
  push: { bark_set: false, bark_hint: null, text: "full" },
};

// ————————————————————————————————————————————————————————————

test("no Bearer or the wrong one → 401 on every route, and nothing is written", { timeout: 20000 }, async () => {
  const routes = [
    ["GET", "/present"], ["POST", "/present", { patch: { wake: { on: true } } }],
    ["POST", "/present/compress", {}], ["POST", "/present/report", { kind: "now" }],
    ["POST", "/present/push-test", {}], ["GET", "/present/prompts"],
    ["POST", "/present/prompts", { key: "wake", text: "x" }],
    ["POST", "/loci/source", { v: 1, source: { system: "gateway", instance: "desk", container: `thread:${THREAD}`, id: "m_20261007_0001" }, revision: null, span: null, scope: null, max_chars: 8000 }],
  ];
  for (const [method, route, body] of routes) {
    for (const auth of [null, "not-the-token", `${TOKEN}x`, ""]) {
      const r = await call(method, route, { body, auth });
      assert.strictEqual(r.status, 401, `${method} ${route} with ${JSON.stringify(auth)}`);
      assert.ok(!r.text.includes(TOKEN), "the token is never echoed");
      assert.ok(!r.text.includes("今天的第一句"));
    }
  }
  assert.strictEqual(file_bytes(settings_file), null);
  assert.strictEqual(file_bytes(prompts_file), null);
});

test("GET /present: host, connected, every default, honest placeholders", { timeout: 20000 }, async () => {
  const r = await call("GET", "/present");
  assert.strictEqual(r.status, 200);
  assert.strictEqual(r.headers.get("cache-control"), "no-store");
  assert.strictEqual(r.json.host, "desk");
  assert.strictEqual(r.json.connected, true);
  assert.deepStrictEqual(r.json.settings, DEFAULT_VIEW);
  assert.deepStrictEqual(r.json.settings_state, { state: "defaults", errors: [] });
  assert.ok(!r.text.includes("sk-fixed-upstream-key"), "the fixed key is never echoed");
  const s = r.json.status;
  // the planted ledger holds no window the gateway can read: numbers unknown, said as null
  assert.strictEqual(s.compress.state, "ok");
  assert.strictEqual(s.compress.thread, THREAD);
  assert.strictEqual(s.compress.fill_pct, null);
  assert.strictEqual(s.compress.used_tokens, null);
  assert.strictEqual(s.compress.kept_raw, null);
  assert.strictEqual(s.compress.last, null);
  assert.deepStrictEqual(s.compress.context, { tokens: 1000000, source: "default" });
  // the planted line was said after this morning's report window opened: yesterday was quiet
  assert.strictEqual(s.report.state, "quiet");
  assert.strictEqual(s.report.day, "2026-10-06");
  assert.strictEqual(s.report.text, null);
  assert.strictEqual(s.report.next_flip, "daily");
  assert.strictEqual(s.wake.state, "off");
  assert.strictEqual(s.wake.next_why, "off");
  assert.strictEqual(s.wake.next_why_words, "自动唤醒关着");
  assert.deepStrictEqual([s.wake.last, s.wake.next_at, s.wake.held], [null, null, 0]);
  assert.deepStrictEqual(s.wake.today, { woke: 0, spoke: 0, paid_failures: 0, dry_run: 0 });
  assert.deepStrictEqual(s.push, { state: "off", last: null, retrying: 0 }, "no Bark code: off, nothing pushed yet");
});

test("POST /present: a good patch is kept, the Bark code is never echoed", { timeout: 20000 }, async () => {
  const r = await call("POST", "/present", { body: { patch: {
    compress: { ask_pct: 70, weak_pct: [60, 50], context_tokens: 200000 },
    wake: { on: true, held_cap: 3, segments: { mode: "only", list: [{ from: "09:00", to: "12:00" }, { from: "22:00", to: "01:00" }] } },
    push: { bark: "https://api.day.app/AbCdEfGh7Q/", bark_set: false },
  } } });
  assert.strictEqual(r.status, 200, r.text);
  assert.strictEqual(r.json.connected, true);
  assert.deepStrictEqual(r.json.settings.compress.weak_pct, [50, 60]);
  assert.strictEqual(r.json.settings.compress.ask_pct, 70);
  assert.deepStrictEqual(r.json.status.compress.context, { tokens: 200000, source: "user" });
  assert.strictEqual(r.json.settings.wake.on, true);
  assert.strictEqual(r.json.settings.wake.held_cap, 3);
  // on, with a key, but the planted thread holds no answered request to wake into
  assert.strictEqual(r.json.status.wake.state, "on");
  assert.strictEqual(r.json.status.wake.next_why, "no_thread");
  assert.deepStrictEqual(r.json.settings.push, { bark_set: true, bark_hint: "https://api.day.app/••••7Q", text: "full" });
  assert.ok(!r.text.includes("AbCdEfGh7Q"));
  const again = await call("GET", "/present");
  assert.ok(!again.text.includes("AbCdEfGh7Q"));
  assert.deepStrictEqual(again.json.settings, r.json.settings);
  const disk = JSON.parse(file_bytes(settings_file));
  assert.strictEqual(disk.push.bark, "https://api.day.app/AbCdEfGh7Q/", "the code itself is kept on disk, for the gateway to push with");
  assert.ok(!("bark_set" in disk.push), "read-only echoes are dropped, not stored");

  const cleared = await call("POST", "/present", { body: { patch: { push: { bark: "" } } } });
  assert.deepStrictEqual(cleared.json.settings.push, { bark_set: false, bark_hint: null, text: "full" });
});

test("POST /present: a bad patch → 400 naming the field; present.json untouched", { timeout: 30000 }, async () => {
  const cases = [
    [{ report: { from: "4:00" } }, "report.from"],
    [{ report: { to: "04:00" } }, "report.to"],
    [{ compress: { ask_pct: 100 } }, "compress.ask_pct"],
    [{ compress: { ask_pct: 0 } }, "compress.ask_pct"],
    [{ compress: { weak_pct: [80] } }, "compress.weak_pct"],
    [{ compress: { ask_pct: 50 } }, "compress.ask_pct"],
    [{ compress: { force_pct: 60 } }, "compress.force_pct"],
    [{ compress: { weak_pct: [40, 40] } }, "compress.weak_pct"],
    [{ compress: { keep_raw: 2.5 } }, "compress.keep_raw"],
    [{ wake: { segments: { list: [{ from: "09:00", to: "12:00" }, { from: "11:00", to: "13:00" }] } } }, "wake.segments.list[1]"],
    [{ wake: { segments: { list: [{ from: "23:00", to: "02:00" }, { from: "01:00", to: "03:00" }] } } }, "wake.segments.list[1]"],
    [{ wake: { segments: { mode: "only", list: [] } } }, "wake.segments.list"],
    [{ wake: { segments: { mode: "sometimes" } } }, "wake.segments.mode"],
    [{ wake: { dnd: { from: "25:00" } } }, "wake.dnd.from"],
    [{ wake: { dnd: { loud: true } } }, "wake.dnd.loud"],
    [{ wake: { every_min: 5 } }, "wake.every_min"],
    [{ wake: { held_cap: 0 } }, "wake.held_cap"],
    [{ wake: { on: "yes" } }, "wake.on"],
    [{ report: { flip: "weekly" } }, "report.flip"],
    [{ push: { bark: "has a space" } }, "push.bark"],
    [{ push: { text: "loud" } }, "push.text"],
    [{ compress: { nope: 1 } }, "compress.nope"],
    [{ nope: {} }, "nope"],
    ["not an object", "patch"],
  ];
  const before_bytes = file_bytes(settings_file);
  for (const [patch, field] of cases) {
    const r = await call("POST", "/present", { body: { patch } });
    assert.strictEqual(r.status, 400, `${JSON.stringify(patch)} → ${r.text}`);
    assert.strictEqual(r.json.field, field, `${JSON.stringify(patch)} → ${r.text}`);
    assert.match(r.json.error, CHINESE, `the panel shows it as it is: ${r.text}`);
  }
  const no_patch = await call("POST", "/present", { body: { wake: { on: true } } });
  assert.strictEqual(no_patch.status, 400);
  assert.strictEqual(no_patch.json.field, "patch");
  assert.match(no_patch.json.error, CHINESE);
  assert.strictEqual(file_bytes(settings_file), before_bytes, "no bad patch touched the file");

  // allow_short lowers the floor, in the same patch or from the file
  const short = await call("POST", "/present", { body: { patch: { wake: { allow_short: true, every_min: 2 } } } });
  assert.strictEqual(short.status, 200, short.text);
  assert.strictEqual(short.json.settings.wake.every_min, 2);
});

test("present.json that cannot be read: defaults, do not wake, and never written over", { timeout: 20000 }, async () => {
  const good = file_bytes(settings_file);
  fs.writeFileSync(settings_file, "{ this is not json", "utf8");
  try {
    const r = await call("GET", "/present");
    assert.strictEqual(r.status, 200);
    assert.strictEqual(r.json.settings_state.state, "unreadable");
    assert.deepStrictEqual(r.json.settings.compress, DEFAULT_VIEW.compress, "compress runs on the defaults");
    assert.deepStrictEqual(r.json.settings.report, DEFAULT_VIEW.report, "report runs on the defaults");
    assert.strictEqual(r.json.status.wake.next_why, "settings_unreadable", "wake treats it as do not wake");
    const w = await call("POST", "/present", { body: { patch: { wake: { on: false } } } });
    assert.strictEqual(w.status, 409, w.text);
    assert.strictEqual(w.json.field, "present.json");
    assert.match(w.json.error, /^present\.json 读不出来/);
    assert.strictEqual(file_bytes(settings_file), "{ this is not json", "the owner's broken file is left as it is");
    const h = await call("GET", "/health");
    assert.strictEqual(h.json.present.settings.state, "unreadable");

    // readable, one section bad by hand: that section falls back, and a bad wake means do not wake
    fs.writeFileSync(settings_file, JSON.stringify({ compress: { ask_pct: 70 }, wake: { on: true, every_min: "often" } }), "utf8");
    const part = await call("GET", "/present");
    assert.strictEqual(part.json.settings_state.state, "invalid");
    assert.deepStrictEqual(part.json.settings_state.errors.map((e) => e.field), ["wake.every_min"]);
    assert.strictEqual(part.json.settings.compress.ask_pct, 70, "the good section is read");
    assert.strictEqual(part.json.settings.wake.on, false, "the bad section is the default");
    assert.strictEqual(part.json.status.wake.next_why, "settings_unreadable");
    const fix_elsewhere = await call("POST", "/present", { body: { patch: { compress: { ask_pct: 72 } } } });
    assert.strictEqual(fix_elsewhere.status, 400);
    assert.strictEqual(fix_elsewhere.json.field, "wake.every_min", "the hand-edited field is named until it is fixed");
    const fixed = await call("POST", "/present", { body: { patch: { wake: { every_min: 30 } } } });
    assert.strictEqual(fixed.status, 200, fixed.text);
    assert.strictEqual(fixed.json.settings_state.state, "ok");
  } finally {
    fs.writeFileSync(settings_file, good, "utf8");
  }
});

test("prompt cards: defaults, a rewrite, back to the default, a reset", { timeout: 20000 }, async () => {
  const r = await call("GET", "/present/prompts");
  assert.strictEqual(r.status, 200);
  assert.deepStrictEqual(r.json.items.map((c) => c.key), ["compress", "report", "wake"]);
  for (const c of r.json.items) {
    assert.deepStrictEqual(Object.keys(c).sort(), ["changed", "changed_at", "default", "edited", "key", "notes", "text", "title"]);
    assert.strictEqual(c.text, CARDS[c.key].text);
    assert.strictEqual(c.default, CARDS[c.key].text);
    assert.strictEqual(c.edited, false);
    assert.strictEqual(c.changed, null);
    assert.ok(c.title && c.notes.length >= 2);
  }
  assert.ok(r.json.items[2].text.startsWith("现在是你的自由时间。"));

  const NEW = "现在是你自己的时间。想说就说，不想说就不说。";
  const w = await call("POST", "/present/prompts", { body: { key: "wake", text: NEW } });
  assert.strictEqual(w.status, 200, w.text);
  assert.strictEqual(w.json.ok, true);
  assert.strictEqual(w.json.item.text, NEW);
  assert.strictEqual(w.json.item.edited, true);
  assert.strictEqual(w.json.item.changed, "2026-10-07");
  assert.strictEqual(w.json.item.changed_at, "2026-10-07T20:30:00+08:00");
  assert.deepStrictEqual(Object.keys(JSON.parse(file_bytes(prompts_file)).prompts), ["wake"], "only the rewrite is stored");
  const listed = await call("GET", "/present/prompts");
  assert.strictEqual(listed.json.items[2].text, NEW);
  assert.strictEqual(listed.json.items[0].edited, false);

  const same = await call("POST", "/present/prompts", { body: { key: "wake", text: CARDS.wake.text } });
  assert.strictEqual(same.json.item.edited, false, "the default text is no rewrite");
  assert.deepStrictEqual(JSON.parse(file_bytes(prompts_file)).prompts, {});

  await call("POST", "/present/prompts", { body: { key: "report", text: "写日报。" } });
  const reset = await call("POST", "/present/prompts", { body: { key: "report", reset: true } });
  assert.strictEqual(reset.json.item.edited, false);
  assert.strictEqual(reset.json.item.text, CARDS.report.text);

  for (const body of [{ key: "nope", text: "x" }, { text: "x" }, { key: "wake" }, { key: "wake", text: "   " }, { key: "wake", text: "长".repeat(20001) }]) {
    const bad = await call("POST", "/present/prompts", { body });
    assert.strictEqual(bad.status, 400, JSON.stringify(body).slice(0, 80));
    assert.match(bad.json.error, CHINESE, bad.text);
  }

  fs.writeFileSync(prompts_file, "nope", "utf8");
  try {
    const broken = await call("GET", "/present/prompts");
    assert.strictEqual(broken.status, 200);
    assert.ok(broken.json.error);
    assert.strictEqual(broken.json.items[0].text, CARDS.compress.text);
    const refused = await call("POST", "/present/prompts", { body: { key: "wake", text: NEW } });
    assert.strictEqual(refused.status, 409);
    assert.match(refused.json.error, CHINESE, refused.text);
    assert.strictEqual(file_bytes(prompts_file), "nope");
  } finally { fs.rmSync(prompts_file); }
});

test("push-test without a code says so; report refuses what it cannot do; compress queues a pack", { timeout: 20000 }, async () => {
  // 「现在压」 on the planted conversation: queued (its one line is inside keep_raw, so the
  // pack finds nothing to fold and sends nothing); an unknown conversation is a 404
  const queued = await call("POST", "/present/compress", { body: {} });
  assert.strictEqual(queued.status, 200, queued.text);
  assert.deepStrictEqual(queued.json, { ok: true, queued: true, thread: THREAD });
  assert.strictEqual((await call("POST", "/present/compress", { body: { thread: "t_000000" } })).status, 404);
  assert.strictEqual((await call("POST", "/present/compress", { body: { thread: 7 } })).status, 400);
  const pushed = await call("POST", "/present/push-test", { body: {} });
  assert.strictEqual(pushed.status, 200);
  assert.deepStrictEqual(pushed.json, { ok: false, status: "skipped_config_missing", error: "推送码没填", endpoint_redacted: "" });
  const bad = await call("POST", "/present/report", { body: { kind: "later" } });
  assert.deepStrictEqual([bad.status, bad.json.field, bad.json.error], [400, "kind", "kind 只能是 missing 或 now"]);
  // a manual flip schedules nothing, so nothing is missing (no turn is started here)
  assert.strictEqual((await call("POST", "/present", { body: { patch: { report: { flip: "manual" } } } })).status, 200);
  const none = await call("POST", "/present/report", { body: { kind: "missing" } });
  assert.strictEqual(none.status, 409, none.text);
  assert.strictEqual(none.json.ok, false);
  assert.match(none.json.error, /不该写日报/);
  assert.strictEqual((await call("POST", "/present", { body: { patch: { report: { flip: "daily" } } } })).status, 200);
  assert.strictEqual((await call("GET", "/present/compress")).status, 404);
  assert.strictEqual((await call("GET", "/present/nope")).status, 404);
  assert.strictEqual((await call("DELETE", "/present")).status, 404);
  assert.strictEqual((await call("GET", "/loci/source")).status, 404);
});

test("/health: a present section that says when each part last succeeded", { timeout: 20000 }, async () => {
  const r = await call("GET", "/health", { auth: null });
  assert.strictEqual(r.status, 200);
  const p = r.json.present;
  assert.deepStrictEqual(p.recording, { last_line_at: "2026-10-07T20:00:00+08:00", seconds_ago: 1800 });
  assert.deepStrictEqual(p.push, { state: "off", last_ok_at: null, last_ok_seconds_ago: null, failures_since_ok: 0,
                                   last_failure_status: null, retrying: 0 });
  assert.deepStrictEqual([p.report.state, p.report.last_ok_at, p.report.last_ok_seconds_ago, p.report.failures_since_ok, p.report.gave_up],
    ["never", null, null, 0, false]);
  assert.deepStrictEqual([p.report.handoff.last_ok_at, p.report.handoff.failures_since_ok], [null, 0]);
  assert.deepStrictEqual(p.wake, { state: p.wake.state, last_ok_at: null, last_ok_seconds_ago: null, failures_since_ok: 0, held: 0 });
  // no window was ever folded here; the manual pack above found nothing and is no failure
  assert.deepStrictEqual([p.compress.state, p.compress.last_ok_seconds_ago, p.compress.failures_since_ok], ["never", null, 0]);
  assert.strictEqual(p.settings.state, "ok");
  assert.deepStrictEqual(p.doors, { present_api: "open", loci_source: "open", bind: "127.0.0.1", passphrase_required: false });
  assert.ok(r.json.verdict, "the rest of /health is unchanged");
});

test("sentinel: nothing from the private thread state reaches any reply", { timeout: 20000 }, async () => {
  const ledger = fs.readFileSync(path.join(data_root, "threads", `${THREAD}.json`), "utf8");
  assert.ok(ledger.includes(SENTINEL), "the sentinel is really there");
  // one more pass over every route, then every reply this file has seen
  await call("GET", "/present");
  await call("POST", "/present", { body: { patch: { compress: { keep_raw: 30 } } } });
  await call("POST", "/present/compress", { body: { thread: THREAD } });
  await call("POST", "/present/report", { body: { kind: "now" } });
  await call("POST", "/present/push-test", { body: {} });
  await call("GET", "/present/prompts");
  await call("POST", "/present/prompts", { body: { key: "compress", reset: true } });
  await call("GET", "/health", { auth: null });
  for (const id of ["m_20261007_0001", "m_20261007_0002"]) {
    const src = await call("POST", "/loci/source", { body: { v: 1, source: { system: "gateway", instance: "desk", container: `thread:${THREAD}`, id }, revision: null, span: null, scope: null, max_chars: 8000 } });
    assert.strictEqual(src.status, 200);
  }
  const run = await call("POST", "/loci/source", { body: { v: 1, source: { system: "gateway", instance: "desk", container: `thread:${THREAD}`, id: "m_20261007_0001", through: "m_20261007_0009" }, revision: null, span: null, scope: null, max_chars: 8000 } });
  assert.strictEqual(run.status, 200);
  assert.ok(replies.length > 40);
  const leaks = replies.filter((t) => t.includes(SENTINEL) || t.includes("SENTINEL"));
  assert.deepStrictEqual(leaks, []);
});

test("LOCI_GATEWAY_TOKEN unset: /present/* closed with 404, /loci/source with 503", { timeout: 20000 }, async () => {
  const closed_root = path.join(root, "data-closed");
  plant(closed_root);
  const closed = await boot(closed_root, { LOCI_GATEWAY_DAYS: host_dir });
  for (let i = 0; i < 250 && !closed.全部输出().includes("closed — LOCI_GATEWAY_TOKEN unset"); i++) await new Promise((r) => setTimeout(r, 20));
  assert.ok(closed.全部输出().includes("closed — LOCI_GATEWAY_TOKEN unset"), "the banner says the doors are closed");
  for (const [method, route, status] of [["GET", "/present", 404], ["POST", "/present", 404], ["GET", "/present/prompts", 404],
    ["POST", "/loci/source", 503]]) {
    for (const auth of [null, TOKEN, ""]) {
      const r = await call(method, route, { body: method === "POST" ? {} : undefined, auth, to: closed });
      assert.strictEqual(r.status, status, `${method} ${route}`);
      assert.strictEqual(r.json.connected, false);
    }
  }
  // /loci/source as Loci really asks it: 503 (UNAVAILABLE on Loci's side, so the memory's
  // own body stands in), and no text
  const src = await call("POST", "/loci/source", { auth: TOKEN, to: closed, body: { v: 1,
    source: { system: "gateway", instance: "gateway", container: `thread:${THREAD}`, id: "m_20261007_0001" },
    revision: null, span: null, scope: null, max_chars: 8000 } });
  assert.strictEqual(src.status, 503);
  assert.ok(!src.text.includes("今天的第一句"));
  const h = await call("GET", "/health", { auth: null, to: closed });
  assert.strictEqual(h.json.present.doors.present_api, "closed: LOCI_GATEWAY_TOKEN is not set");
  assert.strictEqual(file_bytes(path.join(closed_root, "present.json")), null);
});

test("reconciliation: every outbound connection stayed inside this run", () => {
  const entries = fs.existsSync(ledger_path)
    ? fs.readFileSync(ledger_path, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
  const strays = entries.filter((e) => !OUR_PORTS.has(e.端口));
  assert.deepStrictEqual(strays, []);
  assert.ok(entries.every((e) => e.放行), "nothing had to be blocked");
});
