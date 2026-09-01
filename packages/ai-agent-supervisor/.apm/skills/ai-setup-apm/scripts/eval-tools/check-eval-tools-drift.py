#!/usr/bin/env python3
"""Проверка расхождения оснастки модельных прогонов с её каноническим источником.

Канонический источник оснастки — каталог eval-tools этого навыка. В проекте
оснастка лежит в рабочих местах (`tools/` и образец в корне), куда её кладёт
install-eval-tools. Эта проверка читает eval-tools/manifest.txt и сравнивает
содержимое источника с установленной копией, чтобы рабочие копии не разошлись с
источником без повторной установки.

Запуск из корня проекта:
    python3 .apm/skills/ai-setup-apm/scripts/eval-tools/check-eval-tools-drift.py

Код выхода 0 — копии совпадают; 1 — есть расхождение или пропажа.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent
MANIFEST = SRC_DIR / "manifest.txt"


def parse_manifest() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 2:
            print(f"Строка манифеста не распознана: {line!r}", file=sys.stderr)
            sys.exit(1)
        pairs.append((parts[0].strip(), parts[1].strip()))
    return pairs


def main() -> int:
    project_root = Path.cwd().resolve()
    problems: list[str] = []

    for src_rel, dest_rel in parse_manifest():
        src_path = SRC_DIR / src_rel
        dest_path = project_root / dest_rel
        if not src_path.is_file():
            problems.append(f"нет источника: {src_path}")
            continue
        if not dest_path.is_file():
            problems.append(
                f"нет рабочей копии: {dest_rel} "
                f"(запустите install-eval-tools)"
            )
            continue
        if src_path.read_bytes() != dest_path.read_bytes():
            problems.append(
                f"расхождение: {dest_rel} отличается от источника "
                f"{src_rel}; правьте источник в eval-tools и переустановите"
            )

    if problems:
        print("Оснастка модельных прогонов разошлась с источником:", file=sys.stderr)
        for item in problems:
            print(f"  - {item}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
