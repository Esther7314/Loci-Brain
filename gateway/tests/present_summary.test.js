// ============================================================
// gateway/tests/present_summary.test.js — the summary block filter, the reminders and the
// window he folds himself, in-process
//
// Drives present/stream_filter.js, compress.js and window.js directly. The black-box
// version (the real gateway, a fake upstream, a fake Loci) is present_compress.test.js.
//
// Nothing here touches the network (the fence is loaded anyway) or the disk.
// ============================================================

const { test } = require("node:test");
const assert = require("node:assert");
const { Readable } = require("node:stream");

require("./network_fence.js");
const { to_said } = require("../present/threads.js");
const win = require("../present/window.js");
const compress = require("../present/compress.js");
const { CARDS, compress_self_shell } = require("../present/prompts.js");
const {
  create_summary_scanner, split_summary, create_sse_filter, create_json_filter, OPEN, CLOSE,
} = require("../present/stream_filter.js");

const SENTINEL = "哨兵·SUMMARY-91b4-不许出门";
const u = (content) => ({ role: "user", content });
const a = (content) => ({ role: "assistant", content });

async function through(filter, chunks) {
  const out = [];
  const s = Readable.from(chunks.map((c) => Buffer.from(c))).pipe(filter);
  for await (const c of s) out.push(c);
  return Buffer.concat(out).toString("utf8");
}

function every_byte(text) {
  const bytes = Buffer.from(text);
  const out = [];
  for (let i = 0; i < bytes.length; i++) out.push(bytes.subarray(i, i + 1));
  return out;
}

const ev = (o) => `data: ${JSON.stringify(o)}\n\n`;
const piece = (content) => ev({ id: "c1", object: "chat.completion.chunk", choices: [{ index: 0, delta: { content } }] });
const stop = ev({ id: "c1", object: "chat.completion.chunk", choices: [{ index: 0, delta: {}, finish_reason: "stop" }] });
const DONE = "data: [DONE]\n\n";

function sse_of(text, size) {
  let body = ev({ id: "c1", choices: [{ index: 0, delta: { role: "assistant" } }] });
  for (let i = 0; i < text.length; i += size) body += piece(text.slice(i, i + size));
  return body + stop + DONE;
}

/** The text a client reassembles from an SSE body. */
function text_of_sse(body) {
  let text = "";
  for (const block of body.split(/\r?\n\r?\n/)) {
    const line = block.split(/\r?\n/).find((l) => l.startsWith("data:"));
    if (!line) continue;
    const data = line.slice(5).trim();
    if (data === "[DONE]") continue;
    const c = JSON.parse(data);
    for (const ch of c.choices || []) if (typeof ch.delta?.content === "string") text += ch.delta.content;
  }
  return text;
}

// ———— the scanner ————

test("scanner: a block at a line start is taken out and captured; the rest passes as it is", () => {
  const text = `好，我收一下。\n${OPEN}ta 今天考完试，很累。${SENTINEL}${CLOSE}\n明天见。`;
  const got = split_summary(text);
  assert.strictEqual(got.visible, "好，我收一下。\n\n明天见。");
  assert.strictEqual(got.summary, `ta 今天考完试，很累。${SENTINEL}`);
  assert.strictEqual(got.half, false);
  assert.deepStrictEqual(split_summary(`${OPEN}开头就写${CLOSE}`), { visible: "", summary: "开头就写", half: false },
    "the very start of the reply is a line start");
});

test("scanner: a near miss or a mention is released whole, and captures nothing", () => {
  for (const text of [
    "第一行\n【窗口不是摘要】", "【窗口摘", "\n【", "【【窗口摘要】x", "窗口摘要这几个字", "我在写窗口摘要】",
    `我说的「${OPEN}」不是记号`, `用 \`${OPEN}\` 包起来`, `书名号《${OPEN}》`, `那个${OPEN}」是什么`,
    `用${OPEN}…${CLOSE}包起来`, `空的${OPEN}${CLOSE}也一样`, `${OPEN} …… ${CLOSE}`, `收尾的${CLOSE}单独出现`,
  ]) {
    const got = split_summary(text);
    assert.strictEqual(got.visible, text, JSON.stringify(text));
    assert.deepStrictEqual([got.summary, got.half], [null, false], JSON.stringify(text));
  }
});

test("🔴 scanner: the opening marker counts anywhere — mid-sentence, after spaces, inside markdown", () => {
  const cases = [
    [`好的。${OPEN}ta 累了${SENTINEL}${CLOSE}晚安。`, "好的。晚安。"],
    [`好的。 ${OPEN}ta 累了${SENTINEL}${CLOSE}`, "好的。 "],
    [` ${OPEN}ta 累了${SENTINEL}${CLOSE}\n晚安。`, "\n晚安。"],
    [`嗯\n**${OPEN}**\nta 累了${SENTINEL}\n**${CLOSE}**\n晚安。`, "嗯\n\n晚安。"],
    [`嗯\n> ${OPEN}ta 累了${SENTINEL}${CLOSE}\n晚安。`, "嗯\n\n晚安。"],
    [`嗯\n### ${OPEN}\nta 累了${SENTINEL}\n${CLOSE} \n晚安。`, "嗯\n\n晚安。"],
    [`嗯\n　- ${OPEN}ta 累了${SENTINEL}${CLOSE}`, "嗯\n"],
  ];
  for (const [text, visible] of cases) {
    const got = split_summary(text);
    assert.strictEqual(got.visible, visible, JSON.stringify(text));
    assert.strictEqual(got.summary, `ta 累了${SENTINEL}`, JSON.stringify(text));
    for (let size = 1; size <= 5; size++) {
      const s = create_summary_scanner();
      let out = "";
      for (let i = 0; i < text.length; i += size) out += s.push(text.slice(i, i + size));
      out += s.finish();
      assert.strictEqual(out, visible, `${JSON.stringify(text)} cut every ${size}`);
    }
  }
  // decoration that turns out not to belong to a marker is released as it was
  assert.strictEqual(split_summary("- 一\n**粗** 体\n> 引用\n  缩进").visible, "- 一\n**粗** 体\n> 引用\n  缩进");
  assert.strictEqual(split_summary(`x\n${OPEN}a${CLOSE}** 后面还有话`).visible, "x\n** 后面还有话");
  // mid-sentence and never closed: dropped, never captured
  const half = split_summary(`好的。${OPEN}写到一半${SENTINEL}`);
  assert.deepStrictEqual(half, { visible: "好的。", summary: null, half: true });
});

test("scanner: a block that never closes is dropped and counted as half, never captured", () => {
  const got = split_summary(`好的\n${OPEN}写到一半${SENTINEL}`);
  assert.strictEqual(got.visible, "好的\n");
  assert.strictEqual(got.summary, null);
  assert.strictEqual(got.half, true);
});

test("scanner: the output does not depend on how the text is cut", () => {
  const text = `前面的话\n${OPEN}第一段${SENTINEL}${CLOSE}中间\n${OPEN}第二段${CLOSE}\n【窗口外\n后面`;
  const whole = split_summary(text);
  assert.strictEqual(whole.visible, "前面的话\n中间\n\n【窗口外\n后面");
  assert.strictEqual(whole.summary, `第一段${SENTINEL}\n\n第二段`);
  for (let size = 1; size <= 7; size++) {
    const s = create_summary_scanner();
    let out = "";
    for (let i = 0; i < text.length; i += size) out += s.push(text.slice(i, i + size));
    out += s.finish();
    assert.strictEqual(out, whole.visible, `cut every ${size}`);
    assert.deepStrictEqual(s.result(), { summary: whole.summary, half: false });
  }
});

// ———— SSE ————

test("SSE filter: the block never reaches the client, cut every way, inside multi-byte characters too", async () => {
  const text = `考完了就好好歇着。\n${OPEN}ta 累了，想睡。${SENTINEL}${CLOSE}\n晚安。`;
  for (const size of [1, 2, 3, 5, 40]) {
    const body = sse_of(text, size);
    for (const chunks of [[body], every_byte(body)]) {
      const scanner = create_summary_scanner();
      const out = await through(create_sse_filter({ scanner }), chunks);
      assert.ok(!out.includes("SUMMARY-91b4"), `pieces of ${size}`);
      assert.ok(!out.includes(OPEN) && !out.includes("窗口摘要"));
      assert.strictEqual(text_of_sse(out), "考完了就好好歇着。\n\n晚安。");
      assert.ok(out.endsWith(DONE));
      assert.deepStrictEqual(scanner.result(), { summary: `ta 累了，想睡。${SENTINEL}`, half: false });
    }
  }
});

test("SSE filter: events it does not touch go out as the bytes that came in", async () => {
  const body = sse_of("没有摘要的一句话，\n第二行也没有。", 4);
  assert.strictEqual(await through(create_sse_filter(), every_byte(body)), body);
  const quoted = sse_of(`我说的「${OPEN}」不是记号`, 3);
  const out = await through(create_sse_filter(), [quoted]);
  assert.strictEqual(out, quoted, "a quoted marker is a mention: every byte as sent");
  const crlf = sse_of("一句。", 2).replace(/\n/g, "\r\n");
  assert.strictEqual(await through(create_sse_filter(), [crlf]), crlf);
});

test("SSE filter: text held back as a possible marker is released when it is not one", async () => {
  const body = sse_of("第一行\n【窗口外面下雨了", 1);
  const out = await through(create_sse_filter(), [body]);
  assert.strictEqual(text_of_sse(out), "第一行\n【窗口外面下雨了");
  // held back right up to the end of the answer, then released before [DONE]
  const tail = sse_of("第一行\n【窗口摘", 2);
  const out2 = await through(create_sse_filter(), [tail]);
  assert.strictEqual(text_of_sse(out2), "第一行\n【窗口摘");
  assert.ok(out2.endsWith(DONE));
});

test("SSE filter: a stream cut inside the block, with or without a half event, leaks nothing", async () => {
  const text = `嗯。\n${OPEN}写到这里${SENTINEL}还没写完`;
  const body = sse_of(text, 3);
  const cut_at = body.indexOf("还没");
  for (const end of [body.lastIndexOf("data:", cut_at), cut_at + 2, cut_at + 10]) {
    const scanner = create_summary_scanner();
    const out = await through(create_sse_filter({ scanner }), every_byte(body.slice(0, end)));
    assert.ok(!out.includes("SUMMARY-91b4") && !out.includes("91b4"), `cut at ${end}`);
    assert.strictEqual(text_of_sse(out.replace(/data: \{[^\n]*$/, "")), "嗯。\n");
    assert.strictEqual(scanner.result().summary, null, "a half block is never captured");
    assert.strictEqual(scanner.result().half, true);
  }
});

/** Every text a client could read from an SSE body, any field, any choice, logprobs included. */
function all_text_of_sse(body) {
  let text = "";
  for (const block of body.split(/\r?\n\r?\n/)) {
    const line = block.split(/\r?\n/).find((l) => l.startsWith("data:"));
    if (!line || line.slice(5).trim() === "[DONE]") continue;
    text += JSON.stringify(JSON.parse(line.slice(5).trim()));
  }
  return text;
}

test("🔴 SSE filter: a block written mid-sentence or in markdown never reaches the client, split anywhere", async () => {
  for (const text of [`好的。${OPEN}ta 累了${SENTINEL}${CLOSE}晚安。`, `好的。\n**${OPEN}**\nta 累了${SENTINEL}\n**${CLOSE}**\n晚安。`]) {
    for (const size of [1, 2, 4, 40]) {
      const body = sse_of(text, size);
      for (const chunks of [[body], every_byte(body)]) {
        const scanner = create_summary_scanner();
        const out = await through(create_sse_filter({ scanner }), chunks);
        assert.ok(!out.includes("91b4") && !out.includes("窗口摘要"), `pieces of ${size}`);
        assert.strictEqual(text_of_sse(out).replace(/\n+/g, "\n"), "好的。" + (text.includes("**") ? "\n" : "") + "晚安。");
        assert.strictEqual(scanner.result().summary, `ta 累了${SENTINEL}`);
      }
    }
  }
});

test("🔴 SSE filter: thinking fields, message-shaped chunks and every other choice are scanned too, never captured", async () => {
  const chunk = (choices) => ev({ id: "c1", object: "chat.completion.chunk", choices });
  const pieces = (s, size) => { const out = []; for (let i = 0; i < s.length; i += size) out.push(s.slice(i, i + size)); return out; };
  const thought = `先想想。\n${OPEN}思考里的${SENTINEL}${CLOSE}\n想好了。`;
  const said = `嗯。${OPEN}别的选项里的${SENTINEL}${CLOSE}`;
  let body = "";
  for (const p of pieces(thought, 2)) body += chunk([{ index: 0, delta: { reasoning_content: p } }]);
  for (const p of pieces(thought, 3)) body += chunk([{ index: 0, delta: { reasoning: p } }]);
  for (const p of pieces(said, 2)) body += chunk([{ index: 0, delta: { content: p } }, { index: 1, delta: { content: p } }]);
  for (const p of pieces(said, 5)) body += chunk([{ index: 2, message: { role: "assistant", content: p } }]);
  body += chunk([{ index: 3, delta: { content: said }, logprobs: { content: [{ token: SENTINEL, logprob: -0.1 }] } }]);
  body += chunk([0, 1, 2, 3].map((index) => ({ index, delta: {}, finish_reason: "stop" }))) + DONE;
  for (const chunks of [[body], every_byte(body)]) {
    const scanner = create_summary_scanner();
    const out = await through(create_sse_filter({ scanner }), chunks);
    assert.ok(!all_text_of_sse(out).includes("91b4"), "no field of any choice carries the block");
    assert.ok(!out.includes("窗口摘要"));
    assert.strictEqual(scanner.result().summary, `别的选项里的${SENTINEL}`, "only choice 0's content is the reply");
    let reasoning = "";
    let reasoning2 = "";
    for (const block of out.split("\n\n")) {
      if (!block.startsWith("data: {")) continue;
      for (const c of JSON.parse(block.slice(6)).choices) {
        if (typeof c.delta?.reasoning_content === "string") reasoning += c.delta.reasoning_content;
        if (typeof c.delta?.reasoning === "string") reasoning2 += c.delta.reasoning;
      }
    }
    assert.strictEqual(reasoning, "先想想。\n\n想好了。");
    assert.strictEqual(reasoning2, "先想想。\n\n想好了。");
  }
  // a JSON answer: every choice, the thinking beside the content too
  const reply = JSON.stringify({ id: "x", object: "chat.completion", choices: [
    { index: 0, message: { role: "assistant", content: said, reasoning_content: thought }, finish_reason: "stop" },
    { index: 1, message: { role: "assistant", content: [{ type: "text", text: said }] }, logprobs: { content: [{ token: SENTINEL }] } },
  ] });
  const scanner = create_summary_scanner();
  const out = await through(create_json_filter({ scanner }), every_byte(reply));
  assert.ok(!out.includes("91b4") && !out.includes("窗口摘要"));
  const got = JSON.parse(out);
  assert.deepStrictEqual([got.choices[0].message.content, got.choices[0].message.reasoning_content, got.choices[1].message.content[0].text],
    ["嗯。", "先想想。\n\n想好了。", "嗯。"]);
  assert.strictEqual(got.choices[1].logprobs, null);
  assert.strictEqual(scanner.result().summary, `别的选项里的${SENTINEL}`);
});

test("SSE filter: the usage chunk is still stripped when asked, and the summary filter can be off", async () => {
  const usage = ev({ choices: [], usage: { prompt_tokens: 10 } });
  const body = piece(`a\n${OPEN}x${CLOSE}`) + stop + usage + DONE;
  const out = await through(create_sse_filter({ strip_usage: true }), [body]);
  assert.ok(!out.includes("prompt_tokens"));
  assert.strictEqual(text_of_sse(out), "a\n");
  const kept = await through(create_sse_filter({ strip_usage: false }), [body]);
  assert.ok(kept.includes("prompt_tokens"));
});

// ———— plain JSON ————

test("JSON filter: the block is taken out of the message; an untouched answer is the same bytes", async () => {
  const reply = (content) => JSON.stringify({ id: "x", object: "chat.completion",
    choices: [{ index: 0, message: { role: "assistant", content }, finish_reason: "stop" }], usage: { prompt_tokens: 5 } });
  const scanner = create_summary_scanner();
  const out = await through(create_json_filter({ scanner }), every_byte(reply(`好。\n${OPEN}${SENTINEL}${CLOSE}\n嗯`)));
  assert.ok(!out.includes("91b4"));
  assert.strictEqual(JSON.parse(out).choices[0].message.content, "好。\n\n嗯");
  assert.strictEqual(scanner.result().summary, SENTINEL);
  const plain = reply("没有摘要。");
  assert.strictEqual(await through(create_json_filter(), [plain]), plain);
  // a provider that escapes every non-ASCII character
  const escaped = reply(`好。\n${OPEN}${SENTINEL}${CLOSE}`).replace(/[^\x00-\x7f]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`);
  const out2 = await through(create_json_filter(), [escaped]);
  assert.strictEqual(JSON.parse(out2).choices[0].message.content, "好。\n");
  // cut short: whatever it is, nothing from the marker on goes out
  const cut = reply(`好。\n${OPEN}${SENTINEL}`).slice(0, -30);
  const out3 = await through(create_json_filter(), [cut]);
  assert.ok(!out3.includes("91b4") && !out3.includes("【"));
  const cut_escaped = escaped.slice(0, -30);
  assert.ok(!(await through(create_json_filter(), [cut_escaped])).toLowerCase().includes("\\u3010"));
});

// ———— reminders ————

const VALUES = { on: true, weak_pct: [65], ask_pct: 75, keep_raw: 40, force_pct: 85 };

test("due_line: one line per crossing, the highest when several are crossed at once", () => {
  assert.strictEqual(compress.due_line({ fill_pct: null, values: VALUES }), null);
  assert.strictEqual(compress.due_line({ fill_pct: 64.9, values: VALUES }), null);
  assert.deepStrictEqual(compress.due_line({ fill_pct: 65, values: VALUES }), { line: "weak", covers: ["weak:65"] });
  assert.strictEqual(compress.due_line({ fill_pct: 70, values: VALUES, offered: ["weak:65"] }), null);
  assert.deepStrictEqual(compress.due_line({ fill_pct: 75, values: VALUES, offered: ["weak:65"] }), { line: "ask", covers: ["ask"] });
  assert.deepStrictEqual(compress.due_line({ fill_pct: 90, values: VALUES }), { line: "ask", covers: ["weak:65", "ask"] });
  assert.strictEqual(compress.due_line({ fill_pct: 99, values: VALUES, offered: ["weak:65", "ask"] }), null);
  const two = { ...VALUES, weak_pct: [50, 65] };
  assert.deepStrictEqual(compress.due_line({ fill_pct: 66, values: two, offered: ["weak:50"] }), { line: "weak", covers: ["weak:65"] });
});

test("offer_reminder: at her line's tail after the card, once per line per window, never while off", () => {
  const h = [u("一"), a("一的回话，长一点的一句。"), u("二")];
  const t = { id: "t_aaaaaa", branch: h.map((m, i) => ({ id: `m_${i + 1}`, role: m.role, fp: to_said(m).fp })) };
  const w = win.ensure(t, 0);
  w.usage = { fill_pct: 70 };
  assert.strictEqual(compress.offer_reminder({ w, turn: "m_3", values: { ...VALUES, on: false }, card: () => "卡" }), null);
  assert.deepStrictEqual(w.offered, []);

  const o = compress.offer_reminder({ w, turn: "m_3", values: VALUES, card: () => CARDS.compress.text });
  assert.strictEqual(o.text, compress_self_shell({ card: CARDS.compress.text, keep_raw: 40, line: "weak" }));
  assert.deepStrictEqual(w.offered, ["weak:65"]);
  // the card for this turn arrives after the reminder was made (a retried cue): the reminder still goes last
  win.add_overlay(w, { kind: "cue", anchor: "m_3", place: "after", text: "〔相关记忆〕卡", turn: "m_3" });
  const built = win.assemble(h, win.map_ids(h, t.branch, "m_3"), w);
  assert.deepStrictEqual(built.messages.slice(-3).map((m) => m.content), ["二", "〔相关记忆〕卡", o.text]);

  assert.strictEqual(compress.offer_reminder({ w, turn: "m_3", values: VALUES, card: () => "卡" }), null, "a resend replays, no second one");
  assert.strictEqual(compress.offer_reminder({ w, turn: "m_5", values: VALUES, card: () => "卡" }), null, "the weak line is spent");
  w.usage = { fill_pct: 80 };
  const ask = compress.offer_reminder({ w, turn: "m_5", values: VALUES, card: () => "改过的卡" });
  assert.strictEqual(ask.text, compress_self_shell({ card: "改过的卡", keep_raw: 40, line: "ask" }));
  assert.deepStrictEqual(w.offered, ["weak:65", "ask"]);
});

// ———— the flip ————

test("mark_for_tail: the last keep_raw lines, moved onto a line of hers", () => {
  const b = (roles) => roles.split("").map((r, i) => ({ id: `m_${i + 1}`, role: r === "u" ? "user" : "assistant" }));
  assert.strictEqual(win.mark_for_tail(b("uauaua"), 3), "m_5", "the tail starts on a reply: moved forward to her line");
  assert.strictEqual(win.mark_for_tail(b("uauaua"), 4), "m_3");
  assert.strictEqual(win.mark_for_tail(b("uauaua"), 40), "m_1");
  assert.strictEqual(win.mark_for_tail(b("uaaaaa"), 2), "m_1", "no line of hers in the tail: back to her nearest");
  assert.strictEqual(win.mark_for_tail(b("aaa"), 2), null);
});

test("self_flip: carry = previous report part + new summary; fill, offered and overlays cleared; opened_by self", () => {
  const t = { id: "t_bbbbbb", branch: "uauaua".split("").map((r, i) => ({ id: `m_${i + 1}`, role: r === "u" ? "user" : "assistant" })) };
  const w = win.ensure(t, 0);
  w.carry = "旧的";
  w.carry_parts = { report: "昨天的日报正文", summary: "上一次的摘要" };
  w.offered = ["weak:65", "ask"];
  w.usage = { fill_pct: 80, prompt_tokens: 8000 };
  w.model = "m";
  win.add_overlay(w, { kind: "cue", anchor: "m_5", place: "after", text: "卡", turn: "m_5" });
  const flip = compress.self_flip({ thread: t, summary: `新的摘要${SENTINEL}`, keep_raw: 3, at: "2026-10-07T21:00:00+08:00", now: 9 });
  assert.deepStrictEqual(flip, { old_name: "t_bbbbbb#w1", name: "t_bbbbbb#w2", mark: "m_5" });
  const n = t.window;
  assert.strictEqual(n.no, 2);
  assert.strictEqual(n.mark, "m_5");
  assert.deepStrictEqual(n.carry_parts, { report: "昨天的日报正文", summary: `新的摘要${SENTINEL}` });
  assert.strictEqual(n.carry, win.compose_carry({ report: "昨天的日报正文", summary: `新的摘要${SENTINEL}` }));
  assert.ok(n.carry.indexOf("昨天的日报正文") < n.carry.indexOf("新的摘要"), "the report first, then the summary");
  assert.ok(!n.carry.includes("上一次的摘要"), "the old summary is replaced");
  assert.ok(!n.carry.includes(OPEN) && !n.carry.includes(CLOSE), "the carry never carries the markers");
  assert.deepStrictEqual([n.offered, n.overlays, n.usage], [[], [], null]);
  assert.strictEqual(n.model, "m");
  assert.deepStrictEqual(n.opened_by, { at: "2026-10-07T21:00:00+08:00", how: "self" });

  // a window with no report part: the carry is the summary alone
  const t2 = { id: "t_cccccc", branch: [{ id: "m_1", role: "user" }] };
  win.ensure(t2, 0).carry = "种进来的一段（没有 parts）";
  compress.self_flip({ thread: t2, summary: "只有摘要", keep_raw: 40, at: "x", now: 1 });
  assert.deepStrictEqual(t2.window.carry_parts, { report: null, summary: "只有摘要" });
  assert.ok(!t2.window.carry.includes("〔上一份日报〕"));
  assert.strictEqual(win.compose_carry({}), null);
});

test("a fork keeps a reminder line offered only when the reminder came along", () => {
  const parent = { id: "t_parent", branch: "uaua".split("").map((r, i) => ({ id: `m_${i + 1}`, role: r === "u" ? "user" : "assistant" })) };
  const w = win.ensure(parent, 0);
  w.usage = { fill_pct: 70 };
  compress.offer_reminder({ w, turn: "m_3", values: VALUES, card: () => "卡" });
  const keeps = { id: "t_fork01", branch: parent.branch.slice(0, 3) };
  win.inherit(keeps, parent, 1);
  assert.deepStrictEqual(keeps.window.offered, ["weak:65"]);
  const loses = { id: "t_fork02", branch: parent.branch.slice(0, 2) };
  win.inherit(loses, parent, 1);
  assert.deepStrictEqual(loses.window.offered, [], "the reminder is not on this branch: it can be offered again");
});
