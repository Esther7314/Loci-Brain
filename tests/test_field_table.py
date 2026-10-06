# -*- coding: utf-8 -*-
"""
tests/test_field_table.py — every field the store writes is described in core/fields.py.

The export package's schema note is generated from that table, so a field the store
accepts and the table does not name would travel without a meaning. The keys are read off
the code itself (core/_bucket_write.py): create()'s keyword arguments as they land in the
frontmatter, the keys update() handles one by one, its pass-through list, V2_FIELDS and the
text limits. create() and _update_locked() run in named steps (`_create_*`, `_update_*`),
and each step is read along with them.
"""

import ast
from pathlib import Path

from core import _bucket_write as W
from core import bucket_manager as BM
from core import fields as F

SOURCE = Path(W.__file__).read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

# update() arguments that are gestures on a field, not fields of their own.
GESTURES = {"content", "media_append", "meaning_append"}


def _functions(name: str, steps: str) -> list[ast.AST]:
    """`name` and every function whose name starts with `steps`."""
    found = [node for node in ast.walk(TREE)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and (node.name == name or node.name.startswith(steps))]
    if not any(node.name == name for node in found):
        raise AssertionError(f"{name} not found in _bucket_write.py")
    return found


def _update_keys() -> set[str]:
    keys: set[str] = set()
    nodes = [n for fn in _functions("_update_locked", "_update_") for n in ast.walk(fn)]
    for node in nodes:
        # `"x" in kwargs`
        if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant)
                and isinstance(node.left.value, str)
                and any(isinstance(op, ast.In) for op in node.ops)
                and any(isinstance(c, ast.Name) and c.id == "kwargs"
                        for c in node.comparators)):
            keys.add(node.left.value)
        # the pass-through loop: `for k in (...)` whose body asks `k in kwargs`
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple):
            for elt in node.iter.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    keys.add(elt.value)
                elif isinstance(elt, ast.Name) and elt.id == "PROV_FIELD":
                    keys.add(BM.PROV_FIELD)
                elif isinstance(elt, ast.Starred) and getattr(elt.value, "id", "") == "V2_FIELDS":
                    keys.update(BM.V2_FIELDS)
    return keys - GESTURES


def _create_keys() -> set[str]:
    keys: set[str] = set()
    nodes = [n for fn in _functions("create", "_create_") for n in ast.walk(fn)]
    for node in nodes:
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == "metadata" and isinstance(node.ctx, ast.Store)):
            sl = node.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                keys.add(sl.value)
            elif isinstance(sl, ast.Name) and sl.id == "PROV_FIELD":
                keys.add(BM.PROV_FIELD)
    return keys


def test_the_parsers_found_the_fields():
    # Criterion: two empty sets would agree forever.
    assert len(_update_keys()) > 40
    assert {"id", "name", "created", "room"} <= _create_keys()


def test_every_field_update_accepts_is_described():
    missing = F.undescribed(_update_keys())
    assert not missing, f"update() accepts keys core/fields.py does not describe: {missing}"


def test_every_field_create_writes_is_described():
    keys = {k for k in _create_keys() if not k.startswith(("kind", "created_by", "erasable"))}
    missing = F.undescribed(keys)
    assert not missing, f"create() writes keys core/fields.py does not describe: {missing}"


def test_v2_fields_and_text_limits_are_described():
    assert not F.undescribed(BM.V2_FIELDS)
    assert not F.undescribed(BM._METADATA_TEXT_LIMITS)


def test_the_table_names_each_field_once_in_a_known_group():
    names = [f.name for f in F.FIELDS]
    assert len(names) == len(set(names))
    assert {f.group for f in F.FIELDS} <= set(F.GROUPS)
    assert all(f.meaning.strip() for f in F.FIELDS)
