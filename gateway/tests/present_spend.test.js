// ============================================================
// gateway/tests/present_spend.test.js — no paid call the gateway repeats when it cannot
// succeed or cannot help, and no single bad turn that degrades it for good
//
// Three ways the present layer used to spend her money, each reproduced first:
//   · a forced pack that cannot bring the window under the force line (the client's
//     system prompt, the carry and keep_raw lines already over it) was started again on
//     every finished turn (pack.js: now predicted and held back, said once)
//   · a wall error naming no limit made the last successful prompt_tokens the window,
//     forever and above everything but her own number (context_window.js / wall.js: a
//     guess only when the size in use cannot explain the refusal, lapsing after a day,
//     never over a named limit, raised by any bigger turn that goes through)
//   · after the wall's cut-down resend went through, the wake snapshot was still the
//     refused request, so every wake went out over the wall (wall.js replaces ctx.sent and
//     its layout); and a wake refused as too long was repeated as it was (wake.js drops the
//     earlier wake pairs, then stops on that snapshot, said once)
// In-process, behind the network fence, on fake clocks. Temp directories only. Fixtures
// say 「ta」.
// ============================================================

const { test, after } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const fence = require("./network_fence.js");
const { create_fake_clock } = require("./fake_clock.js");

const { create_context_windows, GUESS_TTL_MS } = require("../present/context_window.js");
const { create_wall_escape } = require("../present/wall.js");
const { create_packer } = require("../present/pack.js");
const { create_wake, WHY_WORDS } = require("../present/wake.js");
const { create_settings } = require("../present/settings.js");
const { create_prompts } = require("../present/prompts.js");
const { estimate_prompt } = require("../present/fill.js");
const { create_present } = require("../present/index.js");
const { create_relay } = require("../relay.js");

const ZONE = "Asia/Shanghai";
const MIN = 60 * 1000;
const at = (local) => Date.parse(`${local}+08:00`);
const root = fs.mkdtempSync(path.join(os.tmpdir(), "loci-spend-"));
let n = 0;
const fresh_dir = () => { const d = path.join(root, `d${++n}`); fs.mkdirSync(d, { recursive: true }); return d; };
after(() => fs.rmSync(root, { recursive: true, force: true }));

const log_lines = (dir) => {
  const f = path.join(dir, "logs", "present.jsonl");
  return fs.existsSync(f) ? fs.readFileSync(f, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
};
const wall_body = (extra = "") => JSON.stringify({ error: { code: "context_length_exceeded", message: `The input is too long for this model.${extra}` } });

// ———— finding 4: what a wall teaches about the window ————

test("a last_ok guess lapses after a day; a limit the error named does not", () => {
  const clock = create_fake_clock(at("2026-10-08T10:00:00"));
  const cw = create_context_windows({ data_root: fresh_dir(), clock });
  assert.ok(cw.learn("deepseek-chat", 5000, "last_ok"));
  assert.deepStrictEqual(cw.resolve("deepseek-chat"), { tokens: 5000, source: "learned" });
  clock.advance(GUESS_TTL_MS + MIN);
  assert.deepStrictEqual(cw.resolve("deepseek-chat"), { tokens: 128000, source: "table" }, "the guess lapsed: the table again");
  assert.ok(cw.learn("deepseek-chat", 60000, "error"));
  clock.advance(30 * GUESS_TTL_MS);
  assert.deepStrictEqual(cw.resolve("deepseek-chat"), { tokens: 60000, source: "learned" }, "a named limit stays");
});

test("a guess never replaces a limit the error named; a turn that goes through with more raises a learned size", () => {
  const clock = create_fake_clock(at("2026-10-08T10:00:00"));
  const cw = create_context_windows({ data_root: fresh_dir(), clock });
  assert.ok(cw.learn("m-a", 60000, "error"));
  assert.strictEqual(cw.learn("m-a", 5000, "last_ok"), false);
  assert.deepStrictEqual(cw.resolve("m-a"), { tokens: 60000, source: "learned" });

  assert.ok(cw.learn("m-b", 5000, "last_ok"));
  assert.strictEqual(cw.note_ok("m-b", 4000), false, "a smaller turn proves nothing");
  assert.ok(cw.note_ok("m-b", 9000));
  assert.deepStrictEqual(cw.resolve("m-b"), { tokens: 9000, source: "learned" });
  assert.strictEqual(cw.note_ok("never-learned", 9000), false, "nothing learned, nothing to raise");
});

/** A wall escape over a real window store, with the thread ledger and the resend faked. */
function wall_rig({ model, force_pct = 85, on = false } = {}) {
  const dir = fresh_dir();
  const clock = create_fake_clock(at("2026-10-08T10:00:00"));
  const windows = create_context_windows({ data_root: dir, clock });
  const thread = { id: "t_wall01", window: { name: "w_1" } };
  const asked = [];
  const wall = create_wall_escape({
    windows, clock, zone: ZONE, data_root: dir, log: () => {},
    threads: { get: () => thread, save: () => true },
    read_compress: () => ({ on, force_pct }),
    request_pack: (id, hint) => asked.push({ id, hint }),
  });
  const sent = [];
  const send = async (body) => { sent.push(body); return new Response("{}", { status: 200 }); };
  return { dir, clock, windows, wall, thread, asked, sent, send, model };
}

const turns = (count, chars) => {
  const out = [{ role: "system", content: "你是 ta 的伴。" }];
  for (let i = 0; i < count; i++) {
    out.push({ role: "user", content: `第${i}句${"话".repeat(chars)}` }, { role: "assistant", content: `回${i}${"嗯".repeat(chars)}` });
  }
  out.push({ role: "user", content: "最后一句" });
  return out;
};

test("🔴 one long paste on a vague wall error: the window in use already explains it, nothing is learned", async () => {
  const r = wall_rig();
  r.wall.note_usage("deepseek-chat", { prompt_tokens: 5000 }, 5000);
  const messages = turns(2, 10);
  messages[3] = { role: "user", content: "看看这个：" + "长".repeat(300000) };   // her paste, now history
  const ctx = { thread: "t_wall01", window: "w_1", model: "deepseek-chat", forward: { messages }, keep_head: 1 };
  const out = await r.wall.escape(ctx, new Response(wall_body(), { status: 400 }), r.send);
  assert.strictEqual(out.status, 200, "the cut resend went through");
  assert.deepStrictEqual(r.windows.resolve("deepseek-chat"), { tokens: 128000, source: "table" },
    "the table's 128k is untouched: 300k refused says nothing new about it");
  const line = log_lines(r.dir).find((l) => l.event === "撞墙");
  assert.strictEqual(line.learned, null);
  assert.ok(/窗口不改/.test(line.words), line.words);
});

test("a vague wall error the window in use cannot explain: the last_ok guess is learned, with a lapse time", async () => {
  const r = wall_rig();
  r.wall.note_usage("odd-model", { prompt_tokens: 30000 }, 30000);
  const ctx = { thread: "t_wall01", window: "w_1", model: "odd-model", forward: { messages: turns(40, 500) }, keep_head: 1 };
  await r.wall.escape(ctx, new Response(wall_body(), { status: 400 }), r.send);
  assert.deepStrictEqual(r.windows.resolve("odd-model"), { tokens: 30000, source: "learned" });
  const kept = JSON.parse(fs.readFileSync(r.windows.file, "utf8")).learned["odd-model"];
  assert.deepStrictEqual([kept.how, kept.until - kept.at], ["last_ok", GUESS_TTL_MS]);
  // a later turn that goes through with more raises it
  r.wall.note_usage("odd-model", { prompt_tokens: 41000 }, 41000);
  assert.deepStrictEqual(r.windows.resolve("odd-model"), { tokens: 41000, source: "learned" });
});

// ———— finding 6: the snapshot after a cut-down resend ————

test("🔴 after the wall's cut-down resend goes through, ctx.sent and its layout are the request that went", async () => {
  const r = wall_rig();
  const messages = turns(30, 200);
  const forward = { model: "m", messages };
  const keep_head = 1;
  const layout = { head: 1, lines: messages.slice(1).map((m, k) => [k + 1, `l${k}`]) };
  const ctx = { thread: "t_wall01", window: "w_1", model: "m", forward, sent: forward, layout, keep_head };
  const msg = "maximum context length is 8192 tokens. However, your messages resulted in 20000 tokens.";
  await r.wall.escape(ctx, new Response(JSON.stringify({ error: { message: msg } }), { status: 400 }), r.send);
  assert.strictEqual(r.sent.length, 1);
  assert.strictEqual(ctx.sent, r.sent[0], "the snapshot is the resend's body");
  assert.ok(ctx.sent.messages.length < messages.length);
  // every layout entry still points at the same client message, inside the cut request
  const by_id = new Map(layout.lines.map(([k, id]) => [id, messages[k]]));
  assert.ok(ctx.layout.lines.length > 0);
  for (const [k, id] of ctx.layout.lines) assert.strictEqual(ctx.sent.messages[k], by_id.get(id), `line ${id} at ${k}`);
  assert.strictEqual(ctx.layout.lines.at(-1)[0], ctx.sent.messages.length - 1, "her current line is the last");
});

test("🔴 a wake refused as too long: the earlier wake pairs go, then that snapshot is not woken into again (said once)", async () => {
  const dir = fresh_dir();
  const clock = create_fake_clock(at("2026-10-08T10:00:00"));
  fs.writeFileSync(path.join(dir, "present.json"), JSON.stringify({ wake: { on: true, every_min: 60,
    dnd: { on: false, from: "23:00", to: "08:00" }, daily_cap: 16 } }));
  const pairs = [1, 2, 3].map((i) => ({ letter: `信${i}`, reply: `醒着说的第${i}句` }));
  const thread = { id: "t_wake01", last_at: clock.now(),
    last_sent: { at: 1, request: { model: "m", messages: [{ role: "user", content: "在吗" }] }, reply: "在。", wakes: pairs } };
  const runs = [];
  const refused = { outcome: "paid", reason: "http_400", run: "r",
    error: '{"error":{"code":"context_length_exceeded","message":"This model\'s maximum context length is 8192 tokens."}}' };
  const own_turn = {
    status: () => ({ key: "borrowed", running: null }), busy: () => false, abort: () => false,
    run: async (args) => { runs.push(args); return { ...refused }; },
  };
  const wake = create_wake({
    data_root: dir, clock, zone: ZONE, own_turn, log: () => {},
    threads: { list: () => [thread], get: () => thread, save: () => true },
    day_store: { append_new: () => { throw new Error("nothing should be written"); } },
    settings: create_settings({ file: path.join(dir, "present.json"), env: {} }),
    prompts: create_prompts({ file: path.join(dir, "prompts.json"), clock, zone: ZONE }),
    fetch_poke: async () => ({ dreams: [], muse_pending: 0 }),
  });
  const ran = [];
  for (let i = 0; i < 12 * 60; i++) {
    clock.advance(MIN);
    const r = await wake.tick();
    if (r.ran) ran.push(r.result);
  }
  assert.deepStrictEqual(ran, ["paid_failure", "paid_failure"], "two refusals, then no more paid calls");
  assert.strictEqual(runs[0].messages.length, 1 + 1 + 2 * 3 + 1, "the first went with the three earlier pairs");
  assert.strictEqual(runs[1].messages.length, 1 + 1 + 1, "the second with none");
  assert.deepStrictEqual(thread.last_sent.wakes, []);
  assert.ok(thread.last_sent.walled, "the snapshot is marked");
  const s = wake.status();
  assert.deepStrictEqual([s.next_why, s.next_why_words], ["walled", WHY_WORDS.walled]);
  const lines = log_lines(dir);
  assert.deepStrictEqual(lines.filter((l) => l.event === "wake_wall").map((l) => l.dropped_wakes), [3, 0]);
  assert.strictEqual(lines.filter((l) => l.event === "wake_gate" && l.blocked === "walled").length, 1, "said once");

  // her next turn makes a new snapshot: the gate opens again
  wake.remember_turn(thread.id, { model: "m", messages: [{ role: "user", content: "我回来了" }] }, { text: "欢迎回来。" });
  assert.ok(!thread.last_sent.walled);
  own_turn.run = async (args) => { runs.push(args); return { outcome: "ok", text: "", silent: true, run: "r2" }; };
  clock.advance(61 * MIN);
  assert.strictEqual((await wake.tick()).result, "silent");
});

// ———— finding 3: a forced pack that cannot help ————

/** A packer over a hand-made thread; own_turn only counts (and answers "no key": unpaid). */
function pack_rig({ system_chars, lines, line_chars, fill_pct, keep_raw = 2, force_pct = 85 }) {
  const dir = fresh_dir();
  const clock = create_fake_clock(at("2026-10-08T10:00:00"));
  const branch = [];
  const store = new Map();
  for (let i = 0; i < lines; i++) {
    const role = i % 2 === 0 ? "user" : "assistant";
    const id = `L${i}`;
    branch.push({ id, role });
    store.set(id, { id, role, text: `${i}${"字".repeat(line_chars)}`, at: "2026-10-08T09:00:00+08:00" });
  }
  const thread = { id: "t_pack01", branch, window: { name: "t_pack01_w1", mark: null, carry: null, usage: { fill_pct } } };
  const runs = [];
  const packer = create_packer({
    threads: { get: (id) => (id === thread.id ? thread : null), save: () => true, list: () => [thread] },
    day_store: { get: (id) => store.get(id) || null },
    own_turn: { run: async (args) => { runs.push(args); return { ok: false, outcome: "unpaid", reason: "no_key", run: "r" }; } },
    prompts: { current: () => "压缩卡" },
    read_compress: () => ({ on: true, keep_raw, force_pct }),
    system_of: () => [{ role: "system", content: "人设".repeat(system_chars / 2) }],
    data_root: dir, clock, zone: ZONE, log: () => {},
  });
  return { dir, clock, thread, runs, packer };
}
const settle = () => new Promise((r) => setImmediate(r));

test("🔴 a forced pack that cannot get under the force line is not started, turn after turn; said once; manual still packs", async () => {
  // a long persona: system + the two kept lines are already past 85% of the window
  const r = pack_rig({ system_chars: 9000, lines: 8, line_chars: 20, fill_pct: 95 });
  const first = r.packer.request(r.thread.id, "forced");
  assert.strictEqual(first.no_help, true, JSON.stringify(first));
  assert.strictEqual(first.reason, "over_line");
  for (let i = 0; i < 5; i++) r.packer.request(r.thread.id, "forced");
  r.packer.request(r.thread.id, "forced", { fill_pct: 101 });   // the wall's refused request
  r.packer.beat();
  await settle();
  assert.strictEqual(r.runs.length, 0, "no paid pack");
  const held = log_lines(r.dir).filter((l) => l.event === "pack" && l.outcome === "no_help");
  assert.strictEqual(held.length, 1, "one ledger line for the window");
  assert.ok(/强制压缩先停了/.test(held[0].words) && held[0].predicted_pct >= 85, held[0].words);
  const st = r.packer.status();
  assert.deepStrictEqual(st.stopped.map((s) => [s.thread, s.reason]), [[r.thread.id, "over_line"]]);
  assert.strictEqual(st.running.length + st.pending.length, 0);

  assert.deepStrictEqual(r.packer.request(r.thread.id, "manual"), { started: true }, "she asked: it goes");
  await settle();
  assert.strictEqual(r.runs.length, 1);
});

test("a forced pack that would free only a couple of points waits; one that folds most of the window goes", async () => {
  const thin = pack_rig({ system_chars: 6000, lines: 6, line_chars: 30, fill_pct: 86 });
  assert.strictEqual(thin.packer.request(thin.thread.id, "forced").reason, "small_gain");
  await settle();
  assert.strictEqual(thin.runs.length, 0);
  assert.ok(/不到 10 个点/.test(log_lines(thin.dir).find((l) => l.outcome === "no_help").words));

  const thick = pack_rig({ system_chars: 200, lines: 40, line_chars: 300, fill_pct: 90 });
  assert.deepStrictEqual(thick.packer.request(thick.thread.id, "forced"), { started: true });
  await settle();
  assert.strictEqual(thick.runs.length, 1);
  assert.strictEqual(thick.packer.status().stopped.length, 0);
});

// ———— finding 6, through the real relay: the next wake goes out under the wall ————

test("🔴 end to end: a turn escapes the wall, and the wake that follows is built on what went through", { timeout: 30000 }, async () => {
  const LIMIT = 11000;
  const got = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      if (req.method === "GET") { res.writeHead(200, { "Content-Type": "application/json" }); return res.end('{"data":[]}'); }
      const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      const size = estimate_prompt(body);
      const wake = String(body.messages.at(-1)?.content || "").startsWith("【自由时间");
      got.push({ wake, size });
      if (size > LIMIT) {
        res.writeHead(400, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ error: { code: "context_length_exceeded",
          message: `This model's maximum context length is ${LIMIT} tokens. However, your messages resulted in ${size} tokens.` } }));
      }
      const events = [{ choices: [{ index: 0, delta: { content: wake ? "" : "好。" } }] },
        { choices: [{ index: 0, delta: {}, finish_reason: "stop" }] },
        { choices: [], usage: { prompt_tokens: size, completion_tokens: 2, total_tokens: size + 2 } }];
      res.writeHead(200, { "Content-Type": "text/event-stream" });
      res.end(events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("") + "data: [DONE]\n\n");
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const up_port = server.address().port;
  fence.allow(up_port);

  const dir = fresh_dir();
  fs.mkdirSync(path.join(dir, "state"), { recursive: true });
  fs.writeFileSync(path.join(dir, "state", "poke-window.json"),
    JSON.stringify({ lastUserMessageTime: new Date().toISOString(), wakePending: false }));
  fs.writeFileSync(path.join(dir, "present.json"), JSON.stringify({
    wake: { on: true, every_min: 60, dnd: { on: false, from: "23:00", to: "08:00" }, daily_cap: 16 },
    report: { flip: "manual" }, compress: { on: false, context_tokens: 1000000 } }));
  const clock = create_fake_clock(at("2026-10-08T10:00:00"));
  const present = create_present({ env: { LOCI_TZ: ZONE }, data_root: dir, clock, upstream: `http://127.0.0.1:${up_port}/v1`,
                                   loci: "http://127.0.0.1:9/mcp", log: () => {} });
  const relay = create_relay({ upstream: `http://127.0.0.1:${up_port}/v1`, present });
  const gw = http.createServer((req, res) => relay(req, res, { start: Date.now() }));
  await new Promise((r) => gw.listen(0, "127.0.0.1", r));
  fence.allow(gw.address().port);
  try {
    const tools = [{ type: "function", function: { name: "noop", parameters: { type: "object", properties: {} } } }];
    const resp = await fetch(`http://127.0.0.1:${gw.address().port}/v1/chat/completions`, {
      method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer sk-spend-test" },
      body: JSON.stringify({ model: "her-model", stream: true, tools, messages: turns(20, 300) }) });
    assert.strictEqual(resp.status, 200);
    await resp.text();
    const end = Date.now() + 3000;
    while (!present.threads.list().some((t) => t.last_sent)) {
      if (Date.now() > end) throw new Error("no snapshot");
      await new Promise((r) => setTimeout(r, 5));
    }
    assert.deepStrictEqual(got.map((g) => g.size > LIMIT), [true, false], "refused, then the cut resend went through");
    const snap = present.threads.list().find((t) => t.last_sent).last_sent;
    assert.ok(estimate_prompt(snap.request) <= LIMIT, "the snapshot is the request that fit");

    clock.advance(61 * MIN);
    const r = await present.wake.tick();
    assert.strictEqual(r.result, "silent", JSON.stringify(r));
    assert.deepStrictEqual(got.filter((g) => g.wake).map((g) => g.size <= LIMIT), [true], "the wake went out under the wall");
  } finally {
    gw.closeAllConnections?.(); server.closeAllConnections?.();
    await new Promise((r) => gw.close(r));
    await new Promise((r) => server.close(r));
  }
});
