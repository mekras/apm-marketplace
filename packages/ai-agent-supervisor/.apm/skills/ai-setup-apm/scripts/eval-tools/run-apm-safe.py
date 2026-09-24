#!/usr/bin/env python3
"""Выполнить установку и аудит APM с барьерами Python-артефактов."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import os
import subprocess
import sys
from pathlib import Path

# Русские сообщения не должны падать на консоли с однобайтовой кодировкой.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8", errors="replace")


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
    parser.add_argument(
        "--allow-unpublished-local-version",
        action="store_true",
        help=(
            "Передать запускателю аудита явное разрешение на проверяемую "
            "локальную версию, ещё не опубликованную в реестре."
        ),
    )
    return parser.parse_args()


def run(command: list[str], root: Path, env: dict[str, str]) -> int:
    result = subprocess.run(command, cwd=root, env=env, check=False)
    return result.returncode


IGNORED_LOCK_GRAPH_BLOCKS = frozenset({"deployed_files", "deployed_file_hashes"})
IGNORED_LOCK_GRAPH_FIELDS = frozenset({"content_hash", "resolved_at"})


def indentation(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def yaml_key_value(line: str) -> tuple[str | None, str | None]:
    value = line.strip()
    if value.startswith("- "):
        value = value[2:].lstrip()
    key, separator, scalar = value.partition(":")
    if not separator:
        return None, None
    return key.strip(), scalar.strip()


def normalize_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def canonical_graph_line(line: str) -> str:
    key, value = yaml_key_value(line)
    if key is None or value is None:
        return line.strip()
    return f"{key}={normalize_scalar(value)}"


def lock_graph_snapshot(lockfile: Path) -> str:
    """Return a stable representation of the dependency graph in a lockfile."""

    lines = lockfile.read_text(encoding="utf-8").splitlines()
    top_level: list[str] = []
    dependencies: list[tuple[str, ...]] = []
    dependency: list[str] | None = None
    in_dependencies = False
    skip_indent: int | None = None

    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        level = indentation(line)

        if not in_dependencies:
            if level != 0:
                continue
            key, value = yaml_key_value(line)
            if key == "dependencies":
                if value not in ("", "[]"):
                    raise ValueError(
                        f"строка {line_number}: неподдерживаемая запись dependencies"
                    )
                in_dependencies = True
            elif key in {"lockfile_version", "apm_version"} and value is not None:
                top_level.append(f"{key}={normalize_scalar(value)}")
            continue

        if level == 0 and stripped.startswith("- "):
            if dependency is not None:
                dependencies.append(tuple(sorted(dependency)))
            dependency = [canonical_graph_line(stripped[2:])]
            skip_indent = None
            continue

        if level == 0:
            if dependency is not None:
                dependencies.append(tuple(sorted(dependency)))
            dependency = None
            in_dependencies = False
            continue

        if dependency is None:
            raise ValueError(
                f"строка {line_number}: поле зависимости вне записи пакета"
            )

        if skip_indent is not None:
            if level <= skip_indent and not stripped.startswith("- "):
                skip_indent = None
            else:
                continue

        key, value = yaml_key_value(line)
        if key is None:
            continue
        if level == 2 and key in IGNORED_LOCK_GRAPH_BLOCKS:
            if value == "":
                skip_indent = level
            continue
        if level == 2 and key in IGNORED_LOCK_GRAPH_FIELDS:
            continue
        dependency.append(canonical_graph_line(line))

    if dependency is not None:
        dependencies.append(tuple(sorted(dependency)))

    parts = [f"top:{item}" for item in sorted(top_level)]
    for item in sorted(dependencies):
        parts.append("dependency")
        parts.extend(item)
        parts.append("end-dependency")
    return "\n".join(parts) + "\n"


def lock_graph_digest(snapshot: str) -> str:
    return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()


def report_lock_graph_change(before: str, after: str) -> None:
    before_digest = lock_graph_digest(before)
    after_digest = lock_graph_digest(after)
    print(
        "Безопасный цикл APM остановлен: "
        "apm install --frozen изменил lock-граф.",
        file=sys.stderr,
    )
    print(f"Снимок до установки: sha256:{before_digest}", file=sys.stderr)
    print(f"Снимок после установки: sha256:{after_digest}", file=sys.stderr)
    diff = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile="lock-graph-before",
            tofile="lock-graph-after",
            lineterm="",
        )
    )
    if diff:
        limit = 80
        print("Различия lock-графа:", file=sys.stderr)
        print("\n".join(diff[:limit]), file=sys.stderr)
        if len(diff) > limit:
            print("Различия сокращены после 80 строк.", file=sys.stderr)


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

    lockfile = root / "apm.lock.yaml"
    try:
        before_lock_graph = lock_graph_snapshot(lockfile)
    except (OSError, ValueError) as error:
        print(f"Не удалось сохранить снимок lock-графа: {error}", file=sys.stderr)
        return 1

    install_status = run([args.apm, "install", "--frozen"], root, env)
    try:
        after_lock_graph = lock_graph_snapshot(lockfile)
    except (OSError, ValueError) as error:
        print(f"Не удалось проверить lock-граф после установки: {error}", file=sys.stderr)
        return 1
    if before_lock_graph != after_lock_graph:
        report_lock_graph_change(before_lock_graph, after_lock_graph)
        return 1
    if install_status != 0:
        return 1
    if run(preflight, root, env) != 0:
        print("Установка APM создала или распространила Python-артефакт.", file=sys.stderr)
        return 1

    audit = (
        [
            sys.executable,
            str(audit_runner),
            *(
                ["--allow-unpublished-local-version"]
                if args.allow_unpublished_local_version
                else []
            ),
        ]
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
