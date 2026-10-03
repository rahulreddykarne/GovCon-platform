"""Where attachment bytes are kept (roadmap gap 11, ADR-065).

Every read and write of stored attachment bytes goes through an
``AttachmentStore``, so a shared store can replace local disk before workers
run on more than one machine. Only the local store exists today; its keys
are the absolute paths already recorded in ``files.local_path``.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from pathlib import Path
from typing import Protocol

from govcon.config import Settings


class AttachmentStore(Protocol):
    def put(self, data: bytes, sha: str, opportunity_id: int, filename: str) -> str:
        """Store bytes and return their key. Never overwrites different bytes."""

    def read(self, key: str) -> bytes | None:
        """The stored bytes, or None when the key no longer resolves."""


class LocalAttachmentStore:
    """Content-addressed files under ``DATA_DIR/attachments/<opportunity id>/``."""

    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir) / "attachments"

    def put(self, data: bytes, sha: str, opportunity_id: int, filename: str) -> str:
        from govcon.enrich.attachments import sanitize_filename

        base = (self.root / str(int(opportunity_id))).resolve()
        base.mkdir(parents=True, exist_ok=True)
        safe = sanitize_filename(filename)
        target = (base / f"{sha[:16]}_{safe}").resolve()
        if target.parent != base:
            raise ValueError("attachment path escapes the opportunity folder")
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() == sha:
                return str(target)
            target = (base / f"{sha[:16]}_{uuid.uuid4().hex[:8]}_{safe}").resolve()
        fd, tmp_name = tempfile.mkstemp(dir=base, prefix=".part-")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(tmp, target)  # fails instead of replacing an existing file
            except FileExistsError:
                if hashlib.sha256(target.read_bytes()).hexdigest() != sha:
                    raise
            except OSError:
                if target.exists():
                    raise
                os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        return str(target)

    def read(self, key: str) -> bytes | None:
        path = Path(key)
        return path.read_bytes() if path.is_file() else None


def get_store(settings: Settings) -> AttachmentStore:
    if settings.attachment_store == "local":
        return LocalAttachmentStore(Path(settings.data_dir))
    raise ValueError(f"unknown ATTACHMENT_STORE: {settings.attachment_store!r}")
