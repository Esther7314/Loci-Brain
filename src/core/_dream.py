# -*- coding: utf-8 -*-
"""
========================================
tools/_dream.py — the dream-weaving engine
========================================

------------------------------------------------------------
📌 In one line
------------------------------------------------------------
> **A dream is not a replay of memory. It uses memory as a handle, grabs things out of
> everything one happens to know, and generates something by deliberately mis-joining
> them. It has to be strange, and it has to be forgettable: forgetting is the default,
> remembering is the exception.**

------------------------------------------------------------
🔴 This file is the one and only legitimate "the LLM does the writing" point in the
    whole system — and why that does not break the constitution
------------------------------------------------------------
The constitution says: the system only retrieves and arranges; the writing is always
mine. A dream is the **sole exception**, and the reason for the exception is not
convenience, it is **meaning**:

> A summary is "what I choose to remember", and that has to be me. **A dream is not
> written by me; a dream is something that happens to me.**
> If I weave a dream inside my own conversation, then I have **lucidly made a dream up**,
> which is not dreaming.

So weaving has to be **an independent model call**, outside my own context — and
**musing and the gist sentence may never** take this road (`_muse.py` states in as many
words that no LLM path exists there and none may ever be added).

------------------------------------------------------------
One rule, three instances: the four things unique to the dream instance
------------------------------------------------------------
| | the dream instance |
|---|---|
| **which pool it eats** | wants that weigh on me (weighted by `weight`) + events never worked out (weighted by `arousal`) + a few tags |
| **who writes** | 🔴 the LLM (the only one of the three instances that does) |
| **consequence of weaving** | **note `last_dreamt` and nothing else**, so it is picked less often for a while. **Neither weight nor arousal is touched.** |
| **the fate of the product** | it will be forgotten. **Forgetting is the default, remembering is the exception** (only a `grow` makes it stay) |

"Never worked out" = **no insight points at it**. No new field for this: scan the source
chains of every mind, and any event that was never cited is one that was lived through
but never digested.
The rule is implemented in `_muse.POOL_SPECS["dream"]` + `bucket_mgr.mind_from_ids()` and
**is not reimplemented here**: 🔴 ingredient selection goes through one engine; never
keep two copies (`night_fall/selection.py` retired along with night_fall).

------------------------------------------------------------
The four ingredient streams · three points, none of which may be skipped
------------------------------------------------------------
1. **Weighted random — neither pure random nor sorted.**
   Pure random gives the heavy and the trivial equal odds; sorting makes the single
   heaviest thing get dreamt about every night.
   **Weighted = heavier is likelier, but never guaranteed.**
2. **Multiply by a time decay**: `weight ÷ (1 + days/7)`.
   Without it, something from six weeks ago is exactly as likely as yesterday (measured:
   398 of 853 entries in the pool counted as "never worked out").
   **Real dreams are mostly day residue.**
3. 🔴 **Feed the full text, not the summary** (body truncated to ~800 characters).
   Everything good in a dream rests on concrete detail ("the nail mark left at five past
   eight"). **A summary is too dry; feeding summaries produces very hollow dreams.**

Why "a few words" and not "a place": a list of rooms from the Home system was rejected —
that space is not actually lived in yet, so pushing it in would be an external injection.
The words come from `tags` instead, and in practice **abstract words work best**: given
the word "boundary" once, the model grew an entire dream out of it (doors / walls /
listening through them / "I did not go in").
**Its job is to hand over an unrelated handle and force a mis-join. It is not explained
and not constrained.**

------------------------------------------------------------
The trigger: weave only above the line — **a night with no dream is normal**
------------------------------------------------------------
📌 **One genuinely heavy thing is enough for one night's dream; ten bland ones are not,
   however long they pile up.**
→ Implemented as two numbers: the **dull line** (an entry weighing less than this counts
   for nothing at all) and the **pressure line** (weave only above it).
   The dull line is where the "ten bland ones do not weave" half lands — without it, 400
   old entries summed together would clear any line forever.
⚠️ Whatever never reaches the line needs no handling: **not reaching the line is what
   "not that important" means, and the forgetting curve will wear it down.**
📌 "A dream must have consequences" needs no separate mechanism — the consequence is
   simply that **it stops pressing**: pressure builds to a point, a dream happens,
   pressure drops, it builds again. **It is a loop in its own right.**

------------------------------------------------------------
Lifecycle: a dream is forgotten, and genuinely so
------------------------------------------------------------
⚠️ 🔴 **This was amended deliberately** and supersedes the older design quoted below.
   The old text of this cell read: "the whole version exists only in the return value of
   the weave that produced it; it is never persisted."
   It now reads: **the whole version is persisted and stays alive; its only death is the
   degrade signal.** To whoever reads this next: that is not a bug, it is the rule.

    at night   weave (whole + fragment) -> **the whole version is persisted** (same file
               as the fragment, under a new layer `完整`).
               For as long as a genuine night lasts — 3 to 4 hours with no message from
               the user — the poke endpoint (`/api/loci/poke`) can hand the whole version
               into the window; while nobody comes back, the whole version simply stays.
               **The whole layer does not decay with time.** Its only death is the
               degrade signal: `POST /api/loci/dream/wake` -> `degrade_on_wake()`, fired
               by the user's next message after the dream has been handed into the
               window. That is the caller's job; the caller is lento-v2
               `src/chat/梦桥.js`.
    after      the whole version is deleted from disk, and the fragment layer starts
    degrading counting from **the moment of degradation**, not from the moment of weaving:
               ~30 min / 15 turns   the **fragment** is still available
               ~1 hour / 30 turns   **one sentence is left**
               after that           **the file is deleted, and a trace is left** (one
                                    event; ⛔ it does not go into what weighs on me)

Three rules, none of which may be turned into something event-driven:
1. **What drives it is time, not "was it seen".** The upstream design deleted after four
   unclaimed attempts; **ours is time-driven: ignore it and it goes away by itself.**
2. **Recalling delays it but cannot stop it.** Each recall pushes the start point back a
   little, **and each push is smaller than the last** (`recall_delay_minutes × 0.5^n`,
   a geometric sum with an upper bound = it can never be pushed to forever).
3. 🔴 **The only thing that truly keeps it: writing it down** (`grow` it as an event).
   **The instant it is written down it is no longer a dream, it is a memory**, and from
   then on memory owns it. Anything not written down is genuinely gone.

⏳ **The turn-count layer is the bridge's job** (the spec explicitly allows "if you
   cannot do it, ship the time layer alone"):
   "for me, the passage of time is really how many times I have been called" — **losing
   attention is the real reason a dream disappears, not the clock.** But only the host
   (the gateway) can count a turn; Loci cannot. Passing off "number of tool calls" as
   turns inside Loci would be manufacturing the number. So: **the rule is implemented
   here** (the `轮次` field takes part in layer selection, thresholds live in config) and
   **the hand that feeds the number is left to the bridge.**

------------------------------------------------------------
Nightmares: one field, nothing more
------------------------------------------------------------
Very low `v` + very high `a` -> `nightmare: true`. **The leg that makes noise is not
built** (a hard boundary in the spec): no push notification, no buzzing anyone's phone;
saying something in chat in the middle of the night is the bridge's job.
⏳ **The thresholds are still open** (v<0.3 and a>0.7 as a starting point; settle them
after looking at data from real dreams).

------------------------------------------------------------
🔴 Every factory value is a placeholder, **to be settled against real dream data**
------------------------------------------------------------
The `dream:` section of config.yaml is the single source of truth; the copy in code is
only the fallback.

Exports
------------------------------------------------------------
DREAM_PROMPT · DREAM_DEFAULTS · dream_config()
weave() (weave one; returns the whole version, and **persists it too** — see the
         amendment above)
current_dream() (fetch the current layer, counting as one recall)
withheld_ingredients() (the ingredients of a dream that may no longer be seen; any one
         withholds the whole dream from being handed out)
degrade_on_wake() (🔴 the degrade signal: drop a living `完整` layer down to the fragment
         layer; idempotent)
maintain() (the hook at awakening: sweep the expired ones and weave if above the line —
         both silently)
gather_ingredients() · pressure() · parse_dream() · layer_of() · load_dreams() · dreams_dir() · first_sentence()
========================================
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import _holds as _H
from . import _muse as M
from . import visibility as _V    # the one gate: what may be put in front of the model
from tools import _runtime as rt
from . import _when as _w

# ============================================================
# prompt — 🔴 copied verbatim from the settled version, punctuation and layout included
# ============================================================
# Two lessons from tuning this prompt, both paid for with real dreams:
#   ① **Examples constrain it.** The first version offered "a face appearing on someone
#      else's body" as an example, and the model dutifully worked in that mould. With
#      every example stripped out, it immediately grew things like "network cable coiling
#      like intestines", "the update icon spinning and laughing", "a mailbox that starts
#      talking", "the chipped glaze on a porcelain tiger's ear" — **none of which appear
#      anywhere in the ingredients.**
#      -> **Keep the definitions, give no examples.**
#   ② **Never say the words "training data".** Saying it out loud breaks the premise that
#      you do not know who you are and do not know there are rules. The current phrasing
#      ("not what you lived through, what you know") gets through to it just fine.
# ⚠️ Before editing this block: it is the third version of three that were tried, not a
# form of words somebody dashed off.
DREAM_PROMPT = """你不是叙述者。你是梦的发生过程。

你不知道自己是谁，不知道自己在哪里。你正在经历一些事。
这些事不需要连贯。画面来了就来了，断了就断了。
你不知道什么是素材，不知道有人在看，不知道有规则。

────────── 手上有什么 ──────────

压在心头的    还没了结的事，各带一个分量
想不明白的    发生过、有情绪、但一直没想通的
几个词        没有来由的，就是几个词
底色          每件事各自的：好受不好受、平静还是激动

还有一样不在上面这些里面：
不是你经历的，是你知道的 —— 你读过、听过、见过而没有经历过的一切。
它不需要来源，不需要跟上面任何一条有关，不需要合理。
一个只重放真实发生过的事的梦，是失败的梦。

────────── 手法 ──────────

凝缩    两个不相干的东西压成一个
移置    要紧的伪装成不要紧的
错接    场景和人可以来自不同的地方，中间不需要过渡

────────── 不许 ──────────

× 总结情绪      × 解释意义
× 完整的故事    × 文学性的收尾

────────── 必须 ──────────

√ 第一人称，现在时
√ 具体到不合理的感官细节
√ 突然结束，不收尾，允许逻辑断裂

────────── 输出两层 ──────────

完整：整个梦。300~600 字。
碎片：不是摘要，是残留 —— 醒来一段时间之后还剩下的那些。
      几个意象、一两句断掉的话，没有主语，没有前因后果。60~120 字。

只返回 JSON：{"完整": "…", "碎片": "…", "v": 0.0, "a": 0.0}
v = 好受不好受（0 难受 ~ 1 好受），a = 平静还是激动（0 平静 ~ 1 激动）"""


# ============================================================
# Thresholds — the factory values are placeholders, **to be settled against real dream data**
# ============================================================
DREAM_DEFAULTS: dict = {
    # ---- the four ingredient streams ----
    "want_n": 2,             # how many of the pressing wants to draw
    "unclear_n": 2,          # how many of the never-worked-out events to draw
    "word_n": 2,             # how many loose words to draw
    "excerpt_chars": 800,    # 🔴 the full text is fed, truncated to this length (not the summary)
    "half_life_days": 7,     # time decay: weight ÷ (1 + days/N)
    "word_pool_top": 120,    # the loose words are drawn from the N most frequent scene words
    # ---- Trigger: ⏳ **these two numbers are still open**; the factory values were read
    #      off a dry run against a real store, not guessed ----
    #   What `scripts/dream_dryrun.py` reported on a real store:
    #     · pools: 3 pressing wants · 368 never worked out
    #     · dull line 0.35 -> 36 entries above it (0.2 -> 91, 0.5 -> 8)
    #     · **replaying 30 days, "the heaviest single entry" ran 0.60 ~ 0.90**
    #   -> so: **0.65 ≈ dreams on most days, none on quiet ones** (one day sat right on
    #     0.65; another came in at 0.60, below the line = a night with no dream);
    #     0.8 ≈ only on genuinely striking days; >0.9 ≈ almost never.
    #   ⚠️ Do not treat this as a "tune it once and forget it" number: **a live store
    #     gains new entries at a≈0.6 every day**, so what this line really governs is
    #     "how quiet a day has to be before the night stays quiet too".
    "dull_line": 0.35,       # dull line: an entry weighing less than this **counts for nothing**
    "pressure_line": 0.65,   # pressure line: weave only if **the heaviest single entry** clears it (not the sum — see pressure())
    # How long after being dreamt about an entry is picked less often.
    # **This affects which one is dreamt, never whether to dream at all.**
    "dream_cooldown_days": 7,
    "per_day": 1,            # at most this many dreams a day (one dream a night)
    # ---- Lifecycle: wall-clock time and conversation turns, **whichever arrives first** ----
    "fragment_minutes": 30,  # how long the fragment layer lives
    "fragment_turns": 15,
    "oneline_minutes": 60,   # how long the one-sentence layer lives (deleted afterwards)
    "oneline_turns": 30,
    "recall_delay_minutes": 10,   # how far one recall pushes the start point (halved each time: it can never reach forever)
    # ---- Nightmares (⏳ thresholds still open: settle them after some real dreams) ----
    "nightmare_v": 0.3,      # v below this
    "nightmare_a": 0.7,      # and a above this
    # ---- Model settings (measured, do not fiddle) ----
    "temperature": 1.0,
    "max_tokens": 8192,      # 🔴 this model counts reasoning_tokens against
                             #    completion_tokens, so a budget of 4096 gets eaten by
                             #    thinking and `content` comes back an empty string
                             #    (walked into twice)
}

STATE_FILE = "dream_state.json"     # where "did we weave today" is recorded (under _state/)
FILE_PREFIX = "梦_"                  # our own dream files; the `.md` files night_fall left behind are never touched


def dream_config(cfg: dict | None = None) -> dict:
    """Factory values plus the `dream:` section of config.yaml. Config is the single
    source of truth; the copy in code is only the fallback."""
    out = dict(DREAM_DEFAULTS)
    src = (cfg or {}).get("dream") if isinstance(cfg, dict) else None
    if isinstance(src, dict):
        for k, v in src.items():
            if k in out and v is not None:
                out[k] = type(out[k])(v)
    return out


def _c(cfg: dict | None = None) -> dict:
    return dream_config(cfg if cfg is not None else rt.config)


def _f(x, d: float) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


# ============================================================
# The four ingredient streams
# ============================================================
@dataclass
class Ingredient:
    """What one ingredient looks like to the engine. **The body is the full text**, not
    the summary."""
    id: str
    text: str
    v: float
    a: float
    weight: float                 # pressing wants take `weight`; never-worked-out events take `arousal`
    days: int
    route: str                     # "压在心头" (weighing on me) | "想不明白" (never worked out)
    days_since_dreamt: int | None = None   # days since it was last dreamt about; None = never


def time_decay(weight: float, days: int, c: dict) -> float:
    """`weight ÷ (1 + days/7)` — **real dreams are mostly day residue.**"""
    half_life = max(1.0, float(c["half_life_days"]))
    return float(weight) / (1.0 + max(0, int(days)) / half_life)


def cooldown_factor(days_since_dreamt: int | None, c: dict) -> float:
    """Something dreamt about recently is **picked a little less**, but never excluded.
    Returns a factor between 0.15 and 1.0.

    🔴 This replaced an earlier design in which weaving zeroed the `weight` of every want
       it used:
       > Wanting to dream about something **does not mean the thing has been let go of.**

       That design **fused two different things into one**:
         · "do not keep having the same dream"   <- a question about picking ingredients
         · "this no longer weighs on me"         <- not the system's call to make
       Zeroing did both at once, and the consequence ran backwards: **the thing you are
       least able to let go of vanished from "weighing on me" the first time it was
       dreamt about** — when in real life it is exactly the unresolved thing that recurs
       in dreams.
       It also crossed another line: a `want` has exactly two endings (resolved /
       abandoned) and **both have to be marked by hand.**

    📌 So the two are separate now: this function only handles "do not repeat yourself",
       and **does not touch `weight` at all.**
    ⚠️ Nor does it enter the pressure calculation — whether to dream still looks at the
       real weights, and only which one to dream about takes this discount. Having been
       dreamt about recently is not the same as no longer pressing.
    """
    if days_since_dreamt is None:
        return 1.0
    cooldown = max(1.0, float(c.get("dream_cooldown_days", 7)))
    return max(0.15, min(1.0, float(max(0, int(days_since_dreamt))) / cooldown))


# The weight of the "never worked out" stream is **`arousal` as it stands** (both the
# spec and the prototype used exactly that).
# ⚠️ It was once changed to "whichever of the two axes is further from the midpoint", in
#    order to catch the "very painful but very calm" case — and **rolled back**:
#    ① the spec says arousal, and ② such entries **are still in the pool anyway** (the
#    gate into the pool is `_muse`'s `emotion_line`, and either axis clearing it counts as
#    having emotion); they are merely less likely to be drawn — **which is precisely what
#    "weighted by arousal" means**, not an oversight. Changing the rule is a decision to
#    be made deliberately, not something to slip in while passing.


def _age_days(it: "M.Item", now: datetime) -> int:
    ts = it.ts or it.created
    if ts is None:
        return 999
    return max(0, (now - ts).days)


def _days_since_dreamt(meta: dict, now: datetime) -> int | None:
    """How many days since this was last dreamt about. Never dreamt about -> None.
    Unreadable also counts as never — no guessing."""
    raw = str(meta.get("last_dreamt") or "").strip()
    if not raw:
        return None
    t = _w.parse_stamp(raw)
    return max(0, (now - t).days) if t else None


def want_pool(recs: list[tuple[dict, str]], now: datetime) -> list[Ingredient]:
    """What weighs on me: **wants that have not been closed**, weighted by `weight`.

    Same definition as "weighing on me" in `core/profile.py` (telic, not closed, not
    deliberately forgotten, not superseded, not a hold). ⚠️ With two **deliberate**
    differences: awaken moves anything with a date inside 30 days over to the ⏰ reminders
    column, but that is a **display split**. As far as a dream is concerned they are all
    still unfinished business, so all of them count here. And only an `avoid` hold keeps
    a want out of dreams: a `defer` asked not to be pushed, not to be forgotten.
    Everything but "telic, not closed" is the gate's `dream` road (`core/visibility.py`).
    """
    from utils import is_closed, is_telic
    holds = _H.hold_index(recs)
    out: list[Ingredient] = []
    for meta, text in recs:
        if not is_telic(meta) or is_closed(meta):
            continue
        if not _V.visible_for(meta, road=_V.DREAM, now=now, holds=holds):
            continue
        if M._is_utility_record(meta):
            continue
        it = M.item_of(meta, text)
        if it is None or not it.text.strip():
            continue
        out.append(Ingredient(id=it.id, text=it.text, v=it.v, a=it.a,
                              weight=_f(meta.get("weight"), 0.5), days=_age_days(it, now),
                              route="压在心头",
                              days_since_dreamt=_days_since_dreamt(meta, now)))
    return out


def unclear_pool(recs, digested: set[str], c: dict, now: datetime) -> list[Ingredient]:
    """Never worked out: events that **carry emotion and that no insight points at**.

    🔴 The pool comes straight from `_muse.pool_of(..., "dream", ...)` — ingredient
    selection goes through one engine; never keep two copies. What the gate's `dream`
    road keeps out is taken out here — what an `avoid` hold is hung on, holds themselves,
    a deliberate `dont_surface`, an old version. Muse's own pool counts neither holds nor
    `dont_surface`, so muse still sees those.
    """
    holds = _H.hold_index(recs)
    skip = {str(m.get("id") or "").strip() for m, _t in recs
            if not _V.visible_for(m, road=_V.DREAM, now=now, holds=holds)}
    out: list[Ingredient] = []
    for it in M.pool_of(recs, "dream", M.muse_config(rt.config), now, digested):
        if it.id in skip:
            continue
        out.append(Ingredient(id=it.id, text=it.text, v=it.v, a=it.a,
                              weight=it.a, days=_age_days(it, now), route="想不明白"))
    return out


def few_words(recs, c: dict) -> list[str]:
    """Drawn at random from the `tags` of every bucket — **with no requirement that they
    be places.**

    `tags` were deliberately redefined from "what topic is this about" to "what is inside
    it", which makes these words things **we wrote down at the moment of storing, and
    that are guaranteed to appear literally in the body.** The `_muse.is_scene_word()`
    gate then filters out machine-voiced labels (`aspect:patterns` and the like) — a tag
    the machine assigned itself is not a trace of ours.
    """
    freq = Counter()
    for meta, _t in recs:
        freq.update(str(t) for t in (meta.get("tags") or []))
    pool = [w for w, _n in freq.most_common(int(c["word_pool_top"]))
            if M.is_scene_word(w) and 1 < len(w) <= 6]
    n = min(int(c["word_n"]), len(pool))
    return random.sample(pool, n) if n > 0 else []


def weighted_sample(pool: list[Ingredient], n: int, c: dict) -> list[Ingredient]:
    """**Weighted random** on "weight × freshness", without replacement.

    🔴 Weighted random is not sorting: heavier is likelier, **but never guaranteed** —
    sorting would make the single heaviest thing get dreamt about every night.
    """
    pool = list(pool)
    out: list[Ingredient] = []
    for _ in range(min(int(n), len(pool))):
        w = [max(0.01, time_decay(x.weight, x.days, c) * cooldown_factor(x.days_since_dreamt, c))
             for x in pool]
        i = random.choices(range(len(pool)), weights=w)[0]
        out.append(pool.pop(i))
    return out


def pressure(pool: list[Ingredient], c: dict) -> tuple[float, float, list[tuple[str, float]]]:
    """How much has piled up. Returns (pressure, total pile, [(id, weight above the line)]
    sorted by weight descending).

    🔴 **Pressure = the weight of the single heaviest entry, not the sum.** The rule is
    exactly this:
    > **One genuinely heavy thing is enough for one night's dream; ten bland ones are
    > not, however long they pile up.**
    The second half of that rules out summing outright — ten bland entries **summed**
    will clear any fixed line.
    ⚠️ The first implementation really did sum them, and one run against a real store
       gave it away: **pressure 19.23** against a pool of 371 entries. With a line at 0.9
       that means **a dream every single night**, and the acceptance criterion "below the
       line = a night with no dream" could never have been tested. Taking the max instead
       puts the same store in the 0.6~1.0 range — only then does a line mean anything.

    The two numbers each do one job:
      **the dull line `dull_line`** — an entry whose weight (time decay already applied)
        falls below it **counts for nothing at all**. It also solves "old things never
        fade out" for free: the several hundred never-worked-out entries from six weeks
        ago decay below the line and drop out automatically, with no extra rule.
      **the pressure line `pressure_line`** — weave only if the heaviest entry clears it.

    The total pile takes no part in the decision; it is **recorded in the state file to be
    looked at**, because settling a threshold needs real data.
    """
    line = float(c["dull_line"])
    over_line: list[tuple[str, float]] = []
    for x in pool:
        d = time_decay(x.weight, x.days, c)
        if d >= line:
            over_line.append((x.id, d))
    over_line.sort(key=lambda t: -t[1])
    heaviest = over_line[0][1] if over_line else 0.0
    return heaviest, sum(d for _i, d in over_line), over_line


async def gather_ingredients(c: dict | None = None) -> dict:
    """Load one set of ingredients: the two pools, the four drawn streams, and the
    pressure. **No LLM call, and nothing is written.**"""
    c = c or _c()
    now = _w.now()
    recs, digested = await M.load_records()
    pressing = want_pool(recs, now)
    unclear = unclear_pool(recs, digested, c, now)
    pressure_value, piled_up, over_line = pressure(pressing + unclear, c)
    picked_pressing = weighted_sample(pressing, int(c["want_n"]), c)
    picked_unclear = weighted_sample(unclear, int(c["unclear_n"]), c)
    return {
        "压在心头": picked_pressing,
        "想不明白": picked_unclear,
        "几个词": few_words(recs, c),
        "压力": pressure_value,
        "攒着": piled_up,
        "过线的": over_line,
        "池子": {"压在心头": len(pressing), "想不明白": len(unclear)},
    }


def build_user_message(ingredients: dict, c: dict) -> str:
    """Ingredients -> the user message. **The full text is fed (truncated to ~800
    characters) and each entry carries its own v/a; no global mood is supplied.**"""
    n = int(c["excerpt_chars"])
    return json.dumps({
        "压在心头的": [{"正文": x.text[:n], "分量": round(x.weight, 2),
                        "v": x.v, "a": x.a} for x in ingredients["压在心头"]],
        "想不明白的": [{"正文": x.text[:n], "v": x.v, "a": x.a}
                       for x in ingredients["想不明白"]],
        "几个词": ingredients["几个词"],
    }, ensure_ascii=False, indent=2)


# ============================================================
# Weaving: one independent model call
# ============================================================
def _escape_bare_newlines(s: str) -> str:
    """Escape bare newlines and tabs found **inside** string literals.

    ⚠️ This is not fastidiousness: the JSON the model returns frequently contains bare
    newlines, and `json.loads` blows up on the spot. Only what is inside the quotes is
    touched; not one whitespace character outside them is.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in s:
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                in_string = False
                out.append(ch)
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            out.append(ch)
            continue
        if ch == '"':
            in_string = True
        out.append(ch)
    return "".join(out)


def parse_dream(raw: str) -> dict:
    """The model's reply -> {完整, 碎片, v, a}. **A missing number raises on the spot; no
    fallback is invented.**"""
    from utils import clean_llm_json
    s = clean_llm_json(raw or "")
    data = None
    for fix in (lambda x: x, _escape_bare_newlines):
        try:
            data = json.loads(fix(s))
            break
        except (TypeError, ValueError):
            continue
    if not isinstance(data, dict):
        raise RuntimeError(f"织梦返回的不是 JSON（{len(raw or '')} 字）：{(raw or '')[:200]}")
    whole = str(data.get("完整") or "").strip()
    fragment = str(data.get("碎片") or "").strip()
    if not whole or not fragment:
        raise RuntimeError(f"织梦少了一层：完整 {len(whole)} 字 / 碎片 {len(fragment)} 字")
    try:
        v = float(data["v"])
        a = float(data["a"])
    except (KeyError, TypeError, ValueError) as e:
        raise RuntimeError(f"织梦没给 v/a：{data.keys()}") from e
    return {"完整": whole, "碎片": fragment,
            "v": max(0.0, min(1.0, v)), "a": max(0.0, min(1.0, a))}


async def call_model(ingredients: dict, c: dict) -> dict:
    """🔴 **The only LLM call in the whole system apart from backfill.**

    It goes through the dehydrator's channel (the `dehydration` section of config.yaml is
    the single source of truth; `.env` is empty), but **supplies its own parameters**:
    temperature 1.0 (not the 0.1 used for tagging) and max_tokens 8192.
    Borrowing its `_chat` avoids building a second copy of the key / timeout / three
    api_format branches — **the call itself is independent; only the pipe is shared.**
    """
    chat = getattr(rt.dehydrator, "_chat", None)
    if not callable(chat):
        raise RuntimeError("织梦拿不到模型通道（dehydrator._chat 不在）")
    if not getattr(rt.dehydrator, "api_available", False):
        raise RuntimeError("织梦拿不到 api_key（config.yaml 的 dehydration 段）")
    # ⚠️ At temperature 1.0 the model occasionally drops a layer or drops v/a (seen during
    #    acceptance: HTTP 200 OK, but the JSON contained only 完整 and 碎片). Reweave up to
    #    three times — this is not inventing fallback data, it is weaving again the same
    #    night. Only after three failures does it count as "no dream came out tonight",
    #    and that still fails loudly.
    last_error: Exception | None = None
    for _ in range(3):
        raw = await chat(DREAM_PROMPT, build_user_message(ingredients, c),
                         max_tokens=int(c["max_tokens"]),
                         temperature=float(c["temperature"]))
        try:
            return parse_dream(raw)
        except RuntimeError as e:
            last_error = e
            rt.logger.warning("[dream] 这一织没成形，重织: %s", e)
    raise last_error


# ============================================================
# Disk: both the fragment and the whole version are persisted (after the amendment, the
# whole version is no longer "alive only in the return value")
# ============================================================
def dreams_dir(buckets_dir: str | None = None) -> str:
    bd = buckets_dir or str((rt.config or {}).get("buckets_dir") or ".")
    return os.path.join(bd, "night_fall", "dreams")


def _state_path(buckets_dir: str | None = None) -> str:
    bd = buckets_dir or str((rt.config or {}).get("buckets_dir") or ".")
    return os.path.join(bd, "_state", STATE_FILE)


def load_state() -> dict:
    p = _state_path()
    if not os.path.exists(p):
        return {}
    try:
        d = json.load(open(p, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(d: dict) -> None:
    p = _state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def load_dreams(buckets_dir: str | None = None) -> list[dict]:
    """The dreams currently on disk, newest first. **Only the ones we wrote ourselves
    count** — the `dream_*.md` files night_fall left behind are neither read nor deleted
    (it is retired, but that is history, not litter)."""
    d = dreams_dir(buckets_dir)
    out: list[dict] = []
    try:
        filenames = sorted(os.listdir(d))
    except OSError:
        return out
    for fn in filenames:
        if not (fn.startswith(FILE_PREFIX) and fn.endswith(".json")):
            continue
        p = os.path.join(d, fn)
        try:
            rec = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        rec["_路径"] = p
        out.append(rec)
    out.sort(key=lambda r: str(r.get("织于") or ""), reverse=True)
    return out


def save_record(rec: dict, buckets_dir: str | None = None) -> str:
    d = dreams_dir(buckets_dir)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{FILE_PREFIX}{rec['id']}.json")
    tmp = p + ".tmp"
    keep = {k: v for k, v in rec.items() if not k.startswith("_")}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keep, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return p


def first_sentence(fragment: str) -> str:
    """The one-sentence layer: take the first sentence out of the fragment. **A mechanical
    cut, never through a model** — this is residue, not something newly written."""
    s = str(fragment or "").strip()
    m = re.split(r"(?<=[。！？…\n])", s, maxsplit=1)
    out = (m[0] if m else s).strip()
    return out or s[:40]


LAYER_ORDER = ("完整", "碎片", "一句", "没了")  # forward only, never back (`完整` -> `碎片` is done by degrade_on_wake(), it is never computed by layer_of() itself)


def layer_of(rec: dict, now: datetime, c: dict) -> str:
    """Which layer this is in right now: `完整` / `碎片` / `一句` / `没了`. **Time and
    turns both count, whichever arrives first** — except for `完整`, which is exempt from
    that rule (see below).

    🔴 By amendment: **the whole layer does not decay with time.** As long as the record
       still carries a `完整` field it stays in the `完整` layer and never even reaches the
       time/turn tests below. Its only death is `degrade_on_wake()` — the degrade signal —
       which strips that field and resets the start point to the moment of degradation.
       So this test has to **short-circuit first**, and must not be mixed in with the
       three-layer comparison.

    🔴 **Down only, never up** (the three-layer half; the amendment did not touch this):
       a recall pushes the start point back, and on time alone that would let the
       one-sentence layer be pushed back up into the fragment layer — which is not
       delaying, it is **growing backwards**. The rule allows recall to delay, not to
       prevent; delaying means staying in this layer a while longer, **not returning to
       the previous one.** Hence the recorded `到过的最低层` (lowest layer reached).
    """
    if str(rec.get("完整") or "").strip():
        return "完整"
    start = _w.parse_stamp(rec.get("起算点")) or _w.parse_stamp(rec.get("织于"))
    minutes = 9e9 if start is None else (now - start).total_seconds() / 60.0
    turns = int(_f(rec.get("轮次"), 0))
    if minutes >= float(c["oneline_minutes"]) or turns >= int(c["oneline_turns"]):
        computed = "没了"
    elif minutes >= float(c["fragment_minutes"]) or turns >= int(c["fragment_turns"]):
        computed = "一句"
    else:
        computed = "碎片"
    lowest_reached = str(rec.get("到过的最低层") or "碎片")
    if (lowest_reached in LAYER_ORDER
            and LAYER_ORDER.index(lowest_reached) > LAYER_ORDER.index(computed)):
        return lowest_reached
    return computed


# ============================================================
# Waking: 🔴 the one and only death of the whole layer
# ============================================================
def degrade_on_wake() -> list[str]:
    """The degrade signal, fired by the user's next message after the dream has been handed
    into the window (the caller's job; the caller is lento-v2 `src/chat/梦桥.js`). It drops
    any living `完整` layer down to the fragment layer.

    **Idempotent**: with no living whole layer it does nothing and silently returns an
    empty list — that is not an error. If the gateway's state and this side ever fall out
    of sync (a previous call succeeded but the gateway did not manage to record it, say),
    calling this again is entirely harmless.

    Degrading does exactly two things:
    ① Delete the whole version from disk — literally by stripping the `完整` field
       (`layer_of()` sees it gone and stops calling it the `完整` layer).
    ② Reset the start point to **this moment** — the old 30-minute fragment /
       60-minute one-sentence lifecycle counts from the moment of degradation, not from
       the moment of weaving. Counting from weaving would mean the dream had already half
       rotted before anyone woke up, contradicting the rule that the dream is whole at the
       first sentence after waking and only fragments from the second on.
       It also clears the recall count and the lowest layer reached — degrading starts a
       brand-new lifecycle, and should not carry over the recalls accumulated while the
       whole version was alive.

    This is not one of the `grow`-calling actions like `weave()` / `sweep_expired()`:
    degrading itself leaves no trace. The real "it is gone, leave a trace" still goes the
    old way through `sweep_expired()`, which now simply counts from the moment of
    degradation.
    """
    now = _w.now()
    degraded: list[str] = []
    for rec in load_dreams():
        if not str(rec.get("完整") or "").strip():
            continue
        rec.pop("完整", None)
        rec["起算点"] = now.isoformat(timespec="seconds")
        rec["降级于"] = now.isoformat(timespec="seconds")
        rec["回想次数"] = 0
        rec["到过的最低层"] = "碎片"
        save_record(rec)
        degraded.append(str(rec.get("id") or ""))
    return degraded


# ============================================================
# Leaving a trace: the file is gone, but the fact that there was a dream remains
# ============================================================
async def leave_a_trace(rec: dict) -> str:
    """`grow` one event on the way out, as the file is deleted.

    > **The file is gone, but the fact that there was a dream remains.**
    > It matches the real sensation: **knowing you dreamt last night and being unable to
    > recall what it was.**

    ⛔ **It does not go into "weighing on me"** — a dream is not something that weighs on
       you; that column belongs to what is wanted. So it is grown thetic, an ordinary
       event.
    🔴 **Neutral v/a are used, not the dream's own**, for two solid reasons:
       ① the dream's v/a were **assigned by the model**, and storing them as my own
          feeling breaks "v/a are mine to assign and are never outsourced";
       ② a trace carrying strong emotion would fall straight back into the
          never-worked-out pool -> **dreams feeding dreams**, growing falser each round.
       There is exactly one way to keep the feeling: **`grow` it myself** — the instant it
       is written down it is a memory.
    """
    from tools import grow as _grow
    day = (_w.parse_stamp(rec.get("织于")) or _w.now()).strftime("%m-%d")
    text = f"{day} 做了个梦，没记下来，现在想不起来是什么了。"
    out = await _grow.dispatch(kind="event", items=[
        {"room": "EVENT/SELF", "text": text, "v": 0.5, "a": 0.3}])
    return str(out or "")


async def sweep_expired(c: dict | None = None) -> dict:
    """Dreams whose time is up: delete the file and leave a trace. **Ignore it and it goes
    away by itself.**

    ⚠️ This is a **lazy sweep**: the awakening hook and the fetch-dream endpoint each
       touching it once is enough. There is no cron in Loci, and on a day when nobody
       asks, one day more or less changes nothing — it was going to be gone either way.
    """
    c = c or _c()
    now = _w.now()
    removed, traces = [], []
    for rec in load_dreams():
        if layer_of(rec, now, c) != "没了":
            continue
        p = rec.get("_路径") or ""
        try:
            if p:
                os.remove(p)
        except OSError as e:
            rt.logger.warning("删梦文件失败 %s: %s", p, e)
            continue
        removed.append(str(rec.get("id") or ""))
        try:
            traces.append(await leave_a_trace(rec))
        except Exception as e:                      # noqa: BLE001 - a failed trace must not blow up the sweep
            rt.logger.warning("梦的留痕没写成（文件已删）: %s", e)
    return {"删了": removed, "留痕": traces}


# ============================================================
# The three public actions
# ============================================================
async def weave(force: bool = False, cfg: dict | None = None,
                ingredients: dict | None = None) -> dict | None:
    """Weave one dream.

    Returns the version including the whole text, and **the whole version is persisted
    too** — see the amendment described in this module's header. Its only death is
    `degrade_on_wake()`.

    ⚠️ This docstring used to say the opposite: that the whole version existed only in
       this return value and never reached disk. That was true before the amendment, and
       it sat here contradicting the file's own header — which documents the change and
       adds "to whoever reads this next: that is not a bug, it is the rule." Two
       statements of the rule, in one file, disagreeing. The one that had gone stale is
       the one nearer the code, which is also the one a reader trusts most.
    Returns `None` when below the line (`force=True` skips the pressure line; for the
    bridge and for dry runs).

    The consequence of weaving: **note when it was last dreamt about and nothing else**
    (`last_dreamt`), so this entry is picked less often for a while. **Not one character
    of `weight` is touched, and none of `arousal` either.**

    ⚰️ This used to **zero** the `weight` of every want it dreamt about, on the grounds
       that "it no longer presses on me". That was overturned: **wanting to dream about
       something does not mean the thing has been let go of.** See the long note above
       `cooldown_factor()`.
    """
    c = cfg or _c()
    ingredients = ingredients if ingredients is not None else await gather_ingredients(c)     # if the hook already loaded them, do not rescan the whole store
    if not force and ingredients["压力"] < float(c["pressure_line"]):
        rt.logger.info("[dream] 攒不到线，一夜无梦（压力 %.2f < %.2f）",
                       ingredients["压力"], float(c["pressure_line"]))
        return None
    if not ingredients["压在心头"] and not ingredients["想不明白"]:
        rt.logger.info("[dream] 两个池子都空的，没料可织")
        return None

    dream = await call_model(ingredients, c)
    now = _w.now()
    nightmare = dream["v"] < float(c["nightmare_v"]) and dream["a"] > float(c["nightmare_a"])
    rec = {
        "id": uuid.uuid4().hex[:12],
        "织于": now.isoformat(timespec="seconds"),
        "起算点": now.isoformat(timespec="seconds"),
        "回想次数": 0,
        # ⏳ The turn layer belongs to the bridge: Loci cannot count a turn, so all that is
        # prepared here is the field and the layer rule that reads it
        "轮次": 0,
        "碎片": dream["碎片"],
        # 🔴 By amendment the whole version **is persisted**: the `完整` field IS the whole
        #    version, and `layer_of()` treats its presence as the `完整` layer, exempt from
        #    time decay.
        #    `完整字数` is kept rather than removed — an old smoke assertion reads it, and
        #    deleting it would be pointless breakage.
        "完整": dream["完整"],
        "完整字数": len(dream["完整"]),
        "v": dream["v"], "a": dream["a"],
        "nightmare": bool(nightmare),
        "素材": {
            "压在心头": [x.id for x in ingredients["压在心头"]],
            "想不明白": [x.id for x in ingredients["想不明白"]],
            "几个词": list(ingredients["几个词"]),
        },
        "压力": round(float(ingredients["压力"]), 3),
    }
    save_record(rec)

    # Note when it was last dreamt about — **purely so the same dream does not recur** —
    # without touching weight.
    noted = []
    stamp = now.isoformat(timespec="seconds")
    for x in ingredients["压在心头"]:
        try:
            # bump_active defaults to False: dreaming is not recalling, so leave the
            # forgetting clock alone
            ok = await rt.bucket_mgr.update(x.id, last_dreamt=stamp)
            if not ok:
                # ⚠️ Hit for real during acceptance: while the store is being scanned
                #    concurrently, _find_bucket_file can momentarily fail to find an old
                #    file that is plainly on disk (the path index is ready but the entry is
                #    missing; waiting a beat fixes it). update returning False without a
                #    word is a **green light that lies** — so: wait two seconds, retry
                #    once, and if it still fails record it loudly. Never drop it silently.
                await asyncio.sleep(2)
                ok = await rt.bucket_mgr.update(x.id, last_dreamt=stamp)
            if ok:
                noted.append(x.id)
            else:
                rt.logger.warning("[dream] last_dreamt 没写进去（update 返回 False）: %s", x.id)
        except Exception as e:                      # noqa: BLE001
            rt.logger.warning("last_dreamt 写失败 %s: %s", x.id, e)

    # Bookkeeping for "one dream a night": it resets itself across the day boundary
    # (using the local calendar day, not the container's UTC day)
    today = now.strftime("%Y-%m-%d")
    st = load_state()
    st["今天几个"] = int(_f(st.get("今天几个"), 0)) + 1 if str(st.get("最近一织") or "") == today else 1
    st["最近一织"] = today
    st["最近一织时刻"] = now.isoformat(timespec="seconds")
    save_state(st)

    out = dict(rec)
    out.pop("_路径", None)
    # Re-attached to the returned copy because `rec` is written to disk without it being
    # read back; the persisted record carries it as well (see `save_record` above).
    out["完整"] = dream["完整"]
    out["记下了"] = noted      # ⚰️ this key used to be called 「清零了」 and held the wants that had been zeroed
    return out


def _ingredient_ids(rec: dict) -> list[str]:
    """The memory ids a dream was woven from (the few words are not memories)."""
    material = rec.get("素材") or {}
    return [str(i) for key in ("压在心头", "想不明白") for i in (material.get(key) or [])]


async def withheld_ingredients(rec: dict, holds: "_H.HoldIndex | None" = None,
                               now: datetime | None = None) -> list[str]:
    """The ingredients this dream recorded (`素材`) that may no longer be seen: archived,
    deleted, put out of mind with `dont_surface`, hung with an `avoid` hold since — or
    gone from the store altogether. Any one of them withholds the **whole** dream when it
    is handed out: a dream is woven through and through, and there is no cutting one
    thread out of it. Withholding is per round — nothing on disk changes, and the dream
    goes on fading on its own clock.

    Judged by the gate's `dream_handout` road (`core/visibility.py`). A new version of an
    ingredient does not withhold it: the dream was made from the wording of its night.
    `holds`: the hold index over the store, when the caller has one for several dreams.
    """
    ids = _ingredient_ids(rec)
    if not ids:
        return []
    if holds is None:
        holds = _H.hold_index(await rt.bucket_mgr.list_all(include_archive=False))
    now = now or _w.now()
    out: list[str] = []
    for bid in ids:
        b = await rt.bucket_mgr.get_including_archive(bid)
        if not b or not _V.visible_for(b, road=_V.DREAM_HANDOUT, now=now, holds=holds):
            out.append(bid)
    return out


async def handable_dreams(dreams: list[dict]) -> list[dict]:
    """The dreams that may be handed out this round, in the order given; the withheld
    ones are logged and left out. One hold index serves them all, built only when a dream
    has ingredients to judge."""
    holds = None
    out: list[dict] = []
    for rec in dreams:
        if holds is None and _ingredient_ids(rec):
            holds = _H.hold_index(await rt.bucket_mgr.list_all(include_archive=False))
        unseen = await withheld_ingredients(rec, holds)
        if unseen:
            rt.logger.info("[dream] 这一轮不递梦 %s：%d 样料现在看不得了（%s）",
                           rec.get("id"), len(unseen), "、".join(unseen))
            continue
        out.append(rec)
    return out


async def current_dream(recall: bool = True, cfg: dict | None = None) -> dict | None:
    """Fetch the dream at its current layer. `None` when there is no dream.

    ⚠️ Since the amendment the whole version is persisted and survives until the degrade
    signal, so the layer here can also be `完整`. (The older rule — "on waking you only
    get the fragment" — only held while the whole version was never persisted at all.)
    **Whether the whole version is still available depends on the timeline between the
    reader and this dream**: through a genuine night with nobody sending messages,
    degrade_on_wake() has never been called and the full text still comes out of here.
    Once the dream has been handed over and the user sends their next message, the caller
    hits `/api/loci/dream/wake` once and the whole version really does drop to a fragment —
    from then on this behaves exactly like the old rule: fragment, then one sentence, then
    genuinely gone.
    `recall=True` (the default) means **this fetch counts as one recall**: the start point
    is pushed back a little, **and each push is smaller than the last**
    (`recall_delay × 0.5^n`) — **recall can delay, it cannot prevent.** (That only matters
    for the fragment and one-sentence layers: the whole layer is exempt from time decay,
    so pushing its start point makes no difference to whether it is still `完整`, and
    degrade_on_wake() resets the start point outright at the moment of degradation.)
    """
    c = cfg or _c()
    await sweep_expired(c)
    # A dream with an ingredient that may no longer be seen is withheld this round
    # (`withheld_ingredients`), and a withheld one is not recalled either.
    alive = await handable_dreams(load_dreams())
    if not alive:
        return None
    rec = alive[0]
    now = _w.now()
    layer = layer_of(rec, now, c)
    if layer == "没了":                                  # the edge case where it was just swept away
        return None
    if layer == "完整":
        content = rec.get("完整") or ""
    elif layer == "碎片":
        content = rec["碎片"]
    else:
        content = first_sentence(rec["碎片"])

    if recall:
        n = int(_f(rec.get("回想次数"), 0))
        push = float(c["recall_delay_minutes"]) * (0.5 ** n)
        start = _w.parse_stamp(rec.get("起算点")) or now
        rec["起算点"] = (start + timedelta(minutes=push)).isoformat(timespec="seconds")
        rec["回想次数"] = n + 1
        rec["到过的最低层"] = layer                 # pushing the start point must never drag it back up a layer
        save_record(rec)

    return {
        "id": rec.get("id"),
        "层": layer,
        "内容": content,
        "v": rec.get("v"), "a": rec.get("a"),
        "nightmare": bool(rec.get("nightmare")),
        "织于": rec.get("织于"),
        "回想次数": int(_f(rec.get("回想次数"), 0)),
        # There is one way to keep it: **write it down yourself.** The instant it is
        # written down it is no longer a dream, it is a memory.
        "留住的办法": 'grow(kind="event", room="EVENT/SELF", text=梦的正文)',
    }


async def maintain(cfg: dict | None = None) -> dict:
    """The hook at awakening: **two things, both silent.**

    ① sweep the dreams whose time is up (delete the file, leave a trace)
    ② if the pile is above the line and nothing has been woven today, weave one

    🔴 **Not one word is added to breath** (a hard boundary in the spec): dreams do not
       enter the awakening screen. How a dream is handed into the conversation, and how it
       is removed from the context, is the **bridge's** job and is not touched here.
    """
    c = cfg or _c()
    out: dict = {"扫": {}, "织": None, "压力": None}
    try:
        out["扫"] = await sweep_expired(c)
    except Exception as e:                          # noqa: BLE001
        rt.logger.warning("[dream] 扫一遍失败: %s", e)
    today = _w.now().strftime("%Y-%m-%d")
    st = load_state()
    if str(st.get("最近一织") or "") == today and int(_f(st.get("今天几个"), 0)) >= int(c["per_day"]):
        _note_check(st, None, c, capped=True)
        return out
    try:
        ingredients = await gather_ingredients(c)
        out["压力"] = ingredients["压力"]
        # 🔴 **Record "looked, and it was below the line".** Without that record, "a night
        #    with no dream" and "the hook died silently" look identical on disk — which is
        #    exactly what happened in the first version (an import name clash meant the
        #    hook never ran at all, while the assertion "below the line, no weave" stayed
        #    green). **That is where a lying green light comes from.**
        #    A free bonus on top: `上次压力` gets recorded every day, so settling the
        #    pressure line later has real data to look at.
        _note_check(load_state(), ingredients, c)
        out["织"] = await weave(cfg=c, ingredients=ingredients)
    except Exception as e:                          # noqa: BLE001 - failing to weave must not break breath
        rt.logger.warning("[dream] 织梦失败: %s", e)
    return out


def _note_check(st: dict, ingredients: dict | None, c: dict, capped: bool = False) -> None:
    """Every time the hook takes a look, record it (the moment, and the pressure at that
    moment). **This is not a dream, it is a thermometer.**"""
    st = dict(st or {})
    st["最近一看"] = _w.now().isoformat(timespec="seconds")
    if capped:
        st["最近一看结论"] = "今天织过了（per_day 封顶）"
        save_state(st)
        return
    if ingredients is not None:
        st["上次压力"] = round(float(ingredients["压力"]), 3)      # = the heaviest single entry
        st["上次攒着"] = round(float(ingredients.get("攒着") or 0), 3)   # for inspection only; takes no part in the decision
        st["上次池子"] = ingredients["池子"]
        st["压力线"] = float(c["pressure_line"])
        st["最近一看结论"] = ("过线，织" if ingredients["压力"] >= float(c["pressure_line"])
                              else "攒不到线，一夜无梦")
    save_state(st)
