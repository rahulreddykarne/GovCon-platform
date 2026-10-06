"""Compose a full system prompt from a task prompt and its shared fragment includes.

The ``includes`` front-matter key lists shared prompt names (e.g.
``shared/source_security_rules_v1``). The renderer resolves each include
to its on-disk body and prepends them before the task prompt body.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path

from govcon.prompting.loader import PromptAsset, load_markdown_prompt

logger = logging.getLogger("govcon.prompting.renderer")


def render_system_prompt(asset: PromptAsset, prompt_root: Path) -> str:
    """Build the full system prompt text.

    Resolves ``includes`` from front matter and prepends each fragment
    body before the task prompt body. Fragment names are relative paths
    under ``prompt_root`` without the ``.md`` extension. A prompt that
    declares a registered ``schema_version`` ends with that schema, so the
    model knows the exact JSON it must return.
    """
    includes_raw = asset.metadata.get("includes", "")
    schema = _output_schema_block(asset)
    if not includes_raw:
        return asset.body + (f"\n\n{schema}" if schema else "")

    include_names = _parse_includes(includes_raw)
    parts: list[str] = []
    for name in include_names:
        fragment = _resolve_include(name, prompt_root)
        if fragment is None:
            raise PromptRenderError(f"required shared include {name!r} is missing or cannot be loaded")
        parts.append(fragment.body.strip())
    parts.append(asset.body.strip())
    if schema:
        parts.append(schema)
    return "\n\n".join(parts)


def _output_schema_block(asset: PromptAsset) -> str | None:
    """The output contract, as JSON Schema. Without it a model invents its own keys
    (seen 2026-10-05: deepseek-flash answered solicitation_analysis with its own
    structure, which validated as an empty summary)."""
    import json

    from govcon.ai.schemas import SCHEMA_REGISTRY

    schema_cls = SCHEMA_REGISTRY.get(asset.metadata.get("schema_version", ""))
    if schema_cls is None:
        return None
    schema = json.dumps(schema_cls.model_json_schema(), separators=(",", ":"), sort_keys=True)
    return ("OUTPUT SCHEMA\nReturn one JSON object that validates against this JSON Schema. Use exactly these "
            "property names and types; put nothing outside them. Use null or [] for values the sources do not "
            f"state.\n{schema}")


class PromptRenderError(ValueError):
    """A required prompt variable was missing or the prompt could not be rendered."""


def required_variables(asset: PromptAsset) -> list[str]:
    """Return the ``required_variables`` names declared in front matter."""
    return _parse_includes(asset.metadata.get("required_variables", ""))


def render_user_context(asset: PromptAsset, variables: Mapping[str, object]) -> str:
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
