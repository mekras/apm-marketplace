#!/usr/bin/env python3
"""Validate skill result scenario eval files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проверить файлы evals/result-scenarios.json в каталогах навыков.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Каталоги навыков или корни репозитория для обхода. По умолчанию текущий каталог.",
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


def require_string(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label}: must be a non-empty string")


def require_string_list(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, list) or not value:
        errors.append(f"{label}: must be a non-empty array")
        return
    for index, item in enumerate(value):
        require_string(item, f"{label}[{index}]", errors)


def validate_input_files(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, list) or not value:
        errors.append(f"{label}: input_files must be a non-empty array")
        return
    for index, item in enumerate(value):
        item_label = f"{label}: input_files[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{item_label}: must be an object")
            continue
        require_string(item.get("path"), f"{item_label}.path", errors)
        require_string(item.get("purpose"), f"{item_label}.purpose", errors)


def validate_expected_output(
    value: Any,
    label: str,
    errors: list[str],
) -> None:
    if not isinstance(value, dict):
        errors.append(f"{label}: expected_output must be an object")
        return

    require_string_list(
        value.get("report_structure"),
        f"{label}: expected_output.report_structure",
        errors,
    )

    findings = value.get("findings")
    if not isinstance(findings, list) or not findings:
        errors.append(f"{label}: expected_output.findings must be a non-empty array")
        return

    for index, finding in enumerate(findings):
        finding_label = f"{label}: expected_output.findings[{index}]"
        if not isinstance(finding, dict):
            errors.append(f"{finding_label}: must be an object")
            continue
        require_string(finding.get("severity"), f"{finding_label}.severity", errors)
        require_string(
            finding.get("observed_problem"),
            f"{finding_label}.observed_problem",
            errors,
        )
        require_string(
            finding.get("expected_conclusion"),
            f"{finding_label}.expected_conclusion",
            errors,
        )
        require_string(
            finding.get("acceptable_fix_direction"),
            f"{finding_label}.acceptable_fix_direction",
            errors,
        )
def validate_oracle(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"{label}.oracle: must be an object")
        return
    require_string_list(
        value.get("success_criteria"),
        f"{label}.oracle.success_criteria",
        errors,
    )
    require_string_list(
        value.get("failure_indicators"),
        f"{label}.oracle.failure_indicators",
        errors,
    )


def validate_negative_control(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"{label}.negative_control: must be an object")
        return
    require_string(
        value.get("description"),
        f"{label}.negative_control.description",
        errors,
    )
    expected_failure = value.get("expected_failure")
    not_applicable_reason = value.get("not_applicable_reason")
    if isinstance(expected_failure, str) and expected_failure.strip():
        return
    if isinstance(not_applicable_reason, str) and not_applicable_reason.strip():
        return
    errors.append(
        f"{label}.negative_control: expected_failure or "
        "not_applicable_reason must be a non-empty string",
    )


def validate_application_contract(case: dict[str, Any], label: str, errors: list[str]) -> None:
    require_string(case.get("evaluation_surface"), f"{label}.evaluation_surface", errors)
    require_string_list(
        case.get("application_evidence"),
        f"{label}.application_evidence",
        errors,
    )
    validate_oracle(case.get("oracle"), label, errors)
    validate_negative_control(case.get("negative_control"), label, errors)


def validate_case(
    case: Any,
    index: int,
    skill_name: str,
    seen_ids: set[str],
    seen_prompts: set[str],
    errors: list[str],
) -> None:
    label = f"{skill_name}: cases[{index}]"
    if not isinstance(case, dict):
        errors.append(f"{label}: must be an object")
        return

    case_id = case.get("id")
    prompt = case.get("prompt")
    require_string(case_id, f"{label}.id", errors)
    require_string(prompt, f"{label}.prompt", errors)

    if isinstance(case_id, str):
        if case_id in seen_ids:
            errors.append(f"{label}: duplicate id {case_id!r}")
        seen_ids.add(case_id)
        if not case_id.startswith(f"{skill_name}-"):
            errors.append(f"{label}: id must start with {skill_name!r}")

    if isinstance(prompt, str):
        if prompt in seen_prompts:
            errors.append(f"{label}: duplicate prompt")
        seen_prompts.add(prompt)

    validate_input_files(case.get("input_files"), label, errors)

    validate_expected_output(case.get("expected_output"), label, errors)
    validate_application_contract(case, label, errors)
    require_string_list(case.get("assertions"), f"{label}.assertions", errors)
    require_string_list(case.get("must_not"), f"{label}.must_not", errors)


def validate_result_file(skill_dir: Path) -> list[str]:
    errors: list[str] = []
    result_path = skill_dir / "evals" / "result-scenarios.json"
    if not result_path.exists():
        return errors

    skill_name = read_skill_name(skill_dir / "SKILL.md")
    if not skill_name:
        return [f"{skill_dir / 'SKILL.md'}: missing frontmatter name"]

    data = load_json(result_path, errors)
    if data is None:
        return errors
    if not isinstance(data, dict):
        return [f"{result_path}: root must be an object"]
    if data.get("skill_name") != skill_name:
        errors.append(
            f"{result_path}: skill_name must be {skill_name!r}, "
            f"got {data.get('skill_name')!r}",
        )

    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append(f"{result_path}: cases must be a non-empty array")
        return errors

    seen_ids: set[str] = set()
    seen_prompts: set[str] = set()
    for index, case in enumerate(cases):
        validate_case(
            case,
            index,
            skill_name,
            seen_ids,
            seen_prompts,
            errors,
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
        if (skill_dir / "evals" / "result-scenarios.json").exists():
            checked += 1
        errors.extend(validate_result_file(skill_dir))

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print(f"Проверено сценариев результата: {checked}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
