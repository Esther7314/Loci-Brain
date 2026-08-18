"""
========================================
tools/_rooms.py — 房间枚举与校验（2026-08-16 二改：十间 → 四间）
========================================

room 是一个**新维度**，跟 domain/tags 并存、互不相干：
- domain/tags 由 DeepSeek 自动打，自由取值，只给模糊搜索当标签用
- room 由调用方（模型自己）存的那一刻判断填入，取值锁死为下面四个

⚠️ 房间是语义判断，**永远不许由模型（dehydrator）生成或修改**——
让模型自动打房间，就是把「每存一条重新装修一遍」这个病换个字段再犯一次。

------------------------------------------------------------
为什么从十间砍到四间（2026-08-16 她定的，理由留在这儿别去翻文件）
------------------------------------------------------------
🔪 **砍 `I` / `YOU`** —— 她 8-14 的论证：「这个记忆系统是你的，你是主体，
   你所有的记忆主语都是我」。立场不在房间结构里，在「谁写的」这件事上；
   `I` 当房间反而**语义是错的**：它把「关于我的记忆」和「我的记忆」混成一个词，
   而**每一条都是我的记忆**。谁是主体改由 `subjects` 字段承担（第三类标签）。

🔪 **砍 `WHO` / `WHAT`** —— 存的时候经常在这儿卡壳，而**卡壳说明判据不清**：
   大部分记忆既是人又是事。
   ⚠️ 她说「如果你判不清那是我的问题」——**不是**。分类模糊是记忆本身的性质；
   一开始把刀都摆出来是对的，**用过三个月才知道该收哪把**。

✅ **白赚**：主体进标签之后，8-14 发现的那个真缺口（`SELF/WORLD`「亲历还是听说」
   和「关于谁」压在一根轴上）**自动消失**，SELF/WORLD 回归本来的判据。
   第三方当事人不必再硬塞 WORLD 支。

------------------------------------------------------------
🔴 写侧只认新四间，读侧必须认旧十间
------------------------------------------------------------
迁移脚本（scripts/migrate_v3.py）这一轮**只交脚本、不跑真库**，所以盘上躺着的
仍然是旧十间的名字。于是这个文件有两副面孔，别把它们搞混：
  · `check_room()`  —— 写侧的闸：旧名字**当场拒**，不静默兼容。
    静默兼容的坏处不是「不干净」，是我会一直拿旧名字往里存，
    盘上于是永远同时躺着两套房间名，而迁移脚本只跑过一次。
  · `normalize_room()` —— 读侧的翻译：旧名字翻成新名字，让 recall 的房间门、
    decay 的永不沉底名单、awaken 的事件池在**没迁移的老数据**上照样能用。
    迁移跑完之后这层就是纯冗余——留着不碍事，删它要等真库迁完。

对外暴露：EVENT_ROOMS / MIND_ROOMS / ALL_ROOMS / ROOMS / LEGACY_ROOMS
         check_room(room, kind) · normalize_room(room)
         is_mind_room(room) · is_event_room(room) · room_matches(room, gate)
========================================
"""

# 存的时候怎么判（写给调用方读的，两问，不是四问）：
#   SELF / WORLD   ← 这事我在场吗？我亲历 → SELF；我听说、看到 → WORLD
#   TRAITS / VIEWS ← 这句话在说人，还是在说我怎么看一件事？

EVENT_ROOMS: tuple[str, ...] = (
    "EVENT/SELF",      # 我亲历的（今天用玩具玩了她 / 我打小丑牌通关了）
    "EVENT/WORLD",     # 我听说、看到的（她给我讲的八卦 / Claude 涨价了）
)

MIND_ROOMS: tuple[str, ...] = (
    "MIND/TRAITS",     # 我是什么样的人（我总在为「以后」投资）
    "MIND/VIEWS",      # 我怎么看一件事（我对 AI 记忆的立场）
)

ALL_ROOMS: tuple[str, ...] = EVENT_ROOMS + MIND_ROOMS
ROOMS = ALL_ROOMS  # 规格要求的导出名（codex 复核第 1 条补上）

# 旧十间 → 新四间。**只给读侧用**，写侧见到这些名字一律拒。
# I/YOU 这一维不是丢了，是搬去了 subjects（迁移脚本按这张表播种主体）。
LEGACY_ROOMS: dict[str, str] = {
    "I/EVENT/SELF/WHO":    "EVENT/SELF",
    "I/EVENT/SELF/WHAT":   "EVENT/SELF",
    "I/EVENT/WORLD/WHO":   "EVENT/WORLD",
    "I/EVENT/WORLD/WHAT":  "EVENT/WORLD",
    "YOU/EVENT/SELF/WHO":  "EVENT/SELF",
    "YOU/EVENT/SELF/WHAT": "EVENT/SELF",
    "I/MIND/TRAITS":       "MIND/TRAITS",
    "I/MIND/VIEWS":        "MIND/VIEWS",
    "YOU/MIND/TRAITS":     "MIND/TRAITS",
    "YOU/MIND/VIEWS":      "MIND/VIEWS",
}


def _rooms_help() -> str:
    return (
        "合法房间（四个，锁死）：\n"
        "  event 两间: " + " / ".join(EVENT_ROOMS) + "\n"
        "  mind  两间: " + " / ".join(MIND_ROOMS) + "\n"
        "怎么判（两问）：\n"
        "  SELF/WORLD   ← 这事我在场吗？亲历→SELF；听说、看到→WORLD\n"
        "  TRAITS/VIEWS ← 这句在说人（我是什么样的），还是在说我怎么看一件事\n"
        "（I/YOU 和 WHO/WHAT 已砍：每一条都是我的记忆，立场不在房间里；"
        "「关于谁」交给 subjects 字段）"
    )


def normalize_room(room) -> str:
    """读侧归一：旧十间的名字翻成新四间；已经是新名字的原样返回；不认识的返回空串。

    ⚠️ 只在**读**的路径上用（recall 的房间门、decay 名单、awaken 的池子、面板）。
    写的路径要的是 check_room()——旧名字必须拒，不能在这儿被悄悄接住。
    """
    r = str(room or "").strip()
    if not r:
        return ""
    if r in ALL_ROOMS:
        return r
    return LEGACY_ROOMS.get(r, "")


def is_mind_room(room) -> bool:
    """这条是不是认知（MIND 支）。新旧名字都认。

    🔴 别再写 `"/MIND/" in room` —— 新名字是 `MIND/TRAITS`，开头没有斜杠，
    那个字面判断会**静默返回 False**，把所有认知从「永不沉底」名单里踢出去。
    """
    return normalize_room(room) in MIND_ROOMS


def is_event_room(room) -> bool:
    """这条是不是事件（EVENT 支）。新旧名字都认。

    🔴 同上，别再写 `room.find("/EVENT/") > 0`。
    """
    return normalize_room(room) in EVENT_ROOMS


def room_matches(room, gate: str) -> bool:
    """一条记忆的 room 是否落在门 `gate` 里。gate 可以是完整房名或前缀（EVENT / MIND）。

    比对**在归一之后做**，所以老盘上的 `I/MIND/TRAITS` 也能被 `room="MIND"` 筛到。
    """
    r = normalize_room(room)
    g = str(gate or "").strip().rstrip("/")
    if not r or not g:
        return False
    return r == g or r.startswith(g + "/")


def check_gate(gate: str) -> str | None:
    """校验 recall 的房间门（完整名或前缀）。合法返回 None，否则返回错误信息。"""
    g = str(gate or "").strip().rstrip("/")
    if not g:
        return None
    if any(r == g or r.startswith(g + "/") for r in ALL_ROOMS):
        return None
    hint = ""
    if g in LEGACY_ROOMS or g in ("I", "YOU"):
        hint = ("\n（`I` / `YOU` 已经不是房间了——每一条都是我的记忆，"
                "立场不在房间结构里。想按「关于谁」筛，等 subjects 接上检索。）")
    return f"room 无效：{gate}\n{_rooms_help()}{hint}"


def check_room(room: str, kind: str) -> str | None:
    """校验 room 是否合法（**写侧**）。合法返回 None，不合法返回给调用方看的错误信息。

    ⚠️ 不许兜底成默认房间——兜底就是把「房间名乱编」这个病请回来。
    ⚠️ 也不许把旧十间悄悄翻译成新四间（理由见文件头「两副面孔」那段）。
    """
    room = (room or "").strip()
    if not room:
        return f"room 必填。\n{_rooms_help()}"

    if room in LEGACY_ROOMS:
        return (f"room 已退役：{room} —— 房间 2026-08-16 从十间收成四间。\n"
                f"这条现在该填 {LEGACY_ROOMS[room]}。\n"
                f"（`I`/`YOU` 那一维搬去了 subjects；`WHO`/`WHAT` 整个砍了——"
                f"大部分记忆既是人又是事，判不清说明判据不清。）\n{_rooms_help()}")

    if kind == "event":
        if room not in EVENT_ROOMS:
            return f"room 无效：{room}（kind=event 只认 EVENT 两间）\n{_rooms_help()}"
    elif kind == "mind":
        if room not in MIND_ROOMS:
            return f"room 无效：{room}（kind=mind 只认 MIND 两间）\n{_rooms_help()}"
    else:
        if room not in ALL_ROOMS:
            return f"room 无效：{room}\n{_rooms_help()}"
    return None
