#!/usr/bin/env python3
"""Detect hidden Unicode characters in source files."""

from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    column: int
    codepoint: str
    severity: str
    description: str


SUSPICIOUS_RANGES: tuple[tuple[int, int, str, str], ...] = (
    (0xE0001, 0xE007F, "critical", "Unicode tag character"),
    (0x202A, 0x202A, "critical", "Left-to-right embedding (LRE)"),
    (0x202B, 0x202B, "critical", "Right-to-left embedding (RLE)"),
    (0x202C, 0x202C, "critical", "Pop directional formatting (PDF)"),
    (0x202D, 0x202D, "critical", "Left-to-right override (LRO)"),
    (0x202E, 0x202E, "critical", "Right-to-left override (RLO)"),
    (0x2066, 0x2066, "critical", "Left-to-right isolate (LRI)"),
    (0x2067, 0x2067, "critical", "Right-to-left isolate (RLI)"),
    (0x2068, 0x2068, "critical", "First strong isolate (FSI)"),
    (0x2069, 0x2069, "critical", "Pop directional isolate (PDI)"),
    (0xE0100, 0xE01EF, "critical", "Variation selector (SMP)"),
    (0x200B, 0x200B, "warning", "Zero-width space"),
    (0x200C, 0x200C, "warning", "Zero-width non-joiner (ZWNJ)"),
    (0x200D, 0x200D, "warning", "Zero-width joiner (ZWJ)"),
    (0x2060, 0x2060, "warning", "Word joiner"),
    (0xFE00, 0xFE0D, "warning", "Variation selector"),
    (0xFE0E, 0xFE0E, "warning", "Text presentation selector"),
    (0x00AD, 0x00AD, "warning", "Soft hyphen"),
    (0x200E, 0x200E, "warning", "Left-to-right mark (LRM)"),
    (0x200F, 0x200F, "warning", "Right-to-left mark (RLM)"),
    (0x061C, 0x061C, "warning", "Arabic letter mark (ALM)"),
    (0x2061, 0x2061, "warning", "Function application"),
    (0x2062, 0x2062, "warning", "Invisible times"),
    (0x2063, 0x2063, "warning", "Invisible separator"),
    (0x2064, 0x2064, "warning", "Invisible plus"),
    (0xFFF9, 0xFFF9, "warning", "Interlinear annotation anchor"),
    (0xFFFA, 0xFFFA, "warning", "Interlinear annotation separator"),
    (0xFFFB, 0xFFFB, "warning", "Interlinear annotation terminator"),
    (0x206A, 0x206F, "warning", "Deprecated formatting character"),
)

CHAR_LOOKUP: dict[int, tuple[str, str]] = {}
for start, end, severity, description in SUSPICIOUS_RANGES:
    for codepoint in range(start, end + 1):
        CHAR_LOOKUP[codepoint] = (severity, description)

DEFAULT_EXCLUDES = (
    ".git",
    ".git/**",
    ".codex",
    ".codex/**",
    ".agents",
    ".agents/**",
    ".claude",
    ".claude/**",
    "apm_modules",
    "apm_modules/**",
    "build",
    "build/**",
    "dist",
    "dist/**",
    "**/__pycache__",
    "**/__pycache__/**",
    "**/*.pyc",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проверить исходные файлы на скрытые Unicode-символы.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Файлы или каталоги для обхода. По умолчанию текущий каталог.",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Glob-исключение от корня проверки. Можно указать несколько раз.",
    )
    parser.add_argument(
        "--include-info",
        action="store_true",
        help="Показывать информационные находки вроде начального BOM.",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Проверять только индексированные версии файлов из Git.",
    )
    return parser.parse_args()


def is_emoji_char(char: str) -> bool:
    return unicodedata.category(char) == "So"


def zwj_in_emoji_context(text: str, index: int) -> bool:
    prev = index - 1
    while prev >= 0:
        codepoint = ord(text[prev])
        if codepoint == 0xFE0F or 0x1F3FB <= codepoint <= 0x1F3FF:
            prev -= 1
            continue
        break

    next_index = index + 1
    return (
        prev >= 0
        and next_index < len(text)
        and is_emoji_char(text[prev])
        and is_emoji_char(text[next_index])
    )


def matches_any(path: Path, patterns: tuple[str, ...]) -> bool:
    text = path.as_posix()
    return any(fnmatch.fnmatch(text, pattern) for pattern in patterns)


def iter_files(paths: list[Path], excludes: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for raw_path in paths or [Path(".")]:
        path = raw_path.resolve()
        if path.is_file():
            try:
                rel = path.relative_to(Path.cwd().resolve())
            except ValueError:
                rel = path
            if not matches_any(rel, excludes):
                files.append(path)
            continue

        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            current = Path(dirpath)
            try:
                rel_dir = current.relative_to(Path.cwd().resolve())
            except ValueError:
                rel_dir = current
            dirnames[:] = [
                name
                for name in dirnames
                if not matches_any(rel_dir / name, excludes)
            ]
            for filename in filenames:
                file_path = current / filename
                if file_path.is_symlink():
                    continue
                try:
                    rel_file = file_path.relative_to(Path.cwd().resolve())
                except ValueError:
                    rel_file = file_path
                if matches_any(rel_file, excludes):
                    continue
                files.append(file_path)
    return sorted(set(files))


def scan_text(content: str, path: Path, include_info: bool) -> list[Finding]:
    if not content or content.isascii():
        return []

    findings: list[Finding] = []
    for line_index, line in enumerate(content.split("\n"), start=1):
        for column_index, char in enumerate(line, start=1):
            codepoint = ord(char)
            if codepoint == 0xFEFF:
                severity = "info" if line_index == 1 and column_index == 1 else "warning"
                if include_info or severity != "info":
                    findings.append(
                        Finding(
                            path,
                            line_index,
                            column_index,
                            "U+FEFF",
                            severity,
                            "Byte order mark",
                        )
                    )
                continue

            entry = CHAR_LOOKUP.get(codepoint)
            if entry is None:
                continue

            severity, description = entry
            if codepoint == 0x200D and zwj_in_emoji_context(line, column_index - 1):
                severity = "info"
                description = "Zero-width joiner (emoji sequence)"
            if severity == "info" and not include_info:
                continue
            findings.append(
                Finding(
                    path,
                    line_index,
                    column_index,
                    f"U+{codepoint:04X}",
                    severity,
                    description,
                )
            )
    return findings


def scan_file(path: Path, include_info: bool) -> list[Finding]:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return scan_text(content, path, include_info)


def staged_files() -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=ACMR",
            "-z",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    entries = [item for item in result.stdout.decode("utf-8").split("\0") if item]
    return [Path(item) for item in entries]


def scan_staged_file(path: Path, include_info: bool) -> list[Finding]:
    try:
        result = subprocess.run(
            ["git", "show", f":{path.as_posix()}"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError:
        return []

    try:
        content = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return []
    return scan_text(content, path, include_info)


def main() -> int:
    args = parse_args()
    excludes = (*DEFAULT_EXCLUDES, *tuple(args.exclude))
    findings: list[Finding] = []
    files_scanned = 0

    if args.staged:
        for path in staged_files():
            if matches_any(path, excludes):
                continue
            files_scanned += 1
            findings.extend(scan_staged_file(path, args.include_info))
    else:
        for path in iter_files(args.paths, excludes):
            files_scanned += 1
            findings.extend(scan_file(path, args.include_info))

    actionable = [item for item in findings if item.severity in {"critical", "warning"}]
    if not actionable:
        print(f"Скрытые Unicode-символы не найдены: проверено файлов {files_scanned}")
        return 0

    print("Найдены скрытые Unicode-символы:", file=sys.stderr)
    for item in actionable:
        try:
            rel_path = item.path.relative_to(Path.cwd().resolve())
        except ValueError:
            rel_path = item.path
        print(
            f"  {rel_path}:{item.line}:{item.column} "
            f"{item.codepoint} {item.description} [{item.severity}]",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
