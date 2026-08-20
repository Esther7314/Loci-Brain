"""Memory footprint: compress the internal event mirror into one readable line of
history for ``breath``.

A footprint never stores or rewrites memory content, and it is not a second source of
truth. It reads the compatible ``_ledger/events.jsonl``, ignores technical noise such as
touches and re-indexing, and expresses only what is meaningful to the model: creation,
amendment, forgetting, archival, restoration. The older LedgerMirror stays on as the
on-disk adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


_ARCHIVED_KINDS = {"archived", "deleted", "tombstone"}


@dataclass(frozen=True)
class FootprintSnapshot:
    """A read-only footprint snapshot reused for the span of one breath, so that the
    JSONL is not rescanned once per bucket."""

    events_by_trace: dict[str, tuple[dict[str, Any], ...]]

    @classmethod
    def from_events(cls, events: Iterable[dict[str, Any]]) -> "FootprintSnapshot":
        grouped: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            trace_id = str(event.get("trace_id") or "").strip()
            if trace_id:
                grouped.setdefault(trace_id, []).append(dict(event))
        return cls({key: tuple(value) for key, value in grouped.items()})

    def summary(self, trace_id: str, metadata: dict | None = None) -> str:
        labels = [
            label
            for event in self.events_by_trace.get(str(trace_id), ())
            if (label := _event_label(event))
        ]
        if not labels:
            labels = ["已留下"]
        compact: list[str] = []
        for label in labels:
            if compact and compact[-1].split("×", 1)[0] == label:
                base = compact[-1].split("×", 1)[0]
                count = int(compact[-1].split("×", 1)[1]) + 1 if "×" in compact[-1] else 2
                compact[-1] = f"{base}×{count}"
            else:
                compact.append(label)
        # Creation is always kept; when the middle runs long, keep only the earliest step
        # and the most recent few.
        if len(compact) > 4:
            compact = [compact[0], "…", *compact[-2:]]
        return "👣 Footprint：" + " → ".join(compact)

    def original_kind(self, trace_id: str, metadata: dict | None = None) -> str:
        for event in self.events_by_trace.get(str(trace_id), ()):
            kind = str(event.get("trace_kind") or "").strip().lower()
            if kind and kind not in _ARCHIVED_KINDS:
                return kind
        meta_kind = str((metadata or {}).get("type") or "").strip().lower()
        return meta_kind if meta_kind not in _ARCHIVED_KINDS else "dynamic"


def _event_label(event: dict[str, Any]) -> str:
    event_type = str(event.get("event_type") or "")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if event_type == "TraceCreated":
        return "创建"
    if event_type == "TraceRestored":
        return "重新回忆"
    if event_type == "TraceArchived":
        return "淡去归档"
    if event_type == "TraceDeletedToArchive":
        return "删除到档案"
    if event_type == "TraceHardDeleted":
        return "测试痕迹清理"
    if event_type != "TraceUpdated":
        return ""  # TraceTouched and unknown technical events do not spend breath tokens.

    fields = {str(item) for item in payload.get("changed_fields") or []}
    if "last_merged_by" in fields:
        return "事件补充"
    if "dont_surface" in fields:
        return "主动淡忘" if payload.get("dont_surface") else "重新浮现"
    if "pinned" in fields:
        return "钉为核心" if payload.get("pinned") else "解除核心"
    if "anchor" in fields:
        return "设为地标" if payload.get("anchor") else "解除地标"
    if "resolved" in fields:
        return "已经放下" if payload.get("resolved") else "重新激活"
    if "content" in fields:
        return "正文重构"
    return "更新"
