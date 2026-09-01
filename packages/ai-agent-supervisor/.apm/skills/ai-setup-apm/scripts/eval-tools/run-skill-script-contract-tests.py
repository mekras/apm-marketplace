#!/usr/bin/env python3
"""Запустить контрактные проверки публичных Python-скриптов навыков.

Каждая объявленная операция публичного Python-скрипта первого уровня в
``scripts/`` должна иметь успешный рабочий сценарий в
``evals/script-contract-tests.json``. Сценарий запускает поставляемую команду
в копии фикстуры и проверяет наблюдаемый результат. Ожидаемые отказы могут
дополнять, но не заменять успешный сценарий. Необязательные входные файлы,
которые меняют поведение операции, объявляются в ``operations[].inputs``:
каждый объявленный вход обязан присутствовать хотя бы в одной фикстуре
успешного сценария этой операции, иначе зависящая от него ветвь остаётся
непроверенной. Во время успешного сценария запускатель наблюдает действительно
отсутствующие пути внутри фикстуры. Отрицательный результат проверки типа
существующего пути не считается отсутствием. Запускатель исключает пути,
созданные или заменённые самой операцией, и отклоняет оставшиеся пути, не
объявленные в ``inputs`` покрываемой операции.
Если в контракте есть независимая ошибка полноты, запускатель всё равно
выполняет корректно описанные сценарии и сообщает все найденные ошибки за один
запуск.

Объявленный вход в формате JSON обязан быть содержательным хотя бы в одной
фикстуре успешного сценария операции: если во всех таких фикстурах все его
коллекции пусты, вход считается вырожденным и не доказывает обработку
элементов. Дополнительно запускатель измеряет, какие исполняемые строки
каждого публичного скрипта выполнили сценарии. Функция скрипта, ни одна строка
которой не выполнена ни одним сценарием, считается непроверенной: это ошибка,
если функция не перечислена в ``unexercised_functions`` контракта с указанием
скрипта, полного имени и причины. Запись о фактически выполненной функции
устаревает и тоже считается ошибкой. Отчёт о прочих невыполненных строках не
меняет код возврата, но является обязательной очередью проверки для аудита
поставляемой автоматизации.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from typing import Any


FIXTURE_PREFIX = Path("evals/script-fixtures")

TRACE_SITECUSTOMIZE = r'''"""Наблюдение за необъявленными условными входами контракта."""

import builtins
import json
import os
from pathlib import Path


_root = os.environ.get("APM_CONTRACT_FIXTURE_ROOT")
_log = os.environ.get("APM_CONTRACT_INPUT_LOG")
_marker = os.environ.get("APM_CONTRACT_TRACE_MARKER")
_original_exists = Path.exists
_original_is_file = Path.is_file
_original_is_dir = Path.is_dir
_original_path_open = Path.open
_original_open = builtins.open
_original_os_open = os.open
_original_os_exists = os.path.exists
_original_os_isfile = os.path.isfile
_original_os_isdir = os.path.isdir
_original_os_replace = os.replace
_original_os_rename = os.rename
_original_os_mkdir = os.mkdir


def _relative(value):
    if not _root or not _log:
        return None
    try:
        root = os.path.abspath(_root)
        candidate = os.path.abspath(os.fspath(value))
        if os.path.commonpath([root, candidate]) != root:
            return None
        relative = os.path.relpath(candidate, root)
    except (OSError, TypeError, ValueError):
        return None
    if relative == "." or relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return None
    return relative.replace(os.sep, "/")


def _record(value, event):
    relative = _relative(value)
    if relative is None:
        return
    payload = (json.dumps({"path": relative, "event": event}, ensure_ascii=False) + "\n").encode("utf-8")
    descriptor = _original_os_open(_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)


def _exists_probe(original):
    def wrapped(self):
        result = original(self)
        if not result:
            _record(self, "missing")
        return result
    return wrapped


def _typed_path_probe(original):
    def wrapped(self):
        result = original(self)
        if not result and not _original_exists(self):
            _record(self, "missing")
        return result
    return wrapped


def _os_exists_probe(original):
    def wrapped(value):
        result = original(value)
        if not result:
            _record(value, "missing")
        return result
    return wrapped


def _typed_os_probe(original):
    def wrapped(value):
        result = original(value)
        if not result and not _original_os_exists(value):
            _record(value, "missing")
        return result
    return wrapped


def _path_open(self, *args, **kwargs):
    mode = kwargs.get("mode", args[0] if args else "r")
    try:
        result = _original_path_open(self, *args, **kwargs)
    except FileNotFoundError:
        _record(self, "missing")
        raise
    if any(marker in mode for marker in "wax+"):
        _record(self, "write")
    return result


def _open(file, *args, **kwargs):
    mode = kwargs.get("mode", args[0] if args else "r")
    try:
        result = _original_open(file, *args, **kwargs)
    except FileNotFoundError:
        _record(file, "missing")
        raise
    if isinstance(mode, str) and any(marker in mode for marker in "wax+"):
        _record(file, "write")
    return result


def _os_open(file, flags, *args, **kwargs):
    result = _original_os_open(file, flags, *args, **kwargs)
    if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
        _record(file, "write")
    return result


def _replace(source, destination, *args, **kwargs):
    result = _original_os_replace(source, destination, *args, **kwargs)
    _record(destination, "write")
    return result


def _rename(source, destination, *args, **kwargs):
    result = _original_os_rename(source, destination, *args, **kwargs)
    _record(destination, "write")
    return result


def _mkdir(path, *args, **kwargs):
    result = _original_os_mkdir(path, *args, **kwargs)
    _record(path, "write")
    return result


Path.exists = _exists_probe(_original_exists)
Path.is_file = _typed_path_probe(_original_is_file)
Path.is_dir = _typed_path_probe(_original_is_dir)
Path.open = _path_open
builtins.open = _open
os.open = _os_open
os.path.exists = _os_exists_probe(_original_os_exists)
os.path.isfile = _typed_os_probe(_original_os_isfile)
os.path.isdir = _typed_os_probe(_original_os_isdir)
os.replace = _replace
os.rename = _rename
os.mkdir = _mkdir

if _marker:
    descriptor = _original_os_open(_marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(descriptor)

_coverage_script = os.environ.get("APM_CONTRACT_COVERAGE_SCRIPT")
_coverage_log = os.environ.get("APM_CONTRACT_COVERAGE_LOG")

if _coverage_script and _coverage_log:
    import atexit
    import sys
    import threading

    _executed_lines = set()

    def _line_tracer(frame, event, arg):
        if event == "line":
            _executed_lines.add(frame.f_lineno)
        return _line_tracer

    def _call_tracer(frame, event, arg):
        if frame.f_code.co_filename == _coverage_script:
            _executed_lines.add(frame.f_lineno)
            return _line_tracer
        return None

    def _dump_coverage():
        sys.settrace(None)
        payload = (json.dumps(sorted(_executed_lines)) + "\n").encode("utf-8")
        descriptor = _original_os_open(
            _coverage_log,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)

    atexit.register(_dump_coverage)
    threading.settrace(_call_tracer)
    sys.settrace(_call_tracer)
'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Запустить контрактные проверки Python-скриптов навыков.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[Path(".apm/skills")],
        help="Каталоги навыков или отдельные навыки.",
    )
    return parser.parse_args()


def skill_directories(paths: list[Path]) -> list[Path]:
    result: set[Path] = set()
    for path in paths:
        if (path / "SKILL.md").is_file():
            result.add(path)
        elif path.is_dir():
            result.update(file.parent for file in path.rglob("SKILL.md"))
    return sorted(result)


def public_python_scripts(skill: Path) -> list[Path]:
    scripts = skill / "scripts"
    if not scripts.is_dir():
        return []
    return sorted(path for path in scripts.glob("*.py") if path.is_file())


def safe_relative(value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        return None
    return path


def string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        return None
    return value


def command_list(value: Any) -> list[list[str]] | None:
    if not isinstance(value, list):
        return None
    result: list[list[str]] = []
    for item in value:
        command = string_list(item)
        if command is None or not command:
            return None
        result.append(command)
    return result


def command_matches_operation(command: list[str], prefix: list[str]) -> bool:
    """Проверить, что команда запускает объявленную операцию скрипта."""
    try:
        script_index = command.index("{script}")
    except ValueError:
        return False
    return command[script_index + 1 : script_index + 1 + len(prefix)] == prefix


def is_runnable_case(skill: Path, case: dict[str, Any]) -> bool:
    """Проверить, достаточно ли данных случая для безопасного запуска."""
    script = safe_relative(case.get("script"))
    fixture = safe_relative(case.get("fixture"))
    command = string_list(case.get("command"))
    return (
        script is not None
        and script in {path.relative_to(skill) for path in public_python_scripts(skill)}
        and fixture is not None
        and fixture.is_relative_to(FIXTURE_PREFIX)
        and (skill / fixture).is_dir()
        and command is not None
        and command
        and command[0] == "{python}"
        and "{script}" in command
        and not any(item in {"--help", "-h"} for item in command)
        and command_list(case.get("prepare", [])) is not None
        and isinstance(case.get("expect"), dict)
    )


def load_unexercised_allowances(
    skill: Path,
    data: dict[str, Any],
    expected_scripts: set[Path],
    errors: list[str],
) -> dict[Path, dict[str, str]]:
    contract = skill / "evals" / "script-contract-tests.json"
    entries = data.get("unexercised_functions", [])
    allowances: dict[Path, dict[str, str]] = {}
    if not isinstance(entries, list):
        errors.append(f"{contract}: unexercised_functions должен быть массивом")
        return allowances
    for index, entry in enumerate(entries):
        label = f"{contract}: unexercised_functions[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{label}: должна быть запись со script, function и reason")
            continue
        script = safe_relative(entry.get("script"))
        if script is None or script not in expected_scripts:
            errors.append(f"{label}.script: нужен Python-скрипт первого уровня scripts/")
            continue
        function = entry.get("function")
        if not isinstance(function, str) or not function:
            errors.append(f"{label}.function: нужно полное имя функции")
            continue
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{label}.reason: нужна непустая причина")
            continue
        if function in allowances.setdefault(script, {}):
            errors.append(f"{label}.function: повтор {function!r}")
            continue
        allowances[script][function] = reason.strip()
    return allowances


def load_cases(
    skill: Path,
    errors: list[str],
) -> tuple[list[dict[str, Any]], dict[Path, dict[str, str]]]:
    contract = skill / "evals" / "script-contract-tests.json"
    if not contract.is_file():
        if public_python_scripts(skill):
            errors.append(f"{skill}: нет {contract.relative_to(skill)}")
        return [], {}
    try:
        data = json.loads(contract.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"{contract}: JSON не разобран: {exc}")
        return [], {}
    if not isinstance(data, dict) or data.get("version") != 2:
        errors.append(f"{contract}: нужен объект с version: 2")
        return [], {}
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append(f"{contract}: нужен непустой массив cases")
        return [], {}
    expected_scripts = {path.relative_to(skill) for path in public_python_scripts(skill)}
    allowances = load_unexercised_allowances(skill, data, expected_scripts, errors)
    operations = data.get("operations")
    if not isinstance(operations, list) or not operations:
        errors.append(f"{contract}: нужен непустой массив operations")
        operations = []
    operation_prefixes: dict[str, tuple[Path, list[str]]] = {}
    operation_inputs: dict[str, list[Path]] = {}
    for index, operation in enumerate(operations):
        label = f"{contract}: operations[{index}]"
        if not isinstance(operation, dict):
            errors.append(f"{label}: должен быть объект")
            continue
        identifier = operation.get("id")
        if not isinstance(identifier, str) or not identifier:
            errors.append(f"{label}.id: нужна непустая строка")
            continue
        if identifier in operation_prefixes:
            errors.append(f"{label}.id: повтор {identifier!r}")
            continue
        script = safe_relative(operation.get("script"))
        if script is None or script not in expected_scripts:
            errors.append(f"{label}.script: нужен Python-скрипт первого уровня scripts/")
            continue
        prefix = string_list(operation.get("command_prefix"))
        if prefix is None:
            errors.append(f"{label}.command_prefix: нужен массив строк")
            continue
        if "inputs" not in operation:
            errors.append(
                f"{label}.inputs: нужен явный массив. Пустой массив означает, "
                "что условные входы операции проверены и не найдены",
            )
            inputs_value = []
        else:
            inputs_value = operation["inputs"]
        inputs = string_list(inputs_value) if isinstance(inputs_value, list) else None
        declared_inputs: list[Path] = []
        if inputs is None:
            errors.append(f"{label}.inputs: нужен массив непустых строк")
        else:
            for input_value in inputs:
                input_path = safe_relative(input_value)
                if input_path is None:
                    errors.append(
                        f"{label}.inputs: нужен безопасный относительный путь, "
                        f"получено {input_value!r}",
                    )
                else:
                    declared_inputs.append(input_path)
        operation_prefixes[identifier] = (script, prefix)
        operation_inputs[identifier] = declared_inputs
    missing_operations = sorted(
        expected_scripts - {script for script, _ in operation_prefixes.values()},
    )
    if missing_operations:
        errors.append(
            f"{contract}: не объявлена операция для: "
            + ", ".join(path.as_posix() for path in missing_operations),
        )
    valid_cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    successfully_covered_operations: set[str] = set()
    success_fixtures: dict[str, set[Path]] = {}
    for index, case in enumerate(cases):
        label = f"{contract}: cases[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{label}: должен быть объект")
            continue
        identifier = case.get("id")
        if not isinstance(identifier, str) or not identifier:
            errors.append(f"{label}.id: нужна непустая строка")
        elif identifier in seen_ids:
            errors.append(f"{label}.id: повтор {identifier!r}")
        else:
            seen_ids.add(identifier)
        script = safe_relative(case.get("script"))
        if script is None or script not in expected_scripts:
            errors.append(f"{label}.script: нужен Python-скрипт первого уровня scripts/")
        fixture = safe_relative(case.get("fixture"))
        if fixture is None or not fixture.is_relative_to(FIXTURE_PREFIX):
            errors.append(
                f"{label}.fixture: нужен каталог внутри {FIXTURE_PREFIX.as_posix()}/",
            )
        elif not (skill / fixture).is_dir():
            errors.append(f"{label}.fixture: каталог не найден")
        command = string_list(case.get("command"))
        if command is None or not command:
            errors.append(f"{label}.command: нужен непустой массив строк")
        elif command[0] != "{python}" or "{script}" not in command:
            errors.append(
                f"{label}.command: команда должна запускать {{python}} и {{script}}",
            )
        elif any(item in {"--help", "-h"} for item in command):
            errors.append(f"{label}.command: --help не является контрактным сценарием")
        covers = case.get("covers", [])
        if not isinstance(covers, list) or not all(
            isinstance(item, str) and item for item in covers
        ):
            errors.append(f"{label}.covers: нужен массив непустых идентификаторов")
            covers = []
        elif len(covers) != len(set(covers)):
            errors.append(f"{label}.covers: идентификаторы не должны повторяться")
        for operation_id in covers:
            operation = operation_prefixes.get(operation_id)
            if operation is None:
                errors.append(f"{label}.covers: не объявлена операция {operation_id!r}")
                continue
            operation_script, prefix = operation
            if script != operation_script:
                errors.append(
                    f"{label}.covers: операция {operation_id!r} относится к другому скрипту",
                )
                continue
            if command is None or not command_matches_operation(command, prefix):
                errors.append(
                    f"{label}.covers: команда не начинается с command_prefix операции {operation_id!r}",
                )
        prepare = case.get("prepare", [])
        if command_list(prepare) is None:
            errors.append(
                f"{label}.prepare: нужен массив непустых массивов команд",
            )
        expect = case.get("expect")
        if not isinstance(expect, dict):
            errors.append(f"{label}.expect: нужен объект с наблюдаемым результатом")
        else:
            checks = 0
            exit_code = expect.get("exit_code", 0)
            if not isinstance(exit_code, int) or exit_code < 0:
                errors.append(f"{label}.expect.exit_code: нужен неотрицательный код")
            elif exit_code == 0 and script is not None:
                for operation_id in covers:
                    operation = operation_prefixes.get(operation_id)
                    if operation is not None and operation[0] == script and command is not None and command_matches_operation(command, operation[1]):
                        successfully_covered_operations.add(operation_id)
                        if fixture is not None and (skill / fixture).is_dir():
                            success_fixtures.setdefault(operation_id, set()).add(fixture)
            for key in ("stdout_contains", "stderr_contains"):
                value = expect.get(key)
                if value is not None:
                    values = string_list(value)
                    if values is None or not values:
                        errors.append(f"{label}.expect.{key}: нужен непустой массив строк")
                    else:
                        checks += len(values)
            files = expect.get("files")
            if files is not None:
                if not isinstance(files, list) or not files:
                    errors.append(f"{label}.expect.files: нужен непустой массив")
                else:
                    for file_index, item in enumerate(files):
                        file_label = f"{label}.expect.files[{file_index}]"
                        if not isinstance(item, dict) or safe_relative(item.get("path")) is None:
                            errors.append(f"{file_label}.path: нужен безопасный относительный путь")
                            continue
                        if item.get("json") is not None and not isinstance(item["json"], bool):
                            errors.append(f"{file_label}.json: допускается только true или false")
                        contains = item.get("contains")
                        if contains is not None and (not isinstance(contains, str) or not contains):
                            errors.append(f"{file_label}.contains: нужна непустая строка")
                        checks += 1
            if not checks:
                errors.append(f"{label}.expect: нужен хотя бы один проверяемый результат")
        if is_runnable_case(skill, case):
            runnable_case = dict(case)
            runnable_case["_operation_inputs"] = {
                operation_id: [path.as_posix() for path in operation_inputs[operation_id]]
                for operation_id in covers
                if operation_id in operation_inputs
            }
            valid_cases.append(runnable_case)
    missing_operation_success = sorted(set(operation_prefixes) - successfully_covered_operations)
    if missing_operation_success:
        errors.append(
            f"{contract}: нет успешного рабочего сценария для операций: "
            + ", ".join(missing_operation_success),
        )
    for operation_id, declared_inputs in sorted(operation_inputs.items()):
        fixtures = success_fixtures.get(operation_id, set())
        for input_path in declared_inputs:
            present = [
                skill / fixture / input_path
                for fixture in fixtures
                if (skill / fixture / input_path).is_file()
            ]
            if not present:
                errors.append(
                    f"{contract}: объявленный вход {input_path.as_posix()} "
                    f"отсутствует во всех фикстурах успешных сценариев "
                    f"операции {operation_id!r}",
                )
            elif input_path.suffix == ".json" and all(
                degenerate_json_input(path) for path in present
            ):
                errors.append(
                    f"{contract}: объявленный вход {input_path.as_posix()} "
                    f"операции {operation_id!r} во всех фикстурах успешных "
                    "сценариев содержит только пустые коллекции; вырожденный "
                    "вход не доказывает обработку элементов — добавь успешный "
                    "сценарий с фикстурой, где этот вход содержит данные",
                )
    return valid_cases, allowances


def degenerate_json_input(path: Path) -> bool:
    """Определить, что JSON-вход содержит только пустые коллекции."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if value == {} or value == []:
        return True
    arrays: list[list[Any]] = []
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, list):
            arrays.append(current)
            stack.extend(current)
        elif isinstance(current, dict):
            stack.extend(current.values())
    return bool(arrays) and all(not array for array in arrays)


def executable_lines(script: Path) -> set[int]:
    """Собрать номера исполняемых строк скрипта по его байт-коду."""
    source = script.read_text(encoding="utf-8")
    code = compile(source, str(script), "exec")
    lines: set[int] = set()
    stack = [code]
    while stack:
        current = stack.pop()
        lines.update(
            line for _, _, line in current.co_lines() if line is not None and line > 0
        )
        stack.extend(
            constant
            for constant in current.co_consts
            if isinstance(constant, types.CodeType)
        )
    return lines


def script_functions(script: Path) -> dict[str, set[int]]:
    """Собрать функции скрипта и номера строк, исполняемых только их телом.

    Строка ``def`` исполняется объемлющим кодом при определении функции,
    поэтому строки объемлющего кода исключаются: остаются строки, которые
    выполняются только при фактическом вызове функции.
    """
    source = script.read_text(encoding="utf-8")
    code = compile(source, str(script), "exec")
    functions: dict[str, set[int]] = {}
    stack: list[tuple[types.CodeType, set[int]]] = [(code, set())]
    while stack:
        current, enclosing = stack.pop()
        own = {
            line for _, _, line in current.co_lines() if line is not None and line > 0
        }
        if current.co_name != "<module>" and not current.co_name.startswith("<"):
            name = getattr(current, "co_qualname", current.co_name)
            body_lines = own - enclosing
            if body_lines:
                functions.setdefault(name, set()).update(body_lines)
        stack.extend(
            (constant, own)
            for constant in current.co_consts
            if isinstance(constant, types.CodeType)
        )
    return functions


def format_line_ranges(lines: set[int]) -> str:
    ranges: list[list[int]] = []
    for line in sorted(lines):
        if ranges and line == ranges[-1][1] + 1:
            ranges[-1][1] = line
        else:
            ranges.append([line, line])
    return ", ".join(
        str(first) if first == last else f"{first}-{last}"
        for first, last in ranges
    )


def executed_lines_from_log(log: Path) -> set[int] | None:
    if not log.is_file():
        return None
    executed: set[int] = set()
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            values = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(values, list):
            executed.update(
                value for value in values if isinstance(value, int) and value > 0
            )
    return executed


def render_command(command: list[str], script: Path, fixture: Path) -> list[str]:
    values = {
        "{python}": sys.executable,
        "{script}": str(script),
        "{fixture}": str(fixture),
    }
    return [
        item.replace("{python}", values["{python}"])
        .replace("{script}", values["{script}"])
        .replace("{fixture}", values["{fixture}"])
        for item in command
    ]


def verify_output(case: dict[str, Any], fixture: Path) -> list[str]:
    expect = case["expect"]
    errors: list[str] = []
    for item in expect.get("files", []):
        path = fixture / item["path"]
        if not path.is_file():
            errors.append(f"не создан ожидаемый файл {item['path']}")
            continue
        text = path.read_text(encoding="utf-8")
        if item.get("json"):
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                errors.append(f"{item['path']}: JSON не разобран: {exc}")
        contains = item.get("contains")
        if contains and contains not in text:
            errors.append(f"{item['path']}: нет ожидаемого текста {contains!r}")
    return errors


def trace_environment(
    tracer: Path,
    fixture: Path,
    log: Path,
    marker: Path,
    script: Path,
    coverage_log: Path,
) -> dict[str, str]:
    python_path = str(tracer)
    if inherited := os.environ.get("PYTHONPATH"):
        python_path += os.pathsep + inherited
    return {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": python_path,
        "APM_CONTRACT_FIXTURE_ROOT": str(fixture),
        "APM_CONTRACT_INPUT_LOG": str(log),
        "APM_CONTRACT_TRACE_MARKER": str(marker),
        "APM_CONTRACT_COVERAGE_SCRIPT": str(script),
        "APM_CONTRACT_COVERAGE_LOG": str(coverage_log),
    }


def observed_conditional_inputs(log: Path) -> set[str]:
    if not log.is_file():
        return set()
    missing: set[str] = set()
    written: set[str] = set()
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        path = safe_relative(item.get("path")) if isinstance(item, dict) else None
        if path is None:
            continue
        value = path.as_posix()
        if item.get("event") == "write":
            written.add(value)
        elif item.get("event") in {None, "missing"}:
            missing.add(value)
    return missing - written


def run_case(
    skill: Path,
    case: dict[str, Any],
    coverage: dict[tuple[Path, Path], set[int]],
) -> list[str]:
    source_fixture = skill / case["fixture"]
    script = (skill / case["script"]).resolve()
    with tempfile.TemporaryDirectory(prefix="проверка скрипта ") as temporary:
        temporary_path = Path(temporary)
        fixture = temporary_path / "fixture"
        tracer = temporary_path / "trace"
        trace_log = temporary_path / "conditional-inputs.jsonl"
        trace_marker = temporary_path / "trace-loaded"
        coverage_log = temporary_path / "coverage.jsonl"
        shutil.copytree(source_fixture, fixture)
        tracer.mkdir()
        (tracer / "sitecustomize.py").write_text(
            TRACE_SITECUSTOMIZE,
            encoding="utf-8",
        )
        for index, prepare in enumerate(case.get("prepare", [])):
            prepared_command = render_command(prepare, script, fixture)
            try:
                prepared = subprocess.run(
                    prepared_command,
                    cwd=fixture,
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    timeout=60,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return [f"не удалось запустить подготовительную команду {index}: {exc}"]
            if prepared.returncode != 0:
                details = prepared.stderr.strip() or prepared.stdout.strip()
                return [
                    f"подготовительная команда {index} завершилась с кодом "
                    f"{prepared.returncode}: {details}",
                ]
        command = render_command(case["command"], script, fixture)
        try:
            result = subprocess.run(
                command,
                cwd=fixture,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=trace_environment(
                    tracer,
                    fixture,
                    trace_log,
                    trace_marker,
                    script,
                    coverage_log,
                ),
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return [f"не удалось выполнить контрактный сценарий за 60 с: {exc}"]
        errors: list[str] = []
        if not trace_marker.is_file():
            errors.append("не удалось включить наблюдение за условными входами")
        executed = executed_lines_from_log(coverage_log)
        if executed is None:
            errors.append("не удалось собрать покрытие строк скрипта")
        else:
            coverage.setdefault((skill, Path(case["script"])), set()).update(executed)
        expect = case["expect"]
        expected_exit_code = expect.get("exit_code", 0)
        if result.returncode != expected_exit_code:
            errors.append(
                f"команда завершилась с кодом {result.returncode}, ожидался "
                f"{expected_exit_code}: "
                f"{result.stderr.strip() or result.stdout.strip()}",
            )
            return errors
        for value in expect.get("stdout_contains", []):
            if value not in result.stdout:
                errors.append(f"stdout не содержит {value!r}")
        for value in expect.get("stderr_contains", []):
            if value not in result.stderr:
                errors.append(f"stderr не содержит {value!r}")
        errors.extend(verify_output(case, fixture))
        if expected_exit_code == 0:
            observed = observed_conditional_inputs(trace_log)
            for operation_id, declared_values in case.get("_operation_inputs", {}).items():
                undeclared = sorted(observed - set(declared_values))
                for path in undeclared:
                    errors.append(
                        f"операция {operation_id!r} проверяет отсутствующий путь "
                        f"{path}, но не объявляет его в inputs",
                    )
        return errors


def main() -> int:
    args = parse_args()
    missing = [str(path) for path in args.paths if not path.exists()]
    if missing:
        print(f"Пути не найдены: {', '.join(missing)}", file=sys.stderr)
        return 2
    errors: list[str] = []
    cases_by_skill = {}
    allowances_by_skill = {}
    for skill in skill_directories(args.paths):
        cases, allowances = load_cases(skill, errors)
        cases_by_skill[skill] = cases
        allowances_by_skill[skill] = allowances
    run_errors: list[str] = []
    count = 0
    coverage: dict[tuple[Path, Path], set[int]] = {}
    for skill, cases in cases_by_skill.items():
        for case in cases:
            count += 1
            for error in run_case(skill, case, coverage):
                run_errors.append(f"{skill}::{case['id']}: {error}")
    checked_allowances: set[tuple[Path, Path]] = set()
    for (skill, script_rel), executed in sorted(coverage.items()):
        script = skill / script_rel
        try:
            expected = executable_lines(script)
            functions = script_functions(script)
        except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
            run_errors.append(f"{script}: не удалось определить исполняемые строки: {exc}")
            continue
        allowed = allowances_by_skill.get(skill, {}).get(script_rel, {})
        checked_allowances.add((skill, script_rel))
        unexecuted = {
            name for name, lines in functions.items() if not (lines & executed)
        }
        for name in sorted(unexecuted - set(allowed)):
            run_errors.append(
                f"{script}: функция {name} не выполнена ни одним сценарием — "
                "добавь активирующий успешный сценарий либо запись в "
                "unexercised_functions с причиной",
            )
        for name in sorted(allowed):
            if name not in functions:
                run_errors.append(
                    f"{script}: unexercised_functions называет неизвестную "
                    f"функцию {name}",
                )
            elif name not in unexecuted:
                run_errors.append(
                    f"{script}: запись unexercised_functions о функции {name} "
                    "устарела — функция выполняется сценариями, удали запись",
                )
        acknowledged = sorted(unexecuted & set(allowed))
        if acknowledged:
            print(
                f"{script}: непроверенные функции по объявленным причинам: "
                + ", ".join(acknowledged),
            )
        uncovered = expected - executed
        if uncovered:
            print(
                f"{script}: сценарии не выполнили {len(uncovered)} из "
                f"{len(expected)} исполняемых строк. Непроверенные строки — "
                "обязательная очередь поведенческой проверки: "
                f"{format_line_ranges(uncovered)}",
            )
        else:
            print(f"{script}: сценарии выполнили все {len(expected)} исполняемых строк.")
    for skill, allowances in allowances_by_skill.items():
        for script_rel in sorted(set(allowances) - {
            script for owner, script in checked_allowances if owner == skill
        }):
            run_errors.append(
                f"{skill / script_rel}: unexercised_functions объявлен, но нет "
                "данных покрытия — у скрипта нет выполненных сценариев",
            )
    all_errors = errors + run_errors
    if all_errors:
        print("\n".join(all_errors), file=sys.stderr)
        return 1
    print(f"Контрактные сценарии скриптов пройдены: {count}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
