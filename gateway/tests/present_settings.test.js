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

const { create_settings, DEFAULTS, bark_hint } = require("../present/settings.js");
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
  assert.deepStrictEqual(got.errors, [{ field: "wake.dnd.from", error: "a time written HH:MM (00:00–23:59)" }]);
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
  assert.deepStrictEqual([overlap.status, overlap.field], [400, "wake.segments.list[1]"]);
});

test("the Bark code is shown only as its origin and a two-character tail", () => {
  assert.strictEqual(bark_hint(""), null);
  assert.strictEqual(bark_hint("AbCdEfGh7Q"), "••••7Q");
  assert.strictEqual(bark_hint("abcd"), "••••");
  assert.strictEqual(bark_hint("https://api.day.app/AbCdEfGh7Q/"), "https://api.day.app/••••7Q");
  assert.strictEqual(bark_hint("https://bark.example.com:8443/KeyKeyKeyZZ?sound=x"), "https://bark.example.com:8443/••••ZZ");
  assert.strictEqual(bark_hint("https://bark.example.com/"), "https://bark.example.com/••••");
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
