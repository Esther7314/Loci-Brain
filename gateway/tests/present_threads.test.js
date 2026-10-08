// ============================================================
// gateway/tests/present_threads.test.js — the day store and the thread rules, in-process
//
// These drive present/day_store.js and present/threads.js directly with a fake clock and
// a fresh temp directory per test, playing the client: each call hands over the whole
// history the way a chat client does, and the assertions read the day file back.
// The black-box version of the headline case (through the real gateway process) is in
// present_recording.test.js.
//
// Nothing here touches the network (the fence is loaded anyway) or any directory outside
// os.tmpdir().
// ============================================================

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

require("./network_fence.js");
const { create_fake_clock } = require("./fake_clock.js");
const { local_stamp } = require("../present/clock.js");
const { create_day_store } = require("../present/day_store.js");
const { create_threads, RESEND_WINDOW_MS } = require("../present/threads.js");
const { create_sse_reader, parse_json_reply } = require("../present/reply_capture.js");

const ZONE = "Asia/Shanghai";
const START = Date.parse("2026-10-07T12:41:05Z");   // 20:41:05 in Shanghai
const DAY = "2026-10-07";
const SYS = { role: "system", content: "你是一个温柔的伴侣。" };

function scene(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "loci-present-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const clock = create_fake_clock(START);
  const days_dir = path.join(root, "host", "days");
  const threads_dir = path.join(root, "threads");
  const make = () => {
    const day_store = create_day_store({ dir: days_dir, clock, zone: ZONE });
    const threads = create_threads({ dir: threads_dir, day_store, clock });
    return { day_store, threads };
  };
  const { day_store, threads } = make();
  return { root, clock, day_store, threads, days_dir, threads_dir, reopen: make };
}

const user = (content) => ({ role: "user", content });
const assistant = (content, tools) => (tools
  ? { role: "assistant", content, tool_calls: tools.map((name, i) => ({ id: `c${i}`, type: "function", function: { name, arguments: "{\"secret\":\"参数不许落盘\"}" } })) }
  : { role: "assistant", content });

/** One request: ingest the history, then (optionally) the reply finishing. */
function send(threads, history, reply) {
  const seen = threads.ingest(history);
  const done = reply === undefined ? null
    : threads.record_reply(seen, typeof reply === "string" ? { text: reply, tools: [] } : reply);
  return { seen, done };
}

const LONG = (n) => `这是第${n}轮的回话，模型说得很具体，不会跟别的对话里的哪一句重样。`;

// ————————————————————————————————————————————————————————————

test("local day: the owner's zone decides the date, the id, and the offset in `at`", () => {
  assert.deepStrictEqual(local_stamp(START, ZONE),
    { day: "2026-10-07", compact: "20261007", iso: "2026-10-07T20:41:05+08:00" });
  assert.deepStrictEqual(local_stamp(START, "America/New_York"),
    { day: "2026-10-07", compact: "20261007", iso: "2026-10-07T08:41:05-04:00" });
  // 16:30 UTC is already the 8th in Shanghai
  assert.strictEqual(local_stamp(Date.parse("2026-10-07T16:30:00Z"), ZONE).day, "2026-10-08");
});

test("day store: ids are born with the line, the line has exactly the blueprint's shape", (t) => {
  const { day_store, days_dir } = scene(t);
  const a = day_store.append_new({ thread: "t_aaaaaa", role: "user", text: "考完了！", attach: ["image"] });
  const b = day_store.append_new({ thread: "t_aaaaaa", role: "assistant", text: "恭喜！", tools: ["recall"] });
  assert.strictEqual(a.id, "m_20261007_0001");
  assert.strictEqual(b.id, "m_20261007_0002");
  const raw = fs.readFileSync(path.join(days_dir, `${DAY}.jsonl`), "utf8").trim().split("\n").map((l) => JSON.parse(l));
  assert.deepStrictEqual(raw[0], {
    id: "m_20261007_0001", rev: 1, at: "2026-10-07T20:41:05+08:00", thread: "t_aaaaaa",
    role: "user", text: "考完了！", attach: ["image"], tools: null, woke: false, state: "live",
  });
  assert.deepStrictEqual(Object.keys(raw[1]), ["id", "rev", "at", "thread", "role", "text", "attach", "tools", "woke", "state"]);
  assert.deepStrictEqual(raw[1].tools, ["recall"]);
});

test("day store: a revision goes to the file its id was born in, even on the next day", (t) => {
  const { day_store, clock, days_dir } = scene(t);
  const a = day_store.append_new({ thread: "t_aaaaaa", role: "assistant", text: "昨晚说的" });
  clock.advance(6 * 3600 * 1000);   // 02:41 on the 8th
  const r = day_store.revise(a.id, { state: "replaced" });
  assert.strictEqual(r.rev, 2);
  assert.strictEqual(r.at, "2026-10-08T02:41:05+08:00");
  assert.ok(!fs.existsSync(path.join(days_dir, "2026-10-08.jsonl")), "nothing about yesterday's id lands in today's file");
  assert.deepStrictEqual(day_store.read_day(DAY).map((l) => [l.id, l.rev, l.state]),
    [[a.id, 1, "live"], [a.id, 2, "replaced"]]);
  assert.strictEqual(day_store.get(a.id).state, "replaced");
  // and a new line today counts from 1 again
  assert.strictEqual(day_store.append_new({ thread: "t_aaaaaa", role: "user", text: "早" }).id, "m_20261008_0001");
});

test("day store: a torn last line is skipped and never glued to the next one; seq survives a restart", (t) => {
  const { day_store, days_dir, reopen } = scene(t);
  day_store.append_new({ thread: "t_aaaaaa", role: "user", text: "一" });
  fs.appendFileSync(path.join(days_dir, `${DAY}.jsonl`), "{\"id\":\"m_20261007_0002\",\"rev\":1,\"te");   // died mid-write
  const again = reopen().day_store;
  const next = again.append_new({ thread: "t_aaaaaa", role: "user", text: "二" });
  assert.strictEqual(next.id, "m_20261007_0002", "the torn line never became a line, so its seq is free");
  assert.deepStrictEqual(again.read_day(DAY).map((l) => l.text), ["一", "二"]);
});

test("headline: 3 rounds + a resend + a regenerate → 6 live + 1 replaced", (t) => {
  const { threads, day_store } = scene(t);
  const h1 = [SYS, user("你好")];
  const r1 = send(threads, h1, LONG(1));
  const h2 = [...h1, assistant(LONG(1)), user("今天考试了")];
  const r2 = send(threads, h2, LONG(2));
  const h3 = [...h2, assistant(LONG(2)), user("考完了！")];
  const first = send(threads, h3);                 // the upstream fails: no reply comes back
  const resend = send(threads, h3, LONG(3));       // the client sends the very same request again
  const regen = send(threads, h3, `${LONG(3)}（重新生成）`);

  assert.strictEqual(r2.seen.thread, r1.seen.thread);
  assert.strictEqual(first.seen.thread, r1.seen.thread);
  assert.strictEqual(resend.seen.resend, true);
  assert.deepStrictEqual(resend.seen.wrote, [], "a resend writes nothing");
  assert.strictEqual(resend.seen.turn, first.seen.turn, "a resend is the same turn");
  assert.strictEqual(regen.seen.turn, first.seen.turn, "a regenerate is the same turn too");
  assert.deepStrictEqual(regen.done.replaced, [resend.done.wrote]);

  const current = day_store.current(DAY);
  assert.strictEqual(current.filter((l) => l.state === "live").length, 6);
  assert.strictEqual(current.filter((l) => l.state === "replaced").length, 1);
  assert.deepStrictEqual(current.filter((l) => l.state === "live").map((l) => [l.role, l.text]), [
    ["user", "你好"], ["assistant", LONG(1)], ["user", "今天考试了"], ["assistant", LONG(2)],
    ["user", "考完了！"], ["assistant", `${LONG(3)}（重新生成）`],
  ]);
  // the raw history: the replaced version is its own revision, the old line stays
  assert.deepStrictEqual(day_store.read_day(DAY).map((l) => `${l.id}@${l.rev}:${l.state}`), [
    "m_20261007_0001@1:live", "m_20261007_0002@1:live", "m_20261007_0003@1:live", "m_20261007_0004@1:live",
    "m_20261007_0005@1:live", "m_20261007_0006@1:live", "m_20261007_0006@2:replaced", "m_20261007_0007@1:live",
  ]);
});

test("regenerate that comes back word for word the same writes nothing", (t) => {
  const { threads, day_store } = scene(t);
  const h1 = [SYS, user("你好")];
  send(threads, h1, LONG(1));
  const h2 = [...h1, assistant(LONG(1)), user("再说一遍")];
  send(threads, h2, LONG(2));
  const again = send(threads, h2, LONG(2));
  assert.strictEqual(again.done.wrote, null);
  assert.strictEqual(day_store.read_day(DAY).length, 4);
});

test("a new chat opening with the same words is its own thread; within two minutes the same request is a resend", (t) => {
  const { threads, clock, day_store } = scene(t);
  const a = send(threads, [SYS, user("你好")], LONG(1));
  const a2 = send(threads, [SYS, user("你好"), assistant(LONG(1)), user("在干嘛")], LONG(2));

  // within the window, the identical opening request: the same turn of chat A
  clock.advance(RESEND_WINDOW_MS - 1000);
  const retry = send(threads, [SYS, user("你好")]);
  assert.strictEqual(retry.seen.resend, true);
  assert.strictEqual(retry.seen.thread, a.seen.thread);

  // a different system prompt is a different assistant: never the same chat
  const other = send(threads, [{ role: "system", content: "你是翻译。" }, user("你好")]);
  assert.notStrictEqual(other.seen.thread, a.seen.thread);

  // past the window, the same opening is a brand-new chat
  clock.advance(RESEND_WINDOW_MS + 1000);
  const b = send(threads, [SYS, user("你好")], "你好呀，今天想聊点什么呢？我在这儿。");
  assert.notStrictEqual(b.seen.thread, a.seen.thread);
  assert.deepStrictEqual(b.seen.wrote.length, 1);
  const b2 = send(threads, [SYS, user("你好"), assistant("你好呀，今天想聊点什么呢？我在这儿。"), user("随便聊聊")], LONG(9));
  assert.strictEqual(b2.seen.thread, b.seen.thread, "chat B's second turn stays on B");
  // and chat A carries on where it was
  const a3 = send(threads, [SYS, user("你好"), assistant(LONG(1)), user("在干嘛"), assistant(LONG(2)), user("我回来了")]);
  assert.strictEqual(a3.seen.thread, a.seen.thread);
  assert.strictEqual(a2.seen.thread, a.seen.thread);
  const by_thread = {};
  for (const l of day_store.current(DAY)) (by_thread[l.thread] ||= []).push(l.text);
  assert.deepStrictEqual(by_thread[b.seen.thread], ["你好", "你好呀，今天想聊点什么呢？我在这儿。", "随便聊聊", LONG(9)]);
});

test("a short generic reply shared with another chat does not glue two chats together", (t) => {
  const { threads } = scene(t);
  const a = send(threads, [SYS, user("在吗")], "在的。");
  // a chat that was going on elsewhere before it came through the gateway, which happens
  // to contain the same two-line exchange
  const b = send(threads, [SYS, user("帮我看看这个"), assistant("好，发来吧，我仔细看看。"), user("在吗"), assistant("在的。"), user("继续")]);
  assert.notStrictEqual(b.seen.thread, a.seen.thread);
});

test("a thread born mid-chat (history from before the gateway) keeps its later turns", (t) => {
  const { threads } = scene(t);
  const old = [SYS, user("很早以前的话"), assistant("很早以前的回话，网关那时还没接上。")];
  const first = send(threads, [...old, user("现在换到网关了")], LONG(1));
  const second = send(threads, [...old, user("现在换到网关了"), assistant(LONG(1)), user("接得上吗")]);
  assert.strictEqual(second.seen.thread, first.seen.thread);
  assert.strictEqual(first.seen.wrote.length, 1, "history from before the gateway is not written as if it were heard now");
});

test("a fork leaves the original thread alone; both carry on", (t) => {
  const { threads, day_store } = scene(t);
  const h1 = [SYS, user("你好")];
  const r1 = send(threads, h1, LONG(1));
  const h2 = [...h1, assistant(LONG(1)), user("聊考试")];
  send(threads, h2, LONG(2));
  const fork = send(threads, [...h1, assistant(LONG(1)), user("不，聊电影")], LONG(3));
  assert.notStrictEqual(fork.seen.thread, r1.seen.thread);
  assert.strictEqual(fork.seen.forked_from, r1.seen.thread);
  // the original goes on as if nothing happened
  const back = send(threads, [...h2, assistant(LONG(2)), user("考得不错")]);
  assert.strictEqual(back.seen.thread, r1.seen.thread);
  // and the fork goes on on its own
  const on = send(threads, [...h1, assistant(LONG(1)), user("不，聊电影"), assistant(LONG(3)), user("哪部好看")]);
  assert.strictEqual(on.seen.thread, fork.seen.thread);
  assert.strictEqual(day_store.current(DAY).filter((l) => l.state === "replaced").length, 0, "a fork replaces nothing");
  // the fork reuses the shared lines instead of copying them
  assert.deepStrictEqual(threads.get(fork.seen.thread).branch.slice(0, 2).map((e) => e.id),
    threads.get(r1.seen.thread).branch.slice(0, 2).map((e) => e.id));
});

test("regenerating far back (the thread has moved on) forks instead of replacing", (t) => {
  const { threads, day_store } = scene(t);
  const h1 = [SYS, user("你好")];
  send(threads, h1, LONG(1));
  const h2 = [...h1, assistant(LONG(1)), user("第二句")];
  send(threads, h2, LONG(2));
  send(threads, [...h2, assistant(LONG(2)), user("第三句")], LONG(3));
  const deep = send(threads, h2, `${LONG(2)}（另一个版本）`);
  assert.deepStrictEqual(deep.done.replaced, []);
  assert.ok(deep.done.forked_from);
  assert.strictEqual(day_store.current(DAY).filter((l) => l.state === "replaced").length, 0);
});

test("tool turn: a request ending in a tool result writes no line; tools are kept by name only", (t) => {
  const { threads, day_store, days_dir } = scene(t);
  const h1 = [SYS, user("上周我们说到哪了")];
  const r1 = send(threads, h1, { text: "", tools: ["recall"] });
  const h2 = [...h1, assistant(null, ["recall"]), { role: "tool", tool_call_id: "c0", content: "工具结果，绝不落盘" }];
  const r2 = send(threads, h2, LONG(1));
  assert.strictEqual(r2.seen.kind, "continuation");
  assert.deepStrictEqual(r2.seen.wrote, []);
  assert.strictEqual(r2.seen.thread, r1.seen.thread);
  assert.strictEqual(r2.seen.turn, r1.seen.turn, "the turn is still the owner's line");
  assert.deepStrictEqual(day_store.current(DAY).map((l) => [l.role, l.text, l.tools]), [
    ["user", "上周我们说到哪了", null], ["assistant", "", ["recall"]], ["assistant", LONG(1), null],
  ]);
  const raw = fs.readFileSync(path.join(days_dir, `${DAY}.jsonl`), "utf8");
  assert.ok(!raw.includes("工具结果") && !raw.includes("参数不许落盘"), "no tool arguments, no tool results");
  // the next turn anchors on the final reply
  const r3 = send(threads, [...h2, assistant(LONG(1)), user("对，就是那个")]);
  assert.strictEqual(r3.seen.thread, r1.seen.thread);
  assert.strictEqual(r3.seen.wrote.length, 1);
});

test("attachments are recorded by kind; the image itself never reaches the file", (t) => {
  const { threads, days_dir } = scene(t);
  send(threads, [SYS, user([{ type: "text", text: "考完了！" }, { type: "image_url", image_url: { url: "data:image/png;base64,QUJDREVGRw==" } }])]);
  const line = JSON.parse(fs.readFileSync(path.join(days_dir, `${DAY}.jsonl`), "utf8").trim());
  assert.strictEqual(line.text, "考完了！");
  assert.deepStrictEqual(line.attach, ["image"]);
  assert.ok(!JSON.stringify(line).includes("base64"));
});

test("a reply the client sends back edited becomes a new revision of that line", (t) => {
  const { threads, day_store } = scene(t);
  const h1 = [SYS, user("你好")];
  const r1 = send(threads, h1, LONG(1));
  const h2 = [...h1, assistant(LONG(1)), user("第二句")];
  const r2 = send(threads, h2, LONG(2));
  const r3 = send(threads, [...h2, assistant("（被客户端改过的回话）"), user("嗯")]);
  assert.strictEqual(r3.seen.thread, r1.seen.thread, "the earlier reply still anchors the chat");
  assert.deepStrictEqual(r3.seen.revised, [r2.done.wrote]);
  assert.strictEqual(r3.seen.wrote.length, 1);
  const line = day_store.get(r2.done.wrote);
  assert.strictEqual(line.rev, 2);
  assert.strictEqual(line.state, "live");
  assert.strictEqual(line.text, "（被客户端改过的回话）");
});

test("a thread file that will not parse is never matched and never overwritten", (t) => {
  const { threads_dir, reopen } = scene(t);
  fs.mkdirSync(threads_dir, { recursive: true });
  fs.writeFileSync(path.join(threads_dir, "t_bad000.json"), "{ not json");
  const { threads } = reopen();
  const r = send(threads, [SYS, user("你好")], LONG(1));
  assert.notStrictEqual(r.seen.thread, "t_bad000");
  assert.deepStrictEqual(threads.broken(), ["t_bad000"]);
  assert.strictEqual(fs.readFileSync(path.join(threads_dir, "t_bad000.json"), "utf8"), "{ not json");
});

test("threads survive a restart: the ledger on disk is enough to carry on", (t) => {
  const { threads, reopen } = scene(t);
  const h1 = [SYS, user("你好")];
  const r1 = send(threads, h1, LONG(1));
  const after = reopen().threads;
  const r2 = send(after, [...h1, assistant(LONG(1)), user("我重启回来了")]);
  assert.strictEqual(r2.seen.thread, r1.seen.thread);
  assert.strictEqual(r2.seen.wrote[0], "m_20261007_0003");
});

test("requests that are not a turn write nothing", (t) => {
  const { threads, days_dir } = scene(t);
  const r = threads.ingest([SYS, assistant("前缀续写")]);
  assert.strictEqual(r.kind, "ignored");
  assert.ok(!fs.existsSync(days_dir));
});

test("reply capture: SSE pieces reassemble, unfinished streams and error bodies yield nothing", () => {
  const sse = create_sse_reader();
  sse.push("data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"你\"}}]}\n\nda");
  sse.push("ta: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"好\",\"tool_calls\":[{\"index\":0,\"function\":{\"name\":\"rec\"}}]}}]}\n\n");
  sse.push("data: {\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"function\":{\"name\":\"all\",\"arguments\":\"{}\"}}]}}]}\n\n");
  sse.push("data: {\"choices\":[{\"index\":1,\"delta\":{\"content\":\"别的选项\"}}]}\n\n");
  sse.push("data: [DONE]\n\n");
  assert.deepStrictEqual(sse.end(), { text: "你好", tools: ["recall"] });

  const cut = create_sse_reader();
  cut.push("data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"半截\"}}]}\n\n");
  assert.strictEqual(cut.end(), null, "a stream that never finished is not a reply");

  assert.strictEqual(parse_json_reply("{\"error\":{\"message\":\"bad\"}}"), null);
  assert.deepStrictEqual(parse_json_reply("{\"choices\":[{\"index\":0,\"message\":{\"content\":\"嗯\"}}]}"), { text: "嗯", tools: [] });
});
