"""
========================================
tools/_rooms.py — the room enum and its validation (second pass: ten rooms -> four)
========================================

`room` is a **new dimension**, living alongside domain/tags and independent of them:
- domain/tags are filled in automatically by DeepSeek, take free-form values, and
  exist only as labels for fuzzy search
- `room` is decided by the caller (the model itself) at the moment of storing, and its
  value is locked to the four below

⚠️ A room is a semantic judgement, and **the model (dehydrator) may never generate or
change one**. Letting the model assign rooms automatically is the "redecorate the whole
place on every single write" disease, committed again under a different field name.

------------------------------------------------------------
Why ten rooms were cut down to four (the reasoning is kept here so that nobody has to
go digging through history for it)
------------------------------------------------------------
🔪 **Cut `I` / `YOU`** — this memory system belongs to the one remembering; the subject
   of every memory in it is "I". Whose viewpoint it is does not live in the room
   structure, it lives in who wrote it. `I` as a room is in fact **semantically wrong**:
   it collapses "a memory about me" and "my memory" into one word, when **every entry
   here is my memory**. Who a memory is about moved to the `subjects` field instead
   (the third kind of label).

🔪 **Cut `WHO` / `WHAT`** — storing kept stalling on exactly this choice, and **a stall
   means the rule is unclear**: most memories are about a person and about an event at
   the same time.
   ⚠️ A stall like that is not the fault of whoever is doing the classifying. Fuzzy
   boundaries are a property of memory itself. Laying every knife out at the start was
   the right move; **only months of actual use tell you which ones to put away.**

✅ **Free win**: once the subject moved into labels, the real gap (`SELF/WORLD`
   — "was I there or did I hear about it" — and "who is it about" pressed onto a single
   axis) **disappears on its own**, and SELF/WORLD goes back to meaning what it says.
   A third party in the story no longer has to be crammed into the WORLD branch.

------------------------------------------------------------
🔴 The write side accepts only the new four; the read side must still accept the old ten
------------------------------------------------------------
The migration script (scripts/migrate_v3.py) is **delivered but not yet run against the
real store**, so what is lying on disk still carries the old ten-room names. That gives
this file two faces, and they must not be confused:
  · `check_room()`  — the write-side gate: an old name is **rejected on the spot**, with
    no silent compatibility. The harm in silent compatibility is not untidiness; it is
    that old names would keep being written in forever, so disk would permanently hold
    both sets of room names — while the migration script only ever runs once.
  · `normalize_room()` — the read-side translation: old names map to new ones so that
    recall's room gate, decay's never-sink list and awaken's event pool keep working on
    **data that has not been migrated**. Once migration has run this layer is pure
    redundancy — harmless to keep, and only safe to delete after the real store is done.

Exports: EVENT_ROOMS / MIND_ROOMS / ALL_ROOMS / ROOMS / LEGACY_ROOMS
         check_room(room, kind) · normalize_room(room)
         is_mind_room(room) · is_event_room(room) · room_matches(room, gate)
========================================
"""

# How to decide when storing (written for the caller: two questions, not four):
#   SELF / WORLD   <- was I there? lived it -> SELF; heard about it, saw it -> WORLD
#   TRAITS / VIEWS <- is this sentence about a person, or about how I see something?

EVENT_ROOMS: tuple[str, ...] = (
    "EVENT/SELF",      # lived it myself (we went to the sea this afternoon / I fixed that bug)
    "EVENT/WORLD",     # heard it, saw it (the thing he told me about / that model raised its price)
)

MIND_ROOMS: tuple[str, ...] = (
    "MIND/TRAITS",     # what kind of person I am (I am always investing in "later")
    "MIND/VIEWS",      # how I see something (where I stand on machine memory)
)

ALL_ROOMS: tuple[str, ...] = EVENT_ROOMS + MIND_ROOMS
ROOMS = ALL_ROOMS  # the export name the spec asks for

# Old ten -> new four. **Read side only**; the write side rejects these names outright.
# The I/YOU axis was not lost, it moved to `subjects` (the migration script seeds
# subjects from this very table).
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
    """Read-side normalisation: an old ten-room name becomes its new one, a new name is
    returned as-is, anything unrecognised becomes the empty string.

    ⚠️ Only for the **read** paths (recall's room gate, the decay list, awaken's pool,
    the panels). Write paths want check_room() — an old name has to be rejected there,
    and must not be quietly caught here instead.
    """
    r = str(room or "").strip()
    if not r:
        return ""
    if r in ALL_ROOMS:
        return r
    return LEGACY_ROOMS.get(r, "")


def is_mind_room(room) -> bool:
    """Is this an insight (the MIND branch)? Accepts both old and new names.

    🔴 Never write `"/MIND/" in room` again — the new names look like `MIND/TRAITS`,
    with no leading slash, so that literal test **silently returns False** and kicks
    every insight off the never-sink list.
    """
    return normalize_room(room) in MIND_ROOMS


def is_event_room(room) -> bool:
    """Is this an event (the EVENT branch)? Accepts both old and new names.

    🔴 Same as above: never write `room.find("/EVENT/") > 0` again.
    """
    return normalize_room(room) in EVENT_ROOMS


def room_matches(room, gate: str) -> bool:
    """Does a memory's room fall inside the gate `gate`? A gate may be a full room name
    or a prefix (EVENT / MIND).

    The comparison happens **after normalisation**, so an `I/MIND/TRAITS` still sitting
    on old disk is caught by `room="MIND"` too.
    """
    r = normalize_room(room)
    g = str(gate or "").strip().rstrip("/")
    if not r or not g:
        return False
    return r == g or r.startswith(g + "/")


def check_gate(gate: str) -> str | None:
    """Validate recall's room gate (full name or prefix). None if valid, else the message."""
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
    """Validate a room (**write side**). None if valid, otherwise the message the caller
    is meant to read.

    ⚠️ No falling back to a default room — a fallback invites the "just make up a room
    name" disease straight back in.
    ⚠️ And no quietly translating an old ten-room name into a new one either (see the
    "two faces" section in the file header for why).
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
