"""Shared helpers for the append-only plan change log."""

from datetime import datetime
from typing import Any

__all__ = ["append_plan_change_log"]


def append_plan_change_log(
    old_history: Any,
    action: str,
    **fields: Any,
) -> list[dict[str, Any]]:
    """Copy a plan history and append one normalized timestamped entry."""
    history = list(old_history or [])
    entry: dict[str, Any] = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "action": action,
    }
    for key, value in fields.items():
        if value is not None:
            entry[key] = value
    history.append(entry)
    return history

