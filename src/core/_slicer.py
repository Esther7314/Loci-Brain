"""
========================================
core/_slicer.py — attaching sources after the fact: slicing a day's raw lines
========================================

The main model does not write source ids while it talks. Before a window is changed,
the host hands Loci the stretch of raw lines it is about to let go of (Lento: the
nightly 日报 step, one day of one chat), each with the host's own id. A side model
slices them: each slice is a run of consecutive lines (first id .. last id) with one
sentence of what it is. Code then guesses which memory written that day already
covers it (vector similarity against memories created that day). The slices wait as
**pending**: not in the library, not merged into anything. The main model handles
each one itself (tools/_slices.py): not recorded yet -> it writes the memory with
grow and the slice becomes one of its `sources`; already recorded -> the slice is
appended to that memory's `sources`; sliced wrong -> it re-cuts the span or drops it.
The side model only slices; it never writes, merges or decides.

    POST /api/v2/slices  -> take_batch(): check the batch, slice_lines(), guesses,
                            PendingSlices.record_batch()

A slice of any length is one source record (record_for): `id` its first line, `through`
its last (left out for a one-line slice), the batch's `revision`, and a fingerprint Loci
computes, so `fingerprint_by: loci`:

    line hash    = "sha256:" + sha256(the line's UTF-8 text)       (fingerprint_of)
    slice record = "sha256:" + sha256(its line hashes, in order, joined by "\\n")
                                                                   (slice_fingerprint)

The raw text is not kept anywhere. The only thing kept from a line is its hash, in the
pending store, so a re-cut slice gets its fingerprint computed again over its new lines,
and the same lines handed over again give the same fingerprint. The host can hand the
lines over again: a batch is named by source + day + line ids, and the same batch resent
replaces its pending slices instead of doubling them.

The pending store is `<buckets>/_sources/pending_slices.jsonl`, append-only like the
source registry beside it: a `batch` line carries the batch (source, day, revision,
each line's id and hash) and its slices; `close` and `recut` lines change one
slice. A closed slice stays in the file as what handled it, so a second use of its id
is refused by name. The index is rebuilt from the file on first use and whenever it
has grown. An append-only log rather than a file per batch: every change is one whole
line flushed to disk (a torn last line is skipped on read), and the file is the history
of who handled what.

Nothing here knows which host it is or where the lines came from; an import (7.1) hands
its lines to the same take_batch.

Exports: SLICER_PROMPT_VERSION · SLICER_PROMPT · GIST_MAX · Slice · SlicerError ·
         BatchError · SliceError · slice_lines · parse_slices · side_model ·
         read_batch · batch_id_of · fingerprint_of · slice_fingerprint · FINGERPRINT_BY ·
         guess_covering · take_batch · PendingSlices (record_batch · get · record_for ·
         run_length · close · recut · open_batches · pending_count · rebuild_index)
========================================
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from . import _sources as _src
from . import _when as _w
from utils import clean_llm_json

# ============================================================
# The side model's contract
# ============================================================

# Bump whenever SLICER_PROMPT changes what comes back; every batch line records the
# version it was sliced under.
SLICER_PROMPT_VERSION = 1

SLICER_PROMPT = """你在帮一个记忆系统把一段聊天原文切成几片。每片是一件事、一个话题：连续的几行，从第几行到第几行，再用一句话说这片在讲什么。

规则：
- 只切，不总结全天，不评价，不改写原话，不替谁下结论。
- 一片是连续的行；片与片不重叠，按行号从小到大排。
- 一片一般 5～40 行，最多 60 行；话题长就按话题再切开。
- 寒暄、只有表情、没有内容的行可以不放进任何一片。
- gist 是一句话，不超过 40 个字：用原文里的说法，写清楚谁做了什么、说了什么。
- 只输出 JSON，不要别的字：
{"slices": [{"from": 1, "to": 12, "gist": "……"}, {"from": 13, "to": 30, "gist": "……"}]}
"""

GIST_MAX = 80                  # characters; a longer gist is cut, not refused
GUESS_TOP = 3                  # guesses kept per slice
# The guess line: the same cosine recall uses for a purely semantic candidate
# (bucket_manager._VECTOR_RECALL_THRESHOLD). Config: slices.guess_threshold.
DEFAULT_GUESS_THRESHOLD = 0.65
# A batch over this many lines is refused (send it in parts). Config:
# slices.max_lines_per_batch. A Lento day is a few hundred lines.
DEFAULT_MAX_LINES_PER_BATCH = 2000
# One side-model call takes at most this many lines / characters of prompt; a bigger
# batch is sliced in consecutive chunks, and a topic running across a chunk edge comes
# back as two slices.
_CALL_MAX_LINES = 300
_CALL_MAX_CHARS = 24000
_LINE_PROMPT_MAX = 400         # one line's text in the prompt; the slicer needs the topic only
_SLICER_MAX_TOKENS = 4096      # thinking models spend tokens before the JSON
_SPEAKER_MAX = 64
_AT_MAX = 40

ModelCall = Callable[[str, str], Awaitable[str]]


class SlicerError(RuntimeError):
    """The side model failed or answered outside its contract. The whole batch fails
    with it (HTTP 502) and nothing is written."""


class BatchError(ValueError):
    """The host's batch is malformed (HTTP 400)."""


class SliceError(ValueError):
    """A slice the store will not handle that way. str() is English; `zh` is how a tool
    says it."""

    def __init__(self, message: str, zh: str):
        super().__init__(message)
        self.zh = zh


@dataclass(frozen=True)
class Slice:
    """One slice as the slicer hands it over: a run of consecutive lines."""
    first: str
    last: str
    count: int
    gist: str


# ============================================================
# Slicing
# ============================================================

def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _prompt_line(n: int, line: dict) -> str:
    text = _one_line(line.get("text"))
    if len(text) > _LINE_PROMPT_MAX:
        text = text[:_LINE_PROMPT_MAX - 1] + "…"
    head = " ".join(x for x in (_one_line(line.get("at")), _one_line(line.get("speaker"))) if x)
    return f"[{n}] {head}: {text}" if head else f"[{n}] {text}"


def _chunks(lines: list[dict]) -> list[list[dict]]:
    """Consecutive chunks of at most _CALL_MAX_LINES lines and _CALL_MAX_CHARS of
    prompt each (a single line never splits)."""
    out: list[list[dict]] = []
    cur: list[dict] = []
    size = 0
    for line in lines:
        cost = len(_prompt_line(len(cur) + 1, line)) + 1
        if cur and (len(cur) >= _CALL_MAX_LINES or size + cost > _CALL_MAX_CHARS):
            out.append(cur)
            cur, size = [], 0
        cur.append(line)
        size += cost
    if cur:
        out.append(cur)
    return out


def _int(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _cut_gist(gist: str, room: int = GIST_MAX) -> str:
    gist = _one_line(gist)
    return gist if len(gist) <= room else gist[:room - 1] + "…"


def parse_slices(raw: str, n: int) -> list[tuple[int, int, str]]:
    """The side model's answer for n lines -> [(from, to, gist)], 1-based inclusive,
    sorted, not overlapping. Raises SlicerError on anything outside the contract.

    What is repaired and what is refused:
      · not JSON, not {"slices": [...]} (or a bare list), an entry without a gist, or a
        line number outside 1..n or from > to: refused, the whole batch fails. A model
        that miscounts once cannot be trusted with the rest of its numbers.
      · two slices overlapping: the later one starts after the earlier one ends (a
        boundary disagreement); one lying wholly inside the earlier is dropped.
      · lines in no slice: left out. That is the model saying they carry nothing to
        remember (greetings, a lone sticker); the response counts them (`unsliced`).
      · a gist over GIST_MAX characters: cut."""
    text = clean_llm_json(raw)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        raise SlicerError(f"the side model's answer is not JSON: {str(raw)[:120]!r}")
    if isinstance(data, dict):
        data = data.get("slices")
    if not isinstance(data, list):
        raise SlicerError('the side model\'s answer has no "slices" list')
    found: list[tuple[int, int, str]] = []
    for k, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise SlicerError(f"slice {k} is not an object")
        a, b = _int(item.get("from")), _int(item.get("to"))
        gist = _one_line(item.get("gist"))
        if not gist:
            raise SlicerError(f"slice {k} has no gist")
        if a is None or b is None or not 1 <= a <= b <= n:
            raise SlicerError(f"slice {k} names lines {item.get('from')!r}..{item.get('to')!r}, "
                              f"outside 1..{n}")
        found.append((a, b, gist))
    found.sort(key=lambda t: (t[0], t[1]))
    out: list[tuple[int, int, str]] = []
    end = 0
    for a, b, gist in found:
        a = max(a, end + 1)
        if a > b:
            continue
        out.append((a, b, gist))
        end = b
    return out


async def slice_lines(lines: list[dict], *, model: ModelCall) -> list[Slice]:
    """Slice lines ([{id, text, at?, speaker?}], in order) with the side model.

    `model(system, user) -> raw text` is the side model (side_model() in production; a
    stub in tests). One call per chunk of the batch. Raises SlicerError when a call
    fails or answers outside the contract (parse_slices); nothing is half-returned."""
    out: list[Slice] = []
    for chunk in _chunks(list(lines)):
        user = (f"下面是 {len(chunk)} 行聊天原文，行号在方括号里：\n"
                + "\n".join(_prompt_line(i, line) for i, line in enumerate(chunk, 1)))
        try:
            raw = await model(SLICER_PROMPT, user)
        except SlicerError:
            raise
        except Exception as e:
            raise SlicerError(f"the side model call failed: {type(e).__name__}: {e}") from e
        if not str(raw or "").strip():
            raise SlicerError("the side model returned nothing")
        for a, b, gist in parse_slices(str(raw), len(chunk)):
            out.append(Slice(first=str(chunk[a - 1]["id"]), last=str(chunk[b - 1]["id"]),
                             count=b - a + 1, gist=_cut_gist(gist)))
    return out


def side_model(dehydrator, config: Optional[dict]) -> ModelCall:
    """The production slicer: the backfill side model's client and key (dehydration: in
    config), with `dehydration.slicer_model` when it names another model on the same
    endpoint. The returned callable carries `model_name` for the batch line."""
    name = str(((config or {}).get("dehydration") or {}).get("slicer_model") or "").strip() or None

    async def call(system: str, user: str) -> str:
        if dehydrator is None:
            raise SlicerError("no side model is configured (dehydration: in config)")
        dehydrator._require_api()
        return await dehydrator._chat(system, user, max_tokens=_SLICER_MAX_TOKENS,
                                      temperature=0.0, model=name)

    call.model_name = name or str(getattr(dehydrator, "model", "") or "")
    return call


# ============================================================
# The batch the host hands over
# ============================================================

_BODY_KEYS = {"source", "day", "lines", "revision", "fingerprint_by"}
_LINE_KEYS = {"id", "text", "at", "speaker"}
_SOURCE_KEYS = ("system", "instance", "container")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# Who computed a slice record's fingerprint: Loci, from the line hashes it kept.
FINGERPRINT_BY = "loci"


def fingerprint_of(text: str) -> str:
    """One line's hash."""
    return "sha256:" + hashlib.sha256(
        str(text).encode("utf-8", errors="surrogatepass")).hexdigest()


def slice_fingerprint(line_hashes: list[str]) -> str:
    """A slice record's fingerprint: over its lines' hashes, in order, one per line."""
    return "sha256:" + hashlib.sha256("\n".join(line_hashes).encode("utf-8")).hexdigest()


def batch_id_of(source: dict, day: str, ids: list[str]) -> str:
    """A batch is its source, its day and its line ids: the same three resent are the
    same batch."""
    head = f"{source['system']}:{source['instance']}/{source['container']}|{day}|"
    return "b_" + hashlib.sha256((head + "\n".join(ids)).encode("utf-8")).hexdigest()[:16]


def _short_text(value, key: str, limit: int, where: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise BatchError(f"{where}.{key} must be text")
    text = str(value).strip()
    if len(text) > limit:
        raise BatchError(f"{where}.{key} is longer than {limit} characters")
    return text or None


def read_batch(body, *, max_lines: int = DEFAULT_MAX_LINES_PER_BATCH) -> dict:
    """The intake body -> {source, day, revision, lines, batch_id}. Every line id has to
    make a valid source record (core/_sources); ids are unique. A `fingerprint_by` in
    the body is taken as text and not used: the fingerprints are Loci's own
    (FINGERPRINT_BY). Raises BatchError."""
    if not isinstance(body, dict):
        raise BatchError("the body must be a JSON object")
    extra = sorted(set(map(str, body)) - _BODY_KEYS)
    if extra:
        raise BatchError(f"unknown keys {extra}; a batch has {sorted(_BODY_KEYS)}")
    source = body.get("source")
    if not isinstance(source, dict) or sorted(set(map(str, source)) - set(_SOURCE_KEYS)):
        raise BatchError("source must be {system, instance, container}")
    day = str(body.get("day") or "").strip()
    if not _DAY_RE.match(day) or _w.parse_date_or_none(day) is None:
        raise BatchError(f"day must be a calendar day YYYY-MM-DD, got {day!r}")
    lines = body.get("lines")
    if not isinstance(lines, list) or not lines:
        raise BatchError("lines must be a non-empty list of {id, text, at?, speaker?}")
    if len(lines) > max_lines:
        raise BatchError(f"{len(lines)} lines is over the {max_lines}-line cap of one "
                         "batch; send the day in parts")
    _short_text(body.get("fingerprint_by"), "fingerprint_by", 32, "body")
    template = {k: source.get(k) for k in _SOURCE_KEYS}
    template.update(revision=body.get("revision"), fingerprint_by=FINGERPRINT_BY)
    out_lines: list[dict] = []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        where = f"lines[{i}]"
        if not isinstance(line, dict):
            raise BatchError(f"{where} must be an object {{id, text, at?, speaker?}}")
        extra = sorted(set(map(str, line)) - _LINE_KEYS)
        if extra:
            raise BatchError(f"{where} has unknown keys {extra}")
        if not isinstance(line.get("text"), str):
            raise BatchError(f"{where}.text must be a string")
        text = line["text"]
        try:
            [record] = _src.normalize_sources({**template, "id": line.get("id"),
                                               "fingerprint": fingerprint_of(text)})
        except _src.SourceRecordError as e:
            raise BatchError(f"{where}: {e}") from e
        if record["id"] in seen:
            raise BatchError(f"{where}: id {record['id']} appears twice in the batch")
        seen.add(record["id"])
        out_lines.append({"id": record["id"], "text": text,
                          "fingerprint": record["fingerprint"],
                          "at": _short_text(line.get("at"), "at", _AT_MAX, where),
                          "speaker": _short_text(line.get("speaker"), "speaker",
                                                 _SPEAKER_MAX, where)})
    norm_source = {k: record[k] for k in _SOURCE_KEYS}
    return {"source": norm_source, "day": day, "revision": record["revision"],
            "lines": out_lines,
            "batch_id": batch_id_of(norm_source, day, [ln["id"] for ln in out_lines])}


# ============================================================
# The guess: which memory written that day already covers a slice
# ============================================================

async def _day_memories(store, day: str) -> list[str]:
    """Live, current memories whose `created` falls on `day` on the local calendar."""
    out: list[str] = []
    for b in await store.list_all(include_archive=False):
        meta = b.get("metadata") or {}
        bid = str(meta.get("id") or b.get("id") or "")
        if not bid or meta.get("deleted_at") or meta.get("superseded_by"):
            continue
        created = _w.parse_stamp(meta.get("created"))
        if created is not None and created.date().isoformat() == day:
            out.append(bid)
    return out


async def guess_covering(store, gists: list[str], day: str, *,
                         threshold: float = DEFAULT_GUESS_THRESHOLD,
                         top: int = GUESS_TOP) -> list[list[dict]]:
    """For each gist, up to `top` memories created on `day` whose vectors are closest,
    at or above `threshold`: [[{id, score}]]. A hint for the main model, not a
    decision: with embeddings off, or a failing provider, every list is empty."""
    empty: list[list[dict]] = [[] for _ in gists]
    engine = getattr(store, "embedding_engine", None)
    if engine is None or not getattr(engine, "enabled", False) or not gists:
        return empty
    among = await _day_memories(store, day)
    if not among:
        return empty
    out: list[list[dict]] = []
    for gist in gists:
        pairs = await engine.search_similar(gist, top_k=top, among=among)
        out.append([{"id": str(bid), "score": round(float(score), 3)}
                    for bid, score in pairs if float(score) >= threshold])
    return out


# ============================================================
# Intake
# ============================================================

async def take_batch(store, body, *, model: ModelCall,
                     max_lines: int = DEFAULT_MAX_LINES_PER_BATCH,
                     threshold: float = DEFAULT_GUESS_THRESHOLD) -> dict:
    """One batch from the host -> pending slices. `store` is the BucketManager (its
    `slices` store, its embedding engine). Returns

        {batch_id, day, source, slices: [{slice_id, span: {first, last, count}, gist,
         guesses: [{id, short, score}]}], unsliced, replaced}

    Raises BatchError (malformed, nothing is called) or SlicerError (the side model
    failed; nothing is written). The raw text goes no further than the side model."""
    batch = read_batch(body, max_lines=max_lines)
    slices = await slice_lines(batch["lines"], model=model)
    guesses = await guess_covering(store, [s.gist for s in slices], batch["day"],
                                   threshold=threshold)
    row, replaced = await store.slices.record_batch(
        batch_id=batch["batch_id"], source=batch["source"], day=batch["day"],
        revision=batch["revision"],
        lines=[(ln["id"], ln["fingerprint"]) for ln in batch["lines"]],
        slices=[{"first": s.first, "last": s.last, "gist": s.gist, "guesses": g}
                for s, g in zip(slices, guesses)],
        model=str(getattr(model, "model_name", "") or ""))
    covered = sum(s.count for s in slices)
    return {"batch_id": row["batch_id"], "day": row["day"], "source": dict(row["source"]),
            "slices": [store.slices.get(s["slice_id"], public=True) for s in row["slices"]],
            "unsliced": len(batch["lines"]) - covered, "replaced": replaced}


# ============================================================
# The pending store
# ============================================================

SLICES_FILE = "pending_slices.jsonl"
OPEN, CLOSED, REPLACED = "open", "closed", "replaced"
HOW = ("grow", "trace", "drop")
_HOW_WORD = {"grow": "写成了", "trace": "挂到了", "drop": "丢掉了"}
_LOCK_KEY = "pending-slices"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _short_id(bucket_id: str) -> str:
    """The handle a read tool prints (recall's _short_id): a 12-hex id cut to 6, any
    other id in full. The write tools take it back (tools/_common.resolve_bucket_id)."""
    return bucket_id[:6] if re.fullmatch(r"[0-9a-f]{12}", bucket_id) else bucket_id


class PendingSlices:
    """The pending slices of one library (`<buckets>/_sources/pending_slices.jsonl`)."""

    def __init__(self, base_dir):
        self.base_dir = str(base_dir)
        self.path = Path(base_dir) / _src.SOURCES_DIR / SLICES_FILE
        self._guard = threading.RLock()
        self._size = -1
        self._seq = 0
        self._batches: dict[str, dict] = {}
        self._slices: dict[str, dict] = {}

    # ---------- the index ----------

    def rebuild_index(self) -> None:
        with self._guard:
            size = _src._size(self.path)
            self._seq, self._batches, self._slices = 0, {}, {}
            for row in _src._read_lines(self.path):
                self._index(row)
            self._size = size

    def _fresh(self) -> None:
        with self._guard:
            if _src._size(self.path) != self._size:
                self.rebuild_index()

    def _index(self, row: dict) -> None:
        try:
            seq = int(row.get("seq") or 0)
            kind = str(row["kind"])
        except (KeyError, TypeError, ValueError):
            return
        self._seq = max(self._seq, seq)
        if kind == "batch":
            try:
                bid = str(row["batch_id"])
                lines = [(str(i), str(fp)) for i, fp in row["lines"]]
                slices = list(row["slices"])
            except (KeyError, TypeError, ValueError):
                return
            prev = self._batches.get(bid)
            for sid in (prev or {}).get("slice_ids", []):
                st = self._slices.get(sid)
                if st and st["state"] == OPEN:
                    st["state"] = REPLACED
            self._batches[bid] = {
                "batch_id": bid, "seq": seq, "source": dict(row.get("source") or {}),
                "day": row.get("day"), "revision": row.get("revision"), "lines": lines,
                "order": {lid: i for i, (lid, _fp) in enumerate(lines)},
                "slice_ids": [str(s.get("slice_id")) for s in slices],
                "recorded_at": row.get("recorded_at")}
            for s in slices:
                sid = str(s.get("slice_id") or "")
                if sid:
                    self._slices[sid] = {
                        "slice_id": sid, "batch_id": bid, "first": str(s.get("first")),
                        "last": str(s.get("last")), "gist": str(s.get("gist") or ""),
                        "guesses": list(s.get("guesses") or []), "edited": False,
                        "state": OPEN, "closed": None, "seq": seq}
        elif kind in ("close", "recut"):
            st = self._slices.get(str(row.get("slice_id") or ""))
            if st is None:
                return
            if kind == "close":
                st["state"] = CLOSED
                st["closed"] = {"how": row.get("how"), "by": list(row.get("by") or []),
                                "at": row.get("recorded_at")}
            else:
                st["first"], st["last"], st["edited"] = str(row["first"]), str(row["last"]), True

    def _append(self, row: dict) -> dict:
        row = {**row, "seq": self._seq + 1, "recorded_at": _now()}
        _src._append_line(self.path, row)
        self._index(row)
        self._size = _src._size(self.path)
        return row

    def _turn(self):
        from .bucket_manager import _filesystem_turn      # lazy: bucket_manager imports this module
        return _filesystem_turn(self.base_dir, _LOCK_KEY)

    # ---------- reading ----------

    def _span(self, st: dict) -> tuple[int, int]:
        order = self._batches[st["batch_id"]]["order"]
        return order[st["first"]], order[st["last"]]

    def get(self, slice_id: str, *, public: bool = False) -> Optional[dict]:
        """One slice: {slice_id, batch_id, span: {first, last, count}, gist, guesses,
        edited, state, closed}; `public` keeps the fields the host and the model see.
        None when the id is unknown."""
        self._fresh()
        with self._guard:
            st = self._slices.get(str(slice_id or "").strip())
            if st is None:
                return None
            a, b = self._span(st)
            out = {"slice_id": st["slice_id"],
                   "span": {"first": st["first"], "last": st["last"], "count": b - a + 1},
                   "gist": st["gist"],
                   "guesses": [{**g, "short": _short_id(str(g.get("id") or ""))}
                               for g in st["guesses"]]}
            if st["edited"]:
                out["edited"] = True
            if public:
                return out
            return {**out, "batch_id": st["batch_id"], "edited": st["edited"],
                    "state": st["state"],
                    "closed": dict(st["closed"]) if st["closed"] else None}

    def run_length(self, sid: _src.SourceId) -> Optional[int]:
        """How many lines a run of lines spans, when a batch here holds both its ends in
        order; None otherwise (a run the host recorded itself, or a single line)."""
        if not sid.through:
            return None
        source = {"system": sid.system, "instance": sid.instance, "container": sid.container}
        self._fresh()
        with self._guard:
            for b in self._batches.values():
                order = b["order"]
                if b["source"] == source and sid.id in order and sid.through in order:
                    if order[sid.id] <= order[sid.through]:
                        return order[sid.through] - order[sid.id] + 1
            return None

    def pending_count(self) -> int:
        """How many slices wait to be handled."""
        self._fresh()
        with self._guard:
            return sum(1 for st in self._slices.values() if st["state"] == OPEN)

    def open_batches(self) -> list[dict]:
        """The batches with slices still pending, newest first: [{batch_id, source, day,
        revision, recorded_at, slices: [public slice, in line order]}]."""
        self._fresh()
        with self._guard:
            out = []
            for b in sorted(self._batches.values(), key=lambda b: -b["seq"]):
                open_ids = [sid for sid in b["slice_ids"]
                            if self._slices.get(sid, {}).get("state") == OPEN]
                if not open_ids:
                    continue
                open_ids.sort(key=lambda sid: self._span(self._slices[sid]))
                out.append({"batch_id": b["batch_id"], "source": dict(b["source"]),
                            "day": b["day"], "revision": b["revision"],
                            "recorded_at": b["recorded_at"],
                            "slices": [self.get(sid, public=True) for sid in open_ids]})
            return out

    def refusal(self, slice_id: str) -> Optional[SliceError]:
        """Why this slice cannot be handled now, or None when it is open."""
        sid = str(slice_id or "").strip()
        st = self.get(sid)
        if st is None:
            return SliceError(f"no slice {sid!r}",
                              f"没有这片切片：{sid}。recall(view=\"slices\") 看还有哪些待认领。")
        if st["state"] == CLOSED:
            how = (st["closed"] or {}).get("how")
            by = "、".join((st["closed"] or {}).get("by") or [])
            done = _HOW_WORD.get(how, "处理了") + (f" {by}" if by else "")
            return SliceError(f"slice {sid} was already handled ({how})",
                              f"切片 {sid} 已经处理过了（{done}），不能再用。")
        if st["state"] == REPLACED:
            return SliceError(f"slice {sid} was replaced by a resend of its batch",
                              f"切片 {sid} 已经被同一批重送的切片换掉了，"
                              "recall(view=\"slices\") 看现在的。")
        return None

    def record_for(self, slice_id: str) -> dict:
        """The one source record a slice stands for, computed from its span as it is now
        (a re-cut gets its own fingerprint): id = the first line, through = the last
        (none for one line), the batch's source and revision, slice_fingerprint over the
        span's line hashes."""
        self._fresh()
        with self._guard:
            st = self._slices[str(slice_id).strip()]
            b = self._batches[st["batch_id"]]
            a, z = self._span(st)
            span = b["lines"][a:z + 1]
            record = {**b["source"], "id": span[0][0], "revision": b["revision"],
                      "fingerprint": slice_fingerprint([fp for _lid, fp in span]),
                      "fingerprint_by": FINGERPRINT_BY}
            if z > a:
                record["through"] = span[-1][0]
            return record

    # ---------- writing ----------

    async def record_batch(self, *, batch_id: str, source: dict, day: str,
                           revision: Optional[str],
                           lines: list[tuple[str, str]], slices: list[dict],
                           model: str = "") -> tuple[dict, int]:
        """Append a batch and its slices (each gets its slice_id here). The same batch_id
        again replaces the slices of the earlier one still pending; those already
        handled stay handled. Returns (the batch line, how many were replaced)."""
        async with self._turn():
            self._fresh()
            with self._guard:
                prev = self._batches.get(batch_id)
                replaced = sum(1 for sid in (prev or {}).get("slice_ids", [])
                               if self._slices.get(sid, {}).get("state") == OPEN)
                seq = self._seq + 1
                named = []
                for n, s in enumerate(slices):
                    key = f"{batch_id}|{seq}|{n}|{s['first']}|{s['last']}"
                    sid = "sl_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
                    named.append({"slice_id": sid, **s})
                row = self._append({
                    "kind": "batch", "batch_id": batch_id, "source": dict(source),
                    "day": day, "revision": revision,
                    "lines": [[i, fp] for i, fp in lines], "slices": named,
                    "model": model, "prompt_version": SLICER_PROMPT_VERSION})
                return row, replaced

    async def close(self, slice_id: str, how: str, by: list[str]) -> dict:
        """Close an open slice: `how` is grow / trace / drop, `by` the memories it went
        to. Raises SliceError when it is not open."""
        if how not in HOW:
            raise ValueError(f"how must be one of {HOW}")
        async with self._turn():
            self._fresh()
            with self._guard:
                why = self.refusal(slice_id)
                if why:
                    raise why
                return self._append({"kind": "close", "slice_id": str(slice_id).strip(),
                                     "how": how, "by": list(dict.fromkeys(by))})

    async def recut(self, slice_id: str, first: str, last: str) -> dict:
        """Move an open slice's span to first..last, both ids of its batch, in order. The
        gist stays (the text is gone) and is marked edited. Raises SliceError."""
        sid = str(slice_id or "").strip()
        first, last = str(first or "").strip(), str(last or "").strip()
        async with self._turn():
            self._fresh()
            with self._guard:
                why = self.refusal(sid)
                if why:
                    raise why
                b = self._batches[self._slices[sid]["batch_id"]]
                order = b["order"]
                lost = [x for x in (first, last) if x not in order]
                if lost:
                    whole = f"{b['lines'][0][0]}..{b['lines'][-1][0]}"
                    raise SliceError(
                        f"{', '.join(lost)} not in the slice's batch",
                        f"{'、'.join(lost)} 不在这片的那一批里（那批是 {whole}，"
                        f"{len(order)} 行）。")
                if order[first] > order[last]:
                    raise SliceError("first comes after last",
                                     f"{first} 在 {last} 后面——写成 前..后。")
                return self._append({"kind": "recut", "slice_id": sid,
                                     "first": first, "last": last})
