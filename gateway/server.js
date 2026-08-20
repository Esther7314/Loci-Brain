// ============================================================
// gateway/server.js — the mini gateway that sits in front of Loci
//
// **What it does**: your client posts a chat request here, this layer forwards it to
// the real model, and on the way it pokes Loci twice to put what the AI ought to know
// into this round's messages.
//
// Two pokes, landing in two different places — do not mix them up:
//   · poke delivery (poke_delivery.js)     dreams / muse, pinned in the stable prefix
//   · relevance reminder (auto_attach.js)  **pinned at the true tail** — after the
//                                          latest user message, at the very end of the
//                                          whole messages array
//
// 🔴 Why the reminder has to go on last: its position has to be "as close as possible
//    to the moment the model speaks". So build_relevance_notice() never touches
//    messages itself — it only computes the patch and hands it back, and
//    attach_at_true_tail() runs as the last step, once everything else is inserted and
//    the request body is assembled. Get the order wrong and the position is wrong.
//
// ⛔ **This shell does not call breath() on the AI's behalf.** auto_attach.js also
//    carries a function that pastes a whole breath() into the system prompt on the
//    first turn of a window (`attach_once`), and here it is **deliberately left
//    unwired**: "breathe before you speak" is a hand **the AI has to reach out with
//    itself**, and it lives in the system prompt (docs/系统提示-中文.md). Have the
//    gateway paste it in and the AI is no longer "remembering to open its eyes" —
//    it is "being handed a summary". Those are two entirely different things.
//    If you do want the gateway to do it for you, the function is right there in the
//    module: wire it up in one line.
//
// 🔴 Three boundaries (shared with both modules — do not lose them when you edit):
//   · **A failure never blocks the chat.** Any poke that comes up empty — a timeout,
//     Loci not running, a reply that is not JSON — still forwards as usual and writes
//     one log line. Better to miss an attachment this round than to stall a human
//     conversation.
//   · **Nearly all reads, barely any writes.** It touches breath / recall (reads) and
//     poke / dream.wake (a read-only endpoint plus an idempotent signal).
//     ⛔ It never modifies a memory.
//   · **It reports that something exists, never what it says.** What the reminder
//     inserts is a **count** — "there are N relevant memories" — and the judgement is
//     left to the AI itself.
//
// Zero dependencies: only Node's built-in http / fetch (Node 18+).
//
// Run:
//     LOCI_UPSTREAM=https://api.deepseek.com/v1 node gateway/server.js
// then point the client's base_url at http://127.0.0.1:3100/v1.
// The API key still comes from the client — this layer forwards it verbatim and
// **neither stores nor reads it**.
// ============================================================

const http = require("http");
const path = require("path");
const { Readable } = require("stream");
const auto = require("./auto_attach.js");
const poke = require("./poke_delivery.js");

const port = Number(process.env.PORT || 3100);
const upstream = (process.env.LOCI_UPSTREAM || "").replace(/\/+$/, "");
const LOCI = process.env.LOCI_MCP || poke.DEFAULT_ADDRESS;
const idle_threshold_minutes = Number(process.env.POKE_IDLE_MINUTES || poke.DEFAULT_IDLE_MINUTES);
const min_score = Number(process.env.RELEVANCE_MIN_SCORE || auto.DEFAULT_MIN_SCORE);
const data_root = process.env.LOCI_GATEWAY_DATA || path.join(__dirname, "data");
// ⚰️ **The "recent memory view" was pulled out wholesale.**
//    What it did: on the first turn of the next day's window, paste excerpts of
//    recall(when="yesterday") into the context. But "yesterday's memory" never needed
//    a trip to Loci — **it is exactly the few sentences squeezed out when the previous
//    window closed**, and carrying those into the next window is the whole job. Asking
//    Loci a second time only buys a second way of doing the same thing.
//    The idea is kept in gateway/README.md section four (closing-window compression);
//    the code is not. Something the docs never mention but the code still runs is a
//    road with no entrance.
const log_path = path.join(data_root, "logs", "memory-actions.jsonl");

if (!upstream) {
  console.error("没配 LOCI_UPSTREAM —— 我不知道该把请求转给谁。");
  console.error("例：LOCI_UPSTREAM=https://api.deepseek.com/v1 node gateway/server.js");
  process.exit(1);
}

function read_body(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

function is_chat(req, body) {
  return req.method === "POST"
    && /\/chat\/completions$/.test(req.url.split("?")[0])
    && body && Array.isArray(body.messages);
}

// ════════════════════════════════════════════════════════════════
// "Is it actually working?" — read-only, inferred from the log it already writes
// ════════════════════════════════════════════════════════════════
// 🔴 Added because of this very gateway: **the timeout was set to 5 seconds, so from
//    the day it shipped it never once did its job, and it stayed that way for days
//    with nobody noticing.** It did not crash, it did not raise, and the console gave
//    nothing away — `notes.push("提醒无")` prints exactly the same for a timeout, a
//    500, a refused connection, and "this sentence was never meant to trigger a
//    lookup" alike.
// 📌 The test: **do not ask "is the process up", ask "when did it last actually get
//    something done".** The first only proves it is standing there; the second is the
//    evidence that it is working.
// ⚠️ Deliberately **not a second set of books**: `memory-actions.jsonl` already writes
//    injected / error / triggered down one line at a time. A second ledger is one more
//    place where the books themselves can rot without anyone knowing — and that is the
//    exact disease this section exists to treat.
const WINDOW_SIZE = 200;         // how many recent **relevance checks** to look at, not log lines
const MAX_READ_BYTES = 4 * 1024 * 1024;

function read_recent_records(n = WINDOW_SIZE) {
  const fs = require("fs");
  try {
    if (!fs.existsSync(log_path)) return null;   // null = no file (kept apart from "file exists, no records")
    // 🔴 **Actually read only the tail.** The first version's comment said "reads
    //    only the tail" while the code readFileSync'd the entire file into memory and
    //    then sliced off the last 200 lines — at a few hundred megabytes of log it is
    //    just as slow and just as hungry. A comment that disagrees with its code is
    //    worse than no comment at all: **the next person will believe it.**
    const size = fs.statSync(log_path).size;
    const start = Math.max(0, size - MAX_READ_BYTES);
    const fd = fs.openSync(log_path, "r");
    const buf = Buffer.alloc(Math.min(size, MAX_READ_BYTES));
    fs.readSync(fd, buf, 0, buf.length, start);
    fs.closeSync(fd);
    let text = buf.toString("utf8");
    if (start > 0) text = text.slice(text.indexOf("\n") + 1);   // the first line is probably cut in half, drop it
    // 🔴 **Filter first, then slice** — not slice first and filter after. The same
    //    jsonl also holds poke / dream records, and one noisy poke would push the
    //    relevance records out of the window; in the extreme the health endpoint says
    //    "no records at all yet" while the lines above it are nothing but records.
    const records = [];
    for (const l of text.trimEnd().split("\n")) {
      let r = null;
      try { r = JSON.parse(l); } catch { continue; }
      if (r && r.action === "relevance_reminder_observed") records.push(r);
    }
    return records.slice(-n);
  } catch { return []; }
}

function build_health() {
  // 🔴 **One shape, log or no log.** The first version returned early on the "no
  //    file" branch with a short object that was missing the count fields — the reader
  //    then had to cope with two shapes, and the entire point of this endpoint is that
  //    you understand it at a glance. The distinction is still made (日志档存在: false,
  //    plus 结论 spelling it out), but not one field is dropped.
  const read_records = read_recent_records();
  const log_exists = read_records !== null;
  const records = read_records || [];
  const seconds_ago = (t) => (t ? Math.round((Date.now() - new Date(t).getTime()) / 1000) : null);
  const number_or_raw = (v) => (v === undefined || v === null || v === "" ? "（没设，用默认）"
                            : (Number.isFinite(Number(v)) ? Number(v) : `⚠️ 这不是个数：${JSON.stringify(v)}`));

  const triggered_records = records.filter((r) => r.triggered);
  const injected_records = records.filter((r) => r.injected);
  const error_records = records.filter((r) => r.error);
  const last_injected = [...records].reverse().find((r) => r.injected) || null;
  const last_error = [...records].reverse().find((r) => r.error) || null;

  // 🔴 **How many failures came AFTER the last success.** The first version asked
  //    "was there ever a success in the window", and that answer lies: the window is
  //    the last 200 records, not the last stretch of time, so **a single success from
  //    yesterday sits in the window forever, covering up today's total outage**.
  //    Measured it: 2 healthy rounds followed by 4 straight failures, and it still
  //    said "working" — precisely the moment it most needed to speak up.
  //    Turn it around, too: the original "5 second timeout" bug would have stayed
  //    invisible here as long as anything at all had succeeded earlier.
  //    **A monitor that lies is worse than no monitor.**
  const last_success_index = records.map((r) => !!r.injected).lastIndexOf(true);
  const errors_since_success = records.slice(last_success_index + 1).filter((r) => r.error).length;

  let verdict;
  if (!log_exists) {
    // "No file" and "file exists but has no records yet" are two different things.
    // The first usually means LOCI_GATEWAY_DATA points somewhere else, so the log the
    // health endpoint reads is not the log the gateway writes — and then it will say
    // "no records yet" forever while the gateway is working perfectly.
    // **These two cases have to stay distinguishable.**
    verdict = "还没有任何一次记录 —— 日志档还不存在（刚起来？还是 LOCI_GATEWAY_DATA 指错了？）";
  } else if (records.length === 0) {
    verdict = "还没有任何一次记录 —— 它可能刚起来，也可能从来没被调用过";
  } else if (triggered_records.length === 0) {
    // ⚠️ Not an alarm: if nobody said anything relevant, zero triggers is correct.
    //    But say something about language — the strong-trigger word list ships in
    //    Chinese, so anyone who does not speak Chinese would sit on this one verdict
    //    forever while it reassures them that things are "probably fine".
    //    **That misses an entire class of users.**
    verdict = "最近这些轮里一次都没触发（可能正常：没人说到相关的事）"
      + (records.length >= 30 ? "　⚠️ 攒了这么多轮一次都没触发，也可能是强档词表跟你说的语言对不上" : "");
  } else if (errors_since_success >= 3) {
    verdict = `🔴 它是刚坏的：最近一次真的贴上之后，又连着失败了 ${errors_since_success} 次`;
  } else if (injected_records.length > 0) {
    verdict = "在工作";
  } else if (error_records.length > 0 && triggered_records.length >= 2) {
    // This is the shape of that bug: **triggered, errored, never once attached.**
    // ⚠️ Only shout at ≥2: the README itself says the first relevance check after a
    //    Loci restart will very likely time out, and going red on that cold start
    //    means crying wolf on every single restart.
    verdict = `🔴 触发了 ${triggered_records.length} 次，出错 ${error_records.length} 次，一次都没贴上 —— 它在安静地什么都不做`;
  } else if (error_records.length === 0) {
    // 🔴 **No alarm here.** The first version shouted 🔴 on this branch, and what it
    //    caught was a perfectly healthy install: fresh setup, nothing relevant in the
    //    library yet — the lookup ran fine, 0 hits, not one error. Ship that and
    //    somebody sees a red light on day one.
    // ⚠️ This verdict has a ceiling, and the ceiling has to be said out loud: "the
    //    library really has nothing" and "the parser has gone blind" (Loci changed its
    //    render layout) **look identical in the log**; nobody can tell them apart.
    //    So list the possibilities and **never conclude "everything is fine"**.
    verdict = `触发了 ${triggered_records.length} 次，一条都没过线（没报错：可能库里确实没有 / 分数线太高 / Loci 改了渲染排版）`;
  } else {
    verdict = `触发了 ${triggered_records.length} 次还没贴上过，出错 ${error_records.length} 次 —— 次数还太少，再看看`;
  }

  return {
    结论: verdict,
    最近这些轮: records.length,
    触发过: triggered_records.length,
    真的贴上: injected_records.length,
    出过错: error_records.length,
    最近一次真的贴上之后又崩了: errors_since_success,
    最近一次真的贴上: last_injected ? { 几秒前: seconds_ago(last_injected.time), 命中: (last_injected.event_count || 0) + (last_injected.mind_count || 0) } : null,
    最近一次出错: last_error ? { 几秒前: seconds_ago(last_error.time), 是什么: String(last_error.error).slice(0, 200) } : null,
    // ⚠️ Do not copy auto_attach.js's default (12000) — a copied constant is one more
    //    constant that can drift. "Not set" is itself information worth seeing, and a
    //    typo is reported as the typo it is: catching a misconfiguration is exactly
    //    what this endpoint is for.
    超时设的是: number_or_raw(process.env.RELEVANCE_TIMEOUT_MS),
    相关度最低分: number_or_raw(min_score),
    日志档: log_path,
    日志档存在: log_exists,
  };
}


const server = http.createServer(async (req, res) => {
  const start = Date.now();

  // Read-only health endpoint. It goes first: nothing below should be able to affect
  // it, and it should affect nothing below.
  // 🔴 This route **was originally spelled `/健康`, and nobody could reach it** — the
  //    client sends the percent-escaped `/%E5%81%A5%E5%BA%B7` while the comparison
  //    here is byte-for-byte, so it never matched.
  //    The consequence is worse than "no response": **the health probe was treated as
  //    an ordinary request and forwarded to the upstream model.** An endpoint whose
  //    only job is to say whether it is working did not work, and shipped the probe
  //    off upstream on its way out.
  //    ⚠️ The fix is not a decodeURIComponent here — that only hides the problem.
  //       **A URL should not contain non-ASCII in the first place.** Renaming it
  //       `/health` removes the problem at the root.
  if (req.method === "GET" && req.url.split("?")[0] === "/health") {
    req.resume();   // a GET has no body, but do not leave unread bytes on a keep-alive connection to derail the next request
    let payload = "{}";
    try { payload = JSON.stringify(build_health(), null, 2); }
    catch (err) { payload = JSON.stringify({ 结论: "健康口自己算不出来了", 错: String(err?.message || err) }); }
    const buf = Buffer.from(payload, "utf8");
    res.writeHead(200, { "Content-Type": "application/json; charset=utf-8", "Content-Length": buf.length });
    res.end(buf);
    return;
  }

  const raw = await read_body(req).catch(() => Buffer.alloc(0));
  let body = null;
  try { body = raw.length ? JSON.parse(raw.toString("utf8")) : null; } catch { body = null; }

  const notes = [];
  if (is_chat(req, body)) {
    const common = {
      messages: body.messages,
      requestId: String(req.headers["x-request-id"] || start),
      logPath: log_path,
      地址: LOCI,
    };

    // ---- Poke delivery: dreams / muse, pinned in the same prefix as the breath paste. ----
    try {
      const d = await poke.attach_once({
        ...common,
        statePath: path.join(data_root, "state", "poke-window.json"),
        闲时阈值分钟: idle_threshold_minutes,
      });
      notes.push(d.patchInjected ? `戳戳(梦=${d.hasDream} 发呆=${d.musePending})`
        : d.calledLoci ? "戳戳无" : "戳戳没问(不够闲)");
    } catch (err) { notes.push("戳戳炸:" + (err?.message || err)); }

    // ---- Relevance reminder. **Last step, pinned at the true tail.** ----
    // Only runs when triggered (strong = keyword hit, weak = local heuristic).
    // No trigger, no recall call at all.
    try {
      const b = await auto.build_relevance_notice({ ...common, 最低分: min_score });
      if (b && b.patch) {
        auto.attach_at_true_tail(body.messages, b.patch);
        notes.push("提醒(贴了真尾巴)");
      } else notes.push("提醒无");
    } catch (err) { notes.push("提醒炸:" + (err?.message || err)); }
  } else notes.push("不是聊天，直接转发");

  // 🔴 **Anything that does not look like an API path gets a local 404.** (A whole
  //    class of bug that the gateway tests caught.)
  //    This layer is a proxy, and a proxy's default behaviour is "forward everything
  //    upstream" — which means it has **no such thing as an unrecognised address**.
  //    Misspell a route and an ordinary server gives you a 404; here it is
  //    **forwarded as usual**. In practice: `/favicon.ico`, mistyped URLs and scanner
  //    probes all go upstream carrying the client's Authorization header.
  //    It surfaced when the health endpoint was first spelled in Chinese as `/健康`:
  //    the client sends the escaped form, the byte comparison fails, it falls through
  //    to this default path → **a probe meant to check "is it working" was sent to the
  //    upstream model.**
  //    ⚠️ Patching the symptom (decodeURIComponent at the health endpoint) does not
  //       save you: curl on Windows sends `%BD%A1%BF%B5`, escaped per the local
  //       codepage, and decodeURIComponent throws outright on that — still leaking.
  //       **The cure is this line: only API paths get out.**
  const route = req.url.split("?")[0];
  if (!route.startsWith("/v1/")) {
    req.resume();
    const payload = Buffer.from(JSON.stringify({
      error: `这一层只转发 /v1/* 的请求，${route} 没往上游发。`,
      提示: "看它在不在工作：GET /health",
    }, null, 2), "utf8");
    res.writeHead(404, { "Content-Type": "application/json; charset=utf-8", "Content-Length": payload.length });
    res.end(payload);
    console.log(`[gateway] ${req.method} ${req.url} → 404（不是 /v1/*，没往上游发）`);
    return;
  }

  const forward_body = body ? Buffer.from(JSON.stringify(body)) : raw;
  const headers = { ...req.headers };
  delete headers.host; delete headers["content-length"]; delete headers["accept-encoding"];

  const target = upstream.replace(/\/v1$/, "") + req.url;
  let resp;
  try {
    resp = await fetch(target, {
      method: req.method,
      headers: headers,
      body: ["GET", "HEAD"].includes(req.method) ? undefined : forward_body,
    });
  } catch (err) {
    res.writeHead(502, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ error: "连不上上游：" + String(err?.message || err) }));
    console.error(`[gateway] ${req.method} ${req.url} → 上游连不上：${err?.message || err}`);
    return;
  }

  const resp_headers = {};
  // 🔴 `content-length` has to be dropped together with `content-encoding` — found by
  //    the gateway's first test suite.
  //    The `delete headers["accept-encoding"]` above means "do not let upstream
  //    compress", but Node's built-in fetch **puts one back itself**,
  //    `accept-encoding: gzip, deflate`, so upstream gzips anyway. fetch then
  //    decompresses the body while `content-length` still describes the **compressed**
  //    size. Skip only content-encoding and the client is told "N bytes in total" (the
  //    compressed number) while the real body is the longer decompressed one —
  //    **cut off at N, half a JSON document.**
  //    ⚠️ Why it survived this long: chat calls are almost all stream:true, and a
  //       streamed response is chunked with no content-length, so the whole path is
  //       bypassed. **Only non-streaming calls get bitten** (completions, embeddings,
  //       any synchronous SDK call) — another failure you never see day to day.
  //    With both dropped, Node computes the length from the real body (or goes
  //    chunked) and the two sides agree again.
  resp.headers.forEach((v, k) => {
    if (k !== "content-encoding" && k !== "content-length") resp_headers[k] = v;
  });
  res.writeHead(resp.status, resp_headers);
  if (resp.body) Readable.fromWeb(resp.body).pipe(res);
  else res.end();

  console.log(`[gateway] ${req.method} ${req.url} → ${resp.status}  ${Date.now() - start}ms  ${notes.join(" · ")}`);
});

server.listen(port, () => {
  console.log(`[gateway] 起来了 http://127.0.0.1:${port}`);
  console.log(`[gateway] 上游        ${upstream}`);
  console.log(`[gateway] Loci        ${poke._internal.httpBase(LOCI)}`);
  console.log(`[gateway] 相关度最低分 ${min_score}  ·  闲时阈值 ${idle_threshold_minutes} 分钟`);
  // 🔴 This line **is the one that used not to be printed**, and the "5 second timeout
  //    → never worked once since it shipped" bug would have been **visible on day one**
  //    had it been on the first screen at startup.
  //    📌 The rule: any number that lets someone spot a misconfiguration at a glance
  //    belongs on the first screen at startup.
  console.log(`[gateway] Loci 超时      ${process.env.RELEVANCE_TIMEOUT_MS || "（没设，用默认）"}`);
  console.log(`[gateway] 它在不在工作    GET http://127.0.0.1:${port}/health`);
  console.log(`[gateway] 把客户端的 base_url 指到 http://127.0.0.1:${port}/v1`);
});
