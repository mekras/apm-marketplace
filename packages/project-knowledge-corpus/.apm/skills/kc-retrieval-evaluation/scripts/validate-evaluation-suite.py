#!/usr/bin/env python3
"""Проверить набор оценки доступности знаний."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evaluation_suite import load_yaml, validate_suite


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проверить структуру и опорные фрагменты набора оценки.",
    )
    parser.add_argument("suite", type=Path, help="Файл YAML с набором проверки.")
    parser.add_argument(
        "--corpus-root",
        type=Path,
        help="Корень корпуса для проверки путей, строк и хэшей.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.suite.is_file():
        print(f"Набор не найден: {args.suite}", file=sys.stderr)
        return 2
    if args.corpus_root is not None and not args.corpus_root.is_dir():
        print(f"Корень корпуса не найден: {args.corpus_root}", file=sys.stderr)
        return 2

    try:
        data = load_yaml(args.suite)
    except OSError as exc:
        print(f"Не удалось прочитать набор: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Файл набора не разобран как YAML: {exc}", file=sys.stderr)
        return 2

    errors, warnings = validate_suite(data, corpus_root=args.corpus_root)
    for warning in warnings:
        print(f"Предупреждение: {warning}")
    if errors:
        for error in errors:
            print(f"Ошибка: {error}", file=sys.stderr)
        return 1

    cases = data.get("cases", [])
    approved = sum(
        1 for case in cases if isinstance(case, dict) and case.get("status") == "approved"
    )
    print(f"Набор прошёл проверку. Примеров: {len(cases)}, принято: {approved}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
