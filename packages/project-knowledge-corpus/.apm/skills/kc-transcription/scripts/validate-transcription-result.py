#!/usr/bin/env python3
"""Verify a raw transcript and optionally remove verified temporary media."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path, PurePosixPath


class TranscriptionError(RuntimeError):
    """The transcription result is not safe to use or clean up after."""


def item_path(root: Path, raw_path: str) -> Path:
    path = PurePosixPath(raw_path)
    if path.is_absolute() or ".." in path.parts or path.name != raw_path:
        raise TranscriptionError(f"Путь медиа должен быть именем файла внутри единицы: {raw_path}")
    candidate = (root / path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise TranscriptionError(f"Путь медиа выходит за пределы единицы: {raw_path}") from exc
    return candidate


def temporary_media(path: Path) -> bool:
    return ".tmp." in path.name and path.is_file()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Проверить сырую расшифровку и очистить временное медиа.")
    parser.add_argument("item_dir", type=Path, help="Папка единицы с transcript.txt.")
    parser.add_argument("--media", action="append", default=[], help="Имя временного медиа внутри папки единицы.")
    parser.add_argument("--cleanup", action="store_true", help="Удалить указанные *.tmp.* после успешной проверки.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.item_dir.resolve()
    if not root.is_dir():
        raise TranscriptionError(f"Папка единицы не найдена: {args.item_dir}")
    transcript = root / "transcript.txt"
    if not transcript.is_file() or not transcript.read_text(encoding="utf-8").strip():
        raise TranscriptionError("Нужен непустой transcript.txt; очистка временного медиа запрещена.")
    media = [item_path(root, raw_path) for raw_path in args.media]
    invalid = [path.name for path in media if not temporary_media(path)]
    if invalid:
        raise TranscriptionError(f"Очистке подлежат только существующие *.tmp.*: {', '.join(invalid)}")
    removed: list[str] = []
    if args.cleanup:
        for path in media:
            path.unlink()
            removed.append(path.name)
    print(
        json.dumps(
            {
                "contract_version": 1,
                "status": "ready_for_normalization",
                "transcript": "transcript.txt",
                "cleanup_candidates": [path.name for path in media if path.name not in removed],
                "removed": removed,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TranscriptionError as exc:
        print(f"Ошибка расшифровки: {exc}", file=sys.stderr)
        raise SystemExit(2)
