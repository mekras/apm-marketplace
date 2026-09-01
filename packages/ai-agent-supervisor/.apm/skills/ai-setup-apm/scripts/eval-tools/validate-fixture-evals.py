#!/usr/bin/env python3
"""Проверить структуру тестовых проектов модельной оценки."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверить реестр тестовых проектов.")
    parser.add_argument("registry", nargs="?", type=Path, default=Path("evals/fixtures/registry.json"))
    args = parser.parse_args()
    path = args.registry.resolve()
    if not path.is_file():
        print(f"Нет реестра проектных фикстур: {path}", file=sys.stderr)
        return 1
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"{path}: JSON не разобран: {exc}", file=sys.stderr)
        return 1
    errors: list[str] = []
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("cases"), list):
        errors.append("реестр должен содержать version: 1 и непустой массив cases")
    seen: set[str] = set()
    for index, case in enumerate(data.get("cases", []) if isinstance(data, dict) else []):
        label = f"cases[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{label}: должен быть объектом")
            continue
        for key in ("id", "prompt", "target_skill", "fixture", "oracle"):
            if not isinstance(case.get(key), str) or not case[key].strip():
                errors.append(f"{label}.{key}: обязательная непустая строка")
        case_id = case.get("id")
        if isinstance(case_id, str):
            if case_id in seen:
                errors.append(f"{label}.id: повтор {case_id!r}")
            seen.add(case_id)
        fixture = path.parent / str(case.get("fixture", ""))
        oracle = path.parent / str(case.get("oracle", ""))
        if not fixture.is_dir() or not any(item.is_file() for item in fixture.rglob("*")):
            errors.append(f"{label}.fixture: нужен непустой каталог fixture")
        if not oracle.is_file():
            errors.append(f"{label}.oracle: файл не найден")
            continue
        try:
            oracle_data = json.loads(oracle.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{label}.oracle: JSON не разобран: {exc}")
            continue
        if not isinstance(oracle_data, dict) or not all(isinstance(oracle_data.get(key), list) and oracle_data[key] for key in ("success_criteria", "failure_indicators")):
            errors.append(f"{label}.oracle: обязательны непустые success_criteria и failure_indicators")
        required_diff = oracle_data.get("required_diff") if isinstance(oracle_data, dict) else None
        if required_diff is not None:
            if not isinstance(required_diff, dict):
                errors.append(f"{label}.oracle.required_diff: должен быть объектом")
            elif not any(isinstance(required_diff.get(key), list) for key in ("paths", "must_include", "must_not_include")):
                errors.append(f"{label}.oracle.required_diff: нужен хотя бы один список правил")
        fixture_checks = oracle_data.get("fixture_checks") if isinstance(oracle_data, dict) else None
        if fixture_checks is not None:
            if not isinstance(fixture_checks, list) or not fixture_checks:
                errors.append(f"{label}.oracle.fixture_checks: нужен непустой массив")
            else:
                for check_index, check in enumerate(fixture_checks):
                    check_label = f"{label}.oracle.fixture_checks[{check_index}]"
                    if not isinstance(check, dict):
                        errors.append(f"{check_label}: должен быть объектом")
                        continue
                    command = check.get("command")
                    if not isinstance(command, list) or not command or not all(
                        isinstance(part, str) and part for part in command
                    ):
                        errors.append(f"{check_label}.command: нужен непустой массив строк")
                    if not isinstance(check.get("exit_code"), int):
                        errors.append(f"{check_label}.exit_code: требуется целое число")
                    for key in ("stdout_contains", "stderr_contains", "json_file"):
                        if key in check and (not isinstance(check[key], str) or not check[key]):
                            errors.append(f"{check_label}.{key}: требуется непустая строка")
                    if "json_equals" in check and "json_file" not in check:
                        errors.append(f"{check_label}.json_equals: требуется json_file")
    if not seen:
        errors.append("cases не должен быть пустым")
    if errors:
        for error in errors:
            print(f"{path}: {error}", file=sys.stderr)
        return 1
    print(f"Проверено тестовых проектов: {len(seen)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
