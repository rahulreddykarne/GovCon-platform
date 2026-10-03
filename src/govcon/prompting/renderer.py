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
        if fragment is None:
            raise PromptRenderError(
                f"prompt {asset.name}@{asset.version} requires shared include {name!r}, "
                "which is missing or unreadable"
            )
        parts.append(fragment.body.strip())
    parts.append(asset.body.strip())
    return "\n\n".join(parts)


class PromptRenderError(ValueError):
    """A required prompt variable was missing or the prompt could not be rendered."""


def required_variables(asset: PromptAsset) -> list[str]:
    """Return the ``required_variables`` names declared in front matter."""
    return _parse_includes(asset.metadata.get("required_variables", ""))


def render_user_context(asset: PromptAsset, variables: dict[str, object]) -> str:
    """Render bounded structured context as the user message.

    Source content is untrusted data, so it is delivered in delimited data
    blocks in the user message and never concatenated into system
    instructions (§37.2). Every name listed in ``required_variables`` must be
    present; a missing variable is a render error (§37.3).
    """
    import json

    required = required_variables(asset)
    missing = [name for name in required if name not in variables or variables[name] is None]
    if missing:
        raise PromptRenderError(f"prompt {asset.name}@{asset.version} missing required variables: {', '.join(missing)}")
    ordered = required + sorted(name for name in variables if name not in required)
    blocks: list[str] = []
    for name in ordered:
        value = variables[name]
        body = value if isinstance(value, str) else json.dumps(value, indent=2, sort_keys=True, default=str)
        blocks.append(f"<<<BEGIN {name}>>>\n{body}\n<<<END {name}>>>")
    return "\n\n".join(blocks)


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
