"""Preserve each fix's immediate predecessor, including earlier completed fixes."""
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).parent
before = json.loads((OUT / "before.json").read_text(encoding="utf-8"))
target = Path(before["backup_root"]) / "stages" / sys.argv[1]
for name in sys.argv[2:]:
    source = (ROOT / name).resolve()
    assert source.is_relative_to(ROOT.resolve())
    destination = target / source.relative_to(ROOT)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
print("Saved predecessor for", sys.argv[1], sys.argv[2:])
