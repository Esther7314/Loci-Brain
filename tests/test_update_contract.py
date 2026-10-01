# -*- coding: utf-8 -*-
"""CONTRACT: update() — the one gate every mutation walks through.

WHAT THIS FREEZES AND WHY IT IS FROZEN NOW
    `_update_locked` is ~430 lines and 119 branches, and every write in the system goes
    through it: trace, plan, anchor, the dehydrator's backfill, regrow, fold, the decay
    loop. Four historical bugs in this repo share one shape — **no error, only the thing
    that should have happened did not happen** (a field silently dropped from the
    whitelist, importance stuck at 10 after unpinning, the anchor cap bypassed, the
    forgetting clock reset by a metadata edit). None of those raise; only assertions
    can see them.

    So this file asserts input shape -> the outcome that must hold, always through the
    public `update()` / `get()` surface. It never counts calls and never peeks at which
    branch ran: everything inside may be rewritten and this file must stay green.

WHY THIS ONE TOUCHES THE DISK
    The claims are about what lands in (or moves between) the vault's directories —
    that layout is part of the public Obsidian contract. Everything runs under
    `tmp_path`, all data is synthetic, and no embedding engine is attached.
"""
import asyncio

import pytest

from core.bucket_manager import BucketManager

BODY = "an ordinary synthetic memory body, nothing personal in here"


@pytest.fixture
def store(tmp_path):
    return BucketManager({"buckets_dir": str(tmp_path)})


def run(coro):
    return asyncio.run(coro)


async def _meta(store, bid) -> dict:
    return (await store.get(bid))["metadata"]


def _files_under(tmp_path, subdir, bid):
    return list((tmp_path / subdir).rglob(f"*{bid}*.md"))


# ───────────────────────── existence and terminal states ─────────────────────────

def test_updating_an_unknown_id_reports_failure_not_success(store):
    async def go():
        # Criterion: a False here is the only thing telling the caller the write was
        # lost. A True would let trace/plan report success while nothing reached disk.
        assert await store.update("no-such-id", tags=["x"]) is False
    run(go())


def test_a_deleted_bucket_refuses_ordinary_updates(store, tmp_path):
    async def go():
        bid = await store.create(BODY, tags=["t"])
        assert await store.delete(bid) is True
        # Criterion: archive is a terminal lifecycle state. An ordinary update slipping
        # through could resurrect the file into the active tree (pinned=True forces
        # type=permanent), turning a tombstone back into a living memory.
        assert await store.update(bid, pinned=True) is False
        archived = _files_under(tmp_path, "archive", bid)
        assert len(archived) == 1
        assert "pinned: true" not in archived[0].read_text(encoding="utf-8")
    run(go())


# ───────────────────────── booleans at the storage boundary ─────────────────────────

def test_the_string_false_is_stored_as_false(store):
    async def go():
        bid = await store.create(BODY)
        assert await store.update(bid, resolved="false") is True
        # Criterion: YAML/JSON callers send quoted booleans. Python's bool("false") is
        # True; persisting that would resolve a want the caller explicitly kept open.
        assert (await _meta(store, bid))["resolved"] is False
    run(go())


def test_a_string_that_is_not_a_boolean_is_rejected_not_guessed(store):
    async def go():
        bid = await store.create(BODY)
        with pytest.raises(ValueError):
            await store.update(bid, resolved="maybe")
        # Criterion: guessing either way would silently flip a lifecycle flag. The
        # memory must be left exactly as it was.
        assert "resolved" not in await _meta(store, bid)
    run(go())


def test_the_string_false_does_not_pin(store):
    async def go():
        bid = await store.create(BODY)
        assert await store.update(bid, pinned="false") is True
        meta = await _meta(store, bid)
        # Criterion: same trap, higher stakes — a truthy "false" would promote the
        # bucket to permanent and lock importance at 10.
        assert meta.get("pinned") in (False, None)
        assert meta["type"] != "permanent"
    run(go())


# ───────────────────────── pinned and type decide each other ─────────────────────────

def test_pinning_promotes_to_permanent_and_locks_importance(store, tmp_path):
    async def go():
        bid = await store.create(BODY, importance=4)
        assert await store.update(bid, pinned=True) is True
        meta = await _meta(store, bid)
        assert meta["pinned"] is True
        assert meta["type"] == "permanent"
        assert meta["importance"] == 10
        # Criterion: type is a physical-storage decision too. Metadata saying
        # "permanent" while the file sits under dynamic/ would break the Obsidian
        # layout contract and every path-based scan.
        assert len(_files_under(tmp_path, "permanent", bid)) == 1
        assert _files_under(tmp_path, "dynamic", bid) == []
    run(go())


def test_pinning_while_asking_for_an_incompatible_type_is_refused_whole(store):
    async def go():
        bid = await store.create(BODY, importance=4)
        assert await store.update(bid, pinned=True, type="dynamic") is False
        meta = await _meta(store, bid)
        # Criterion: refused means refused — not "pinned landed and type did not".
        # A half-applied call would leave a pinned bucket in the dynamic tree.
        assert meta.get("pinned") is None
        assert meta["importance"] == 4
    run(go())


def test_a_pinned_bucket_refuses_a_type_change_away_from_permanent(store):
    async def go():
        bid = await store.create(BODY, pinned=True)
        assert await store.update(bid, type="dynamic") is False
        assert (await _meta(store, bid))["type"] == "permanent"
    run(go())


def test_unpinning_demotes_to_dynamic_and_importance_comes_down_with_it(store, tmp_path):
    async def go():
        bid = await store.create(BODY, pinned=True)
        assert await store.update(bid, pinned=False) is True
        meta = await _meta(store, bid)
        assert meta["type"] == "dynamic"
        # Criterion: the regression this repo already lived through (bug 4). Unpinning
        # used to leave importance at 10, dropping the bucket into the importance>=9
        # pool at full marks with no entry point anywhere to bring it back down.
        assert meta["importance"] < 9
        assert len(_files_under(tmp_path, "dynamic", bid)) == 1
    run(go())


def test_unpinning_with_an_explicit_importance_keeps_that_importance(store):
    async def go():
        bid = await store.create(BODY, pinned=True)
        assert await store.update(bid, pinned=False, importance=6) is True
        # Criterion: the atomic quota-safe unpin. The final state is not pinned, so the
        # caller's number must win over both the lock at 10 and the default demotion.
        assert (await _meta(store, bid))["importance"] == 6
    run(go())


def test_importance_on_a_bucket_that_stays_pinned_is_ignored(store):
    async def go():
        bid = await store.create(BODY, pinned=True)
        assert await store.update(bid, importance=3) is True
        # Criterion: pinned means locked at 10. If this went through, a background
        # importance sweep could quietly demote a core rule.
        assert (await _meta(store, bid))["importance"] == 10
    run(go())


def test_an_explicitly_permanent_memory_survives_pinned_false(store, tmp_path):
    async def go():
        bid = await store.create(BODY, bucket_type="permanent")
        assert await store.update(bid, pinned=False) is True
        # Criterion: demotion is for buckets that WERE pinned. A memory made permanent
        # on purpose (never pinned) must not be dragged into dynamic by a no-op unpin.
        assert (await _meta(store, bid))["type"] == "permanent"
        assert len(_files_under(tmp_path, "permanent", bid)) == 1
    run(go())


def test_an_unsupported_type_is_refused_and_nothing_is_written(store):
    async def go():
        bid = await store.create(BODY, tags=["keep"])
        assert await store.update(bid, type="archived", tags=["gone"]) is False
        assert await store.update(bid, type="garbage") is False
        meta = await _meta(store, bid)
        # Criterion: "archived" reached through update() would be a delete that skips
        # the tombstone bookkeeping. And the refusal must be whole: the tags passed
        # alongside must not have landed either.
        assert meta["type"] == "dynamic"
        assert meta["tags"] == ["keep"]
    run(go())


def test_a_protected_bucket_refuses_leaving_permanent_but_may_enter_it(store):
    async def go():
        bid = await store.create(BODY, protected=True)
        assert await store.update(bid, type="permanent") is True
        assert (await _meta(store, bid))["type"] == "permanent"
        assert await store.update(bid, type="feel") is False
        assert (await _meta(store, bid))["type"] == "permanent"
    run(go())


def test_a_protected_bucket_keeps_importance_ten(store):
    async def go():
        bid = await store.create(BODY, protected=True)
        assert await store.update(bid, importance=2) is True
        assert (await _meta(store, bid))["importance"] == 10
    run(go())


def test_unpinning_a_protected_bucket_does_not_demote_it(store):
    async def go():
        bid = await store.create(BODY, pinned=True, protected=True)
        assert await store.update(bid, pinned=False) is True
        meta = await _meta(store, bid)
        # Criterion: protected outranks the unpin demotion. Dropping a protected
        # bucket to dynamic/importance 8 would put it back into decay's reach.
        assert meta["type"] == "permanent"
        assert meta["importance"] == 10
    run(go())


# ───────────────────────── normalization on the write path ─────────────────────────

def test_tags_are_deduplicated_and_empties_dropped(store):
    async def go():
        bid = await store.create(BODY)
        await store.update(bid, tags=["a", "a", "", "  ", "b"])
        assert (await _meta(store, bid))["tags"] == ["a", "b"]
    run(go())


def test_a_lone_string_tag_becomes_a_one_element_list(store):
    async def go():
        bid = await store.create(BODY)
        await store.update(bid, tags="solo")
        # Criterion: a bare string iterated as a list would store one tag per
        # character, and every one of them would "appear literally in the body".
        assert (await _meta(store, bid))["tags"] == ["solo"]
    run(go())


def test_an_empty_alias_list_removes_the_field(store):
    async def go():
        bid = await store.create(BODY)
        await store.update(bid, aliases=["x", "y"])
        assert (await _meta(store, bid))["aliases"] == ["x", "y"]
        await store.update(bid, aliases=[])
        # Criterion: clearing means the frontmatter row disappears, not an empty list
        # left behind for the BM25 side to keep indexing.
        assert "aliases" not in await _meta(store, bid)
    run(go())


def test_subjects_are_normalized_like_the_other_label_kinds(store):
    async def go():
        bid = await store.create(BODY)
        await store.update(bid, subjects=["Es", "Es", "", "X"])
        # Criterion: the dehydrator's backfill comes through this exact path.
        assert (await _meta(store, bid))["subjects"] == ["Es", "X"]
    run(go())


def test_out_of_range_scores_are_clamped_not_stored(store):
    async def go():
        bid = await store.create(BODY)
        await store.update(bid, importance=99, valence=1.5, arousal=-3)
        meta = await _meta(store, bid)
        # Criterion: every scoring dimension multiplies these. One 99 on disk would
        # dominate ranking forever and no later read re-validates stored values.
        assert meta["importance"] == 10
        assert meta["valence"] == 1.0
        assert meta["arousal"] == 0.0
    run(go())


def test_unparseable_scores_fall_back_to_neutral(store):
    async def go():
        bid = await store.create(BODY)
        await store.update(bid, importance="not-a-number", valence="junk")
        meta = await _meta(store, bid)
        assert meta["importance"] == 5
        assert meta["valence"] == 0.5
    run(go())


def test_control_and_bidi_characters_never_reach_the_body(store):
    async def go():
        bid = await store.create(BODY)
        await store.update(bid, content="a\u202eb\x00c\x01d")
        # Criterion: a bidi override in a Markdown file flips how every later reader
        # renders the line (F-04). Newlines and tabs stay; the invisibles must not.
        assert (await store.get(bid))["content"] == "abcd"
    run(go())


def test_oversized_content_is_refused_and_the_old_body_survives(tmp_path):
    async def go():
        bm = BucketManager({"buckets_dir": str(tmp_path),
                            "limits": {"max_bucket_bytes": 200}})
        bid = await bm.create(BODY)
        with pytest.raises(ValueError):
            await bm.update(bid, content="x" * 201)
        # Criterion: the refusal must come BEFORE anything touches the file. A cap
        # enforced after a partial write would trade "too big" for "truncated".
        assert (await bm.get(bid))["content"] == BODY
    run(go())


def test_the_content_cap_counts_bytes_not_characters(tmp_path):
    async def go():
        bm = BucketManager({"buckets_dir": str(tmp_path),
                            "limits": {"max_bucket_bytes": 10}})
        bid = await bm.create("ok")
        # Four CJK characters are 12 UTF-8 bytes: under a 10-byte cap they must be
        # refused even though len() says 4.
        with pytest.raises(ValueError):
            await bm.update(bid, content="好" * 4)
    run(go())


def test_a_cap_of_zero_means_no_cap(tmp_path):
    async def go():
        bm = BucketManager({"buckets_dir": str(tmp_path),
                            "limits": {"max_bucket_bytes": 0}})
        bid = await bm.create("ok")
        assert await bm.update(bid, content="x" * 5000) is True
        assert (await bm.get(bid))["content"] == "x" * 5000
    run(go())


def test_long_free_text_fields_are_truncated_to_their_caps(store):
    async def go():
        bid = await store.create(BODY)
        await store.update(bid,
                           summary="s" * 500,
                           room="r" * 200,
                           when="w" * 100,
                           closed_by="c" * 80)
        meta = await _meta(store, bid)
        # Criterion: these caps are what keeps the frontmatter from bloating; a model
        # having a fit can hand over kilobytes in any of them.
        assert len(meta["summary"]) == 200
        assert len(meta["room"]) == 64
        assert len(meta["when"]) == 32
        assert len(meta["closed_by"]) == 50
    run(go())


# ───────────── the whitelist regressions: fields that once vanished silently ─────────────

def test_closing_a_want_actually_records_who_closed_it(store):
    async def go():
        bid = await store.create(BODY, direction_of_fit="telic", weight=0.5)
        assert await store.update(bid, status="resolved", closed_by="Es",
                                  last_asked="2026-08-01") is True
        meta = await _meta(store, bid)
        # Criterion: the historical hole — both fields were assembled by trace_core and
        # silently discarded here because they were missing from the pass-through list.
        # The close button reported success while not one character reached disk.
        assert meta["closed_by"] == "Es"
        assert meta["last_asked"] == "2026-08-01"
        assert meta["status"] == "resolved"
    run(go())


def test_last_dreamt_reaches_disk(store):
    async def go():
        bid = await store.create(BODY)
        assert await store.update(bid, last_dreamt="2026-08-20") is True
        # Criterion: the same trap sprung again within the hour it was documented.
        # Ingredient selection discounts by this field; dropped, every night weaves
        # the same dream.
        assert (await _meta(store, bid))["last_dreamt"] == "2026-08-20"
    run(go())


def test_invalidation_reaches_disk(store):
    async def go():
        bid = await store.create(BODY)
        first = {"kind": "overturn", "of": "aaaaaaaaaaaa", "by": "bbbbbbbbbbbb",
                 "at": "2026-10-01T02:00:00"}
        assert await store.update(bid, invalidation=[first]) is True
        # Criterion: the same trap a fourth time. regrow's overturn marks every
        # descendant through this field; dropped here, an overturned basis leaves no
        # trace anywhere and every descendant reads as standing on solid ground.
        assert (await _meta(store, bid))["invalidation"] == [first]
        second = dict(first, by="cccccccccccc", at="2026-10-02T02:00:00")
        assert await store.update(bid, invalidation=[first, second]) is True
        assert (await _meta(store, bid))["invalidation"] == [first, second]
        # A record without a kind says nothing; an empty list clears the field.
        assert await store.update(bid, invalidation=[{"of": "x"}]) is True
        assert "invalidation" not in await _meta(store, bid)
    run(go())


def test_prov_reaches_disk(store):
    async def go():
        bid = await store.create(BODY)
        lines = [{"rel": "wasDerivedFrom", "target": "aaaaaaaaaaaa"},
                 {"rel": "wasQuotedFrom", "target": "m_0931"}]
        assert await store.update(bid, prov=lines) is True
        # Criterion: the same trap a fifth time. Every source a memory names goes
        # through this field; dropped here, a thought reads as made up out of nothing.
        assert (await _meta(store, bid))["prov"] == lines
        # A bad line refuses the whole write instead of reaching disk in part.
        assert await store.update(bid, prov=lines + [{"rel": "inspiredBy", "target": "x"}]) is False
        assert (await _meta(store, bid))["prov"] == lines
        # An empty list clears the field.
        assert await store.update(bid, prov=[]) is True
        assert "prov" not in await _meta(store, bid)
    run(go())


def test_cue_and_hold_fields_reach_disk(store):
    async def go():
        bid = await store.create(BODY)
        cue = {"condition": "the exam is over", "phrasings": ["after the exam"]}
        assert await store.update(bid, cue=cue, exception_of="aaaaaaaaaaaa", hold="defer",
                                  review_after="2026-10-13") is True
        # Criterion: the same trap a sixth time. A hold's two halves and its review day
        # go through these fields; dropped here, a hold is an ordinary want that nags.
        meta = await _meta(store, bid)
        assert meta["cue"] == cue
        assert (meta["exception_of"], meta["hold"], meta["review_after"]) == \
            ("aaaaaaaaaaaa", "defer", "2026-10-13")
        # Half a hold is refused whole; a value outside its shape too.
        assert await store.update(bid, hold="") is False
        assert await store.update(bid, hold="later") is False
        assert await store.update(bid, review_after="2026-02-30") is False
        assert await store.update(bid, cue={"phrasings": ["x"]}) is False
        assert (await _meta(store, bid))["hold"] == "defer"
        # Empty clears; both halves of the hold go together.
        assert await store.update(bid, cue="", exception_of="", hold="",
                                  review_after="") is True
        meta = await _meta(store, bid)
        assert not {"cue", "exception_of", "hold", "review_after"} & set(meta)
    run(go())


def test_none_deletes_a_field_instead_of_writing_the_word_none(store):
    async def go():
        bid = await store.create(BODY)
        await store.update(bid, _pre_anchor_source_tool="grow")
        assert (await _meta(store, bid))["_pre_anchor_source_tool"] == "grow"
        await store.update(bid, _pre_anchor_source_tool=None)
        # Criterion: the anchor-release path clears its temporary field this way.
        # A literal None/null left in frontmatter would read back as a real value.
        assert "_pre_anchor_source_tool" not in await _meta(store, bid)
    run(go())


# ───────────────────────── meaning: append versus replace ─────────────────────────

def test_meaning_append_accumulates_in_order(store):
    async def go():
        bid = await store.create(BODY, meaning="origin")
        await store.update(bid, meaning_append="second")
        await store.update(bid, meaning_append="third")
        # Criterion: every hold appends. An overwrite here would erase the record of
        # all the earlier moments this memory was touched.
        assert (await _meta(store, bid))["meaning"] == ["origin", "second", "third"]
    run(go())


def test_meaning_replace_overwrites_and_empty_clears(store):
    async def go():
        bid = await store.create(BODY, meaning="origin")
        await store.update(bid, meaning=["a", "a"])
        # No dedup on purpose: the same sentence at two different moments is itself
        # information.
        assert (await _meta(store, bid))["meaning"] == ["a", "a"]
        await store.update(bid, meaning=[])
        assert "meaning" not in await _meta(store, bid)
    run(go())


# ───────────────────────── the anchor cap ─────────────────────────

def test_the_anchor_cap_refuses_the_bucket_over_the_limit(store):
    async def go():
        store.ANCHOR_LIMIT = 3
        ids = [await store.create(f"{BODY} {i}") for i in range(4)]
        for bid in ids[:3]:
            assert await store.update(bid, anchor=True) is True
        # Criterion: the cap is the whole point of anchor being scarce. The update()
        # fallback path used to bypass it, so a bulk script could push past 24.
        assert await store.update(ids[3], anchor=True) is False
        assert "anchor" not in await _meta(store, ids[3])
    run(go())


def test_re_anchoring_an_anchored_bucket_at_the_cap_still_succeeds(store):
    async def go():
        store.ANCHOR_LIMIT = 2
        ids = [await store.create(f"{BODY} {i}") for i in range(2)]
        for bid in ids:
            assert await store.update(bid, anchor=True) is True
        # Criterion: only a False->True transition counts against the cap. A migration
        # re-stamping existing anchors must not be locked out by the quota it fills.
        assert await store.update(ids[0], anchor=True) is True
        assert (await _meta(store, ids[0]))["anchor"] is True
    run(go())


def test_anchor_false_removes_the_field(store):
    async def go():
        bid = await store.create(BODY)
        await store.update(bid, anchor=True)
        await store.update(bid, anchor=False)
        assert "anchor" not in await _meta(store, bid)
    run(go())


def test_pinning_clears_the_anchor_mark(store):
    async def go():
        bid = await store.create(BODY)
        await store.update(bid, anchor=True)
        assert await store.update(bid, pinned=True) is True
        meta = await _meta(store, bid)
        # Criterion: pinned means "always surfaces", anchor means "deliberately does
        # not". Both at once would make a core rule the model can never keep down.
        assert meta["pinned"] is True
        assert "anchor" not in meta
    run(go())


# ───────────────────────── the forgetting clock ─────────────────────────

def test_a_metadata_edit_does_not_reset_the_forgetting_clock(store):
    async def go():
        bid = await store.create(BODY)
        before = await _meta(store, bid)
        await store.update(bid, tags=["edited"], summary="a line")
        after = await _meta(store, bid)
        # Criterion: last_active means "genuinely recalled", and decay's recency score
        # reads it. If trace/plan/backfill refreshed it, routine bookkeeping would
        # keep every touched memory eternally young.
        assert after["last_active"] == before["last_active"]
        assert int(after.get("activation_count") or 0) == int(before.get("activation_count") or 0)
    run(go())


def test_bump_active_counts_as_one_genuine_activation(store):
    async def go():
        bid = await store.create(BODY)
        before = int((await _meta(store, bid)).get("activation_count") or 0)
        assert await store.update(bid, tags=["merged"], bump_active=True) is True
        after = await _meta(store, bid)
        assert int(after["activation_count"]) == before + 1
        assert after["last_active"]
    run(go())


# ───────────────────────── type changes move the file ─────────────────────────

def test_changing_type_to_feel_moves_the_file_and_the_id_still_resolves(store, tmp_path):
    async def go():
        bid = await store.create(BODY)
        assert await store.update(bid, type="feel") is True
        assert len(_files_under(tmp_path, "feel", bid)) == 1
        assert _files_under(tmp_path, "dynamic", bid) == []
        # Criterion: a move that loses the id->path mapping makes the very next
        # update() on this bucket silently return False — the lost-write shape.
        assert (await store.get(bid))["content"] == BODY
    run(go())


def test_changing_domain_relocates_within_the_dynamic_tree(store, tmp_path):
    async def go():
        bid = await store.create(BODY, domain=["old-place"])
        assert await store.update(bid, domain=["new-place"]) is True
        assert len(list((tmp_path / "dynamic" / "new-place").glob(f"*{bid}*.md"))) == 1
        assert list((tmp_path / "dynamic" / "old-place").glob(f"*{bid}*.md")) == []
        assert (await store.get(bid))["content"] == BODY
    run(go())


def test_an_emptied_domain_falls_back_to_the_default(store):
    async def go():
        bid = await store.create(BODY, domain=["somewhere"])
        assert await store.update(bid, domain=[]) is True
        # Criterion: an empty domain list on a dynamic bucket has no folder to live
        # in; the default placeholder is what keeps the path computable.
        assert (await _meta(store, bid))["domain"] == ["未分类"]
    run(go())


# ───────────────────────── the write itself ─────────────────────────

def test_a_content_update_changes_the_body_and_only_the_body(store):
    async def go():
        bid = await store.create(BODY, tags=["keep"], importance=7)
        assert await store.update(bid, content="a new body") is True
        b = await store.get(bid)
        assert b["content"] == "a new body"
        # Criterion: fields not named in the call do not move.
        assert b["metadata"]["tags"] == ["keep"]
        assert b["metadata"]["importance"] == 7
    run(go())
