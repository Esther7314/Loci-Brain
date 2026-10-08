// ============================================================
// gateway/tests/fake_loci.js — the end that impersonates the memory system
//
// 🔴 **It has to be fake.** The real Loci is on 18002 (the real memory library), and the
//    tests must never touch it: one recall takes seconds and its result moves with the
//    library, so no assertion could be pinned down; and it holds someone's own data,
//    which the tests have no business knocking on.
//    This impostor returns a **hard-coded, predictable** search result, and the
//    assertions are written against exactly that.
//
// It impersonates the faces the gateway knocks on (the real Loci hangs them all on the
// same port, which is what the gateway assumes too):
//   · `POST /api/v2/cue`, `/cue/delivered`, `/cue/dropped`   the cards for a turn and the
//                            two acknowledgements (present/cue.js). The same window and
//                            turn asked again get the same answer, as the real Loci does.
//                            The default answer is one card that names the turn, so a
//                            test can tell whose card sits where; `cue_with(fn)` overrides.
//   · `POST /mcp`            MCP streamable-http; auto_attach.js's recall (no longer on
//                            the chat path — any traffic here is booked and visible);
//                            tools/list names recall and grow, for the gateway's own turns
//   · `GET  /api/loci/poke`  ordinary REST; poke_delivery.js goes here. It rides in the
//                            same request as the cue, so it has to be booked as well —
//                            "nothing leaked" is a claim that must be provable from the
//                            ledger.
//
// Six moods (switched with 设模式):
//   正常    — returns that hard-coded search result / the default card, honestly
//   五百    — the handshake is fine, tools/call and the cue routes answer HTTP 500 (Loci
//             alive but broken)
//   断连    — any /mcp or /api/v2/* request has its connection cut (Loci is not running)
//   慢      — tools/call and the cue routes deliberately drag past the gateway's timeout
//             (the scene of that "5 second bug")
//   换排版  — same content, different layout (what it looks like the day Loci changes
//             its recall render)
//   空库    — the lookup succeeds, but nothing relevant exists (**exactly what day one
//             looks like for a fresh install**)
// ============================================================

const http = require("node:http");

// ——— The hard-coded search result: copies Loci recall's render layout, which is the
//     format auto_attach.js's parse_score_line recognises ———
// `{score:5.1f}  [🧠]{摘要}  ({短id})  {MM-DD}`, with the score on a 0~100 scale.
// One 12.7 entry sits in there on purpose: **the score floor must block it**, or else
// "only counts if it clears the floor" is a lie.
const RENDERED_TEXT = [
  "找到 4 条：",
  " 88.4  上次把网关的超时从 5 秒提到 12 秒。  (aa11bb22)  08-19",
  " 71.0  今晚第一次把 Loci 接了上来。  (cc33dd44)  08-03",
  " 63.2  🧠 判据不是有没有报错，是说的和做的是不是同一件事。  (ee55ff66)  08-05",
  " 12.7  一条不该过线的旧事。  (99aa88bb)  07-11",
  "另有 12 条在线下。",
].join("\n");

// What the rendered text above should come out to under "only ≥50 counts". The
// assertions compare against these, rather than copying the magic numbers a second time
// into the test file — copy them twice and one day the two copies disagree.
const EXPECTED_PASSING_IDS = ["aa11bb22", "cc33dd44", "ee55ff66"];

// **The same content in a different layout** — the date moved to the front, the id from
// round brackets to square ones. This is what it looks like the day Loci changes its
// recall render. auto_attach.js's parse_score_line is a regex hard-coded to the old
// layout, so after such a change it recognises nothing at all — which is what makes that
// "silent blindness" visible.
const RELAYOUT_RENDERED_TEXT = [
  "找到 4 条：",
  " 08-19  88.4  上次把网关的超时从 5 秒提到 12 秒。  [aa11bb22]",
  " 08-03  71.0  今晚第一次把 Loci 接了上来。  [cc33dd44]",
  " 08-05  63.2  🧠 判据不是有没有报错，是说的和做的是不是同一件事。  [ee55ff66]",
  " 07-11  12.7  一条不该过线的旧事。  [99aa88bb]",
].join("\n");
const EXPECTED_EVENT_COUNT = 2;   // 88.4 / 71.0, no 🧠 badge
const EXPECTED_MIND_COUNT = 1;   // 63.2 wears the 🧠 badge
const BLOCKED_ID = "99aa88bb";
// The lookup succeeded, the library simply holds nothing relevant — that is not a fault,
// that is day one for a fresh install.
// The trace it leaves in the log is **identical** to "Loci changed its layout"
// (triggered, recall_called, 0 entries, injected=false, no error), and the health
// endpoint cannot tell the two apart — see section eight of the test file.
const EMPTY_RENDERED_TEXT = "找到 0 条。";

// The actual memory text — **not one character of it may show up in the line pasted back** ("counts only, never content")
const MEMORY_BODY_SAMPLES = ["上次把网关的超时从 5 秒提到 12 秒。", "判据不是有没有报错，是说的和做的是不是同一件事。"];

async function start_fake_loci({ 端口: port }) {
  const received = [];        // one entry per HTTP request (each test clears the books at its start)
  const tool_calls = [];    // tools/call only: { 工具, 参数 }
  // The whole-run ledger: **清账 cannot clear this one.** It is what the final
  // reconciliation reads — a claim like "that path never made a sound across the whole
  // run" can only be made from a ledger kept from beginning to end.
  const all_received = [];
  let mode = "正常";
  let slow_ms = 3000;
  const timers = new Set();
  let cue_script = null;
  const cue_answers = new Map();   // "<window>|<turn>" → the first answer, given again on a retry
  let card_seq = 0;

  function cue_answer(body) {
    const key = `${body?.window}|${body?.turn}`;
    if (cue_answers.has(key)) return cue_answers.get(key);
    let answer = cue_script ? cue_script(body) : null;
    if (!answer) {
      if (mode === "空库") answer = { cards: [], text: "" };
      else {
        card_seq += 1;
        const text = `〔相关记忆〕卡${card_seq}·${body?.turn}：${String(body?.text || "").slice(0, 12)}`;
        answer = { cards: [{ card: `e${card_seq}@v1`, kind: "memory", id: `e${card_seq}`, short: `e${card_seq}`, why: "phrase", text }], text };
      }
    }
    const full = { window: body?.window, turn: body?.turn, cards: answer.cards, text: answer.text, scope: "" };
    cue_answers.set(key, full);
    return full;
  }

  function later(fn) {
    const timer = setTimeout(() => { timers.delete(timer); try { fn(); } catch { /* the other side left long ago; normal */ } }, slow_ms);
    if (timer.unref) timer.unref();
    timers.add(timer);
  }

  function send_sse(res, obj, session) {
    const headers = { "Content-Type": "text/event-stream; charset=utf-8" };
    if (session) headers["Mcp-Session-Id"] = session;
    res.writeHead(200, headers);
    res.end(`event: message\ndata: ${JSON.stringify(obj)}\n\n`);
  }

  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      let body = null;
      try { body = raw ? JSON.parse(raw) : null; } catch { body = null; }
      const route = String(req.url || "").split("?")[0];
      const entry = { 方法: req.method, 路径: route, rpc方法: body?.method || null, 体: body, 模式: mode };
      received.push(entry);
      all_received.push(entry);

      // ——— 断连: Loci is not running at all, so the connection is cut the moment it is made ———
      if (mode === "断连" && (route === "/mcp" || route.startsWith("/api/v2/"))) { req.socket.destroy(); return; }

      // ——— The cue face ———
      if (req.method === "POST" && route.startsWith("/api/v2/cue")) {
        const json = (status, obj) => { res.writeHead(status, { "Content-Type": "application/json" }); res.end(JSON.stringify(obj)); };
        const answer = () => {
          if (mode === "五百") return json(500, { error: "假 Loci 故意炸给你看" });
          if (route === "/api/v2/cue") return json(200, cue_answer(body));
          if (route === "/api/v2/cue/delivered") return json(200, { window: body?.window, delivered: [], unknown: [] });
          if (route === "/api/v2/cue/dropped") return json(200, { window: body?.window, dropped: [], cleared: Boolean(body?.all) });
          return json(404, { error: "no such route" });
        };
        if (mode === "慢") return later(answer);
        return answer();
      }

      // ——— The MCP face ———
      if (req.method === "POST" && route === "/mcp") {
        const rpc = body?.method;
        if (rpc === "initialize") {
          return send_sse(res, {
            jsonrpc: "2.0", id: body.id,
            result: { protocolVersion: "2024-11-05", capabilities: {}, serverInfo: { name: "假loci", version: "0" } },
          // 🔴 The session id has to come back in a header, or the client's handshake
          //    judges itself failed. And it **must be ASCII** — HTTP header values are
          //    latin-1, so a Chinese one earns an ERR_INVALID_CHAR from Node and the
          //    handshake dies on the spot (stepped in this once).
          }, "fake-session-1");
        }
        if (rpc === "notifications/initialized") {
          res.writeHead(202); return res.end();     // a notification has no id: 202 with an empty body, exactly what real MCP answers
        }
        if (rpc === "tools/list") {
          return send_sse(res, { jsonrpc: "2.0", id: body.id, result: { tools: [
            { name: "recall", description: "找回记忆", inputSchema: { type: "object", properties: { query: { type: "string" } } } },
            { name: "grow", description: "写下", inputSchema: { type: "object", properties: {} } },
          ] } });
        }
        if (rpc === "tools/call") {
          tool_calls.push({ 工具: body?.params?.name, 参数: body?.params?.arguments || {} });
          if (mode === "五百") {
            res.writeHead(500, { "Content-Type": "text/plain" });
            return res.end("假 Loci 故意炸给你看");
          }
          const resp = {
            jsonrpc: "2.0", id: body.id,
            result: { content: [{ type: "text", text:
              mode === "换排版" ? RELAYOUT_RENDERED_TEXT
              : mode === "空库" ? EMPTY_RENDERED_TEXT
              : RENDERED_TEXT }] },
          };
          if (mode === "慢") {
            // Answer only after dragging past the gateway's timeout. By then the other
            // side has usually aborted and the write fails, which is entirely normal —
            // hence the wrapper: the impostor must never take the test process down.
            const timer = setTimeout(() => {
              timers.delete(timer);
              try { send_sse(res, resp); } catch { /* the other side left long ago; normal */ }
            }, slow_ms);
            if (timer.unref) timer.unref();   // do not let it hold the process open
            timers.add(timer);
            return;
          }
          return send_sse(res, resp);
        }
        res.writeHead(400); return res.end();
      }

      // ——— The REST face: in this suite **it should never once be knocked on**, and if it is, the ledger holds the evidence ———
      if (route === "/api/loci/poke") {
        res.writeHead(200, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ dreams: [], muse_pending: 0 }));
      }
      if (route === "/api/loci/dream/wake") {
        res.writeHead(200, { "Content-Type": "application/json" });
        return res.end("{}");
      }

      res.writeHead(404); res.end();
    });
  });

  await new Promise((resolve, reject) => {
    server.once("error", reject);
  // 🔴 **Port 0 means "whoever is asking, you pick".** Passing a number here used to mean
  //    the caller had guessed one — asked whether it was free, then bound it a moment
  //    later — and two test runs at once could be told the same number was free. The
  //    loser died in `before()` and took all 19 tests down with it, which reads as
  //    "the gateway is broken" rather than "two runs collided".
  //    The port that actually got bound is read back below; nobody guesses any more.
    server.listen(port ?? 0, "127.0.0.1", resolve);   // loopback only
  });

  const bound = server.address().port;
  return {
    端口: bound,
    地址: `http://127.0.0.1:${bound}/mcp`,
    收到: received,
    工具调用: tool_calls,
    全程收到: all_received,
    渲染文本: RENDERED_TEXT,
    换了排版的渲染文本: RELAYOUT_RENDERED_TEXT,
    空库渲染文本: EMPTY_RENDERED_TEXT,
    应该过线的id: EXPECTED_PASSING_IDS, 应该的事件数: EXPECTED_EVENT_COUNT,
    应该的认知数: EXPECTED_MIND_COUNT, 挡在线下的id: BLOCKED_ID, 记忆正文样本: MEMORY_BODY_SAMPLES,
    设模式(new_mode, ms) { mode = new_mode; if (ms != null) slow_ms = ms; },
    /** fn(body) → { cards, text } decides the next fresh cue answers; null goes back to the default card. */
    cue_with(fn) { cue_script = fn; },
    /** The cue requests (asks, delivered, dropped) in the whole-run ledger, in order. */
    cue_requests(route = null) { return all_received.filter((r) => (route ? r.路径 === route : r.路径.startsWith("/api/v2/cue"))); },
    清账() { received.length = 0; tool_calls.length = 0; },
    async 关() {
      for (const t of timers) clearTimeout(t);
      timers.clear();
      // same as the fake upstream: without actively cutting keep-alive connections, close() just hangs there
      server.closeAllConnections?.();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

module.exports = { start_fake_loci, 渲染文本: RENDERED_TEXT, 应该过线的id: EXPECTED_PASSING_IDS,
  应该的事件数: EXPECTED_EVENT_COUNT, 应该的认知数: EXPECTED_MIND_COUNT };
