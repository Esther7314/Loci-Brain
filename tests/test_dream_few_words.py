# -*- coding: utf-8 -*-
"""
tests/test_dream_few_words.py — the dream's "few words" pass the same gate as its other
ingredients.

The words are drawn from tags; a tag is a word out of a memory's body. A memory whose source
was withdrawn, one kept out of sight on purpose, or one an avoid-hold covers must not lend a
word to a dream any more than its body — and the dream record names which memories the
words came from, so a later withdrawal can find the dream.
"""

from core import _dream as D
from core import _when as W


def _cfg():
    c = D.dream_config({})
    c["word_n"] = 10
    c["word_pool_top"] = 100
    return c


def _rec(bid, tag, **meta):
    return ({"id": bid, "room": "EVENT/SELF", "tags": [tag], "created": "2026-09-01T10:00:00",
             **meta}, f"a day with {tag} in it")


def test_only_words_the_dream_may_see_are_drawn_and_their_sources_named():
    now = W.now()
    recs = [
        _rec("aaaaaaaaaaaa", "sea"),
        _rec("bbbbbbbbbbbb", "cave", dont_surface=True),
        _rec("cccccccccccc", "gone",
             invalidation=[{"kind": "source_gone", "of": "x", "at": "2026-09-02"}]),
        _rec("dddddddddddd", "fog"),
        ({"id": "eeeeeeeeeeee", "room": "EVENT/SELF", "direction_of_fit": "telic",
          "exception_of": "dddddddddddd", "hold": "avoid", "tags": [],
          "created": "2026-09-01T10:00:00"}, "not now"),
    ]
    words, sources = D.few_words(recs, _cfg(), now=now)
    assert words == ["sea"]
    assert sources == ["aaaaaaaaaaaa"]
