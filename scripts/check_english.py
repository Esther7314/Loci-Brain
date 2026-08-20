# -*- coding: utf-8 -*-
"""
check_english.py — the finish line for "the code that ships is in English"

    python scripts/check_english.py            # report + ratchet check
    python scripts/check_english.py --list     # also print every remaining offender
    python scripts/check_english.py --baseline # print the numbers as a BASELINE block

═══════════════════════════════════════════════════════════════════
Why this exists
═══════════════════════════════════════════════════════════════════
This is the second attempt at translating this codebase. The first one ended with
"I think it's done" and left 77 Chinese function names behind, and nobody found out
for weeks — because "did I finish?" was answered by a person's memory instead of by
a command.

So the finish line is a command, and the command is this file.

═══════════════════════════════════════════════════════════════════
A ratchet, not a gate — on purpose
═══════════════════════════════════════════════════════════════════
The honest thing to do the day you start a long translation is NOT to add a check
that fails, because a check that is red on day one gets skipped by day two, and then
it is decoration. So each counter carries a BASELINE, and the rule is:

    over the baseline  → red. Something went backwards.
    at or under it     → green, and it tells you to lower the baseline.

Lowering the baseline is a line in a diff, which means every batch of translation has
to state how far it got. When every counter reaches zero, the baseline is zero and
this stops being a ratchet and becomes an ordinary gate — with no code change needed.

═══════════════════════════════════════════════════════════════════
What counts as an offence, and what deliberately does not
═══════════════════════════════════════════════════════════════════
    identifiers   a def/function/class/const whose NAME contains Chinese. Always wrong
                  in shipped code: it is the thing a stranger has to type.
    filenames     same reasoning, one level up.
    personal      "她" and "她说/她定的". A stranger does not know who she is, so it is
                  noise to them — and it is her, in a public repo.
    comment lines counted, never enforced. A comment can be perfectly good Chinese
                  while being wrong for this repo, and no regex can tell the difference
                  between one being translated and one being deleted.

`frontend/loci.html` is a special case with a rule of its own: it is the panel she
uses every day, so its interface copy stays in Chinese. Only its comments and the
mentions of her are in scope. Its Chinese line count is therefore reported as
context and never enforced.

`docs/`, `README`, `CHANGELOG` are prose for people to read, not code. Out of scope
entirely — that was decided separately, and this file does not get a vote.
"""
import argparse
import os
import re
import sys

sys.stdout = __import__("io").TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CJK = re.compile(r"[一-鿿]")
DEF = re.compile(r"^\s*(?:async\s+)?(?:def|function|class)\s+([^\s(:]+)")
JS_CONST_FN = re.compile(r"^\s*(?:const|let|var)\s+([^\s=]+)\s*=\s*(?:async\s*)?(?:function\b|\()")
SHE = "她"
SHE_SAID = re.compile(r"她(说|的原话|定的|拍的|要求|提的)")

SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".venv", "venv"}
CODE_EXT = (".py", ".js", ".mjs", ".ts", ".yaml", ".yml", ".sh")

# Directories whose code must end up fully in English.
SCOPE = ["src", "gateway", "scripts", "tests", "config"]

# Counted for context, never enforced: interface copy stays in Chinese here.
COPY_EXEMPT = {os.path.join("frontend", "loci.html")}

# This file is not scanned. It has to contain the very strings it searches for, so
# scanning itself makes the count go up by however thoroughly the check is written —
# and the first run of it did exactly that, reporting ten mentions that were its own
# pattern definitions. A detector that counts itself measures the detector.
SELF = os.path.join("scripts", "check_english.py")

# ═══ BASELINE — lower these as batches land; when all are 0 this becomes a plain gate ═══
BASELINE = {
    "identifiers": 153,
    "filenames": 8,
    "she": 551,
}


def _walk(rel_root: str):
    base = os.path.join(ROOT, rel_root)
    if not os.path.isdir(base):
        return
    for cur, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            yield os.path.join(cur, f)


def _rel(path: str) -> str:
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def scan() -> dict:
    identifiers, filenames, she, comment_lines = [], [], [], 0
    she_files = set()

    targets = list(SCOPE) + ["frontend"]
    for root in targets:
        for path in _walk(root):
            rel = _rel(path)
            name = os.path.basename(path)
            if os.path.relpath(path, ROOT) == SELF:
                continue

            # Filenames are checked everywhere in scope, including non-code files.
            if CJK.search(name) and root in SCOPE:
                filenames.append(rel)

            if not name.endswith(CODE_EXT) and not name.endswith(".html"):
                continue
            try:
                text = open(path, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue

            exempt_copy = os.path.relpath(path, ROOT) in COPY_EXEMPT

            for i, line in enumerate(text.splitlines(), 1):
                if CJK.search(line) and not exempt_copy:
                    comment_lines += 1
                if root in SCOPE:
                    m = DEF.match(line) or JS_CONST_FN.match(line)
                    if m and CJK.search(m.group(1)):
                        identifiers.append(f"{rel}:{i}  {m.group(1)}")

            n = text.count(SHE)
            if n:
                she.append((rel, n, len(SHE_SAID.findall(text))))
                she_files.add(rel)

    return {
        "identifiers": identifiers,
        "filenames": filenames,
        "she": she,
        "she_total": sum(n for _, n, _ in she),
        "she_said_total": sum(s for _, _, s in she),
        "comment_lines": comment_lines,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="print every remaining offender")
    ap.add_argument("--baseline", action="store_true", help="print a BASELINE block to paste back")
    args = ap.parse_args()

    r = scan()
    counts = {
        "identifiers": len(r["identifiers"]),
        "filenames": len(r["filenames"]),
        "she": r["she_total"],
    }

    if args.baseline:
        print("BASELINE = {")
        for k in ("identifiers", "filenames", "she"):
            print(f'    "{k}": {counts[k]},')
        print("}")
        return 0

    print("═══ English check ═══")
    labels = {
        "identifiers": "Chinese identifiers (def/function/class)",
        "filenames": "Chinese filenames",
        "she": "mentions of 她",
    }
    worse = []
    for key, label in labels.items():
        now, base = counts[key], BASELINE[key]
        if now > base:
            mark, note = "❌", f"WENT UP from {base}"
            worse.append(key)
        elif now < base:
            mark, note = "✅", f"down from {base} — lower the BASELINE in this file"
        else:
            mark, note = ("✅", "done") if now == 0 else ("·", f"unchanged ({base})")
        print(f"  {mark} {label:44s} {now:5d}   {note}")

    print(f"  · {'Chinese comment lines (context only)':44s} {r['comment_lines']:5d}")
    print(f"  · {'of the 她 above, 「她说/她定的」':44s} {r['she_said_total']:5d}")

    if args.list:
        if r["identifiers"]:
            print("\n--- identifiers ---")
            for x in r["identifiers"]:
                print("   ", x)
        if r["filenames"]:
            print("\n--- filenames ---")
            for x in r["filenames"]:
                print("   ", x)
        if r["she"]:
            print("\n--- 她, by file ---")
            for rel, n, said in sorted(r["she"], key=lambda t: -t[1]):
                print(f"    {n:4d}  ({said} 说)  {rel}")

    if worse:
        print(f"\n❌ {', '.join(worse)} went up. Something was added back in Chinese.")
        return 1
    if all(counts[k] == 0 for k in counts):
        print("\n✅ Nothing left. This is the finish line the first attempt never had.")
        return 0
    print("\n✅ No regressions. Still to go — see the numbers above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
