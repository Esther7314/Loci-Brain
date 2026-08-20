"""
========================================
dehydrator.py — the LLM calls: dehydrate / merge / tag / split
========================================

This file wraps every prompt and every call that goes to an external LLM. tools/hold,
tools/grow, tools/dream and the rest all go through it whenever a model has to understand
some content; none of them assembles a prompt itself.

Key behaviours:
- dehydrate(content): compress long content into a dense summary and save tokens
- merge(old, new): blend new content into old while keeping bucket size roughly constant
- analyze(content, for_mind=False): returns {domain, valence, arousal, tags, aliases,
  suggested_name} (tags = the scene anchors, verified to appear literally in the body;
  aliases = expansion words that only feed bm25; for_mind skips scene extraction)
- digest(content): split a diary entry or long text into 2~6 independent entries (used by grow)
- Goes through an OpenAI-compatible client (DeepSeek / Ollama / LM Studio / vLLM / Gemini all work)
- Caches dehydration results in SQLite so identical content never hits the API twice

What it deliberately does not do:
- It neither reads nor writes memory bucket files (it has no idea what a bucket looks like)
- It does not decide when to be called, and it makes no dedup judgements (hold/grow do)
- With no API key it does not raise; it returns a degraded result and lets the layer above
  decide what to do

Exports: the Dehydrator class (dehydrate / merge / analyze / digest) and the default
prompt strings
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
from typing import Optional

from openai import AsyncOpenAI

from utils import clean_llm_json, count_tokens_approx, parse_bool, positive_float
from tools._subjects import normalize_subjects

from locibrain.integrations.provider_detect import (
    is_gemini_native_host,
    strip_native_resource_prefix,
)

logger = logging.getLogger("loci_brain.dehydrator")


# ============================================================
# Tunable constants
# ------------------------------------------------------------
# rule.md §①: no bare magic numbers. These used to be scattered across the five _api_*
# methods; gathered here, the whole tuning surface is visible at a glance. The prompt
# templates themselves stay below, where readability wins.
# ============================================================

# --- Dehydration cache version ---
# Bump this by one whenever a prompt that affects dehydrate/merge output changes, so
# existing cache entries fall out of use naturally (see _content_key).
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
_PROMPT_VERSION = 4

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
_ANALYZE_INPUT_LIMIT = 2000
_DIGEST_INPUT_LIMIT = 5000    # a whole day of diary is a lot of text
_PLAN_JUDGE_INPUT_LIMIT = 1500  # one each for the plan and the new event
_SAME_EVENT_INPUT_LIMIT = 1800  # one each for the old bucket and the new content

# --- max_tokens overrides for the specialised calls ---
_ANALYZE_MAX_TOKENS = 4096      # thinking models burn a lot of tokens; leave headroom
_DIGEST_MAX_TOKENS = 8192       # splitting a diary produces a lot: thinking and output both need room
_PLAN_JUDGE_MAX_TOKENS = 2048   # under a thinking model, 200 tokens is nowhere near enough
_PLAN_JUDGE_TEMPERATURE = 0.0   # a judgement has to be deterministic
_SAME_EVENT_MAX_TOKENS = 1024   # it only returns compact JSON
_SAME_EVENT_TEMPERATURE = 0.0   # deciding an event boundary has to be deterministic
_DIGEST_TEMPERATURE = 0.0       # splitting has to be deterministic

# --- Default emotion coordinates (kept in step with bucket_manager) ---
_DEFAULT_VALENCE = 0.5  # 0 = extremely negative, 1 = extremely positive
_DEFAULT_AROUSAL = 0.3  # 0 = completely calm, 1 = extremely aroused

# --- Output truncation lengths ---
_TAGS_MAX = 15           # how many tags are kept at most
# Tags that merely point at "the two people this store is about" — their names, and bare
# pronouns — never make it into tags. The reasoning is in the comments inside
# _parse_analysis.
# 🔴 The names themselves were **moved out of the code.** They used to be hard-coded here,
#    which amounts to publishing living people's names; and anyone cloning this repo would
#    have inherited a filter for someone else's household, which is useless to them.
#    The names now come from `AI_NAME` / `LOCI_OWNER_NAME` and the `people:` section of
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
_PLAN_REASON_MAX = 200   # cap on the reason from a plan judgement
_SAME_EVENT_REASON_MAX = 200  # cap on the reason from a merge-boundary judgement
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


# --- Auto-tagging prompt: analyze content for domain and emotion coords ---
# The three tag groups were re-divided, cutting straight to the root:
#   core   -> 🗑️ cut. What it extracted were words already in the body, and both the bm25
#            index and literal matching already cover the body, so its contribution to
#            search was zero. The one effect it did have was precisely the unwanted one:
#            when a collapsed row picks tags by frequency, the abstract core words win
#            every time, and that row ends up reading like a list of category names.
#   scene  -> ✅ kept, and tags now consist of nothing else (the literal check guarantees
#            they really do appear in the body).
#   expand -> moved to the new `aliases` field: it feeds bm25 only, never enters the
#            vectors, and never shows up in a collapsed row or in chips.
ANALYZE_PROMPT = """你是一个内容分析器。请分析以下文本，输出结构化的元数据。

分析规则：
1. domain（主题域）：选最精确的 1~2 个，只选真正相关的
   日常: ["饮食", "穿搭", "出行", "居家", "购物"]
   人际: ["家庭", "恋爱", "友谊", "社交"]
   成长: ["工作", "学习", "考试", "求职"]
   身心: ["健康", "心理", "睡眠", "运动"]
   兴趣: ["游戏", "影视", "音乐", "阅读", "创作", "手工"]
   数字: ["编程", "AI", "硬件", "网络"]
   事务: ["财务", "计划", "待办"]
   内心: ["情绪", "回忆", "梦境", "自省"]
2. valence（情感效价）：0.0~1.0，0=极度消极 → 0.5=中性 → 1.0=极度积极
3. arousal（情感唤醒度）：0.0~1.0，0=非常平静 → 0.5=普通 → 1.0=非常激动
4. 关键词分两组，各自成数组：
   scene（场景锚点）：设想那一刻如果拍了一张照片——原文里哪些词，是这张
     照片里能看见的东西，例如：人、地方、物件等等；或者当时身体直接能
     接收到的，例如光线明暗、冷热、声音、气味等。
     ⚠️ 原文里没写的，一个都不许补。宁可这一步交白卷（空数组）。
   expand（引申词）：5~8 个语义相关词（近义词、上位词、可能用别的措辞搜索的词）
5. subjects（主体）：这段话里**出现了哪些人**，只列人，不列地点、物件、组织。
   用文本里对他们的**称呼原样列出**（「小王」「我哥」「老板」这类**名字和称谓**），
   ⚠️ **纯代词一律不要**（我/你/他/她/它/我们/自己/对方等）——代词是指代不是名字。
   不要推测真名，不要给没出现的人。没有人就交白卷（空数组）。
6. suggested_name（建议桶名）：10字以内的简短标题
7. 在关键词和 suggested_name 中不要使用 [[]] 双链标记

输出格式（纯 JSON，无其他内容）：
{
  "domain": ["主题域1", "主题域2"],
  "valence": 0.7,
  "arousal": 0.4,
  "scene": ["场景锚点1", "场景锚点2"],
  "expand": ["引申词1", "引申词2"],
  "subjects": ["称呼1", "称呼2"],
  "suggested_name": "简短标题"
}"""

# MIND only: there are no photographs inside an insight, so scene is not extracted —
# forcing a model to find "the picture" inside a piece of thinking only forces it to make
# one up. All that is wanted is expand, so a different phrasing still finds it.
ANALYZE_PROMPT_MIND = """你是一个内容分析器。下面是一段第一人称的认知/判断（不是事件叙述）。请输出结构化元数据。

分析规则：
1. domain（主题域）：选最精确的 1~2 个（可选：情绪/回忆/自省/心理/恋爱/工作/学习/编程/AI 等）
2. valence / arousal：0.0~1.0（仅供参考，调用方可能已自带）
3. expand（引申词）：5~8 个语义相关词（近义词、上位词、可能用别的措辞搜索的词）
4. subjects（主体）：这段话里**出现了哪些人**，只列人。用文本里对他们的称呼原样列出
   （名字和称谓；⚠️ **纯代词一律不要**：我/你/他/她/它/我们/自己/对方等），
   不要推测真名，不要给没出现的人。没有人就交白卷（空数组）。
5. suggested_name（建议桶名）：10字以内的简短标题
6. 不要使用 [[]] 双链标记

输出格式（纯 JSON，无其他内容）：
{
  "domain": ["主题域1"],
  "valence": 0.5,
  "arousal": 0.3,
  "expand": ["引申词1", "引申词2"],
  "subjects": ["称呼1", "称呼2"],
  "suggested_name": "简短标题"
}"""


class Dehydrator:
    """
    Data dehydrator + content analyzer.
    Three capabilities: dehydration / merge / auto-tagging (domain + emotion).
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

        The cache used to be keyed on content_hash alone, so once the dehydration prompt
        changed or the person's name changed, an old third-person summary still came back
        as a cache hit — meaning the perspective fix did not reach existing content. Mixing
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

        dehydrate / merge / analyze / digest each used to repeat
        `if not self.api_available: raise RuntimeError("...")`. With this, a caller writes
        one line, `self._require_api()`, and the wording is changed in one place for all of
        them.
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
    ) -> str:
        """The single chat entry point: exponential backoff retries on transient errors
        such as 429 / 5xx / timeouts.

        The actual single call lives in _chat_once; this only handles retry and backoff, so
        that the occasional 429/503 from a free tier does not knock dehydration or merging
        out cold (see the troubleshooting table in the README)."""
        last_exc: BaseException | None = None
        for attempt in range(_RETRY_MAX_ATTEMPTS):
            try:
                return await self._chat_once(
                    system, user, max_tokens=max_tokens, temperature=temperature
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
    ) -> str:
        """The single OpenAI-compatible chat call.

        Five _api_* methods used to repeat the same boilerplate:
          * build the messages
          * call client.chat.completions.create
          * check that response.choices is non-empty
          * take choices[0].message.content and fall back to an empty string
        Now:
          * the caller passes a system and user prompt plus optional max_tokens / temperature
          * defaults come from self.max_tokens / self.temperature (decided by config.yaml)
          * it always returns a str (an empty one if the response is malformed, leaving the
            decision to each caller)

        Parameters:
            system, user — the system/user messages of the chat completion
            max_tokens   — override the default (analyze and digest each want their own)
            temperature  — override the default (digest and plan_judge need 0.0)
        """
        if self.api_format == "gemini":
            return await self._chat_gemini(system, user, max_tokens=max_tokens, temperature=temperature)
        if self.api_format == "anthropic":
            return await self._chat_anthropic(system, user, max_tokens=max_tokens, temperature=temperature)
        if self.api_format != "openai_compat":
            logger.warning(f"Unknown api_format '{self.api_format}', falling back to openai_compat")
        # openai_compat (default)
        if self.client is None:
            return ""
        response = await self.client.chat.completions.create(
            model=self.model,
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
    ) -> str:
        """Native Gemini generateContent API call (no OpenAI-compat wrapper)."""
        if not self.api_key:
            return ""
        import httpx
        # Strip any accidental "models/" prefix — Google rejects double-prefix in the URL
        model_id = strip_native_resource_prefix(self.model)
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
            "model": self.model,
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

        Three places validate an LLM response the same way (_format_output /
        _parse_analysis / _parse_digest); gathering it here guarantees all three behave
        identically: a parse failure always returns (default V, default A).
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
            elif _btype == "plan":
                _icon = "📋"
            elif _btype == "letter":
                _icon = "💌"
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
    # Auto-tagging: analyze content for domain + emotion + tags
    # Called by server.py when storing new memories
    # ---------------------------------------------------------
    async def analyze(self, content: str, for_mind: bool = False) -> dict:
        """
        Analyze content and return structured metadata.

        for_mind=True: a MIND entry wants only aliases plus the summary, and no scene is
        extracted — there are no photographs inside an insight, so its tags come back
        empty. That is the design, not a defect.

        Returns: {"domain", "valence", "arousal", "tags", "aliases", "suggested_name"}
        """
        if not content or not content.strip():
            return self._default_analysis()

        # --- API analyze (no local fallback) ---
        self._require_api()
        try:
            result = await self._api_analyze(content, for_mind=for_mind)
            if result:
                return result
            raise RuntimeError("API 打标返回空结果")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"API 打标失败，请检查 API 连接: {e}") from e

    # ---------------------------------------------------------
    # API call: auto-tagging
    # ---------------------------------------------------------
    async def _api_analyze(self, content: str, for_mind: bool = False) -> dict:
        """
        Call LLM API for content analysis / tagging.
        """
        raw = await self._chat(
            ANALYZE_PROMPT_MIND if for_mind else ANALYZE_PROMPT,
            content[:_ANALYZE_INPUT_LIMIT],
            max_tokens=_ANALYZE_MAX_TOKENS,
            temperature=_DEFAULT_TEMPERATURE,
        )
        if not raw.strip():
            return self._default_analysis()
        return self._parse_analysis(raw, content)

    # ---------------------------------------------------------
    # Parse API JSON response with safety checks
    # Ensure valence/arousal in 0~1, domain/tags valid
    # ---------------------------------------------------------
    def _parse_analysis(self, raw: str, content: str = "") -> dict:
        """
        Parse and validate API tagging result.

        `content` is the original text, used to **verify literally** that each scene anchor
        really appears in it — see the comments below.
        """
        try:
            cleaned = self._strip_md_fence(raw)
            result = json.loads(cleaned)
        except (json.JSONDecodeError, IndexError, ValueError):
            logger.warning(f"API tagging JSON parse failed / JSON 解析失败: {raw[:_PARSE_ERR_PREVIEW]}")
            return self._default_analysis()

        if not isinstance(result, dict):
            return self._default_analysis()

        # --- Validate and clamp value ranges ---
        valence, arousal = self._clamp_va(result)

        # --- tags = scene only; expand moved out to aliases ---
        # Why (three groups were defined, then cut to two): upstream had only the single
        # `tags` dimension, so abstract distillations were moonlighting as a classifier.
        # Loci has `room`, so classification already has an owner, and tags can concentrate
        # on recording "what is inside this one" — a collapsed row then turns into picture
        # words by itself, with not one line of the ordering logic changed, and the raw
        # material for stringing pictures together comes for free.
        #
        # scene (the scene anchors): memory works like a series of photographs, and what
        # surfaces first on recall is what was in the picture (a table, an institution,
        # some materials, and so on).
        # ⚠️ A scene word must appear literally in the body: the model occasionally
        # substitutes a variant (turning a phrase into its near-opposite), measured at 4.3%.
        # Blocking that here in code is more reliable than repeating the instruction in the
        # prompt.
        #
        # aliases (formerly expand, the expansion words): they feed bm25 only, so a
        # different phrasing still finds it. They never enter the vectors and never appear
        # in a collapsed row or in chips — they are search's hidden rail, not a label meant
        # for human eyes.
        #
        # Tags that merely point at the two people this store is about never get in: the
        # entire store is about them, so their names, and the bare pronouns in
        # `_PRONOUN_TAGS`, carry no distinguishing power under any circumstances.
        # ⚠️ Only those two are blocked. Anyone else's name does distinguish, and is kept.
        # ⚠️ Blocked in code, not lectured about in the prompt — the model may output them
        # all it likes, they simply do not get in.
        _person_stop = _person_tags()      # recomputed every time: a config change needs no restart
        scene = [str(t) for t in (result.get("scene") or [])
                 if str(t).strip() and str(t) in content]
        tags = [t for t in scene if t not in _person_stop]
        aliases = [str(t) for t in (result.get("expand") or [])
                   if str(t).strip() and str(t) not in _person_stop]
        if not tags and not aliases:
            # Fallback: the model still returned pre-merged `tags` in the old format
            # (during a prompt transition, or because it did not comply)
            tags = [str(t) for t in (result.get("tags") or [])
                    if str(t).strip() and str(t) not in _person_stop]
        _seen: set[str] = set()
        tags = [t for t in tags if not (t in _seen or _seen.add(t))]
        _seen = set(tags)  # an alias duplicating a tag is pointless (bm25 already has it)
        aliases = [t for t in aliases if not (t in _seen or _seen.add(t))]

        # --- subjects: who. **A third, independent kind** — it enters neither tags nor aliases ---
        # What the model extracts are the **forms of address** used in the body (things like
        # 「我哥」 or 「老板」), and normalize_subjects then runs them through the alias table
        # to reach a canonical name. Normalising is a gate, not a convention: without it a
        # nickname and a full name split into two different subjects and retrieval breaks on
        # the spot.
        # ⚠️ There is deliberately **no** "must appear literally in the body" check here —
        #    that guarantee belongs to tags, whereas the entire job of a subject is to
        #    translate a form of address into the canonical name behind it. The two rules
        #    point in opposite directions; do not copy one into the other.
        subjects = normalize_subjects(result.get("subjects"))

        # 🔴 **A subject extracted from this record may not also appear in its tags or
        #    aliases.** "Who" already has a field of its own, and appearing once more as a
        #    label carries zero information.
        #    This rule **knows no names and holds for everyone** — unlike the stop-list
        #    above it needs no configuration, because it uses the people this very record
        #    extracted. So it is correct the moment anyone installs this, without filling in
        #    their own name first.
        #    (Both are kept: the stop-list blocks the two people who carry no distinguishing
        #     power in ANY entry, even one that did not extract them as subjects; this rule
        #     blocks whoever has already been named in THIS entry.)
        if subjects:
            _subj = {str(x).strip() for x in subjects if str(x).strip()}
            tags = [t for t in tags if t not in _subj]
            aliases = [t for t in aliases if t not in _subj]

        return {
            "domain": result.get("domain", ["未分类"])[:_DOMAIN_MAX],
            "valence": valence,
            "arousal": arousal,
            "tags": tags[:_TAGS_MAX],
            "aliases": aliases[:_TAGS_MAX],
            "subjects": subjects,
            "suggested_name": str(result.get("suggested_name", ""))[:_NAME_MAX_CHARS],
        }

    # ---------------------------------------------------------
    # Default analysis result (empty content or total failure)
    # ---------------------------------------------------------
    def _default_analysis(self) -> dict:
        """
        Return default neutral analysis result.
        """
        return {
            "domain": ["未分类"],
            "valence": _DEFAULT_VALENCE,
            "arousal": _DEFAULT_AROUSAL,
            "tags": [],
            "aliases": [],
            "subjects": [],
            "suggested_name": "",
        }

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

    # ⚰️ `cut()` — which asked the model where to split a long body — was deleted.
    #    Its only caller was `grow_core`, and that path went away along with the principle
    #    that the system does not decide on your behalf how many things this is.

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

    # ---------------------------------------------------------
    # API call: judge whether a new event resolves an active plan
    # ---------------------------------------------------------
    async def judge_plan_resolution(self, plan_text: str, new_event_text: str) -> dict:
        """
        Conservative judgement: false negatives are encouraged, false positives are not.
        resolved=True is returned only when the new event states plainly that the plan is done.
        Returns: {"resolved": bool, "confidence": float, "reason": str}
        Returns {"resolved": False} silently when API unavailable.
        """
        if not self.api_available:
            return {"resolved": False, "confidence": 0.0, "reason": "API 不可用"}
        system = (
            "你是一个保守的计划完成判断器。给定一条 plan 和一条新事件，"
            "只在新事件明确表示该 plan 已被完成、放弃或不再相关时，输出 resolved=true；"
            "其它情况一律 false。返回严格 JSON：{\"resolved\": true/false, \"confidence\": 0~1, \"reason\": \"...\"}。"
            "不要解释、不要 markdown、不要多余文本。"
        )
        user = (
            f"PLAN:\n{plan_text[:_PLAN_JUDGE_INPUT_LIMIT]}\n\n"
            f"NEW EVENT:\n{new_event_text[:_PLAN_JUDGE_INPUT_LIMIT]}"
        )
        try:
            raw = await self._chat(
                system,
                user,
                max_tokens=_PLAN_JUDGE_MAX_TOKENS,
                temperature=_PLAN_JUDGE_TEMPERATURE,
            )
            if not raw:
                return {"resolved": False, "confidence": 0.0, "reason": "空响应"}
            cleaned = self._strip_md_fence(raw)
            data = json.loads(cleaned)
            return {
                "resolved": parse_bool(data.get("resolved", False), default=False),
                "confidence": float(data.get("confidence", 0.0)),
                "reason": str(data.get("reason", ""))[:_PLAN_REASON_MAX],
            }
        except Exception as e:
            logger.warning(f"judge_plan_resolution failed: {e}")
            return {"resolved": False, "confidence": 0.0, "reason": str(e)}

    async def judge_same_event(self, old_memory: str, new_content: str) -> dict:
        """Conservatively decide whether two pieces of content describe the same concrete
        event.

        A shared topic is not enough to merge on; same_event=True is returned only when the
        latter is an addition to, a development of, a correction of, or a restatement of the
        former. With the API unavailable, or on a parse failure, it conservatively returns
        False.
        """
        if old_memory.strip() == new_content.strip():
            return {"same_event": True, "confidence": 1.0, "reason": "正文完全相同"}
        if not self.api_available:
            return {"same_event": False, "confidence": 0.0, "reason": "API 不可用"}
        system = (
            "你是一个保守的记忆事件边界判定器。判断新内容与旧记忆是否描述同一个具体事件。"
            "只有新内容是旧事件的补充、进展、纠正或重复表述时才能判为 true。"
            "仅主题、人物、情绪或 tags 相似必须判为 false。"
            "日期不同、场景不同、关键动作不同，或两段各自已是语义闭合的独立事件，必须判为 false。"
            "有疑问时一律 false。只返回严格 JSON："
            '{"same_event": true/false, "confidence": 0~1, "reason": "..."}。'
        )
        user = (
            f"OLD MEMORY:\n{old_memory[:_SAME_EVENT_INPUT_LIMIT]}\n\n"
            f"NEW CONTENT:\n{new_content[:_SAME_EVENT_INPUT_LIMIT]}"
        )
        try:
            raw = await self._chat(
                system,
                user,
                max_tokens=_SAME_EVENT_MAX_TOKENS,
                temperature=_SAME_EVENT_TEMPERATURE,
            )
            if not raw:
                return {"same_event": False, "confidence": 0.0, "reason": "空响应"}
            data = json.loads(self._strip_md_fence(raw))
            return {
                "same_event": parse_bool(data.get("same_event", False), default=False),
                "confidence": float(data.get("confidence", 0.0)),
                "reason": str(data.get("reason", ""))[:_SAME_EVENT_REASON_MAX],
            }
        except Exception as e:
            logger.warning(f"judge_same_event failed: {e}")
            return {"same_event": False, "confidence": 0.0, "reason": str(e)}
