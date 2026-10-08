# -*- coding: utf-8 -*-
"""
========================================
core/thresholds.py — the similarity lines, read live from config's `thresholds:` section
========================================

Six numbers decide when two texts count as "about the same thing". Five are cosine lines
between two vectors, one is recall's relevance floor on its 0–100 combined score:

    reconsolidation     0.80  the look-back after a write: which old view the new body
                              runs into in meaning (core/_reconsolidation.py)
    fold_merge          0.80  whether two gists say the same thing and a merge is asked
                              (tools/fold)
    backfill_duplicate  0.80  the backfill's 「疑似同件」 sticker (tools/grow/_backfill.py)
    recall_meaning      0.65  recall's 意思 mark: a hit at or above it matched in meaning
                              (core/_bucket_search.py)
    slice_guess         0.65  a slice's guess at what was already written that day
                              (web/host_api, the grow page)
    recall_floor        35    recall's relevance floor: what scores below it is counted
                              in one line, not shown (tools/recall/_search.py)

Every reader asks `value(key)` at the moment it decides, so an edit on the panel
(`POST /api/config` with `thresholds:`) applies to the next call with no restart. With no
`thresholds:` section the defaults below run, which are the values the code was tuned
with. Two older homes still count as the default when `thresholds:` does not set the
line: `slices.guess_threshold` for slice_guess and the `LOCI_RELEVANCE_FLOOR` environment
variable for recall_floor.

The cosine lines were tuned on one embedding model; another model spreads its cosines
differently. When the live model is not the one the lines were last looked at with, the
setting page shows a note that they may need retuning (`retune`). Nothing is reset: the
note goes away when the person saves a line or dismisses it (`settle`). The model the lines
were last looked at with is kept in `<buckets>/_state/thresholds_model.json`; the first time
it is asked for, the live model is taken as that one, and the panel's model switch records
the outgoing model before it swaps (`remember_model`) so the first switch is noticed.

Exports: RECONSOLIDATION · FOLD_MERGE · BACKFILL_DUPLICATE · RECALL_MEANING · SLICE_GUESS ·
         RECALL_FLOOR · LINES · Line · value · default · rows · validate · retune ·
         settle · remember_model
========================================
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import runtime as rt

logger = logging.getLogger("loci_brain.thresholds")

SECTION = "thresholds"
STATE_FILE = "thresholds_model.json"
_STATE_DIR = "_state"
FLOOR_ENV = "LOCI_RELEVANCE_FLOOR"

RECONSOLIDATION = "reconsolidation"
FOLD_MERGE = "fold_merge"
BACKFILL_DUPLICATE = "backfill_duplicate"
RECALL_MEANING = "recall_meaning"
SLICE_GUESS = "slice_guess"
RECALL_FLOOR = "recall_floor"

_COSINE = "余弦"
_SCORE = "综合分（0–100）"


@dataclass(frozen=True)
class Line:
    """One line: its key under `thresholds:`, the value the code was tuned with, the range
    the panel may set it in, and what it decides (shown to the person)."""
    key: str
    default: float
    low: float
    high: float
    scale: str
    decides: str


# Cosine lines below 0.30 let nearly any two texts through on any model; 1.00 is allowed so
# a line can be set to "only the same text".
LINES: tuple[Line, ...] = (
    Line(RECONSOLIDATION, 0.80, 0.30, 1.00, _COSINE,
         "回望：新写的东西跟哪条旧看法算「撞意思」"),
    Line(FOLD_MERGE, 0.80, 0.30, 1.00, _COSINE,
         "两条概括说的是不是一回事、要不要问合并"),
    Line(BACKFILL_DUPLICATE, 0.80, 0.30, 1.00, _COSINE,
         "回填时「可能是同一件事」的提示"),
    Line(RECALL_MEANING, 0.65, 0.30, 1.00, _COSINE,
         "recall 里「意思」那个标记"),
    Line(SLICE_GUESS, 0.65, 0.30, 1.00, _COSINE,
         "切片「白天像是已经记过哪条」的猜测"),
    Line(RECALL_FLOOR, 35.0, 0.0, 100.0, _SCORE,
         "recall 的相关线：低于它的不摆出来（语义占 2.5 份）"),
)
_BY_KEY = {line.key: line for line in LINES}

DISMISS = "dismiss_retune"


def _config(config) -> Mapping:
    cfg = rt.config if config is None else config
    return cfg if isinstance(cfg, Mapping) else {}


def _section(config) -> Mapping:
    got = _config(config).get(SECTION)
    return got if isinstance(got, Mapping) else {}


def _number(raw) -> float | None:
    """A finite number, or None (a bool is not a number here)."""
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _in_range(line: Line, raw) -> float | None:
    v = _number(raw)
    return v if v is not None and line.low <= v <= line.high else None


def default(key: str, config=None) -> float:
    """The value a line runs on when `thresholds:` does not set it."""
    line = _BY_KEY[key]
    if key == SLICE_GUESS:
        slices = _config(config).get("slices")
        if isinstance(slices, Mapping) and "guess_threshold" in slices:
            v = _number(slices.get("guess_threshold"))
            if v is not None:
                return v
    if key == RECALL_FLOOR:
        v = _number(os.environ.get(FLOOR_ENV, "") or None)
        if v is not None:
            return v
    return line.default


def value(key: str, config=None) -> float:
    """The line as it runs now: `thresholds.<key>` when it is a number in range, else the
    default. `config` defaults to the running configuration (core/runtime)."""
    v = _in_range(_BY_KEY[key], _section(config).get(key))
    return default(key, config) if v is None else v


def rows(config=None) -> list[dict]:
    """Every line for the setting page: {key, value, default, low, high, scale, decides,
    set, note}. `set` says `thresholds:` holds a value for it; `note` says why a value
    written there by hand is not the one running."""
    section = _section(config)
    out = []
    for line in LINES:
        raw = section.get(line.key)
        given = line.key in section and raw is not None
        note = ""
        if given and _in_range(line, raw) is None:
            note = (f"config.yaml 里写的 {raw!r} 不是 {line.low:g} 到 {line.high:g} 之间的数，"
                    "现在用的是缺省")
        out.append({"key": line.key, "value": value(line.key, config),
                    "default": default(line.key, config), "low": line.low,
                    "high": line.high, "scale": line.scale, "decides": line.decides,
                    "set": given and not note, "note": note})
    return out


def validate(payload) -> tuple[dict, bool, list[str]]:
    """The `thresholds` object of a POST /api/config -> (changes {key: value, or None to go
    back to the default}, whether the retune note is dismissed, what is wrong with it).
    Nothing is applied when the list of problems is not empty."""
    if not isinstance(payload, Mapping):
        return {}, False, ["thresholds 要是一个对象"]
    changes: dict = {}
    problems: list[str] = []
    dismiss = False
    for key, raw in payload.items():
        if key == DISMISS:
            if not isinstance(raw, bool):
                problems.append(f"thresholds.{DISMISS} 只能是 true / false")
            dismiss = raw is True
            continue
        line = _BY_KEY.get(str(key))
        if line is None:
            problems.append(f"没有叫 thresholds.{key} 的线（有：{'、'.join(_BY_KEY)}）")
            continue
        if raw is None:
            changes[line.key] = None
            continue
        v = _in_range(line, raw)
        if v is None:
            problems.append(f"thresholds.{line.key} 要是 {line.low:g} 到 {line.high:g} 之间的数"
                            f"（{line.scale}），收到 {raw!r}")
            continue
        changes[line.key] = v
    return changes, dismiss, problems


# ============================================================
# The model the lines were last looked at with
# ============================================================

def _state_path(base_dir: str) -> Path:
    return Path(str(base_dir)) / _STATE_DIR / STATE_FILE


def _same(a: str, b: str) -> bool:
    from .embedding_engine import _norm_model
    return _norm_model(str(a or "")) == _norm_model(str(b or ""))


def _read_model(base_dir: str) -> str:
    try:
        with open(_state_path(base_dir), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return ""
    return str(data.get("tuned_on") or "") if isinstance(data, dict) else ""


def _write_model(base_dir: str, model: str) -> None:
    from utils import atomic_write_text, now_iso
    path = _state_path(base_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"tuned_on": str(model), "at": now_iso()},
                                           ensure_ascii=False))
    except OSError as exc:
        logger.warning("could not record the model the thresholds were tuned on: %s", exc)


def remember_model(base_dir: str, model: str) -> None:
    """Record `model` as the one the lines were looked at with, unless one is recorded
    already. Called with the outgoing model just before the embedding model changes."""
    if base_dir and str(model or "").strip() and not _read_model(base_dir):
        _write_model(base_dir, model)


def settle(base_dir: str, model: str) -> None:
    """The person has looked at the lines with `model` live (saved one, or dismissed the
    note): the note goes away until the model changes again."""
    if base_dir and str(model or "").strip():
        _write_model(base_dir, model)


def retune(base_dir: str, live_model: str) -> dict:
    """{needed, tuned_on, model, words}: whether the live embedding model differs from the
    one the lines were last looked at with."""
    live = str(live_model or "").strip()
    out = {"needed": False, "tuned_on": "", "model": live, "words": ""}
    if not base_dir or not live:
        return out
    tuned_on = _read_model(base_dir)
    if not tuned_on:
        _write_model(base_dir, live)
        out["tuned_on"] = live
        return out
    out["tuned_on"] = tuned_on
    if not _same(tuned_on, live):
        out["needed"] = True
        out["words"] = (f"向量模型换了（{tuned_on} → {live}）。这几条余弦线是按原来的模型调的，"
                        "新模型的分数分布可能不一样，看一眼要不要重调；这里不会自动改回缺省。")
    return out
