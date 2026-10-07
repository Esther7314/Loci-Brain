# -*- coding: utf-8 -*-
"""
tests/test_dev_panel_library.py — the dev runner's sample library is one this version
writes.

scripts/dev_panel.py seeds its throwaway library in a child process before the server
ever starts. The library it leaves has to be what a server-made library is: stamped with
the current version before its first memory, so that its export package
(core/export_package.py) is one this version's importer (core/package_import.py) takes.
"""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from core import export_package as EP
from core import schema
from core.bucket_manager import BucketManager
from core.package_import import MigrateEngine

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "dev_panel.py"


class _FakeEmbedding:
    """What the importer reads of an embedding engine: the model and the vector store."""

    def __init__(self, db_path: Path, model="fake-embed"):
        self.model = model
        self.db_path = str(db_path)
        self._backend = None
        self.enabled = False
        with closing(sqlite3.connect(db_path)) as c:
            c.execute("CREATE TABLE IF NOT EXISTS embeddings (bucket_id TEXT PRIMARY KEY, "
                      "embedding TEXT NOT NULL, updated_at TEXT NOT NULL, "
                      "content_hash TEXT NOT NULL DEFAULT '', meaning_embedding TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS embeddings_meta "
                      "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            c.commit()


@pytest.fixture
def seeded():
    """A library seeded the way the runner seeds it: its child, with its environment."""
    base = os.path.realpath(tempfile.mkdtemp(prefix="loci-panel-dev-test-"))
    try:
        sys.path.insert(0, str(SCRIPT.parent))
        try:
            import dev_panel as DP
        finally:
            sys.path.remove(str(SCRIPT.parent))
        with open(os.path.join(base, DP.MARKER), "w", encoding="utf-8") as f:
            f.write("test\n")
        paths = DP._layout(base)
        for d in (paths["buckets"], paths["logs"]):
            os.makedirs(d, exist_ok=True)
        DP._write_config(paths)
        done = subprocess.run([sys.executable, str(SCRIPT), "--seed-into", base],
                              env=DP._child_env(paths, 0, "x" * 32), cwd=base,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=300)
        assert done.returncode == 0, done.stdout + done.stderr
        yield Path(paths["buckets"])
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_the_sample_library_is_stamped_current(seeded):
    assert schema.library_version(seeded) == schema.CURRENT_VERSION


def test_its_export_is_taken_by_the_importer(seeded, tmp_path, monkeypatch):
    from core import runtime as rt

    monkeypatch.setenv("LOCI_ALIAS_TABLE", str(seeded / "aliases.yaml"))
    store = BucketManager({"buckets_dir": str(seeded)})
    monkeypatch.setattr(rt, "bucket_mgr", store)
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(seeded)})
    _FakeEmbedding(seeded / "embeddings.db")
    zip_path, _ = asyncio.run(EP.build_package(
        store, embedding_db_path=str(seeded / "embeddings.db"),
        export_meta={"exported_at": "2026-10-07T00:00:00Z", "version": "test",
                     "embedding": {"model": "fake-embed", "dim": 3, "backend": "api"}},
        alias_path=str(seeded / "aliases.yaml")))
    try:
        with zipfile.ZipFile(zip_path) as z:
            meta = json.loads(z.read("export_meta.json"))
        assert meta["library_schema_version"] == schema.CURRENT_VERSION

        dst = tmp_path / "dst"
        dst.mkdir()
        schema.stamp_new_library(dst)
        target = BucketManager({"buckets_dir": str(dst)})
        monkeypatch.setattr(rt, "bucket_mgr", target)
        monkeypatch.setattr(rt, "config", {"buckets_dir": str(dst)})
        engine = MigrateEngine({"buckets_dir": str(dst)}, target,
                               _FakeEmbedding(dst / "embeddings.db"))
        parsed = asyncio.run(engine.parse_zip_file(zip_path))
        assert parsed["ok"], parsed
    finally:
        os.unlink(zip_path)
