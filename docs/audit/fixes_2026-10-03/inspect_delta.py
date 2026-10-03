"""Check source scope, signatures and preservation of existing tests."""
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).parent
before = json.loads((OUT / "before.json").read_text())
backup = Path(before["backup_root"])


def signatures(tree):
    found = {}

    def visit(node, prefix=()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = ".".join((*prefix, node.name))
            found[name] = (ast.dump(node.args), ast.dump(node.returns) if node.returns else None,
                           tuple(ast.dump(d) for d in node.decorator_list))
            prefix = (*prefix, node.name)
        elif isinstance(node, ast.ClassDef):
            prefix = (*prefix, node.name)
        for child in ast.iter_child_nodes(node):
            visit(child, prefix)

    visit(tree)
    return found


changed = []
new_helpers = {}
existing_tests = 0
for name, digest in before["hashes"].items():
    target = ROOT / name
    current = hashlib.sha256(target.read_bytes()).hexdigest()
    if name.startswith("tests\\"):
        assert current == digest, f"Existing test changed: {name}"
        existing_tests += 1
    elif current != digest:
        old = signatures(ast.parse((backup / name).read_text(encoding="utf-8")))
        new = signatures(ast.parse(target.read_text(encoding="utf-8")))
        assert all(new.get(k) == value for k, value in old.items()), f"Existing signature/decorator changed: {name}"
        changed.append(name.replace("\\", "/"))
        if new.keys() - old.keys():
            new_helpers[name.replace("\\", "/")] = sorted(new.keys() - old.keys())
assert len(changed) == 8
result = {"production_files_changed": sorted(changed), "existing_signatures_and_decorators": "unchanged",
          "new_helpers": new_helpers, "existing_test_files_unchanged": existing_tests}
(OUT / "delta-check.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result))
