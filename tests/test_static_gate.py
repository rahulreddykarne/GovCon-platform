"""Every static diagnostic must fail the release gate."""

import importlib.util
from collections import Counter
from pathlib import Path
from typing import get_type_hints


def test_static_gate_requires_zero_errors(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("static_gate", Path(__file__).resolve().parents[1] / "scripts/check_static.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    old = gate.diagnostic_key("mypy", "src/govcon/_build.py", "import-untyped",
                              'Library stubs not installed for "setuptools.command.build_py"')
    monkeypatch.setattr(gate, "collect", Counter)
    assert gate.main() == 0
    monkeypatch.setattr(gate, "collect", lambda: Counter({old: 1}))
    assert gate.main() == 1
    monkeypatch.setattr(gate, "collect", lambda: Counter({old: 2}))
    assert gate.main() == 1
    new = gate.diagnostic_key("mypy", "src/new_feature.py", "union-attr", "None has no attribute id")
    monkeypatch.setattr(gate, "collect", lambda: Counter({old: 1, new: 1}))
    assert gate.main() == 1 and "mypy: src/new_feature.py" in capsys.readouterr().out


def test_notification_annotations_resolve_for_runtime_introspection():
    from govcon.collaboration.notifications import acknowledge
    from govcon.models import User

    assert get_type_hints(acknowledge)["user"] is User
