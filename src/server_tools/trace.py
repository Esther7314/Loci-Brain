# -*- coding: utf-8 -*-
"""
server_tools/trace.py — trace: change an entry's metadata, or put it away

The face a client sees: the signature, the description, and the one call that
forwards to tools/trace/ through server_call._with_notice. server.py mounts it
(mcp.tool()) and then runs adapt(mcp).
"""

import logging
from typing import Annotated, Optional

from pydantic import Field as _PydField

from core.strict_args import harden as _harden_tool
from tools import trace as _t_trace
from server_call import _with_notice

logger = logging.getLogger("loci_brain")


async def trace(
    bucket_id: Annotated[str, _PydField(description=(
        "Which entry to change. Required, except to re-cut or drop a slice."
    ))] = "",
    name: Annotated[Optional[str], _PydField(description=(
        "The entry's title."
    ))] = "",
    domain: Annotated[Optional[str], _PydField(description=(
        "Its subject, which also decides the folder it lives in."
    ))] = "",
    valence: Annotated[float, _PydField(description=(
        "0~1."
    ))] = -1,
    arousal: Annotated[float, _PydField(description=(
        "0~1."
    ))] = -1,
    tags: Annotated[Optional[str], _PydField(description=(
        "Its tags."
    ))] = "",
    pinned: Annotated[int, _PydField(description=(
        "1 pins it as a principle; 0 unpins. Nothing is refused: if it does not read "
        "like something you mean to do, it is still pinned and you get a note back."
    ))] = -1,
    delete: Annotated[bool, _PydField(description=(
        "True moves it to the archive. A soft delete; it can always be brought back."
    ))] = False,
    status: Annotated[Optional[str], _PydField(description=(
        '"resolved" done / "abandoned" not doing it / "active" back on the table.'
    ))] = "",
    direction_of_fit: Annotated[Optional[str], _PydField(description=(
        '"telic": this is something wanted — a promise, a plan, a wish ("finish her gift '
        'before her birthday"). "thetic": a record of how things are or were. Change it '
        "when an entry was stored as the wrong one."
    ))] = "",
    bound: Annotated[Optional[list], _PydField(description=(
        "For something wanted: who is bound by it. Names, or 我 for yourself and 你 for "
        'the person you talk to. ["我"] you owe it; ["我", "<her name>"] you both agreed; '
        "[] just a wish, nobody owes anything. Other pronouns are refused: write the name."
    ))] = None,
    cue: Annotated[Optional[dict | str], _PydField(description=(
        'What this entry is waiting for: {"condition": "<the event, one sentence>"}. '
        "Replaces any cue it had; the condition is required. An empty string takes the "
        'cue off. e.g. cue={"condition": "he is back from the trip"}.'
    ))] = None,
    card_of: Annotated[Optional[str], _PydField(description=(
        "Make this mind entry the card of a name (a person in MIND/TRAITS, a thing in "
        "MIND/VIEWS; one card per name). An empty string takes it off as a card. "
        'e.g. card_of="Detroit".'
    ))] = None,
    room: Annotated[str, _PydField(description=(
        "Move the entry to another room. Which room it is in is metadata: it says what "
        "kind of thing this is, not what the entry says, so changing it leaves no version "
        "behind. Wrong or unknown rooms are refused here exactly as they are anywhere else."
    ))] = "",
    when: Annotated[str, _PydField(description=(
        "Where this entry hangs in time. An ordinary entry takes the day it happened "
        '("2026-07-06"); something wanted takes a date or a length ("3w"); a period takes its range '
        '("2026-07-31..2026-08-05"); a hold takes the day it ends or the days it covers. '
        'Wrong shapes are refused. Written entries carry the '
        "day they were written until you say otherwise, which is not always the day the "
        "thing happened."
    ))] = "",
    folds_append: Annotated[list, _PydField(description=(
        "Put a few more entries underneath a gist you already wrote. Append only: the "
        "line itself does not change, only which entries it now stands for. There is no "
        "way to hand in a replacement list, because forgetting one id would quietly let "
        "that entry surface again."
    ))] = [],
    sources_append: Annotated[Optional[list | dict | str], _PydField(description=(
        "Add pieces of the host's material this entry was formed from, as grow's sources "
        "takes them. Append only; a withdrawn or deleted source is refused."
    ))] = None,
    invalidation: Annotated[str, _PydField(description=(
        '"confirmed": an entry listed under 依据变了的 — you looked, what it stood on '
        "changed, and it still stands as written. It leaves that block and keeps a dated "
        "record of the check. Refused while a source it stands on is withdrawn or deleted."
    ))] = "",
    slice: Annotated[str, _PydField(description=(
        "A pending slice of the host's raw lines (the \"sl_…\" id from "
        'recall(view="slices")). With bucket_id: this entry already records it, and the '
        "slice is appended to its sources as one record. Without one: slice_span re-cuts it, "
        "drop_slice drops it."
    ))] = "",
    slice_span: Annotated[str, _PydField(description=(
        "With slice, no bucket_id: the slice's lines were cut wrong; its new span as "
        '"first_id..last_id" (one line: just that id), within the lines it was cut from. '
        "Its gist stays as it was."
    ))] = "",
    drop_slice: Annotated[bool, _PydField(description=(
        "With slice, no bucket_id: nothing in it is worth keeping. It is dropped and "
        "attached to nothing."
    ))] = False,
    weight: Annotated[float, _PydField(description=(
        "Something wanted only: how heavily it sits on you, 0~1."
    ))] = -1,
    dont_surface: Annotated[int, _PydField(description=(
        "1 stops it from coming up on its own. It stays searchable."
    ))] = -1,
    media_append: Annotated[Optional[list | str], _PydField(description=(
        "Attaches media (an image, say) to the entry."
    ))] = None,
    media_replace: Annotated[Optional[list | str], _PydField(description=(
        "Replaces the whole media list."
    ))] = None,
    hard_delete: Annotated[bool, _PydField(description=(
        "True deletes it for real. Only works on entries created with test_data, "
        "and delete_reason must be given."
    ))] = False,
    delete_reason: Annotated[Optional[str], _PydField(description=(
        "Goes with hard_delete."
    ))] = "",
    restore: Annotated[bool, _PydField(description=(
        "True brings it back, from the archive or from having sunk to a summary."
    ))] = False,
    old_str: Annotated[Optional[str], _PydField(description=(
        "The passage to replace. Must match word for word and appear only once."
    ))] = "",
    new_str: Annotated[Optional[str], _PydField(description=(
        "What to put there. Empty cuts the passage out."
    ))] = None,
) -> str:
    """Change an entry that is already stored: pin it, close it, archive it, or change one of
    its fields.

    Pass only what you are changing. Anything you leave out stays as it was.

    Pinning:
      pinned=1 pins the entry to the first screen of breath, the few lines you see before you
      say anything.
      🔴 What belongs here is "how I mean to act", not "this one matters". Nothing is
         refused, though: a sentence that reads as description still gets pinned, and a
         note comes back with it. The one thing worth catching is a flaw pinned as a
         principle — pin "I always rush" and it reads as "I intend to keep making this
         mistake" — and no check can tell that apart from a description worth keeping.
         That call is yours. Scarcity is held by the cap, not by the wording.
      pinned=0 unpins.

    Closing something you wanted:
      status="resolved"   done
      status="abandoned"  not doing it
      status="active"     back on the table
      🔴 Nothing closes itself, and a date passing does not close it either. There are only
         these two endings, and both are yours to call, once you have seen what actually
         happened. What was done is its own entry: grow a thetic event with from pointing
         at the wanted one, so "what I meant to do" and "what happened" both stay.
      Whether something is wanted at all is direction_of_fit, not status.
      A hold (grown with exception_of) is closed the same way: status="resolved" on the
      hold lifts it early, and the agreement it was hung on comes back. A dated hold
      lifts by itself once its last day has passed.

    Archiving and bringing back:
      delete=True   moves it to the archive and timestamps it. Nothing is really deleted;
                    looking it up by id always brings it back.
      restore=True  brings it back, whether it was archived or had sunk to a summary (the
                    original is read back out of storage, and it counts as genuinely
                    remembering it).

    Editing the text:
      old_str / new_str  replaces one passage, matched word for word and only if unique.
                         Leave new_str empty to cut the passage out.
                         🔴 This edits what is on disk and keeps no earlier version. If you
                            want the earlier version kept, use regrow instead.

    Changing fields:
      name / domain / tags / valence / arousal / weight / dont_surface / room / when /
      direction_of_fit / bound / cue / card_of
      Everything here is metadata: what kind of thing this is, where it hangs in time,
      how it felt. None of it is what the entry says, so none of it leaves a version
      behind — this is correction fluid, not a new draft. The moment the words themselves
      have to change, that is regrow.

    Putting more under a gist:
      folds_append=[ids]  the line stays as written; only what it stands for grows.

    When what an entry stood on changed (breath's 依据变了的 lists it):
      rewrite it          regrow; the new version carries no mark
      put it away         delete=True
      keep it as it is    invalidation="confirmed"

    Naming more of where it came from:
      sources_append=[{system, instance, container, id, …}]  more of the host's material
                         this entry was formed from; append only.

    A pending slice of the host's raw lines (recall(view="slices") lists them):
      bucket_id + slice="sl_…"        this entry already records it: the slice joins the
                                       entry's sources as one record
      slice="sl_…", slice_span="m_0012..m_0031"   it was cut wrong: move its span
      slice="sl_…", drop_slice=True   nothing in it to keep
      (Not recorded anywhere yet: grow(..., slice="sl_…").)

    Do not use this tool when:
    · The words themselves are wrong or have moved on. Use regrow, which keeps the old
      version instead of writing over it.
    · The entry has a newer version. Use regrow."""
    return await _with_notice(
        _t_trace.dispatch(
            bucket_id=bucket_id, name=name, domain=domain,
            valence=valence, arousal=arousal,
            tags=tags, pinned=pinned, room=room, when=when,
            folds_append=folds_append,
            delete=delete, status=status, weight=weight,
            dont_surface=dont_surface,
            media_append=media_append, media_replace=media_replace,
            hard_delete=hard_delete, delete_reason=delete_reason,
            restore=restore,
            old_str=old_str, new_str=new_str,
            direction_of_fit=direction_of_fit, bound=bound, cue=cue, card_of=card_of,
            sources_append=sources_append, invalidation=invalidation,
            slice_id=slice, slice_span=slice_span, drop_slice=drop_slice,
        ),
        op="trace",
        args={
            "bucket_id": bucket_id, "name": name, "domain": domain,
            "valence": valence, "arousal": arousal,
            "tags": tags, "pinned": pinned, "room": room, "when": when,
            "folds_append": folds_append,
            "delete": delete, "status": status,
            "direction_of_fit": direction_of_fit, "bound": bound, "cue": cue,
            "card_of": card_of,
            "sources_append": (len(sources_append) if isinstance(sources_append, list)
                               else bool(sources_append)),
            "slice": slice, "slice_span": slice_span, "drop_slice": drop_slice,
            "invalidation": invalidation,
            "hard_delete": hard_delete,
            "restore": restore,
            "delete_reason_len": len(str(delete_reason or "")),
            "old_str_len": len(str(old_str or "")),
            "new_str_len": len(str(new_str or "")) if new_str is not None else 0,
            "weight": weight, "dont_surface": dont_surface,
            "media_append_count": len(media_append or []),
            "media_replace_count": len(media_replace or []),
        },
    )


# Reject misspelled/unknown trace arguments instead of letting Pydantic's
# default extra=ignore silently degrade an intended edit into a bucket-id-only
# no-op.  This is especially important for old_str/new_str patch calls.
# This gate now doubles as the guard against **seven removed parameters**: `content`,
#    `importance`, `digested`, `meaning_append`, `meaning_replace`, `why_remembered` and
#    `resolved`.
#    Among them, `content` (replace the whole body) was the only entry point in the system
#    that **changed the original text without keeping the old version** — duplicating regrow
#    and more dangerous than it. `why_remembered` was the same thing as the retired
#    `meaning`: "why I remember it" and "why it matters" are one question. Anything worth
#    saying there should be written as a real realization instead.
#    WARNING: 141 older buckets on disk still carry the why_remembered field. **Not one row
#    of data was touched**; new ones simply are not written.
def adapt(mcp) -> None:
    """What server.py runs right after mounting trace (see the comment above)."""
    try:
        _trace_public_tool = mcp._tool_manager.get_tool("trace")
        if _trace_public_tool is None:
            raise RuntimeError("registered trace tool is missing")
        # All three steps (forbid · rebuild · re-publish the cached schema) live in
        # core/strict_args.harden — see that module for what happens when a caller copies
        # only the first two.
        _harden_tool(_trace_public_tool)
    except (AttributeError, RuntimeError, TypeError, ValueError) as _trace_schema_exc:
        logger.warning(
            "trace strict-argument adapter unavailable: %s",
            _trace_schema_exc,
        )
