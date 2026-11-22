#!/usr/bin/env python3
"""Подготовить и завершить приватную запись применения обучения."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Русские сообщения не должны падать на консоли с однобайтовой кодировкой.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8", errors="replace")


REQUIRED = ("source_id", "revision", "group_before", "group_after", "transformation", "expected_result", "rule_map")


def object_value(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label}: ожидается объект")
    return value


def text_value(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}: ожидается непустая строка")
    return value


def relative_path(value, label):
    path = Path(text_value(value, label))
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        raise ValueError(f"{label}: ожидается относительный путь внутри рабочей области")
    return path


def git_metadata_dirs(root):
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-dir", "--git-common-dir"],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return ()
    if result.returncode:
        return ()
    return tuple((root / os.fsdecode(path)).resolve() for path in result.stdout.splitlines())


def storage_path(root, value, label, git_dirs):
    relative = relative_path(value, label)
    destination = root / relative
    resolved = destination.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError(f"{label}: фактический путь должен быть внутри рабочей области")
    if (
        any(part.lower() == ".git" for part in (*relative.parts, *resolved.relative_to(root).parts))
        or any(resolved.is_relative_to(directory) for directory in git_dirs)
    ):
        raise ValueError(f"{label}: каталог метаданных Git нельзя использовать для состояния обучения")
    if destination.is_symlink():
        raise ValueError(f"{label}: используйте путь к файлу или каталогу без конечной ссылки")
    return destination


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Повторяющееся поле: {key}")
        result[key] = value
    return result


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)


def write_json(path, value):
    # Для атомарной замены временный файл должен быть на той же файловой системе.
    stream = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix=f".{path.name}.", dir=path.parent, delete=False
    )
    temporary = Path(stream.name)
    try:
        with stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_record(record):
    record = object_value(record, "record")
    for field in REQUIRED:
        if field not in record:
            raise ValueError(f"record.{field}: обязательное поле отсутствует")
    text_value(record["source_id"], "record.source_id")
    text_value(record["revision"], "record.revision")
    if not isinstance(record["group_before"], list) or not record["group_before"]:
        raise ValueError("record.group_before: ожидается непустой список")
    if not isinstance(record["group_after"], list) or not record["group_after"]:
        raise ValueError("record.group_after: ожидается непустой список")
    if not isinstance(record["transformation"], list) or not record["transformation"]:
        raise ValueError("record.transformation: ожидается непустой список")
    text_value(record["expected_result"], "record.expected_result")
    rules = record["rule_map"]
    if not isinstance(rules, list) or not rules:
        raise ValueError("record.rule_map: ожидается непустой список")
    for item in rules:
        item = object_value(item, "record.rule_map[]")
        text_value(item.get("rule"), "record.rule_map[].rule")
        text_value(item.get("from"), "record.rule_map[].from")
        text_value(item.get("to"), "record.rule_map[].to")
    return record


def source_paths(root, record):
    paths = []
    for item in record["group_before"]:
        path = relative_path(item.get("path") if isinstance(item, dict) else item, "record.group_before[].path")
        source = root / path
        if not source.is_file():
            raise ValueError(f"Исходный файл не найден: {path}")
        paths.append((path, source))
    return paths


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def initiate(root, request, git_dirs):
    record = validate_record(request.get("record"))
    state = storage_path(root, request.get("state_path"), "state_path", git_dirs)
    backup = storage_path(root, request.get("backup_dir"), "backup_dir", git_dirs)
    if state.exists() or backup.exists():
        raise ValueError("state_path и backup_dir должны отсутствовать до подготовки")
    sources = source_paths(root, record)
    backup.mkdir(parents=True)
    for relative, source in sources:
        target = backup / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    record = {**record, "status": "applying", "checks": [], "backup_dir": backup.relative_to(root).as_posix(),
              "before": [{"path": path.as_posix(), "sha256": fingerprint(source)} for path, source in sources]}
    state.parent.mkdir(parents=True, exist_ok=True)
    write_json(state, record)
    return {"action": "initiated", "state_path": state.relative_to(root).as_posix(), "backup_dir": backup.relative_to(root).as_posix(), "write_authorized": False}


def complete(root, request, git_dirs):
    state = storage_path(root, request.get("state_path"), "state_path", git_dirs)
    record = validate_record(load_json(state))
    if record.get("status") != "applying":
        raise ValueError("Запись должна иметь status: applying")
    checks = request.get("checks")
    if not isinstance(checks, list) or not checks or any(not isinstance(item, str) or not item.strip() for item in checks):
        raise ValueError("checks: ожидается непустой список результатов проверок")
    record["status"] = "applied"
    record["checks"] = checks
    write_json(state, record)
    return {"action": "completed", "state_path": state.relative_to(root).as_posix(), "write_authorized": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Файл JSON с запросом")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Корень рабочей области")
    args = parser.parse_args()
    try:
        root = args.workspace.resolve()
        request = object_value(load_json(args.input), "Вход")
        action = request.get("action")
        if action not in ("initiate", "complete"):
            raise ValueError("action: ожидается initiate или complete")
        operation = initiate if action == "initiate" else complete
        result = operation(root, request, git_metadata_dirs(root))
    except (OSError, ValueError, RecursionError, json.JSONDecodeError) as error:
        print(f"Запись обучения не подготовлена: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
