#!/usr/bin/env python3
"""Проверки синтаксического валидатора без записи байткода."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VALIDATOR = ROOT / "validate-python-syntax.py"


def run(project: Path, path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), str(path)],
        cwd=project,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        project = Path(temporary)
        source = project / ".apm" / "skills" / "example" / "module.py"
        source.parent.mkdir(parents=True)
        source.write_text("VALUE = 1\n", encoding="utf-8")
        passed = run(project, source.parent)
        assert passed.returncode == 0, passed.stderr
        assert not list(project.rglob("*.pyc"))
        assert not list(project.rglob("__pycache__"))

        source.write_text("def broken(:\n", encoding="utf-8")
        failed = run(project, source)
        assert failed.returncode == 1
        assert "ошибками синтаксиса" in failed.stderr
        assert not list(project.rglob("*.pyc"))
        assert not list(project.rglob("__pycache__"))

    print("Синтаксическая проверка без записи байткода проверена.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
