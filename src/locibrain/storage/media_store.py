"""Persistent media storage for the vault.

Copies whatever an MCP call carried — a server-readable temporary file, or Base64
data — into the persistent media directory, and returns stable metadata that can be
written into Markdown frontmatter. It does not understand memory content, does not
touch bucket files, and never deletes media just because a memory was archived.
Public surface: ``MediaStore`` and ``MediaPersistenceError``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import mimetypes
import os
import re
import tempfile
from pathlib import Path
from typing import Any

_SAFE_SUFFIX = re.compile(r"^\.[a-zA-Z0-9]{1,10}$")
_DEFAULT_MAX_MEDIA_BYTES = 25 * 1024 * 1024


class MediaPersistenceError(ValueError):
    """Media could not be stored permanently on the server."""


class MediaStore:
    """Copy media into the persistent directory and mint a stable reference."""

    def __init__(
        self,
        vault_dir: str,
        media_dir: str,
        *,
        max_bytes: int = _DEFAULT_MAX_MEDIA_BYTES,
    ) -> None:
        self.vault_dir = Path(vault_dir).resolve()
        self.media_dir = Path(media_dir).resolve()
        self.max_bytes = max(1, int(max_bytes))
        self.media_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _suffix(name: str, mime_type: str) -> str:
        suffix = Path(name).suffix.lower()
        if _SAFE_SUFFIX.fullmatch(suffix):
            return suffix
        guessed = mimetypes.guess_extension(mime_type or "") or ".bin"
        return guessed if _SAFE_SUFFIX.fullmatch(guessed) else ".bin"

    def _stable_path(self, bucket_id: str, digest: str, suffix: str) -> Path:
        safe_bucket = re.sub(r"[^a-zA-Z0-9_.-]", "_", bucket_id)[:128]
        target_dir = (self.media_dir / safe_bucket).resolve()
        if self.media_dir not in target_dir.parents:
            raise MediaPersistenceError("媒体目录越界，已拒绝保存。")
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir / f"{digest}{suffix}"

    def _frontmatter_path(self, target: Path) -> str:
        try:
            return target.relative_to(self.vault_dir).as_posix()
        except ValueError:
            return str(target)

    @staticmethod
    def _atomic_write(target: Path, data: bytes) -> None:
        """Write to a temp file inside the target directory, then rename over the
        target. A crash mid-write must not leave half an image behind."""
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _read_path(self, raw_path: str) -> tuple[bytes, str]:
        source = Path(raw_path).expanduser()
        if not source.is_file():
            raise MediaPersistenceError(
                f"媒体临时路径在 OB 服务器上不可读：{raw_path}。"
                "请改传 data_base64，不能把客户端临时路径直接写进记忆。"
            )
        size = source.stat().st_size
        if size > self.max_bytes:
            raise MediaPersistenceError(
                f"媒体文件超过单项上限 {self.max_bytes} 字节：{raw_path}"
            )
        return source.read_bytes(), source.name

    def _decode_base64(self, value: str) -> bytes:
        payload = value.strip()
        if payload.startswith("data:"):
            _, separator, payload = payload.partition(",")
            if not separator:
                raise MediaPersistenceError("媒体 data URI 缺少数据部分。")
        try:
            data = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MediaPersistenceError("媒体 data_base64 不是有效 Base64。") from exc
        if len(data) > self.max_bytes:
            raise MediaPersistenceError(
                f"媒体数据超过单项上限 {self.max_bytes} 字节。"
            )
        return data

    def _persist_one(self, bucket_id: str, item: Any) -> dict[str, Any]:
        entry = {"path": item} if isinstance(item, str) else dict(item or {})
        mime_type = str(entry.get("type") or entry.get("mime_type") or "")[:128]
        if entry.get("data_base64"):
            data = self._decode_base64(str(entry["data_base64"]))
            source_name = str(entry.get("filename") or entry.get("title") or "media")
        else:
            raw_path = str(entry.get("path") or "").strip()
            if not raw_path:
                raise MediaPersistenceError("media 每项必须提供 path 或 data_base64。")
            data, source_name = self._read_path(raw_path)
        digest = hashlib.sha256(data).hexdigest()
        suffix = self._suffix(source_name, mime_type)
        target = self._stable_path(bucket_id, digest, suffix)
        if not target.exists():
            self._atomic_write(target, data)
        result: dict[str, Any] = {
            "path": self._frontmatter_path(target),
            "sha256": digest,
            "size": len(data),
            "stored": True,
        }
        for key, limit in (("title", 200), ("type", 128), ("note", 500)):
            value = entry.get(key)
            if value:
                result[key] = str(value)[:limit]
        return result

    async def persist(self, bucket_id: str, media: Any) -> list[dict[str, Any]]:
        """Persist one or many media items; any single failure raises rather than
        silently dropping the item."""
        if not media:
            return []
        items = media if isinstance(media, list) else [media]
        return await asyncio.to_thread(
            lambda: [self._persist_one(bucket_id, item) for item in items]
        )
