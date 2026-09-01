#!/usr/bin/env python3
"""Не допускает отслеживание Git проекций зависимостей APM."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def error(message: str) -> int:
    print(f"Ошибка проверки границы APM и Git: {message}", file=sys.stderr)
    return 2


def run(project_root: Path, *args: str) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(
            args,
            cwd=project_root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        error(f"не удалось запустить {args[0]!r}: {exc}")
        return None


def tracked_paths(project_root: Path) -> set[str] | None:
    result = run(project_root, "git", "ls-files", "-z")
    if result is None:
        return None
    if result.returncode != 0:
        details = result.stderr.decode("utf-8", errors="replace").strip()
        error(f"репозиторий Git недоступен в {project_root}: {details}")
        return None
    return {
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    }


def package_owners(project_root: Path, path: str) -> list[str] | None:
    result = run(project_root, "apm", "find", path)
    if result is None:
        return None
    decoded = result.stdout.decode("utf-8", errors="replace")
    if result.returncode == 1 and decoded.lstrip().startswith("[x]"):
        return []
    if result.returncode != 0:
        details = result.stderr.decode("utf-8", errors="replace").strip()
        error(f"apm find не смог проверить {path!r}: {details}")
        return None
    lines = decoded.splitlines()
    return [
        line.strip()
        for line in lines
        if line.strip() and line.strip() != "." and not line.lstrip().startswith("[x]")
    ]


def validate(project_root: Path) -> int:
    lockfile = project_root / "apm.lock.yaml"
    if not lockfile.is_file():
        return error(f"не найден файл блокировки APM: {lockfile}")
    tracked = tracked_paths(project_root)
    if tracked is None:
        return 2
    violations: list[tuple[str, str]] = []
    for path in sorted(tracked):
        owners = package_owners(project_root, path)
        if owners is None:
            return 2
        violations.extend((path, owner) for owner in owners)
    if violations:
        print("Найдены отслеживаемые проекции зависимостей APM:", file=sys.stderr)
        for path, owner in violations:
            print(
                f"- {path} (пакет: {owner}). Удалите файл из индекса Git и "
                "добавьте подходящее правило в .gitignore.",
                file=sys.stderr,
            )
        return 1
    print("Отслеживаемых Git проекций зависимостей APM не найдено.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Проверяет, что Git не отслеживает файлы зависимостей APM."
    )
    parser.add_argument("project_root", nargs="?", default=".", type=Path)
    args = parser.parse_args(argv)
    project_root = args.project_root.resolve()
    if not project_root.is_dir():
        return error(f"не найден каталог проекта: {project_root}")
    return validate(project_root)


if __name__ == "__main__":
    raise SystemExit(main())
