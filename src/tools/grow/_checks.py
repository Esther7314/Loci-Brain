"""
========================================
tools/grow/_checks.py — the arguments a grow write checks before anything is stored
========================================

Each check takes what the caller passed and returns what create() stores, or a refusal
to hand back word for word; none of them writes anything. grow_event and grow_mind run
them first, so an invalid item refuses the whole call before a single bucket exists.
trace and the grow entry point reach some of them too (clock_when, check_cue,
_retired_fields_msg), through tools/grow/rooms_path.

Exports: clock_when(when) · check_cue(cue)
========================================
"""

import re as _re
from datetime import datetime, timedelta

from core import _holds as _H
from .. import _runtime as rt
from .._common import resolve_bucket_id
from .._subjects import normalize_bound
from utils import parse_bool

# ------------------------------------------------------------
# when
# ------------------------------------------------------------
_WHEN_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}([ T].*)?$")
# A want's duration marker, which carries a magnitude — `<N><unit>`, where
# d=day w=week m=month (~30 days) y=year (~365 days). No prefix symbol: a symbol
# that carries no meaning does not stay.
# Recognised only for something wanted (telic) — an ordinary event's when is "the day this
# happened", where a duration marker means nothing, so that path still accepts
# absolute dates only.
_WANT_DURATION_RE = _re.compile(r"^\d+[dwmy]$")
# A date with a clock time: `2026-10-03 20:00`, `2026-10-03T20:00`, with or without an offset.
_CLOCK_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}")


def clock_when(when: str) -> tuple[str, bool]:
    """A `when` carrying a clock time -> (the form stored, whether it reads as one). The
    model writes the hour as it lives it; a stored stamp without an offset is read as UTC
    (core/_when.py), so the local offset is written out — `2026-10-03 20:00` is stored as
    `2026-10-03T20:00+08:00`, the same form the backfill writes (core/_dates.stamp). A
    stamp that states its own offset is kept as given. Anything else -> (when, False)."""
    if not _CLOCK_RE.match(when or ""):
        return when, False
    try:
        moment = datetime.fromisoformat(when.replace("Z", "+00:00"))
    except ValueError:
        return when, False
    if moment.tzinfo is not None:
        return when, True
    from core import _when as _w
    precise = moment.second or moment.microsecond
    return (moment.replace(tzinfo=_w.LOCAL_TZ)
            .isoformat(timespec="seconds" if precise else "minutes"), True)


def _in_the_future(when: str) -> bool:
    """Is `when` after now? A bare date is its local midnight; ten minutes of slack for
    a clock that runs a little ahead."""
    from core import _when as _w
    t = _w.parse_stamp(when)
    return bool(t) and t > _w.now() + timedelta(minutes=10)


# ------------------------------------------------------------
# The gate in front of retired fields
# ------------------------------------------------------------
# Two sentences are the verdict on these two fields:
#   importance —— `decay_engine.py` states in black and white that importance
#     takes no part, and its only consumer's own comment says it no longer
#     decides whether any bucket lives or dies. Keeping a score I have to assign
#     every single time and nobody reads only adds weight to writing.
#   meaning    —— "why it matters" is, put plainly, the thinking this thing
#     provoked; and what matters in the end is that thinking. Keeping the field
#     means **leaving a back door for an event to write a mind on the sly**, and
#     a sentence written into meaning has no source chain, cannot be regrown, and
#     cannot be pulled together while musing — it is a dead field.
#     Evidence: what was sitting in one bucket's meaning was, by itself, a
#     complete piece of thinking.
# 🔴 To say "why it matters", write a proper grow(kind="mind") entry, so that it
#    has a provenance and can be re-versioned.
_RETIRED_MSG = {
    "importance": (
        'importance 已退役（2026-08-16）——它不参与遗忘公式，'
        '「不再决定任何桶的生死」是它自己注释里的原话。别再打这个分。'),
    "meaning": (
        'meaning 已退役（2026-08-16）——想说「为什么重要」就写成一条真的认知：'
        'grow(kind="mind", room="MIND/TRAITS", text="…", from=["这条事件的id"], v=, a=)。'
        '写在 meaning 里的话没有来源链、不能 regrow、不能被发呆整合。'),
    "digested": (
        'digested 已退役（2026-08-16）——我们没有「消化」这个动作，'
        '所有的事件、认知都长新的。'),
}


def _retired_fields_msg(*names: str) -> str:
    """Assemble one rejection message for a set of retired fields (shaped like
    _rooms.py's rejections: say what is wrong and give a way out)."""
    hits = [n for n in names if n in _RETIRED_MSG]
    return "\n".join(_RETIRED_MSG[n] for n in hits)


def _retired_item_fields(item: dict) -> str:
    """If items[i] carries a retired field, return the rejection message;
    otherwise return an empty string."""
    hit = [n for n in ("importance", "meaning", "digested")
           if item.get(n) not in (None, "")]
    return _retired_fields_msg(*hit)


# ------------------------------------------------------------
# The v2 write-layer arguments, cue and hold
# ------------------------------------------------------------

def _check_v2(kind: str, direction_of_fit, bound, evidential,
              internally_generated, weight) -> tuple[dict, str]:
    """The v2 write-layer arguments -> the create() keywords, or an error to hand back.

    Written in the same create() as the body: a follow-up update() that fails would
    leave a body without its orientation."""
    d = str(direction_of_fit or "").strip().lower()
    if d not in ("", "thetic", "telic"):
        return {}, ('direction_of_fit 只有两个值："telic"（想让它发生：答应的、计划的、想要的），'
                    '或者不填（记下已经是这样的）。')
    telic = d == "telic"
    names, err = normalize_bound(bound)
    if err:
        return {}, err
    if names and not telic:
        return {}, 'bound 只跟 direction_of_fit="telic" 一起用：只有想要的事才有谁被绑着。'
    ev = str(evidential or "").strip().lower()
    if ev and kind != "mind":
        return {}, ('evidential 只给 mind。事件是亲历还是听说，写在房间里：'
                    'EVENT/SELF 亲历，EVENT/WORLD 听说、看到。')
    if ev not in ("", "inference", "assumption"):
        return {}, 'evidential 只有两个值："inference"（有看得见的迹象）/ "assumption"（凭推理、常识）。'
    ig = parse_bool(internally_generated, default=False)
    if ig and kind != "event":
        return {}, ("internally_generated 只给事件：梦和想象是一段经历，存成 EVENT/SELF；"
                    "梦醒后想明白的才是 mind，它不带这个记号。")
    if weight is not None and not telic:
        return {}, 'weight 只跟 direction_of_fit="telic" 一起用：压在心上多重，只有想要的事才有。'
    fields = {"direction_of_fit": d if telic else "", "bound": names,
              "evidential": ev, "internally_generated": ig}
    if weight is not None:
        fields["weight"] = max(0.0, min(1.0, float(weight)))
    return fields, ""


_CUE_KEYS = ("condition", "phrasings")
_CUE_CONDITION_MAX = 200


def check_cue(cue) -> tuple[dict | None, str]:
    """The `cue` argument -> {condition, phrasings} to store, None when none was given,
    or a refusal. Writing a cue is the declaration that the entry waits on something,
    so the condition (what it waits on) is what cannot be missing. phrasings belong to
    the backfill and are taken as given. trace reads cues through here too."""
    if cue is None or cue == "":
        return None, ""
    if isinstance(cue, str):
        cue = {"condition": cue}
    if not isinstance(cue, dict):
        return None, 'cue 写成 {"condition": "等的那件事，一句话"}。'
    extra = [k for k in cue if k not in _CUE_KEYS]
    if extra:
        return None, (f"cue 不认识 {', '.join(map(str, extra))}：只有 condition（等的那件事）"
                      "和 phrasings（它可能被怎么说出来，可以不填）。")
    condition = str(cue.get("condition") or "").strip()
    if not condition:
        return None, ('cue 要有 condition：等的是哪件事，一句话（例如 "考完试"）。'
                      "写了 cue 就是在等，等什么得说出来；不是在等就别写 cue。")
    if len(condition) > _CUE_CONDITION_MAX:
        return None, f"cue 的 condition 是一句话，最多 {_CUE_CONDITION_MAX} 字（收到 {len(condition)}）。"
    phrasings = cue.get("phrasings") or []
    if isinstance(phrasings, str):
        phrasings = [phrasings]
    if not isinstance(phrasings, list) or not all(isinstance(p, str) for p in phrasings):
        return None, "cue 的 phrasings 是几句话的列表。"
    return {"condition": condition, "phrasings": [p.strip() for p in phrasings if p.strip()]}, ""


async def _check_hold(exception_of, hold) -> tuple[str, str, str]:
    """The hold arguments -> (target id, level, refusal). Both empty is no hold. The
    target goes through the same short-id resolver as every write tool, and has to be
    a live, current entry that is not itself a hold."""
    target = str(exception_of or "").strip()
    level = str(hold or "").strip().lower()
    if not target and not level:
        return "", "", ""
    if not target:
        return "", "", "有 hold 就要有 exception_of：这张条子挂在哪条约定上，填它的 id。"
    if not level:
        return "", "", ('挂条子要说是哪种：hold="defer"（先别催 / 先别提，事还算数），'
                        'hold="avoid"（别碰，连想都别想）。')
    if level not in _H.HOLD_LEVELS:
        return "", "", (f'hold 只有两个值："defer"（先别催 / 先别提）/ "avoid"（别碰）'
                        f"，收到 {hold}。")
    target, id_err = await resolve_bucket_id(target)
    if id_err:
        return "", "", f"exception_of —— {id_err}"
    found = await rt.bucket_mgr.get_including_archive(target)
    if not found:
        return "", "", f"exception_of 指的 {target} 不存在。填那条约定的真 id。"
    meta = found.get("metadata") or {}
    if meta.get("deleted_at") or not rt.bucket_mgr.is_live(target):
        return "", "", (f"{target} 在归档区，条子挂不上去。"
                        f'先 trace(bucket_id="{target}", restore=True) 捞回来。')
    newer = str(meta.get("superseded_by") or "").strip()
    if newer:
        return "", "", f"{target} 已经有新版 {newer}——条子挂到新版上。"
    if _H.is_hold(meta):
        return "", "", (f"{target} 本身是一张条子。条子挂在约定上："
                        f"exception_of 填它挂着的 {meta.get('exception_of')}。")
    return target, level, ""


def _hold_receipt(bid: str, target: str, level: str, when: str, review_after: str) -> str:
    """One line saying how this hold will end, so the model hears it once at writing."""
    head = f"📎 条子 {bid} 挂在 {target} 上（{level}）"
    close = f'trace(bucket_id="{bid}", status="resolved")'
    if when:
        return f"{head}：{when} 过完自己放下；提前放下就 {close}。"
    if review_after:
        return f"{head}：没写日子，不会自己放下，{review_after} 是回头再看它的日子；放下就 {close}。"
    return f"{head}：不会自己放下，要放下就 {close}。"
