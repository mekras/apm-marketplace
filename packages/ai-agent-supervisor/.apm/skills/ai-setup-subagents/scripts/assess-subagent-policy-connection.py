#!/usr/bin/env python3
"""Проверяет подключение локальной политики к рабочим инструкциям и след запуска."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any


for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8", errors="replace")


START = "<!-- ai-setup-subagents:runtime-policy:start -->"
END = "<!-- ai-setup-subagents:runtime-policy:end -->"
REQUIRED_RUNTIME_FIELDS = {
    "launcher",
    "target",
    "direct_execution",
    "direct_execution_scope",
    "subtask_routing",
    "parent_comparison",
    "unknown_parent_parameters",
    "parent_change",
    "assignment_basis",
    "unavailable_route",
    "execution_failure",
    "unconfirmed_model",
    "result_acceptance",
}

RUNTIME_SCHEMA_VERSION = 2
RUNTIME_ENUMS = {
    "direct_execution_scope": {"whole_parent_task"},
    "subtask_routing": {"match_each_bounded_subtask"},
    "parent_comparison": {"actual_current_session"},
    "unknown_parent_parameters": {"comparison_unresolved"},
    "parent_change": {"invalidate_comparison"},
    "assignment_basis": {"historical_only"},
}


def read_toml(path: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"политика не прочитана: {error}") from error
    if not isinstance(data, dict):
        raise ValueError("политика должна быть TOML-таблицей")
    return data


def runtime_policy(data: dict[str, Any]) -> dict[str, Any]:
    runtime = data.get("policy_runtime")
    if not isinstance(runtime, dict):
        raise ValueError("не найдена таблица policy_runtime")
    if runtime.get("schema_version") != RUNTIME_SCHEMA_VERSION:
        raise ValueError(f"policy_runtime.schema_version должен быть равен {RUNTIME_SCHEMA_VERSION}")
    missing = sorted(field for field in REQUIRED_RUNTIME_FIELDS if not runtime.get(field))
    if missing:
        raise ValueError("в policy_runtime не заданы: " + ", ".join(missing))
    if not isinstance(runtime["launcher"], str) or not isinstance(runtime["target"], str):
        raise ValueError("policy_runtime.launcher и policy_runtime.target должны быть непустыми строками")
    if not isinstance(runtime["direct_execution"], list) or not all(
        isinstance(item, str) and item.strip() for item in runtime["direct_execution"]
    ):
        raise ValueError("policy_runtime.direct_execution должен быть непустым списком строк")
    for field, allowed in RUNTIME_ENUMS.items():
        if runtime[field] not in allowed:
            values = ", ".join(sorted(allowed))
            raise ValueError(f"policy_runtime.{field} имеет недопустимое значение; ожидается: {values}")
    routes = runtime.get("routes")
    if not isinstance(routes, list) or not routes:
        raise ValueError("в policy_runtime не заданы маршруты routes")
    for route in routes:
        if not isinstance(route, dict) or not route.get("when") or not route.get("execution_class"):
            raise ValueError("каждый маршрут policy_runtime.routes требует when и execution_class")
    return runtime


def parse_entrypoint(value: str) -> tuple[str, Path]:
    name, separator, raw_path = value.partition("=")
    if not separator or not name or not raw_path:
        raise ValueError("точка входа задаётся как оснастка=путь")
    return name, Path(raw_path)


def entrypoint_status(path: Path, policy_name: str) -> tuple[bool, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        return False, f"не прочитана: {error}"
    if START not in text or END not in text:
        return False, "нет обязательного фрагмента рабочей политики"
    fragment = text.split(START, 1)[1].split(END, 1)[0]
    if policy_name not in fragment:
        return False, "фрагмент не ссылается на локальную политику"
    return True, "подключена"


def run_status(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "process": "not_checked",
            "result": "not_checked",
            "acceptance_record": "not_checked",
            "quality": "not_evidenced",
            "model": "not_checked",
            "economy": "not_checked",
        }
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {"error": f"запись запуска не прочитана: {error}"}
    if not isinstance(record, dict):
        return {"error": "запись запуска должна быть объектом JSON"}
    process = "completed" if record.get("returncode") == 0 else "failed"
    result_path = record.get("result_path")
    result = "available" if isinstance(result_path, str) and Path(result_path).is_file() else "missing"
    acceptance = "not_checked"
    accepted = record.get("acceptance")
    if isinstance(accepted, dict):
        acceptance = "accepted_recorded" if accepted.get("status") == "accepted" else "not_accepted"
    model = "confirmed" if record.get("model_status") == "confirmed" and record.get("model_matches") is True else "unconfirmed"
    quality = "not_evidenced"
    quality_record = record.get("quality_assessment")
    if isinstance(quality_record, dict):
        quality = "assessment_recorded" if quality_record.get("status") == "passed" else "assessment_not_passed"
    economy = "not_measured"
    measurement = record.get("economy")
    if isinstance(measurement, dict):
        economy = "measurement_recorded" if measurement.get("status") == "measured" else "measurement_not_confirmed"
    return {"process": process, "result": result, "acceptance_record": acceptance, "quality": quality, "model": model, "economy": economy}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("subagents.local.toml"))
    parser.add_argument("--entrypoint", action="append", default=[])
    parser.add_argument("--run-record", type=Path)
    parser.add_argument("--output", type=Path, help="сохранить отчёт JSON по явному пути")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    errors: list[str] = []
    entrypoints: dict[str, str] = {}
    try:
        policy = runtime_policy(read_toml(args.config))
    except ValueError as error:
        errors.append(str(error))
        policy = {}
    for raw in args.entrypoint:
        try:
            harness, path = parse_entrypoint(raw)
            connected, status = entrypoint_status(path, args.config.name)
            entrypoints[harness] = status
            if not connected:
                errors.append(f"{harness}: {status}")
        except ValueError as error:
            errors.append(str(error))

    connected = bool(entrypoints) and not errors
    report = {
        "configuration": "saved" if not any(error.startswith("политика") or error.startswith("не найдена") or error.startswith("policy_runtime") or error.startswith("в policy_runtime") for error in errors) else "incomplete",
        "instruction_entrypoints": "referenced" if connected else "not_referenced",
        "working_sessions": "not_observed",
        "entrypoints": entrypoints,
        "runtime_policy": {
            **{key: policy[key] for key in REQUIRED_RUNTIME_FIELDS if key in policy},
            "routes": policy.get("routes", []),
        },
        "execution": run_status(args.run_record),
        "errors": errors,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
