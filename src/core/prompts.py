"""
========================================
core/prompts.py — the side model's prompts the owner may rewrite from the panel
========================================

Each prompt has a key, the text shipped in code (its default), and at most one saved
rewrite per library. The code that calls the side model asks `current(key)` and gets the
rewrite when there is one, the shipped text otherwise; nothing else holds a copy.

    backfill  core/dehydrator.BACKFILL_PROMPT — the one call that fills a new entry's
              blanks (grow's 高级设置). `{kinds}` is replaced per call.
    dream     core/_dream.DREAM_PROMPT — weaving a dream (dream's 高级设置).

A rewrite the parser could not read is refused before it is saved (`missing`): every key
in `required` has to stay in the text. A saved rewrite that a later version's parser no
longer accepts is not used: `current` falls back to the shipped text and logs it, so a
library never stops filling or dreaming over a prompt.

Where a rewrite lives: `<buckets>/_state/prompts.json`, beside the library it shapes.
It is per library, so two libraries on one installation keep their own; it is inside the
library folder, so a backup (core/schema.backup zips that folder) takes it along; and an
export package carries it into a new, empty library (core/export_package.STATE_FILES), the
way a library moves. config.yaml is the installation's (credentials, models, hosts): a
package leaves it behind and it may live outside the library folder.

    {"version": 1, "prompts": {"<key>": {"text": "...", "at": "<local ISO time>"}}}

Nothing here goes into the ledger; the panel's 改过 date is the `at` kept with the text.

Adding a prompt is one row in `PROMPTS`.

Exports: BACKFILL · DREAM · PROMPTS · Prompt · FILE · TEXT_MAX
         keys() · default(key) · missing(key, text) · current(key, base_dir)
         card(key, base_dir) · cards(base_dir) · save(key, text, base_dir, now)
         reset(key, base_dir)
========================================
"""

from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .ledger_mirror import file_lease

FILE = "prompts.json"
_DIR = "_state"
_VERSION = 1

# The longest rewrite taken: the shipped prompts are a few thousand characters, and every
# character is sent with each call.
TEXT_MAX = 20000

BACKFILL = "backfill"
DREAM = "dream"


@dataclass(frozen=True)
class Prompt:
    """One rewritable prompt. `source` is "module:CONSTANT" (read on use, so this module
    imports neither caller); `required` the pieces the parser reads, each of which has to
    stay in the text; `note` the panel's 不建议改 line, word for word as approved."""
    key: str
    source: str
    required: tuple[str, ...]
    note: str


PROMPTS: dict[str, Prompt] = {
    BACKFILL: Prompt(
        key=BACKFILL,
        source="core.dehydrator:BACKFILL_PROMPT",
        # backfill_request fills `{kinds}`; parse_backfill reads the twelve top-level keys,
        # each subject's "kind" and the time's three parts.
        required=("{kinds}",
                  '"name"', '"summary"', '"tags"', '"aliases"', '"domain"', '"subjects"',
                  '"kind"', '"bound"', '"time"', '"phrase"', '"yearly"', '"absolute"',
                  '"internally_generated"', '"evidential"', '"cue_phrasings"',
                  '"looks_like_promise"'),
        note=("不建议改：最后「输出格式」那一段，还有 `{kinds}` 这个占位 —— 程序按这些键名读结果，"
              "改了名字或删了键，那一格就再也填不上；`{kinds}` 会换成你在面板 name 页用的类别。"),
    ),
    DREAM: Prompt(
        key=DREAM,
        source="core._dream:DREAM_PROMPT",
        # parse_dream reads 完整 碎片 v a, and 线索 for the thread candidates.
        required=('"完整"', '"碎片"', '"v"', '"a"', '"线索"'),
        note=("不建议改：最后「只返回 JSON」那一行 —— 程序靠「完整」「碎片」「v」「a」「线索」"
              "这几个名字拆出梦的全文、碎片、情绪和线索，改了名字，梦就存不进去。"),
    ),
}


def keys() -> tuple[str, ...]:
    return tuple(PROMPTS)


def _spec(key: str) -> Prompt:
    spec = PROMPTS.get(str(key))
    if spec is None:
        raise KeyError(key)
    return spec


def default(key: str) -> str:
    """The text shipped in code."""
    module, name = _spec(key).source.split(":")
    return getattr(importlib.import_module(module), name)


def missing(key: str, text: str) -> list[str]:
    """The required pieces `text` lacks, in the table's order; [] when it can be used."""
    text = str(text or "")
    return [piece for piece in _spec(key).required if piece not in text]


def _base(base_dir: str | None) -> str:
    if base_dir:
        return str(base_dir)
    from . import runtime as rt
    return str((rt.config or {}).get("buckets_dir") or ".")


def _path(base_dir: str | None) -> Path:
    return Path(_base(base_dir)) / _DIR / FILE


def _lock(base_dir: str | None) -> Path:
    p = _path(base_dir)
    return p.with_name(p.name + ".lock")


def _load_all(base_dir: str | None) -> dict:
    """{key: {"text", "at"}} as saved, keys this version does not know included (a
    write here keeps them); unreadable or absent is empty."""
    try:
        with open(_path(base_dir), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    got = data.get("prompts") if isinstance(data, dict) else None
    if not isinstance(got, dict):
        return {}
    return {str(k): v for k, v in got.items()
            if isinstance(v, dict) and isinstance(v.get("text"), str)}


def _load(base_dir: str | None) -> dict:
    """The saved rewrites of the keys this version knows (every other prompt is the
    shipped one)."""
    return {k: v for k, v in _load_all(base_dir).items() if k in PROMPTS}


def _save_all(base_dir: str | None, saved: dict) -> None:
    path = _path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": _VERSION, "prompts": saved}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def current(key: str, base_dir: str | None = None) -> str:
    """The text the side model is given: the saved rewrite, or the shipped text when there
    is none or the rewrite lacks a piece the parser reads."""
    saved = _load(base_dir).get(key)
    if saved is None:
        return default(key)
    lacking = missing(key, saved["text"])
    if lacking:
        from . import runtime as rt
        if rt.logger is not None:
            rt.logger.warning(f"[prompts] 改过的 {key} 提示词缺了 {'、'.join(lacking)}，"
                              "这次用的是默认那份")
        return default(key)
    return saved["text"]


def card(key: str, base_dir: str | None = None) -> dict:
    """What the panel's prompt card shows for one key: the text in force or saved, the
    shipped text, whether it was rewritten and on which local day, and the 不建议改 line."""
    spec = _spec(key)
    saved = _load(base_dir).get(key)
    at = str(saved.get("at") or "") if saved else ""
    return {
        "key": key,
        "text": saved["text"] if saved else default(key),
        "default": default(key),
        "edited": saved is not None,
        "changed": at[:10] or None,
        "changed_at": at or None,
        "notes": [spec.note],
    }


def cards(base_dir: str | None = None) -> list[dict]:
    return [card(k, base_dir) for k in PROMPTS]


def save(key: str, text: str, base_dir: str | None, now: datetime) -> dict:
    """Keep `text` as the rewrite of `key`; the card after. A text equal to the shipped
    one is no rewrite and clears it. Raises ValueError naming what is missing, or saying
    it is too long — nothing is written then."""
    _spec(key)
    text = str(text if text is not None else "")
    if len(text) > TEXT_MAX:
        raise ValueError(f"提示词太长了：{len(text)} 字（最多 {TEXT_MAX} 字）")
    lacking = missing(key, text)
    if lacking:
        raise ValueError(f"这样改程序就读不出结果了，缺了：{'、'.join(lacking)}")
    if text == default(key):
        return reset(key, base_dir)
    with file_lease(_lock(base_dir)):
        saved = _load_all(base_dir)
        saved[key] = {"text": text, "at": now.isoformat(timespec="seconds")}
        _save_all(base_dir, saved)
    return card(key, base_dir)


def reset(key: str, base_dir: str | None = None) -> dict:
    """Back to the shipped text: the rewrite is dropped. The card after."""
    _spec(key)
    with file_lease(_lock(base_dir)):
        saved = _load_all(base_dir)
        if key in saved:
            del saved[key]
            _save_all(base_dir, saved)
    return card(key, base_dir)
