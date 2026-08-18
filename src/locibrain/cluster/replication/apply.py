from __future__ import annotations

from locibrain.fabric.storage.engine import MemoryFabric
from locibrain.protocol.schemas import MemoryEvent


def apply_committed_event(fabric: MemoryFabric, event: MemoryEvent) -> int:
    return fabric.append_event(event)
