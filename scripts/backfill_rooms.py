# -*- coding: utf-8 -*-
"""
backfill_rooms.py — after migrating from an older generation of this system, give the
memories that have no room one to belong to.

========================================
What this script does NOT do — stated first, on purpose
========================================
· No model calls. It costs nothing and never goes online.
· It does not touch the body, the tags, the domain, or any field that is already set.
· Buckets that already have a room are SKIPPED. It is idempotent; run it as often as
  you like.
· It DRY RUNS by default: it counts and shows you, and writes not one byte. Add --apply
  to actually write.

Why it fills in only the room and nothing else:
    Every read path — recall, the waking screen, the panel — goes through
    `core/_rooms.normalize_room()`, which recognises the older room names and translates
    them on the fly. **So a migrated library is already readable.** The one thing that is
    not readable is a bucket with no room field at all: it belongs to no room, so it
    cannot be found through the panel's or recall's room filters. That is the single gap
    this script closes.

Why it does not guess the room from the content:
    "Is this an event or a realization" is a judgement about meaning, and rules guess it
    badly. The cost of guessing wrong is that a realization gets filed as something that
    happened — which raises no error and simply reads as subtly wrong forever after.

    So it does not guess. Everything lands in one room (EVENT/SELF by default: material
    migrated out of your own conversation history is overwhelmingly "things I was there
    for"), and --room overrides it. Sorting them more finely is work for `regrow`, one
    at a time, by someone who can tell — not a decision a script should make on their
    behalf.

Usage:
    python scripts/backfill_rooms.py --buckets /path/to/buckets              # dry run
    python scripts/backfill_rooms.py --buckets /path/to/buckets --apply      # write
    python scripts/backfill_rooms.py --buckets ... --room MIND/VIEWS --apply
========================================
"""
import argparse
import io
import os
import re
import sys
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOMS = ("EVENT/SELF", "EVENT/WORLD", "MIND/TRAITS", "MIND/VIEWS")
# The older ten rooms → the current four. Same mapping as `core/_rooms.LEGACY_ROOMS`.
# It is duplicated here on purpose: this script has to be runnable on its own, without
# importing the rest of the codebase, because someone may well want to point it at a
# library and see what it says before installing anything.
LEGACY = {
    "I/EVENT/SELF": "EVENT/SELF", "I/EVENT/WHO": "EVENT/SELF",
    "I/EVENT/WHAT": "EVENT/SELF", "YOU/EVENT/WHO": "EVENT/WORLD",
    "YOU/EVENT/WHAT": "EVENT/WORLD", "I/MIND/WHO": "MIND/TRAITS",
    "I/MIND/WHAT": "MIND/VIEWS", "YOU/MIND/WHO": "MIND/TRAITS",
    "YOU/MIND/WHAT": "MIND/VIEWS", "I/MIND/SELF": "MIND/TRAITS",
}
SCAN = ("dynamic", "permanent", "feel", "plans", "archive")
_ROOM_LINE = re.compile(r"^room:\s*(.*)$", re.M)


def scan_buckets(buckets: str):
    for sub in SCAN:
        d = os.path.join(buckets, sub)
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for fn in files:
                if fn.endswith(".md"):
                    yield os.path.join(root, fn)


def fill_one(path: str, room: str, apply: bool) -> str:
    """What happened to this one: have / legacy / fill / skip."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    if not text.startswith("---"):
        return "skip"                     # not standard frontmatter — leave it alone
    end = text.find("\n---", 3)
    if end < 0:
        return "skip"
    head = text[:end]
    m = _ROOM_LINE.search(head)
    if m:
        cur = m.group(1).strip().strip('"').strip("'")
        if cur in ROOMS:
            return "have"                 # already one of the current names
        if cur in LEGACY:
            return "legacy"               # an older name — translated on read, so the
                                          # stored file does not need to change
        # A name we do not recognise. Leave it and report it: better that someone sees it
        # than that a script quietly rewrites something it did not understand.
        return "skip"
    if not apply:
        return "fill"
    # Insert one line before the `---` that closes the frontmatter. ONE line, inserted
    # as text — the whole YAML block is deliberately not re-serialised, because that would
    # reorder and reformat everything the owner wrote by hand.
    new = text[:end] + "\nroom: " + room + text[end:]
    data = new.encode("utf-8")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)                 # encode first, then swap atomically
    return "fill"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--buckets", required=True,
                    help="the memory directory — the one containing dynamic/, permanent/ and so on")
    ap.add_argument("--room", default="EVENT/SELF", choices=ROOMS,
                    help="which room the ones with no room should get")
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without it this is a dry run")
    a = ap.parse_args()
    if not os.path.isdir(a.buckets):
        print("no such directory: " + a.buckets)
        sys.exit(1)

    c = Counter()
    for p in scan_buckets(a.buckets):
        c[fill_one(p, a.room, a.apply)] += 1

    print("=" * 46)
    print("DRY RUN — not one byte written" if not a.apply else "written")
    print("=" * 46)
    print("  already on a current room   %d" % c["have"])
    print("  on an older room name      %d   <- translated on read; the stored file is fine as it is"
          % c["legacy"])
    print("  no room at all            %d   <- %s" % (
        c["fill"], ("would be filled with " + a.room) if not a.apply
        else ("filled with " + a.room)))
    print("  left untouched            %d   <- not standard frontmatter, or a room name "
          "we do not recognise" % c["skip"])
    if not a.apply and c["fill"]:
        print()
        print("To write for real: the same command with --apply. "
              "COPY THE WHOLE BUCKETS DIRECTORY FIRST.")


if __name__ == "__main__":
    main()
