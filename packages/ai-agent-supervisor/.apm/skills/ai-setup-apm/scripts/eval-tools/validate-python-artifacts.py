#!/usr/bin/env python3
"""Запретить Python-байткод в защищённых деревьях и файле блокировки APM."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


BYTECODE_SUFFIXES = {".pyc", ".pyo"}
DEFAULT_PATHS = (
    Path(".apm"),
    Path(".agents"),
    Path(".claude"),
    Path(".codex"),
    Path("tools"),
)
LOCK_MARKER = re.compile(
    r"(?:^|[/\\])__pycache__(?:[/\\]|$)|\.(?:pyc|pyo)(?:$|[:\s'\"])",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проверить защищённые деревья и lock-файл APM на Python-байткод.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help=(
            "Каталоги или файлы для проверки. По умолчанию проверяются существующие "
            ".apm, .agents, .claude, .codex и tools."
        ),
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        help="Файл блокировки APM. По умолчанию проверяется существующий apm.lock.yaml.",
    )
    return parser.parse_args()


def relative(path: Path) -> str:
    try:
        return path.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


def find_artifacts(path: Path) -> list[Path]:
    if path.is_file() or path.is_symlink():
        return [path] if path.name == "__pycache__" or path.suffix in BYTECODE_SUFFIXES else []

    findings: list[Path] = []
    for directory, subdirectories, files in os.walk(path, followlinks=False):
        current = Path(directory)
        if "__pycache__" in subdirectories:
            findings.append(current / "__pycache__")
            subdirectories.remove("__pycache__")
        findings.extend(
            current / name
            for name in files
            if name == "__pycache__" or Path(name).suffix in BYTECODE_SUFFIXES
        )
    return findings


def find_lockfile_artifacts(path: Path) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        semantic = line.split("#", 1)[0].strip()
        if semantic and LOCK_MARKER.search(semantic):
            findings.append((number, semantic))
    return findings


def main() -> int:
    args = parse_args()
    explicit_paths = bool(args.paths)
    paths = args.paths if explicit_paths else [path for path in DEFAULT_PATHS if path.exists()]
    lockfile = args.lock_file
    if lockfile is None:
        default_lockfile = Path("apm.lock.yaml")
        lockfile = default_lockfile if default_lockfile.is_file() else None

    missing = [str(path) for path in paths if not path.exists()]
    if args.lock_file is not None and not args.lock_file.is_file():
        missing.append(str(args.lock_file))
    if missing:
        print(f"Пути не найдены: {', '.join(missing)}", file=sys.stderr)
        return 2

    findings = sorted(
        {artifact.resolve() for path in paths for artifact in find_artifacts(path)},
    )
    try:
        lock_findings = find_lockfile_artifacts(lockfile) if lockfile else []
    except (OSError, UnicodeError) as error:
        print(f"Не удалось прочитать файл блокировки {lockfile}: {error}", file=sys.stderr)
        return 2
    if findings or lock_findings:
        print(
            "В защищённом состоянии проекта найдены скомпилированные Python-артефакты:",
            file=sys.stderr,
        )
        for artifact in findings:
            print(f"  - {relative(artifact)}", file=sys.stderr)
        for number, value in lock_findings:
            print(f"  - {relative(lockfile)}:{number}: {value}", file=sys.stderr)
        print(
            "Удалите производные артефакты, пересоздайте загрязнённый lock-файл "
            "из чистого исходного дерева и используйте безопасный запуск Python. "
            "Не применяйте py_compile или compileall к защищённым деревьям.",
            file=sys.stderr,
        )
        return 1

    print("Скомпилированных Python-артефактов в защищённом состоянии проекта нет.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
