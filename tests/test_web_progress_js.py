"""Run the progress controller's form-preservation and retry regressions."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_progress_controller():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the browser progress controller tests")
    result = subprocess.run([node, "--test", str(Path(__file__).with_name("web_progress.test.cjs"))],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
