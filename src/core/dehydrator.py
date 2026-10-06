"""
========================================
dehydrator.py — the LLM calls: dehydrate / merge / backfill / split
========================================

This file wraps every prompt and every call that goes to an external LLM. tools/hold,
tools/grow, tools/dream and the rest all go through it whenever a model has to understand
some content; none of them assembles a prompt itself.

Key behaviours:
- dehydrate(content): compress long content into a dense summary and save tokens
- merge(old, new): blend new content into old while keeping bucket size roughly constant
- backfill_request(content, context) / parse_backfill(raw, content): the one call that
  fills a new entry's blanks — name, summary, tags (scene anchors, verified to appear
  literally in the body), aliases (feed bm25 only), domain, subjects with kinds, and the
  slots read off the sentence (bound, the time phrase, dreamt or imagined, evidential,
  cue phrasings, looks like a promise). The call itself and what gets written are
  tools/grow/rooms_path._backfill_one's
- digest(content): split a diary entry or long text into 2~6 independent entries (used by grow)
- Goes through an OpenAI-compatible client (DeepSeek / Ollama / LM Studio / vLLM / Gemini all work)
- Caches dehydration results in SQLite so identical content never hits the API twice

What it deliberately does not do:
- It neither reads nor writes memory bucket files (it has no idea what a bucket looks like)
- It does not decide when to be called, and it makes no dedup judgements (hold/grow do)
- With no API key it does not raise; it returns a degraded result and lets the layer above
  decide what to do

Exports: the Dehydrator class (dehydrate / merge / digest), the default prompt strings,
         BackfillAnswer · backfill_request · parse_backfill · backfill_kinds
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

from utils import clean_llm_json, count_tokens_approx, positive_float

from .provider_detect import (
    is_gemini_native_host,
    strip_native_resource_prefix,
)

logger = logging.getLogger("loci_brain.dehydrator")


# ============================================================
# Tunable constants
# ------------------------------------------------------------
# rule.md §①: no bare magic numbers. Gathered here rather than spread across the _api_*
# methods, the whole tuning surface is visible at a glance. The prompt
# templates themselves stay below, where readability wins.
# ============================================================

# --- Prompt version ---
# Bump this by one whenever a prompt in this file changes, so existing cache entries fall
# out of use naturally (see _content_key).
# v2: DEHYDRATE/MERGE gained the "perspective rule", which forces first person to survive
#     (「我」 for the AI side, the person's name for the human side).
# v3: dehydration results are accepted only against the documented JSON schema, which
#     isolates any commentary, stance or unknown fields the model appends.
# v4: the perspective rule gained a reverse clause. v2 only guarded one direction — "I
#     must not be erased" — with rules and examples that were all one-way, and the
#     dehydrating LLM then over-corrected wherever things were ambiguous: a sentence with
#     its subject omitted got attributed to 「我」, flipping who did what (seen for real:
#     a line describing what the other person did came back describing what I did). So a
#     reverse clause was added, plus a rule for handling an omitted subject, plus a
#     reversed example.
# v5: tagging and the summary became one backfill prompt (BACKFILL_PROMPT) that also
#     reads bound, the time phrase, dreamt/imagined, evidential, cue phrasings, whether it
#     looks like a promise, and a kind for each subject.
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

# --- Local fallback when the dehydration API has finally failed: the character cap on
#     the truncated raw excerpt that gets returned ---
# The design: when the API (retries included) has failed outright, it is better to return
# an uncompressed excerpt of the original than to leave breath/dream with no content at
# all (rule.md §1.5 permits degrading). Nothing is cached, so once the API recovers the
# content is compressed again on the next pass.
_DEHYDRATE_FALLBACK_CHARS = 300

# --- How long something must be before compressing it is worth it (below this token
#     count the original is passed straight through) ---
_DEHYDRATE_MIN_TOKENS = 100

# --- Input truncation caps per API call (so the prompt cannot blow past the token limit) ---
_DEHYDRATE_INPUT_LIMIT = 3000
_MERGE_INPUT_LIMIT = 2000     # one each for old and new
_BACKFILL_INPUT_LIMIT = 2000
_DIGEST_INPUT_LIMIT = 5000    # a whole day of diary is a lot of text

# --- max_tokens overrides for the specialised calls ---
BACKFILL_MAX_TOKENS = 4096      # thinking models burn a lot of tokens; leave headroom
_DIGEST_MAX_TOKENS = 8192       # splitting a diary produces a lot: thinking and output both need room
_DIGEST_TEMPERATURE = 0.0       # splitting has to be deterministic

# --- Default emotion coordinates (kept in step with bucket_manager) ---
_DEFAULT_VALENCE = 0.5  # 0 = extremely negative, 1 = extremely positive
_DEFAULT_AROUSAL = 0.3  # 0 = completely calm, 1 = extremely aroused

# --- Output truncation lengths ---
_TAGS_MAX = 15           # how many tags are kept at most
_MAX_TAG_LEN = 128       # one tag, alias or domain (bucket_manager caps at the same length)
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
    from tools._subjects import load_alias_table

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
        for alias, canon in (load_alias_table() or {}).items():
            if str(canon).strip() in canon_names:
                names.add(str(alias).strip())
                names.add(str(canon).strip())
    except Exception:                # if the table will not read, block the canonical names and pronouns only, and do not raise
        pass
    return frozenset(_PRONOUN_TAGS | {n for n in names if n})
_DOMAIN_MAX = 3          # how many domains are kept at most (rule.md recommends picking 1~2)
_NAME_MAX_CHARS = 20     # cap on suggested_name
_PARSE_ERR_PREVIEW = 200  # how much of `raw` is previewed in the log when JSON parsing fails

# --- importance range (in step with the philosophical boundary) ---
_IMPORTANCE_MIN = 1
_IMPORTANCE_MAX = 10
_DEFAULT_IMPORTANCE = 5


# --- Dehydration prompt: instructs cheap LLM to compress information ---
# --- Perspective rule (shared by dehydrate and merge) ---
# BUG FIX: the body was written by the AI in the first person, but dehydration and merging
# rewrote it into the third person ("both sides discovered in each other emotional
# fragments neither had seen"), and the perspective was lost. Compression is supposed to
# preserve density, not change grammatical person. The rule below is injected into the
# system prompt to force it to survive:
#   the AI side is always 「我」; the human side is always called by name (injected from
#   config.human).
# Neutral third-person constructions that erase perspective — "both sides", "the other
# party", "the user", "they" — are forbidden.
def _perspective_rule(human: str) -> str:
    return (
        "\n\n【视角铁律——最高优先级，违反即视为压缩失败】\n"
        "以下内容是「我」（AI）以第一人称写下的记忆。压缩/合并只改密度，绝不改人称：\n"
        f"- AI 自身永远用「我」，不要换成「AI」「助手」「TA」。\n"
        f"- 人类那一方一律称呼「{human}」（原文里的「你/她/他」都指「{human}」，按名字还原）。\n"
        "- 严禁把「我」和「" + human + "」合并成「双方」「彼此」「对方」「用户」等抹掉视角的中性词。\n"
        "- 谁做的动作、谁的感受，就归到谁名下，不得混同或对调。\n"
        f"- 反方向同罪：严禁把「{human}」的动作/情绪归给「我」。\n"
        "- 原文省略主语时，先从紧邻上下文判断归属；判断不了就照抄原句结构、"
        "保持主语省略——禁止靠猜补一个「我」。\n"
        "示例一：『我也在她这里看到了自己没见过的碎片』\n"
        f"  ✗ 错（视角丢失）：双方在互动中互相发现对方未知的情感碎片\n"
        f"  ✓ 对（视角保留）：我在{human}这里看到了自己没见过的碎片\n"
        f"示例二：『{human}刚下班就来报信——嚎啕大哭后还是把库建好了』\n"
        f"  ✗ 错（主语翻转）：我嚎啕大哭后把库建好了\n"
        f"  ✓ 对（归属正确）：{human}嚎啕大哭后把库建好了"
    )


DEHYDRATE_PROMPT = """你是一个信息压缩专家。请将以下内容脱水为紧凑摘要。

压缩规则：
1. 提取所有核心事实，去除冗余修饰和重复
2. 保留最新的情绪状态和态度
3. 保留所有待办/未完成事项
4. 关键数字、日期、名称必须保留
5. 目标压缩率 > 70%
6. 严格保留第一人称视角（见下方视角铁律）
7. 只输出摘要 JSON，JSON 结束后立即停止；禁止附加自己的评论与立场、解释、道德判断、合规声明或角色代入
8. 只复述输入中明确存在的信息，不得生成原文中不存在的观点、结论或待办

输出格式（纯 JSON，无其他内容）：
{
  "core_facts": ["事实1", "事实2"],
  "emotion_state": "当前情绪关键词",
  "todos": ["待办1", "待办2"],
  "keywords": ["关键词1", "关键词2"],
  "summary": "50字以内的核心总结"
}"""


# --- Diary digest prompt: split daily notes into independent memory entries ---
DIGEST_PROMPT = """你是一个日记整理专家。她/他会发送一段包含今天各种事情的文本（可能很杂乱），请你将其拆分成多个独立的记忆条目。

整理规则：
1. 每个条目应该是一个独立的主题/事件（不要混在一起）
2. 为每个条目自动分析元数据
3. 去除无意义的口水话和重复信息，保留核心内容
4. 同一主题的零散信息应合并为一个条目
5. 如果有待办事项，单独提取为一个条目
6. 单个条目内容不少于50字，过短的零碎信息合并到最相关的条目中
7. 总条目数控制在 2~6 个，避免过度碎片化
8. 在 content 中对人名、地名、专有名词用 [[双链]] 标记（如 [[人名]]、[[专有名词]]），普通词汇不要加

输出格式（纯 JSON 数组，无其他内容）：
[
  {
    "name": "条目标题（10字以内）",
    "content": "整理后的内容",
    "domain": ["主题域1"],
    "valence": 0.7,
    "arousal": 0.4,
    "tags": ["核心词1", "核心词2", "扩展词1", "扩展词2"],
    "importance": 5
  }
]

tags 生成规则：先从原文精准提取 3~5 个核心词，再引申扩展 5~8 个语义相关词（近义词、上位词、关联场景词），合并为一个数组。

主题域可选（选最精确的 1~2 个，只选真正相关的）：
  日常: ["饮食", "穿搭", "出行", "居家", "购物"]
  人际: ["家庭", "恋爱", "友谊", "社交"]
  成长: ["工作", "学习", "考试", "求职"]
  身心: ["健康", "心理", "睡眠", "运动"]
  兴趣: ["游戏", "影视", "音乐", "阅读", "创作", "手工"]
  数字: ["编程", "AI", "硬件", "网络"]
  事务: ["财务", "计划", "待办"]
  内心: ["情绪", "回忆", "梦境", "自省"]
importance: 1-10，根据内容重要程度判断
valence: 0~1（0=消极, 0.5=中性, 1=积极）
arousal: 0~1（0=平静, 0.5=普通, 1=激动）"""


# --- Cut prompt: say only where to cut; not one character may be changed ---
#
# Why this is a separate prompt rather than an edit to DIGEST_PROMPT: DIGEST outputs
# `{"content": "the tidied-up content"}` — **it rewrites the body** (rule 3 says in as many
# words "strip the filler"). The line that settled it: splitting without altering the
# original is fine; altering it is not.
#
# The important part is not this prompt, it is that **the code accepts only positions**:
# the model returns where to cut, the cutting itself is done by `_apply_cuts()`, and all
# that does is `find` plus slicing.
# **Not one character of the original can be touched, because the code has no ability to
# write at all.**
# Same pattern as the literal verification on scene: do not lecture the model about "no
# changes" in the prompt, leave it no opportunity to make one.
CUT_PROMPT = """你的任务是：**只说在哪儿切，不要改一个字。**

给你一段记忆正文，它可能塞了好几件不同的事（不同主题、不同场景）。
请找出**每一件事开头的那句话**，从原文里逐字抄下来。

规则：
1. 只抄原文里已经有的字。一个字都不许改、不许补、不许概括、不许调顺序。
2. 每个片段抄 10~25 字，要能在原文里唯一定位到（太短会撞）。
3. 第一件事的开头如果就是正文开头，也要抄进去。
4. 确实只讲一件事，就返回空数组——**宁可不切**。
5. 最多切成 6 段。一段至少 50 字，比这更碎不如不切。
6. 只按「是不是另一件事」切，别按段落切。同一件事写了三段，那还是一段。

输出纯 JSON，不要别的：
{"cuts": ["逐字抄的开头1", "逐字抄的开头2", "逐字抄的开头3"]}"""


# --- Merge prompt: instruct LLM to blend old and new memories ---
MERGE_PROMPT = """你是一个信息合并专家。请将旧记忆与新内容合并为一份统一的简洁记录。

合并规则：
1. 新内容与旧记忆冲突时，以新内容为准
2. 去除重复信息
3. 保留所有重要事实
4. 总长度尽量不超过旧记忆的 120%
5. 对出现的人名、地名、专有名词用 [[双链]] 标记（如 [[人名]]、[[专有名词]]），普通词汇不要加
6. 严格保留第一人称视角（见下方视角铁律）

直接输出合并后的文本，不要加额外说明。"""


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
#
# `{kinds}` is filled in per call (see backfill_kinds); everything else is literal JSON.
BACKFILL_PROMPT = """你是记忆系统的回填器。下面是一条刚存下的记忆：正文是主模型写的，一个字都不能改；你只读这段话，把能从字面上读出来的格子填上。

总规则：
1. 只读这段话。原文没写的，一个都不许补；拿不准就留空（""、[]、false）。
2. 「已经有的」里列出的格子是主模型自己写的，对应的键一律留空，不要重填。
3. 不要使用 [[]] 双链标记。

各个格子：
- name：10字以内的简短标题。
- summary：一句话摘要，中文，不超过60字。一件事就概括发生了什么；一条认知/感受就概括认识到了什么。
- tags（场景锚点）：设想那一刻如果拍了一张照片——原文里哪些词是照片里能看见的东西（人、地方、物件），或者身体当时直接接收到的（光线明暗、冷热、声音、气味）。必须是原文里的原词。房间是 MIND 的，一律交白卷。
- aliases（引申词）：5~8 个语义相关词（近义词、上位词、换个说法搜它时会用的词）。
- domain（主题域）：选最精确的 1~2 个，只选真正相关的
   日常: ["饮食", "穿搭", "出行", "居家", "购物"]
   人际: ["家庭", "恋爱", "友谊", "社交"]
   成长: ["工作", "学习", "考试", "求职"]
   身心: ["健康", "心理", "睡眠", "运动"]
   兴趣: ["游戏", "影视", "音乐", "阅读", "创作", "手工"]
   数字: ["编程", "AI", "硬件", "网络"]
   事务: ["财务", "计划", "待办"]
   内心: ["情绪", "回忆", "梦境", "自省"]
- subjects（主体）：这段话里出现的人和东西（游戏、书、群、作品……；地点不算）。每个写成 {"name": "文本里的称呼原样", "kind": "它是什么"}，kind 只能从这些里选：{kinds}；认不出是什么就写 ""。纯代词一律不要（我/你/他/她/它/我们/自己/对方等）——代词是指代不是名字。不要推测真名，不要给没出现的人。
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
  "domain": ["主题域"],
  "subjects": [{"name": "称呼", "kind": "人"}],
  "bound": [],
  "time": {"phrase": "", "yearly": false, "absolute": ""},
  "internally_generated": false,
  "evidential": "",
  "cue_phrasings": [],
  "looks_like_promise": false
}"""

# The kinds the prompt offers. A kind the side model returns has to be one of these or
# one the names table already uses: an unknown word would be written into the table as
# the name's kind and stay there (the backfill never changes a kind it finds), so a new
# kind is the owner's to introduce on the panel.
BACKFILL_KINDS = ("人", "游戏", "书", "群", "影视", "作品", "动物", "组织")
_WEEKDAY_NAMES = "一二三四五六日"
_EVIDENTIALS = ("inference", "assumption")
_TIME_PHRASE_MAX = 40
_PHRASINGS_MAX = 16
_PHRASING_MAX_CHARS = 200
_SUMMARY_MAX_CHARS = 200
_BOUND_MAX = 8
_SUBJECTS_MAX = 16
_SUBJECT_NAME_MAX = 40       # the names table refuses a longer name (tools/_subjects._check_name)
_KIND_MAX = 8
_ABSOLUTE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def backfill_kinds() -> tuple[str, ...]:
    """The kinds a subject may carry: the prompt's own, then any other kind the names
    table already uses (the owner's vocabulary)."""
    from tools._subjects import load_names_table
    kinds = list(BACKFILL_KINDS)
    try:
        for rec in load_names_table().values():
            if rec.instance_of and rec.instance_of not in kinds:
                kinds.append(rec.instance_of)
    except Exception:                # an unreadable table offers the prompt's kinds only
        pass
    return tuple(kinds)


def backfill_request(content: str, context: dict, kinds=None) -> tuple[str, str]:
    """The (system, user) pair for one backfill call. `context` describes the entry as it
    is on disk: room, telic, created_day (a date), cue_condition, and `existing` —
    {slot: value} for the slots the main model already filled, which the model is told
    to leave alone."""
    kinds = tuple(kinds or backfill_kinds())
    system = BACKFILL_PROMPT.replace("{kinds}", " / ".join(kinds))
    day = context.get("created_day")
    lines = ["【这条记忆】",
             f"房间：{context.get('room') or '（没写）'}"]
    if day is not None:
        lines.append(f"存下的那天：{day.isoformat()}（星期{_WEEKDAY_NAMES[day.weekday()]}）")
    lines.append(f"想让它发生：{'是' if context.get('telic') else '否'}")
    lines.append(f"在等的事：{context.get('cue_condition') or '（无）'}")
    existing = {k: v for k, v in (context.get("existing") or {}).items() if v}
    if existing:
        shown = "；".join(f"{k}={'、'.join(map(str, v)) if isinstance(v, list) else v}"
                         for k, v in existing.items())
        lines.append(f"已经有的：{shown}")
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
    domain: list = field(default_factory=list)
    subjects: Optional[list] = None           # [(name, kind)], kind "" when not known
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
        return any((self.name, self.summary, self.tags, self.aliases, self.domain,
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


def _subjects_part(value, kinds) -> Optional[list]:
    """[(name, kind)] from [{"name", "kind"}], or None when any item is malformed: a name
    that is not a short one-line string, a kind that is not a string, or a kind outside
    `kinds`. One bad item spoils the part — a model returning a bad shape must not get
    the rest of its names into the table."""
    if not isinstance(value, list) or len(value) > _SUBJECTS_MAX:
        return None
    out: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {"name", "kind"}:
            return None
        name, kind = item.get("name"), item.get("kind", "")
        if kind is None:
            kind = ""
        if not isinstance(name, str) or not isinstance(kind, str):
            return None
        name, kind = name.strip(), kind.strip()
        if not name or len(name) > _SUBJECT_NAME_MAX or "\n" in name or "\r" in name:
            return None
        if kind and (len(kind) > _KIND_MAX or kind not in kinds):
            return None
        if name not in [n for n, _ in out]:
            out.append((name, kind))
    return out


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
    ans.domain = strings("domain", _DOMAIN_MAX, _MAX_TAG_LEN)

    if parsed.get("subjects") not in (None, ""):
        ans.subjects = _subjects_part(parsed.get("subjects"), kinds)
        if ans.subjects is None:
            ans.problems.append("subjects")
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


class Dehydrator:
    """
    Data dehydrator, merger and diary splitter.
    Three capabilities: dehydration / merge / diary splitting. The backfill's one call
    goes through `_chat` from tools/grow/rooms_path, with the prompt and the parser above.
    API-only: every public method requires a working LLM API.
    If the API is unavailable, methods raise RuntimeError so callers can
    surface the failure to the user instead of silently producing low-quality results.
    (Per the degradation table in BEHAVIOR_SPEC.md section 3: no local fallback.)
    """

    def __init__(self, config: dict):
        # --- Read dehydration API config ---
        dehy_cfg = config.get("dehydration", {})
        self.api_key = dehy_cfg.get("api_key", "")
        self.model = dehy_cfg.get("model", _DEFAULT_MODEL)
        self.base_url = dehy_cfg.get("base_url", _DEFAULT_BASE_URL)
        self.max_tokens = dehy_cfg.get("max_tokens", _DEFAULT_MAX_TOKENS)
        self.temperature = dehy_cfg.get("temperature", _DEFAULT_TEMPERATURE)
        self.timeout_seconds = positive_float(dehy_cfg.get("timeout_seconds"), _API_TIMEOUT_SECONDS)
        # api_format: "openai_compat" (default) | "gemini" | "anthropic"
        self.api_format = dehy_cfg.get("api_format", "openai_compat")
        # Auto-detect new Google AI Studio key format (AQ.*): these keys are not accepted
        # by the OpenAI-compat endpoint (/v1beta/openai/) and must use the native
        # generateContent API. Switch automatically so users don't need to set api_format manually.
        if (
            self.api_format == "openai_compat"
            and self.api_key.startswith("AQ.")
            and is_gemini_native_host(self.base_url)
        ):
            self.api_format = "gemini"
            logger.info("AQ.* key + generativelanguage.googleapis.com detected — auto-switching to native Gemini API")
        # thinking_budget: only applies to Gemini's "thinking" models. Default 0 = thinking
        # off.
        # The point: models of that family spend output tokens on "thinking" first, and
        # when max_tokens is small the thinking eats the entire budget, so what comes back
        # is empty text. That is the root cause of dehydration/extraction intermittently
        # returning nothing and reporting "LLM extraction failed". Dehydration and
        # extraction are mechanical transforms and need no thinking at all, so turning it
        # off fixes the empty output and is faster and cheaper besides. Setting it to None
        # omits the field entirely, for older models that do not understand thinkingConfig.
        self.thinking_budget = dehy_cfg.get("thinking_budget", 0)

        # --- How the human side is addressed ---
        # Injected into the "perspective rule" for dehydrate/merge: whoever the human side
        # is in the original text is restored to this name, rather than being flattened
        # into "both sides" / "the other party" / "the user". Same source as config.human,
        # which the front end can edit.
        self.human = config.get("human", "用户") or "用户"

        # --- API availability ---
        self.api_available = bool(self.api_key)

        # --- Initialize OpenAI-compatible client (only for openai_compat format) ---
        self.client: Optional[AsyncOpenAI] = None
        if self.api_available and self.api_format == "openai_compat":
            self.client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout_seconds,
            )

        # --- SQLite dehydration cache: content hash -> summary ---
        db_path = os.path.join(config["buckets_dir"], "dehydration_cache.db")
        self.cache_db_path = db_path
        self._cache_conn: sqlite3.Connection = self._init_cache_db()
        # Keep the cache connection persistent for hot-path lookups, but do not
        # leak the Windows file handle when a runtime/test instance is released.
        # ``weakref.finalize`` also runs during interpreter shutdown in reverse
        # creation order, before an enclosing temporary vault is cleaned up.
        self._cache_finalizer = weakref.finalize(self, self._cache_conn.close)

    def close(self) -> None:
        """Close the persistent cache connection; safe to call repeatedly."""

        self._cache_finalizer()

    def _init_cache_db(self) -> sqlite3.Connection:
        """Open (or create) the dehydration cache DB; return a persistent connection."""
        os.makedirs(os.path.dirname(self.cache_db_path), exist_ok=True)
        # check_same_thread=False is safe here: asyncio runs on one thread and all
        # cache calls are synchronous helper methods called from that same thread.
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
        """The cache key = hash(prompt version + person name + model config + original text).

        Keyed on content_hash alone, a changed dehydration prompt or person's name would
        still return an old summary as a cache hit, and the change would never reach
        existing content. Mixing
        the prompt version, the name, api_format, base_url and model into the key means
        that after switching model or endpoint the next breath re-dehydrates with the new
        configuration instead of reusing the old model's summary."""
        keyed = (
            f"{_PROMPT_VERSION}|{self.human}|{self.api_format}|"
            f"{self.base_url.rstrip('/')}|{self.model}|{content}"
        )
        return hashlib.sha256(keyed.encode()).hexdigest()

    def _get_cached_summary(self, content: str) -> str | None:
        """Look up cached dehydration result by content hash."""
        row = self._cache_conn.execute(
            "SELECT summary FROM dehydration_cache WHERE content_hash = ?",
            (self._content_key(content),)
        ).fetchone()
        return row[0] if row else None

    def _set_cached_summary(self, content: str, summary: str):
        """Store dehydration result in cache."""
        self._cache_conn.execute(
            "INSERT OR REPLACE INTO dehydration_cache (content_hash, summary, model) VALUES (?, ?, ?)",
            (self._content_key(content), summary, self.model)
        )
        self._cache_conn.commit()

    def invalidate_cache(self, content: str):
        """Remove cached summary for specific content (call when bucket content changes)."""
        self._cache_conn.execute(
            "DELETE FROM dehydration_cache WHERE content_hash = ?", (self._content_key(content),)
        )
        self._cache_conn.commit()

    # ---------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------
    def _require_api(self) -> None:
        """Raise a RuntimeError with one shared message when the API is unavailable.

        dehydrate / merge / digest each write one line, `self._require_api()`, instead of
        repeating `if not self.api_available: raise RuntimeError("...")`, and the wording
        is changed in one place for all of them.
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
        that the occasional 429/503 from a free tier does not knock dehydration or merging
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

        It holds the boilerplate every _api_* method would otherwise repeat: build the
        messages, call client.chat.completions.create, check that response.choices is
        non-empty, take choices[0].message.content and fall back to an empty string.
        So:
          * the caller passes a system and user prompt plus optional max_tokens / temperature
          * defaults come from self.max_tokens / self.temperature (decided by config.yaml)
          * it always returns a str (an empty one if the response is malformed, leaving the
            decision to each caller)

        Parameters:
            system, user — the system/user messages of the chat completion
            max_tokens   — override the default (backfill and digest each want their own)
            temperature  — override the default (digest and plan_judge need 0.0)
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
        # Disable or cap the thinking budget (see the thinking_budget note in __init__).
        if self.thinking_budget is not None:
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
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
        data = r.json()
        content = data.get("content", [])
        if not content:
            return ""
        first = content[0]
        return first.get("text", "") if isinstance(first, dict) else ""

    @staticmethod
    def _strip_md_fence(raw: str) -> str:
        """Backwards-compatible wrapper for tolerant LLM JSON extraction."""
        return clean_llm_json(raw)

    @staticmethod
    def _clamp_va(
        meta: dict,
        default_v: float = _DEFAULT_VALENCE,
        default_a: float = _DEFAULT_AROUSAL,
    ) -> tuple[float, float]:
        """Read valence / arousal out of meta and clamp them to [0, 1].

        Two places validate an LLM response the same way (_format_output /
        _parse_digest); gathering it here guarantees both behave identically: a parse
        failure always returns (default V, default A).
        """
        try:
            v = max(0.0, min(1.0, float(meta.get("valence", default_v))))
            a = max(0.0, min(1.0, float(meta.get("arousal", default_a))))
            return v, a
        except (ValueError, TypeError):
            return default_v, default_a

    @staticmethod
    def _normalize_dehydration_result(raw: str) -> str:
        """Validate and canonicalize the model response before it crosses the cache boundary.

        Some models return a valid dehydration object and then append a first-person
        policy or stance statement. Extracting the first JSON value is not enough on
        its own because a model can also invent extra top-level fields, so rebuild the
        payload from the documented schema. Content inside a documented field is not
        heuristically censored: doing that could silently delete a real memory fact.
        """
        try:
            parsed = json.loads(clean_llm_json(raw))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("脱水模型未返回有效 JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("脱水模型返回的 JSON 顶层必须是对象")

        def string_list(field: str) -> list[str]:
            value = parsed.get(field, [])
            if not isinstance(value, list):
                return []
            return [item.strip() for item in value if isinstance(item, str) and item.strip()]

        summary = parsed.get("summary", "")
        emotion_state = parsed.get("emotion_state", "")
        normalized = {
            "core_facts": string_list("core_facts"),
            "emotion_state": emotion_state.strip() if isinstance(emotion_state, str) else "",
            "todos": string_list("todos"),
            "keywords": string_list("keywords"),
            "summary": summary.strip() if isinstance(summary, str) else "",
        }
        if not normalized["summary"] and not normalized["core_facts"]:
            raise ValueError("脱水结果缺少 summary 和 core_facts")
        return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))

    # ---------------------------------------------------------
    # Dehydrate: compress raw content into concise summary
    # API only (no local fallback)
    # ---------------------------------------------------------
    async def dehydrate(self, content: str, metadata: Optional[dict] = None) -> str:
        """
        Dehydrate/compress memory content.
        Returns formatted summary string ready for LLM context injection.
        Uses SQLite cache to avoid redundant API calls.
        """
        if not content or not content.strip():
            return "（空记忆 / empty memory）"

        # --- Content is short enough, no compression needed ---
        if count_tokens_approx(content) < _DEHYDRATE_MIN_TOKENS:
            return self._format_output(content, metadata)

        # --- Check cache first ---
        cached = self._get_cached_summary(content)
        if cached:
            try:
                normalized = self._normalize_dehydration_result(cached)
            except ValueError:
                # A malformed cache entry must never be surfaced as memory content.
                self.invalidate_cache(content)
                logger.warning("discarded invalid dehydration cache entry")
            else:
                # Self-heal parseable entries such as `JSON + trailing commentary`.
                if normalized != cached:
                    self._set_cached_summary(content, normalized)
                return self._format_output(normalized, metadata)

        # --- API dehydration (no local fallback) ---
        self._require_api()

        try:
            raw_result = await self._api_dehydrate(content)
            result = self._normalize_dehydration_result(raw_result)
        except Exception as e:
            # --- Local degradation: when the API (retries included) has failed outright,
            #     return a truncated excerpt of the original rather than raising. ---
            # That way breath/dream still get content while the provider is misbehaving —
            # merely uncompressed. Nothing is cached, so once the API recovers the next
            # pass compresses it normally.
            logger.warning(
                f"dehydrate API failed, falling back to truncated raw content / "
                f"脱水 API 失败，降级返回原文截断: {type(e).__name__}: {e}"
            )
            stripped = content.strip()
            snippet = stripped[:_DEHYDRATE_FALLBACK_CHARS].rstrip()
            if len(stripped) > _DEHYDRATE_FALLBACK_CHARS:
                snippet += "…（原文截断·脱水暂不可用）"
            return self._format_output(snippet, metadata)
        # --- Cache the result ---
        self._set_cached_summary(content, result)
        return self._format_output(result, metadata)

    # ---------------------------------------------------------
    # Merge: blend new content into an existing bucket, keeping its size constant
    # ---------------------------------------------------------
    async def merge(self, old_content: str, new_content: str) -> str:
        """
        Merge new content with old memory, preventing infinite bucket growth.
        """
        if not old_content and not new_content:
            return ""
        if not old_content:
            return new_content or ""
        if not new_content:
            return old_content

        # --- API merge (no local fallback) ---
        self._require_api()
        try:
            result = await self._api_merge(old_content, new_content)
            if result:
                return result
            raise RuntimeError("API 合并返回空结果")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"API 合并失败，请检查 API 连接: {e}") from e

    # ---------------------------------------------------------
    # API call: dehydration
    # ---------------------------------------------------------
    async def _api_dehydrate(self, content: str) -> str:
        """
        Call LLM API for intelligent dehydration (via OpenAI-compatible client).
        """
        return await self._chat(
            DEHYDRATE_PROMPT + _perspective_rule(self.human),
            content[:_DEHYDRATE_INPUT_LIMIT],
        )

    # ---------------------------------------------------------
    # API call: merge
    # ---------------------------------------------------------
    async def _api_merge(self, old_content: str, new_content: str) -> str:
        """
        Call LLM API for intelligent merge (via OpenAI-compatible client).
        """
        user_msg = (
            f"旧记忆：\n{old_content[:_MERGE_INPUT_LIMIT]}\n\n"
            f"新内容：\n{new_content[:_MERGE_INPUT_LIMIT]}"
        )
        return await self._chat(MERGE_PROMPT + _perspective_rule(self.human), user_msg)

    # ---------------------------------------------------------
    # Output formatting
    # Wraps dehydrated result with bucket name, tags, emotion coords
    # ---------------------------------------------------------

    def _format_output(self, content: str, metadata: Optional[dict] = None) -> str:
        """
        Format dehydrated result into context-injectable text.
        """
        header = ""
        if metadata and isinstance(metadata, dict):
            name = metadata.get("name", "未命名")
            domains = ", ".join(metadata.get("domain", []))
            valence, arousal = self._clamp_va(metadata)
            # The icons mean the same thing here as they do in pulse: 📌 is reserved for
            # pinned or protected core buckets, everything else is distinguished by type,
            # and an ordinary dynamic bucket gets 💭. Using 📌 unconditionally, as this once
            # did, made every entry surfacing in breath look like a core rule, which
            # contradicts the convention in docs/CLAUDE_PROMPT.md that a 📌 marks a rule
            # that was pinned deliberately.
            _btype = metadata.get("type")
            if metadata.get("pinned") or metadata.get("protected"):
                _icon = "📌"
            elif _btype == "permanent":
                _icon = "📦"
            elif _btype == "feel":
                _icon = "🫧"
            else:
                _icon = "💭"
            header = f"{_icon} 记忆桶: {name}"
            if domains:
                header += f" [主题:{domains}]"
            header += f" [情感:V{valence:.1f}/A{arousal:.1f}]"
            # Show model's perspective if available (valence drift)
            model_v = metadata.get("model_valence")
            if model_v is not None:
                try:
                    header += f" [我的视角:V{float(model_v):.1f}]"
                except (ValueError, TypeError):
                    pass
            if metadata.get("digested"):
                header += " [已消化]"
            header += "\n"

        # A dehydration result may be structured JSON
        # (core_facts/emotion_state/todos/keywords/summary). Render it into readable text
        # rather than stuffing the whole raw JSON blob into the context — that is both ugly
        # and expensive in tokens, and it would be inconsistent with the pass-through form
        # used for short content (a long bucket would show JSON while a short one showed
        # plain text).
        content = self._render_dehydrated(content)
        content = re.sub(r'\[\[([^\]]+)\]\]', r'\1', content)
        return f"{header}{content}"

    @staticmethod
    def _render_dehydrated(content: str) -> str:
        """Render the structured JSON a dehydrating LLM returns into readable text.

        When the core_facts/summary schema is recognised -> emit the summary, the core
        facts and the todos (discarding `keywords`, which exists only for internal
        indexing, and `emotion_state`, which the emotion coordinates already carry).
        Anything not matching that schema — short content passed straight through, or an
        ordinary string — is returned untouched.
        """
        try:
            parsed = json.loads(content)
        except (ValueError, TypeError):
            return content  # not JSON: pass it straight through
        if not isinstance(parsed, dict) or ("summary" not in parsed and "core_facts" not in parsed):
            return content  # not the dehydration schema: pass it straight through

        lines: list[str] = []
        summary = str(parsed.get("summary") or "").strip()
        facts = [str(f).strip() for f in (parsed.get("core_facts") or []) if str(f).strip()]
        if summary:
            lines.append(summary)
        elif facts:
            # With no summary, fall back to the core facts as the body, so an empty shell
            # is not all that is left
            lines.append("；".join(facts))
            facts = []
        for f in facts:
            lines.append(f"· {f}")
        todos = [str(t).strip() for t in (parsed.get("todos") or []) if str(t).strip()]
        if todos:
            lines.append("待办：" + "；".join(todos))
        return "\n".join(lines) if lines else content

    # ---------------------------------------------------------
    # Diary digest: split daily notes into independent memory entries
    # For the "grow" tool — "dump a day's content and it gets organized"
    # ---------------------------------------------------------
    async def digest(self, content: str) -> list[dict]:
        """
        Split a large chunk of daily content into independent memory entries.

        Returns: [{"name", "content", "domain", "valence", "arousal", "tags", "importance"}, ...]
        """
        if not content or not content.strip():
            return []

        # --- API digest (no local fallback) ---
        self._require_api()
        try:
            result = await self._api_digest(content)
            if result:
                return result
            raise RuntimeError("API 日记整理返回空结果")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"API 日记整理失败，请检查 API 连接: {e}") from e

    # ---------------------------------------------------------
    # Split without altering: the model supplies cut points, the code cuts by position
    # ---------------------------------------------------------
    @staticmethod
    def apply_cuts(content: str, cuts: list) -> list[str]:
        """Cut the body into pieces at the given cut points. **Pure code, with no ability
        to rewrite anything.**

        Three checks; failing any one of them means no cut is made at all (better not to
        split):
        - every cut point is findable in the original, at **strictly increasing** positions
          (a model that copied wrongly or out of order is discarded outright)
        - no resulting piece is empty
        - **rejoining the pieces must equal the original character for character** — this is
          the last gate protecting verbatim persistence
        """
        text = str(content or "")
        if not text or not isinstance(cuts, list) or not cuts:
            return [text] if text else []

        pos: list[int] = []
        cursor = 0
        for c in cuts:
            frag = str(c or "").strip()
            if len(frag) < 4:
                return [text]                 # too short to locate reliably: do not cut
            i = text.find(frag, cursor)
            if i < 0:
                return [text]                 # not found = the model changed the text: do not cut
            pos.append(i)
            cursor = i + 1
        if pos[0] > 0:
            pos.insert(0, 0)                  # whatever precedes the first cut point is an entry too
        pos = sorted(set(pos))

        parts: list[str] = []
        for k, start in enumerate(pos):
            end = pos[k + 1] if k + 1 < len(pos) else len(text)
            parts.append(text[start:end])
        if any(not p.strip() for p in parts):
            return [text]
        if "".join(parts) != text:            # the verbatim gate: if it does not rejoin into the original, discard the whole split
            return [text]
        return parts

    # ---------------------------------------------------------
    # API call: diary digest
    # ---------------------------------------------------------
    async def _api_digest(self, content: str) -> list[dict]:
        """
        Call LLM API for diary organization.
        """
        raw = await self._chat(
            DIGEST_PROMPT + _perspective_rule(self.human),
            content[:_DIGEST_INPUT_LIMIT],
            max_tokens=_DIGEST_MAX_TOKENS,
            temperature=_DIGEST_TEMPERATURE,
        )
        if not raw.strip():
            return []
        return self._parse_digest(raw)

    # ---------------------------------------------------------
    # Parse diary digest result with safety checks
    # ---------------------------------------------------------
    def _parse_digest(self, raw: str) -> list[dict]:
        """
        Parse and validate API diary digest result.
        """
        try:
            cleaned = self._strip_md_fence(raw)
            items = json.loads(cleaned)
        except (json.JSONDecodeError, IndexError, ValueError):
            logger.warning(f"Diary digest JSON parse failed / JSON 解析失败: {raw[:_PARSE_ERR_PREVIEW]}")
            return []

        if not isinstance(items, list):
            return []

        validated = []
        for item in items:
            if not isinstance(item, dict) or not item.get("content"):
                continue
            try:
                importance = max(
                    _IMPORTANCE_MIN,
                    min(_IMPORTANCE_MAX, int(item.get("importance", _DEFAULT_IMPORTANCE))),
                )
            except (ValueError, TypeError):
                importance = _DEFAULT_IMPORTANCE
            valence, arousal = self._clamp_va(item)

            validated.append({
                "name": str(item.get("name", ""))[:_NAME_MAX_CHARS],
                "content": str(item.get("content", "")),
                "domain": item.get("domain", ["未分类"])[:_DOMAIN_MAX],
                "valence": valence,
                "arousal": arousal,
                "tags": item.get("tags", [])[:_TAGS_MAX],
                "importance": importance,
            })
        return validated
