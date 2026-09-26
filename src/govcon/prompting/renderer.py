"""Compose a full system prompt from a task prompt and its shared fragment includes.

The ``includes`` front-matter key lists shared prompt names (e.g.
``shared/source_security_rules_v1``). The renderer resolves each include
to its on-disk body and prepends them before the task prompt body.
"""

from __future__ import annotations

import logging
from pathlib import Path

from govcon.prompting.loader import PromptAsset, load_markdown_prompt

logger = logging.getLogger("govcon.prompting.renderer")


def render_system_prompt(asset: PromptAsset, prompt_root: Path) -> str:
    """Build the full system prompt text.

    Resolves ``includes`` from front matter and prepends each fragment
    body before the task prompt body. Fragment names are relative paths
    under ``prompt_root`` without the ``.md`` extension.
    """
    includes_raw = asset.metadata.get("includes", "")
    if not includes_raw:
        return asset.body

    include_names = _parse_includes(includes_raw)
    parts: list[str] = []
    for name in include_names:
        fragment = _resolve_include(name, prompt_root)
        if fragment:
            parts.append(fragment.body.strip())
    parts.append(asset.body.strip())
    return "\n\n".join(parts)


def _parse_includes(raw: str) -> list[str]:
    """Parse the includes value from front-matter YAML.

    The loader flattens YAML lists into a comma-separated string because
    it uses a simplified key:value parser rather than a full YAML library.
    Handles both ``"a, b, c"`` and ``"a\\n  - b\\n  - c"`` styles.
    """
    names: list[str] = []
    for part in raw.replace("\n", ",").split(","):
        cleaned = part.strip().lstrip("- ").strip()
        if cleaned:
            names.append(cleaned)
    return names


def _resolve_include(name: str, prompt_root: Path) -> PromptAsset | None:
    """Resolve a fragment name to a ``PromptAsset``.

    Tries ``<prompt_root>/<name>.md`` first, then searches recursively.
    """
    candidate = prompt_root / f"{name}.md"
    if candidate.exists():
        try:
            return load_markdown_prompt(candidate)
        except Exception:
            logger.warning("failed to load include %s from %s", name, candidate)
            return None

    base = name.rsplit("/", 1)[-1]
    for path in prompt_root.rglob(f"{base}.md"):
        try:
            return load_markdown_prompt(path)
        except Exception:
            continue
    logger.warning("include %s not found under %s", name, prompt_root)
    return None
