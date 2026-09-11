#!/usr/bin/env python3
"""Отобрать событие для разбора опыта без записи и запуска модели."""

import argparse
import json
import sys
from pathlib import Path


SIGNALS = {"repeatable_workflow", "resolved_error", "user_correction", "skill_gap"}


def object_value(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label}: ожидается объект")
    return value


def text_value(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}: ожидается непустая строка")
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Повторяющееся поле: {key}")
        result[key] = value
    return result


def select(payload):
    payload = object_value(payload, "Вход")
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ValueError("Поддерживается только schema_version: 1")
    policy = object_value(payload.get("learning"), "learning")
    automatic = policy.get("automatic")
    manual = payload.get("manual", False)
    if type(automatic) is not bool or type(manual) is not bool:
        raise ValueError("automatic и manual должны быть логическими значениями")
    project = text_value(policy.get("project"), "learning.project")
    event = object_value(payload.get("event"), "event")
    for field in ("project", "source_id", "task_id", "revision", "source_ref"):
        text_value(event.get(field), f"event.{field}")
    if event.get("origin") not in ("task", "learning"):
        raise ValueError("event.origin: ожидается task или learning")
    if event.get("status") not in ("completed", "stopped", "running"):
        raise ValueError("event.status: неизвестное состояние задачи")
    signals = event.get("signals")
    if not isinstance(signals, list) or any(not isinstance(s, str) or s not in SIGNALS for s in signals):
        raise ValueError("event.signals: ожидается список известных сигналов")
    reviews = policy.get("reviews")
    if not isinstance(reviews, list):
        raise ValueError("learning.reviews: ожидается список")
    reviewed = set()
    for review in reviews:
        review = object_value(review, "review")
        key = tuple(text_value(review.get(f), f"review.{f}") for f in ("source_id", "task_id", "revision"))
        if review.get("result") not in ("reviewed", "rejected"):
            raise ValueError("review.result: ожидается reviewed или rejected")
        reviewed.add(key)
    key = tuple(event[f] for f in ("source_id", "task_id", "revision"))
    action, reason = "skip", "disabled"
    if event["project"] != project:
        reason = "other_project"
    elif manual:
        action, reason = "review", "manual_request"
    elif not automatic:
        reason = "disabled"
    elif event["origin"] == "learning":
        reason = "learning_event"
    elif event["status"] == "running":
        reason = "task_running"
    elif key in reviewed:
        reason = "already_reviewed"
    elif not signals:
        reason = "no_signal"
    else:
        action, reason = "review", "new_evidence"
    return {
        "action": action,
        "reason": reason,
        "key": list(key),
        "write_authorized": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Файл JSON с событием и настройкой")
    args = parser.parse_args()
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
        result = select(payload)
    except (OSError, ValueError, RecursionError) as error:
        print(f"Отбор события не выполнен: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
