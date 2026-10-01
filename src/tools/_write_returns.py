"""
========================================
tools/_write_returns.py — what a write's return finds out on the spot
========================================

Plan part three: what can be noticed at the moment of writing is said in the write tool's
own return, never queued for later. grow and regrow call `noticed()` once, after the
entries are on disk and before the return is handed back:

  回望 (reconsolidation)  the old views the new bodies run into — core/_reconsolidation.py
  场景常来 (case_recall)  a scene word the last two weeks keep coming back to — core/_case_recall.py

Both read the library as it stood before the write (the caller lists it before writing: a
write clears the store's cache, and listing after it would re-read every file), both go
through the gate under the request's read scope, and neither can fail the write: anything
that goes wrong here is logged and the return goes out without these lines.

Exports: noticed(library, writes, *, skip, scene_texts) -> list[str]
========================================
"""

from core import _case_recall as _C
from core import _reconsolidation as _R
from core import _when as _w

from . import _runtime as rt
from ._common import read_scope


async def noticed(library: list, writes: list[tuple[str, str]], *, skip=(),
                  scene_texts: list[str] = ()) -> list[str]:
    """The lines to append to a write's return. `writes` are (new id, body) for the look-back,
    `skip` their lineage; `scene_texts` are the bodies that may earn the scene question (new
    events that happened — not a want, a dream, a hold or a new version)."""
    if not writes or library is None:
        return []
    store = rt.bucket_mgr
    now = _w.now()
    lines: list[str] = []
    try:
        view = await read_scope()
    except Exception as e:  # noqa: BLE001 - without a scope nothing is shown, the write stands
        rt.logger.warning(f"write return: read scope unavailable, nothing noticed: {e}")
        return []
    try:
        hits = await _R.look_back(store, library, writes, scope=view, now=now, skip=skip)
        lines += _R.render(hits)
        _R.record_shown(store, hits)
    except Exception as e:  # noqa: BLE001 - the look-back is a hint; the write is done
        rt.logger.warning(f"write return: look-back failed: {e}")
    if scene_texts:
        try:
            buckets_dir = str(getattr(store, "base_dir", "") or (rt.config or {}).get("buckets_dir") or ".")
            q = _C.ask(library, list(scene_texts), buckets_dir=buckets_dir, scope=view, now=now)
            if q is not None:
                lines.append(_C.render(q))
                _C.record_asked(buckets_dir, q.word, _w.to_local(now).date())
                _C.record_shown(store, q)
        except Exception as e:  # noqa: BLE001 - the question is a hint; the write is done
            rt.logger.warning(f"write return: scene question failed: {e}")
    return lines
