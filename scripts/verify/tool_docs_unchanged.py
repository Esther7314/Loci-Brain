# -*- coding: utf-8 -*-
"""tool_docs_unchanged.py — the one thing `only_comments.py` is structurally blind to.

    python tool_docs_unchanged.py <git-ref>

Why this exists
    `only_comments.py` proves a change touched nothing but comments and docstrings. For
    almost every file that is exactly the right guarantee: docstrings are prose, prose is
    being translated, so prose must be allowed to move.

    There is one place where that reasoning inverts. The docstrings on the `@mcp.tool()`
    functions in `src/server.py` are not prose about the code — **they are the product**.
    They are the text a model reads to decide which tool to reach for, they were written
    a line at a time over an evening, and they are already in English.

    So a comment batch that "translated" them would be waved through by the very tool
    meant to catch it, and the damage would be invisible in a diff full of translation.

    This checks the one thing the other one cannot: those docstrings are byte-identical.

When it should be allowed to fail
    When someone deliberately rewrites a tool description — which is a real task, listed
    separately. Then the correct move is to review the diff it prints, not to skip it.
"""
import ast
import subprocess
import sys

TARGET = "src/server.py"


def tool_docs(source: str) -> dict:
    out = {}
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        print(f"  !! could not parse {TARGET}: {exc}")
        return out
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorators = [ast.unparse(d) for d in node.decorator_list]
        if any("tool()" in d for d in decorators):
            out[node.name] = ast.get_docstring(node) or ""
    return out


def main() -> int:
    # Windows consoles default to GBK, and the verdict lines below carry 🔴/✔. Without
    # this the findings print fine and then **the conclusion throws** — the one line
    # you actually came for is the one that does not make it out. (strdiff.py already
    # had this; the other three did not, which is why it went unnoticed.)
    sys.stdout.reconfigure(encoding="utf-8")
    ref = sys.argv[1] if len(sys.argv) > 1 else "HEAD"
    old = subprocess.run(["git", "show", f"{ref}:{TARGET}"], capture_output=True)
    if old.returncode != 0:
        print(f"cannot read {ref}:{TARGET}")
        return 2
    before = tool_docs(old.stdout.decode("utf-8", "replace"))
    after = tool_docs(open(TARGET, encoding="utf-8").read())

    if not before:
        print("!! found no @mcp.tool() docstrings in the old version — check the detector, "
              "not the code. A check that silently examines nothing always passes.")
        return 2

    bad = 0
    for name in sorted(set(before) | set(after)):
        a, b = before.get(name), after.get(name)
        if a == b:
            print(f"  OK   {name}  ({len((a or '').splitlines())} lines, unchanged)")
        else:
            bad += 1
            print(f"  CHANGED  {name}")
            if a is None:
                print("         (did not exist before)")
            elif b is None:
                print("         (gone)")
            else:
                al, bl = a.splitlines(), b.splitlines()
                for i in range(max(len(al), len(bl))):
                    x = al[i] if i < len(al) else "<missing>"
                    y = bl[i] if i < len(bl) else "<missing>"
                    if x != y:
                        print(f"         line {i + 1}\n           was : {x}\n           now : {y}")
                        break
    print("\nOK — the shipped tool descriptions are untouched." if not bad
          else f"\n🔴 {bad} tool description(s) changed. These are the product, not comments "
               f"about it — review the diff above before accepting.")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
