#!/usr/bin/env python3
"""Validate skill trigger eval files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REQUIRED_CASE_FIELDS = {
    "id": str,
    "prompt": str,
    "should_trigger": bool,
    "rationale": str,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проверить файлы evals/triggers.json в каталогах навыков.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Каталоги навыков или корни репозитория для обхода. По умолчанию текущий каталог.",
    )
    parser.add_argument(
        "--require-all",
        action="store_true",
        help=(
            "Требовать файл evals/triggers.json у каждого найденного каталога навыка."
        ),
    )
    return parser.parse_args()


def find_skill_dirs(paths: list[Path]) -> list[Path]:
    skill_dirs: set[Path] = set()
    for path in paths:
        path = path.resolve()
        if (path / "SKILL.md").is_file():
            skill_dirs.add(path)
            continue
        for skill_file in path.rglob("SKILL.md"):
            if ".git" in skill_file.parts:
                continue
            skill_dirs.add(skill_file.parent)
    return sorted(skill_dirs)


def read_skill_name(skill_path: Path) -> str | None:
    in_frontmatter = False
    for line in skill_path.read_text(encoding="utf-8").splitlines():
        if line.strip() == "---":
            if not in_frontmatter:
                in_frontmatter = True
                continue
            break
        if in_frontmatter and line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    return None


def load_json(path: Path, errors: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"{path}: invalid JSON: {exc}")
    return None


def require_non_empty_string(
    value: Any,
    field: str,
    case_label: str,
    errors: list[str],
) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(
            f"{case_label}: field {field!r} must be a non-empty string",
        )


def validate_case(
    case: Any,
    index: int,
    skill_name: str,
    seen_ids: set[str],
    seen_prompts: set[str],
    errors: list[str],
) -> bool | None:
    case_label = f"{skill_name}: cases[{index}]"
    if not isinstance(case, dict):
        errors.append(f"{case_label}: must be an object")
        return None

    missing = sorted(set(REQUIRED_CASE_FIELDS) - set(case))
    if missing:
        errors.append(f"{case_label}: missing fields: {', '.join(missing)}")

    for field, expected_type in REQUIRED_CASE_FIELDS.items():
        if field not in case:
            continue
        value = case[field]
        if expected_type is bool:
            if not isinstance(value, bool):
                errors.append(f"{case_label}: field {field!r} must be boolean")
            continue
        require_non_empty_string(value, field, case_label, errors)

    case_id = case.get("id")
    if isinstance(case_id, str):
        if case_id in seen_ids:
            errors.append(f"{case_label}: duplicate id {case_id!r}")
        seen_ids.add(case_id)
        if not case_id.startswith(f"{skill_name}-"):
            errors.append(
                f"{case_label}: id {case_id!r} must start with {skill_name!r}",
            )

    prompt = case.get("prompt")
    if isinstance(prompt, str):
        if prompt in seen_prompts:
            errors.append(f"{case_label}: duplicate prompt")
        seen_prompts.add(prompt)

    should_trigger = case.get("should_trigger")
    if isinstance(should_trigger, bool):
        return should_trigger
    return None


def validate_trigger_file(skill_dir: Path, require_all: bool) -> list[str]:
    errors: list[str] = []
    trigger_path = skill_dir / "evals" / "triggers.json"
    skill_path = skill_dir / "SKILL.md"
    skill_name = read_skill_name(skill_path)

    if not skill_name:
        return [f"{skill_path}: missing frontmatter name"]

    if not trigger_path.exists():
        if require_all or (skill_dir / "evals").exists():
            errors.append(f"{trigger_path}: missing trigger eval file")
        return errors

    data = load_json(trigger_path, errors)
    if data is None:
        return errors

    if not isinstance(data, dict):
        return [f"{trigger_path}: root must be an object"]

    if data.get("skill_name") != skill_name:
        errors.append(
            f"{trigger_path}: skill_name must be {skill_name!r}, "
            f"got {data.get('skill_name')!r}",
        )

    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append(f"{trigger_path}: cases must be a non-empty array")
        return errors

    seen_ids: set[str] = set()
    seen_prompts: set[str] = set()
    trigger_values: list[bool] = []

    for index, case in enumerate(cases):
        value = validate_case(
            case,
            index,
            skill_name,
            seen_ids,
            seen_prompts,
            errors,
        )
        if value is not None:
            trigger_values.append(value)

    if True not in trigger_values:
        errors.append(
            f"{trigger_path}: add at least one should_trigger=true case",
        )
    if False not in trigger_values:
        errors.append(
            f"{trigger_path}: add at least one should_trigger=false case",
        )

    return errors


def main() -> int:
    args = parse_args()
    roots = args.paths or [Path.cwd()]
    skill_dirs = find_skill_dirs(roots)
    if not skill_dirs:
        print("Каталоги навыков не найдены.", file=sys.stderr)
        return 1

    errors: list[str] = []
    checked = 0
    for skill_dir in skill_dirs:
        skill_errors = validate_trigger_file(skill_dir, args.require_all)
        errors.extend(skill_errors)
        if (skill_dir / "evals" / "triggers.json").exists():
            checked += 1

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print(f"Проверено сценариев выбора навыка: {checked}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
