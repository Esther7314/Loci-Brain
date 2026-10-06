"""
========================================
tools/grow/_cards.py — name cards: card_of
========================================

grow_mind files a card through check_card; trace sets and moves cards through
check_card and card_room_rule, by way of tools/grow/rooms_path.

Exports: check_card(card_of, room, exclude) · card_room_rule(name, room) · live_card(name)
========================================
"""

from .. import _runtime as rt
from .._common import read_scope
from core._rooms import is_mind_room
from .. import _subjects as _S


# ------------------------------------------------------------
# Name cards: card_of
# ------------------------------------------------------------
# A card is one MIND entry filed as the card of a name: how I see that person, or that
# game, book, group. What the name is (person or thing) is the names table's call; the
# card only follows it, and says nothing when the table does not know yet.

_CARD_NAME_MAX = 64      # the store's cap on one name (bucket_manager._MAX_SUBJECT_CHARS)


def card_room_rule(name: str, room: str) -> tuple[str, str]:
    """Which room a card of `name` may sit in. Returns (refusal, note).

    A card is a mind. A person's card is MIND/TRAITS and a thing's MIND/VIEWS, once
    the names table says which the name is; while it does not, either MIND room is
    taken and the note says the table does not know yet — nothing is guessed from
    the card itself."""
    if not is_mind_room(room):
        return ("名字卡是一条 mind：人的卡放 MIND/TRAITS（这个人是什么样的），"
                "东西的卡放 MIND/VIEWS（我怎么看它）。"), ""
    person = _S.is_person(name)
    if person is None:
        return "", f"人名表里还不知道「{name}」是什么（人还是东西），先按 {room} 收下。"
    if person and room != "MIND/TRAITS":
        return f"「{name}」在人名表里是人，人的卡放 MIND/TRAITS。", ""
    if not person and room != "MIND/VIEWS":
        kind = _S.kind_of(name)
        what = f"是{kind}" if kind else "记着不是人"
        return f"「{name}」在人名表里{what}，东西的卡放 MIND/VIEWS（我怎么看它）。", ""
    return "", ""


async def live_card(name: str, exclude: str = "") -> str:
    """The id of `name`'s live card, or "". Live = in the active store and not replaced
    by a live newer version; a stored card_of is read through the table as it is now,
    so a card filed under a spelling that has since become an alias still counts.

    Under a read scope only what the request may read counts — the card, and the newer
    version that would replace it: a card out of scope is not there, so it neither
    blocks a card of the request's own nor gets named. A name may then hold two live
    cards; every reader of cards takes the newest one it may see."""
    want = name.lower()
    view = await read_scope()
    for b in await rt.bucket_mgr.list_all(include_archive=False):
        meta = b.get("metadata") or {}
        bid = str(meta.get("id") or b.get("id") or "")
        card = str(meta.get("card_of") or "").strip()
        if not card or bid == exclude or meta.get("deleted_at"):
            continue
        if view is not None and not view.permits(meta):
            continue
        newer = str(meta.get("superseded_by") or "").strip()
        if newer and rt.bucket_mgr.is_live(newer) and (view is None or view.permits_id(newer)):
            continue
        if (_S.name_key(card) or card).lower() == want:
            return bid
    return ""


async def check_card(card_of, room: str, exclude: str = "") -> tuple[str, str, str]:
    """The card_of argument -> (the name to store, a note for the reply, refusal). An
    empty argument is no card. `exclude` is the entry being changed, so trace can set
    the card an entry already is.

    The name is stored as the table's key for it (an alias spelling files under the
    entry it stands for). One live card per name: a second is refused, naming the one
    there is."""
    raw = str(card_of or "").strip()
    if not raw:
        return "", "", ""
    if "\n" in raw or len(raw) > _CARD_NAME_MAX:
        return "", "", f"card_of 是一个名字（最多 {_CARD_NAME_MAX} 字，不换行）。"
    name = _S.name_key(raw)
    if not name:
        return "", "", f"card_of 写名字，不写「{raw}」——卡是某个名字的卡。"
    owners = _S.candidates(raw)
    if len(owners) > 1 and name not in owners:
        return "", "", (f"「{raw}」在人名表里挂在好几个名字底下（{'、'.join(owners)}）"
                        "——card_of 写其中那一个的规范名。")
    room_err, note = card_room_rule(name, room)
    if room_err:
        return "", "", room_err
    existing = await live_card(name, exclude=exclude)
    if existing:
        return "", "", (f"「{name}」已经有名字卡了：{existing}。一个名字一张卡——"
                        f'改写它用 regrow(bucket_id="{existing}", ...)；真要换一张，'
                        f'先 trace(bucket_id="{existing}", card_of="") 把那张摘掉。')
    return name, note, ""
