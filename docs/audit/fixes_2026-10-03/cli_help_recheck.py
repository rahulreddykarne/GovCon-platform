"""Recheck every registered CLI command without changing audit evidence."""
import ast
import json
from pathlib import Path

from typer.testing import CliRunner
from govcon.cli import app


def test_all_registered_cli_help_still_works():
    root = Path(__file__).resolve().parents[3]
    tree = ast.parse((root / "src/govcon/cli.py").read_text(encoding="utf-8"))
    groups = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_typer" and ast.unparse(node.func.value) == "app":
            groups[ast.unparse(node.args[0])] = next(k.value.value for k in node.keywords if k.arg == "name")
    checked = []
    failures = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr == "command":
                    group = ast.unparse(decorator.func.value)
                    command = decorator.args[0].value
                    args = ([groups[group]] if group != "app" else []) + [command, "--help"]
                    result = CliRunner().invoke(app, args)
                    checked.append(" ".join(args[:-1]))
                    if result.exit_code:
                        failures.append({"command": args[:-1], "exit_code": result.exit_code})
    Path(__file__).with_name("cli-help-results.json").write_text(json.dumps({"commands": checked, "failures": failures}, indent=2), encoding="utf-8")
    assert len(checked) == 96 and failures == []
