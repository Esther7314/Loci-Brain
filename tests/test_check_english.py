# -*- coding: utf-8 -*-
"""The ruler that decides "is the translation finished" has to be checked itself.

WHY THIS FILE EXISTS
    `scripts/check_english.py` is the finish line for turning this codebase into English.
    The whole reason it exists is that the previous attempt ended with someone's memory
    saying "done" over 77 Chinese function names. Replacing memory with a command only
    helps if the command is right — an unchecked ruler just moves the false confidence
    one level up, and makes it harder to see.

    Two ways it can be wrong, and they are not symmetric:

      undercounting  it reports 0 while Chinese names are still there. The translation
                     gets declared finished and stays unfinished. This already nearly
                     happened: the counter originally saw only def/class/function, so it
                     was about to print 0 over 412 remaining Chinese parameters,
                     variables and exported constants.

      overcounting   it flags things that must NEVER be translated — the field names in
                     the dream files on disk, the keys the panel reads, the Chinese in a
                     prompt that gets shown to a model. Work done to satisfy an
                     overcounting ruler actively destroys data.

    Overcounting is the worse of the two, which is why most of this file is about it.

    This is also the reason the Python side is parsed with `ast` rather than matched with
    a regex: a parser cannot mistake a string, a comment or a dict key for a name. That
    is not a performance choice, it is the correctness of the whole measurement.
"""
import importlib.util
from pathlib import Path

import pytest

_CHECKER = Path(__file__).resolve().parent.parent / "scripts" / "check_english.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_english", _CHECKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CE = _load()


# ───────────────── must never be counted (counting these destroys data) ─────────────────

def test_dict_keys_are_not_names():
    # Criterion: `rec["完整"]` is the frontmatter key of every dream file already on disk,
    # and `out["删了"]` is a field the panel reads. Flagging them would send someone to
    # rename them, which orphans her existing data and breaks the API in one move.
    decl, other = CE._python_names('rec["完整"] = "碎片"\nout = {"删了": [], "留痕": []}\n')
    assert (decl, other) == (set(), set())


def test_string_contents_are_not_names():
    # Criterion: THE case that nearly bit during the first batch. This line is a prompt
    # shown to a model, and `唤醒(` in it is the word "arousal" — visually identical to a
    # call of the function that was being renamed. A word-boundary regex would have
    # rewritten it, and no test anywhere would have gone red.
    decl, other = CE._python_names('s = "v=效价(0难受~1开心) a=唤醒(0平静~1强烈)"\n')
    assert (decl, other) == (set(), set())


def test_comments_are_not_names():
    # Criterion: comments are counted separately and never enforced, because no automated
    # check can tell a comment that was translated from one that was deleted.
    decl, other = CE._python_names("# 这一行是注释，里面有中文\nx = 1\n")
    assert (decl, other) == (set(), set())


def test_attribute_reads_are_counted_too():
    # Criterion: this one reverses an earlier decision, and the reversal was earned.
    #
    # The first version counted bindings only, on the reasoning that reading someone
    # else's attribute is not ours to fix. Then a field rename proved the reasoning
    # backwards: renaming `Cluster.架v` made the binding count drop to zero **while two
    # other files still read `x.架v`**. Everything was green — pyflakes cannot see a
    # misspelled attribute, and no test had ever built one of those objects — and the
    # first real call would have raised.
    #
    # Counting only definitions reports a rename as finished the moment it is started.
    _, other = CE._python_names("y = obj.某个字段\n")
    assert other == {".某个字段"}


def test_an_ascii_attribute_is_not_counted():
    _, other = CE._python_names("y = obj.some_field\n")
    assert other == set()


# ───────────────── must be counted (missing these is how a third round happens) ─────────────────

@pytest.mark.parametrize("source,expected", [
    ("料 = 1\n带数 = 2\n", {"料", "带数"}),
    ("def f(强制, 重试=3):\n    pass\n", {"强制", "重试"}),
    ("def f(*额外, **其余):\n    pass\n", {"额外", "其余"}),
    ("import os as 系统\n", {"系统"}),
    ("try:\n    pass\nexcept ValueError as 错:\n    pass\n", {"错"}),
    ("for 条 in items:\n    pass\n", {"条"}),
    ("with open('f') as 档:\n    pass\n", {"档"}),
])
def test_every_binding_form_is_counted(source, expected):
    # Criterion: each of these is a name a stranger has to read. The list is written out
    # form by form because the failure mode is silent: a form nobody thought of simply
    # does not appear in the total, and the total is what gets believed.
    _, other = CE._python_names(source)
    assert other == expected


def test_declarations_and_other_names_are_kept_apart():
    # Criterion: the two counters mean different things — one is the API surface a
    # stranger types, the other is everything they read. Collapsing them would hide which
    # of the two is finished.
    decl, other = CE._python_names("def 扫一遍(条):\n    结果 = 1\n    return 结果\n")
    assert decl == {"扫一遍"}
    assert other == {"条", "结果"}


def test_a_class_counts_as_a_declaration():
    decl, other = CE._python_names("class 团:\n    pass\n")
    assert decl == {"团"} and other == set()


def test_ascii_names_are_never_counted():
    decl, other = CE._python_names("def sweep(x):\n    y = 1\n    return y\n")
    assert (decl, other) == (set(), set())


def test_unparseable_python_does_not_raise():
    # Criterion: the checker walks the whole tree, and one broken file must not stop the
    # count — a crash reads as "something is wrong with the code", not "the ruler died".
    assert CE._python_names("def (((:\n") == (set(), set())


# ───────────────── the JavaScript side is a floor, and says so ─────────────────

def test_js_declaration_forms_are_counted():
    decl, other = CE._js_names(
        'function 贴一次() {}\nconst 默认地址 = "x";\nlet 端口 = 1;\nclass 架 {}\n')
    assert decl == {"贴一次", "架"}
    assert other == {"默认地址", "端口"}


def test_js_exported_constants_are_the_ones_that_matter_most():
    # Criterion: `强档关键词` is not internal — it is a name a stranger types after
    # `require(...)`. It is exactly what the first version of this checker could not see.
    _, other = CE._js_names("const 强档关键词 = [];\nmodule.exports = { 强档关键词 };\n")
    assert "强档关键词" in other


def test_js_attribute_reads_are_deliberately_not_counted():
    # Criterion: this pins a decision that looks like an oversight, so nobody "fixes" it.
    #
    # The Python side counts attribute reads, and has to. Doing the same for JS was tried
    # and flagged 45 things, every one of them a Chinese object KEY that must never be
    # renamed — the /health response fields, the cross-process ledger keys, the test
    # doubles' method names. Without a parser, `x.收到` cannot be told apart from a field.
    #
    # 🔴 100% false positives, on exactly the category where acting on the report destroys
    #    data. Undercounting leaves work undone; overcounting sends someone to do work that
    #    must not be done. When only one of the two is available, take the first.
    _, other = CE._js_names("const a = x.收到;\nfake.清账();\n")
    assert other == set()


def test_js_strings_are_not_mistaken_for_declarations():
    # Criterion: the JS side has no parser, so this is the boundary of what it can claim.
    # It matches declaration keywords only — a Chinese word inside a string has no
    # `const`/`function` in front of it and cannot be picked up.
    decl, other = CE._js_names('const label = "这是中文文案";\n')
    assert (decl, other) == (set(), set())


# ───────────────── the baseline in the file has to match the repo ─────────────────

def test_the_baseline_is_not_below_reality():
    # Criterion: the ratchet's whole value is that the number in the file is the number in
    # the repo. A baseline that drifted UPWARD would silently loosen the ratchet, and one
    # that drifted downward would go red for no reason. Both agents in the first batch
    # flagged this as the thing to watch when two people edit it at onCE.
    r = CE.scan()
    now = {
        "identifiers": len(r["identifiers"]),
        "names": len(r["names"]),
        "filenames": len(r["filenames"]),
        "she": r["she_total"],
    }
    for key, value in now.items():
        assert value <= CE.BASELINE[key], (
            f"{key} is {value} but the recorded baseline is {CE.BASELINE[key]} — "
            f"Chinese was added back into shipped code")


def test_the_checker_does_not_count_itself():
    # Criterion: the checker's first ever run reported ten personal mentions that were
    # its own pattern definitions. A detector that counts itself measures the detector.
    #
    # ⚠️ And the ratchet caught this very file for the same reason: the sentence above
    #    originally quoted the character it searches for, which pushed the count up by
    #    one. Writing the rule down is not exempt from the rule.
    #
    # 🔴 Third time, 2026-08-22 — and the third time is what changed the fix. Tests for
    #    the stamp table put six of the searched-for character into assertion messages,
    #    the checker counted them, and the command line and the test suite disagreed
    #    about the same number. Twice it was patched by rewording the sentence; that
    #    only works until someone writes a test, because a test **has** to contain what
    #    it tests. So the exemption now covers the detector AND its tests, and this
    #    assertion checks both are in there.
    _self = {s.replace("\\", "/") for s in CE.SELF}
    assert any(s.endswith("scripts/check_english.py") for s in _self)
    assert any(s.endswith("tests/test_check_english.py") for s in _self), (
        "the detector's tests must be exempt too — they cannot avoid quoting what they check"
    )
    r = CE.scan()
    assert not any("check_english" in row for row in r["names"] + r["identifiers"])


# ══════════════════════════════════════════════════════════════════════════════
# The stamps on the eight 她 that stay (DELIBERATE_SHE)
#
# WHY THESE EXIST
#     Getting to "0 mentions of 她" was not done by translating the last eight — it was
#     done by exempting them. That makes the exemption table part of the ruler, and an
#     exemption that keeps applying after its line changed is exactly the overcounting
#     failure's twin: the number reads 0 while nobody is looking at anything.
#
#     Both failure modes below were hit for real while writing the table on 2026-08-22:
#     the keys were built with os.path.join, came out backslashed on Windows, matched no
#     file at all — and **nothing said so**. The count simply stayed at 8 with an empty
#     stale list. A table that can silently apply to zero files is worse than no table.
# ══════════════════════════════════════════════════════════════════════════════

def test_a_stamp_exempts_its_own_line_only():
    rel = next(iter(CE.DELIBERATE_SHE))
    needle = CE.DELIBERATE_SHE[rel][0][0]
    exempt, stale = CE._stamped_she(rel, needle + "\n她 somewhere else\n")
    assert stale == []
    assert exempt == needle.count("她"), "only the stamped line is exempt"


def test_a_stamp_whose_line_changed_goes_stale_instead_of_staying_silent():
    rel = next(iter(CE.DELIBERATE_SHE))
    exempt, stale = CE._stamped_she(rel, "她 但是那一行已经被改掉了\n")
    assert exempt == 0, "a stamp that no longer matches must not exempt anything"
    assert len(stale) == len(CE.DELIBERATE_SHE[rel])
    assert "not found" in stale[0]


def test_a_stamp_that_matches_twice_is_stale_too():
    # Two matches means the substring stopped identifying which occurrence was blessed.
    # Exempting "one of them" would be a guess, so it refuses and says so.
    rel = next(iter(CE.DELIBERATE_SHE))
    needle = CE.DELIBERATE_SHE[rel][0][0]
    exempt, stale = CE._stamped_she(rel, needle + "\n" + needle + "\n")
    assert exempt == 0
    assert "found 2x" in stale[0]


def test_a_stamped_file_the_scan_never_saw_is_reported():
    """The one that actually happened: a path typo makes the whole table a no-op."""
    CE._STAMPED_FILES_SEEN.clear()
    assert set(CE._unused_stamp_files()) == set(CE.DELIBERATE_SHE)
    for rel in CE.DELIBERATE_SHE:
        CE._stamped_she(rel, "")
    assert CE._unused_stamp_files() == [], "visiting every stamped file clears the warning"


def test_the_stamp_keys_are_the_paths_the_scan_actually_produces():
    """Backslash keys match nothing on Windows and the table dies quietly."""
    for rel in CE.DELIBERATE_SHE:
        assert "\\" not in rel, f"{rel!r} must use forward slashes, like _rel() emits"
        assert (Path(CE.ROOT) / rel).is_file(), f"{rel} is stamped but does not exist"


def test_every_stamp_still_matches_the_real_file():
    """The live check: run the table against the actual sources, right now."""
    stale = []
    for rel in CE.DELIBERATE_SHE:
        text = (Path(CE.ROOT) / rel).read_text(encoding="utf-8")
        stale += CE._stamped_she(rel, text)[1]
    assert stale == [], "a stamp drifted off its line — re-read it before re-stamping"


def test_the_stamped_total_matches_what_the_ratchet_zeroed_out():
    # If this ever disagrees with BASELINE["she"] == 0, the finish line moved without
    # anyone deciding to move it.
    r = CE.scan()
    assert CE.BASELINE["she"] == 0
    assert r["she_total"] == 0
    assert r["stamped_she"] == 8, "eight stamped 她 — the owner ruled on exactly these"
    assert r["stale_stamps"] == []
