#!/usr/bin/env python3
"""Выполнить установку и аудит APM с барьерами Python-артефактов."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проверить состояние, выполнить apm install --frozen и аудит.",
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--apm", default="apm", help="Путь к исполняемому APM.")
    parser.add_argument(
        "--audit-runner",
        type=Path,
        help="Необязательный Python-запускатель аудита. Выполняется из корня проекта.",
    )
    return parser.parse_args()


def run(command: list[str], root: Path, env: dict[str, str]) -> int:
    result = subprocess.run(command, cwd=root, env=env, check=False)
    return result.returncode


def main() -> int:
    args = parse_args()
    root = args.project_root.resolve()
    if not root.is_dir():
        print(f"Не найден каталог проекта: {root}", file=sys.stderr)
        return 2
    validator = root / "tools" / "validate-python-artifacts.py"
    if not validator.is_file():
        print(f"Не найден обязательный валидатор: {validator}", file=sys.stderr)
        return 2
    if args.audit_runner is not None:
        audit_runner = args.audit_runner
        if not audit_runner.is_absolute():
            audit_runner = root / audit_runner
        if not audit_runner.is_file():
            print(f"Не найден запускатель аудита: {audit_runner}", file=sys.stderr)
            return 2
    else:
        audit_runner = None

    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    preflight = [sys.executable, str(validator)]
    if run(preflight, root, env) != 0:
        print("Установка APM остановлена предварительным барьером.", file=sys.stderr)
        return 1
    if run([args.apm, "install", "--frozen"], root, env) != 0:
        return 1
    if run(preflight, root, env) != 0:
        print("Установка APM создала или распространила Python-артефакт.", file=sys.stderr)
        return 1

    audit = (
        [sys.executable, str(audit_runner)]
        if audit_runner is not None
        else [args.apm, "audit", "--ci"]
    )
    if run(audit, root, env) != 0:
        return 1
    if run(preflight, root, env) != 0:
        print("Аудит APM создал или распространил Python-артефакт.", file=sys.stderr)
        return 1
    print("Безопасный цикл установки и аудита APM пройден.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
