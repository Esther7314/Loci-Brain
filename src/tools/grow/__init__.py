"""
========================================
tools/grow/__init__.py — grow tool entry point
========================================

grow is "I file something into memory". Short content (<30 chars) took the
shortpath,
⚰️ the tool face no longer accepts long prose (see the epitaph at the end of dispatch); the two old paths, shortpath and core, are kept in the archive but no longer have an entry point
a standalone event bucket.

Key behaviour:
- The entry point validates items
- Which branch to take is decided by stripped length < 30 characters

What this file deliberately does not do:
- No token-level budgeting (grow cares about "how many pieces", not "how much to show")
- Returns no structured data; always one short sentence

Exports: dispatch(items=... / kind+text) -> str
========================================
"""

from typing import Optional

from .. import _runtime as rt
from .._common import check_grow_input_size, check_grow_items_payload
# ⚰️ The two old long-prose splitting paths (grow_shortpath / grow_core) were
#    deleted, code and all.
#    "Retire it, don't delete the archive" is about **data** (memories, the
#    thirteen seeds) — not about code. Leaving a pile of uncalled dead code in a
#    repository that ships only makes the next reader think it is still alive.
from .core import grow_items
from .rooms_path import (grow_event, grow_mind, backfill_sweep,
                         _retired_fields_msg)

# A self-healing scan runs once, on the first grow call in each process:
# background backfills still in flight are lost across a restart, and the sweep
# repairs buckets that have a room but are missing their summary.
_sweep_started = False


async def dispatch(
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
) -> str:
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

    # ⚰️ The two retired parameters `importance` / `meaning` were **removed
    #    entirely**. They used to be kept so that passing one produced a
    #    human-readable complaint; that job now belongs to extra="forbid" on the
    #    tool face (the grow block in server.py) — passing one is rejected by
    #    parameter validation, which is cleaner than keeping a pair of fake
    #    parameters around.
    if kind in ("event", "mind", "big"):
        global _sweep_started
        if not _sweep_started:
            _sweep_started = True
            import asyncio as _asyncio
            _asyncio.create_task(backfill_sweep())
    if kind == "event":
        return await grow_event(items or [], direction_of_fit=direction_of_fit,
                                bound=bound, evidential=evidential,
                                internally_generated=internally_generated, weight=weight,
                                from_ids=from_, test_data=test_data)
    if kind == "mind":
        return await grow_mind(room, text, from_, v, a,
                               direction_of_fit=direction_of_fit, bound=bound,
                               evidential=evidential,
                               internally_generated=internally_generated,
                               weight=weight, test_data=test_data)
    if kind == "big":
        # ⚰️ `kind="big"` was pulled from the tool face.
        #    Underneath it called fold's own bones (`_F.save_gist`) — it was a
        #    **pure alias**. Naming a stretch of time had two entry points, and
        #    two entry points sooner or later tell two different stories.
        #    What is left is the single path fold(when="起..止"). The grow_big
        #    implementation is still there; nothing calls it.
        return ('立一个「时期」（给一段日子起个名字）用 fold：\n'
                '  fold(when="2026-08-15..2026-08-18", room="EVENT/SELF", '
                'text="那阵子在做什么", v=…, a=…)\n'
                'grow 只管存发生了什么（items）和你从中看出什么（kind="mind"）。')
    if kind:
        return (f'kind 无效：{kind}。可选："event"（发生了什么）/ '
                '"mind"（我从中看出什么）。'
                '给一段日子起名字是 fold 的活。')

    # --- Everything below is the old path, kept verbatim ---
    # Pre-split mode: the calling model has already produced N final bodies ->
    # store them verbatim, skipping digest's second round of rewriting.
    # Passing items (a non-empty list) takes this path; omitting it behaves
    # exactly like the old version (backward compatible).
    if isinstance(items, list) and len(items) > 0:
        err = check_grow_items_payload(items)
        if err:
            return err
        return await grow_items(items)

    # ⚰️ `content` (throw in a long passage and let the system split it into
    #    several) was cut.
    #    The rule behind it: **that was the one place in the whole system where
    #    the system decided for me how many things this was**, which runs against
    #    "the one who writes it down is always me"; and `items=[...]` already
    #    covers the case completely — working out for yourself, at the end of a
    #    stretch, how many things happened and then storing them in one call is
    #    the posture this system is built around.
    #    (The two splitting paths `grow_core` / `grow_shortpath`, along with
    #     dehydrator.cut(), **stay in the archive**; they simply have no entry
    #     point on the tool face any more.)
    return ("grow 现在只收拆好的：items=[{room,text,v,a},...] 存多条，"
            "或 kind=\"mind\"/\"big\" + text 存一条。"
            "长文丢进来让系统替你拆成几条那条路已经撤了——"
            "这一摊是几件事，得你自己说了算。")
