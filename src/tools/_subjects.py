"""
========================================
tools/_subjects.py — 主体（subjects）归一（二改 B 件，2026-08-16）
========================================

标签三类，主体是**新的第三类**：

| | 是什么 | 保证 | 谁写 |
|---|---|---|---|
| 场景锚点 `tags`   | 里面有什么（床、青岛） | 🔴 字面一定在原文里 | deepseek |
| 引申词 `aliases`  | 正文里没有的近义词，只喂 BM25、不进向量 | 无 | deepseek |
| 🆕 主体 `subjects`| 谁 | 走别名表（老张／张三 → 同一人） | deepseek 抽，别名表我们维护 |

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

# 「这不是人」的黑名单，就住在同一张表里的一个特殊键下（她 2026-08-19 定的 A 方案）。
# 为什么不另开一个文件：人名的事全在一个文件里，改的时候不用记「那个在哪儿」。
# 为什么必须有它：8-19 撞见的活证据 —— 我存了一条记忆说「小刀批是模型抽错的噪音」，
# 打标模型读到正文里那三个字，**又把它抽成了一个人名**，那个名字从 1 条变成 2 条。
# 「说它是噪音」这个动作本身在生产噪音。所以光删不管用，得有一道以后不再抽它的闸。
_NOT_PERSON_KEY = "__不是人__"

_lock = threading.RLock()
_cache: dict | None = None
_cache_blocked: frozenset = frozenset()
_cache_mtime: float = -1.0


def load_alias_table() -> dict[str, str]:
    """读别名表，返回 {别名小写: 规范名}。文件没有/坏了 → 空表（不炸，主体照样落盘）。

    ⚠️ 表坏掉的正确行为是**不归一**，不是不落盘：抽到的主体本身还是真的，
    只是「老张/张三」这次没并成一个人。丢归一比丢主体轻。
    带 mtime 缓存——这张表几个月才改一次，但回填每条都要读。
    """
    global _cache, _cache_blocked, _cache_mtime
    with _lock:
        path = _alias_path()          # 每次重算：表是后放进数据卷的也认
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            _cache, _cache_blocked, _cache_mtime = {}, frozenset(), -1.0
            return {}
        if _cache is not None and mtime == _cache_mtime:
            return _cache
        table: dict[str, str] = {}
        blocked: set[str] = set()
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            if isinstance(raw, dict):
                for canon, aliases in raw.items():
                    canon = str(canon).strip()
                    if not canon:
                        continue
                    # 特殊键：底下那些名字是「不是人」，不是某个人的别名。
                    # 绝不能让它掉进 table —— 掉进去就等于把它们归一成
                    # 一个叫「__不是人__」的人，那比不管更糟。
                    if canon == _NOT_PERSON_KEY:
                        for a in (aliases or []):
                            a = str(a).strip()
                            if a:
                                blocked.add(a.lower())
                        continue
                    table[canon.lower()] = canon      # 规范名自己也认自己
                    for a in (aliases or []):
                        a = str(a).strip()
                        if a:
                            table[a.lower()] = canon
        except Exception:
            table, blocked = {}, set()
        _cache, _cache_blocked, _cache_mtime = table, frozenset(blocked), mtime
        return table


def load_not_person() -> frozenset:
    """黑名单：被人亲手标成「这不是人」的那些名字（小写）。

    跟别名表同一个文件、同一次读、同一份 mtime 缓存 —— 两边永远不会各说各话。
    """
    load_alias_table()                # 顺带把 _cache_blocked 填上（或用上缓存）
    return _cache_blocked


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
    """一个主体名归一成规范名。返回空串 = 这个名字不要了。

    三种情况返回空：空的 · 代词 · 被人标成「这不是人」（黑名单）。
    表里没有就原样返回（去空白）—— 新出现的人照样落盘，只是还没归一。
    """
    n = str(name or "").strip()
    if not n or n in _PRONOUNS:
        return ""
    table = load_alias_table()        # 先读，_cache_blocked 才是新的
    if n.lower() in _cache_blocked:
        return ""
    return table.get(n.lower(), n)


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

# ============================================================
# 往表里写（2026-08-19）—— **只有人点了那一下才会走到这儿**
# ============================================================
# 判据跟 muse/fold 同一条：系统只负责摆出来，改哪个是人点的那一下。
# 所以这两个写口没有任何自动触发路径，只挂在面板「都有谁」那一屏的按钮上。
#
# 🔴 为什么是**文本插入**而不是 yaml.safe_dump 重写整份：
#    这张表是她一行行手写的，开头那一大段注释讲清了「为什么它是闸不是约定」。
#    safe_dump 会把注释全部冲掉 —— 那等于用一次点击换掉她写下的判据。
#    插入法只往里加行，别的一个字节都不动。

_NL = chr(10)
_CR = chr(13)
_TAB = chr(9)
_QUOTES = '"' + chr(39)

_NEW_TABLE_HEADER = _NL.join([
    "# " + "=" * 58,
    "# 别名表 —— 主体（subjects）归一用。手工维护，不给模型写。",
    "# " + "=" * 58,
    "# 规范名（key）= 落进 frontmatter 的那个词；别名（value）= 正文里的各种写法。",
    "# 特殊键 __不是人__ 底下那些不是别名，是「这几个词根本不是人」的黑名单。",
    "# 形状见 config/aliases.example.yaml。",
    "",
])


def _yaml_scalar(v: str) -> str:
    """一个名字写进 YAML 该长什么样。交给 yaml 自己决定要不要加引号——
    「77」这种纯数字、带冒号或井号的名字，手写引号迟早写漏一个。"""
    out = yaml.safe_dump(str(v), allow_unicode=True,
                         default_flow_style=True).strip()
    if out.endswith("..."):          # safe_dump 给标量会带个文档结束符
        out = out[:-3].strip()
    return out


def _is_indented(line: str) -> bool:
    return line.startswith(" ") or line.startswith(_TAB)


def _list_items_under(lines: list, i: int) -> tuple:
    """从 key 所在行 i 往下走，返回（最后一个列表项的行号，那些项的原文）。

    插在**最后一个列表项**后面而不是整块末尾：块里可能有注释，
    插到注释下面会看着像那句注释在说这个新名字。
    """
    last, items = i, []
    j = i + 1
    while j < len(lines):
        ln = lines[j]
        if ln.strip() == "":
            j += 1
            continue
        if not _is_indented(ln):
            break                    # 下一个顶格 key，块结束
        body = ln.lstrip()
        if body.startswith("- "):
            last = j
            items.append(body[2:].strip().strip(_QUOTES))
        j += 1
    return last, items


def _insert_under(text: str, key: str, value: str, comment: str = "") -> tuple:
    """把 value 插到 key 那个列表末尾。返回（新全文，动了没）。key 不存在就新建一块。

    comment 只在**新建那一块**的时候写在它上面 —— 已经存在的块不动，
    免得每加一个名字就多一遍同样的话。
    """
    lines = text.splitlines()
    ki = None
    for idx, ln in enumerate(lines):
        if _is_indented(ln) or ln.lstrip().startswith("#") or ":" not in ln:
            continue
        if ln.split(":", 1)[0].strip().strip(_QUOTES) == key:
            ki = idx
            break
    if ki is None:
        block = []
        if comment:
            block += ["# " + ln for ln in comment.split(_NL)]
        block += [_yaml_scalar(key) + ":", "  - " + _yaml_scalar(value), ""]
        return text.rstrip(_NL) + _NL + _NL + _NL.join(block), True
    last, items = _list_items_under(lines, ki)
    low = [x.lower() for x in items]
    if value in items or value.lower() in low:
        return text, False           # 已经在里头了，不重复写
    lines.insert(last + 1, "  - " + _yaml_scalar(value))
    return _NL.join(lines) + _NL, True


def _read_table_text() -> str:
    path = _alias_path()
    if not os.path.isfile(path):
        return _NEW_TABLE_HEADER
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write_table(text: str) -> None:
    """落盘：先 encode 成 bytes → 写 .tmp → os.replace 原子换。

    🔴 2026-08-19 凌晨的血：open(p, "w") 是**先截断再写**，编码那一步一炸，
       文件就地清零、内容还没进去（那天 web/loci.py 就这么没的，只能从镜像里捞）。
       先编码就永远炸在内存里，原文件一根汗毛都碰不到。
    """
    global _cache, _cache_mtime
    path = _alias_path()
    data = text.encode("utf-8")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    with _lock:
        # 主动作废缓存：别赌 mtime 的秒级精度（同一秒内改两次就看不见了）
        _cache, _cache_mtime = None, -1.0


def _check_name(v, what: str) -> str:
    v = str(v or "").strip()
    if not v:
        raise ValueError(what + "不能是空的")
    if len(v) > 40:
        raise ValueError(what + "太长了（超过 40 个字，多半是把正文粘进来了）")
    if _NL in v or _CR in v:
        raise ValueError(what + "里不能有换行")
    return v


def mark_not_person(name) -> bool:
    """「这不是人」：记进黑名单，以后不再抽它。历史那几条**一个字节都不动**。

    她 2026-08-19 定的 A 方案。判据：改历史 metadata 是往她的记忆里写字，
    而「当时模型这么抽的」本身是个事实；黑名单已经达到目的（不再冒头、不再抽），
    而且随时能反悔——把那一行从表里删掉就回来了。
    """
    name = _check_name(name, "名字")
    # 这段注释只在**第一次**建这个块的时候写进去。面板上那一屏一个字都不提
    # 被标掉的名字（她 8-19 定的：隐藏就是隐藏，再列一遍等于没隐藏），
    # 所以「怎么反悔」必须写在这儿 —— 要反悔的人本来就得开这个文件。
    why = _NL.join([
        "下面这些不是别名，是「这几个词根本不是人」——",
        "面板「人名表」那一屏上点「这不是人」写进来的。",
        "它们不再摆出来、以后也不再抽；历史那些条一个字节都没动。",
        "想反悔：把对应那一行删掉就回来了。",
    ])
    new, changed = _insert_under(_read_table_text(), _NOT_PERSON_KEY, name,
                                 comment=why)
    if changed:
        _write_table(new)
    return changed


def add_alias(canon, alias) -> bool:
    """「这两个是一个人」/「给他个正式名字」：alias 归到 canon 底下。

    ⚠️ 只管**以后**：老条目盘上还是老名字（这张表没有迁移脚本，表头那句
       「要改先想清楚谁来改盘」说的就是这个）。面板那一屏按表把它们并起来看，
       所以点完当场就少一行——但盘上仍是两个词，recall 搜老名字照样搜得到。
    """
    canon = _check_name(canon, "规范名")
    alias = _check_name(alias, "别名")
    if _NOT_PERSON_KEY in (canon, alias):
        raise ValueError("__不是人__ 是特殊键，不能当名字")
    if is_pronoun(canon) or is_pronoun(alias):
        raise ValueError("代词不进这张表——指代不是名字")
    if canon.lower() == alias.lower():
        raise ValueError("这两个是同一个词")
    new, changed = _insert_under(_read_table_text(), canon, alias)
    if changed:
        _write_table(new)
    return changed
