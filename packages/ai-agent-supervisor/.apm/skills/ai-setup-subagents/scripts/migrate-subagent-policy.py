#!/usr/bin/env python3
"""Мигрирует локальную политику подагентов и её входной фрагмент."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path


for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8", errors="replace")


START = "<!-- ai-setup-subagents:runtime-policy:start -->"
END = "<!-- ai-setup-subagents:runtime-policy:end -->"
ASSET = Path(__file__).resolve().parents[1] / "assets" / "worker-policy-entrypoint.md"
SEMANTIC_DEFAULTS = (
    ('direct_execution_scope = "whole_parent_task"', "direct_execution_scope"),
    ('subtask_routing = "match_each_bounded_subtask"', "subtask_routing"),
    ('parent_comparison = "actual_current_session"', "parent_comparison"),
    ('unknown_parent_parameters = "comparison_unresolved"', "unknown_parent_parameters"),
    ('parent_change = "invalidate_comparison"', "parent_change"),
    ('assignment_basis = "historical_only"', "assignment_basis"),
)
PARENT_FIELDS = {
    "parent_model": "parent_basis_model",
    "parent_effort": "parent_basis_effort",
    "parent_evidence": "parent_basis_evidence",
}


def parse_toml(text: str, path: Path) -> dict:
    try:
        value = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"{path}: TOML не прочитан: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{path}: политика должна быть TOML-таблицей")
    return value


def insert_after_schema(text: str) -> str:
    match = re.search(
        r"(?m)^(\s*schema_version\s*=\s*)1(\s*(?:#.*)?)(\r?\n|$)",
        text,
    )
    if match is None:
        raise ValueError("в policy_runtime не найден schema_version = 1")
    newline = match.group(3) or "\n"
    replacement = match.group(1) + "2" + match.group(2) + newline
    additions = newline.join(item[0] for item in SEMANTIC_DEFAULTS) + newline
    return text[: match.start()] + replacement + additions + text[match.end() :]


def migrate_task_block(match: re.Match[str]) -> str:
    block = match.group(0)
    moved = False
    for old, new in PARENT_FIELDS.items():
        old_match = re.search(rf"(?m)^(\s*){re.escape(old)}(\s*=)", block)
        new_match = re.search(rf"(?m)^\s*{re.escape(new)}\s*=", block)
        if old_match and new_match:
            raise ValueError(f"задача уже содержит оба поля {old} и {new}")
        if old_match:
            block = re.sub(
                rf"(?m)^(\s*){re.escape(old)}(\s*=)",
                rf"\1{new}\2",
                block,
                count=1,
            )
            moved = True
    if not moved:
        return block
    newline = "\r\n" if "\r\n" in block else "\n"
    header_end = block.find(newline)
    if header_end < 0:
        return block
    header_end += len(newline)
    additions: list[str] = []
    if not re.search(r"(?m)^\s*parent_basis\s*=", block):
        additions.append('parent_basis = "historical_setup_observation"')
    if not re.search(r"(?m)^\s*parent_basis_observed_at\s*=", block):
        additions.append('parent_basis_observed_at = "unknown"')
    if not additions:
        return block
    return block[:header_end] + newline.join(additions) + newline + block[header_end:]


def migrate_config(text: str, path: Path) -> str:
    data = parse_toml(text, path)
    runtime = data.get("policy_runtime")
    if not isinstance(runtime, dict):
        raise ValueError(f"{path}: не найдена таблица policy_runtime")
    version = runtime.get("schema_version")
    if version == 2:
        return text
    if version != 1:
        raise ValueError(f"{path}: ожидается policy_runtime.schema_version = 1 или 2")
    migrated = insert_after_schema(text)
    task_pattern = re.compile(
        r"(?ms)^\[\[policy_design\.tasks\]\].*?(?=^\[|\Z)",
    )
    return task_pattern.sub(migrate_task_block, migrated)


def replace_entrypoint(text: str, canonical: str) -> str:
    starts = [match.start() for match in re.finditer(re.escape(START), text)]
    ends = [match.start() for match in re.finditer(re.escape(END), text)]
    if len(starts) > 1 or len(ends) > 1:
        raise ValueError("точка входа содержит несколько маркированных фрагментов")
    if not starts and not ends:
        if not text:
            return canonical
        separator = "" if text.endswith(("\n", "\r")) else "\n"
        return text + separator + "\n" + canonical
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise ValueError("точка входа содержит неполный маркированный фрагмент")
    start = text.rfind("\n", 0, starts[0]) + 1
    end_marker = ends[0] + len(END)
    end = text.find("\n", end_marker)
    if end >= 0:
        end += 1
    else:
        end = len(text)
    return text[:start] + canonical + text[end:]


def entrypoint_path(value: str) -> Path:
    name, separator, raw_path = value.partition("=")
    if not separator or not name or not raw_path:
        raise ValueError("точка входа задаётся как оснастка=путь")
    return Path(raw_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("subagents.local.toml"))
    parser.add_argument("--entrypoint", action="append", default=[])
    parser.add_argument("--check", action="store_true", help="только проверить, нужна ли миграция")
    args = parser.parse_args()

    config = args.config
    config_text = config.read_text(encoding="utf-8")
    migrated_config = migrate_config(config_text, config)
    canonical = ASSET.read_text(encoding="utf-8")
    entrypoints: list[tuple[Path, str, str]] = []
    for raw in args.entrypoint:
        path = entrypoint_path(raw)
        text = path.read_text(encoding="utf-8")
        entrypoints.append((path, text, replace_entrypoint(text, canonical)))

    changed = migrated_config != config_text or any(old != new for _, old, new in entrypoints)
    if args.check:
        if changed:
            print("Требуется миграция политики подагентов.")
            return 1
        print("Миграция политики подагентов не требуется.")
        return 0

    if migrated_config != config_text:
        config.write_text(migrated_config, encoding="utf-8")
    for path, old, new in entrypoints:
        if old != new:
            path.write_text(new, encoding="utf-8")
    print("Политика подагентов мигрирована." if changed else "Политика подагентов уже актуальна.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
