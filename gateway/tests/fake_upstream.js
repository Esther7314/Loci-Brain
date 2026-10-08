// ============================================================
// gateway/tests/fake_upstream.js — the end that pretends to be "the real model"
//
// Why it has to exist: **only upstream can see whether the gateway really changed the
// messages.** The gateway edits the messages array inside its own process, which the
// client never sees, and a log line saying "attached" is only the gateway talking about
// itself. So the assertions have to land on **the body upstream received** — the one
// piece of evidence that something really went out.
//
// It keeps the books as a sideline: every request that arrives here is written down
// (method / path / headers / body), so the reconciliation at the end can confirm the
// gateway never leaked a single request anywhere else.
// ============================================================

const http = require("node:http");
const zlib = require("node:zlib");

/**
 * @param 端口  a high port (19xxx), picked and confirmed free by the caller
 */
async function start_fake_upstream({ 端口: port }) {
  const received = [];
  // Compression: a real upstream (DeepSeek / OpenAI / GLM) gzips as soon as the request
  // carries an accept-encoding header. Off by default; only the test that specifically
  // covers forwarding turns it on.
  let compress = false;
  // Scripted replies, for the present-layer tests: script(body) decides what comes back.
  // Unset (the default), every request gets the one fixed answer below.
  let script = null;
  const sent = [];   // the exact bytes of each scripted answer, to compare with what the client got

  function answer_scripted(res, body, plan) {
    const status = plan.status ?? 200;
    const text = String(plan.text ?? "");
    const tools = plan.tools || [];
    if (status !== 200) {
      const payload = JSON.stringify({ error: { message: plan.error || "scripted failure" } });
      sent.push(payload);
      res.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
      return res.end(payload);
    }
    if (body?.stream === true) {
      const events = [{ choices: [{ index: 0, delta: { role: "assistant" } }] }];
      // the text arrives in pieces, cut inside a multi-byte character's string on purpose
      for (let i = 0; i < text.length; i += 3) events.push({ choices: [{ index: 0, delta: { content: text.slice(i, i + 3) } }] });
      tools.forEach((name, i) => events.push({ choices: [{ index: 0, delta: { tool_calls: [{ index: i, id: `call_${i}`, type: "function", function: { name, arguments: "{\"q\":1}" } }] } }] }));
      events.push({ choices: [{ index: 0, delta: {}, finish_reason: tools.length ? "tool_calls" : "stop" }] });
      const payload = events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("") + "data: [DONE]\n\n";
      sent.push(payload);
      res.writeHead(200, { "Content-Type": "text/event-stream; charset=utf-8" });
      // written in two halves, so the gateway sees more than one chunk
      const bytes = Buffer.from(payload, "utf8");
      const half = Math.floor(bytes.length / 2);
      res.write(bytes.subarray(0, half));
      return setTimeout(() => res.end(bytes.subarray(half)), 10);
    }
    const message = { role: "assistant", content: tools.length && !text ? null : text };
    if (tools.length) message.tool_calls = tools.map((name, i) => ({ id: `call_${i}`, type: "function", function: { name, arguments: "{}" } }));
    const payload = JSON.stringify({ id: "scripted", object: "chat.completion",
      choices: [{ index: 0, message, finish_reason: tools.length ? "tool_calls" : "stop" }] });
    sent.push(payload);
    res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
    res.end(payload);
  }

  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      let body = null;
      try { body = raw ? JSON.parse(raw) : null; } catch { body = null; }
      received.push({
        方法: req.method,
        路径: req.url,
        头: { ...req.headers },
        体: body,
        原文: raw,
        // the gateway must not lose or scramble messages, so the raw text is kept too and can be compared character by character
      });
      if (script) return answer_scripted(res, body, script(body) || {});
      const resp = {
        id: "假上游-固定回应",
        object: "chat.completion",
        choices: [{ index: 0, message: { role: "assistant", content: "假上游收到了。" }, finish_reason: "stop" }],
      };
      const payload = Buffer.from(JSON.stringify(resp), "utf8");
      if (compress) {
        // exactly how a real upstream answers: a gzipped body + content-encoding + the **compressed** content-length
        const gzipped = zlib.gzipSync(payload);
        res.writeHead(200, {
          "Content-Type": "application/json; charset=utf-8",
          "Content-Encoding": "gzip",
          "Content-Length": String(gzipped.length),
        });
        return res.end(gzipped);
      }
      res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
      res.end(payload);
    });
  });

  await new Promise((resolve, reject) => {
    server.once("error", reject);
    // listen on 127.0.0.1 only: no opening left for the local network
  // 🔴 **Port 0 means "whoever is asking, you pick".** Passing a number here used to mean
  //    the caller had guessed one — asked whether it was free, then bound it a moment
  //    later — and two test runs at once could be told the same number was free. The
  //    loser died in `before()` and took all 19 tests down with it, which reads as
  //    "the gateway is broken" rather than "two runs collided".
  //    The port that actually got bound is read back below; nobody guesses any more.
    server.listen(port ?? 0, "127.0.0.1", resolve);
  });

  const bound = server.address().port;
  return {
    端口: bound,
    地址: `http://127.0.0.1:${bound}/v1`,
    收到: received,
    清账() { received.length = 0; },
    最后一笔() { return received[received.length - 1]; },
    设压缩(on) { compress = Boolean(on); },
    /**
     * script(body) → { status?, text?, tools?: [names], error? }; streamed as SSE when the
     * request asked for stream:true. Pass null to go back to the fixed answer.
     */
    reply_with(fn) { script = fn; },
    /** The exact text of every scripted answer sent so far, in order. */
    sent,
    /** The upstream response body verbatim (uncompressed, the client should receive exactly this) */
    应该拿到的正文: JSON.stringify({
      id: "假上游-固定回应",
      object: "chat.completion",
      choices: [{ index: 0, message: { role: "assistant", content: "假上游收到了。" }, finish_reason: "stop" }],
    }),
    async 关() {
      // closeAllConnections: the gateway side is keep-alive, so a bare close() waits
      // forever for that connection to drop on its own and the test hangs in teardown.
      server.closeAllConnections?.();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

module.exports = { start_fake_upstream };
