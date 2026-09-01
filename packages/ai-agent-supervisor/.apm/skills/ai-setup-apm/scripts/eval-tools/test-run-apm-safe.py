#!/usr/bin/env python3
"""Регрессии для безопасного цикла установки и аудита APM."""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


SOURCE_TOOLS = Path(__file__).resolve().parent
RUNNER = SOURCE_TOOLS / "run-apm-safe.py"


def prepare(project: Path, mode: str = "clean") -> Path:
    tools = project / "tools"
    tools.mkdir()
    shutil.copy2(SOURCE_TOOLS / "validate-python-artifacts.py", tools)
    (project / ".apm" / "skills" / "example").mkdir(parents=True)
    (project / "apm.lock.yaml").write_text("deployments: []\n", encoding="utf-8")
    fake = project / "fake-apm"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "assert os.environ.get('PYTHONDONTWRITEBYTECODE') == '1'\n"
        "root = pathlib.Path.cwd()\n"
        "with (root / 'apm-calls.log').open('a', encoding='utf-8') as stream:\n"
        "    stream.write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"mode = {mode!r}\n"
        "if mode == 'install-cache' and sys.argv[1] == 'install':\n"
        "    cache = root / '.agents/skills/example/__pycache__'\n"
        "    cache.mkdir(parents=True, exist_ok=True)\n"
        "    (cache / 'module.cpython-313.pyc').write_bytes(b'cache')\n"
        "if mode == 'audit-cache' and sys.argv[1] == 'audit':\n"
        "    cache = root / '.claude/skills/example/__pycache__'\n"
        "    cache.mkdir(parents=True, exist_ok=True)\n"
        "    (cache / 'module.cpython-313.pyc').write_bytes(b'cache')\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


def run(project: Path, fake: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--project-root",
            str(project),
            "--apm",
            str(fake),
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        project = Path(temporary)
        passed = run(project, prepare(project))
        assert passed.returncode == 0, passed.stdout + passed.stderr
        assert (project / "apm-calls.log").read_text(encoding="utf-8").splitlines() == [
            "install --frozen",
            "audit --ci",
        ]

    with tempfile.TemporaryDirectory() as temporary:
        project = Path(temporary)
        fake = prepare(project)
        cache = project / ".apm" / "skills" / "example" / "__pycache__"
        cache.mkdir()
        (cache / "module.cpython-313.pyc").write_bytes(b"cache")
        rejected_source = run(project, fake)
        assert rejected_source.returncode == 1
        assert not (project / "apm-calls.log").exists()

    with tempfile.TemporaryDirectory() as temporary:
        project = Path(temporary)
        fake = prepare(project)
        (project / "apm.lock.yaml").write_text(
            "deployments:\n- value: .agents/example/__pycache__/module.cpython-313.pyc\n",
            encoding="utf-8",
        )
        rejected_lock = run(project, fake)
        assert rejected_lock.returncode == 1
        assert not (project / "apm-calls.log").exists()

    with tempfile.TemporaryDirectory() as temporary:
        project = Path(temporary)
        rejected_install = run(project, prepare(project, "install-cache"))
        assert rejected_install.returncode == 1
        assert (project / "apm-calls.log").read_text(encoding="utf-8").splitlines() == [
            "install --frozen"
        ]

    with tempfile.TemporaryDirectory() as temporary:
        project = Path(temporary)
        rejected_audit = run(project, prepare(project, "audit-cache"))
        assert rejected_audit.returncode == 1
        assert (project / "apm-calls.log").read_text(encoding="utf-8").splitlines() == [
            "install --frozen",
            "audit --ci",
        ]

    print("Безопасный цикл APM проверен до и после установки.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
