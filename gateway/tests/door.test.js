// ============================================================
// gateway/tests/door.test.js — LOCI_GATEWAY_BIND and the passphrase in front of every path
//
// The rule (the owner's ruling on binding beyond loopback): on a non-loopback bind every
// path — /v1/*, /present/*, /loci/source, /health — must start with
// /<LOCI_GATEWAY_PASSPHRASE>/, else 404; on loopback no prefix is needed.
//
// The non-loopback side is proved on door.js itself plus one black box that must refuse
// to start: a test never listens beyond loopback (that would open a port on the LAN, and
// on Windows raise a firewall prompt in the middle of a run). The loopback side runs as a
// black box: a prefixed path is let in with the prefix taken off before anything sees it,
// the upstream included.
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
const { check_door, create_door, is_loopback } = require("../door.js");

const PASS = "Ab3-very.secret_door~0042";
const root = fs.mkdtempSync(path.join(os.tmpdir(), "loci-door-"));
const ledger_path = path.join(root, "fence.jsonl");
const OUR_PORTS = new Set();
const gateways = [];
let fake_upstream, fake_loci;

before(async () => {
  fake_upstream = await start_fake_upstream({ 端口: 0 });
  fake_loci = await start_fake_loci({ 端口: 0 });
  OUR_PORTS.add(fake_upstream.端口); OUR_PORTS.add(fake_loci.端口);
});

after(async () => {
  for (const g of gateways) await g.停();
  if (fake_upstream) await fake_upstream.关();
  if (fake_loci) await fake_loci.关();
  fs.rmSync(root, { recursive: true, force: true });
});

async function boot(name, extra_env) {
  const dir = path.join(root, name);
  fs.mkdirSync(path.join(dir, "state"), { recursive: true });
  fs.writeFileSync(path.join(dir, "state", "poke-window.json"),
    JSON.stringify({ lastUserMessageTime: new Date().toISOString(), wakePending: false }));
  const g = await start_gateway({
    端口: 0, 上游地址: fake_upstream.地址, loci地址: fake_loci.地址, 数据根: dir, 相关超时毫秒: 1200,
    白名单端口: [fake_upstream.端口, fake_loci.端口], 账本路径: ledger_path, extra_env,
  });
  gateways.push(g);
  OUR_PORTS.add(g.端口);
  fence.allow(g.端口);
  return g;
}

test("loopback: what counts", () => {
  for (const b of ["127.0.0.1", "127.1.2.3", "localhost", "::1", "[::1]", "::ffff:127.0.0.1"]) assert.ok(is_loopback(b), b);
  for (const b of ["0.0.0.0", "192.168.1.20", "::", "10.0.0.1", "example.com", "::ffff:192.168.1.2"]) assert.ok(!is_loopback(b), b);
});

test("startup check: a non-loopback bind needs a passphrase that survives a URL path", () => {
  assert.deepStrictEqual(check_door({}), { bind: "127.0.0.1", loopback: true, passphrase: "", required: false });
  assert.match(check_door({ LOCI_GATEWAY_BIND: "0.0.0.0" }).error, /LOCI_GATEWAY_PASSPHRASE/);
  assert.match(check_door({ LOCI_GATEWAY_BIND: "0.0.0.0", LOCI_GATEWAY_PASSPHRASE: "short" }).error, /16/);
  assert.match(check_door({ LOCI_GATEWAY_PASSPHRASE: "has/a/slash/in/it/oh/no" }).error, /letters/);
  assert.deepStrictEqual(check_door({ LOCI_GATEWAY_BIND: "192.168.1.20", LOCI_GATEWAY_PASSPHRASE: PASS }),
    { bind: "192.168.1.20", loopback: false, passphrase: PASS, required: true });
});

test("non-loopback: only a path that starts with the passphrase gets in, without it", () => {
  const admit = create_door({ passphrase: PASS, required: true });
  assert.strictEqual(admit(`/${PASS}/v1/chat/completions`), "/v1/chat/completions");
  assert.strictEqual(admit(`/${PASS}/present/prompts?x=1`), "/present/prompts?x=1");
  assert.strictEqual(admit(`/${PASS}/loci/source`), "/loci/source");
  assert.strictEqual(admit(`/${PASS}/health`), "/health");
  assert.strictEqual(admit(`/${PASS}`), "/");
  for (const url of ["/v1/chat/completions", "/present", "/loci/source", "/health", `/${PASS}x/v1/models`,
    `/x${PASS}/v1/models`, `/v1/${PASS}/models`, "/", ""]) {
    assert.strictEqual(admit(url), null, url);
  }
});

test("loopback: no prefix needed; with a passphrase set, a prefixed path works too", () => {
  const open = create_door({ passphrase: "", required: false });
  assert.strictEqual(open("/v1/models"), "/v1/models");
  const both = create_door({ passphrase: PASS, required: false });
  assert.strictEqual(both("/v1/models"), "/v1/models");
  assert.strictEqual(both(`/${PASS}/v1/models`), "/v1/models");
});

test("black box: a non-loopback bind without a passphrase refuses to start (it never listens)", { timeout: 20000 }, async () => {
  await assert.rejects(boot("refused", { LOCI_GATEWAY_BIND: "0.0.0.0" }), /LOCI_GATEWAY_PASSPHRASE/);
});

test("black box on loopback: the prefix is taken off before routing and never reaches upstream", { timeout: 20000 }, async () => {
  const g = await boot("loopback", { LOCI_GATEWAY_PASSPHRASE: PASS, LOCI_GATEWAY_TOKEN: "gw-token" });
  fake_upstream.清账();
  const chat = await fetch(`${g.地址}/${PASS}/v1/chat/completions`, {
    method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer client-key" },
    body: JSON.stringify({ model: "m", messages: [{ role: "user", content: "嗯" }] }),
  });
  assert.strictEqual(chat.status, 200);
  await chat.text();
  assert.strictEqual(fake_upstream.最后一笔().路径, "/v1/chat/completions");
  assert.ok(!JSON.stringify(fake_upstream.收到).includes(PASS), "the passphrase never goes upstream");

  const present = await fetch(`${g.地址}/${PASS}/present`, { headers: { Authorization: "Bearer gw-token" } });
  assert.strictEqual(present.status, 200);
  assert.strictEqual((await present.json()).connected, true);
  const health = await fetch(`${g.地址}/health`);
  assert.strictEqual(health.status, 200, "no prefix needed on loopback");
  const h = await health.json();
  assert.strictEqual(h.present.doors.passphrase_required, false);
  assert.ok(!g.全部输出().includes(PASS), "the passphrase is never printed");
});

test("reconciliation: every outbound connection stayed inside this run", () => {
  const entries = fs.existsSync(ledger_path)
    ? fs.readFileSync(ledger_path, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
  assert.deepStrictEqual(entries.filter((e) => !OUR_PORTS.has(e.端口)), []);
});
