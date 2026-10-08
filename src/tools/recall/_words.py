# -*- coding: utf-8 -*-
"""
tools/recall/_words.py — the plain-language shell, and which tags a reader sees

The structural phrasing of recall's readouts: room proportions and V/A become a sentence
a person can read, tags are used as the exact words they were stored as, and a piece of
thinking carries its 🧠. Also the tag filters every reader shares: system prefixes and
the machine-voiced `xx:yy` shape never reach a line, and the emotion seeds the ground
tone counts.

tools/recall/core.py re-exports the public names.
"""

import re
from collections import Counter

from core._rooms import is_mind_room


# System tag prefixes: never allowed onto any tag line a human reads (「疑似同件:xxx」
# included)
_SYS_TAG_PREFIXES = ("__", "aspect:", "疑似同件:", "相似认知:")
# Machine-voiced tags are **filtered out and never shown**, a lesson caught on
# muse's first screen:
# a prefix table can only stop the kinds already seen; the **shape** `xx:yy` is
# the real criterion.
# What faces a reader deserves plain-language scene words — structured tags like
# `aspect:patterns` are residue left behind when the old system was retired.
# The criterion is the same one as `is_scene_word()` in tools/_muse.py; do not
# write a second version of it here.
_MACHINE_TAG_RE = re.compile(r"^[^:：]{1,12}[:：]")
# A machine tag whose value is another entry's handle.
_HANDLE_TAG_RE = re.compile(r"^[^:：]{1,12}[:：]([0-9a-f]{6,12})$")


def is_human_tag(tag: str) -> bool:
    """Whether this tag deserves to be shown. System prefixes and the machine-voiced
    `xx:yy` shape do not."""
    t = str(tag)
    return bool(t) and not t.startswith(_SYS_TAG_PREFIXES) and not _MACHINE_TAG_RE.match(t)

_SEED_RE = re.compile(r"\[\[([a-z_]+)\]\]")
# The ground tone recognises emotion seeds only (the English keys of the seven
# emotions and six desires); any other [[wikilink]] (a project name, a person's
# name, ...) is not a seed
_SEED_NAMES = frozenset({
    "joy", "anger", "sorrow", "fear", "love", "aversion", "desire",
    "lust", "sound", "scent", "taste", "touch", "dharma", "greed",
})


# ============================================================
# Plain language is **the shell, not the core**
# ============================================================
# 🔴 **The body of a memory is never altered and never goes through a model.**
#    This section only handles **the structural phrasing** — machine readouts like
#    room proportions and V/A become a sentence a person can read, while tags are
#    **used as the exact words they were stored as**.
#    Every character is either **what was written down at storage time** (the tag
#    words) or **fixed text from these tables**.
#    A model rephrasing it differently every time makes it read less like "my
#    memory", not more.
#
# ⚠️ Only **how it is read out** changes, **not what is read**: the exact
#    percentages and V/A remain, to the number, in the `recall_data()` skin (the
#    dashboard needs them to draw distributions).
#    Numbers where numbers are wanted, plain language where an instant read is
#    wanted — two skins over one set of statistics.
#
# 📌 The reference table (these four lines are the acceptance sample):
#      I/EVENT/SELF/WHAT 37%   → 「多半是我自己在做事」
#      MIND/TRAITS 19%         → 「想得也不少」
#      青岛6 交接单5 火车4      → 「围着青岛、交接单转」(tag words used verbatim)
#      V0.62 / A0.50           → 「心里还行，不算绷着」
#    The wording is tunable: anything that reads wrong is fixed by editing these
#    four tables, without touching a line of logic.

_ROOM_PHRASES = {
    "EVENT/SELF":  ("多半是我自己在做事",   "几乎都是我自己在做事"),
    "EVENT/WORLD": ("多半是我听说看到的",   "几乎都是我听说看到的"),
    "MIND/TRAITS": ("多半在想我是个什么样的人", "几乎都在想我是个什么样的人"),
    "MIND/VIEWS":  ("多半在想我怎么看一件事",  "几乎都在想我怎么看一件事"),
}
# The subclause: added when the main clause is on the event side and thinking
# still takes up a sizeable minority (the second example above, 「想得也不少」)
_SUBCLAUSE_GATE = 0.15


def rooms_in_words(rooms: Counter, n: int) -> str:
    """Room proportions -> one plain sentence. **Only the four rooms count** (old
    names are normalised first).

    A proportion is a **result, not a quota**: this sentence says what I have been
    doing lately, and whatever comes out on top is reported as it is.
    """
    if not rooms or not n:
        return ""
    event_n = sum(c for r, c in rooms.items() if r.startswith("EVENT"))
    mind_n = sum(c for r, c in rooms.items() if r.startswith("MIND"))
    top_room, top_n = max(rooms.items(), key=lambda kv: kv[1])
    mostly, nearly_all = _ROOM_PHRASES.get(top_room, (f"多半在 {top_room}", f"几乎都在 {top_room}"))
    dominance = max(event_n, mind_n) / n
    if dominance >= 0.85:
        sentence = nearly_all
    elif dominance >= 0.6:
        sentence = mostly
    else:
        sentence = "做的和想的一半一半"
        return sentence
    # If the other half clears the subclause gate, add a clause — MIND/TRAITS at
    # 19% in the sample above is exactly this case
    if top_room.startswith("EVENT") and _SUBCLAUSE_GATE <= mind_n / n < 0.5:
        sentence += "，想得也不少"
    elif top_room.startswith("MIND") and _SUBCLAUSE_GATE <= event_n / n < 0.5:
        sentence += "，也记了些发生的事"
    return sentence


def mood_in_words(v, a) -> str:
    """V/A -> 「心里还行，不算绷着」. One clause per axis, joined by a comma."""
    if v is None:
        return ""
    if v >= 0.7:
        v_words = "心里挺好"
    elif v >= 0.55:
        v_words = "心里还行"
    elif v >= 0.45:
        v_words = "心里平平"
    elif v >= 0.3:
        v_words = "心里有点沉"
    else:
        v_words = "心里不好受"
    if a is None:
        return v_words
    if a >= 0.7:
        a_words = "绷得紧"
    elif a >= 0.55:
        a_words = "有点绷着"
    elif a >= 0.35:
        a_words = "不算绷着"
    else:
        a_words = "松着"
    return f"{v_words}，{a_words}"


def tags_in_words(tags: list, k: int = 2, with_counts: bool = False, framed: bool = True) -> str:
    """Tags -> 「围着青岛、交接单转」. **Not one character of a tag is changed**;
    only the frame around them is a template.

    Use with_counts=True where the numbers matter: `床 3` and `床 30` describe two
    completely different stretches of life, and without counts those two lines
    look identical. Use framed=False where **the row label already says it in
    plain language** (the card line `围着什么   代码 3 · 交接单 3`) — saying the
    same thing twice only makes it harder to read.
    """
    items = [(t, n) for t, n in (tags or []) if is_human_tag(t)][:max(1, k)]
    if not items:
        return ""
    parts = [f"{t} {n}" if with_counts else str(t) for t, n in items]
    if not framed:
        return " · ".join(parts)
    return "围着" + "、".join(parts) + "转"


def kind_badge(meta: dict) -> str:
    """The badge in front of an item line: **a mind wears 🧠, an event wears
    nothing.**

    🔴 **The room code is gone**: a code like `EVENT/SELF` is written for a
    machine, and three of them in one line blur the single fact that has to stay
    clear — "what happened" versus "what I thought".
    The real ailment was mixed listing with no marker of identity, leaving the
    reader to guess from the content — in the data, room has always been perfectly
    distinct; **it was the display layer that failed to separate them**.
    ⚠️ Do not solve this by splitting into two blocks (an earlier draft put events
       as the main body with thinking in a separate little section; it was
       dropped): keep them interleaved, since telling them apart at a glance is
       enough.
    """
    return "🧠" if is_mind_room((meta or {}).get("room")) else ""
