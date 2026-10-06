# -*- coding: utf-8 -*-
"""
tests/test_package_upload_limit.py — an export package comes back through the real HTTP app.

The routes are reached through the ASGI app the server builds (server_app.build_http_app:
the body limits, the CSRF guard, CORS, the panel gate), not by calling the route function:

  · a package larger than the 4 MiB management limit is taken whole and restored;
  · each upload route has its own ceiling, enforced while the body streams — a body past it
    is answered 413 without being held, whether its length was declared or not;
  · the old /api/migrate/upload is no longer a way around the management limit.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

import pytest

from bridge import request_limits as RL
from core import export_package as EP
from core import schema
from core.bucket_manager import BucketManager
from core.package_import import MigrateEngine
from test_export_package import _FakeEmbedding, _point_runtime, run

HOST = b"127.0.0.1:8000"
ORIGIN = b"http://127.0.0.1:8000"
BOUNDARY = "loci-upload-boundary"


def _app(monkeypatch, store, emb, migrate):
    from mcp.server.fastmcp import FastMCP

    import web
    from server_app import HTTPRuntimeSettings, RuntimeLifecycle, build_http_app
    from web import _shared as sh
    from web import panel_auth as PA

    monkeypatch.setattr(PA, "gate_needed", lambda: False)
    monkeypatch.setattr(sh, "config", {"buckets_dir": str(store.base_dir)})
    monkeypatch.setattr(sh, "bucket_mgr", store)
    monkeypatch.setattr(sh, "embedding_engine", emb)
    monkeypatch.setattr(sh, "migrate_engine", migrate)
    monkeypatch.setattr(sh, "version", "test")
    mcp = FastMCP("upload-limit-test")
    web.register_all(mcp)
    return build_http_app(
        mcp, "streamable-http",
        settings=HTTPRuntimeSettings(auth_required=False, max_request_bytes=1024 * 1024),
        token_validator=lambda *a, **k: False,
        lifecycle=RuntimeLifecycle(logger=logging.getLogger("upload-limit-test")))


async def _call(app, method, path, body=b"", content_type="application/json",
                declare_length=True, chunk=256 * 1024):
    """One request through the ASGI app, its body in chunks the way a server hands it on.
    Returns (status, body bytes, how many body bytes the app read)."""
    chunks = [body[i:i + chunk] for i in range(0, len(body), chunk)] or [b""]
    state = {"next": 0, "read": 0}
    sent = []

    async def receive():
        i = state["next"]
        if i < len(chunks):
            state["next"] += 1
            state["read"] += len(chunks[i])
            return {"type": "http.request", "body": chunks[i],
                    "more_body": i + 1 < len(chunks)}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    headers = [(b"host", HOST), (b"origin", ORIGIN),
               (b"content-type", content_type.encode())]
    if declare_length:
        headers.append((b"content-length", str(len(body)).encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "root_path": "", "query_string": b"", "headers": headers,
             "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8000)}
    await app(scope, receive, send)
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert len(starts) == 1, sent
    data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return starts[0]["status"], data, state["read"]


def _multipart(data: bytes) -> tuple[bytes, str]:
    body = (f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"p.zip\"\r\nContent-Type: application/zip\r\n\r\n").encode()
    body += data + f"\r\n--{BOUNDARY}--\r\n".encode()
    return body, f"multipart/form-data; boundary={BOUNDARY}"


@pytest.fixture
def big_package(tmp_path, monkeypatch):
    """A library whose one entry carries a 5 MiB attachment that does not compress: its
    export package is past the 4 MiB management limit."""
    src = tmp_path / "src"
    src.mkdir()
    schema.stamp_new_library(src)
    store = BucketManager({"buckets_dir": str(src)})
    _point_runtime(monkeypatch, src, store)
    bid = run(store.create("带着一张很大的照片的那天。", room="EVENT/SELF"))
    media = src / "_media" / bid
    media.mkdir(parents=True)
    (media / "big.bin").write_bytes(os.urandom(5 * 1024 * 1024))
    import frontmatter
    path = Path(store._find_bucket_file(bid))
    post = frontmatter.load(path)
    post["media"] = [{"path": f"_media/{bid}/big.bin", "type": "application/octet-stream"}]
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    _FakeEmbedding(src / "embeddings.db")
    zip_path, _manifest = run(EP.build_package(
        store, embedding_db_path=str(src / "embeddings.db"),
        export_meta={"exported_at": "2026-10-03T00:00:00Z", "version": "test"},
        alias_path=str(src / "aliases.yaml")))
    data = Path(zip_path).read_bytes()
    Path(zip_path).unlink()
    assert len(data) > 4 * 1024 * 1024
    return {"data": data, "id": bid, "tmp": tmp_path}


def _empty_library(root: Path, monkeypatch):
    root.mkdir()
    schema.stamp_new_library(root)
    store = BucketManager({"buckets_dir": str(root)})
    _point_runtime(monkeypatch, root, store)
    emb = _FakeEmbedding(root / "embeddings.db")
    return store, emb, MigrateEngine({"buckets_dir": str(root)}, store, emb)


def test_a_package_past_the_management_limit_comes_back_through_the_app(big_package,
                                                                          monkeypatch):
    from web import library_api as L
    dst = big_package["tmp"] / "dst"
    store, emb, engine = _empty_library(dst, monkeypatch)
    app = _app(monkeypatch, store, emb, engine)

    async def go():
        body, ctype = _multipart(big_package["data"])
        status, data, _read = await _call(app, "POST", "/api/loci/import-package", body,
                                          ctype)
        parsed = json.loads(data)
        assert status == 200 and parsed["phase"] == "parsed", (status, parsed)
        status, data, _read = await _call(
            app, "POST", "/api/loci/import-package",
            json.dumps({"job_id": parsed["job_id"]}).encode())
        assert status == 202, data
        for task in list(L._running):
            await task
        status, data, _read = await _call(app, "GET", "/api/loci/import-package")
        st = json.loads(data)
        assert st["phase"] == "done" and st["library_state"]["media"] == 1, st
    asyncio.run(go())
    restored = dst / "_media" / big_package["id"] / "big.bin"
    assert restored.read_bytes() == (big_package["tmp"] / "src" / "_media"
                                     / big_package["id"] / "big.bin").read_bytes()


@pytest.mark.parametrize("declare", [True, False])
def test_a_body_past_the_upload_ceiling_is_refused_while_it_streams(tmp_path, monkeypatch,
                                                                     declare):
    store, emb, engine = _empty_library(tmp_path / "dst", monkeypatch)
    app = _app(monkeypatch, store, emb, engine)
    ceiling = 2 * 1024 * 1024
    monkeypatch.setitem(RL.UPLOAD_CEILINGS, "/api/loci/import-package", ceiling)
    body, ctype = _multipart(os.urandom(6 * 1024 * 1024))

    status, data, read = asyncio.run(_call(app, "POST", "/api/loci/import-package", body,
                                           ctype, declare_length=declare))
    assert status == 413, data
    assert "MB" in json.loads(data)["error"]
    # Undeclared, it stops reading just past the ceiling; declared far past it, it reads
    # nothing at all.
    assert read <= ceiling + 256 * 1024 if not declare else read == 0
    # The engine's slot is free again: the next upload is not told to wait.
    assert engine.phase != "parsing"


def test_each_upload_route_has_its_own_ceiling_and_the_old_one_is_gone():
    assert RL.upload_ceiling("/api/loci/import-package") > 512 * 1024 * 1024
    assert RL.upload_ceiling("/api/import/upload/") == RL.upload_ceiling("/api/import/upload")
    assert RL.upload_ceiling("/api/import/upload") < RL.upload_ceiling(
        "/api/loci/import-package")
    assert RL.upload_ceiling("/api/migrate/upload") == 0


def test_an_ordinary_panel_write_still_has_the_management_limit(tmp_path, monkeypatch):
    store, emb, engine = _empty_library(tmp_path / "dst", monkeypatch)
    app = _app(monkeypatch, store, emb, engine)
    status, _data, _read = asyncio.run(_call(
        app, "POST", "/api/migrate/upload", b"x" * (5 * 1024 * 1024)))
    assert status == 413
