"""Split a document set into cited chunks and per-call batches (ADR-066).

Every chunk names its file and page, so AI output can cite the page it came
from. Batches are sized in UTF-8 bytes, the unit the AI budget reserves, so a
batch never exceeds the per-call input limit. Nothing is dropped here: the
caller decides what to do when the per-opportunity budget runs out, and
records every chunk it did not send as a coverage gap.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

# Long pages are split at paragraph, then line, boundaries into pieces of at
# most this many bytes, so one dense page never overflows a call on its own.
# Sized so a 20-page RFQ produces more, smaller parts whose answers fit the
# DeepSeek 8,192-token output cap.
MAX_CHUNK_BYTES = 4_000


def nbytes(text: str) -> int:
    return len(text.encode("utf-8"))


@dataclass(frozen=True)
class SourceChunk:
    file_id: int | None
    filename: str | None
    page: int | None
    label: str | None
    text: str
    part: int = 1  # 1-based piece of the page when a page was split

    @property
    def header(self) -> str:
        where = f"page={self.page}" if self.page is not None else f"part={self.label or 'document'}"
        return f"### [{self.filename}] (file_id={self.file_id}, {where})"

    def render(self) -> str:
        return f"{self.header}\n{self.text}"

    @property
    def size(self) -> int:
        return nbytes(self.render()) + 2  # blank line between chunks


@dataclass
class Gap:
    file_id: int | None
    filename: str | None
    pages: list[int | str] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict:
        return {"file_id": self.file_id, "filename": self.filename, "pages": self.pages, "reason": self.reason}


def split_text(text: str, limit: int = MAX_CHUNK_BYTES) -> list[str]:
    """Split ``text`` into pieces of at most ``limit`` bytes at natural boundaries."""
    if nbytes(text) <= limit:
        return [text]
    pieces: list[str] = []
    current = ""
    for unit in re.split(r"(\n\s*\n|\n)", text):
        if nbytes(current + unit) <= limit:
            current += unit
            continue
        if current.strip():
            pieces.append(current)
        current = unit
        while nbytes(current) > limit:  # a single overlong line: cut by bytes
            head = current.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
            pieces.append(head)
            current = current[len(head):]
    if current.strip():
        pieces.append(current)
    return pieces


def chunks_for_pages(file_id: int | None, filename: str | None,
                     pages: Iterable[tuple[int | None, str | None, str]]) -> list[SourceChunk]:
    """Chunks for one file from ``(page, label, text)`` triples; blank pages yield none."""
    chunks: list[SourceChunk] = []
    for page, label, text in pages:
        if not text or not text.strip():
            continue
        for part, piece in enumerate(split_text(text), start=1):
            chunks.append(SourceChunk(file_id, filename, page, label, piece, part))
    return chunks


def batch_chunks(chunks: list[SourceChunk], byte_budget: int) -> list[list[SourceChunk]]:
    """Greedy, order-preserving batches whose rendered size fits ``byte_budget``."""
    batches: list[list[SourceChunk]] = []
    current: list[SourceChunk] = []
    used = 0
    for chunk in chunks:
        if current and used + chunk.size > byte_budget:
            batches.append(current)
            current, used = [], 0
        current.append(chunk)
        used += chunk.size
    if current:
        batches.append(current)
    return batches


def render_batch(batch: list[SourceChunk]) -> str:
    return "\n\n".join(chunk.render() for chunk in batch)


def split_source_batch(batch: list[SourceChunk]) -> list[list[SourceChunk]]:
    """Halve a truncated part so it can be retried as smaller calls.

    An unsplittable single chunk returns an empty list: the caller records a
    gap instead of looping on the same capped request.
    """
    if len(batch) >= 2:
        mid = len(batch) // 2
        return [batch[:mid], batch[mid:]]
    if not batch:
        return []
    chunk = batch[0]
    limit = max(nbytes(chunk.text) // 2, 500)
    pieces = split_text(chunk.text, limit)
    if len(pieces) < 2:
        return []
    return [
        [SourceChunk(chunk.file_id, chunk.filename, chunk.page, chunk.label, piece, part=index)]
        for index, piece in enumerate(pieces, start=1)
    ]


def split_text_batch(batch: list[dict]) -> list[list[dict]]:
    """Halve a dict-shaped extraction batch the same way as ``split_source_batch``."""
    if len(batch) >= 2:
        mid = len(batch) // 2
        return [batch[:mid], batch[mid:]]
    if not batch:
        return []
    chunk = dict(batch[0])
    text = str(chunk.get("text") or "")
    limit = max(nbytes(text) // 2, 500)
    pieces = split_text(text, limit)
    if len(pieces) < 2:
        return []
    out: list[list[dict]] = []
    for index, piece in enumerate(pieces, start=1):
        part = dict(chunk)
        part["text"] = piece
        part["chunk_id"] = f"{chunk.get('chunk_id', 'chunk')}:{index}"
        out.append([part])
    return out


def gaps_for(chunks: Iterable[SourceChunk], reason: str) -> list[Gap]:
    """Group unsent chunks into one gap per file, listing pages (or parts)."""
    by_file: dict[int | None, Gap] = {}
    for chunk in chunks:
        gap = by_file.setdefault(chunk.file_id, Gap(chunk.file_id, chunk.filename, reason=reason))
        where = chunk.page if chunk.page is not None else (chunk.label or "document")
        if where not in gap.pages:
            gap.pages.append(where)
    return list(by_file.values())
