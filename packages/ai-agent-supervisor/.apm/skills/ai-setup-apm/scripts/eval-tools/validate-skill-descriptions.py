#!/usr/bin/env python3
"""Validate skill description budgets for the collection."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


SPEC_MAX_DESCRIPTION_CHARS = 1024
DEFAULT_MAX_DESCRIPTION_CHARS = 180
DEFAULT_MAX_TOTAL_CHARS = 2200


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проверить бюджет description в SKILL.md у навыков.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Каталоги навыков или корни репозитория для обхода. По умолчанию текущий каталог.",
    )
    parser.add_argument(
        "--max-description-chars",
        type=int,
        default=int(
            os.environ.get(
                "APM_SKILL_DESCRIPTION_MAX_CHARS",
                DEFAULT_MAX_DESCRIPTION_CHARS,
            )
        ),
        help=(
            "Максимальная длина одного description. По умолчанию берётся из "
            "APM_SKILL_DESCRIPTION_MAX_CHARS или равна 180."
        ),
    )
    parser.add_argument(
        "--max-total-chars",
        type=int,
        default=int(
            os.environ.get(
                "APM_SKILL_DESCRIPTION_TOTAL_MAX_CHARS",
                DEFAULT_MAX_TOTAL_CHARS,
            )
        ),
        help=(
            "Максимальная суммарная длина description в выбранном наборе. По "
            "умолчанию берётся из APM_SKILL_DESCRIPTION_TOTAL_MAX_CHARS или "
            "равна 2200."
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


def parse_frontmatter(skill_path: Path) -> dict[str, str]:
    lines = skill_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    result: dict[str, str] = {}
    index = 1
    while index < len(lines):
        line = lines[index]
        if line.strip() == "---":
            break
        if ":" not in line:
            index += 1
            continue

        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()

        if value == ">":
            index += 1
            block: list[str] = []
            while index < len(lines):
                block_line = lines[index]
                if block_line.strip() == "---":
                    index -= 1
                    break
                if not block_line.startswith((" ", "\t")):
                    index -= 1
                    break
                block.append(block_line.strip())
                index += 1
            result[key] = " ".join(part for part in block if part).strip()
        else:
            result[key] = value.strip("\"'")
        index += 1
    return result


def main() -> int:
    args = parse_args()
    if args.max_description_chars <= 0:
        print("--max-description-chars must be positive", file=sys.stderr)
        return 1
    if args.max_total_chars <= 0:
        print("--max-total-chars must be positive", file=sys.stderr)
        return 1

    roots = args.paths or [Path.cwd()]
    skill_dirs = find_skill_dirs(roots)
    if not skill_dirs:
        print("Каталоги навыков не найдены.", file=sys.stderr)
        return 1

    errors: list[str] = []
    total_chars = 0
    checked = 0

    for skill_dir in skill_dirs:
        skill_path = skill_dir / "SKILL.md"
        frontmatter = parse_frontmatter(skill_path)
        description = frontmatter.get("description", "").strip()

        if not description:
            errors.append(f"{skill_path}: missing non-empty description")
            continue

        checked += 1
        description_len = len(description)
        total_chars += description_len

        if description_len > SPEC_MAX_DESCRIPTION_CHARS:
            errors.append(
                f"{skill_path}: description length {description_len} exceeds "
                f"spec limit {SPEC_MAX_DESCRIPTION_CHARS}",
            )
        if description_len > args.max_description_chars:
            errors.append(
                f"{skill_path}: description length {description_len} exceeds "
                f"project budget {args.max_description_chars}",
            )
        if description.startswith("Навык "):
            errors.append(
                f"{skill_path}: description should route by user intent, not "
                "start with internal narration",
            )

    if total_chars > args.max_total_chars:
        errors.append(
            "total description length "
            f"{total_chars} exceeds project budget {args.max_total_chars}",
        )

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print(
        "Проверено описаний навыков: "
        f"{checked}. Суммарная длина: {total_chars} символов.",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
