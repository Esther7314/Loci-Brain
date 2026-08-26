"""Mutation check: break the source on purpose, confirm the right test goes red, revert.

    python scripts/verify/mutate.py [--root <repo>]

The repository root is derived from this file's own location (scripts/verify/mutate.py →
two levels up), so the tool runs from any working directory and on any checkout. It used
to be one hardcoded absolute path, which meant that on anybody else's machine — or in a
worktree, or a second clone — it either crashed or, worse, quietly mutated and tested a
*different* checkout than the one being worked on and reported a verdict about it.
`--root` is for the deliberate case: pointing it at another checkout on purpose.
"""
import io, subprocess, sys, os
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])

def run(mutations, testfile):
    """mutations: list of (path, old, new). Returns pytest summary line."""
    backups = {}
    try:
        for path, old, new in mutations:
            full = os.path.join(ROOT, path)
            t = io.open(full, encoding='utf-8').read()
            n = t.count(old)
            assert n == 1, (f'mutation target must match EXACTLY once in {path} '
                            f'(found {n}) — a non-unique target silently patches the '
                            f'wrong occurrence and reports a false green: {old!r}')
            backups[full] = t
            io.open(full, 'w', encoding='utf-8').write(t.replace(old, new, 1))
        r = subprocess.run([sys.executable, '-m', 'pytest', testfile, '-q', '--no-header', '-x'],
                           cwd=ROOT, capture_output=True,
                           env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
        out = r.stdout.decode('utf-8', 'replace')
        failed = [l for l in out.splitlines() if l.startswith('FAILED') or 'passed' in l or 'failed' in l]
        return r.returncode, failed[-3:]
    finally:
        for full, t in backups.items():
            io.open(full, 'w', encoding='utf-8').write(t)

CASES = [
    ("rejection_key stops sorting (order-dependence sneaks in)",
     [('src/core/_muse.py',
       'return ",".join(sorted({str(i).strip() for i in (ids or []) if str(i).strip()}))',
       'return ",".join({str(i).strip() for i in (ids or []) if str(i).strip()})')],
     'tests/test_contract_muse_rejection.py'),
    ("record_rejection keeps it in memory only (never reaches disk)",
     [('src/core/_muse.py', '    os.replace(tmp, p)\n    return key, cnt',
       '    os.remove(tmp)\n    return key, cnt')],
     'tests/test_contract_muse_rejection.py'),
    ("corrupt-file guard removed (shape check dropped)",
     [('src/core/_muse.py',
       '    if not isinstance(data, dict) or not isinstance(data.get("rejected"), dict):\n        return {"version": 1, "rejected": {}}\n    return data',
       '    return data')],
     'tests/test_contract_muse_rejection.py'),
    ("`first` timestamp gets overwritten on every rejection",
     [('src/core/_muse.py', '"first": ent.get("first") or now,', '"first": now,')],
     'tests/test_contract_muse_rejection.py'),
]

if __name__ == '__main__':
  # Windows consoles default to GBK, and the verdict lines below carry 🔴/✔. Without
  # this the findings print fine and then **the conclusion throws** — the one line
  # you actually came for is the one that does not make it out. (strdiff.py already
  # had this; the other three did not, which is why it went unnoticed.)
  sys.stdout.reconfigure(encoding="utf-8")
  if '--root' in sys.argv:
      ROOT = str(Path(sys.argv[sys.argv.index('--root') + 1]).resolve())
  print(f'root: {ROOT}')
  for name, muts, tf in CASES:
      code, tail = run(muts, tf)
      verdict = 'RED  ✔ 断言抓住了' if code != 0 else 'GREEN ✘ 假绿！没有断言守这条'
      print(f'{verdict}  —— {name}')
      for l in tail: print(f'        {l}')