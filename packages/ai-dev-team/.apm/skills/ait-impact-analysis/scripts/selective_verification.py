#!/usr/bin/env python3
"""Выбрать, выполнить и сохранить результаты детерминированных проверок."""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

sys.dont_write_bytecode = True

impact_graph = importlib.import_module("impact_graph")


SCHEMA_VERSION = 1
STATE_VERSION = 1
CHECK_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
SHELL_NAMES = {"bash", "cmd", "dash", "fish", "ksh", "powershell", "pwsh", "sh", "zsh"}


class ContractError(ValueError):
    """Объявление проверки не даёт безопасно построить план."""


def stable_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def file_digest(path: Path) -> str:
    try:
        stat = path.lstat()
        if path.is_symlink():
            payload = os.readlink(path).encode("utf-8", "surrogateescape")
        else:
            payload = path.read_bytes()
    except OSError as exc:
        raise ContractError(f"не удалось прочитать {path}: {exc}") from exc
    return stable_hash(
        {
            "mode": stat.st_mode & 0o7777,
            "payload": hashlib.sha256(payload).hexdigest(),
        },
    )


def require_mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{location}: ожидается объект")
    return value


def require_list(value: Any, location: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list):
        raise ContractError(f"{location}: ожидается массив")
    if nonempty and not value:
        raise ContractError(f"{location}: массив не должен быть пустым")
    return value


def require_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{location}: ожидается непустая строка")
    return value


def reject_unknown(value: dict[str, Any], allowed: set[str], location: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ContractError(f"{location}: неизвестные поля: {', '.join(unknown)}")


def normalize_relative_path(
    raw_path: Any,
    location: str,
    *,
    allow_dot: bool = False,
    pattern: bool = False,
) -> str:
    value = require_string(raw_path, location).replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ContractError(f"{location}: путь должен оставаться внутри проекта")
    normalized = str(path)
    if normalized == "." and allow_dot:
        return normalized
    if normalized in {"", "."}:
        raise ContractError(f"{location}: путь должен указывать на файл или каталог")
    if not pattern and any(symbol in normalized for symbol in "*?["):
        raise ContractError(f"{location}: путь не должен содержать шаблон")
    return normalized


def string_list(value: Any, location: str, *, nonempty: bool = False) -> list[str]:
    items = require_list(value, location, nonempty=nonempty)
    result = [require_string(item, f"{location}[{index}]") for index, item in enumerate(items)]
    if len(result) != len(set(result)):
        raise ContractError(f"{location}: повторяющиеся значения")
    return result


def validate_command(value: Any, location: str) -> list[str]:
    command = string_list(value, location, nonempty=True)
    executable = Path(command[0]).name.lower()
    if executable in SHELL_NAMES:
        raise ContractError(f"{location}: запуск через оболочку запрещён")
    return command


def validate_environment(value: Any, location: str) -> dict[str, Any]:
    environment = require_mapping(value, location)
    reject_unknown(environment, {"inherit", "set"}, location)
    inherited = string_list(environment.get("inherit", []), f"{location}.inherit")
    if any("=" in item or "\x00" in item for item in inherited):
        raise ContractError(f"{location}.inherit: указано недопустимое имя переменной")
    assigned = require_mapping(environment.get("set", {}), f"{location}.set")
    for name, raw_value in assigned.items():
        if not isinstance(name, str) or not name or "=" in name or "\x00" in name:
            raise ContractError(f"{location}.set: указано недопустимое имя переменной")
        if not isinstance(raw_value, str) or "\x00" in raw_value:
            raise ContractError(f"{location}.set.{name}: ожидается строка без NUL")
    if set(inherited) & set(assigned):
        raise ContractError(
            f"{location}: переменная не может одновременно наследоваться и задаваться",
        )
    return {"inherit": inherited, "set": dict(sorted(assigned.items()))}


def validate_config(data: Any, repo: Path, config_path: Path) -> dict[str, Any]:
    root = require_mapping(data, "root")
    reject_unknown(root, {"schema_version", "verification", "areas", "checks"}, "root")
    if root.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"root.schema_version: ожидается {SCHEMA_VERSION}")

    verification = require_mapping(root.get("verification"), "root.verification")
    reject_unknown(
        verification,
        {
            "name",
            "impact_graph",
            "full_checks",
            "full_coverage_paths",
            "global_inputs",
            "output_limit",
        },
        "root.verification",
    )
    output_limit = verification.get("output_limit", 2000)
    if not isinstance(output_limit, int) or not 80 <= output_limit <= 20000:
        raise ContractError("root.verification.output_limit: ожидается число от 80 до 20000")
    verified = {
        "name": require_string(verification.get("name"), "root.verification.name"),
        "impact_graph": normalize_relative_path(
            verification.get("impact_graph"),
            "root.verification.impact_graph",
        ),
        "full_checks": string_list(
            verification.get("full_checks"),
            "root.verification.full_checks",
            nonempty=True,
        ),
        "full_coverage_paths": [
            normalize_relative_path(
                value,
                f"root.verification.full_coverage_paths[{index}]",
                pattern=True,
            )
            for index, value in enumerate(
                require_list(
                    verification.get("full_coverage_paths"),
                    "root.verification.full_coverage_paths",
                    nonempty=True,
                ),
            )
        ],
        "global_inputs": [
            normalize_relative_path(
                value,
                f"root.verification.global_inputs[{index}]",
                pattern=True,
            )
            for index, value in enumerate(
                require_list(
                    verification.get("global_inputs"),
                    "root.verification.global_inputs",
                    nonempty=True,
                ),
            )
        ],
        "output_limit": output_limit,
    }

    checks = require_list(root.get("checks"), "root.checks", nonempty=True)
    check_by_id: dict[str, dict[str, Any]] = {}
    for index, raw_check in enumerate(checks):
        location = f"root.checks[{index}]"
        check = require_mapping(raw_check, location)
        reject_unknown(
            check,
            {
                "id",
                "title",
                "command",
                "cwd",
                "inputs",
                "environment",
                "tool_version_command",
            },
            location,
        )
        check_id = require_string(check.get("id"), f"{location}.id")
        if not CHECK_ID_RE.fullmatch(check_id):
            raise ContractError(f"{location}.id: недопустимый идентификатор")
        if check_id in check_by_id:
            raise ContractError(f"{location}.id: повторяется {check_id!r}")
        check_by_id[check_id] = {
            "id": check_id,
            "title": require_string(check.get("title"), f"{location}.title"),
            "command": validate_command(check.get("command"), f"{location}.command"),
            "cwd": normalize_relative_path(
                check.get("cwd", "."),
                f"{location}.cwd",
                allow_dot=True,
            ),
            "inputs": [
                normalize_relative_path(value, f"{location}.inputs[{item_index}]", pattern=True)
                for item_index, value in enumerate(
                    require_list(check.get("inputs"), f"{location}.inputs", nonempty=True),
                )
            ],
            "environment": validate_environment(
                check.get("environment", {}),
                f"{location}.environment",
            ),
            "tool_version_command": validate_command(
                check.get("tool_version_command"),
                f"{location}.tool_version_command",
            ),
        }

    full_checks = verified["full_checks"]
    if len(full_checks) != len(set(full_checks)):
        raise ContractError("root.verification.full_checks: повторяющиеся значения")
    unknown_full = sorted(set(full_checks) - set(check_by_id))
    missing_full = sorted(set(check_by_id) - set(full_checks))
    if unknown_full or missing_full:
        parts: list[str] = []
        if unknown_full:
            parts.append("неизвестные: " + ", ".join(unknown_full))
        if missing_full:
            parts.append("не включены в полный набор: " + ", ".join(missing_full))
        raise ContractError("root.verification.full_checks: " + ", ".join(parts))

    areas = require_list(root.get("areas"), "root.areas", nonempty=True)
    area_by_id: dict[str, dict[str, Any]] = {}
    for index, raw_area in enumerate(areas):
        location = f"root.areas[{index}]"
        area = require_mapping(raw_area, location)
        reject_unknown(area, {"id", "title", "impact_nodes", "checks"}, location)
        area_id = require_string(area.get("id"), f"{location}.id")
        if not CHECK_ID_RE.fullmatch(area_id):
            raise ContractError(f"{location}.id: недопустимый идентификатор")
        if area_id in area_by_id:
            raise ContractError(f"{location}.id: повторяется {area_id!r}")
        nodes = string_list(area.get("impact_nodes"), f"{location}.impact_nodes", nonempty=True)
        if any(not CHECK_ID_RE.fullmatch(node) for node in nodes):
            raise ContractError(f"{location}.impact_nodes: недопустимый идентификатор вершины")
        area_checks = string_list(area.get("checks"), f"{location}.checks", nonempty=True)
        unknown_checks = sorted(set(area_checks) - set(check_by_id))
        if unknown_checks:
            raise ContractError(f"{location}.checks: неизвестны {', '.join(unknown_checks)}")
        area_by_id[area_id] = {
            "id": area_id,
            "title": require_string(area.get("title"), f"{location}.title"),
            "impact_nodes": nodes,
            "checks": area_checks,
        }

    try:
        config_relative = config_path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError as exc:
        raise ContractError("файл объявления должен находиться внутри проекта") from exc

    return {
        "verification": verified,
        "checks": check_by_id,
        "areas": area_by_id,
        "config_relative": config_relative,
    }


def load_config(repo: Path, config_path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = config_path.read_bytes()
    except OSError as exc:
        raise ContractError(f"не удалось прочитать объявление проверок: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"объявление проверок содержит ошибку JSON: {exc}") from exc
    return validate_config(data, repo, config_path), hashlib.sha256(raw).hexdigest()


def run_command(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise ContractError(f"не удалось запустить команду: {exc}") from exc


def git_output(repo: Path, *args: str) -> bytes:
    completed = run_command(["git", "-C", str(repo), *args], cwd=repo)
    if completed.returncode:
        message = completed.stderr.decode("utf-8", "replace").strip()
        raise ContractError(f"git {' '.join(args)} завершился с ошибкой: {message}")
    return completed.stdout


def parse_name_status(output: bytes, origin: str) -> list[dict[str, Any]]:
    fields = [os.fsdecode(field) for field in output.split(b"\0") if field]
    entries: list[dict[str, Any]] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        path_count = 2 if status.startswith(("R", "C")) else 1
        if index + path_count > len(fields):
            raise ContractError("Git вернул неполную запись изменения")
        paths = fields[index : index + path_count]
        index += path_count
        entries.append({"origin": origin, "status": status, "paths": paths})
    return entries


def collect_changes(repo: Path, base: str) -> dict[str, Any]:
    resolved = os.fsdecode(
        git_output(repo, "rev-parse", "--verify", f"{base}^{{commit}}"),
    ).strip()
    conflict_paths = [
        os.fsdecode(path)
        for path in git_output(repo, "ls-files", "--unmerged", "-z").split(b"\0")
        if path
    ]
    if conflict_paths:
        raise ContractError("в индексе есть неразрешённые конфликты")
    staged = parse_name_status(
        git_output(
            repo,
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            "--cached",
            resolved,
            "--",
        ),
        "index",
    )
    worktree = parse_name_status(
        git_output(repo, "diff", "--name-status", "-z", "--find-renames", "--"),
        "worktree",
    )
    untracked_paths = [
        os.fsdecode(path)
        for path in git_output(
            repo,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ).split(b"\0")
        if path
    ]
    untracked = [
        {"origin": "untracked", "status": "??", "paths": [path]}
        for path in untracked_paths
    ]
    entries = [*staged, *worktree, *untracked]
    paths = sorted({path for entry in entries for path in entry["paths"]})
    return {
        "requested_base": base,
        "resolved_base": resolved,
        "entries": entries,
        "paths": paths,
    }


def pattern_matches(pattern: str, path: str) -> bool:
    return fnmatch.fnmatchcase(path, pattern)


def files_for_patterns(repo: Path, patterns: list[str]) -> list[dict[str, Any]]:
    snapshot: list[dict[str, Any]] = []
    for pattern in patterns:
        matches: list[dict[str, str]] = []
        file_pattern = f"{pattern}/*" if pattern.endswith("/**") else pattern
        for path in sorted(repo.glob(file_pattern)):
            try:
                relative = path.relative_to(repo).as_posix()
            except ValueError:
                continue
            if path.is_dir():
                continue
            kind = "symlink" if path.is_symlink() else "file"
            matches.append(
                {"path": relative, "kind": kind, "digest": file_digest(path)},
            )
        snapshot.append({"pattern": pattern, "files": matches})
    return snapshot


def execution_environment(
    specification: dict[str, Any],
) -> tuple[dict[str, str], list[dict[str, str]]]:
    values: dict[str, str] = {}
    fingerprints: list[dict[str, str]] = []
    for name in specification["inherit"]:
        value = os.environ.get(name)
        if value is not None:
            values[name] = value
            source = "inherited"
        else:
            source = "absent"
        fingerprints.append(
            {
                "name": name,
                "source": source,
                "value_digest": stable_hash(value),
            },
        )
    for name, value in specification["set"].items():
        values[name] = value
        fingerprints.append(
            {
                "name": name,
                "source": "configured",
                "value_digest": stable_hash(value),
            },
        )
    return values, sorted(fingerprints, key=lambda item: item["name"])


def tool_fingerprint(
    check: dict[str, Any],
    cwd: Path,
    environment: dict[str, str],
) -> dict[str, Any]:
    completed = run_command(
        check["tool_version_command"],
        cwd=cwd,
        environment=environment,
    )
    if completed.returncode:
        raise ContractError("команда определения версии средства завершилась с ошибкой")
    return {
        "command": check["tool_version_command"],
        "returncode": completed.returncode,
        "stdout_digest": hashlib.sha256(completed.stdout).hexdigest(),
        "stderr_digest": hashlib.sha256(completed.stderr).hexdigest(),
    }


def git_cache_dir(repo: Path) -> Path:
    raw = os.fsdecode(
        git_output(
            repo,
            "rev-parse",
            "--git-path",
            "ai-dev-team/selective-verification",
        ),
    ).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = repo / path
    return path.resolve()


def load_json_file(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def limit_text(raw: bytes, limit: int) -> dict[str, Any]:
    text = raw.decode("utf-8", "replace")
    if len(text) <= limit:
        return {"tail": text, "truncated": False}
    return {"tail": text[-limit:], "truncated": True}


def compact_list(values: list[Any], limit: int = 100) -> dict[str, Any]:
    return {"items": values[:limit], "omitted": max(0, len(values) - limit)}


def planner_fingerprint(config_digest: str, graph_path: Path) -> dict[str, str]:
    try:
        graph_digest = file_digest(graph_path)
    except ContractError as exc:
        graph_digest = "unavailable:" + stable_hash(str(exc))
    return {
        "runner": file_digest(Path(__file__)),
        "configuration": config_digest,
        "impact_graph": graph_digest,
    }


def impact_result(
    config: dict[str, Any],
    repo: Path,
    changed_paths: list[str],
) -> tuple[dict[str, Any] | None, str | None]:
    if not changed_paths:
        return {"changed": [], "affected": [], "unmapped_paths": []}, None
    graph_path = repo / config["verification"]["impact_graph"]
    try:
        graph = impact_graph.load_graph(graph_path)
        validate_area_nodes(config, graph)
        return impact_graph.trace_result(graph, [], changed_paths, None), None
    except (impact_graph.ContractError, ContractError) as exc:
        return None, str(exc)


def validate_area_nodes(config: dict[str, Any], graph: dict[str, Any]) -> None:
    known_nodes = {node["id"] for node in graph["nodes"]}
    unknown = sorted(
        {
            node
            for area in config["areas"].values()
            for node in area["impact_nodes"]
            if node not in known_nodes
        },
    )
    if unknown:
        raise ContractError("области ссылаются на отсутствующие вершины: " + ", ".join(unknown))


def select_checks(
    config: dict[str, Any],
    changes: dict[str, Any],
    impact: dict[str, Any] | None,
    impact_error: str | None,
    planner_changed: bool,
) -> tuple[list[str], list[str], list[str], bool]:
    reasons: list[str] = []
    paths = changes["paths"]
    verification = config["verification"]
    if any(path == config["config_relative"] for path in paths):
        reasons.append("изменено объявление проверок")
    if any(path == verification["impact_graph"] for path in paths):
        reasons.append("изменён граф влияния")
    global_paths = [
        path
        for path in paths
        if any(
            pattern_matches(pattern, path)
            for pattern in [
                *verification["full_coverage_paths"],
                *verification["global_inputs"],
            ]
        )
    ]
    if global_paths:
        reasons.append("изменены общие входы: " + ", ".join(global_paths))
    if planner_changed:
        reasons.append("изменилась версия средства или правил анализа")
    if impact_error is not None:
        reasons.append("анализ влияния недоступен: " + impact_error)
    area_ids: set[str] = set()
    uncovered_nodes: list[str] = []
    if impact is not None:
        node_to_areas: dict[str, set[str]] = {}
        for area in config["areas"].values():
            for node in area["impact_nodes"]:
                node_to_areas.setdefault(node, set()).add(area["id"])
        impacted_nodes = sorted(
            {
                item["id"]
                for item in [*impact["changed"], *impact["affected"]]
            },
        )
        for node in impacted_nodes:
            matches = node_to_areas.get(node, set())
            if not matches:
                uncovered_nodes.append(node)
            area_ids.update(matches)
        if uncovered_nodes:
            reasons.append("нет области для вершин: " + ", ".join(uncovered_nodes))
    if reasons:
        return verification["full_checks"], sorted(area_ids), reasons, True
    selected: list[str] = []
    for area_id in sorted(area_ids):
        for check_id in config["areas"][area_id]["checks"]:
            if check_id not in selected:
                selected.append(check_id)
    return selected, sorted(area_ids), [], False


def cache_key(
    check: dict[str, Any],
    verification: dict[str, Any],
    repo: Path,
    planner: dict[str, str],
) -> tuple[str, dict[str, str], list[dict[str, str]], dict[str, Any]]:
    cwd = (repo / check["cwd"]).resolve()
    try:
        cwd.relative_to(repo.resolve())
    except ValueError as exc:
        raise ContractError(f"рабочий каталог проверки {check['id']} вне проекта") from exc
    if not cwd.is_dir():
        raise ContractError(f"рабочий каталог проверки {check['id']} не существует")
    environment, environment_fingerprint = execution_environment(check["environment"])
    inputs = files_for_patterns(repo, [*verification["global_inputs"], *check["inputs"]])
    tool = tool_fingerprint(check, cwd, environment)
    key_material = {
        "schema_version": SCHEMA_VERSION,
        "check": {
            "id": check["id"],
            "command": check["command"],
            "cwd": check["cwd"],
            "environment": check["environment"],
            "tool_version_command": check["tool_version_command"],
        },
        "planner": planner,
        "inputs": inputs,
        "environment": environment_fingerprint,
        "tool_digest": stable_hash(tool),
    }
    return stable_hash(key_material), environment, environment_fingerprint, tool


def run_check(
    check: dict[str, Any],
    verification: dict[str, Any],
    repo: Path,
    cache_root: Path,
    planner: dict[str, str],
    reason: str,
) -> tuple[dict[str, Any], bool]:
    key, environment, environment_fingerprint, tool = cache_key(
        check,
        verification,
        repo,
        planner,
    )
    entry_path = cache_root / "entries" / f"{key}.json"
    cached = load_json_file(entry_path)
    cached_result = cached.get("result") if cached else None
    if (
        cached
        and cached.get("key") == key
        and cached.get("status") == "success"
        and isinstance(cached_result, dict)
        and cached_result.get("exit_code") == 0
        and isinstance(cached_result.get("duration_seconds"), (int, float))
        and isinstance(cached_result.get("output"), dict)
    ):
        return (
            {
                "id": check["id"],
                "title": check["title"],
                "status": "cached",
                "reason": reason,
                "cache_key": key[:16],
                "exit_code": cached_result["exit_code"],
                "duration_seconds": cached_result["duration_seconds"],
                "output": cached_result["output"],
            },
            True,
        )
    started = time.monotonic()
    completed = run_command(
        check["command"],
        cwd=(repo / check["cwd"]).resolve(),
        environment=environment,
    )
    duration = round(time.monotonic() - started, 3)
    result = {
        "id": check["id"],
        "title": check["title"],
        "status": "executed",
        "reason": reason,
        "cache_key": key[:16],
        "exit_code": completed.returncode,
        "duration_seconds": duration,
        "output": {
            "stdout": limit_text(completed.stdout, verification["output_limit"]),
            "stderr": limit_text(completed.stderr, verification["output_limit"]),
        },
        "environment": environment_fingerprint,
        "tool": tool,
    }
    if completed.returncode == 0:
        write_json(
            entry_path,
            {
                "schema_version": SCHEMA_VERSION,
                "key": key,
                "status": "success",
                "result": {
                    "exit_code": 0,
                    "duration_seconds": duration,
                    "output": result["output"],
                },
            },
        )
    return result, completed.returncode == 0


def run_selective_verification(
    repo: Path,
    config_path: Path,
    base: str,
) -> tuple[dict[str, Any], int]:
    config, config_digest = load_config(repo, config_path)
    changes = collect_changes(repo, base)
    graph_path = repo / config["verification"]["impact_graph"]
    planner = planner_fingerprint(config_digest, graph_path)
    cache_root = git_cache_dir(repo)
    prior_state = load_json_file(cache_root / "state.json")
    planner_changed = prior_state is not None and prior_state.get("planner") != planner
    impact, impact_error = impact_result(config, repo, changes["paths"])
    selected, area_ids, reasons, full_coverage = select_checks(
        config,
        changes,
        impact,
        impact_error,
        planner_changed,
    )
    selected_set = set(selected)
    if full_coverage:
        execution_reason = "полный охват: " + "; ".join(reasons)
    elif area_ids:
        execution_reason = "затронутые области: " + ", ".join(area_ids)
    else:
        execution_reason = "нет затронутых областей"

    results: list[dict[str, Any]] = []
    all_succeeded = True
    for check_id, check in config["checks"].items():
        if check_id not in selected_set:
            results.append(
                {
                    "id": check_id,
                    "title": check["title"],
                    "status": "not_required",
                    "reason": "не связан с затронутыми областями",
                },
            )
            continue
        result, succeeded = run_check(
            check,
            config["verification"],
            repo,
            cache_root,
            planner,
            execution_reason,
        )
        results.append(result)
        all_succeeded = all_succeeded and succeeded

    if all_succeeded:
        write_json(
            cache_root / "state.json",
            {"schema_version": STATE_VERSION, "planner": planner},
        )

    compact_impact: dict[str, Any]
    if impact is None:
        compact_impact = {
            "status": "unavailable",
            "reason": impact_error,
            "changed_nodes": [],
            "affected_nodes": [],
        }
    else:
        compact_impact = {
            "status": "available",
            "changed_nodes": [item["id"] for item in impact["changed"]],
            "affected_nodes": [item["id"] for item in impact["affected"]],
            "unmapped_paths": compact_list(impact["unmapped_paths"]),
        }
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "passed" if all_succeeded else "failed",
        "base": {
            "requested": changes["requested_base"],
            "resolved": changes["resolved_base"],
        },
        "changes": {
            "entries": compact_list(changes["entries"]),
            "paths": compact_list(changes["paths"]),
        },
        "impact": compact_impact,
        "selection": {
            "mode": "full" if full_coverage else "selective",
            "areas": area_ids,
            "reasons": reasons,
        },
        "checks": results,
    }
    return report, 0 if all_succeeded else 1


def command_validate(repo: Path, config_path: Path) -> dict[str, Any]:
    config, _ = load_config(repo, config_path)
    graph_path = repo / config["verification"]["impact_graph"]
    graph = impact_graph.load_graph(graph_path)
    validate_area_nodes(config, graph)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "passed",
        "checks": list(config["checks"]),
        "areas": list(config["areas"]),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Выбрать и выполнить детерминированные проверки по влиянию изменений.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "run"):
        child = subparsers.add_parser(name)
        child.add_argument("--repo", required=True, type=Path)
        child.add_argument("--config", required=True, type=Path)
        if name == "run":
            child.add_argument("--base", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    repo = args.repo.resolve()
    config_path = args.config.resolve()
    try:
        if not repo.is_dir():
            raise ContractError("каталог проекта не найден")
        if args.command == "validate":
            report = command_validate(repo, config_path)
            exit_code = 0
        else:
            report, exit_code = run_selective_verification(repo, config_path, args.base)
    except ContractError as exc:
        report = {"schema_version": SCHEMA_VERSION, "status": "error", "error": str(exc)}
        exit_code = 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
