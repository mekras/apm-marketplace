#!/usr/bin/env python3
"""Регрессии для барьера Python-артефактов."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VALIDATOR = ROOT / "validate-python-artifacts.py"


def run(project: Path, *paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), *(str(path) for path in paths)],
        cwd=project,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def compile_explicitly(source: Path, *, isolated: bool) -> None:
    command = [sys.executable]
    if isolated:
        command.append("-B")
    command.extend(["-m", "py_compile", str(source)])
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"} if not isolated else None
    result = subprocess.run(command, env=env, check=False)
    assert result.returncode == 0


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        project = Path(temporary)
        source = project / ".apm" / "skills" / "example" / "module.py"
        source.parent.mkdir(parents=True)
        source.write_text("VALUE = 1\n", encoding="utf-8")
        assert run(project).returncode == 0

        loader = (
            "import importlib.util, pathlib; "
            f"p=pathlib.Path({str(source)!r}); "
            "s=importlib.util.spec_from_file_location('example_module', p); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m)"
        )
        unprotected_env = dict(os.environ)
        unprotected_env.pop("PYTHONDONTWRITEBYTECODE", None)
        loaded = subprocess.run(
            [sys.executable, "-c", loader],
            env=unprotected_env,
            check=False,
        )
        assert loaded.returncode == 0
        rejected_import = run(project)
        assert rejected_import.returncode == 1
        assert "__pycache__" in rejected_import.stderr

    for isolated in (True, False):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            source = project / ".apm" / "skills" / "example" / "module.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            compile_explicitly(source, isolated=isolated)
            rejected_compile = run(project)
            assert rejected_compile.returncode == 1
            assert "__pycache__" in rejected_compile.stderr

    with tempfile.TemporaryDirectory() as temporary:
        project = Path(temporary)
        (project / ".apm").mkdir()
        (project / "apm.lock.yaml").write_text(
            "file_hashes:\n"
            "  .agents/skills/example/module.pyc: sha256:example\n",
            encoding="utf-8",
        )
        rejected_lock = run(project)
        assert rejected_lock.returncode == 1
        assert "apm.lock.yaml:2" in rejected_lock.stderr

    print("Барьер Python-артефактов проверен для импорта, компилятора и lock-файла.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
