# -*- coding: utf-8 -*-
"""Every place that states this project's version must state the same one.

WHY THIS FILE EXISTS
    There are two VERSION files, and that is deliberate:

        <root>/VERSION   what a reader, the changelog and a release look at
        src/VERSION      what the RUNTIME reads, and it is read FIRST

    src/ wins on purpose. The update path only unpacks `src/` and `frontend/`, so
    `src/VERSION` is guaranteed fresh, while a root VERSION can sit untouched from
    whenever someone first installed. Root-first once made a user's displayed version go
    *backwards* after an update, which is why the order is what it is.

    The cost of that design is one instruction: **when cutting a release, bump both.**

    That instruction was written down clearly, in the docstring of the function that reads
    them — and it was still missed twice in a row. Two releases went out while the runtime
    kept announcing 1.0.0: the startup log, `server.__version__`, and anything asking the
    server what it was, all reported a version from two releases earlier.

    🔴 Nothing broke, and that is the point. Every bug report from that period carried a
       version number that was not the version being run. An instruction that has been
       obeyed zero times out of two is not an instruction any more; it is a wish. This
       file is what replaces it.

    Found by an external review, not by us — which is itself the argument for the test:
    the one thing nobody re-reads is the number they are sure about.
"""
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def test_both_version_files_exist():
    # Criterion: this suite must fail loudly if a file moves, rather than pass by finding
    # nothing to compare. A consistency check over one file is not a consistency check.
    assert (ROOT / "VERSION").is_file()
    assert (ROOT / "src" / "VERSION").is_file(), (
        "src/VERSION is the one the runtime reads first — if it is gone, the runtime is "
        "silently falling back to something else")


def test_the_two_version_files_agree():
    # Criterion: THE assertion. Two files holding the same fact is a design decision with
    # a real reason behind it, and the price of that decision is exactly this check.
    root_version = _read(ROOT / "VERSION")
    src_version = _read(ROOT / "src" / "VERSION")
    assert root_version == src_version, (
        f"<root>/VERSION says {root_version} and src/VERSION says {src_version}. "
        f"src/ is what the runtime reports, so the running system would announce "
        f"{src_version} while the changelog and the release say {root_version}. "
        f"Bump both.")


@pytest.mark.parametrize("relative", ["VERSION", "src/VERSION"])
def test_each_version_is_a_version(relative):
    # Criterion: an empty or malformed file would make the readers fall through to a
    # placeholder, and "0.0.0+unknown" in a bug report is only marginally better than a
    # wrong number.
    value = _read(ROOT / relative)
    assert SEMVER.match(value), f"{relative} holds {value!r}, which is not a version"


def test_the_runtime_reads_the_same_number():
    # Criterion: the files agreeing is not enough — what matters is the number the running
    # system says out loud. This is the exact function the startup log and
    # `server.__version__` go through.
    from utils import get_version
    assert get_version() == _read(ROOT / "src" / "VERSION")


def test_the_runtime_falls_back_to_the_same_number(monkeypatch):
    # Criterion: get_version is the one reader, and it has two strategies — src/VERSION
    # first, the root file when that cannot be read. The root strategy must land on the
    # same number too; two strategies is precisely how the two files drifted apart
    # without anyone noticing.
    import builtins
    from utils import get_version
    real_open = builtins.open
    src_file = os.path.normcase(str(ROOT / "src" / "VERSION"))

    def without_src_version(path, *args, **kwargs):
        if os.path.normcase(os.path.abspath(str(path))) == src_file:
            raise FileNotFoundError(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", without_src_version)
    assert get_version() == _read(ROOT / "VERSION")


def test_the_changelog_documents_the_current_version():
    # Criterion: shipping a version with no entry means whoever upgrades cannot find out
    # what changed. The changelog is the only place that answers that.
    version = _read(ROOT / "VERSION")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## {version}" in changelog, (
        f"CHANGELOG.md has no section for {version} — either the version was bumped "
        f"without writing down what changed, or the heading format moved")
