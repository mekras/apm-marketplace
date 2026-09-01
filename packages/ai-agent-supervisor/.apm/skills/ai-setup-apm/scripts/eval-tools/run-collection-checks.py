#!/usr/bin/env python3
"""Run deterministic skill collection checks without a shell pipeline."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run deterministic collection checks.")
    parser.add_argument(
        "skills_path",
        nargs="?",
        default=os.environ.get("APM_EVAL_PATH", ".apm/skills"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path.cwd()
    tools = root / "tools"
    checks = [
        [tools / "validate-python-artifacts.py"],
        [tools / "validate-python-syntax.py"],
        [tools / "test-validate-python-artifacts.py"],
        [tools / "test-validate-python-syntax.py"],
        [tools / "test-run-apm-safe.py"],
        [tools / "validate-hidden-unicode.py"],
        [tools / "validate-skill-descriptions.py", Path(args.skills_path)],
        [tools / "validate-trigger-evals.py", Path(args.skills_path), "--require-all"],
        [tools / "validate-skill-result-evals.py", Path(args.skills_path)],
        [tools / "validate-skill-portability.py", Path(args.skills_path)],
    ]
    if (root / "evals" / "fixtures" / "registry.json").is_file():
        checks.append([tools / "validate-fixture-evals.py"])
        checks.append([tools / "run-fixture-checks.py"])
    checks.append([tools / "validate-python-artifacts.py"])
    for command in checks:
        script = command[0]
        if not script.is_file():
            print(f"Не найдена обязательная проверка: {script}", file=sys.stderr)
            return 2
        rendered = [sys.executable, *(str(value) for value in command)]
        result = subprocess.run(
            rendered,
            cwd=root,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            check=False,
        )
        if result.returncode != 0:
            return result.returncode
    print("Детерминированные проверки коллекции пройдены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
