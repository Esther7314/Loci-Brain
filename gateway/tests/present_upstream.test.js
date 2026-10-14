// ============================================================
// gateway/tests/present_upstream.test.js — the upstream in force, its test, and the side
// door Loci's side model goes through, through the real gateway
//
// The gateway runs as a black-box child process (start_gateway.js) with two fake
// upstreams (the one LOCI_UPSTREAM names, and another the panel switches to), a fake Loci
// and the network fence. No LOCI_UPSTREAM_KEY: the key in force comes from the client, then
// from the panel.
//
// What is being proved:
//   · before anyone spoke there is no key: the view says so, the test says so, and the side
//     door answers 409 with the words Loci shows — nothing reaches upstream
//   · the side door: refused without LOCI_GATEWAY_TOKEN; it forwards the body byte for byte
//     with the key in force and nothing else of Loci's; it touches no conversation and lends
//     no key; only its two routes exist; a non-JSON POST is refused
//   · a panel key wins over the client's, is never echoed (view, replies, log), is masked out
//     of upstream's error text, and `key: ""` clears it
//   · the upstream test lists the models with the key in force
//   · a URL saved on the panel takes the very next chat and side call; "" goes back to
//     LOCI_UPSTREAM
//   · a bad URL or key → 400 naming the field, the file untouched
//   · no upstream anywhere: the gateway still starts when the panel can reach it, and says
//     so on every door; with the panel's door closed too it does not start
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
const { NO_KEY, NO_UPSTREAM } = require("../present/upstream.js");

const root = fs.mkdtempSync(path.join(os.tmpdir(), "loci-present-upstream-"));
const data_root = path.join(root, "data");
const host_dir = path.join(root, "host");
const ledger_path = path.join(root, "fence.jsonl");
const settings_file = path.join(data_root, "present.json");
const TOKEN = "gw-upstream-token-5d21e0";
const CLIENT_KEY = "sk-client-key-0042";
const PANEL_KEY = "sk-panel-key-77ab";
const OUR_PORTS = new Set();
const gateways = [];
const replies = [];

let up_a, up_b, fake_loci, gateway;

async function boot(dir, { upstream_url, env = {} }) {
  fs.mkdirSync(path.join(dir, "state"), { recursive: true });
  fs.writeFileSync(path.join(dir, "state", "poke-window.json"),
    JSON.stringify({ lastUserMessageTime: new Date().toISOString(), wakePending: false }));
  const g = await start_gateway({
    端口: 0,
    上游地址: upstream_url,
    loci地址: fake_loci.地址,
    数据根: dir,
    相关超时毫秒: 1200,
    白名单端口: [up_a.端口, up_b.端口, fake_loci.端口],
    账本路径: ledger_path,
    extra_env: { LOCI_TZ: "Asia/Shanghai", LOCI_GATEWAY_DAYS: path.join(dir, "host"), ...env },
  });
  gateways.push(g);
  OUR_PORTS.add(g.端口);
  fence.allow(g.端口);
  return g;
}

before(async () => {
  up_a = await start_fake_upstream({ 端口: 0 });
  up_b = await start_fake_upstream({ 端口: 0 });
  fake_loci = await start_fake_loci({ 端口: 0 });
  for (const p of [up_a.端口, up_b.端口, fake_loci.端口]) OUR_PORTS.add(p);
  gateway = await boot(data_root, { upstream_url: up_a.地址,
    env: { LOCI_GATEWAY_TOKEN: TOKEN, LOCI_GATEWAY_DAYS: host_dir } });
});

after(async () => {
  for (const g of gateways) await g.停();
  for (const s of [up_a, up_b]) if (s) await s.关();
  if (fake_loci) await fake_loci.关();
  fs.rmSync(root, { recursive: true, force: true });
});

const CHINESE = /[一-鿿]/;

/** One call; `raw` sends the string as it is, `body` as JSON. `auth: null` sends no Authorization. */
async function call(method, route, { body, raw, auth = TOKEN, to = gateway, type = "application/json", headers = {} } = {}) {
  const h = { "Content-Type": type, ...headers };
  if (auth !== null) h.Authorization = `Bearer ${auth}`;
  const payload = raw !== undefined ? raw : body === undefined ? undefined : JSON.stringify(body);
  const resp = await fetch(`${to.地址}${route}`, { method, headers: h, body: payload });
  const text = await resp.text();
  replies.push(text);
  let json = null;
  try { json = JSON.parse(text); } catch { /* not JSON */ }
  return { status: resp.status, text, json };
}

const view = async (to = gateway) => (await call("GET", "/present", { to })).json.settings.upstream;
const patch = (upstream, to = gateway) => call("POST", "/present", { body: { patch: { upstream } }, to });
const SIDE_CHAT = "/loci/upstream/v1/chat/completions";
// spacing and key order of its own: the side door must hand it on byte for byte
const SIDE_BODY = '{ "model": "side-model",  "messages": [{"role": "system", "content": "只回 JSON"}, {"role": "user", "content": "打个标签"}], "max_tokens": 5, "temperature": 0 }';
const side = (to = gateway) => call("POST", SIDE_CHAT, { raw: SIDE_BODY, to });
const client_chat = (to = gateway) => call("POST", "/v1/chat/completions", { auth: CLIENT_KEY, to,
  body: { model: "chat-model", messages: [{ role: "user", content: "你好呀" }] } });

// Node's fetch adds these on its own; anything else would be the gateway adding something.
const TRANSPORT_HEADERS = new Set(["host", "connection", "content-length", "user-agent", "accept-encoding",
  "accept-language", "sec-fetch-mode"]);

const thread_files = () => {
  const dir = path.join(data_root, "threads");
  return fs.existsSync(dir) ? fs.readdirSync(dir).sort() : [];
};
const day_files = () => {
  const dir = path.join(host_dir, "days");
  return fs.existsSync(dir) ? fs.readdirSync(dir).map((f) => fs.readFileSync(path.join(dir, f), "utf8")).join("") : "";
};

// ————————————————————————————————————————————————————————————

test("before anyone spoke: no key, and every door says so; nothing reaches upstream", { timeout: 20000 }, async () => {
  assert.deepStrictEqual(await view(), {
    base_url: "", env_base_url: up_a.地址, in_force: up_a.地址, key_set: false, key_hint: null, key_from: null,
  });
  const t = await call("POST", "/present/upstream-test", { body: {} });
  assert.strictEqual(t.status, 200);
  assert.deepStrictEqual(t.json, { ok: false, status: "no_key", error: NO_KEY });
  const s = await side();
  assert.strictEqual(s.status, 409, s.text);
  assert.deepStrictEqual(s.json, { error: { message: "网关还没有 key：等 ta 先说一句，或者在网关上游里填一把固定的 key", code: "no_key" } });
  const m = await call("GET", "/loci/upstream/v1/models");
  assert.strictEqual(m.json.error.code, "no_key");
  assert.deepStrictEqual([up_a.收到.length, up_a.models_requests.length], [0, 0]);
});

test("the side door: Loci's token only; the body goes on byte for byte with the client's key, nothing added", { timeout: 20000 }, async () => {
  for (const auth of [null, "not-the-token", CLIENT_KEY]) {
    const r = await call("POST", SIDE_CHAT, { raw: SIDE_BODY, auth });
    assert.strictEqual(r.status, 401, `auth ${auth}`);
  }
  assert.strictEqual(up_a.收到.length, 0, "a refused side call sends nothing");

  const chat = await client_chat();
  assert.strictEqual(chat.status, 200, chat.text);
  assert.deepStrictEqual(await view(), {
    base_url: "", env_base_url: up_a.地址, in_force: up_a.地址, key_set: false, key_hint: "••••42", key_from: "client",
  });

  const threads_before = thread_files();
  const days_before = day_files();
  up_a.清账();
  gateway.输出增量();
  const r = await side();
  assert.strictEqual(r.status, 200, r.text);
  assert.strictEqual(r.text, up_a.应该拿到的正文, "upstream's answer comes back as it was");
  assert.strictEqual(up_a.收到.length, 1);
  const got = up_a.最后一笔();
  assert.strictEqual(got.方法, "POST");
  assert.strictEqual(got.路径, "/v1/chat/completions");
  assert.strictEqual(got.原文, SIDE_BODY, "not a byte of the body changed");
  assert.strictEqual(got.头.authorization, `Bearer ${CLIENT_KEY}`);
  assert.strictEqual(got.头["content-type"], "application/json");
  const extra = Object.keys(got.头).filter((k) => !TRANSPORT_HEADERS.has(k) && !["authorization", "content-type", "accept"].includes(k));
  assert.deepStrictEqual(extra, [], "no header of the gateway's or Loci's own");
  assert.ok(!JSON.stringify(got.头).includes(TOKEN), "Loci's token stays at the gateway");

  const line = await gateway.等增量((t) => /side POST \/v1\/chat\/completions → 200/.test(t));
  assert.match(line, /key from client/);
  assert.deepStrictEqual(thread_files(), threads_before, "no conversation was touched");
  assert.strictEqual(day_files(), days_before, "nothing was written to the day store");
  assert.strictEqual((await view()).key_hint, "••••42", "Loci's token is not remembered as the client's key");

  // only the two routes, and only JSON
  for (const [method, route] of [["GET", SIDE_CHAT], ["POST", "/loci/upstream/v1/embeddings"], ["POST", "/loci/upstream"],
    ["POST", "/loci/upstream/chat/completions"]]) {
    const no = await call(method, route, { raw: method === "POST" ? SIDE_BODY : undefined });
    assert.strictEqual(no.status, 404, `${method} ${route}`);
    assert.strictEqual(no.json.error.code, "not_here");
    assert.match(no.json.error.message, CHINESE);
  }
  const plain = await call("POST", SIDE_CHAT, { raw: SIDE_BODY, type: "text/plain" });
  assert.strictEqual(plain.status, 415);
  assert.strictEqual(up_a.收到.length, 1, "nothing refused went upstream");
});

test("a panel key wins, is never echoed, is masked out of upstream's errors; the test lists the models", { timeout: 20000 }, async () => {
  const r = await patch({ key: `  ${PANEL_KEY} ` });
  assert.strictEqual(r.status, 200, r.text);
  assert.deepStrictEqual(r.json.settings.upstream, {
    base_url: "", env_base_url: up_a.地址, in_force: up_a.地址, key_set: true, key_hint: "••••ab", key_from: "panel",
  });
  assert.strictEqual(r.json.settings.own.fixed_key_set, true);
  assert.ok(!r.text.includes(PANEL_KEY));
  assert.strictEqual(JSON.parse(fs.readFileSync(settings_file, "utf8")).upstream.key, PANEL_KEY, "kept on disk to send with");
  // a pasted "Bearer …" is read as the key; the masked form is refused, not stored
  assert.strictEqual((await patch({ key: `Bearer ${PANEL_KEY}` })).json.settings.upstream.key_hint, "••••ab");
  const masked = await patch({ key: "••••ab" });
  assert.deepStrictEqual([masked.status, masked.json.field], [400, "upstream.key"]);

  up_a.set_models({ object: "list", data: [{ id: "zeta-model" }, { id: "alpha-model" }] });
  const t = await call("POST", "/present/upstream-test", { body: {} });
  assert.deepStrictEqual(t.json, { ok: true, status: 200, models: ["alpha-model", "zeta-model"] });
  assert.strictEqual(up_a.models_requests.at(-1).头.authorization, `Bearer ${PANEL_KEY}`);
  const listed = await call("GET", "/loci/upstream/v1/models");
  assert.strictEqual(listed.status, 200);
  assert.deepStrictEqual(listed.json.data.map((m) => m.id), ["zeta-model", "alpha-model"]);

  up_a.清账();
  assert.strictEqual((await side()).status, 200);
  assert.strictEqual(up_a.最后一笔().头.authorization, `Bearer ${PANEL_KEY}`, "the panel key wins over the client's");

  up_a.reply_with(() => ({ status: 401, error: `Incorrect API key provided: ${PANEL_KEY}.` }));
  try {
    const refused = await side();
    assert.strictEqual(refused.status, 401);
    assert.ok(!refused.text.includes(PANEL_KEY), refused.text);
    assert.match(refused.json.error.message, /Incorrect API key provided: ••••\./);
  } finally { up_a.reply_with(null); }

  const cleared = await patch({ key: "" });
  assert.deepStrictEqual([cleared.json.settings.upstream.key_set, cleared.json.settings.upstream.key_from,
    cleared.json.settings.upstream.key_hint], [false, "client", "••••42"]);
  // the read-only echoes of the view are dropped, not refused
  assert.strictEqual((await patch({ ...cleared.json.settings.upstream })).status, 200);
  assert.ok(!gateway.全部输出().includes(PANEL_KEY), "the panel key is in no log line");
  assert.ok(!gateway.全部输出().includes(CLIENT_KEY), "nor the client's");
});

test("a URL saved on the panel takes the next chat and the next side call; \"\" goes back to LOCI_UPSTREAM", { timeout: 20000 }, async () => {
  const r = await patch({ base_url: `${up_b.地址}/` });
  assert.strictEqual(r.status, 200, r.text);
  assert.deepStrictEqual([r.json.settings.upstream.base_url, r.json.settings.upstream.in_force], [up_b.地址, up_b.地址]);
  up_a.清账(); up_b.清账();
  assert.strictEqual((await client_chat()).status, 200);
  assert.strictEqual((await side()).status, 200);
  assert.deepStrictEqual([up_a.收到.length, up_b.收到.length], [0, 2], "both went to the panel's upstream");
  assert.strictEqual(up_b.最后一笔().原文, SIDE_BODY);

  const back = await patch({ base_url: "" });
  assert.strictEqual(back.json.settings.upstream.in_force, up_a.地址);
  assert.strictEqual((await side()).status, 200);
  assert.deepStrictEqual([up_a.收到.length, up_b.收到.length], [1, 2]);
});

test("a bad URL or key → 400 naming the field; present.json untouched", { timeout: 20000 }, async () => {
  const before_bytes = fs.readFileSync(settings_file, "utf8");
  for (const [upstream, field] of [
    [{ base_url: "ftp://example.com/v1" }, "upstream.base_url"],
    [{ base_url: "api.deepseek.com/v1" }, "upstream.base_url"],
    [{ base_url: "https://example.com/v1?x=1" }, "upstream.base_url"],
    [{ base_url: "https://user:pw@example.com/v1" }, "upstream.base_url"],
    [{ base_url: 7 }, "upstream.base_url"],
    [{ key: "has a space" }, "upstream.key"],
    [{ key: "密钥" }, "upstream.key"],
    [{ key: null }, "upstream.key"],
    [{ nope: 1 }, "upstream.nope"],
  ]) {
    const r = await patch(upstream);
    assert.strictEqual(r.status, 400, `${JSON.stringify(upstream)} → ${r.text}`);
    assert.strictEqual(r.json.field, field, r.text);
    assert.match(r.json.error, CHINESE);
  }
  assert.strictEqual(fs.readFileSync(settings_file, "utf8"), before_bytes);
});

test("no upstream anywhere: starts when the panel can name one, and every door says so", { timeout: 30000 }, async () => {
  const dir = path.join(root, "data-none");
  const g = await boot(dir, { upstream_url: "", env: { LOCI_GATEWAY_TOKEN: TOKEN } });
  for (let i = 0; i < 250 && !/upstream {7}none yet/.test(g.全部输出()); i++) await new Promise((r) => setTimeout(r, 20));
  assert.match(g.全部输出(), /upstream {7}none yet/, "the banner says there is none yet");
  const v = await view(g);
  assert.deepStrictEqual([v.base_url, v.env_base_url, v.in_force], ["", null, null]);
  const chat = await client_chat(g);
  assert.strictEqual(chat.status, 503);
  assert.match(chat.json.error, /网关还没有上游/);
  assert.deepStrictEqual((await call("POST", "/present/upstream-test", { body: {}, to: g })).json,
    { ok: false, status: "no_upstream", error: NO_UPSTREAM });
  const s = await side(g);
  assert.deepStrictEqual([s.status, s.json.error.code], [409, "no_upstream"]);

  // named on the panel: the next request goes there
  assert.strictEqual((await patch({ base_url: up_b.地址, key: PANEL_KEY }, g)).status, 200);
  up_b.清账();
  assert.strictEqual((await side(g)).status, 200);
  assert.strictEqual(up_b.最后一笔().头.authorization, `Bearer ${PANEL_KEY}`);
  await g.停();

  // the panel's door closed too: nothing could ever name one, so it does not start
  await assert.rejects(boot(path.join(root, "data-dead"), { upstream_url: "" }), /LOCI_UPSTREAM is not set/);
});

test("no key in any reply", () => {
  const leaks = replies.filter((t) => t.includes(PANEL_KEY) || t.includes(CLIENT_KEY) || t.includes(TOKEN));
  assert.deepStrictEqual(leaks, []);
});

test("reconciliation: every outbound connection stayed inside this run", () => {
  const entries = fs.existsSync(ledger_path)
    ? fs.readFileSync(ledger_path, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
  const strays = entries.filter((e) => !OUR_PORTS.has(e.端口));
  assert.deepStrictEqual(strays, []);
});
