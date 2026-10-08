// ============================================================
// gateway/tests/present_source.test.js — the fourth joint from the host's side:
// POST /loci/source, through the real gateway
//
// Loci's tests/test_source_originals.py puts a fake host in front of Loci and checks how
// Loci reads each answer. Here it is turned around: the gateway is the host, and these
// tests send it exactly what Loci sends (core/_originals.build_request) and check that
// each answer is one Loci reads — the same shape checks core/_originals.parse_answer
// makes, ported below as `reads_as_loci_would`.
//
// The day files are planted on disk before the gateway starts, one day before and one
// day of lines across two threads, with an edited line, a regenerated reply, an image
// and a long line.
//
// Cases: given · not_found (no such line / no such day / not this gateway's source) ·
// truncated (a cut single line, a run cut and truncated after) · a revised line's
// revision · a replaced line · a run on one thread, across days, with a missing end ·
// scope · span · the Bearer · speaker (LOCI_OWNER_NAME / LOCI_AI_NAME, else 「用户」 / 「AI」).
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
const { speaker_names } = require("../present/source_api.js");

const root = fs.mkdtempSync(path.join(os.tmpdir(), "loci-present-source-"));
const data_root = path.join(root, "data");
const host_dir = path.join(root, "library", "_hosts", "gateway");
const ledger_path = path.join(root, "fence.jsonl");
const TOKEN = "loci-fetch-key-for-the-gateway";
const A = "t_aaaaaa";
const B = "t_bbbbbb";
const LONG = "长".repeat(300);
const OWNER = "小周";
const AI = "Echo";
const OUR_PORTS = new Set();

let fake_upstream, fake_loci, gateway;

function line(id, rev, at, thread, role, text, extra = {}) {
  return { id, rev, at, thread, role, text, attach: null, tools: null, woke: false, state: "live", ...extra };
}

function plant() {
  const days = path.join(host_dir, "days");
  fs.mkdirSync(days, { recursive: true });
  const write = (day, lines) => fs.writeFileSync(path.join(days, `${day}.jsonl`),
    lines.map((l) => JSON.stringify(l)).join("\n") + "\n", "utf8");
  write("2026-10-06", [
    line("m_20261006_0001", 1, "2026-10-06T23:10:00+08:00", A, "user", "昨天最后一句"),
    line("m_20261006_0002", 1, "2026-10-06T23:11:00+08:00", A, "assistant", "晚安，明天见。"),
  ]);
  write("2026-10-07", [
    line("m_20261007_0001", 1, "2026-10-07T20:41:05+08:00", A, "user", "你好"),
    line("m_20261007_0002", 1, "2026-10-07T20:41:09+08:00", A, "assistant", "你好呀，今天过得怎么样？"),
    line("m_20261007_0003", 1, "2026-10-07T20:42:00+08:00", B, "user", "另一个对话里的话"),
    line("m_20261007_0004", 1, "2026-10-07T20:43:00+08:00", A, "assistant", "考完啦！"),
    line("m_20261007_0005", 1, "2026-10-07T20:44:00+08:00", A, "assistant", "考完了！真替你高兴。"),
    // the reply 0004 was regenerated: a new revision with the same text, state replaced
    line("m_20261007_0004", 2, "2026-10-07T20:44:00+08:00", A, "assistant", "考完啦！", { state: "replaced" }),
    line("m_20261007_0006", 1, "2026-10-07T20:45:00+08:00", A, "assistant", "改之前"),
    line("m_20261007_0006", 2, "2026-10-07T20:50:00+08:00", A, "assistant", "改之后"),
    line("m_20261007_0007", 1, "2026-10-07T20:46:00+08:00", A, "user", "看这张", { attach: ["image"] }),
    line("m_20261007_0008", 1, "2026-10-07T20:47:00+08:00", A, "user", LONG),
    line("m_20261007_0009", 1, "2026-10-07T20:48:00+08:00", A, "assistant", "好长。"),
  ]);
  fs.appendFileSync(path.join(days, "2026-10-07.jsonl"), "{\"id\":\"m_20261007_00", "utf8");   // a torn last line
}

before(async () => {
  fake_upstream = await start_fake_upstream({ 端口: 0 });
  fake_loci = await start_fake_loci({ 端口: 0 });
  OUR_PORTS.add(fake_upstream.端口); OUR_PORTS.add(fake_loci.端口);
  plant();
  fs.mkdirSync(path.join(data_root, "state"), { recursive: true });
  fs.writeFileSync(path.join(data_root, "state", "poke-window.json"),
    JSON.stringify({ lastUserMessageTime: new Date().toISOString(), wakePending: false }));
  gateway = await start_gateway({
    端口: 0,
    上游地址: fake_upstream.地址,
    loci地址: fake_loci.地址,
    数据根: data_root,
    相关超时毫秒: 1200,
    白名单端口: [fake_upstream.端口, fake_loci.端口],
    账本路径: ledger_path,
    extra_env: { LOCI_TZ: "Asia/Shanghai", LOCI_GATEWAY_DAYS: host_dir, LOCI_GATEWAY_TOKEN: TOKEN,
                 LOCI_OWNER_NAME: ` ${OWNER} `, LOCI_AI_NAME: AI },
  });
  OUR_PORTS.add(gateway.端口);
  fence.allow(gateway.端口);
});

after(async () => {
  if (gateway) await gateway.停();
  if (fake_upstream) await fake_upstream.关();
  if (fake_loci) await fake_loci.关();
  if (process.env.留下现场) console.log(`[留下现场] ${root}`);
  else fs.rmSync(root, { recursive: true, force: true });
});

/** Loci's request body for one source record (core/_originals.build_request). */
function request_for({ id, through = null, container = `thread:${A}`, system = "gateway", instance = "gateway",
  revision = null, span = null, scope = null, max_chars = 8000 }) {
  const source = { system, instance, container, id };
  if (through) source.through = through;
  return { v: 1, source, revision, span, scope, max_chars };
}

async function ask(body, auth = TOKEN) {
  const headers = { "Content-Type": "application/json", Accept: "application/json" };
  if (auth !== null) headers.Authorization = `Bearer ${auth}`;
  const resp = await fetch(`${gateway.地址}/loci/source`, { method: "POST", headers, body: JSON.stringify(body) });
  const text = await resp.text();
  let json = null;
  try { json = JSON.parse(text); } catch { /* stays null */ }
  return { status: resp.status, json, text };
}

// ———— core/_originals.parse_answer's shape checks, ported ————
const ANSWER_KEYS = new Set(["v", "status", "reason", "lines", "truncated_after"]);
const LINE_KEYS = new Set(["id", "revision", "text", "cut", "missing", "speaker", "at"]);
const MISSING_WORDS = ["withdrawn", "deleted", "out_of_scope", "unavailable", "not_found"];

function reads_as_loci_would(answer, source) {
  assert.ok(answer && typeof answer === "object" && !Array.isArray(answer), "an object");
  for (const k of Object.keys(answer)) assert.ok(ANSWER_KEYS.has(k), `answer key ${k}`);
  assert.strictEqual(answer.v, 1);
  if (answer.status !== "given") {
    assert.ok(["unavailable", "not_allowed"].includes(answer.status));
    return;
  }
  assert.ok(Array.isArray(answer.lines) && answer.lines.length > 0, "given with lines");
  for (const ln of answer.lines) {
    for (const k of Object.keys(ln)) assert.ok(LINE_KEYS.has(k), `line key ${k}`);
    assert.ok(typeof ln.id === "string" && ln.id.trim(), "a line id");
    assert.ok((ln.text == null) !== (ln.missing == null), "exactly one of text and missing");
    for (const k of ["text", "missing", "revision"]) assert.ok(ln[k] == null || typeof ln[k] === "string", `${k} is text or null`);
    if ("cut" in ln) assert.strictEqual(typeof ln.cut, "boolean");
    if (ln.missing != null) assert.ok(MISSING_WORDS.includes(ln.missing), ln.missing);
    if (ln.speaker != null) {
      assert.strictEqual(typeof ln.speaker, "string", "speaker is text");
      const name = ln.speaker.trim();
      assert.ok(name && name === ln.speaker && name.length <= 64 && !/[\u0000-\u001f\u007f]/.test(name), `speaker reads: ${ln.speaker}`);
    }
    if (ln.at != null) assert.match(ln.at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$/, "an ISO time with its offset");
  }
  const ids = answer.lines.map((l) => l.id);
  assert.strictEqual(new Set(ids).size, ids.length, "no id twice");
  assert.strictEqual(ids[0], source.id, "starts at the first line asked for");
  const after = answer.truncated_after;
  if (!source.through) {
    assert.strictEqual(ids.length, 1, "a single piece is exactly one line");
    assert.strictEqual(after, null, "never truncated after");
  } else if (after == null) {
    assert.strictEqual(ids[ids.length - 1], source.through, "an untruncated run ends at its last line");
  } else {
    assert.strictEqual(after, ids[ids.length - 1], "truncated_after is the last line listed");
  }
}

async function given(params) {
  const body = request_for(params);
  const r = await ask(body);
  assert.strictEqual(r.status, 200, r.text);
  reads_as_loci_would(r.json, body.source);
  return r.json;
}

// ————————————————————————————————————————————————————————————

test("given: one line, its revision and when it was said, nothing else", { timeout: 20000 }, async () => {
  const a = await given({ id: "m_20261007_0002", revision: "r1" });
  assert.deepStrictEqual(a, {
    v: 1, status: "given",
    lines: [{ id: "m_20261007_0002", revision: "r1", text: "你好呀，今天过得怎么样？", at: "2026-10-07T20:41:09+08:00", speaker: AI }],
    truncated_after: null,
  });
});

test("not_found: no such line, no such day, not this gateway's source", { timeout: 20000 }, async () => {
  for (const params of [
    { id: "m_20261007_0099" },
    { id: "m_20261001_0001" },
    { id: "m_0003" },
    { id: "m_20261007_0002", system: "lento" },
    { id: "m_20261007_0002", instance: "someone-else" },
  ]) {
    const a = await given(params);
    assert.deepStrictEqual(a.lines, [{ id: params.id, revision: null, missing: "not_found" }], JSON.stringify(params));
    assert.strictEqual(a.truncated_after, null);
  }
});

test("truncated: a single line is cut and never truncated after; a run is cut and truncated after", { timeout: 20000 }, async () => {
  const one = await given({ id: "m_20261007_0008", max_chars: 100 });
  assert.deepStrictEqual(one.lines, [{ id: "m_20261007_0008", revision: "r1", text: "长".repeat(100), cut: true, at: "2026-10-07T20:47:00+08:00", speaker: OWNER }]);
  assert.strictEqual(one.truncated_after, null);

  const run = await given({ id: "m_20261007_0007", through: "m_20261007_0009", max_chars: 107 });
  assert.deepStrictEqual(run.lines.map((l) => [l.id, l.text, l.cut || false]), [
    ["m_20261007_0007", "看这张 [image]", false],
    ["m_20261007_0008", "长".repeat(96), true],
  ]);
  assert.strictEqual(run.truncated_after, "m_20261007_0008");

  const whole = await given({ id: "m_20261007_0007", through: "m_20261007_0009" });
  assert.strictEqual(whole.truncated_after, null);
  assert.deepStrictEqual(whole.lines.map((l) => l.id), ["m_20261007_0007", "m_20261007_0008", "m_20261007_0009"]);
  assert.ok(whole.lines.every((l) => !("cut" in l)));
});

test("a revised line: its latest text with its own revision, said when it was first said", { timeout: 20000 }, async () => {
  const a = await given({ id: "m_20261007_0006", revision: "r1" });
  assert.deepStrictEqual(a.lines, [{ id: "m_20261007_0006", revision: "r2", text: "改之后", at: "2026-10-07T20:45:00+08:00", speaker: AI }]);
});

test("a replaced line (a regenerated reply) is given as it was said, at the revision its text was set", { timeout: 20000 }, async () => {
  const a = await given({ id: "m_20261007_0004", revision: "r1" });
  assert.deepStrictEqual(a.lines, [{ id: "m_20261007_0004", revision: "r1", text: "考完啦！", at: "2026-10-07T20:43:00+08:00", speaker: AI }]);
});

test("a run: the container's thread only, across days, an end that is not there marked not_found", { timeout: 20000 }, async () => {
  const a = await given({ id: "m_20261007_0001", through: "m_20261007_0005" });
  assert.deepStrictEqual(a.lines.map((l) => l.id), ["m_20261007_0001", "m_20261007_0002", "m_20261007_0004", "m_20261007_0005"],
    "the other conversation's line in between is not part of this run");
  const bare = await given({ id: "m_20261007_0001", through: "m_20261007_0005", container: "thread:aaaaaa" });
  assert.deepStrictEqual(bare.lines.map((l) => l.id), a.lines.map((l) => l.id), "thread:<6 hex> names the same thread");

  const days = await given({ id: "m_20261006_0002", through: "m_20261007_0002" });
  assert.deepStrictEqual(days.lines.map((l) => [l.id, l.text]), [
    ["m_20261006_0002", "晚安，明天见。"], ["m_20261007_0001", "你好"], ["m_20261007_0002", "你好呀，今天过得怎么样？"],
  ]);
  assert.deepStrictEqual(days.lines.map((l) => l.speaker), [AI, OWNER, AI], "each line names who said it");

  const open_end = await given({ id: "m_20261007_0009", through: "m_20261007_0012" });
  assert.deepStrictEqual(open_end.lines, [
    { id: "m_20261007_0009", revision: "r1", text: "好长。", at: "2026-10-07T20:48:00+08:00", speaker: AI },
    { id: "m_20261007_0012", revision: null, missing: "not_found" },
  ]);
  const backwards = await given({ id: "m_20261007_0005", through: "m_20261007_0001" });
  assert.ok(backwards.lines.every((l) => l.missing === "not_found"));
});

test("scope: a place outside the grant is not allowed; inside it is given", { timeout: 20000 }, async () => {
  const out = await given({ id: "m_20261007_0002", scope: { v: 1, entry: { system: "telegram", instance: "bot-a" }, venue: "group", audience: ["user:U"], grant: [{ system: "lento", instance: "home" }] } });
  assert.deepStrictEqual(out, { v: 1, status: "not_allowed", reason: "out_of_scope" });
  const inside = await given({ id: "m_20261007_0002", scope: { v: 1, entry: { system: "gateway" }, venue: "private", audience: [], grant: [{ system: "gateway", instance: "gateway" }] } });
  assert.strictEqual(inside.status, "given");
  assert.strictEqual(inside.lines[0].text, "你好呀，今天过得怎么样？");
});

test("span: the fragment's own text; a run with a span is refused", { timeout: 20000 }, async () => {
  const a = await given({ id: "m_20261007_0002", span: { unit: "utf16", start: 0, end: 3 } });
  assert.strictEqual(a.lines[0].text, "你好呀");
  const c = await given({ id: "m_20261007_0002", span: { unit: "char", start: 3, end: 5 } });
  assert.strictEqual(c.lines[0].text, "，今");
  const r = await ask(request_for({ id: "m_20261007_0001", through: "m_20261007_0002", span: { unit: "utf16", start: 0, end: 3 } }));
  assert.strictEqual(r.status, 400);
});

test("the request: Loci's Bearer only; a body that does not read is a 400", { timeout: 20000 }, async () => {
  const body = request_for({ id: "m_20261007_0002" });
  for (const auth of [null, "wrong", ""]) {
    const r = await ask(body, auth);
    assert.strictEqual(r.status, 401);
    assert.ok(!r.text.includes("你好呀"));
  }
  for (const bad of [{ ...body, v: 2 }, { v: 1 }, { ...body, source: { ...body.source, id: "" } }]) {
    const r = await ask(bad);
    assert.strictEqual(r.status, 400);
    assert.match(r.json.error, /[\u4e00-\u9fff]/, r.text);
  }
});

test("speaker names: the env names, trimmed; unset, 「用户」 and 「AI」; AI_NAME stands in for LOCI_AI_NAME", () => {
  assert.deepStrictEqual([speaker_names({}).of("user"), speaker_names({}).of("assistant")], ["用户", "AI"]);
  assert.strictEqual(speaker_names({ AI_NAME: "Nova" }).of("assistant"), "Nova");
  assert.strictEqual(speaker_names({ AI_NAME: "Nova", LOCI_AI_NAME: "Echo" }).of("assistant"), "Echo");
  assert.strictEqual(speaker_names({ LOCI_OWNER_NAME: "  " }).of("user"), "用户", "blank is unset");
});

test("reconciliation: every outbound connection stayed inside this run", () => {
  const entries = fs.existsSync(ledger_path)
    ? fs.readFileSync(ledger_path, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
  assert.deepStrictEqual(entries.filter((e) => !OUR_PORTS.has(e.端口)), []);
});
