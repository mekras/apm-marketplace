#!/usr/bin/env python3
"""Подготовить и завершить приватную запись применения обучения."""

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path


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
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


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


def initiate(root, request):
    record = validate_record(request.get("record"))
    state = root / relative_path(request.get("state_path"), "state_path")
    backup = root / relative_path(request.get("backup_dir"), "backup_dir")
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


def complete(root, request):
    state = root / relative_path(request.get("state_path"), "state_path")
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
        result = initiate(root, request) if action == "initiate" else complete(root, request) if action == "complete" else None
        if result is None:
            raise ValueError("action: ожидается initiate или complete")
    except (OSError, ValueError, RecursionError, json.JSONDecodeError) as error:
        print(f"Запись обучения не подготовлена: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
