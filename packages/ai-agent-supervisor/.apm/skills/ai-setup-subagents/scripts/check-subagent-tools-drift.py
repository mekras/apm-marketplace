#!/usr/bin/env python3
"""Проверяет совпадение средств подагентов в tools/ с источником навыка."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8", errors="replace")


SCRIPT_DIR = Path(__file__).resolve().parent
MANIFEST = SCRIPT_DIR / "subagent-tools.manifest"
PROFILES = {"portable", "codex", "claude"}


def manifest_entries(profile: str) -> list[tuple[str, str]]:
    required = {"portable", profile}
    entries: list[tuple[str, str]] = []
    for number, raw_line in enumerate(MANIFEST.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 4:
            raise ValueError(f"некорректная строка манифеста {number}: {raw_line!r}")
        source, destination, _mode, profiles = parts
        if required.intersection(profiles.split(",")):
            entries.append((source, destination))
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=sorted(PROFILES), required=True)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args()

    problems: list[str] = []
    try:
        entries = manifest_entries(args.profile)
    except (OSError, ValueError) as error:
        print(f"Манифест средств подагентов не прочитан: {error}", file=sys.stderr)
        return 1
    for source_rel, destination_rel in entries:
        source = SCRIPT_DIR / source_rel
        destination = args.project_root.resolve() / destination_rel
        if not source.is_file():
            problems.append(f"нет источника: {source}")
        elif not destination.is_file():
            problems.append(f"нет рабочей копии: {destination_rel}")
        elif source.read_bytes() != destination.read_bytes():
            problems.append(f"расхождение: {destination_rel} отличается от {source_rel}")

    if problems:
        print("Средства подагентов разошлись с источником:", file=sys.stderr)
        print("Переустановите их соответствующим install-*-tools.", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
