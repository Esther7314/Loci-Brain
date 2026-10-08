// ============================================================
// gateway/tests/present_window.test.js — the window, the stream filter, the window size,
// in-process
//
// Drives present/window.js, stream_filter.js, context_window.js and fill.js directly.
// The black-box versions (through the real gateway process, with a fake Loci and a fake
// upstream) are in present_cue.test.js.
//
// Nothing here touches the network (the fence is loaded anyway) or any directory outside
// os.tmpdir().
// ============================================================

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { Readable } = require("node:stream");

require("./network_fence.js");
const { create_fake_clock } = require("./fake_clock.js");
const { to_said } = require("../present/threads.js");
const win = require("../present/window.js");
const { with_usage, create_usage_strip } = require("../present/stream_filter.js");
const { create_context_windows, table_tokens, parse_models_list, DEFAULT_TOKENS } = require("../present/context_window.js");
const { measure, estimate_prompt } = require("../present/fill.js");

const SYS = { role: "system", content: "你是一个温柔的伴侣。" };
const u = (content) => ({ role: "user", content });
const a = (content) => ({ role: "assistant", content });
const card = (text) => ({ role: "system", content: text });

/** A thread whose branch is exactly these messages, ids m_1, m_2, … */
function thread_of(messages, id = "t_aaaaaa") {
  const said = messages.filter((m) => m.role === "user" || m.role === "assistant");
  return { id, branch: said.map((m, i) => ({ id: `m_${i + 1}`, role: m.role, fp: to_said(m).fp })) };
}

test("overlays: a card after her line, a dream before it, replayed with the same bytes next turn", () => {
  const h1 = [SYS, u("你好")];
  const t = thread_of(h1);
  const w = win.ensure(t, 0);
  assert.strictEqual(w.name, "t_aaaaaa#w1");
  win.add_overlay(w, { kind: "poke", anchor: "m_1", place: "before", text: "[Loci poke]\n〔梦〕一个梦", turn: "m_1" });
  win.add_overlay(w, { kind: "cue", anchor: "m_1", place: "after", text: "〔相关记忆〕卡一", turn: "m_1" });
  const one = win.assemble(h1, win.map_ids(h1, t.branch, "m_1"), w);
  assert.deepStrictEqual(one.messages, [SYS, card("[Loci poke]\n〔梦〕一个梦"), u("你好"), card("〔相关记忆〕卡一")]);
  assert.strictEqual(one.mark, "none");

  const h2 = [...h1, a("你好呀，今天怎么样？"), u("考完了")];
  const t2 = thread_of(h2);
  t2.window = w;
  win.add_overlay(w, { kind: "cue", anchor: "m_3", place: "after", text: "〔相关记忆〕卡二", turn: "m_3" });
  const two = win.assemble(h2, win.map_ids(h2, t2.branch, "m_3"), w);
  assert.deepStrictEqual(two.messages, [SYS, card("[Loci poke]\n〔梦〕一个梦"), u("你好"), card("〔相关记忆〕卡一"),
    a("你好呀，今天怎么样？"), u("考完了"), card("〔相关记忆〕卡二")]);
  const prefix = JSON.stringify(one.messages).slice(0, -1);
  assert.ok(JSON.stringify(two.messages).startsWith(prefix), "turn 2 starts with turn 1's exact bytes");
  assert.deepStrictEqual(two.replayed.map((o) => o.text), ["[Loci poke]\n〔梦〕一个梦", "〔相关记忆〕卡一", "〔相关记忆〕卡二"]);
  assert.deepStrictEqual(h2[1], u("你好"), "the client's own messages are not touched");
  assert.strictEqual(h2.length, 4);
});

test("an overlay that answered nothing is remembered but never replayed", () => {
  const h = [SYS, u("嗯")];
  const t = thread_of(h);
  const w = win.ensure(t, 0);
  win.add_overlay(w, { kind: "cue", anchor: "m_1", place: "after", text: "", turn: "m_1", delivered: true });
  assert.ok(win.overlay_for(w, "cue", "m_1"));
  assert.deepStrictEqual(win.assemble(h, win.map_ids(h, t.branch, "m_1"), w).messages, h);
});

test("mark kept: client system + carry + lines from the mark on; void when the history lacks it", () => {
  const h = [SYS, { role: "developer", content: "格式要求" }, u("一"), a("一的回话，比较长的一句。"), u("二"), a("二的回话，也比较长的一句。"), u("三")];
  const t = thread_of(h);
  const w = win.ensure(t, 0);
  w.mark = "m_3";   // u("二")
  w.carry = "〔前情〕今天聊了一。";
  win.add_overlay(w, { kind: "cue", anchor: "m_1", place: "after", text: "〔相关记忆〕砍掉的那一段里的卡", turn: "m_1" });
  win.add_overlay(w, { kind: "cue", anchor: "m_3", place: "after", text: "〔相关记忆〕二的卡", turn: "m_3" });
  const kept = win.assemble(h, win.map_ids(h, t.branch, "m_5"), w);
  assert.strictEqual(kept.mark, "kept");
  assert.strictEqual(kept.cut, 2);
  assert.ok(kept.carried);
  assert.deepStrictEqual(kept.messages, [SYS, h[1], { role: "system", content: "〔前情〕今天聊了一。" },
    u("二"), card("〔相关记忆〕二的卡"), a("二的回话，也比较长的一句。"), u("三")]);

  // the client trimmed its history past the mark: forwarded as sent, overlays still where their anchors are
  const trimmed = [SYS, a("二的回话，也比较长的一句。"), u("三")];
  const void_ = win.assemble(trimmed, win.map_ids(trimmed, t.branch, "m_5"), w);
  assert.strictEqual(void_.mark, "void");
  assert.ok(!void_.carried);
  assert.deepStrictEqual(void_.messages, trimmed);
});

test("map_ids walks back through a tool turn and stops where the history leaves the branch", () => {
  const call = { role: "assistant", content: null, tool_calls: [{ id: "c1", type: "function", function: { name: "recall", arguments: "{}" } }] };
  const h = [SYS, u("以前那个人叫什么"), call, { role: "tool", tool_call_id: "c1", content: "结果" }];
  const t = thread_of(h);
  assert.deepStrictEqual(win.map_ids(h, t.branch, "m_2"), [null, "m_1", "m_2", null]);
  const strange = [SYS, u("分支上没有的一句"), ...h.slice(2)];
  assert.deepStrictEqual(win.map_ids(strange, t.branch, "m_2"), [null, null, "m_2", null]);
});

test("a fork inherits the window when the mark is shared, a fresh one when it is not; open_next clears", () => {
  const parent = thread_of([u("一"), a("一的回话"), u("二"), a("二的回话")], "t_parent");
  const w = win.ensure(parent, 0);
  w.mark = "m_3"; w.carry = "前情"; w.model = "m";
  win.add_overlay(w, { kind: "cue", anchor: "m_3", place: "after", text: "卡二", turn: "m_3" });
  win.add_overlay(w, { kind: "cue", anchor: "m_4", place: "after", text: "不在分叉上的卡", turn: "m_4" });

  const shared = { id: "t_fork01", branch: parent.branch.slice(0, 3) };
  win.inherit(shared, parent, 1);
  assert.strictEqual(shared.window.name, "t_parent#w1", "the fork speaks for the same Loci window");
  assert.strictEqual(shared.window.carry, "前情");
  assert.deepStrictEqual(shared.window.overlays.map((o) => o.text), ["卡二"]);
  assert.notStrictEqual(shared.window, w, "a copy, not the same object");

  const before_mark = { id: "t_fork02", branch: parent.branch.slice(0, 2) };
  win.inherit(before_mark, parent, 1);
  assert.strictEqual(before_mark.window.name, "t_fork02#w1");
  assert.strictEqual(before_mark.window.carry, null);
  assert.strictEqual(before_mark.window.mark, null);

  const flip = win.open_next(parent, { mark: "m_4", carry: "新的前情" }, 2);
  assert.deepStrictEqual(flip, { old_name: "t_parent#w1", name: "t_parent#w2" });
  assert.strictEqual(parent.window.no, 2);
  assert.deepStrictEqual(parent.window.overlays, []);
  assert.strictEqual(parent.window.usage, null);
  assert.strictEqual(parent.window.model, "m");
});

// ———— the usage chunk ————

async function through_strip(chunks) {
  const out = [];
  const strip = Readable.from(chunks.map((c) => Buffer.from(c))).pipe(create_usage_strip());
  for await (const c of strip) out.push(c);
  return Buffer.concat(out).toString("utf8");
}

test("usage strip: only the usage-only chunk goes, every other byte as sent, however it is cut", async () => {
  const ev = (o) => `data: ${JSON.stringify(o)}\n\n`;
  const kept = ev({ choices: [{ index: 0, delta: { content: "你好" } }], usage: null })
    + ev({ choices: [{ index: 0, delta: {}, finish_reason: "stop" }], usage: null });
  const usage_only = ev({ choices: [], usage: { prompt_tokens: 10, completion_tokens: 2 } });
  const done = "data: [DONE]\n\n";
  const full = kept + usage_only + done;
  assert.strictEqual(await through_strip([full]), kept + done);
  const bytes = Buffer.from(full);
  const one_by_one = [];
  for (let i = 0; i < bytes.length; i++) one_by_one.push(bytes.subarray(i, i + 1));
  assert.strictEqual(await through_strip(one_by_one), kept + done, "cut one byte at a time, inside multi-byte characters too");

  const crlf = full.replace(/\n/g, "\r\n");
  assert.strictEqual(await through_strip([crlf]), (kept + done).replace(/\n/g, "\r\n"));

  // usage riding on a chunk that also carries a choice stays: the choice is part of the answer
  const in_choice = ev({ choices: [{ index: 0, delta: {}, finish_reason: "stop" }], usage: { prompt_tokens: 3 } }) + done;
  assert.strictEqual(await through_strip([in_choice]), in_choice);
  // a comment line and a stream cut without its last blank line pass as they are
  assert.strictEqual(await through_strip([": keep-alive\n\n", "data: {\"x\":1}"]), ": keep-alive\n\ndata: {\"x\":1}");
});

test("with_usage asks only for streamed requests the client did not ask for itself", () => {
  const plain = { model: "m", messages: [] };
  assert.deepStrictEqual(with_usage(plain), { body: plain, strip: false });
  const streamed = { model: "m", stream: true, stream_options: { other: 1 }, messages: [] };
  const asked = with_usage(streamed);
  assert.strictEqual(asked.strip, true);
  assert.deepStrictEqual(asked.body.stream_options, { other: 1, include_usage: true });
  assert.deepStrictEqual(streamed.stream_options, { other: 1 }, "the client's body is not touched");
  const own = { stream: true, stream_options: { include_usage: true } };
  assert.deepStrictEqual(with_usage(own), { body: own, strip: false });
});

// ———— window size and fill ————

test("window size: user > learned > provider > table > default, with the source named", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "loci-window-size-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  let settings = {};
  const clock = create_fake_clock(Date.parse("2026-10-08T00:00:00Z"));
  const make = () => create_context_windows({ data_root: root, read_settings: () => settings, clock });
  const cw = make();

  assert.deepStrictEqual(cw.resolve("some-unknown-model"), { tokens: DEFAULT_TOKENS, source: "default" });
  assert.deepStrictEqual(cw.resolve("deepseek-chat"), { tokens: 128000, source: "table" });
  assert.strictEqual(cw.note_models_list({ data: [{ id: "deepseek-chat", context_length: 65536 }, { id: "no-size" }] }), 1);
  assert.deepStrictEqual(cw.resolve("deepseek-chat"), { tokens: 65536, source: "provider" });
  assert.ok(cw.learn("deepseek-chat", 60000, "context_length_exceeded: maximum context length is 60000"));
  assert.deepStrictEqual(cw.resolve("deepseek-chat"), { tokens: 60000, source: "learned" });
  settings = { compress: { context_tokens: 32000 } };
  assert.deepStrictEqual(cw.resolve("deepseek-chat"), { tokens: 32000, source: "user" });
  settings = { compress: { context_tokens: null } };
  assert.deepStrictEqual(make().resolve("deepseek-chat"), { tokens: 60000, source: "learned" }, "kept on disk across a restart");
});

test("window size: provider list fields and the built-in table", () => {
  assert.deepStrictEqual(parse_models_list({ data: [
    { id: "a", context_length: 131072 },
    { id: "b", context_window: 200000 },
    { id: "c", top_provider: { context_length: 64000 } },
    { id: "d", max_model_len: 32768 },
    { id: "e", max_tokens: 8192 },
  ] }), { a: 131072, b: 200000, c: 64000, d: 32768 });
  assert.deepStrictEqual(parse_models_list({ models: [{ name: "models/gemini-2.5-pro", inputTokenLimit: 1048576 }] }), { "gemini-2.5-pro": 1048576 });
  assert.strictEqual(table_tokens("claude-sonnet-4-5"), 200000);
  assert.strictEqual(table_tokens("anthropic/claude-3.5-sonnet"), 200000);
  assert.strictEqual(table_tokens("gpt-4o-mini"), 128000);
  assert.strictEqual(table_tokens("gpt-4.1"), 1047576);
  assert.strictEqual(table_tokens("o3-mini"), 200000);
  assert.strictEqual(table_tokens("moonshot-v1-32k"), 32768);
  assert.strictEqual(table_tokens("gemini-1.5-pro"), 2097152);
  assert.strictEqual(table_tokens("glm-4.6"), 200000);
  assert.strictEqual(table_tokens("kimi-k2-0905-preview"), 262144);
  assert.strictEqual(table_tokens("qwen-plus"), 131072);
  assert.strictEqual(table_tokens("some-local-model"), null);
});

test("fill: prompt_tokens over the window; no usage → an estimate, marked", () => {
  const size = { tokens: 128000, source: "table" };
  const real = measure({ usage: { prompt_tokens: 74210 }, estimate: 1, window: size, model: "m", at: 5 });
  assert.deepStrictEqual(real, { prompt_tokens: 74210, estimated: false, context_tokens: 128000, context_source: "table",
                                 fill_pct: 58, model: "m", at: 5 });
  const est = estimate_prompt({ messages: [SYS, u("考完了！"), a("hello world, four chars a token")] });
  assert.ok(est > 0);
  const guessed = measure({ usage: null, estimate: est, window: size, model: "m", at: 5 });
  assert.strictEqual(guessed.estimated, true);
  assert.strictEqual(guessed.prompt_tokens, est);
  assert.strictEqual(guessed.fill_pct, Math.round((est * 1000) / 128000) / 10);
});
