"""H4: prompt files load the same way regardless of line endings."""

from __future__ import annotations

from pathlib import Path

from govcon.prompting.hashing import sha256_bytes
from govcon.prompting.loader import load_markdown_prompt, parse_front_matter

PROMPT_ROOT = Path(__file__).parent.parent / "src" / "govcon" / "prompts"


def test_crlf_front_matter_parses_like_lf() -> None:
    lf = "---\nname: demo\nversion: v1\nstatus: active\n---\nBody line one.\nBody line two.\n"
    crlf = lf.replace("\n", "\r\n")
    assert parse_front_matter(crlf) == parse_front_matter(lf)


def test_crlf_prompt_file_loads_and_hashes_raw_bytes(tmp_path: Path) -> None:
    source = (PROMPT_ROOT / "deepseek" / "solicitation_analysis_v1.md").read_bytes().replace(b"\r\n", b"\n")
    lf_path = tmp_path / "lf.md"
    crlf_path = tmp_path / "crlf.md"
    lf_path.write_bytes(source)
    crlf_path.write_bytes(source.replace(b"\n", b"\r\n"))
    lf_asset = load_markdown_prompt(lf_path)
    crlf_asset = load_markdown_prompt(crlf_path)
    assert crlf_asset.name == lf_asset.name == "solicitation_analysis"
    assert crlf_asset.metadata == lf_asset.metadata
    # The hash covers the exact bytes on disk (reproducibility of what was sent).
    assert crlf_asset.content_hash == sha256_bytes(crlf_path.read_bytes())


def test_every_shipped_prompt_loads() -> None:
    files = sorted(PROMPT_ROOT.rglob("*.md"))
    assert files
    for path in files:
        asset = load_markdown_prompt(path)
        assert asset.name and asset.version
