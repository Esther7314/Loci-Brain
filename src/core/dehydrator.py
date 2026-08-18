"""
========================================
dehydrator.py — 调用 LLM 做「脱水压缩 / 合并 / 打标 / 拆分」
========================================

这个文件包住对外部 LLM 的所有 prompt 和调用。tools/hold、tools/grow、
tools/dream 等都通过它来「让模型做内容理解」，自身不直接拼 prompt。

关键行为：
- dehydrate(content)：把长内容压成高密度摘要，省 token
- merge(old, new)：揉合新旧内容并保持桶体积大致恒定
- analyze(content, for_mind=False)：返回 {domain, valence, arousal, tags, aliases, suggested_name}
  （tags=场景锚点 scene，字面校验；aliases=引申词，只喂 bm25；for_mind 时不抽 scene）
- digest(content)：把日记/长文拆成 2~6 条独立条目（grow 用）
- 走 OpenAI 兼容客户端（DeepSeek / Ollama / LM Studio / vLLM / Gemini 都行）
- SQLite 缓存脱水结果，避免对相同内容重复调用 API

不做什么（边界）：
- 不读写记忆桶文件（不知道 bucket 是什么形态）
- 不决定何时调用、不做去重判断（hold/grow 决定）
- 没 API key 时不报错，返回降级结果（让上层决定怎么办）

对外暴露：Dehydrator 类（dehydrate / merge / analyze / digest）和默认 prompt 字符串
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
# 调参面板 / Tunable constants
# ------------------------------------------------------------
# rule.md §①：禁裸魔法数字。这些原本散在五个 _api_* 方法中，
# 集中后调参一眼看完；prompt 模板本身仍在下面以可读性优先。
# ============================================================

# --- 脱水缓存版本号 ---
# 改任何会影响脱水/合并输出的 prompt 时 +1，使存量缓存自然失效（见 _content_key）。
# v2：DEHYDRATE/MERGE 加入「视角铁律」，强制保留第一人称（我 / 人名）。
# v3：脱水结果只接受既定 JSON schema，隔离模型追加的评论、立场与未知字段。
# v4：视角铁律补反向条款——v2 只防「我被抹掉」方向（规则和示例都是单向的），
#     脱水 LLM 在含糊处过度矫正：省略主语的句子被归给「我」（实案：正文
#     「07-07嚎啕大哭…吊她」经 /breath-hook 脱水成「07-07我嚎啕大哭…吊我」，
#     主语翻转）。补反向同罪条款 + 省略主语处理规则 + 反向示例。
_PROMPT_VERSION = 4

# --- LLM 默认参数 ---
_DEFAULT_MODEL = "gemini-2.0-flash"
_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
_DEFAULT_MAX_TOKENS = 1024
_DEFAULT_TEMPERATURE = 0.1
_API_TIMEOUT_SECONDS = 60.0

# --- 瞬时错误重试（Gemini 免费层偶发 429 / 503，详见 README 故障表）---
# 总尝试 = 1 次初始 + (max_attempts-1) 次重试；退避 base*2^attempt 秒。
_RETRY_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY = 0.8
_RETRY_STATUS = {429, 500, 502, 503, 504}

# --- 脱水 API 最终失败时的本地降级：返回原文截断片段的字符上限 ---
# 设计：API（含重试）彻底失败时，宁可返回未压缩的原文片段，也不让上层
# breath/dream 拿不到内容（rule.md §1.5 允许降级）。不写缓存，API 恢复后自动重压。
_DEHYDRATE_FALLBACK_CHARS = 300

# --- 该多长才需要压缩（低于该 token 数直接走原文）---
_DEHYDRATE_MIN_TOKENS = 100

# --- 各 API 调用的内容截断上限（防 prompt token 超范围）---
_DEHYDRATE_INPUT_LIMIT = 3000
_MERGE_INPUT_LIMIT = 2000     # 新旧各一份
_ANALYZE_INPUT_LIMIT = 2000
_DIGEST_INPUT_LIMIT = 5000    # 一天的日记量较大
_PLAN_JUDGE_INPUT_LIMIT = 1500  # plan 与 new event 各一份
_SAME_EVENT_INPUT_LIMIT = 1800  # 旧桶与新内容各一份

# --- 各专用调用的 max_tokens 覆盖 ---
_ANALYZE_MAX_TOKENS = 4096      # Gemini 2.5 thinking 会消耗大量 token，需留足余量
_DIGEST_MAX_TOKENS = 8192       # 日记拆条内容多，thinking + 输出都需要足量空间
_PLAN_JUDGE_MAX_TOKENS = 2048   # thinking 模型下 200 token 完全不够
_PLAN_JUDGE_TEMPERATURE = 0.0   # 判定需确定性
_SAME_EVENT_MAX_TOKENS = 1024   # 仅返回紧凑 JSON
_SAME_EVENT_TEMPERATURE = 0.0   # 事件边界判定需确定性
_DIGEST_TEMPERATURE = 0.0       # 拆条需确定性

# --- 默认情感坐标（与 bucket_manager 中保持一致）---
_DEFAULT_VALENCE = 0.5  # 0=极负, 1=极正
_DEFAULT_AROUSAL = 0.3  # 0=完全平静, 1=极激动

# --- 输出截断长度 ---
_TAGS_MAX = 15           # tags 最多保留几个
# 指「这两个人」的标签（名字 + 纯指称），一律不进 tags —— 理由见 _parse_analysis 里的注释。
# 🔴 2026-08-18（E3 脱壳后半）：名字**从代码里搬走了**。原来这儿硬写着我们俩的名字，
#    那等于把活人的名字发布出去；而且别人 clone 下来，挡的还是我们家的名字，对他毫无用处。
#    现在名字来自 `AI_NAME` / `LOCI_OWNER_NAME` 和 config.yaml 的 `people:` 段。
#    代码里只留**通用代词**——那部分对谁都成立。
# 刻意不含「主人/老婆/老公/宝宝」这类称呼——那些是称呼事件本身，有区分度。
_PRONOUN_TAGS = frozenset({"她", "他", "我", "你"})


def _person_tags() -> frozenset:
    """不该进 tags 的人名：通用代词 + 这两个人的**全部叫法**。

    🔴 人名只有**一份**，就是别名表（`aliases.yaml`）。这儿不另立名单。
       2026-08-18 我一度在 config 里又加了一份 `people:`，等于同一件事记两处——
       两处一定会有一处过期。收成现在这样：
         · **谁是这两个人** → `AI_NAME` / `LOCI_OWNER_NAME`（各一个规范名）
         · **他们都被怎么叫** → 别名表里那两条的全部别名
       于是要维护的只有别名表一张，面板将来也只用给它做一个编辑器。
    """
    from utils import get_ai_name, get_owner_name
    from tools._subjects import load_alias_table

    规范名: set[str] = set()
    for n in (get_ai_name(), get_owner_name()):
        n = str(n or "").strip()
        if n and n != "AI":          # 没配名字时的占位符不算名字
            规范名.add(n)
    if not 规范名:
        return frozenset(_PRONOUN_TAGS)

    names = set(规范名)
    try:
        # 别名表是 {别名小写: 规范名}，反着查：这两个规范名底下挂的所有叫法
        for 别名, canon in (load_alias_table() or {}).items():
            if str(canon).strip() in 规范名:
                names.add(str(别名).strip())
                names.add(str(canon).strip())
    except Exception:                # 表读不到就只挡规范名+代词，不炸
        pass
    return frozenset(_PRONOUN_TAGS | {n for n in names if n})
_DOMAIN_MAX = 3          # domain 最多保留几个（rule.md 推荐选 1~2 个）
_NAME_MAX_CHARS = 20     # suggested_name 上限
_PLAN_REASON_MAX = 200   # plan 判定 reason 上限
_SAME_EVENT_REASON_MAX = 200  # 合并边界判定 reason 上限
_PARSE_ERR_PREVIEW = 200  # JSON 解析失败时日志中 raw 预览长度

# --- importance 范围（与哲学边界一致）---
_IMPORTANCE_MIN = 1
_IMPORTANCE_MAX = 10
_DEFAULT_IMPORTANCE = 5


# --- Dehydration prompt: instructs cheap LLM to compress information ---
# --- 脱水提示词：指导廉价 LLM 压缩信息 ---
# --- Perspective rule (shared) ---
# --- 视角铁律（脱水/合并共用）---
# BUG FIX：原文是 AI 第一人称写下的（"我也在她这里看到了自己没见过的碎片"），
# 但脱水/合并后被改写成第三人称（"双方在互动中互相发现对方未知的情感碎片"），
# 视角丢失。压缩本应保密度、不应改人称。下面这条规则注入 system prompt 强制保留：
#   AI 一方恒用「我」；人类一方一律用其名字称呼（由 config.human 注入）。
# 禁止 双方 / 对方 / 用户 / TA 等抹掉视角的中性第三人称。
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
# --- 日记整理提示词：把一大段日常拆分成多个独立记忆条目 ---
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


# --- Cut prompt: 只说在哪儿切，一个字都不许改（2026-08-05 她定的）---
#
# 为什么另起一个而不是改 DIGEST_PROMPT：DIGEST 的输出是 `{"content": "整理后的内容"}`
# —— **它会重写正文**（第 3 条明写「去除无意义的口水话」）。她的原话是
# 「如果 deepseek 只是拆、不改原文，那没问题」。
#
# 关键不在这段提示词，在**代码只接受位置**：模型返回的是「在哪儿切」，
# 切的动作由 `_apply_cuts()` 做，而它只会 `find` + 切片。
# **原文一个字碰不到，因为代码根本没有「写」的能力。**
# 跟 scene 的字面校验同一个模式：别在 prompt 里叮嘱「不许改」，让它没有改的机会。
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
# --- 合并提示词：指导 LLM 揉合新旧记忆 ---
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
# --- 自动打标提示词：分析内容的主题域和情感坐标 ---
# 2026-08-06 tags 三组重新分工（她一刀砍到根）：
#   core   → 🗑️ 砍。它抽的就是正文里的词，bm25 索引和字面命中本来就含正文，
#            对搜索贡献是零；唯一起的作用恰恰是我们不想要的——塌缩按频率取
#            tags 时，抽象的 core 永远赢（那行全是「亲密关系·沟通·关系」）。
#   scene  → ✅ 留，tags 从此只有它（字面校验保证一定在原文里）。
#   expand → 挪去新字段 aliases，只喂 bm25，不进向量、不在塌缩/chips 露面。
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

# MIND 专用（机制④ 第 5 条）：认知里没有照片，不抽 scene——
# 逼模型在一段思考里找「画面」只会逼出编造。只要 expand（换措辞搜得到）。
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
    数据脱水器 + 内容分析器。
    三大能力：脱水压缩 / 新旧合并 / 自动打标。
    仅走 API：API 不可用时直接抛出 RuntimeError，调用方明确感知。
    （根据 BEHAVIOR_SPEC.md 三、降级行为表决策：无本地降级）
    """

    def __init__(self, config: dict):
        # --- Read dehydration API config / 读取脱水 API 配置 ---
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
        # thinking_budget: 仅 Gemini 2.5+/3.x「思考型」模型生效。默认 0 = 关闭思考。
        # 关键：gemini-3.5-flash 等模型默认会先消耗 output token 做「思考」，当
        # max_tokens 较小时思考会吃光预算 → 返回空文本（这正是脱水/抽取偶发返回
        # 空、报 "LLM extraction failed" 的根因）。脱水/抽取是机械式转换，不需要
        # 思考，关掉它既修了空输出、又更快更省。设为 None 可彻底不发该字段（兼容
        # 不支持 thinkingConfig 的老模型）。
        self.thinking_budget = dehy_cfg.get("thinking_budget", 0)

        # --- Human display name / 人类一方的称呼 ---
        # 注入脱水/合并的「视角铁律」：原文里人类那一方统一还原为这个名字，
        # 而不是被压成「双方/对方/用户」。与 config.human 同源（前端可改）。
        self.human = config.get("human", "用户") or "用户"

        # --- API availability / 是否有可用的 API ---
        self.api_available = bool(self.api_key)

        # --- Initialize OpenAI-compatible client (only for openai_compat format) ---
        # --- 初始化 OpenAI 兼容客户端（仅 openai_compat 格式使用）---
        self.client: Optional[AsyncOpenAI] = None
        if self.api_available and self.api_format == "openai_compat":
            self.client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout_seconds,
            )

        # --- SQLite dehydration cache ---
        # --- SQLite 脱水缓存：content hash → summary ---
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
        """缓存键 = hash(prompt 版本 + 人名 + 模型配置 + 原文)。

        缓存原本只按 content_hash 存，导致脱水 prompt 改了、人名改了，旧的
        third-person 摘要仍会命中缓存返回——视角修复对存量内容不生效。把
        prompt 版本、人名、api_format、base_url 和 model 混进 key，换模型或端点后
        下次 breath 会用新配置重新脱水，不会复用旧模型的摘要。"""
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
    # 内部 helpers / Internal helpers
    # ---------------------------------------------------------
    def _require_api(self) -> None:
        """API 不可用时抛出统一文案的 RuntimeError。

        原本 dehydrate / merge / analyze / digest 各处都重复
        `if not self.api_available: raise RuntimeError("...")`，
        统一后调用方一行 `self._require_api()` 即可，且文案改一处全部生效。
        """
        if not self.api_available:
            raise RuntimeError("脱水 API 不可用，请检查 config.yaml 中的 dehydration 配置")

    @staticmethod
    def _is_transient_error(exc: BaseException) -> bool:
        """是否为可重试的瞬时错误：HTTP 429/500/502/503/504、超时、连接错误。

        兼容 httpx.HTTPStatusError（status_code 在 .response 上）与
        openai.APIStatusError（status_code 在异常上）；其余按类名兜底匹配
        timeout / connect / ratelimit / unavailable。"""
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
        """统一 chat 入口：对 429 / 5xx / 超时等瞬时错误做指数退避重试。

        真正的单次调用在 _chat_once；这里只负责重试与退避，让 Gemini 免费层
        偶发的 429/503 不至于直接把脱水/合并打挂（见 README 故障表）。"""
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
        """统一的 OpenAI-compatible chat 调用。

        原本 5 个 _api_* 方法重复了同样的样板：
          * 构造 messages
          * 调 client.chat.completions.create
          * 检查 response.choices 非空
          * 取 choices[0].message.content 并兜底空字符串
        统一后：
          * 调用方传入 system + user prompt 与可选的 max_tokens / temperature
          * 默认值取 self.max_tokens / self.temperature（由 config.yaml 决定）
          * 始终返回 str（response 异常时返回空串，调用方各自决策）

        参数：
            system, user — Chat completion 的 system/user 消息
            max_tokens   — 覆盖默认（如 analyze 用 256，digest 用 2048）
            temperature  — 覆盖默认（如 digest / plan_judge 需要 0.0）
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
        # 关闭/限制思考预算（见 __init__ 的 thinking_budget 说明）。
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
        """读取 meta 中的 valence / arousal 并钳制到 [0, 1]。

        三处 LLM 返回校验逻辑相同（_format_output / _parse_analysis / _parse_digest），
        集中后保证三处行为一致：解析失败一律回 (默认 V, 默认 A)。
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
    # 脱水：将原始内容压缩为精简摘要
    # API only (no local fallback)
    # 仅通过 API 脱水（无本地回退）
    # ---------------------------------------------------------
    async def dehydrate(self, content: str, metadata: Optional[dict] = None) -> str:
        """
        Dehydrate/compress memory content.
        Returns formatted summary string ready for LLM context injection.
        Uses SQLite cache to avoid redundant API calls.
        对记忆内容做脱水压缩。
        返回格式化的摘要字符串，可直接注入 LLM 上下文。
        使用 SQLite 缓存避免重复调用 API。
        """
        if not content or not content.strip():
            return "（空记忆 / empty memory）"

        # --- Content is short enough, no compression needed ---
        # --- 内容已经很短，不需要压缩 ---
        if count_tokens_approx(content) < _DEHYDRATE_MIN_TOKENS:
            return self._format_output(content, metadata)

        # --- Check cache first ---
        # --- 先查缓存 ---
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
        # --- API 脱水（无本地降级）---
        self._require_api()

        try:
            raw_result = await self._api_dehydrate(content)
            result = self._normalize_dehydration_result(raw_result)
        except Exception as e:
            # --- 本地降级：API（已含重试）彻底失败时，返回原文截断片段而非抛异常。---
            # 让 breath/dream 在 Gemini 抽风时仍能拿到内容（只是没压缩）；不写缓存，
            # API 恢复后下次自然重新压缩。
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
    # Merge: blend new content into existing bucket
    # 合并：将新内容揉入已有桶，保持体积恒定
    # ---------------------------------------------------------
    async def merge(self, old_content: str, new_content: str) -> str:
        """
        Merge new content with old memory, preventing infinite bucket growth.
        将新内容与旧记忆合并，避免桶无限膨胀。
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
    # API 调用：脱水压缩
    # ---------------------------------------------------------
    async def _api_dehydrate(self, content: str) -> str:
        """
        Call LLM API for intelligent dehydration (via OpenAI-compatible client).
        调用 LLM API 执行智能脱水。
        """
        return await self._chat(
            DEHYDRATE_PROMPT + _perspective_rule(self.human),
            content[:_DEHYDRATE_INPUT_LIMIT],
        )

    # ---------------------------------------------------------
    # API call: merge
    # API 调用：合并
    # ---------------------------------------------------------
    async def _api_merge(self, old_content: str, new_content: str) -> str:
        """
        Call LLM API for intelligent merge (via OpenAI-compatible client).
        调用 LLM API 执行智能合并。
        """
        user_msg = (
            f"旧记忆：\n{old_content[:_MERGE_INPUT_LIMIT]}\n\n"
            f"新内容：\n{new_content[:_MERGE_INPUT_LIMIT]}"
        )
        return await self._chat(MERGE_PROMPT + _perspective_rule(self.human), user_msg)

    # ---------------------------------------------------------
    # Output formatting
    # 输出格式化
    # Wraps dehydrated result with bucket name, tags, emotion coords
    # 把脱水结果包装成带桶名、标签、情感坐标的可读文本
    # ---------------------------------------------------------

    def _format_output(self, content: str, metadata: Optional[dict] = None) -> str:
        """
        Format dehydrated result into context-injectable text.
        将脱水结果格式化为可注入上下文的文本。
        """
        header = ""
        if metadata and isinstance(metadata, dict):
            name = metadata.get("name", "未命名")
            domains = ", ".join(metadata.get("domain", []))
            valence, arousal = self._clamp_va(metadata)
            # 图标语义与 pulse 一致：📌 只给钉住/保护的核心桶，其余按类型区分，
            # 普通动态桶用 💭。此前无条件用 📌 会让 breath 浮现里每条都像「核心准则」，
            # 与 docs/CLAUDE_PROMPT.md「带 📌 的是我钉的核心准则」的约定冲突。
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

        # 脱水结果可能是结构化 JSON（core_facts/emotion_state/todos/keywords/summary）。
        # 渲染成可读文本，而不是把整坨原始 JSON 塞进上下文——后者又丑又费 token，且与
        # 短内容「原文透传」的形态不一致（长桶显示 JSON、短桶显示纯文本）。
        content = self._render_dehydrated(content)
        content = re.sub(r'\[\[([^\]]+)\]\]', r'\1', content)
        return f"{header}{content}"

    @staticmethod
    def _render_dehydrated(content: str) -> str:
        """把脱水 LLM 返回的结构化 JSON 渲染成可读文本。

        识别到 core_facts/summary schema → 输出 summary + 核心事实 + 待办（丢弃仅供
        内部索引的 keywords、以及已由情感坐标承载的 emotion_state）。非该 schema 的
        内容（如短内容直接透传的原文、或普通字符串）原样返回。
        """
        try:
            parsed = json.loads(content)
        except (ValueError, TypeError):
            return content  # 非 JSON，原样透传
        if not isinstance(parsed, dict) or ("summary" not in parsed and "core_facts" not in parsed):
            return content  # 不是脱水 schema，原样透传

        lines: list[str] = []
        summary = str(parsed.get("summary") or "").strip()
        facts = [str(f).strip() for f in (parsed.get("core_facts") or []) if str(f).strip()]
        if summary:
            lines.append(summary)
        elif facts:
            # 没有 summary 时，用核心事实兜底成正文，避免只剩空壳
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
    # 自动打标：分析内容，输出主题域 + 情感坐标 + 标签
    # Called by server.py when storing new memories
    # 存新记忆时由 server.py 调用
    # ---------------------------------------------------------
    async def analyze(self, content: str, for_mind: bool = False) -> dict:
        """
        Analyze content and return structured metadata.
        分析内容，返回结构化元数据。

        for_mind=True（机制④ 第 5 条）：MIND 只要 aliases + summary，不抽 scene
        ——认知里没有照片，tags 会是空的，这是设计不是缺陷。

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
    # API 调用：自动打标
    # ---------------------------------------------------------
    async def _api_analyze(self, content: str, for_mind: bool = False) -> dict:
        """
        Call LLM API for content analysis / tagging.
        调用 LLM API 执行内容分析打标。
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
    # 解析 API 返回的 JSON，做安全校验
    # Ensure valence/arousal in 0~1, domain/tags valid
    # ---------------------------------------------------------
    def _parse_analysis(self, raw: str, content: str = "") -> dict:
        """
        Parse and validate API tagging result.
        解析并校验 API 返回的打标结果。

        content 传原文，用来给 scene（场景锚点）做**字面校验**——见下方注释。
        """
        try:
            cleaned = self._strip_md_fence(raw)
            result = json.loads(cleaned)
        except (json.JSONDecodeError, IndexError, ValueError):
            logger.warning(f"API tagging JSON parse failed / JSON 解析失败: {raw[:_PARSE_ERR_PREVIEW]}")
            return self._default_analysis()

        if not isinstance(result, dict):
            return self._default_analysis()

        # --- Validate and clamp value ranges / 校验并钳制数值范围 ---
        valence, arousal = self._clamp_va(result)

        # --- tags = 只有 scene；expand 挪去 aliases（2026-08-06 她定的三组分工）---
        # 为什么（2026-08-05 定三组 → 2026-08-06 砍成两组）：上游只有 tags 一个维度，
        # 「潜意识/焦虑/亲密关系」这类提炼词在兼职当分类器。Loci 有了 room，分类的活
        # 已经有人干了，tags 从此专心记「这条里有什么」——塌缩那行自动变画面词，
        # 排序逻辑一行不改；画面串的原材料现成了。
        #
        # scene（场景锚点）：她的记忆是一张张照片，回想时先浮上来的是画面里的东西
        # （桌子、机构、材料……）。
        # ⚠️ scene 必须字面出现在原文里：模型偶尔会做变形替换（原文「脚底下不空了」
        # 提成「脚底下空的」），实测 4.3%。这里硬拦，比在 prompt 里反复叮嘱可靠。
        #
        # aliases（原 expand，引申词）：只喂 bm25（换个措辞也搜得到），不进向量、
        # 不在塌缩/chips 露面——它是搜索的暗轨，不是给人看的标签。
        #
        # 指「这两个人」的标签一律不进：整个库全都是关于这两个人的，
        # 他们的名字和「她/我」这类纯指称，在任何情况下都没有区分度。
        # ⚠️ 只挡这两个人的。别人的名字有区分度，不挡。
        # ⚠️ 在代码里挡，不在 prompt 里叮嘱——模型爱输出就输出，进不来。
        _person_stop = _person_tags()      # 每次现算：改了配置不用重启
        scene = [str(t) for t in (result.get("scene") or [])
                 if str(t).strip() and str(t) in content]
        tags = [t for t in scene if t not in _person_stop]
        aliases = [str(t) for t in (result.get("expand") or [])
                   if str(t).strip() and str(t) not in _person_stop]
        if not tags and not aliases:
            # 兜底：模型仍按老格式返回合并好的 tags（换 prompt 的过渡期，或它没听话）
            tags = [str(t) for t in (result.get("tags") or [])
                    if str(t).strip() and str(t) not in _person_stop]
        _seen: set[str] = set()
        tags = [t for t in tags if not (t in _seen or _seen.add(t))]
        _seen = set(tags)  # aliases 里跟 tags 重复的没意义（bm25 已经吃到了）
        aliases = [t for t in aliases if not (t in _seen or _seen.add(t))]

        # --- subjects（二改 B 件）：谁。**独立第三类**，既不进 tags 也不进 aliases ---
        # 模型抽的是正文里的**称呼**（「她」「我哥」），normalize_subjects 过别名表
        # 归一成规范名。归一是闸不是约定：不做的话「老张」和「张三」分裂成两个主体，
        # 检索当场断掉。
        # ⚠️ 这里**不做**「原文里必须字面存在」的校验——那是 tags 的保证，
        #    而主体恰恰要做的就是把「她」翻成「主人」。两条规则相反，别互相抄。
        subjects = normalize_subjects(result.get("subjects"))

        # 🔴 **这条记录抽到的主体，不许同时出现在 tags/aliases 里。**
        #    「谁」已经有自己的字段了，再在标签里出现一次就是零信息。
        #    这条规则**不认名字、对谁都成立**——不像上面那份 stop-list 需要先配置，
        #    它拿的是这条记录自己抽出来的人。所以别人装上就是对的，不用先填自己的名字。
        #    （两条一起留：stop-list 挡的是「在每一条里都没有区分度」的那两个人，
        #      即使某条没把他们抽成主体；这条挡的是「这一条里已经说过了」。）
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
    # 默认分析结果（内容为空或完全失败时用）
    # ---------------------------------------------------------
    def _default_analysis(self) -> dict:
        """
        Return default neutral analysis result.
        返回默认的中性分析结果。
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
    # 日记整理：把一大段日常拆分成多个独立记忆条目
    # For the "grow" tool — "dump a day's content and it gets organized"
    # 给 grow 工具用，"一天结束发一坨内容"靠这个
    # ---------------------------------------------------------
    async def digest(self, content: str) -> list[dict]:
        """
        Split a large chunk of daily content into independent memory entries.
        将一大段日常内容拆分成多个独立记忆条目。

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
    # 只拆不改：模型给切点，代码按位置切（2026-08-05）
    # ---------------------------------------------------------
    @staticmethod
    def apply_cuts(content: str, cuts: list) -> list[str]:
        """按切点把正文切成几段。**纯代码，没有任何改写能力。**

        校验三条，任何一条不过就整段不切（宁可不拆）：
        - 每个切点都能在原文里找到，且位置**严格递增**（模型抄错/抄乱直接作废）
        - 切完每段非空
        - **切完拼回去必须逐字等于原文** —— 这是逐字落盘的最后一道闸
        """
        text = str(content or "")
        if not text or not isinstance(cuts, list) or not cuts:
            return [text] if text else []

        pos: list[int] = []
        cursor = 0
        for c in cuts:
            frag = str(c or "").strip()
            if len(frag) < 4:
                return [text]                 # 太短定位不住，不切
            i = text.find(frag, cursor)
            if i < 0:
                return [text]                 # 找不到 = 模型改了字，不切
            pos.append(i)
            cursor = i + 1
        if pos[0] > 0:
            pos.insert(0, 0)                  # 第一个切点前面那段也是一条
        pos = sorted(set(pos))

        parts: list[str] = []
        for k, start in enumerate(pos):
            end = pos[k + 1] if k + 1 < len(pos) else len(text)
            parts.append(text[start:end])
        if any(not p.strip() for p in parts):
            return [text]
        if "".join(parts) != text:            # 逐字闸门：拼不回原文就整段作废
            return [text]
        return parts

    # ⚰️ 2026-08-18：`cut()`（让模型说在哪儿切一段长文）删了。
    #    它唯一的调用方是 `grow_core`，而那条路随「系统不替你决定这是几件事」一起撤了。

    # ---------------------------------------------------------
    # API call: diary digest
    # API 调用：日记整理
    # ---------------------------------------------------------
    async def _api_digest(self, content: str) -> list[dict]:
        """
        Call LLM API for diary organization.
        调用 LLM API 执行日记整理。
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
    # 解析日记整理结果，做安全校验
    # ---------------------------------------------------------
    def _parse_digest(self, raw: str) -> list[dict]:
        """
        Parse and validate API diary digest result.
        解析并校验 API 返回的日记整理结果。
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
    # API 调用：判断新事件是否完成了某个 active plan
    # ---------------------------------------------------------
    async def judge_plan_resolution(self, plan_text: str, new_event_text: str) -> dict:
        """
        Conservative judgement (鼓励漏报，避免误报).
        保守判断：仅在新事件明确表示 plan 已完成时返回 resolved=True。
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
        """保守判断两段内容是否属于同一个具体事件。

        主题相似不足以合并；只有后者是前者的补充、进展、纠正或重复表述时
        才返回 same_event=True。API 不可用或解析失败时保守返回 False。
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
