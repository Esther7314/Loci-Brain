# -*- coding: utf-8 -*-
"""
tests/test_bigevent.py — the synchronous pure functions of periods / big events (core/_bigevent.py)

Only the few that **don't reach out for the global runtime** are tested here:
parse_span / fmt_span / is_big / _usable / first_line.
`covering()` needs `rt.bucket_mgr`, which is a job for another ticket; this file doesn't touch
it at all.

What these functions guard are the ones out of her nine rules of 2026-08-05 that can be checked
statically:
  Rule 3  Start and end are filled in one go, written into the existing `when` (`start..end`,
          an empty end = still running) — **no new fields**.
  Rule 5  Cover, don't delete: what's covered no longer takes a line of its own, but not one
          entry is lost and drilling in always reaches it.
  Rule 6  Periods run in parallel and may overlap: `covered_by` is a **list** (at midnight on
          8-17 she pulled back the single-value version).
  Rule 8  Use regrow to supersede, and the old version stays on file.
Plus the pathology named at the top of the file: **the more it looks like objective
information, the less it gets doubted** — so a span that can't be rendered is better left empty.
"""

from datetime import timedelta

from core import _bigevent as _b
from core import _when as _w


# ============================================================
# parse_span —— how start and end are read out of when
# ============================================================

def test_a_closed_spans_end_is_exclusive_so_the_whole_end_day_is_inside_it():
    # Criterion: the docstring states plainly that "the end is **exclusive** (a day has already
    # been added)". `..2026-08-05` means "up to and including 8-05"; in plain speech 8-05 is
    # **included**. Skip adding that day and every memory stored on 8-05 falls outside this
    # period — and 8-05 happens to be the last day of that stretch, usually also the most
    # important one.
    start, end = _b.parse_span({"when": "2026-07-31..2026-08-05"})
    assert start == _w.parse_date("2026-07-31")
    assert end == _w.parse_date("2026-08-05") + timedelta(days=1)
    # 23:59 on 8-05 is still inside the span (that is what the half-open interval [start, end) means)
    assert start <= _w.parse_stamp("2026-08-05T23:59:00+08:00") < end


def test_an_empty_end_means_still_running():
    # Criterion: rule 3 — "leave the end empty while it's still running".
    # Still-running has to be `None` rather than "today" or some far-off date: filling in a
    # concrete value amounts to declaring on her behalf that this stretch of days is over,
    # when this period is still supposed to cover whatever gets stored tomorrow.
    start, end = _b.parse_span({"when": "2026-08-17.."})
    assert start == _w.parse_date("2026-08-17")
    assert end is None


def test_start_and_end_live_only_in_when_and_no_new_field_is_eaten():
    # Criterion: the ⚠️ under rule 3 — "**no new fields**: start and end go into the existing
    # `when`". What she wanted was to replace a new mechanism with one that already exists.
    # Here we stuff in two plausible-looking new fields, and the result has to be identical to
    # not stuffing them in — whoever adds `start`/`end` support one day has to trip over this
    # first.
    with_stray_fields = _b.parse_span({"when": "2026-07-31..2026-08-05",
                                       "start": "2020-01-01", "end": "2030-12-31"})
    assert with_stray_fields == _b.parse_span({"when": "2026-07-31..2026-08-05"})


def test_a_legacy_buckets_single_date_starts_that_day_and_leaves_the_end_empty_as_still_running():
    # Criterion: the legacy-bucket compatibility written in the docstring — the `when` written
    # by hand before 8-05 was a single date. It has no end, so it can only be treated as still
    # running; treat it as "ended the same day" and the earliest period disappears on the spot.
    start, end = _b.parse_span({"when": "2026-08-05"})
    assert start == _w.parse_date("2026-08-05")
    assert end is None


def test_with_no_when_it_falls_back_to_created():
    # Criterion: the same line as above, "the start falls back to when/created".
    # A bucket whose start is None gets `continue`d straight past inside covering() — which is
    # to say the period ceases to exist. With a created available it should never get that far.
    start, end = _b.parse_span({"created": "2026-08-03T17:47:00"})
    assert start == _w.parse_stamp("2026-08-03T17:47:00")
    assert end is None


def test_it_only_gives_up_when_both_when_and_created_are_missing():
    # Criterion: when neither can be obtained it returns (None, None) and quietly sits the
    # round out, rather than inventing a start. An invented start would let this period cover
    # a stretch of days it never actually happened in — and inside recall it would look
    # exactly like the real thing (the pathology at the top of the file).
    assert _b.parse_span({}) == (None, None)
    assert _b.parse_span({"when": "", "created": ""}) == (None, None)


def test_start_and_end_must_be_complete_eight_digit_dates_half_a_date_is_not_a_span():
    # Criterion: `SPAN_RE` requires `\d{4}-\d{2}-\d{2}` at both ends.
    # What this nails down is "no half matches": a month range like `2026-08..2026-09` is not a
    # span, it must not reach the span path, it has to fall through to the fallback below —
    # rather than being computed as a mutilated span.
    start, end = _b.parse_span({"when": "2026-08..2026-09", "created": "2026-01-02T03:04:05"})
    assert end is None                                        # it was not taken for a closed span
    assert start == _w.parse_stamp("2026-01-02T03:04:05")     # it fell back to created


# ============================================================
# fmt_span —— the span as shown to a human
# ============================================================

def test_a_closed_span_is_shown_as_both_ends_joined_by_a_tilde():
    # Criterion: the short form meant for human eyes; leading zeros get eaten by `int()`, and
    # both ends are treated alike.
    # ⚠️ The example in the function's docstring says `7-31~8-05`, which does not match the
    #    implementation (the implementation gives `8-5`) — the implementation is self-consistent
    #    (both ends go through `int()`), it's the comment that doesn't match. Named in the
    #    handover report.
    #    What this assertion holds down is "both ends use the same format": whoever changes
    #    only one end one day turns this red.
    assert _b.fmt_span({"when": "2026-07-31..2026-08-05"}) == "7-31~8-5"


def test_a_still_running_period_is_shown_with_the_open_ended_marker_so_you_can_see_it_is_not_over():
    # Criterion: still-running and already-finished have to be distinguishable **at the level
    # of the eye**. Show both as a range and I will take a stretch of days that is still going
    # on for one that is already behind us.
    assert _b.fmt_span({"when": "2026-08-17.."}) == "8-17 起"


def test_a_legacy_buckets_single_date_is_also_shown_with_the_open_ended_marker():
    # Criterion: consistent with parse_span — a legacy bucket has no end, so it is still
    # running. If the display layer and the computation layer drew different conclusions from
    # the same meta, what's on screen wouldn't match what's actually being covered.
    assert _b.fmt_span({"when": "2026-08-05"}) == "8-5 起"


def test_when_the_span_cannot_be_produced_it_gives_an_empty_string_rather_than_inventing_one():
    # Criterion: the pathology at the top of the file — "the more it looks like objective
    # information, the less it gets doubted". If the span can't be obtained, show nothing at
    # all; better one line short than showing a wrong span that looks very much like the truth.
    assert _b.fmt_span({}) == ""
    assert _b.fmt_span({"when": ""}) == ""
    assert _b.fmt_span({"when": "2026-08"}) == ""     # only down to the month, not enough for a day


# ============================================================
# is_big —— is this entry a period
# ============================================================

def test_a_tagged_bucket_is_recognized_even_mixed_in_among_other_tags():
    # Criterion: a period is recognized by one special tag, and a real bucket's tags also
    # contain a pile of words put there by deepseek. Anything written to look only at the first
    # tag would miss most of the real buckets.
    assert _b.is_big({"tags": ["moving-house", _b.BIGEVENT_TAG, "memory-system"]})


def test_anything_untagged_is_never_a_period():
    # Criterion: this is a **whitelist** check. Loosen it and ordinary events get treated as
    # periods and cover a whole stretch of days — one small memory posing as a main thread is
    # much worse than missing a main thread.
    assert not _b.is_big({})
    assert not _b.is_big({"tags": None})
    assert not _b.is_big({"tags": []})
    assert not _b.is_big({"tags": ["moving-house", "memory-system"]})


def test_the_tag_is_matched_by_exact_equality_not_containment():
    # Criterion: `__大event__x` is not the period tag.
    # Written as `any(BIGEVENT_TAG in t)`, any tag that **contains** that string could pose as
    # the period tag.
    assert not _b.is_big({"tags": ["__大event__x"]})
    assert not _b.is_big({"tags": ["prefix__大event__"]})


def test_non_strings_mixed_into_the_tags_do_not_blow_it_up():
    # Criterion: tags are backfilled by deepseek, so their shape is not entirely under control.
    # This check runs bucket by bucket inside covering()'s loop, and blowing up on one means
    # no periods at all for the whole round — one piece of dirty data should not carry off
    # every main thread.
    assert _b.is_big({"tags": [123, None, _b.BIGEVENT_TAG]})


# ============================================================
# _usable —— which periods should stop surfacing on their own
# ============================================================

def test_a_clean_meta_is_usable_by_default():
    # Criterion: this is a **blacklist** — only what has been explicitly superseded, covered,
    # closed, or archived gets excluded. Write it the other way round as a whitelist and old
    # buckets with incomplete fields disappear en masse.
    assert _b._usable({})
    assert _b._usable({"status": "", "type": "", "covered_by": None})


def test_the_old_version_of_something_superseded_stops_surfacing():
    # Criterion: rule 8 — regrow supersedes, and the old version stays on file.
    # On file means it is searchable and reachable by drilling in, but it **must not go on
    # covering that stretch of days**: with the old and new versions both surfacing, the screen
    # shows two main threads contradicting each other.
    assert not _b._usable({"superseded_by": "abc123"})


def test_a_layer_covered_by_one_above_it_stops_surfacing():
    # Criterion: construction item 3 — a big event can itself be covered (recursively, with no
    # preset number of layers), the topmost is shown, and drilling in reaches the rest (2.4).
    # If what's been covered still surfaces, dividing things into layers was pointless.
    assert not _b._usable({"covered_by": ["outer-layer-id"]})


def test_an_empty_covered_by_list_means_nobody_is_covering_it():
    # Criterion: the point under rule 6 — `covered_by` is a **list** (at midnight on 8-17 she
    # pulled back the single-value version with her own hands).
    # An empty list = nobody has ever covered it, so it has to surface as usual.
    # 🔴 This one exists specifically to pin down the falsy-fallback disease (this repo has
    #    been bitten before: on 8-17 we caught weight=0 being treated as unset):
    #    `if "covered_by" in meta` would treat an empty list as "it's been covered", so every
    #    period that was **once** covered and later uncovered would disappear en masse, with
    #    no error of any kind.
    assert _b._usable({"covered_by": []})


def test_a_soft_deleted_one_does_not_surface():
    # Criterion: `deleted_at` is a soft delete (looking it up by id always brings it back), but
    # being retrievable is not the same as deserving to go on covering the timeline.
    assert not _b._usable({"deleted_at": "2026-08-18T10:00:00"})


def test_resolved_and_abandoned_both_stop_surfacing_but_other_statuses_carry_on():
    # Criterion: there are only two endings, resolved / abandoned (the two `trace` offers).
    # Widen this set and periods still in progress get treated as finished; narrow it and
    # finished ones stay on the screen.
    assert not _b._usable({"status": "resolved"})
    assert not _b._usable({"status": "abandoned"})
    assert _b._usable({"status": "active"})
    assert _b._usable({"status": None})


def test_archived_does_not_surface_but_other_types_carry_on():
    # Criterion: only `type == "archived"` counts as archived.
    # Write this as a fuzzy match and ordinary event/mind buckets become collateral damage.
    assert not _b._usable({"type": "archived"})
    assert _b._usable({"type": "event"})


# ============================================================
# first_line —— that one line
# ============================================================

def test_only_the_first_line_is_wanted_the_lines_after_it_are_footnotes():
    # Criterion: the docstring — "that one line = the first line of the body; the lines after
    # it are left for footnotes such as 'the span / how it was drawn'". Take the whole body as
    # that one line and what gets laid over the top of recall is a great lump, when the entire
    # point of a period is **one** line saying "what we were doing back then".
    assert _b.first_line("Moving house + reshaping memory into four rooms\n"
                         "Span: 7-31 to 8-05\n"
                         "How it was drawn: …") == "Moving house + reshaping memory into four rooms"


def test_a_body_of_one_line_is_just_itself():
    # Criterion: the overwhelming majority of periods are one line with no footnotes. This path
    # must not break just because there is no newline to be found.
    assert _b.first_line("the stretch where we were reshaping memory") == "the stretch where we were reshaping memory"


def test_leading_blank_lines_are_skipped():
    # Criterion: strip first, then split into lines. If one extra newline at the front made it
    # return an empty string, this period would show up on screen as an entry with no name —
    # which is more baffling than not showing it at all.
    assert _b.first_line("\n\n  the stretch where we were moving house\nfootnote") == "the stretch where we were moving house"


def test_an_empty_body_gives_an_empty_string_without_blowing_up():
    # Criterion: `splitlines()[0]` raises IndexError on an empty string.
    # This function runs entry by entry over covering()'s results, and one empty body should
    # not carry off the whole round.
    assert _b.first_line("") == ""
    assert _b.first_line("   \n  \n") == ""


def test_windows_line_endings_count_as_line_breaks_too():
    # Criterion: it uses `splitlines()`, not `split("\n")`.
    # A body pasted in from elsewhere may carry `\r\n` — with split("\n") the first line would
    # have an `\r` hanging off its tail, which displays as an invisible dirty character.
    assert _b.first_line("first line\r\nsecond line") == "first line"


# ============================================================
# the constants
# ============================================================

def test_at_most_three_are_covered_at_once_not_one():
    # Criterion: rule 6 — "periods run in parallel and may overlap".
    # On 8-05, "moving house" and "reshaping memory" were two parallel main threads, and
    # forcing them into a single line loses something. So this ceiling **cannot be 1**; nor
    # should it be too large — cover a whole screen and it is no longer "covering with one
    # line".
    assert _b.COVER_MAX == 3


def test_the_period_tag_is_a_name_that_cannot_collide_with_a_real_tag():
    # Criterion: this tag shares one tags array with the tags deepseek assigns automatically.
    # The two underscores on either side are there to keep it apart from real tags — if it ever
    # became an ordinary word, then the day deepseek produced that same word, that ordinary
    # memory would be posing as a period.
    assert _b.BIGEVENT_TAG.startswith("__") and _b.BIGEVENT_TAG.endswith("__")
