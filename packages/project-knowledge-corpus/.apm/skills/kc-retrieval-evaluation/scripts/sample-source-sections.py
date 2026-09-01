#!/usr/bin/env python3
"""Создать воспроизводимую выборку разделов текстовых источников."""

from __future__ import annotations

import argparse
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from evaluation_suite import dump_yaml, source_text_sha256


HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
SAFE_ID_PATTERN = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Section:
    artifact: str
    line_start: int
    line_end: int
    title: str
    text: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Выбрать разделы источников до обращения к statements.yml.",
    )
    parser.add_argument("corpus_root", type=Path, help="Корень корпуса.")
    parser.add_argument(
        "--source",
        action="append",
        required=True,
        help="Путь к файлу Markdown или TXT относительно корня корпуса.",
    )
    parser.add_argument("--count", type=int, required=True, help="Число разделов в выборке.")
    parser.add_argument("--seed", type=int, required=True, help="Зерно случайной выборки.")
    parser.add_argument("--suite-id", required=True, help="Идентификатор создаваемого набора.")
    parser.add_argument("--output", required=True, type=Path, help="Целевой файл YAML.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Разрешить замену существующего целевого файла.",
    )
    return parser.parse_args()


def safe_case_id(suite_id: str, index: int) -> str:
    normalized = SAFE_ID_PATTERN.sub("-", suite_id.casefold()).strip("-")
    if not normalized:
        normalized = "sample"
    return f"{normalized}-{index:03d}"


def resolve_source(corpus_root: Path, value: str) -> tuple[Path | None, str | None]:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        return None, f"Недопустимый путь источника: {value}"
    root = corpus_root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None, f"Путь выходит за пределы корпуса: {value}"
    if not path.is_file():
        return None, f"Файл источника не найден: {value}"
    if path.suffix.casefold() not in {".md", ".txt"}:
        return None, f"Поддерживаются только Markdown и TXT: {value}"
    return path, None


def markdown_sections(artifact: str, text: str) -> list[Section]:
    lines = text.splitlines()
    headings: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = HEADING_PATTERN.match(line)
        if match:
            headings.append((index, match.group(2).strip()))
    if not headings:
        return text_windows(artifact, text)

    result: list[Section] = []
    for position, (start, title) in enumerate(headings):
        end = headings[position + 1][0] - 1 if position + 1 < len(headings) else len(lines) - 1
        selected = lines[start : end + 1]
        if sum(1 for line in selected if line.strip()) < 2:
            continue
        result.append(
            Section(
                artifact=artifact,
                line_start=start + 1,
                line_end=end + 1,
                title=title,
                text="\n".join(selected),
            )
        )
    return result


def text_windows(artifact: str, text: str, window_size: int = 40) -> list[Section]:
    lines = text.splitlines()
    result: list[Section] = []
    for start in range(0, len(lines), window_size):
        end = min(start + window_size, len(lines))
        selected = lines[start:end]
        if sum(1 for line in selected if line.strip()) < 2:
            continue
        result.append(
            Section(
                artifact=artifact,
                line_start=start + 1,
                line_end=end,
                title=f"Строки {start + 1}–{end}",
                text="\n".join(selected),
            )
        )
    return result


def collect_sections(corpus_root: Path, sources: list[str]) -> tuple[list[Section], list[str]]:
    sections: list[Section] = []
    errors: list[str] = []
    for source in sources:
        path, error = resolve_source(corpus_root, source)
        if error:
            errors.append(error)
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.append(f"Файл не является текстом UTF-8: {source}")
            continue
        relative = path.resolve().relative_to(corpus_root.resolve()).as_posix()
        sections.extend(markdown_sections(relative, text))
    return sections, errors


def main() -> int:
    args = parse_args()
    if args.count < 1:
        print("--count должен быть положительным.", file=sys.stderr)
        return 2
    if not args.corpus_root.is_dir():
        print(f"Корень корпуса не найден: {args.corpus_root}", file=sys.stderr)
        return 2
    if args.output.exists() and not args.force:
        print(
            f"Целевой файл уже существует: {args.output}. "
            "Для замены укажите --force.",
            file=sys.stderr,
        )
        return 2

    sections, errors = collect_sections(args.corpus_root, args.source)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 2
    if not sections:
        print("В указанных источниках нет пригодных разделов.", file=sys.stderr)
        return 1

    rng = random.Random(args.seed)
    selected = rng.sample(sections, min(args.count, len(sections)))
    selected.sort(key=lambda section: (section.artifact, section.line_start))

    data = {
        "suite_version": 1,
        "id": args.suite_id,
        "description": (
            f"Воспроизводимая выборка разделов первоисточников, seed={args.seed}."
        ),
        "sampling": {
            "method": "source_sections_without_statements",
            "seed": args.seed,
            "requested_count": args.count,
            "available_sections": len(sections),
        },
        "cases": [
            {
                "id": safe_case_id(args.suite_id, index),
                "status": "candidate",
                "source": {
                    "artifact": section.artifact,
                    "line_start": section.line_start,
                    "line_end": section.line_end,
                    "sha256": source_text_sha256(section.text),
                },
                "sampling_title": section.title,
            }
            for index, section in enumerate(selected, start=1)
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(dump_yaml(data), encoding="utf-8")
    print(
        f"Создано кандидатов: {len(selected)} из {len(sections)} доступных разделов. "
        f"Файл: {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
