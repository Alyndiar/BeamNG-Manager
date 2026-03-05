from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core import junctions


@pytest.mark.skipif(os.name != "nt", reason="Windows-only junction tests")
def test_is_junction_false_for_normal_dir(tmp_path: Path) -> None:
    normal = tmp_path / "normal"
    normal.mkdir()
    assert junctions.is_junction(normal) is False


@pytest.mark.skipif(os.name != "nt", reason="Windows-only junction tests")
def test_junction_detection_and_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    link = tmp_path / "link"
    target.mkdir()

    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    if result.returncode != 0:
        pytest.skip("Junction creation is not permitted in this environment")

    try:
        assert junctions.is_junction(link) is True
        resolved = junctions.get_junction_target(link)
        assert resolved is not None
        assert str(resolved).lower().rstrip("\\") == str(target).lower().rstrip("\\")
    finally:
        subprocess.run(["cmd", "/c", "rmdir", str(link)], check=False)
        shutil.rmtree(target, ignore_errors=True)
