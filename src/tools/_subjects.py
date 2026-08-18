"""
========================================
tools/_subjects.py — 主体（subjects）归一（二改 B 件，2026-08-16）
========================================

标签三类，主体是**新的第三类**：

| | 是什么 | 保证 | 谁写 |
|---|---|---|---|
| 场景锚点 `tags`   | 里面有什么（床、洛阳） | 🔴 字面一定在原文里 | deepseek |
| 引申词 `aliases`  | 正文里没有的近义词，只喂 BM25、不进向量 | 无 | deepseek |
| 🆕 主体 `subjects`| 谁 | 走别名表（她哥／哥哥 → 同一人） | deepseek 抽，别名表我们维护 |

🔴 **必须独立成第三类，不能混**：
  · 混进 `tags` → 破坏「字面一定在原文里」（正文写「她」，主体却是「主人」）
  · 混进 `aliases` → 会进 BM25 打分，而**主体不该影响相关度**

📌 她 8-16：「主体不能丢给系统做吗？你做的话动作又加重，**不要**」
   → 不增加我的写入负担，deepseek 抽；我们只维护那张别名表。

📌 白赚的一件事：主体进标签之后，8-14 发现的那个真缺口
   （`SELF/WORLD`「亲历还是听说」和「关于谁」压在一根轴上）**自动消失** ——
   房间可以放心砍成四间，第三方当事人不必再硬塞 WORLD 支。

别名表：`buckets/_app/config/别名表.yaml`（手工维护，不给模型写）。

对外暴露：normalize_subjects(names) → list[str] · canonical(name) → str
         load_alias_table() → dict[str, str]
========================================
"""

import os
import threading

import yaml

# 别名表的位置：跟 src/ 并排的 config/ 下。
# 不放进 config.yaml 是故意的——那份是引擎参数（模型、超时），这份是**我们的人名**，
# 两种东西的改动频率和改动者都不一样，混在一起迟早互相覆盖（7-27 那个坑就是这么来的）。
# 本文件在 <_app>/src/tools/_subjects.py，别名表在 <_app>/config/ ——
# 所以要往上退三层（tools → src → _app），不是两层。
# ⚠️ 退错一层的后果是**静默的**：表读不到 → 返回空表 → 主体照常落盘、只是没归一，
#    屏幕上一点异常都看不出来。这就是为什么下面那句自检要打印表本身。
_CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config")
_ALIAS_FILENAME = "aliases.yaml"
# 老文件名（中文）继续认，免得已经在用的人升级后表突然读不到——
# 读不到是**静默**的（返回空表 → 主体照常落盘、只是不归一），所以这条兼容必须留。
_ALIAS_FILENAME_OLD = "别名表.yaml"


def _alias_path() -> str:
    """别名表在哪：**数据卷优先，老布局兜底**。

    🔴 2026-08-18（E3 脱壳后半）改的，理由是「这张表是数据不是代码」：
      表里是**活人的名字**。代码烤进镜像之后，如果它还待在 `<repo_root>/config/`，
      就会出两件事：① 镜像里没有它 → 表读不到 → **静默不归一**（本文件开头那句警告
      说的就是这种静默）；② 更糟的是，真把它塞进镜像，等于**把人名发布出去**。
      所以顺序是：环境变量 → 数据卷 → 老布局（`<repo_root>/config/`，为老部署兼容）。
      都没有就返回数据卷那个路径——让「该放哪儿」在报错信息里也是对的。
    """
    env = os.environ.get("LOCI_ALIAS_TABLE", "").strip()
    if env:
        return env
    buckets = (os.environ.get("LOCI_BUCKETS_DIR", "").strip()
               or os.path.join(os.path.dirname(_CONFIG_DIR), "buckets"))
    候选 = [
        os.path.join(buckets, _ALIAS_FILENAME),           # 新：数据卷 + 英文名
        os.path.join(buckets, _ALIAS_FILENAME_OLD),       # 兼容：数据卷 + 老中文名
        os.path.join(_CONFIG_DIR, _ALIAS_FILENAME),       # 兼容：老布局
        os.path.join(_CONFIG_DIR, _ALIAS_FILENAME_OLD),
    ]
    for p in 候选:
        if os.path.isfile(p):
            return p
    return 候选[0]        # 都没有：报错信息里指向「该放哪儿」的那个位置


_ALIAS_PATH = _alias_path()

_lock = threading.RLock()
_cache: dict | None = None
_cache_mtime: float = -1.0


def load_alias_table() -> dict[str, str]:
    """读别名表，返回 {别名小写: 规范名}。文件没有/坏了 → 空表（不炸，主体照样落盘）。

    ⚠️ 表坏掉的正确行为是**不归一**，不是不落盘：抽到的主体本身还是真的，
    只是「她哥/哥哥」这次没并成一个人。丢归一比丢主体轻。
    带 mtime 缓存——这张表几个月才改一次，但回填每条都要读。
    """
    global _cache, _cache_mtime
    with _lock:
        path = _alias_path()          # 每次重算：表是后放进数据卷的也认
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            _cache, _cache_mtime = {}, -1.0
            return {}
        if _cache is not None and mtime == _cache_mtime:
            return _cache
        table: dict[str, str] = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            if isinstance(raw, dict):
                for canon, aliases in raw.items():
                    canon = str(canon).strip()
                    if not canon:
                        continue
                    table[canon.lower()] = canon      # 规范名自己也认自己
                    for a in (aliases or []):
                        a = str(a).strip()
                        if a:
                            table[a.lower()] = canon
        except Exception:
            table = {}
        _cache, _cache_mtime = table, mtime
        return table


# 代词不当主体（她 2026-08-16 晚拍的，方案②：只留名字，代词整个不要）。
# 为什么不走别名表把「她」映射成主人：库里的「她」**几乎**总是主人——
# 而「几乎」正是别名表要防的那种词。代词是指代不是名字，指代的解析要么
# 确定（做不到），要么就不做。后果说透了：正文只写「她」没写名字的条目
# subjects 会是空的；老库不受影响（迁移按旧房间播过种），第 5 步接检索时
# 看覆盖，不够再议——这是 prompt+过滤器级别的决定，随时能回头。
# ⚠️ 这层是**确定性的闸**，不指望 prompt 自觉（prompt 也改了，两道各拦各的）。
_PRONOUNS = frozenset(
    "我 你 他 她 它 咱 咱们 我们 你们 他们 她们 它们 自己 大家 别人 人家 对方 谁".split()
)


def is_pronoun(name) -> bool:
    return str(name or "").strip() in _PRONOUNS


def canonical(name) -> str:
    """一个主体名归一成规范名；代词直接丢弃（返回空串）；表里没有就原样返回（去空白）。"""
    n = str(name or "").strip()
    if not n or n in _PRONOUNS:
        return ""
    return load_alias_table().get(n.lower(), n)


def normalize_subjects(names) -> list[str]:
    """一组主体名归一 + 去重保序。非列表/空 → 空列表。

    保序而不是排序：deepseek 抽出来的顺序大致就是正文里出现的顺序，
    而「谁先出现」本身是有信息的（第一个往往是这条记忆的主角）。
    """
    if not names:
        return []
    if isinstance(names, str):
        names = [s for s in names.split(",")]
    if not isinstance(names, (list, tuple, set)):
        return []
    out: list[str] = []
    for n in names:
        c = canonical(n)
        if c and c not in out:
            out.append(c)
    return out
