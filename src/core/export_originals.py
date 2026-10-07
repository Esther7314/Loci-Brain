# -*- coding: utf-8 -*-
"""
========================================
core/export_originals.py — the setting page's 「导出原话」: the words as they were said
========================================

`GET /api/loci/export/originals` hands this ZIP to the person. It is for reading, not for
bringing back (that is the export package, core/export_package.py). It holds the original
text Loci itself keeps, and nothing else:

    读我.md                         what is in it, what is not, and how many were left out
    导入的聊天/<batch>/<c…> <title>.md
                                    one imported conversation (core/import_memory.py),
                                    every line with who said it and when, in order
    沉下去的原文/<YYYY-MM-DD>.md     the originals of sunk entries (archive/原文/<id>.txt,
                                    one per entry), merged into one file per local day the
                                    entry was written, oldest first within the day

What never goes in:
  · the text of a withdrawn or deleted source: an imported line the registry reads as
    withdrawn or deleted (on its own or through a run over it), a batch being withdrawn,
    and the sunk original of an entry the export package leaves out (soft-deleted, standing
    on a withdrawn source, or derived from one: export_package.partition, the same line
    every export draws). A withheld stretch of a conversation is marked by how many lines
    it was, never by what they said.
  · the host's own conversations: they stay with the host.
  · anything a model wrote: no summary, name, draft or gist, not even as a heading (a
    sunk original is headed by its time and id). The text of the host's
    self-compression is never stored by Loci, so no member and no field can carry it.

Like the export package, the ZIP is written to a temporary file the caller owns and then
handed over whole.

Exports: README · IMPORTS_FOLDER · SUNK_FOLDER · build
========================================
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import zipfile
from pathlib import Path

from . import _sources as _src
from . import _when as _w
from . import import_memory as _imports
from .export_package import partition
from .scope import IMPORT_SYSTEM

README = "读我.md"
IMPORTS_FOLDER = "导入的聊天"
SUNK_FOLDER = "沉下去的原文"
_NO_DAY = "没有日子"
_TITLE_CHARS = 40
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]+')


def _safe(name: str) -> str:
    """A title as a file name: no separators or characters a file system refuses."""
    cleaned = _UNSAFE.sub(" ", str(name or "")).strip().strip(".")
    return re.sub(r"\s+", " ", cleaned)[:_TITLE_CHARS].strip()


def _gone_line(registry, batch: str, container: str, line_id: str) -> bool:
    if registry is None:
        return False
    sid = _src.SourceId(IMPORT_SYSTEM, batch, container, line_id)
    return registry.state_of(sid) in (_src.WITHDRAWN, _src.DELETED)


def _withheld(count: int) -> str:
    return f"（这里有 {count} 行的来源撤回或删除了，不导出）"


def _conversation_md(meta: dict, conv: dict, rows: list[dict], registry) -> tuple[str, int, int]:
    """(the Markdown, lines written, lines withheld) for one conversation."""
    batch = str(meta.get("batch"))
    container = str(conv.get("container") or "")
    same_self = bool(meta.get("same_self", True))
    human = str(meta.get("human") or "用户")
    title = str(conv.get("title") or "").strip() or "（没有标题）"
    about = [f"导入批次 {batch}"]
    if meta.get("filename"):
        about.append(f"文件 {meta.get('filename')}")
    about.append(f"第 {container} 段")
    if conv.get("day"):
        about.append(str(conv.get("day")))
    out = [f"# {title}", "", " · ".join(about), ""]
    written = withheld = run = 0
    for row in rows:
        if _gone_line(registry, batch, container, str(row.get("id") or "")):
            withheld += 1
            run += 1
            continue
        if run:
            out += [_withheld(run), ""]
            run = 0
        who = _imports.speaker_label(row.get("role"), same_self, human)
        at = _imports._short_at(row.get("at"))
        out += [f"**{who}**" + (f" · {at}" if at else ""), "", str(row.get("text") or ""), ""]
        written += 1
    if run:
        out += [_withheld(run), ""]
    return "\n".join(out), written, withheld


def _sunk_day(meta: dict) -> tuple[str, str]:
    """(local day it was written, the local stamp to order by)."""
    stamp = _w.parse_stamp(meta.get("created"))
    if stamp is None:
        return _NO_DAY, ""
    return stamp.date().isoformat(), stamp.isoformat(timespec="seconds")


def _day_md(day: str, items: list[tuple[str, str, str]]) -> str:
    """One day's sunk originals, oldest first. Each is headed by the time it was written
    and its id only: a summary or a name is model-written, not the original."""
    out = [f"# {day} · 沉下去的原文", "",
           "这一天写下、后来沉下去的记忆：库里的正文只剩摘要，原话在这里。", ""]
    for stamp, bid, text in sorted(items):
        when = stamp[11:16] if len(stamp) >= 16 else ""
        out += [f"## {when + ' · ' if when else ''}{bid}", "", text.rstrip(), ""]
    return "\n".join(out)


def _readme(counts: dict) -> str:
    lines = [
        "# 导出原话", "",
        f"- {IMPORTS_FOLDER}/：导入进来的聊天，{counts['conversations']} 段对话，"
        f"一段一个文件，按批次分文件夹（{counts['lines']} 行）。",
        f"- {SUNK_FOLDER}/：沉下去的记忆的原话，{counts['sunk']} 条，"
        f"按写下的那天并成一天一个文件（{counts['days']} 天）。", "",
        "不在这里的：",
        "- 宿主那边的聊天，在宿主自己那儿。",
        "- 来源撤回或删除了的原话、删掉的记忆的原话，一律不导出"
        f"（这次没导出：导入的 {counts['lines_withheld']} 行，"
        f"沉下去的原文 {counts['sunk_left_out']} 条）。",
        "- 模型写的东西（摘要、草稿、概括）不是原话，不在这里。", ""]
    return "\n".join(lines)


async def build(store) -> tuple[str, dict]:
    """Write the ZIP to a temporary file and return (its path, the counts). The caller owns
    the file."""
    entries = await store.list_all(include_archive=True)
    return await asyncio.to_thread(_write, store, entries)


def _write(store, entries: list[dict]) -> tuple[str, dict]:
    base = str(store.base_dir)
    registry = getattr(store, "sources", None)
    counts = {"conversations": 0, "lines": 0, "lines_withheld": 0,
              "sunk": 0, "days": 0, "sunk_left_out": 0}

    members: list[tuple[str, str]] = []
    imports = _imports.ImportStore(base)
    for meta in imports.batches():
        if meta.get("status") == _imports.WITHDRAWING:
            continue
        batch = str(meta.get("batch"))
        for conv in meta.get("conversations") or []:
            container = str(conv.get("container") or "")
            rows = imports.lines(batch, container)
            if not rows:
                continue
            text, written, withheld = _conversation_md(meta, conv, rows, registry)
            counts["lines_withheld"] += withheld
            if not written:
                continue
            name = " ".join(x for x in (container, _safe(conv.get("title"))) if x)
            members.append((f"{IMPORTS_FOLDER}/{batch}/{name}.md", text))
            counts["conversations"] += 1
            counts["lines"] += written

    kept, deleted, gone = partition(entries, registry)
    days: dict[str, list[tuple[str, str, str, str]]] = {}
    for b in kept:
        meta = b.get("metadata") or {}
        if str(meta.get("decay_stage") or "") != "sunk":
            continue
        bid = str(b.get("id") or meta.get("id") or "")
        path = Path(store._sunk_orig_path(bid))
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        day, stamp = _sunk_day(meta)
        days.setdefault(day, []).append((stamp, bid, text))
        counts["sunk"] += 1
    counts["sunk_left_out"] = sum(
        1 for bid in set(deleted) | set(gone) if bid and os.path.isfile(store._sunk_orig_path(bid)))
    for day in sorted(days):
        members.append((f"{SUNK_FOLDER}/{day}.md", _day_md(day, days[day])))
    counts["days"] = len(days)

    fd, path = tempfile.mkstemp(prefix="loci-originals-", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(README, _readme(counts))
            for name, text in members:
                archive.writestr(name, text)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path, counts
