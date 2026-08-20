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


def test_attribute_access_on_a_chinese_field_is_not_a_binding():
    # Criterion: reading someone else's Chinese attribute is not this file's problem to
    # fix; only names this file itself introduces are.
    decl, other = CE._python_names("y = obj.某个字段\n")
    assert (decl, other) == (set(), set())


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
    # flagged this as the thing to watch when two people edit it at once.
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
    assert CE.SELF.replace("\\", "/").endswith("scripts/check_english.py")
    r = CE.scan()
    assert not any("check_english" in row for row in r["names"] + r["identifiers"])
