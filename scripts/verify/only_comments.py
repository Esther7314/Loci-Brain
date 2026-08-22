# -*- coding: utf-8 -*-
"""only_comments.py — prove that a change touched NOTHING but comments and docstrings.

    python only_comments.py <git-ref> <path> [<path> ...]

Why this exists
    The comment batch of the English migration rewrites thousands of lines across files
    that have no test coverage at all (a stylesheet, a page of markup). "I only changed
    comments" is exactly the kind of claim that is easy to make, impossible to eyeball at
    that volume, and silently false the one time a stray character lands outside a comment.

    So it is checked instead of claimed: strip every comment and docstring from the old
    version and the new one, and require the remainder to be byte-identical.

How each language is handled
    .py     `tokenize` for comments; `ast` for docstrings. COMMENT tokens are dropped, and
            so is a docstring — docstrings are prose, they are being translated too, so
            they must not count as code.

            🔴 Docstrings are located with `ast`, by exact (line, column), and NOT by the
            obvious heuristic "a STRING that follows a newline". That heuristic was what
            this file did first, and it had a hole big enough to matter:

                return JSONResponse({"error": "first part, "
                                              "second part"})

            `tokenize` emits an NL before every continuation line inside brackets, so the
            second fragment of an implicit concatenation looked exactly like a docstring —
            and this tool reported "only comments moved" while a user-facing error message
            had been rewritten. Almost every message in this repository is written that
            way, so the blind spot covered most of the strings it was meant to protect.

            Found by a subagent that went looking for holes in its own verification
            instead of trusting the green light. The lesson is the one this whole toolchain
            keeps relearning: **ask what would make the checker go red, not what it checks.**
    .js/.css/.html   a small scanner that tracks string and template literals and both
            comment forms. It has to track strings, because `"http://x"` contains `//`
            and `"/*"` is a perfectly ordinary string.

            ⚠️ It does NOT track regex literals — an earlier version of this docstring
            claimed it did, which was exactly the kind of statement this tool is used to
            hunt down. A regex containing `//` or `/*`, e.g. one containing an escaped slash, would be
            mis-sliced. No file it has been run on contains one, so the results so far
            hold; a file that does would need a real lexer.

    Anything else is compared verbatim.

What a pass means
    Only comments and docstrings differ. It does NOT mean the new comments are correct,
    or that they still describe what the code does. It means the code was not touched.
"""
import ast
import io
import subprocess
import sys
import tokenize


def _docstring_positions(src: str) -> set:
    """Exact (line, col) of every real docstring, via the parser.

    A docstring is the first statement of a module, class or function and nothing else.
    `ast` knows that; a token-level guess does not, which is the hole described above.
    """
    spots = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return spots
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            spots.add((first.value.lineno, first.value.col_offset))
    return spots


def strip_python(src: str) -> str:
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return src
    docstrings = _docstring_positions(src)
    out = []
    for tok in toks:
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and tok.start in docstrings:
            out.append("<DOCSTRING>")
            continue
        if tok.type in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                        tokenize.DEDENT, tokenize.ENCODING):
            continue
        out.append(tok.string)
    return "\n".join(out)


def strip_cstyle(src: str) -> str:
    """Drop // and /* */ comments and <!-- --> comments, respecting string literals."""
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        # --- string / template literals: copied through untouched ---
        if c in "\"'`":
            quote = c
            out.append(c)
            i += 1
            while i < n:
                if src[i] == "\\":
                    out.append(src[i:i + 2])
                    i += 2
                    continue
                out.append(src[i])
                if src[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        # --- comments ---
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if src.startswith("<!--", i):
            end = src.find("-->", i + 4)
            i = n if end == -1 else end + 3
            continue
        out.append(c)
        i += 1
    # Whitespace runs are collapsed: removing a comment leaves blank lines behind, and
    # those are not code either.
    return " ".join("".join(out).split())


def strip(path: str, src: str) -> str:
    if path.endswith(".py"):
        return strip_python(src)
    if path.endswith((".js", ".mjs", ".ts", ".css", ".html", ".htm")):
        return strip_cstyle(src)
    return src


def main() -> int:
    # Windows consoles default to GBK, and the verdict lines below carry 🔴/✔. Without
    # this the findings print fine and then **the conclusion throws** — the one line
    # you actually came for is the one that does not make it out. (strdiff.py already
    # had this; the other three did not, which is why it went unnoticed.)
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    ref, paths = sys.argv[1], sys.argv[2:]
    bad = 0
    for path in paths:
        old = subprocess.run(["git", "show", f"{ref}:{path}"],
                             capture_output=True)
        if old.returncode != 0:
            print(f"  ?? {path}  (not in {ref})")
            continue
        old_src = old.stdout.decode("utf-8", "replace")
        new_src = io.open(path, encoding="utf-8").read()
        a, b = strip(path, old_src), strip(path, new_src)
        if a == b:
            print(f"  OK   {path}  — code identical, only comments/docstrings differ")
        else:
            bad += 1
            print(f"  FAIL {path}  — something OUTSIDE a comment changed")
            # Show the first divergence so it is findable rather than just asserted.
            for k in range(min(len(a), len(b))):
                if a[k] != b[k]:
                    print(f"         first difference at stripped offset {k}")
                    print(f"         was : ...{a[max(0, k - 60):k + 60]!r}")
                    print(f"         now : ...{b[max(0, k - 60):k + 60]!r}")
                    break
            else:
                print(f"         one side is longer: {len(a)} vs {len(b)} chars")
    print("\nOK — nothing but comments moved." if not bad
          else f"\nFAILED on {bad} file(s): code changed, not just comments.")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
