# -*- coding: utf-8 -*-
"""
tests/test_rooms.py — the whitelist of the four rooms (core/_rooms.py)

The overall criterion this file guards is the "two faces" paragraph at the top of `_rooms.py`:
  · **Write side** (check_room) rejects the old ten rooms **on the spot**, with no silent
    compatibility — the harm of silent compatibility is not untidiness, it is that we would
    go on storing under the old names forever, and the disk would permanently hold two sets
    of room names at once.
  · **Read side** (normalize_room / room_matches) has to recognize the old ten rooms — the
    migration script was only handed over, never run against the real store, so what is
    lying on disk is still the old names.
These two point in opposite directions, so every assertion below has to say which side it
stands on.
"""

from core import _rooms


# ============================================================
# normalize_room —— the read side's translation
# ============================================================

def test_normalize_returns_the_four_new_rooms_untouched():
    # Criterion: a new name goes in and comes out with **not one character changed**.
    # The normalize layer is a bridge built for old data, not a place where new data gets
    # processed; if it helpfully rewrote a new name (adding a prefix, changing the case),
    # every downstream equality comparison made on the room name would start missing.
    for room in _rooms.ALL_ROOMS:
        assert _rooms.normalize_room(room) == room


def test_normalize_translates_all_ten_legacy_rooms_and_every_landing_is_a_valid_new_room():
    # Criterion: the LEGACY_ROOMS table is **the only lifeline unmigrated old data has**.
    # One entry missing, or one that translates to a room name that does not exist, and that
    # batch of old memories disappears simultaneously from recall's room gate, from decay's
    # never-sink list, and from awaken's event pool.
    # So walk it entry by entry, and require every landing to be one of the four rooms
    # (no translating into a misspelled name).
    assert len(_rooms.LEGACY_ROOMS) == 10          # ten rooms collapsed into four, so the map should hold ten entries
    for legacy, new in _rooms.LEGACY_ROOMS.items():
        assert _rooms.normalize_room(legacy) == new
        assert new in _rooms.ALL_ROOMS


def test_normalize_gives_an_empty_string_for_unrecognized_names_and_never_falls_back():
    # Criterion: if it can't recognize the name it says so, and **must not fall back to some
    # default room**. Falling back invites the "made-up room name" disease right back in —
    # a made-up name would be quietly filed under a real room, that memory would sit in the
    # wrong room from then on, and nothing anywhere would report an error.
    assert _rooms.normalize_room("EVENT/BEACH") == ""
    assert _rooms.normalize_room("something i just made up") == ""


def test_normalize_gives_an_empty_string_for_empty_values_without_blowing_up():
    # Criterion: this is a read path, and what gets fed in is **old data off the disk** —
    # early buckets may have no room field at all. A missing field has to quietly become an
    # empty string; it must not raise and take the whole recall down with it.
    assert _rooms.normalize_room(None) == ""
    assert _rooms.normalize_room("") == ""
    assert _rooms.normalize_room("   ") == ""


def test_normalize_eats_leading_and_trailing_whitespace():
    # Criterion: a space that came in by hand-copying or pasting shouldn't drop a memory out
    # of its room.
    assert _rooms.normalize_room("  MIND/TRAITS  ") == "MIND/TRAITS"
    assert _rooms.normalize_room("\tI/MIND/VIEWS\n") == "MIND/VIEWS"


def test_normalize_does_not_fall_back_on_case():
    # Criterion: room names are a **locked enum**, not free text.
    # Accepting lowercase here would be admitting that "case doesn't matter", and two spellings
    # would start appearing on disk, while every place that compares room names for equality
    # (is_mind_room / room_matches) only recognizes one of them.
    assert _rooms.normalize_room("mind/traits") == ""
    assert _rooms.normalize_room("Event/Self") == ""


# ============================================================
# is_mind_room / is_event_room
# ============================================================

def test_is_mind_room_recognizes_new_names_even_without_a_leading_slash():
    # Criterion: this is exactly the 🔴 in the function's docstring — stop writing
    # `"/MIND/" in room`. The new name is `MIND/TRAITS`, which has **no leading slash**, so
    # that kind of literal check silently returns False and kicks every mind entry off the
    # never-sink list (no error, they are just gone one day).
    assert _rooms.is_mind_room("MIND/TRAITS")
    assert _rooms.is_mind_room("MIND/VIEWS")


def test_is_mind_room_recognizes_legacy_names_too():
    # Criterion: this is the read side, and the disk still holds the old ten rooms. Old mind
    # entries have to stay on the list just the same.
    assert _rooms.is_mind_room("I/MIND/TRAITS")
    assert _rooms.is_mind_room("YOU/MIND/VIEWS")


def test_the_two_branches_are_mutually_exclusive_event_is_not_mind_and_mind_is_not_event():
    # Criterion: EVENT and MIND are two different things — "what happened" and "what I
    # thought" — and no single entry can be both. These two checks feed decay and the waking
    # screen, so mixing up one entry means mixing up a whole batch.
    for room in _rooms.EVENT_ROOMS:
        assert _rooms.is_event_room(room) and not _rooms.is_mind_room(room)
    for room in _rooms.MIND_ROOMS:
        assert _rooms.is_mind_room(room) and not _rooms.is_event_room(room)


def test_a_bare_prefix_is_not_a_room():
    # Criterion: these two functions eat **a memory's room field**, which is always a full
    # room name; `"MIND"` is a gate used for searching, not a room. Sharing one check between
    # the two kinds of thing would let half a room name count as a legitimate memory room,
    # which is the same as leaving a back door open for "room name written halfway".
    # (Note this is a different standard from room_matches / check_gate — those two are the
    #  ones that eat prefix gates.)
    assert not _rooms.is_mind_room("MIND")
    assert not _rooms.is_event_room("EVENT")


def test_branch_checks_return_False_for_empty_values_and_garbage_alike():
    # Criterion: an old bucket with the field missing has to land on "neither", rather than
    # raising or being counted into one of the branches.
    for value in (None, "", "  ", "EVENT/BEACH"):
        assert not _rooms.is_mind_room(value)
        assert not _rooms.is_event_room(value)


# ============================================================
# room_matches —— the prefix gate
# ============================================================

def test_room_prefix_gate_MIND_admits_only_the_two_mind_rooms():
    # Criterion: `room="MIND"` is a **prefix gate** — both mind rooms should be let in, and
    # not one of the two event rooms may leak through. Write the prefix gate too loosely and
    # search will stir "what happened" and "what I thought" into the same pot.
    assert _rooms.room_matches("MIND/TRAITS", "MIND")
    assert _rooms.room_matches("MIND/VIEWS", "MIND")
    assert not _rooms.room_matches("EVENT/SELF", "MIND")
    assert not _rooms.room_matches("EVENT/WORLD", "MIND")


def test_room_prefix_gate_EVENT_admits_only_the_two_event_rooms():
    # Criterion: same as above, and it has to hold in the other direction too — the gates for
    # the two branches must be symmetric. Test only one side and nobody finds out when the
    # other side leaks.
    assert _rooms.room_matches("EVENT/SELF", "EVENT")
    assert _rooms.room_matches("EVENT/WORLD", "EVENT")
    assert not _rooms.room_matches("MIND/TRAITS", "EVENT")
    assert not _rooms.room_matches("MIND/VIEWS", "EVENT")


def test_a_full_room_name_used_as_the_gate_is_an_exact_match():
    # Criterion: give the gate in full and only that one room gets in. If `room="MIND/TRAITS"`
    # still let VIEWS through, "filter down to the finest grain" would no longer exist — and
    # not being able to filter finely is the same as not having four rooms at all.
    assert _rooms.room_matches("MIND/TRAITS", "MIND/TRAITS")
    assert not _rooms.room_matches("MIND/VIEWS", "MIND/TRAITS")


def test_legacy_names_on_the_old_disk_are_still_caught_by_the_new_prefix_gate():
    # Criterion: the function's docstring states plainly that "the comparison **happens after
    # normalization**". This is where the whole read-side compatibility story lands: the real
    # store has not been migrated, so searching with a new name has to find old memories,
    # otherwise everything stored before 8-16 vanishes from recall all at once.
    assert _rooms.room_matches("I/MIND/TRAITS", "MIND")
    assert _rooms.room_matches("YOU/EVENT/SELF/WHAT", "EVENT")
    assert _rooms.room_matches("I/EVENT/WORLD/WHO", "EVENT/WORLD")


def test_half_a_word_is_not_a_prefix():
    # Criterion: the prefix gate is cut **by path segment**, not by string start.
    # Written as a bare startswith, `MIND/TRAIT` would match `MIND/TRAITS`, and a gate with
    # one letter typo would silently filter out a pile of things with no sign of where it
    # went wrong.
    assert not _rooms.room_matches("MIND/TRAITS", "MIND/TRAIT")
    assert not _rooms.room_matches("EVENT/SELF", "EVEN")


def test_a_trailing_slash_on_the_gate_makes_no_difference():
    # Criterion: `MIND/` and `MIND` mean the same thing, and one slash typed by accident
    # shouldn't filter down to nothing — an empty result looks exactly like "there really
    # isn't anything", which is the hardest kind of error to notice.
    assert _rooms.room_matches("MIND/TRAITS", "MIND/")
    assert _rooms.room_matches("EVENT/SELF", "EVENT/SELF/")


def test_an_empty_gate_admits_nothing_the_caller_is_responsible_for_checking_first():
    # Criterion: an empty gate here means "no gate was given", and this function only answers
    # "does it fall inside the gate" — no gate to fall inside → False. ⚠️ Note that
    # check_gate("") considers an empty gate **valid** (= no room filter), so the caller has
    # to make the check itself: `if room and not room_matches(...)`. Anyone who drops the
    # first half and uses room_matches as the filter outright will make a search with no gate
    # at all return nothing.
    # This assertion nails that division of labour down here, so it can't quietly change.
    assert not _rooms.room_matches("MIND/TRAITS", "")
    assert not _rooms.room_matches("MIND/TRAITS", None)


def test_a_memory_with_no_room_of_its_own_matches_no_gate():
    # Criterion: a memory whose room name can't be recognized should not be counted into any
    # room — if it can't be placed, don't place it; forcing it into some room is worse than
    # leaving it outside the gate.
    assert not _rooms.room_matches(None, "MIND")
    assert not _rooms.room_matches("EVENT/BEACH", "EVENT")


# ============================================================
# check_gate —— recall's room-gate validation
# ============================================================

def test_the_gate_may_be_left_out_which_means_no_room_filter():
    # Criterion: the room is an **optional** filter. Leaving it out has to be valid (returns
    # None), otherwise "I just want to search everything" — the most travelled path there is —
    # gets blocked.
    assert _rooms.check_gate("") is None
    assert _rooms.check_gate(None) is None
    assert _rooms.check_gate("   ") is None


def test_the_gate_takes_both_a_prefix_and_a_full_room_name():
    # Criterion: when searching, both "I want to see all my mind entries" and "I want to see
    # MIND/TRAITS" are legitimate needs, so both the coarse and the fine setting have to pass.
    for gate in ("EVENT", "MIND", "EVENT/SELF", "MIND/VIEWS", "MIND/"):
        assert _rooms.check_gate(gate) is None


def test_a_mistyped_gate_errors_on_the_spot_instead_of_silently_returning_nothing():
    # Criterion: a wrongly written gate has to **raise an error**; it must not silently filter
    # down to zero results. Zero results looks exactly like "there really isn't anything", and
    # the person searching will take it for the latter and conclude "I never recorded that" —
    # which is the worst mistake a memory system can make.
    for gate in ("MIND/TRAIT", "EVENT/BEACH", "mind", "something i just made up"):
        err = _rooms.check_gate(gate)
        assert err is not None
        assert "room 无效" in err


def test_I_or_YOU_as_a_gate_is_rejected_and_told_where_it_moved_to():
    # Criterion: `I`/`YOU` were cut on 8-16 — every entry is my memory, and standpoint doesn't
    # live in the room structure. But whatever gets cut **has to leave a forwarding address**:
    # say only "invalid" and I will write it again next time.
    # (The assertion picks wording that **appears only in that forwarding note** — the ordinary
    #  help text about the four rooms also mentions subjects, so grepping for "subjects" alone
    #  would go green for the wrong reason.)
    for gate in ("I", "YOU"):
        err = _rooms.check_gate(gate)
        assert err is not None
        assert "已经不是房间了" in err
    err = _rooms.check_gate("I/MIND/TRAITS")
    assert err is not None and "已经不是房间了" in err


def test_the_error_message_must_list_all_four_rooms():
    # Criterion: this layer has **no fallback**, so the error message is the only way out —
    # and an error that doesn't carry "then what should I write?" leaves the caller (me) with
    # nothing but guessing, and the guess will be yet another wrong name.
    err = _rooms.check_gate("something i just made up")
    for room in _rooms.ALL_ROOMS:
        assert room in err


# ============================================================
# check_room —— the write side's sluice gate
# ============================================================

def test_write_side_kind_event_accepts_only_the_two_event_rooms():
    # Criterion: kind and room have to line up. An event that lands in a MIND room gets
    # treated as a mind entry (never sinks, joins the pool for fold) when it is really just
    # a thing that happened — the wrong things stay forever and the right ones get squeezed
    # out.
    assert _rooms.check_room("EVENT/SELF", "event") is None
    assert _rooms.check_room("EVENT/WORLD", "event") is None
    assert _rooms.check_room("MIND/TRAITS", "event") is not None


def test_write_side_kind_mind_accepts_only_the_two_mind_rooms():
    # Criterion: the other direction has to be blocked just the same. A mind entry that lands
    # in an EVENT room decays and sinks along with the events.
    assert _rooms.check_room("MIND/TRAITS", "mind") is None
    assert _rooms.check_room("MIND/VIEWS", "mind") is None
    assert _rooms.check_room("EVENT/SELF", "mind") is not None


def test_write_side_room_is_required_and_an_empty_one_is_rejected_on_the_spot():
    # Criterion: the docstring states plainly "no falling back to a default room".
    # If it isn't filled in, stop and ask; don't pick a room on the caller's behalf — the room
    # you pick for it will be wrong every time.
    for value in ("", "   ", None):
        err = _rooms.check_room(value, "event")
        assert err is not None
        assert "必填" in err


def test_write_side_rejects_the_ten_legacy_rooms_on_the_spot_but_spells_out_which_one_to_use():
    # Criterion: this is where the "two faces" paragraph at the top of the file lands — old
    # names **must be rejected** on the write side, not quietly translated the way the read
    # side does. Quiet translation means I keep storing under the old names, so the disk
    # permanently holds two sets of room names while the migration script only ever ran once.
    # And rejecting isn't enough: it has to hand back the new room name, otherwise this gate
    # only ever shuts people out.
    # (The assertion picks the whole sentence "这条现在该填 X" — the help text about the four
    #  rooms also prints MIND/TRAITS, so searching for the room name alone would go green for
    #  the wrong reason and tell you nothing about whether **this particular entry** got a
    #  forwarding address.)
    err = _rooms.check_room("I/MIND/TRAITS", "mind")
    assert err is not None
    assert "已退役" in err
    assert "这条现在该填 MIND/TRAITS" in err
    # The read side still translates the same name — it's only right when both faces hold at once
    assert _rooms.normalize_room("I/MIND/TRAITS") == "MIND/TRAITS"


def test_write_side_takes_no_prefix_the_room_name_must_be_written_in_full():
    # Criterion: `MIND` is a gate for searching, not a room you can store into.
    # Allowing a prefix when storing is allowing a memory not to pick a side — and picking a
    # side is exactly the judgement the moment of storing is for.
    assert _rooms.check_room("MIND", "mind") is not None
    assert _rooms.check_room("EVENT", "event") is not None


def test_write_side_accepts_all_four_rooms_when_kind_is_neither_of_the_two():
    # Criterion: fold and trace pass `kind=""` (changing a room or folding doesn't branch on
    # it). This path must not turn into "accepts anything" — it still accepts only these four
    # rooms, it just stops additionally requiring alignment with kind.
    for room in _rooms.ALL_ROOMS:
        assert _rooms.check_room(room, "") is None
    assert _rooms.check_room("EVENT/BEACH", "") is not None


def test_write_side_does_not_fall_back_on_case():
    # Criterion: same root as the normalize case — room names are a locked enum.
    # If the write side accepted lowercase, two spellings would be on disk immediately, and
    # the read side's normalize_room doesn't recognize lowercase, so that whole batch of
    # memories would fall out of their rooms at read time.
    assert _rooms.check_room("event/self", "event") is not None
    assert _rooms.check_room("Mind/Traits", "mind") is not None


def test_write_side_treats_surrounding_whitespace_as_not_an_error():
    # Criterion: an extra space typed by accident is a slip of the hand, not a mistaken
    # judgement. This is the one kind of "invalid" that deserves leniency — it doesn't create
    # a second way of spelling a room name.
    assert _rooms.check_room("  EVENT/SELF  ", "event") is None


# ============================================================
# the exported constants
# ============================================================

def test_the_four_rooms_are_locked_there_is_no_fifth():
    # Criterion: "four rooms" is not a description, it is a **locked enum** (the first line at
    # the top of the file). What this assertion is for: whoever adds a fifth room one day has
    # to trip over it here first, rather than finding out three months of storage later that
    # the rooms have grown back into ten.
    assert _rooms.ALL_ROOMS == ("EVENT/SELF", "EVENT/WORLD", "MIND/TRAITS", "MIND/VIEWS")
    assert _rooms.ROOMS is _rooms.ALL_ROOMS       # ROOMS is only an exported alias, it must not be a second copy
    assert len(_rooms.EVENT_ROOMS) == 2 and len(_rooms.MIND_ROOMS) == 2
