"""
========================================
dehydrator.py — the side model: the backfill prompt and its parser, the one chat call
========================================

This file wraps every call that goes to the side model (a cheap external LLM). The
backfill (tools/grow/_backfill), the slicer (core/_slicer) and the dream (core/_dream)
all send their prompt through Dehydrator._chat; none of them holds a client of its own.

Key behaviours:
- backfill_request(content, context) / parse_backfill(raw, content): the one call that
  fills a new entry's blanks — name, summary, tags (scene anchors, verified to appear
  literally in the body), aliases (feed bm25 only), subjects with kinds, and the
  slots read off the sentence (bound, the time phrase, dreamt or imagined, evidential,
  cue phrasings, looks like a promise). The call itself and what gets written are
  tools/grow/rooms_path._backfill_one's
- Dehydrator._chat(system, user): one chat call in the configured format — an
  OpenAI-compatible client (DeepSeek / Ollama / LM Studio / vLLM / Gemini all work),
  native Gemini, or native Anthropic — retried on transient errors
- The summary table (dehydration_cache.db): the key of a row and the writer of one.
  Nothing in the server writes a row or reads one back; see the note in
  Dehydrator.__init__

What it deliberately does not do:
- It neither reads nor writes memory bucket files (it has no idea what a bucket looks like)
- It does not decide when to be called, and it makes no dedup judgements
- With no API key it does not raise on construction: `_chat` answers "" and the layer
  above decides what to do (`_require_api` is there for a caller that would rather raise)

Exports: the Dehydrator class (`_chat`, `_require_api`, the connection settings),
         BACKFILL_PROMPT · BACKFILL_KINDS · BACKFILL_MAX_TOKENS,
         BackfillAnswer · backfill_request · parse_backfill · backfill_kinds,
         RUNS_ON · runs_local · endpoint · thinking_on (where the side model runs and
         what it is called with: the local Ollama or the configured provider),
         openai_client (the OpenAI-compatible client, with no retries of its own)
========================================
"""


import os
import re
import json
import asyncio
import hashlib
import sqlite3
import weakref
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from openai import AsyncOpenAI

from utils import clean_llm_json, positive_float

from . import names as _names
from . import prompts as _prompts
from .provider_detect import (
    is_gemini_native_host,
    strip_native_resource_prefix,
)

logger = logging.getLogger("loci_brain.dehydrator")


# ============================================================
# Tunable constants
# ------------------------------------------------------------
# rule.md §①: no bare magic numbers. Gathered here rather than spread across the
# functions below, the whole tuning surface is visible at a glance. The prompt
# template itself stays below, where readability wins.
# ============================================================

# --- Prompt version ---
# Part of the key a summary row is stored under (see _content_key).
_PROMPT_VERSION = 5

# --- LLM defaults ---
_DEFAULT_MODEL = "gemini-2.0-flash"
_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
_DEFAULT_MAX_TOKENS = 1024
_DEFAULT_TEMPERATURE = 0.1
_API_TIMEOUT_SECONDS = 60.0

# --- Retry on transient errors (the Gemini free tier throws the occasional 429 / 503;
#     see the troubleshooting table in the README) ---
# Total attempts = 1 initial + (max_attempts-1) retries; backoff of base*2^attempt seconds.
_RETRY_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY = 0.8
_RETRY_STATUS = {429, 500, 502, 503, 504}

# --- Input truncation cap of the backfill call (so the prompt cannot blow past the
#     token limit) ---
_BACKFILL_INPUT_LIMIT = 2000

# --- max_tokens override for the backfill call ---
BACKFILL_MAX_TOKENS = 4096      # thinking models burn a lot of tokens; leave headroom

# --- Output truncation lengths ---
_TAGS_MAX = 15           # how many tags are kept at most
_MAX_TAG_LEN = 128       # one tag or alias (bucket_manager caps at the same length)
# Tags that merely point at "the two people this store is about" — their names, and bare
# pronouns — never make it into tags. The reasoning is in the comments inside
# parse_backfill.
# 🔴 The names themselves are **not in the code.** Hard-coding them would amount to
#    publishing living people's names, and anyone cloning this repo would inherit a filter
#    for someone else's household, which is useless to them.
#    The names come from `AI_NAME` / `LOCI_OWNER_NAME` and the `people:` section of
#    config.yaml. All that stays in code is the **generic pronouns**, which hold for
#    everyone.
# Terms of endearment and role words are deliberately absent from this list — those
# describe the act of addressing someone and do carry information.
_PRONOUN_TAGS = frozenset({"她", "他", "我", "你"})


def _person_tags() -> frozenset:
    """The person names that must not become tags: the generic pronouns, plus **every way
    the two people are addressed**.

    🔴 There is exactly **one** list of person names, and it is the alias table
       (`aliases.yaml`). No second list is started here.
       A separate `people:` section was once added to config as well, which recorded the
       same fact in two places — and two places guarantees one of them goes stale. It was
       collapsed into what it is now:
         · **who the two people are** -> `AI_NAME` / `LOCI_OWNER_NAME` (one canonical name each)
         · **every way they get addressed** -> all the aliases those two carry in the table
       So there is one table to maintain, and a future panel only needs one editor for it.
    """
    from utils import get_ai_name, get_owner_name

    canon_names: set[str] = set()
    for n in (get_ai_name(), get_owner_name()):
        n = str(n or "").strip()
        if n and n != "AI":          # the placeholder used when no name is configured is not a name
            canon_names.add(n)
    if not canon_names:
        return frozenset(_PRONOUN_TAGS)

    names = set(canon_names)
    try:
        # The alias table maps {lowercased alias: canonical name}; look it up backwards to
        # get every form of address hanging off those two canonical names
        for alias, canon in (_names.load_alias_table() or {}).items():
            if str(canon).strip() in canon_names:
                names.add(str(alias).strip())
                names.add(str(canon).strip())
    except Exception:                # if the table will not read, block the canonical names and pronouns only, and do not raise
        pass
    return frozenset(_PRONOUN_TAGS | {n for n in names if n})
_NAME_MAX_CHARS = 20     # cap on a backfilled name


# --- Backfill prompt: one call fills every slot the sentence itself can answer ---
# Who fills which slot: the main model writes only what cannot be read off the sentence
# (whether it is wanted, how it felt, where it goes); what can be read off it is this
# prompt's; what has a known origin is code's. So the model copies a time phrase word for
# word and core/_dates.py does the arithmetic, and the model names a kind for each name
# while the names table decides whether that kind is ever written down.
#
# The three tag kinds keep their split:
#   tags     the scene anchors — what a photograph of the moment would show. Verified to
#            appear literally in the body (a model substitutes a near-variant about 4% of
#            the time; the check is in code, not in the prompt).
#   aliases  broadenings: they feed bm25 only, never the vectors, never a row a human reads.
#   subjects who or what — people and things (a game, a book, a group), each with a kind.
#            A name has to tell who is meant: a bare title (妈, 老板娘) is a word many
#            people share, so it is asked for with whose or where it is (「小思的妈」).
#            A prompt that asks for "card" is also shown the cards the table has, and a
#            subject may then say which of them it is (`_cards_line`, BackfillAnswer.same).
#
# `{kinds}` is filled in per call (see backfill_kinds); everything else is literal JSON.
# This is the shipped text: the owner may rewrite it from grow's 设置, and the call
# takes core/prompts.current("backfill"), which keeps `{kinds}` and every key
# parse_backfill reads.
BACKFILL_PROMPT = """你是记忆系统的回填器。下面是一条刚存下的记忆：正文是主模型写的，一个字都不能改；你只读这段话，把能从字面上读出来的格子填上。

总规则：
1. 只读这段话。原文没写的，一个都不许补；拿不准就留空（""、[]、false）。
2. 「已经有的」里列出的格子是主模型自己写的，对应的键一律留空，不要重填。

各个格子：
- name：10字以内的简短标题。
- summary：一句话摘要，中文，不超过60字。一件事就概括发生了什么；一条认知/感受就概括认识到了什么。
- tags（场景锚点）：设想那一刻如果拍了一张照片——原文里哪些词是照片里能看见的东西（人、地方、物件），或者身体当时直接接收到的（光线明暗、冷热、声音、气味）。必须是原文里的原词。房间是 MIND 的，一律交白卷。
- aliases（引申词）：5~8 个语义相关词（近义词、上位词、换个说法搜它时会用的词）。
- subjects（主体）：这段话里出现的人和东西（游戏、书、群、作品……；地点不算）。每个写成 {"name": "能分清是谁的叫法", "kind": "它是什么"}：有名字就原样写名字；只有称呼（妈、老板娘、老大、房东……）就带上是谁的或哪儿的，如「小思的妈」「面馆的老板娘」；原文看不出是谁的就照抄称呼。kind 只能从这些里选：{kinds}；认不出是什么就写 ""。纯代词一律不要（我/你/他/她/它/我们/自己/对方等）——代词是指代不是名字。不要推测真名，不要给没出现的人。如果它就是「已有的名片」里的某一个（同一个人换了个叫法），再加一项 "card"，写那张名片的名字；拿不准就不写。
- bound（谁被绑着）：只在「想让它发生」是「是」的时候填：这件事是谁答应的、谁要去做（「我们俩约好」就是两个人都在）。写名字；写这条记忆的「我」就写「我」，跟「我」说话的那个人写「你」。
- time：句子里说到的时间，**原样抄下来，不要换算**，例如 {"phrase": "下周一两点", "yearly": false, "absolute": ""}。每年都会再来的（生日、纪念日，「每年8月7号」）yearly 写 true。absolute 只在原文写明了是哪一天、你能确定时写 YYYY-MM-DD，否则留空。没说时间就都留空。
- internally_generated：只看房间是 EVENT 的。这段话说的是梦见的、想象的、假设的（「梦见」「幻想」「假如」）写 true。
- evidential：只看房间是 MIND 的。从看得见的迹象推出来的（「看来」「应该是」）写 "inference"；凭猜测、常识（「可能」「大概」「我猜」）写 "assumption"；都不是就留空。
- cue_phrasings：只在「在等的事」有内容时填：那件事以后可能被怎么说出来，写几种说法（最多 16 句，每句不超过 200 字）。
- looks_like_promise：只在「想让它发生」是「否」的时候看：这句话读起来像个承诺、约定（「答应」「说好」「下次一定」）就写 true。

输出格式（纯 JSON，无其他内容）：
{
  "name": "简短标题",
  "summary": "一句话摘要",
  "tags": ["场景锚点"],
  "aliases": ["引申词"],
  "subjects": [{"name": "称呼", "kind": "人", "card": ""}],
  "bound": [],
  "time": {"phrase": "", "yearly": false, "absolute": ""},
  "internally_generated": false,
  "evidential": "",
  "cue_phrasings": [],
  "looks_like_promise": false
}"""

# The kinds the prompt offers. A kind the side model returns has to be one of these or
# one the names table already uses: an unknown word would be offered on the names page as
# the guess, and one 新增 would make it a kind in the table, so a new kind is the
# owner's to introduce on the panel, not the side model's.
BACKFILL_KINDS = ("人", "游戏", "书", "群", "影视", "作品", "动物", "组织")
_WEEKDAY_NAMES = "一二三四五六日"
_EVIDENTIALS = ("inference", "assumption")
_TIME_PHRASE_MAX = 40
_PHRASINGS_MAX = 16
_PHRASING_MAX_CHARS = 200
_SUMMARY_MAX_CHARS = 200
_BOUND_MAX = 8
_SUBJECTS_MAX = 16
_SUBJECT_NAME_MAX = 40       # the names table refuses a longer name (core/names._check_name)
_CARDS_MAX = 80              # how many of the table's cards one call is shown
_CARD_KEY = '"card"'         # a prompt that asks for this key is shown the cards
_KIND_MAX = 8
_ABSOLUTE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def backfill_kinds() -> tuple[str, ...]:
    """The kinds a subject may carry: the prompt's own, then any other kind the names
    table already uses (the owner's vocabulary)."""
    kinds = list(BACKFILL_KINDS)
    try:
        for rec in _names.load_names_table().values():
            if rec.instance_of and rec.instance_of not in kinds:
                kinds.append(rec.instance_of)
    except Exception:                # an unreadable table offers the prompt's kinds only
        pass
    return tuple(kinds)


def _cards_line() -> str:
    """The cards the names table has, as one line of the request: each name with its
    other spellings, in the table's order, the first `_CARDS_MAX`. "" for an empty or
    unreadable table."""
    try:
        records = list(_names.load_names_table().values())[:_CARDS_MAX]
    except Exception:
        return ""
    shown = [r.name + (f"（也叫 {'、'.join(r.aliases)}）" if r.aliases else "") for r in records]
    return "、".join(shown)


def backfill_request(content: str, context: dict, kinds=None) -> tuple[str, str]:
    """The (system, user) pair for one backfill call. `context` describes the entry as it
    is on disk: room, telic, created_day (a date), said_day (the day its lines were said,
    when known: shown in place of the day it was written), cue_condition, and `existing` —
    {slot: value} for the slots the main model already filled, which the model is told
    to leave alone. The system text is the library's prompt (core/prompts: the owner's
    rewrite when there is one, BACKFILL_PROMPT otherwise). When that prompt asks for a
    subject's "card", the request lists the cards there are (`_cards_line`)."""
    kinds = tuple(kinds or backfill_kinds())
    system = _prompts.current(_prompts.BACKFILL).replace("{kinds}", " / ".join(kinds))
    said = context.get("said_day")
    day = said or context.get("created_day")
    lines = ["【这条记忆】",
             f"房间：{context.get('room') or '（没写）'}"]
    if day is not None:
        label = "原话说在" if said else "存下的那天"
        lines.append(f"{label}：{day.isoformat()}（星期{_WEEKDAY_NAMES[day.weekday()]}）")
    lines.append(f"想让它发生：{'是' if context.get('telic') else '否'}")
    lines.append(f"在等的事：{context.get('cue_condition') or '（无）'}")
    existing = {k: v for k, v in (context.get("existing") or {}).items() if v}
    if existing:
        shown = "；".join(f"{k}={'、'.join(map(str, v)) if isinstance(v, list) else v}"
                         for k, v in existing.items())
        lines.append(f"已经有的：{shown}")
    cards = _cards_line() if _CARD_KEY in system else ""
    if cards:
        lines.append(f"已有的名片：{cards}")
    lines += ["", "【正文】", content[:_BACKFILL_INPUT_LIMIT]]
    return system, "\n".join(lines)


@dataclass
class BackfillAnswer:
    """What one backfill call said, every part validated on its own. An empty value is
    "not said"; `subjects` is None when the names part was absent or malformed, which is
    different from a valid empty list. `problems` names each part that was dropped."""
    name: str = ""
    summary: str = ""
    tags: list = field(default_factory=list)
    aliases: list = field(default_factory=list)
    subjects: Optional[list] = None           # [(name, kind)], kind "" when not known
    same: dict = field(default_factory=dict)  # {name: the card the model says it is}
    bound: list = field(default_factory=list)
    time_phrase: str = ""
    time_yearly: bool = False
    time_absolute: str = ""
    internally_generated: bool = False
    evidential: str = ""
    cue_phrasings: list = field(default_factory=list)
    looks_like_promise: bool = False
    problems: list = field(default_factory=list)

    def says_anything(self) -> bool:
        return any((self.name, self.summary, self.tags, self.aliases,
                    self.subjects, self.bound, self.time_phrase, self.time_absolute,
                    self.internally_generated, self.evidential, self.cue_phrasings,
                    self.looks_like_promise))


def _clean_text(value) -> str:
    return re.sub(r"\[\[([^\]]+)\]\]", r"\1", value).strip().strip('"').strip()


def _string_list(value, *, max_items: int, max_chars: int) -> Optional[list]:
    """A list of strings, stripped, deduplicated, empty ones dropped; an item over
    max_chars is dropped rather than cut (half a phrase is not a phrase). None when the
    value is not a list of strings at all."""
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        return None
    out: list[str] = []
    for x in value:
        x = _clean_text(x)
        if x and len(x) <= max_chars and x not in out:
            out.append(x)
    return out[:max_items]


def _subjects_part(value, kinds) -> Optional[tuple[list, dict]]:
    """([(name, kind)], {name: card}) from [{"name", "kind", "card"}], or None when any
    item is malformed: a name that is not a short one-line string, a kind or a card that
    is not a string, or a kind outside `kinds`. One bad item spoils the part — a model
    returning a bad shape must not get the rest of its names into the table. A card is
    kept only when the names table has an entry by that spelling, under that entry's
    name; one it does not have is a wrong guess and is left out."""
    if not isinstance(value, list) or len(value) > _SUBJECTS_MAX:
        return None
    out: list[tuple[str, str]] = []
    same: dict[str, str] = {}
    for item in value:
        if not isinstance(item, dict) or set(item) - {"name", "kind", "card"}:
            return None
        name, kind, card = item.get("name"), item.get("kind", ""), item.get("card", "")
        if kind is None:
            kind = ""
        if card is None:
            card = ""
        if not isinstance(name, str) or not isinstance(kind, str) or not isinstance(card, str):
            return None
        name, kind, card = name.strip(), kind.strip(), card.strip()
        if not name or len(name) > _SUBJECT_NAME_MAX or "\n" in name or "\r" in name:
            return None
        if kind and (len(kind) > _KIND_MAX or kind not in kinds):
            return None
        if name in [n for n, _ in out]:
            continue
        out.append((name, kind))
        rec = _names.record_of(card) if card and len(card) <= _SUBJECT_NAME_MAX else None
        if rec is not None:
            same[name] = rec.name
    return out, same


def _real_day(value: str) -> bool:
    if not _ABSOLUTE_DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def parse_backfill(raw: str, content: str, kinds=None) -> Optional["BackfillAnswer"]:
    """Validate one backfill answer strictly. None when it is not a JSON object or says
    nothing usable; otherwise every part that holds up, each part on its own — a bad
    `time` does not cost the summary. Unknown keys (valence, arousal, ...) are dropped:
    how it felt is never the side model's to answer."""
    try:
        parsed = json.loads(clean_llm_json(raw or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    kinds = tuple(kinds or backfill_kinds())
    ans = BackfillAnswer()

    def text(key: str, cap: int) -> str:
        v = parsed.get(key, "")
        if v in (None, ""):
            return ""
        if not isinstance(v, str):
            ans.problems.append(key)
            return ""
        return _clean_text(v)[:cap]

    def strings(key: str, max_items: int, max_chars: int) -> list:
        v = parsed.get(key, [])
        if v in (None, ""):
            return []
        out = _string_list(v, max_items=max_items, max_chars=max_chars)
        if out is None:
            ans.problems.append(key)
            return []
        return out

    def flag(key: str) -> bool:
        v = parsed.get(key, False)
        if v in (None, ""):
            return False
        if not isinstance(v, bool):
            ans.problems.append(key)
            return False
        return v

    ans.name = text("name", _NAME_MAX_CHARS)
    ans.summary = text("summary", _SUMMARY_MAX_CHARS)
    # Tags that merely point at the two people this store is about never get in: the
    # whole store is about them, so their names and the bare pronouns in `_PRONOUN_TAGS`
    # distinguish nothing. Only those two are blocked — anyone else's name does
    # distinguish, and is kept. Blocked here in code; the model may output them all it
    # likes.
    person_stop = _person_tags()      # recomputed every time: a config change needs no restart
    tags = [t for t in strings("tags", _TAGS_MAX * 2, _MAX_TAG_LEN)
            if t in content and t not in person_stop]
    aliases = [t for t in strings("aliases", _TAGS_MAX * 2, _MAX_TAG_LEN)
               if t not in person_stop and t not in tags]

    if parsed.get("subjects") not in (None, ""):
        part = _subjects_part(parsed.get("subjects"), kinds)
        if part is None:
            ans.problems.append("subjects")
        else:
            ans.subjects, ans.same = part
    # A name already in subjects says nothing more as a label: it never reaches tags or
    # aliases. This needs no configuration — it uses the names this answer extracted.
    named = {n for n, _ in ans.subjects or []}
    ans.tags = [t for t in tags if t not in named][:_TAGS_MAX]
    ans.aliases = [t for t in aliases if t not in named][:_TAGS_MAX]
    ans.bound = strings("bound", _BOUND_MAX, _SUBJECT_NAME_MAX)

    t = parsed.get("time")
    if isinstance(t, dict) and not set(t) - {"phrase", "yearly", "absolute"}:
        phrase, yearly, absolute = t.get("phrase") or "", t.get("yearly") or False, \
            t.get("absolute") or ""
        if (isinstance(phrase, str) and isinstance(yearly, bool)
                and isinstance(absolute, str) and len(phrase.strip()) <= _TIME_PHRASE_MAX
                and (not absolute.strip() or _real_day(absolute.strip()))):
            ans.time_phrase, ans.time_yearly = phrase.strip(), yearly
            ans.time_absolute = absolute.strip()
        else:
            ans.problems.append("time")
    elif t not in (None, "", {}):
        ans.problems.append("time")

    ans.internally_generated = flag("internally_generated")
    ev = text("evidential", 16)
    if ev and ev not in _EVIDENTIALS:
        ans.problems.append("evidential")
        ev = ""
    ans.evidential = ev
    ans.cue_phrasings = strings("cue_phrasings", _PHRASINGS_MAX, _PHRASING_MAX_CHARS)
    ans.looks_like_promise = flag("looks_like_promise")
    return ans if ans.says_anything() else None


# Where the side model runs (`dehydration.runs_on`): a provider in the cloud (the default),
# or the local Ollama.
RUNS_ON = ("cloud", "local")

# What a local Ollama is sent as the key: it checks none, the OpenAI client insists on a
# non-empty one, and a cloud key kept for switching back must never reach a local or
# user-supplied address (the same rule as the embedding's local mode).
_LOCAL_KEY = "ollama"

# Anthropic's extended thinking: the smallest budget it takes, and the answer room kept
# on top of the budget (the thinking counts inside max_tokens there).
_ANTHROPIC_MIN_THINKING = 1024
_ANTHROPIC_ANSWER_ROOM = 1024


def runs_local(dehy_cfg: dict) -> bool:
    return str((dehy_cfg or {}).get("runs_on") or "").strip().lower() == "local"


def endpoint(dehy_cfg: dict, config: dict | None = None) -> tuple[str, str, str]:
    """(api_format, base_url, api_key) the side model is called with.

    Local: Ollama's OpenAI-compatible endpoint (core/ollama_local) and a placeholder key;
    the saved cloud format, address and key stay in config for switching back.
    Cloud: the configured three. A Google AI Studio key of the newer form (`AQ.*`) is not
    accepted by Gemini's OpenAI-compatible endpoint, so on Google's host it goes to the
    native generateContent API without anyone setting api_format by hand."""
    dehy_cfg = dehy_cfg or {}
    if runs_local(dehy_cfg):
        from . import ollama_local
        return "openai_compat", ollama_local.openai_base(config), _LOCAL_KEY
    api_format = dehy_cfg.get("api_format", "openai_compat")
    base_url = dehy_cfg.get("base_url", _DEFAULT_BASE_URL)
    api_key = str(dehy_cfg.get("api_key", "") or "")
    if (
        api_format == "openai_compat"
        and api_key.startswith("AQ.")
        and is_gemini_native_host(base_url)
    ):
        api_format = "gemini"
        logger.info("AQ.* key + generativelanguage.googleapis.com detected — auto-switching to native Gemini API")
    return api_format, base_url, api_key


def openai_client(api_key: str, base_url: str, timeout: float) -> AsyncOpenAI:
    """The side model's OpenAI-compatible client. `max_retries=0`: Dehydrator._chat is
    the one place a failed call is retried (_RETRY_MAX_ATTEMPTS, with backoff). The SDK's
    own retries (two by default, on timeouts, 429 and 5xx alike) would run inside each of
    those attempts, so a side model slower than `timeout` would be sent every call
    3 × 3 = 9 times — each one paid, none of them read."""
    return AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)


def thinking_on(dehy_cfg: dict) -> bool:
    """`dehydration.thinking`: let a model that thinks before answering do so. Off by
    default: tagging is mechanical and needs no thinking."""
    raw = (dehy_cfg or {}).get("thinking", False)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


class Dehydrator:
    """
    The side model: its connection settings and the one chat entry point, `_chat`.
    The backfill's call goes through `_chat` from tools/grow/_backfill, with the prompt
    and the parser above; the slicer's and the dream's go through it with prompts of
    their own. There is no local stand-in for the model: `api_available` says whether a
    key is configured, and `_require_api` raises RuntimeError for a caller that must have
    it, so the failure reaches the user instead of a silently empty answer.
    """

    def __init__(self, config: dict):
        # --- Read dehydration API config ---
        dehy_cfg = config.get("dehydration", {})
        self.model = dehy_cfg.get("model", _DEFAULT_MODEL)
        self.max_tokens = dehy_cfg.get("max_tokens", _DEFAULT_MAX_TOKENS)
        self.temperature = dehy_cfg.get("temperature", _DEFAULT_TEMPERATURE)
        self.timeout_seconds = positive_float(dehy_cfg.get("timeout_seconds"), _API_TIMEOUT_SECONDS)
        # api_format: "openai_compat" (default) | "gemini" | "anthropic"; where it runs and
        # what it is called with come from one function, which the panel's hot update uses
        # too (web/config_api).
        self.runs_on = "local" if runs_local(dehy_cfg) else "cloud"
        self.api_format, self.base_url, self.api_key = endpoint(dehy_cfg, config)
        # thinking: on, a model that thinks first may (Gemini: no budget cap is sent;
        # Anthropic: extended thinking). The OpenAI-compatible format has no knob that
        # every provider reads, so nothing is sent there either way.
        self.thinking = thinking_on(dehy_cfg)
        # thinking_budget: only applies to Gemini's "thinking" models, while thinking is
        # off. Default 0 = thinking off.
        # The point: models of that family spend output tokens on "thinking" first, and
        # when max_tokens is small the thinking eats the entire budget, so what comes back
        # is empty text. The side model's calls are mechanical transforms and need no
        # thinking at all, so turning it off avoids the empty output and is faster and
        # cheaper besides. Setting it to None omits the field entirely, for older models
        # that do not understand thinkingConfig.
        self.thinking_budget = dehy_cfg.get("thinking_budget", 0)

        # --- How the human side is addressed ---
        # Part of a summary row's key (_content_key). Same source as config.human, which
        # the front end can edit.
        self.human = config.get("human", "用户") or "用户"

        # --- API availability ---
        self.api_available = bool(self.api_key)

        # --- Initialize OpenAI-compatible client (only for openai_compat format) ---
        self.client: Optional[AsyncOpenAI] = None
        if self.api_available and self.api_format == "openai_compat":
            self.client = openai_client(self.api_key, self.base_url, self.timeout_seconds)

        # --- The summary table: content hash -> summary ---
        # Nothing in the server writes a row or reads a summary back. A library may hold
        # rows all the same, and a source change clears the ones that carry a cleared
        # entry's words (core/_source_change, the `dehydration_cache` place), finding them
        # by `_content_key` or by the words in the summary. `_set_cached_summary` is how
        # exam's setup puts a row there (exam/runner.seed).
        db_path = os.path.join(config["buckets_dir"], "dehydration_cache.db")
        self.cache_db_path = db_path
        self._cache_conn: sqlite3.Connection = self._init_cache_db()
        # One connection for the instance's lifetime, without leaking the Windows file
        # handle when a runtime/test instance is released.
        # ``weakref.finalize`` also runs during interpreter shutdown in reverse
        # creation order, before an enclosing temporary vault is cleaned up.
        self._cache_finalizer = weakref.finalize(self, self._cache_conn.close)

    def close(self) -> None:
        """Close the persistent cache connection; safe to call repeatedly."""

        self._cache_finalizer()

    def _init_cache_db(self) -> sqlite3.Connection:
        """Open (or create) the summary table's DB; return a persistent connection."""
        os.makedirs(os.path.dirname(self.cache_db_path), exist_ok=True)
        # check_same_thread=False is safe here: asyncio runs on one thread and the
        # write is a synchronous helper method called from that same thread.
        conn = sqlite3.connect(self.cache_db_path, check_same_thread=False)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dehydration_cache (
                content_hash TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        return conn

    def _content_key(self, content: str) -> str:
        """The key of a summary row = hash(prompt version + person name + model config +
        original text): a row belongs to the body it summarises under one prompt, one
        name and one model, not to the body alone."""
        keyed = (
            f"{_PROMPT_VERSION}|{self.human}|{self.api_format}|"
            f"{self.base_url.rstrip('/')}|{self.model}|{content}"
        )
        return hashlib.sha256(keyed.encode()).hexdigest()

    def _set_cached_summary(self, content: str, summary: str):
        """Store one summary row under the content's key."""
        self._cache_conn.execute(
            "INSERT OR REPLACE INTO dehydration_cache (content_hash, summary, model) VALUES (?, ?, ?)",
            (self._content_key(content), summary, self.model)
        )
        self._cache_conn.commit()

    # ---------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------
    def _require_api(self) -> None:
        """Raise a RuntimeError with one shared message when the API is unavailable.

        A caller that must have the model (the slicer, core/_slicer.side_model) writes one
        line, `dehydrator._require_api()`, and the wording lives in one place.
        """
        if not self.api_available:
            raise RuntimeError("脱水 API 不可用，请检查 config.yaml 中的 dehydration 配置")

    @staticmethod
    def _is_transient_error(exc: BaseException) -> bool:
        """Is this a transient, retryable error: HTTP 429/500/502/503/504, a timeout, or a
        connection error?

        It handles both httpx.HTTPStatusError (where status_code lives on `.response`) and
        openai.APIStatusError (where it lives on the exception); anything else falls back to
        matching the class name against timeout / connect / ratelimit / unavailable."""
        status = getattr(exc, "status_code", None)
        if status is None:
            resp = getattr(exc, "response", None)
            status = getattr(resp, "status_code", None)
        if isinstance(status, int) and status in _RETRY_STATUS:
            return True
        name = type(exc).__name__.lower()
        return any(k in name for k in ("timeout", "connect", "ratelimit", "unavailable"))

    async def _chat(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        model: str | None = None,
    ) -> str:
        """The single chat entry point: exponential backoff retries on transient errors
        such as 429 / 5xx / timeouts. `model` names another model on the same endpoint and
        key (the slicer's, core/_slicer.py); None is the configured one.

        The actual single call lives in _chat_once; this only handles retry and backoff, so
        that the occasional 429/503 from a free tier does not knock a side-model call
        out cold (see the troubleshooting table in the README)."""
        last_exc: BaseException | None = None
        for attempt in range(_RETRY_MAX_ATTEMPTS):
            try:
                return await self._chat_once(
                    system, user, max_tokens=max_tokens, temperature=temperature,
                    model=model,
                )
            except Exception as e:
                if not self._is_transient_error(e) or attempt == _RETRY_MAX_ATTEMPTS - 1:
                    raise
                last_exc = e
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"_chat 瞬时错误，{delay:.1f}s 后重试 "
                    f"({attempt + 1}/{_RETRY_MAX_ATTEMPTS}): {type(e).__name__}: {e}"
                )
                await asyncio.sleep(delay)
        if last_exc is not None:
            raise last_exc
        return ""

    async def _chat_once(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        model: str | None = None,
    ) -> str:
        """The single OpenAI-compatible chat call.

        It holds the boilerplate every caller would otherwise repeat: build the
        messages, call client.chat.completions.create, check that response.choices is
        non-empty, take choices[0].message.content and fall back to an empty string.
        So:
          * the caller passes a system and user prompt plus optional max_tokens / temperature
          * defaults come from self.max_tokens / self.temperature (decided by config.yaml)
          * it always returns a str (an empty one if the response is malformed, leaving the
            decision to each caller)

        Parameters:
            system, user — the system/user messages of the chat completion
            max_tokens   — override the default (the backfill wants its own)
            temperature  — override the default
            model        — another model on the same endpoint; None = self.model
        """
        if self.api_format == "gemini":
            return await self._chat_gemini(system, user, max_tokens=max_tokens,
                                           temperature=temperature, model=model)
        if self.api_format == "anthropic":
            return await self._chat_anthropic(system, user, max_tokens=max_tokens,
                                              temperature=temperature, model=model)
        if self.api_format != "openai_compat":
            logger.warning(f"Unknown api_format '{self.api_format}', falling back to openai_compat")
        # openai_compat (default)
        if self.client is None:
            return ""
        response = await self.client.chat.completions.create(
            model=model or self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
            temperature=temperature if temperature is not None else self.temperature,
        )
        if not response.choices:
            return ""
        return response.choices[0].message.content or ""

    async def _chat_gemini(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        model: str | None = None,
    ) -> str:
        """Native Gemini generateContent API call (no OpenAI-compat wrapper)."""
        if not self.api_key:
            return ""
        import httpx
        # Strip any accidental "models/" prefix — Google rejects double-prefix in the URL
        model_id = strip_native_resource_prefix(model or self.model)
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
        payload: dict = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens if max_tokens is not None else self.max_tokens,
                "temperature": temperature if temperature is not None else self.temperature,
            },
        }
        # Thinking off: cap the budget (see the thinking_budget note in __init__). On: no
        # cap is sent, and the model thinks as it does by default.
        if not getattr(self, "thinking", False) and self.thinking_budget is not None:
            payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": self.thinking_budget}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            r = await client.post(
                url,
                headers={"x-goog-api-key": self.api_key},
                json=payload,
            )
            r.raise_for_status()
        data = r.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        return parts[0].get("text", "") if parts else ""

    async def _chat_anthropic(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        model: str | None = None,
    ) -> str:
        """Native Anthropic Messages API call."""
        if not self.api_key:
            return ""
        import httpx
        base = self.base_url.rstrip("/") if self.base_url else "https://api.anthropic.com"
        url = f"{base}/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload: dict = {
            "model": model or self.model,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": temperature if temperature is not None else self.temperature,
        }
        if getattr(self, "thinking", False):
            # Extended thinking: the thinking counts inside max_tokens, the budget has a
            # floor, and a temperature other than the default is refused with it.
            budget = max(_ANTHROPIC_MIN_THINKING, int(payload["max_tokens"]) // 2)
            payload["max_tokens"] = max(int(payload["max_tokens"]), budget + _ANTHROPIC_ANSWER_ROOM)
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            payload.pop("temperature")
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
        data = r.json()
        # The answer is the text block; with thinking on, a thinking block comes first.
        for block in data.get("content", []) or []:
            if isinstance(block, dict) and block.get("type", "text") == "text":
                return block.get("text", "") or ""
        return ""
