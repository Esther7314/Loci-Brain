// ============================================================
// gateway/tests/present_settings.test.js — settings.js and prompts.js in-process
//
// What the black-box tests (present_api.test.js) do not reach directly: what wake is
// handed when the file is bad, the masked Bark tail for each way of writing the code,
// a hand edit with keys this version does not know, and the shells the cards go into.
// Temp directories only; no network.
// ============================================================

const { test, after } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { create_settings, DEFAULTS, bark_code, bark_hint } = require("../present/settings.js");
const P = require("../present/prompts.js");
const { create_fake_clock } = require("./fake_clock.js");

const root = fs.mkdtempSync(path.join(os.tmpdir(), "loci-present-settings-"));
after(() => fs.rmSync(root, { recursive: true, force: true }));
let n = 0;
const fresh = (env = {}) => create_settings({ file: path.join(root, `p${++n}`, "present.json"), env });

test("no file: every default, state defaults, wake may read its (off) section", () => {
  const s = fresh();
  const got = s.load();
  assert.strictEqual(got.state, "defaults");
  assert.deepStrictEqual(got.values, JSON.parse(JSON.stringify(DEFAULTS)));
  assert.deepStrictEqual(s.for_wake(), DEFAULTS.wake);
  assert.strictEqual(s.view().own.fixed_key_set, false);
  assert.strictEqual(fresh({ LOCI_UPSTREAM_KEY: "sk-x" }).view().own.fixed_key_set, true);
});

test("unreadable or a bad wake section: wake gets null (do not wake); compress and report keep running", () => {
  const s = fresh();
  fs.mkdirSync(path.dirname(s.file), { recursive: true });
  fs.writeFileSync(s.file, "[1, 2", "utf8");
  assert.strictEqual(s.load().state, "unreadable");
  assert.deepStrictEqual(s.load().values.compress, DEFAULTS.compress);
  assert.strictEqual(s.for_wake(), null);
  assert.strictEqual(s.patch({ wake: { on: false } }).status, 409);

  fs.writeFileSync(s.file, JSON.stringify({ wake: { dnd: { from: "8 pm" } }, report: { quiet_min: 45 }, someday: { x: 1 } }), "utf8");
  const got = s.load();
  assert.strictEqual(got.state, "invalid");
  assert.deepStrictEqual(got.errors, [{ field: "wake.dnd.from", error: "时间要写成 HH:MM（00:00–23:59）" }]);
  assert.strictEqual(got.values.report.quiet_min, 45);
  assert.strictEqual(s.for_wake(), null);

  fs.writeFileSync(s.file, JSON.stringify({ wake: { on: true, unknown_knob: 1 } }), "utf8");
  assert.strictEqual(s.load().state, "ok", "a key this version does not know is ignored in the file");
  assert.strictEqual(s.for_wake().on, true);
});

test("a patch: merged over what is there, written whole, readable by hand", () => {
  const s = fresh();
  assert.deepStrictEqual(s.patch({ compress: { weak_pct: [70, 55] }, own: { models: [" deepseek-chat "] } }), { ok: true });
  const disk = JSON.parse(fs.readFileSync(s.file, "utf8"));
  assert.deepStrictEqual(disk.compress.weak_pct, [55, 70]);
  assert.deepStrictEqual(disk.own.models, ["deepseek-chat"]);
  assert.strictEqual(disk.report.from, "04:00", "untouched sections are written with their values");
  assert.deepStrictEqual(s.patch({ wake: { segments: { mode: "only", list: [{ from: "22:00", to: "02:00" }, { from: "02:00", to: "06:00" }] } } }), { ok: true },
    "touching ends do not overlap (start inclusive, end exclusive)");
  const overlap = s.patch({ wake: { segments: { list: [{ from: "22:00", to: "02:00" }, { from: "01:59", to: "06:00" }] } } });
  assert.deepStrictEqual([overlap.status, overlap.field, overlap.error], [400, "wake.segments.list[1]", "跟第 1 段时间重叠了"]);
  assert.deepStrictEqual(s.patch({ compress: { force_pct: 60 } }),
    { ok: false, status: 400, field: "compress.force_pct", error: "水位线要比强制线低" }, "the panel shows this text as it is");
});

test("compress.ask_on / advanced_on: both on for a file that has neither; each patched alone; a non-boolean is refused", () => {
  const s = fresh();
  fs.mkdirSync(path.dirname(s.file), { recursive: true });
  const written_before = { compress: { on: true, context_tokens: null, ask_pct: 70, keep_raw: 30, weak_pct: [60], force_pct: 90 } };
  fs.writeFileSync(s.file, JSON.stringify(written_before), "utf8");
  const got = s.load();
  assert.strictEqual(got.state, "ok");
  assert.deepStrictEqual(got.values.compress, { ...written_before.compress, ask_on: true, advanced_on: true });
  assert.deepStrictEqual(s.view().compress, got.values.compress, "the panel reads both switches from the view");

  assert.deepStrictEqual(s.patch({ compress: { ask_on: false } }), { ok: true });
  assert.deepStrictEqual([s.load().values.compress.ask_on, s.load().values.compress.advanced_on], [false, true]);
  assert.deepStrictEqual(s.patch({ compress: { advanced_on: false } }), { ok: true }, "both off under compress.on is a setting like any other");
  const disk = JSON.parse(fs.readFileSync(s.file, "utf8")).compress;
  assert.deepStrictEqual(disk, { ...written_before.compress, ask_on: false, advanced_on: false }, "a group that is off keeps its numbers");

  for (const key of ["ask_on", "advanced_on"]) {
    for (const value of ["off", 0, null]) {
      assert.deepStrictEqual(s.patch({ compress: { [key]: value } }),
        { ok: false, status: 400, field: `compress.${key}`, error: "只能是 true 或 false（开 / 关）" });
    }
  }
  assert.deepStrictEqual(s.patch({ compress: { weak_pct: [80] } }),
    { ok: false, status: 400, field: "compress.weak_pct", error: "弱提醒线要比水位线低" }, "the order holds while both groups are off");
  assert.deepStrictEqual(JSON.parse(fs.readFileSync(s.file, "utf8")).compress, disk, "a refused patch writes nothing");

  // by hand: a switch that is not a boolean fails the section, which then runs on its defaults
  fs.writeFileSync(s.file, JSON.stringify({ compress: { advanced_on: "no" } }), "utf8");
  const bad = s.load();
  assert.deepStrictEqual([bad.state, bad.errors], ["invalid", [{ field: "compress.advanced_on", error: "只能是 true 或 false（开 / 关）" }]]);
  assert.deepStrictEqual(bad.values.compress, DEFAULTS.compress);
});

test("the Bark code is shown only as its origin and a two-character tail", () => {
  assert.strictEqual(bark_hint(""), null);
  assert.strictEqual(bark_hint("AbCdEfGh7Q"), "••••7Q");
  assert.strictEqual(bark_hint("abcd"), "••••");
  assert.strictEqual(bark_hint("https://api.day.app/AbCdEfGh7Q/"), "https://api.day.app/••••7Q");
  assert.strictEqual(bark_hint("https://bark.example.com:8443/KeyKeyKeyZZ?sound=x"), "https://bark.example.com:8443/••••ZZ");
  assert.strictEqual(bark_hint("https://bark.example.com/"), "https://bark.example.com/••••");
});

test("the Bark code is read out of whichever of Bark's three examples was pasted", () => {
  const key = "Ab3dEf6hIj9kLm2nOp5qRs";   // 22, the shape of Bark's keys
  assert.strictEqual(bark_code("  AbCdEfGh7Q "), "AbCdEfGh7Q", "a bare key");
  assert.strictEqual(bark_code(""), "");
  // GET: the sample text (and a title) after the key are dropped, a query is kept
  assert.strictEqual(bark_code(`https://api.day.app/${key}/这里改成你自己的推送内容`), `https://api.day.app/${key}`);
  assert.strictEqual(bark_code(`https://api.day.app/${key}/推送标题/这里改成你自己的推送内容?group=x`), `https://api.day.app/${key}?group=x`);
  assert.strictEqual(bark_code(`https://bark.example.com/bark/${key}/body`), `https://bark.example.com/bark/${key}`, "a self-hosted path before the key stays");
  // a plain URL stays as it is
  assert.strictEqual(bark_code("https://api.day.app/AbCdEfGh7Q/"), "https://api.day.app/AbCdEfGh7Q/");
  assert.strictEqual(bark_code(`https://bark.example.com:8443/${key}?sound=x`), `https://bark.example.com:8443/${key}?sound=x`);
  // POST: a curl line to <server>/<key>
  const post = `curl -X "POST" "https://api.day.app/${key}" \\\n     -H 'Content-Type: application/x-www-form-urlencoded; charset=utf-8' \\\n     --data-urlencode "body=Test Bark Server" \\\n     --data-urlencode "title=Test Title"`;
  assert.strictEqual(bark_code(post), `https://api.day.app/${key}`);
  // JSON: a curl line to <server>/push with device_key in the body
  const json = `curl -X "POST" "https://api.day.app/push" \\\n     -H 'Content-Type: application/json; charset=utf-8' \\\n     -d $'{\n  "body": "Test Bark Server",\n  "title": "Test Title",\n  "device_key": "${key}"\n}'`;
  assert.strictEqual(bark_code(json), `https://api.day.app/${key}`);
  assert.strictEqual(bark_code(`{"device_key": "${key}"}`), key, "a device_key with no URL is the key");
  // nothing to read
  for (const bad of ["has a space", "推送码", "https:// nothing"]) assert.throws(() => bark_code(bad), /push\.bark|推送码|网址/, bad);
});

test("shells: the card goes where {卡} was, the material around it", () => {
  const card = "（这一张是用户改过的卡）";
  const weak = P.compress_self_shell({ card, keep_raw: 40 });
  const ask = P.compress_self_shell({ card, keep_raw: 40, line: "ask" });
  assert.ok(weak.includes(`\n\n${card}\n\n怎么写\n`));
  assert.ok(weak.includes("最近 40 条原话。\n所以这段要补的，是那 40 条之前"));
  assert.ok(weak.includes("挑你自己顺手的那一刻。") && !ask.includes("挑你自己顺手的那一刻。"));
  assert.ok(ask.includes("〔系统提醒 · 不是 ta 发的话〕\n\n这扇窗快装不下了，这一轮或者下一轮找个口子收一下。\n\n收好了"));
  assert.throws(() => P.compress_self_shell({ card, keep_raw: 40, line: "loud" }));

  const forced = P.compress_forced_shell({ card, keep_raw: 30, originals: "ta：考完了！" });
  assert.ok(forced.startsWith("【换窗 · 系统请求，不是 ta 发的消息】"));
  assert.ok(forced.endsWith("——到刚才为止的原话，一字未动——\nta：考完了！"));

  const report = P.report_shell({ card, from: "10月6日", to: "10月7日", slices: null, originals: "……" });
  assert.ok(report.includes("10月6日到10月7日这段日子到这儿为止了。"));
  assert.ok(report.includes("\n今晚的切片没交上，这次先只写日报\n"));
  assert.ok(P.report_shell({ card, from: "a", to: "b", slices: "1. 考试（第 3–9 行）", originals: "" }).includes("\n1. 考试（第 3–9 行）\n"));

  assert.strictEqual(P.wake_shell({ card }), `【自由时间 · 系统给的，不是 ta 发的消息】\n${card}`);
  assert.strictEqual(P.wake_shell({ card, dream: "梦见海边。", muse: true }),
    `【自由时间 · 系统给的，不是 ta 发的消息】\n${card}\n另外，昨晚你做了个梦：梦见海边。（你知道就行，不用复述给 ta。）\n〔发呆〕有几团日子和想法还没整理，该发呆了（muse()）`);
});

test("prompts: the text in force is the rewrite, else the default", () => {
  const clock = create_fake_clock(Date.parse("2026-10-07T12:00:00Z"));
  const prompts = P.create_prompts({ file: path.join(root, "prompts", "prompts.json"), clock, zone: "Asia/Shanghai" });
  assert.strictEqual(prompts.current("compress"), P.CARDS.compress.text);
  assert.strictEqual(prompts.apply({ key: "compress", text: "改过的" }).ok, true);
  assert.strictEqual(prompts.current("compress"), "改过的");
  assert.strictEqual(prompts.current("report"), P.CARDS.report.text);
  assert.throws(() => prompts.current("nope"));
});

test("the upstream: the panel URL over LOCI_UPSTREAM; the panel key over LOCI_UPSTREAM_KEY over the client's", () => {
  let client = null;
  const s = create_settings({ file: path.join(root, `p${++n}`, "present.json"),
    env: { LOCI_UPSTREAM: "https://env.example.com/v1/", LOCI_UPSTREAM_KEY: "sk-env-key-99" },
    client_key: () => client });
  assert.strictEqual(s.upstream_base(), "https://env.example.com/v1");
  assert.deepStrictEqual(s.fixed_key(), { key: "sk-env-key-99", from: "env" });
  assert.deepStrictEqual(s.view().upstream, { base_url: "", env_base_url: "https://env.example.com/v1",
    in_force: "https://env.example.com/v1", key_set: false, key_hint: "••••99", key_from: "env" });

  assert.deepStrictEqual(s.patch({ upstream: { base_url: "http://127.0.0.1:8000/v1", key: "sk-panel-key-12" } }), { ok: true });
  assert.strictEqual(s.upstream_base(), "http://127.0.0.1:8000/v1");
  assert.deepStrictEqual(s.fixed_key(), { key: "sk-panel-key-12", from: "panel" }, "the panel key wins over the env one");
  assert.strictEqual(s.view().upstream.key_from, "panel");
  assert.ok(!JSON.stringify(s.view()).includes("sk-panel-key-12"), "the key never leaves as it is stored");
  assert.deepStrictEqual(s.patch({ upstream: { base_url: "https://other.example.com/v1" } }), { ok: true });
  assert.strictEqual(s.fixed_key().key, "sk-panel-key-12", "a patch without the key keeps it");

  assert.deepStrictEqual(s.patch({ upstream: { key: "" } }), { ok: true });
  assert.deepStrictEqual(s.fixed_key(), { key: "sk-env-key-99", from: "env" }, "cleared: the env key again");

  const bare = create_settings({ file: path.join(root, `p${++n}`, "present.json"), env: {}, client_key: () => client });
  assert.deepStrictEqual([bare.view().upstream.key_from, bare.view().upstream.key_hint, bare.view().upstream.in_force],
    [null, null, null], "nobody spoke yet, nothing set: no key, no upstream");
  client = "";
  assert.deepStrictEqual([bare.view().upstream.key_from, bare.view().upstream.key_hint], ["client", null],
    "the client spoke without a key: its (empty) credential is the one in force");
  client = "sk-client-abc";
  assert.deepStrictEqual([bare.view().upstream.key_from, bare.view().upstream.key_hint], ["client", "••••bc"]);
  assert.strictEqual(bare.view().own.fixed_key_set, false);
});
