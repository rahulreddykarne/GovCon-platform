"""Load prompt markdown files and their flat front matter.

Production prompt bodies and registry activation are owned by later phases.
This loader only reads what is on disk and hashes the exact bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from govcon.prompting.hashing import sha256_bytes


@dataclass(frozen=True)
class PromptAsset:
    path: Path
    name: str
    version: str
    metadata: dict[str, str]
    body: str
    content_hash: str


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        raise ValueError("prompt file is missing YAML front matter")
    try:
        _, header, body = text.split("---", 2)
    except ValueError as exc:
        raise ValueError("prompt front matter is not closed") from exc
    metadata: dict[str, str] = {}
    for line in header.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        metadata[key.strip()] = value.strip().strip("'").strip('"')
    return metadata, body.lstrip("\n")


def load_markdown_prompt(path: Path) -> PromptAsset:
    raw = path.read_bytes()
    metadata, body = parse_front_matter(raw.decode("utf-8"))
    name = metadata.get("name")
    version = metadata.get("version")
    if not name or not version:
        raise ValueError(f"{path} front matter requires name and version")
    return PromptAsset(
        path=path,
        name=name,
        version=version,
        metadata=metadata,
        body=body,
        content_hash=sha256_bytes(raw),
    )


def iter_markdown_prompts(root: Path) -> list[PromptAsset]:
    if not root.is_dir():
        raise FileNotFoundError(f"prompt root does not exist: {root}")
    return [load_markdown_prompt(path) for path in sorted(root.rglob("*.md"))]
