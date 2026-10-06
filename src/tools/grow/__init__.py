"""
========================================
tools/grow/__init__.py — grow tool entry point
========================================

grow is "I file something into memory": kind="event" writes each item as a
standalone event bucket, kind="mind" writes one insight. The tool face does not
accept long prose to be split up.

Key behaviour:
- The entry point validates items
- Which path to take is decided by `kind`; a call without one is refused

What this file deliberately does not do:
- No token-level budgeting (grow cares about "how many pieces", not "how much to show")
- Returns no structured data; always one short sentence

`slice="sl_…"` writes from a pending slice of the host's raw lines: the slice becomes
one of the entry's sources and the slice is closed (tools/_slices.py).

Exports: dispatch(items=... / kind+text) -> str
========================================
"""

import functools
from typing import Optional

from core import _sources as _src
from core import runtime as rt
from .._common import check_grow_items_payload, with_write_key
from .._slices import with_records, write_from_slice
from .rooms_path import (grow_event, grow_mind, backfill_sweep,
                         _retired_fields_msg)

# A self-healing scan runs once, on the first grow call in each process:
# background backfills still in flight are lost across a restart, and the sweep
# repairs buckets that have a room but are missing their summary.
_sweep_started = False


async def _dispatch(
    items: Optional[list] = None,
    kind: str = "",
    room: str = "",
    text: str = "",
    from_=None,
    v=-1,
    a=-1,
    direction_of_fit: str = "",
    bound=None,
    evidential: str = "",
    internally_generated: bool = False,
    weight=None,
    test_data: bool = False,
    when: str = "",
    cue=None,
    exception_of: str = "",
    hold: str = "",
    card_of: str = "",
    sources=None,
    slice_id: str = "",
) -> str:
    # A pending slice (core/_slicer.py) handed in: the same write, with the slice's
    # records added to `sources`, and the slice closed on what it wrote (tools/_slices.py).
    # locals() here, before anything else is bound, is exactly this call's arguments.
    if str(slice_id or "").strip():
        call = {k: v for k, v in locals().items() if k not in ("slice_id", "sources")}
        given = sources
        return await write_from_slice(
            slice_id, "grow",
            lambda records: _dispatch(**call, sources=with_records(given, records)))
    await rt.decay_engine.ensure_started()

    # Clients like GLM sometimes serialise the list as a JSON string — accept it
    # leniently (hit for real on a phone client)
    import json as _json
    if isinstance(items, str):
        try:
            items = _json.loads(items)
        except (ValueError, TypeError):
            pass
    if isinstance(from_, str) and from_.strip().startswith("["):
        try:
            from_ = _json.loads(from_)
        except (ValueError, TypeError):
            pass
    if isinstance(cue, str) and cue.strip().startswith("{"):
        try:
            cue = _json.loads(cue)
        except (ValueError, TypeError):
            pass

    # A top-level `when` is the default for every item that does not carry its own:
    # the tool face documents grow(kind="event", when="2026-09-01", items=[...]).
    when = str(when or "").strip()
    if when and isinstance(items, list):
        items = [
            {**item, "when": when}
            if isinstance(item, dict) and not str(item.get("when") or "").strip()
            else item
            for item in items
        ]

    # --- The kind=event|mind path ---
    # The body lands on disk first and the real id comes back immediately; tags,
    # gist and naming are backfilled in the background, and nothing is merged.
    # See the comment at the top of rooms_path.py.
    kind = (kind or "").strip().lower()

    # There is no `importance` / `meaning`: passing one is rejected by parameter
    #    validation (extra="forbid" on the tool face, the grow block in server.py),
    #    which is cleaner than keeping a pair of fake parameters around.
    if kind in ("event", "mind", "big"):
        global _sweep_started
        if not _sweep_started:
            _sweep_started = True
            import asyncio as _asyncio
            from utils import now_iso as _now_iso
            _asyncio.create_task(backfill_sweep(before=_now_iso()))
    if kind == "event":
        if str(card_of or "").strip():
            return ('名字卡是一条 mind：grow(kind="mind", room="MIND/TRAITS"（人）或 '
                    '"MIND/VIEWS"（东西）, card_of="名字", text=…, from=[…], v=…, a=…)。')
        payload_err = check_grow_items_payload(items or [])
        if payload_err:
            return payload_err
        return await grow_event(items or [], direction_of_fit=direction_of_fit,
                                bound=bound, evidential=evidential,
                                internally_generated=internally_generated, weight=weight,
                                from_ids=from_, test_data=test_data,
                                cue=cue, exception_of=exception_of, hold=hold,
                                sources=sources)
    if kind == "mind":
        if str(exception_of or "").strip() or str(hold or "").strip():
            return ('条子是一条事件：grow(kind="event", exception_of="约定的id", '
                    'hold="defer", items=[{room, text, v, a}])。')
        return await grow_mind(room, text, from_, v, a,
                               direction_of_fit=direction_of_fit, bound=bound,
                               evidential=evidential,
                               internally_generated=internally_generated,
                               weight=weight, test_data=test_data, cue=cue,
                               card_of=card_of, sources=sources, when=when)
    if kind == "big":
        # `kind="big"` is answered with the way to fold: naming a stretch of time
        #    has a single path, fold(when="起..止"). Two entry points sooner or
        #    later tell two different stories.
        return ('立一个「时期」（给一段日子起个名字）用 fold：\n'
                '  fold(when="2026-08-15..2026-08-18", room="EVENT/SELF", '
                'text="那阵子在做什么", v=…, a=…)\n'
                'grow 只管存发生了什么（items）和你从中看出什么（kind="mind"）。')
    if kind:
        return (f'kind 无效：{kind}。可选："event"（发生了什么）/ '
                '"mind"（我从中看出什么）。'
                '给一段日子起名字是 fold 的活。')

    # No kind: refused, with what to write.
    return ('grow 要说存的是什么：kind="event" + items=[{room, text, v, a, when?}, ...] '
            '存发生了什么（想要的事也是事件，加 direction_of_fit="telic"）；'
            'kind="mind" + room + text + from 存你从中看出的一句。')


@functools.wraps(_dispatch)
async def dispatch(*args, **kwargs) -> str:
    # One write call, one write key: a resend of the same delivery gets the first reply
    # back (core/_sources.SourceRegistry.run_once); without a key it simply runs.
    return await with_write_key(_src.current_write_key(),
                                lambda: _dispatch(*args, **kwargs), op="grow")
