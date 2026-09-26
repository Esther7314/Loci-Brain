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

Steps: `at` · `call` (+ `args`, `as`, `capture: {var: regex}`) · `snapshot` (+ `as`) ·
`newest: <id>` (+ `as`: follow `superseded_by` to the latest version) · `check`.

Checks, each tagged with the paper's segment (store / find / think / source / input / use):

| check | passes when |
|---|---|
| `field: k, id: x` (+ `equals` / `contains` / `absent: true`) | the entry's frontmatter says so |
| `body_contains` / `body_lacks` (+ `id`) | the entry's body says so |
| `output: name` + `contains` / `lacks` (+ `section: 提醒`) | a tool's output (or one breath block) says so |
| `unchanged: x, since: snapshot, keys: [...]` | those fields did not move |
| `var: name, equals: v` | a captured value matches |

`interface: none` + `needs:` marks an item the current version has nothing to call for.
It is reported as NO INTERFACE, with what the next version has to expose.
`interface: blocked` is different: the version has the behaviour but the harness cannot
reach it (dreams, today). It is reported as BLOCKED, never as the version lacking it.

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
  config (`LOCI_CONFIG_PATH`: no model keys, no embeddings). No real library is opened.
- `serve.py` starts the real server with `clock.py` installed: every read of "now" in
  Loci reads a small file the runner rewrites between steps. See `clock.py` for the
  places it patches and why naive stamps are kept as UTC.
- `random` is seeded per item (`seed:`, default 0).

## Limits worth knowing

- Items are written against the current tool interface. When v2 changes an interface,
  the items that use it change with it; the paper's wording stays the reference.
- Background work (backfill after a write) is given a short settle time after each call.
  Without a model key it fails fast, so names fall back to the first line of the body
  and no summary is written.
- Dreams have no MCP tool, so dream items cannot run on the tool layer yet.
