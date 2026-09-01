#!/usr/bin/env python3
"""Проверить синтаксис Python без импорта и записи байткода."""

from __future__ import annotations

import argparse
import os
import sys
import tokenize
from pathlib import Path


DEFAULT_PATHS = (Path(".apm"), Path("tools"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проверить синтаксис Python в памяти без py_compile и compileall.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Файлы или каталоги. По умолчанию существующие .apm и tools.",
    )
    return parser.parse_args()


def python_files(path: Path) -> list[Path]:
    if path.is_file() or path.is_symlink():
        return [path] if path.suffix == ".py" else []
    found: list[Path] = []
    for directory, subdirectories, files in os.walk(path, followlinks=False):
        subdirectories[:] = [name for name in subdirectories if name != "__pycache__"]
        current = Path(directory)
        found.extend(current / name for name in files if Path(name).suffix == ".py")
    return found


def main() -> int:
    args = parse_args()
    paths = args.paths if args.paths else [path for path in DEFAULT_PATHS if path.exists()]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        print(f"Пути не найдены: {', '.join(missing)}", file=sys.stderr)
        return 2

    failures: list[str] = []
    files = sorted({item.resolve() for path in paths for item in python_files(path)})
    for path in files:
        try:
            with tokenize.open(path) as source:
                compile(source.read(), str(path), "exec", dont_inherit=True)
        except (OSError, SyntaxError, UnicodeError) as error:
            failures.append(f"{path}: {error}")
    if failures:
        print("Найдены Python-файлы с ошибками синтаксиса:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(f"Python-файлы прошли синтаксическую проверку без записи байткода: {len(files)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
