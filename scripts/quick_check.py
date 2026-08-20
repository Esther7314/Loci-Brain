# -*- coding: utf-8 -*-
"""
quick_check.py —— the layer you can run **immediately** after changing one line (est. 2026-08-20)

    python scripts/quick_check.py

═══════════════════════════════════════════════════════════════════
Why it exists
═══════════════════════════════════════════════════════════════════
The real value of a test = **the number of times it catches a problem**,
and how many times it catches = how many times you ran it x its hit rate.
**If it doesn't get run, the hit rate doesn't matter — the value is zero.**

Before 8-19 we had only one layer: the thing that brings up a real container, connects to a
real model, and runs for 25 minutes. It is well written (370+ assertions, each one with the
origin of its criterion written underneath), but **nobody is going to wait 25 minutes for it
after changing one line** — so at the very moment it should be catching you, right after the
change, it may as well not exist.

Her analogy on 8-19, and the criterion is right there in it:
    What we built is the kind of check-up that is **thorough but takes a three-day hospital
    stay**, so we do it once a year.
    What actually saves lives is **the blood pressure you take every day.**
This file is the blood pressure cuff. It measures far shallower, but it is **affordable**.

═══════════════════════════════════════════════════════════════════
What it runs / what it doesn't
═══════════════════════════════════════════════════════════════════
Runs:  · pyflakes (blocks undefined names — it pointed out those two NameErrors on 8-19 in
         three seconds)
       · unit tests (tests/) — mostly pure functions. Two of the contract files do touch a
         disk, under pytest's `tmp_path`, and one spawns a second interpreter to prove a
         key is stable across processes. That is as far as it goes: no container, no model,
         no network, and never a real memory.
       · gateway tests (gateway/tests/, fake upstream + fake Loci, not one real connection
         leaves the building)
       · the English ratchet — shipped code may not GAIN Chinese identifiers, Chinese
         filenames or mentions of her. Red only when a count goes up, which is what lets
         it sit here during the translation instead of only after it.

Doesn't run: **anything that needs a container, needs a model, or touches a real memory
       library.**
       🔴 That is its definition, not a shortcoming. The moment it starts bringing up
          containers it turns back into the one nobody runs.
       That deeper layer is a separate suite: it brings up a throwaway container against a
       COPY of the library and talks to a real model, and takes about 25 minutes. Run it
       before shipping, not after every edit.

📌 Hard target: **the whole thing finishes within 30 seconds.** Go over and something should be
   cut, not tolerated — a blood pressure cuff that slows down first becomes "I'll measure it in
   a bit", and then becomes not measuring at all.
"""
import os
import subprocess
import sys
import time

sys.stdout = __import__("io").TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIME_LIMIT = 30.0


def run_step(name: str, cmd: list, is_red) -> tuple:
    print(f"\n═══ {name} ═══")
    t0 = time.time()
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, env=env)
    elapsed = time.time() - t0
    out = (r.stdout or b"").decode("utf-8", "replace") + (r.stderr or b"").decode("utf-8", "replace")
    bad = is_red(r.returncode, out)
    print(out.strip()[-1500:] if bad else out.strip()[-400:])
    print(f"  {'❌' if bad else '✅'} {name}  {elapsed:.1f}s")
    return (not bad), elapsed


def main():
    total_t0 = time.time()
    results = {}

    # (1) lint —— undefined names only.
    #     ⚠️ Don't casually add `imported but unused` to the block list: there are a dozen or so
    #        old ones on disk, so blocking them would mean switching this gate off on day one —
    #        and **a gate that has been switched off is worse than no gate** (it makes people
    #        believe there is one).
    results["lint"] = run_step("(1) lint (undefined names only)",
                               [sys.executable, "-m", "pyflakes", "src"],
                               lambda rc, out: any("undefined name" in l for l in out.splitlines()))

    # (2) unit tests
    results["unit"] = run_step("(2) unit tests (tests/)",
                               [sys.executable, "-m", "pytest", "tests/", "-q"],
                               lambda rc, out: rc != 0)

    # (3) gateway
    #     ⚠️ This has to be written as a glob. `node --test gateway/tests` (without the glob) on
    #        Node 24 requires the directory as a module and reports MODULE_NOT_FOUND —
    #        **it looks like the tests failed, when in fact they never ran.**
    results["gateway"] = run_step("(3) gateway tests (fake upstream + fake Loci)",
                                  ["node", "--test", "gateway/tests/*.test.js"],
                                  lambda rc, out: rc != 0)

    # (4) the English ratchet.
    #     ⚠️ It is a RATCHET, not a gate, and that is the only reason it can be here today:
    #        a check that is red on day one gets skipped by day two. It goes red only when a
    #        count goes UP — i.e. when Chinese was put back into code that ships. The running
    #        numbers, and what is deliberately not counted, are in scripts/check_english.py.
    results["english"] = run_step("(4) English ratchet (shipped code only)",
                                  [sys.executable, "scripts/check_english.py"],
                                  lambda rc, out: rc != 0)

    elapsed = time.time() - total_t0
    print("\n═══ fast-lane bill ═══")
    for name, (ok, t) in results.items():
        print(f"  {'✅' if ok else '❌'} {name}  {t:.1f}s")
    print(f"  {elapsed:.1f}s total")
    if elapsed > TIME_LIMIT:
        # Not counted as red, but it absolutely has to be said out loud — it slowing down is the
        # **invisible** kind of bad.
        print(f"  ⚠️ Over {TIME_LIMIT:.0f} seconds. Once the blood pressure cuff slows down it first "
              f"becomes \"I'll measure it in a bit\", and then becomes not measuring at all. "
              f"Cut something, don't put up with it.")
    if not all(ok for ok, _ in results.values()):
        print("\n🔴 Red. **Fix this first** — don't go bring up a container for the 25-minute run; "
              "that layer can't catch these.")
        sys.exit(1)
    print("\n✅ All green. Carry on changing things. "
          "(Remember to run the full end-to-end pass before handing work over.)")


if __name__ == "__main__":
    main()
