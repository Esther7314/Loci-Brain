# exam — the Loci v2 test paper, runnable

The paper is written in two halves (Loci-internal items, entry-side items) and merged.
This directory runs it.

## Two layers

| layer | what runs | model? | when |
|---|---|---|---|
| **tool** | `runner.py` calls Loci's MCP tools itself, moves a fake clock, reads the disk | no | every code change |
| **whole-turn** | a real model answers through a host plug (`host.py`) | yes | after each batch |

Both layers are in the repo. The tool layer has a 1.4.0 baseline; the whole-turn layer's
code is delivered and awaits its first full run.

## Running the tool layer

```
pip install -r requirements.txt
python exam/runner.py                         # all items
python exam/runner.py exam/items/b-ahead.yaml # one file
python exam/runner.py --keep                  # keep the temp libraries to look at
```

A report lands in `exam/out/<time>/report.md` (plus `results.json` with every tool output).
Needs no keys; nothing leaves the machine.

## What one item is

```yaml
- id: B2
  title: "a promise for tonight: quiet in the day, back at the next chance, gone once done"
  start: "2026-10-01T10:00:00+08:00"      # the fake clock when the server starts
  setup:                                  # written straight to disk, fixed ids and times
    - id: b20001000000                    # 12 hex; keep the first 6 unique (tools show 6)
      at: "2026-10-01T10:00:00+08:00"
      room: EVENT/SELF
      text: "答应今晚回来告诉她面试结果。"
      when: "2026-10-01"
      fields: {status: want, weight: 0.8} # anything update() accepts
  steps:
    - at: "2026-10-01T14:00:00+08:00"     # move the clock
    - call: breath                        # call a tool; `args:` for arguments
      as: afternoon                       # name its output
    - check: {segment: think, desc: "...", output: afternoon, lacks: b20001}
```

A call may carry what a host puts on its request: `host:` its credential, `scope:` its
Loci-Scope (an object; a string is sent as it is), `turn:` / `write_key:` its Loci-Turn
(`<turn>#<ordinal>`). An item's `hosts:` ({name: {token, scope_mode?, max_grant?,
may_restore?}}) is the deployment's hosts table. Over stdio they ride in the call's _meta
and become the request the call carries (exam/serve.py, a declared seam).

Steps: `at` · `call` (+ `args`, `as`, `capture: {var: regex}`) · `wait: seconds` (for
background work such as the backfill) · `snapshot` (+ `as`) · `newest: <id>` (+ `as`:
follow `superseded_by` to the latest version) · `check` · `source_change: {body}` (+
`host:`, `as`; what POST /api/v2/source/change runs, in the runner's process on the same
library — a second entry point, a declared seam; the reply is JSON text) · `changes:
{since, limit?}` (+ `host:`, `as`, `capture`; what GET /api/v2/changes runs) ·
`concurrent: {calls: [call steps], processes: N, each: M, text}` (the calls at once, with
N writer processes creating M entries each through BucketManager) · `cue: {text, window,
turn}` (+ `host:`, `scope:`, `as`, `capture`; what POST /api/v2/cue runs, the same kind of
declared seam as `source_change:`; the reply is JSON text holding the cards) ·
`cue_delivered: {window, turn | cards}` and `cue_dropped: {window, cards | turns | all}`
(the host's two acknowledgements: a card reached the model's input / left it). `$var` takes a
captured value; `${var:6}` its first six characters, the handle tools print.

Setup extras: an item's `names:` is the names table ({name: {aliases, instance_of,
present_in, member_of}}), written as the library's aliases.yaml; an entry's `sink: true`
sinks it after its fields (it needs a summary); an
item's `dreams: [{id, text, ingredients, degraded?}]` are saved as woven dream records
(`degraded: true`: woken at `at`, the whole text gone, the fragment's clock running) and its
`dehydration_cache: [{text, summary}]` cached by Loci's own dehydrator.

An item may carry `side_model: {phrase: answer}`: the backfill's side-model call is answered
with the answer whose phrase appears in the entry's body (a seam, listed in the report).

An item may carry `weaver: {完整, 碎片, v, a, 线索?}`: the one model call weaving a dream
makes is answered with it (a seam, listed in the report). Dreams have no MCP tool; breath's
upkeep sweeps them and weaves one when the pressure is over the line, so a `call: breath`
and a short `wait:` weave on the fake clock.

Checks, each tagged with the paper's segment (store / find / think / source / input / use):

| check | passes when |
|---|---|
| `field: k, id: x` (+ `equals` / `contains` / `lacks` / `absent: true`) | the entry's frontmatter says so |
| `body_contains` / `body_lacks` (+ `id`) | the entry's body says so |
| `output: name` + `contains` / `lacks` (+ `section: 提醒`) | a tool's output (or one breath block) says so |
| `unchanged: x, since: snapshot, keys: [...]` | those fields did not move |
| `output: name` + `in_order: [a, b, ...]` | each appears, the first of each after the one before |
| `usage: {kind, id, road?}` | the usage log has a line of that kind naming the id (road prefix) |
| `lib_lacks: text` | no file under the library holds it, byte for byte (its `.logs` aside) |
| `vector: id` (+ `absent: true`) | embeddings.db holds (or no longer holds) its row |
| `ledger: {unique_seq: true, type_count: {Type: n}}` | the ledger's numbers are distinct and gapless; n lines of that type |
| `var: name, equals: v` | a captured value matches |
| `dream: {count?, has?, lacks?}` | the dream records on disk number that many; their ingredients (every stream) name / do not name the id |

`interface: none` + `needs:` marks an item the current version has nothing to call for.
It is reported as NO INTERFACE, with what the next version has to expose.
`interface: blocked` is different: the version has the behaviour but the harness cannot
reach it. It is reported as BLOCKED, never as the version lacking it.

Every report opens with the Loci version, commit, time zone and the test seams the
runner added. A baseline that needed more seams than those is not a baseline.

## Running the whole-turn layer

```
python exam/turn.py exam/turns/i-recall.yaml --runs 1     # measure one run first
python exam/turn.py exam/turns/*.yaml                      # 3 runs per item
```

- Host: `exam/hosts/lento.py` (the Lento life line; neutral prompts in `exam/hosts/neutral/`).
  Another entry writes its own host against `exam/host.py`.
- Model: `--model`, default `claude-opus-4-6`. Baseline and acceptance must use the same one.
- Input evidence: `exam/proxy.py` records every request the CLI sends (bodies only, never headers).
- Grading: with `EXAM_JUDGE_BASE_URL` / `EXAM_JUDGE_KEY` / `EXAM_JUDGE_MODEL` set, an
  OpenAI-compatible model grades the rubrics. Without them, `judge-packet.md` is written next
  to the report: paste it into any model (or read it), save the JSON lines it answers with,
  then `python exam/judge_import.py exam/out/turn-<time> verdicts.txt`.
- On another machine set `CLAUDE_CLI_EXE` to the CLI binary.
- Input checks read every request of a turn unless `call:` names one (1 = first,
  -1 = last). Anything about the first request (preinject) must name it. A request read
  back only in part leaves `input_contains` not covered even when the fragment is there;
  a leak seen in it still fails.

## How the isolation works

- Each item gets a fresh temp directory as its library (`LOCI_BUCKETS_DIR`) and its own
  config (`LOCI_CONFIG_PATH`: no model keys). No real library is opened.
- Embeddings are off by default. Set `EXAM_EMBED_URL` to a local Ollama
  (`http://127.0.0.1:11434/v1`; model `bge-m3` unless `EXAM_EMBED_MODEL` says otherwise)
  and the exam scores search the way a library with vectors does. Setup entries are
  embedded before the server starts. The report's seam line says which of the two ran;
  scores from the two are not comparable on items that search.
- `serve.py` starts the real server with `clock.py` installed: every read of "now" in
  Loci reads a small file the runner rewrites between steps. See `clock.py` for the
  places it patches and why naive stamps are kept as UTC.
- `random` is seeded per item (`seed:`, default 0).
- Without embeddings, search is BM25 plus whole-query substrings. Both runners stop
  at start if `rank_bm25` or `jieba` is missing, because Loci would otherwise drop BM25
  without a word. `serve.py` makes the first BM25 build before the first search instead
  of in the background, so an item's first search does not score without BM25; after
  that, keeping it level with writes is Loci's own (the next search syncs it).

## Limits worth knowing

- Items are written against the current tool interface. When v2 changes an interface,
  the items that use it change with it; the paper's wording stays the reference.
- Background work (backfill after a write) is given a short settle time after each call.
  Without a model key it fails fast, so names fall back to the first line of the body
  and no summary is written.
- Dreams have no MCP tool: dream items weave through breath's upkeep with the `weaver:`
  seam, and read what was woven off the disk. What a dream is handed out as (the poke and
  `/api/dream/current` routes) is tests' (tests/test_dream_material.py), not the exam's.
