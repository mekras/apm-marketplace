#!/usr/bin/env python3
"""Запуск модельных evals навыков через переносимый адаптер модели.

Это измерение, а не контроль качества: модельный прогон опционален и
запускается отдельной целью `apm run evals`. Детерминированный контроль качества
`apm run tests` модель не требует.

Модель вызывается через адаптер по переносимому контракту:
вызов `<адаптер> <модель>`, промпт на stdin, текст ответа на stdout. Средство
запуска само вкладывает требование вернуть JSON в текст промпта и разбирает JSON
из ответа. Привязки к конкретному CLI или модели в этом файле нет: всё задаётся
локальными настройками evals.local.yml, которые в Git не попадают.

Адаптер может дополнительно вернуть JSON-обёртку `{"output": "...", "usage":
{...}}` с фактическими токенами, стоимостью и временем; простой текстовый
контракт сохраняется.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import itertools
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

CONFIG_NAME = "evals.local.yml"
SAMPLE_NAME = "evals.sample.yml"


TRIGGER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "should_trigger", "rationale"],
                "properties": {
                    "id": {"type": "string"},
                    "should_trigger": {"type": "boolean"},
                    "rationale": {"type": "string"},
                },
            },
        },
    },
}


ANSWER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answers"],
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "answer"],
                "properties": {
                    "id": {"type": "string"},
                    "answer": {"type": "string"},
                },
            },
        },
    },
}


FIXTURE_ANSWER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {
        "answer": {"type": "string"},
        "selected_skill": {"type": "string"},
    },
}


CATALOG_SELECTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["selected_skill"],
    "properties": {"selected_skill": {"type": "string"}},
}


JUDGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "passed", "reasons", "missing"],
                "properties": {
                    "id": {"type": "string"},
                    "passed": {"type": "boolean"},
                    "reasons": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "missing": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
    },
}


# Тип вызова модели: (prompt, schema) -> разобранный JSON-объект.
ModelCall = Callable[[str, dict[str, Any]], dict[str, Any]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Запустить модельные evals навыков через адаптер модели.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Каталоги навыков или корни репозитория для обхода. По умолчанию текущий каталог.",
    )
    parser.add_argument(
        "--fixture-registry",
        type=Path,
        default=Path("evals/fixtures/registry.json"),
        help="Реестр проектных фикстур. По умолчанию evals/fixtures/registry.json.",
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=0,
        help="Число повторов каждого режима; 0 берёт repetitions из локальных настроек.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Куда записать JSON-отчёт. По умолчанию каталог results_dir из локальных настроек.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Подтвердить уже показанную стоимость модельного прогона без вопроса.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("APM_EVAL_CONFIG", CONFIG_NAME)),
        help="Путь к локальным настройкам evals. По умолчанию APM_EVAL_CONFIG или evals.local.yml.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=int(os.environ.get("APM_EVAL_LIMIT", "0")),
        help="Ограничить число сценариев результатов. 0 означает все сценарии.",
    )
    parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help=(
            "Запустить только проверку с указанным id. Можно повторять. "
            "Также читается из APM_EVAL_CASE_ID или APM_EVAL_CASE_IDS "
            "через запятую."
        ),
    )
    args = parser.parse_args()
    env_case_ids = []
    for name in ("APM_EVAL_CASE_ID", "APM_EVAL_CASE_IDS"):
        raw_value = os.environ.get(name, "")
        env_case_ids.extend(
            part.strip()
            for part in raw_value.split(",")
            if part.strip()
        )
    args.case_id = [*env_case_ids, *args.case_id]
    return args


def bootstrap_config(repo_root: Path, config_path: Path) -> None:
    """Создать локальные настройки из образца и скрыть их от Git."""
    sample = repo_root / SAMPLE_NAME
    if not sample.exists():
        print(
            f"Нет ни {config_path.name}, ни образца {SAMPLE_NAME}. "
            "Модельные evals настроить нельзя.",
            file=sys.stderr,
        )
        return
    config_path.write_text(sample.read_text(encoding="utf-8"), encoding="utf-8")
    exclude = repo_root / ".git" / "info" / "exclude"
    rel = config_path.name
    local_paths = {rel, "eval-results/"}
    if exclude.parent.is_dir():
        lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
        missing = local_paths - {line.strip() for line in lines}
        if missing:
            with exclude.open("a", encoding="utf-8") as handle:
                handle.writelines(f"{item}\n" for item in sorted(missing))
    print(
        f"Созданы локальные настройки {rel} из образца и добавлены в "
        ".git/info/exclude (включая каталог отчётов eval-results).\n"
        f"Заполните в нём adapters и models, затем повторите `apm run evals`.\n"
        "Модельные evals пока пропущены.",
        flush=True,
    )


def load_config(repo_root: Path, config_path: Path) -> dict[str, Any] | None:
    """Прочитать настройки evals. Вернуть None, если запуск нужно пропустить."""
    if not config_path.is_absolute():
        config_path = repo_root / config_path
    if not config_path.exists():
        bootstrap_config(repo_root, config_path)
        return None
    try:
        data = parse_evals_yaml(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        print(f"Не удалось прочитать {config_path}: {error}", file=sys.stderr)
        return None

    raw_adapters = data.get("adapters")
    if not isinstance(raw_adapters, dict) or not raw_adapters:
        print(
            f"В настройках {config_path.name} не задан раздел adapters "
            "(имя адаптера -> команда). Модельные evals пропущены.",
            file=sys.stderr,
        )
        return None
    adapters = {name: shlex.split(str(command)) for name, command in raw_adapters.items()}

    env_model = os.environ.get("APM_EVAL_MODEL")
    model_specs = [env_model] if env_model else list(data.get("models") or [])
    judge_spec = os.environ.get("APM_EVAL_JUDGE_MODEL") or data.get("judge")
    timeout = int(os.environ.get("APM_EVAL_TIMEOUT") or data.get("timeout") or 900)
    repetitions = int(os.environ.get("APM_EVAL_REPETITIONS") or data.get("repetitions") or 3)
    judge_repetitions = int(os.environ.get("APM_EVAL_JUDGE_REPETITIONS") or data.get("judge_repetitions") or 3)
    if repetitions < 1:
        print("repetitions должен быть не меньше 1.", file=sys.stderr)
        return None
    if judge_repetitions < 1:
        print("judge_repetitions должен быть не меньше 1.", file=sys.stderr)
        return None

    if not model_specs:
        print(
            f"В настройках {config_path.name} не заданы models. "
            "Модельные evals пропущены.",
            file=sys.stderr,
        )
        return None

    spec_errors: list[str] = []
    workspace_models = set(data.get("workspace_models") or [])
    runs = [
        resolve_run(spec, adapters, f"models[{index}]", spec_errors)
        for index, spec in enumerate(model_specs)
    ]
    runs = [run for run in runs if run]
    for run in runs:
        run["workspace"] = run["label"] in workspace_models
    if judge_spec:
        judge = resolve_run(judge_spec, adapters, "judge", spec_errors)
    else:
        judge = None
        spec_errors.append(
            "judge: не задана модель-судья; укажите judge в формате адаптер:модель. "
            "Судья не берётся из models по умолчанию: в models держите слабые модели "
            "для прогона, а судьёй назначайте сильную модель."
        )

    if spec_errors or not runs or judge is None:
        for error in spec_errors:
            print(error, file=sys.stderr)
        print(
            f"Модельные evals пропущены из-за ошибок в {config_path.name}.",
            file=sys.stderr,
        )
        return None
    return {
        "runs": runs,
        "judge": judge,
        "timeout": timeout,
        "repetitions": repetitions,
        "judge_repetitions": judge_repetitions,
        "results_dir": str(data.get("results_dir") or "eval-results"),
        "pricing": data.get("pricing") if isinstance(data.get("pricing"), dict) else {},
    }


def parse_evals_yaml(source: str) -> dict[str, Any]:
    """Разобрать документированное подмножество YAML без сторонних пакетов.

    Конфигурация evals намеренно имеет небольшую схему. Поддерживаются корневые
    скаляры, разделы ``adapters`` и ``pricing``, а также списки ``models`` и
    ``workspace_models``. Остальной YAML отклоняется с номером строки, чтобы
    расширение формата не превратилось в неявную зависимость от PyYAML.
    """
    result: dict[str, Any] = {}
    section: str | None = None
    pricing_model: str | None = None

    for line_number, raw_line in enumerate(source.splitlines(), start=1):
        line = strip_yaml_comment(raw_line).rstrip()
        if not line.strip():
            continue
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            raise ValueError(f"строка {line_number}: отступы должны состоять из пробелов")
        indent = len(line) - len(line.lstrip(" "))
        content = line.lstrip(" ")

        if indent == 0:
            key, raw_value = split_yaml_pair(content, line_number)
            pricing_model = None
            if raw_value:
                result[key] = parse_yaml_scalar(raw_value, line_number)
                section = None
            elif key in {"adapters", "pricing"}:
                result[key] = {}
                section = key
            elif key in {"models", "workspace_models"}:
                result[key] = []
                section = key
            else:
                raise ValueError(
                    f"строка {line_number}: для {key!r} требуется значение"
                )
            continue

        if indent == 2 and section in {"models", "workspace_models"}:
            if not content.startswith("- "):
                raise ValueError(f"строка {line_number}: ожидается элемент списка")
            result[section].append(parse_yaml_scalar(content[2:].strip(), line_number))
            continue

        if indent == 2 and section == "adapters":
            key, raw_value = split_yaml_pair(content, line_number)
            if not raw_value:
                raise ValueError(f"строка {line_number}: команда адаптера не задана")
            result[section][key] = parse_yaml_scalar(raw_value, line_number)
            continue

        if indent == 2 and section == "pricing":
            key, raw_value = split_yaml_pair(content, line_number)
            if raw_value:
                raise ValueError(
                    f"строка {line_number}: тариф модели должен быть разделом"
                )
            result[section][key] = {}
            pricing_model = key
            continue

        if indent == 4 and section == "pricing" and pricing_model:
            key, raw_value = split_yaml_pair(content, line_number)
            if not raw_value:
                raise ValueError(f"строка {line_number}: ставка не задана")
            result[section][pricing_model][key] = parse_yaml_scalar(
                raw_value,
                line_number,
            )
            continue

        raise ValueError(
            f"строка {line_number}: конструкция не входит в поддерживаемую схему evals"
        )

    return result


def strip_yaml_comment(line: str) -> str:
    """Удалить комментарий YAML, не затрагивая решётку внутри кавычек."""
    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if char in {'"', "'"}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            continue
        if char == "#" and quote is None and (index == 0 or line[index - 1].isspace()):
            return line[:index]
    return line


def split_yaml_pair(content: str, line_number: int) -> tuple[str, str]:
    """Разделить пару YAML по двоеточию перед пробелом или концом строки."""
    quote: str | None = None
    escaped = False
    for index, char in enumerate(content):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if char in {'"', "'"}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            continue
        if char == ":" and quote is None and (
            index + 1 == len(content) or content[index + 1].isspace()
        ):
            key = content[:index].strip()
            value = content[index + 1 :].strip()
            if not key:
                raise ValueError(f"строка {line_number}: ключ не задан")
            return key, value
    raise ValueError(f"строка {line_number}: ожидается пара ключ: значение")


def parse_yaml_scalar(value: str, line_number: int) -> Any:
    """Разобрать простой скаляр из поддерживаемой схемы evals."""
    if value == "[]":
        return []
    if value == "{}":
        return {}
    if value in {"null", "Null", "NULL", "~"}:
        return None
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if re.fullmatch(r"[-+]?\d+", value):
        return int(value)
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)", value):
        return float(value)
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"строка {line_number}: неправильная строка в двойных кавычках"
            ) from error
        if not isinstance(parsed, str):
            raise ValueError(f"строка {line_number}: ожидается строка")
        return parsed
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ValueError(
                f"строка {line_number}: неправильная строка в одинарных кавычках"
            )
        return value[1:-1].replace("''", "'")
    return value


def resolve_run(
    spec: Any,
    adapters: dict[str, list[str]],
    label: str,
    errors: list[str],
) -> dict[str, Any] | None:
    """Разобрать запись `адаптер:модель` и связать её с командой адаптера."""
    if not isinstance(spec, str) or ":" not in spec:
        errors.append(f"{label}: ожидается формат адаптер:модель, получено {spec!r}")
        return None
    name, model = spec.split(":", 1)
    name, model = name.strip(), model.strip()
    if name not in adapters:
        errors.append(
            f"{label}: неизвестный адаптер {name!r}; задайте его в разделе adapters",
        )
        return None
    if not model:
        errors.append(f"{label}: не указана модель в {spec!r}")
        return None
    return {"adapter": adapters[name], "model": model, "label": spec}


def russian_count(value: int, one: str, few: str, many: str) -> str:
    """Вернуть число с подходящей формой русского существительного."""
    remainder = abs(value) % 100
    if 11 <= remainder <= 14:
        form = many
    else:
        last = remainder % 10
        form = one if last == 1 else few if 2 <= last <= 4 else many
    return f"{value} {form}"


def extract_json(text: str) -> dict[str, Any]:
    """Достать JSON-объект из текстового ответа модели (best-effort)."""
    text = text.strip()
    candidates: list[str] = []
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    candidates.append(text)
    first = first_json_object(text)
    if first:
        candidates.append(first)
    for candidate in candidates:
        try:
            # Модель может оставить в строковом поле буквальный управляющий
            # символ. Структуру JSON и схему всё равно проверяет вызывающий
            # код, поэтому допускаем только эту особенность строк.
            return json.loads(candidate, strict=False)
        except json.JSONDecodeError:
            try:
                return json.loads(repair_unescaped_quotes(candidate), strict=False)
            except json.JSONDecodeError:
                continue
    answer_envelope = extract_answer_envelope(text)
    if answer_envelope is not None:
        return answer_envelope
    raise RuntimeError(f"Модель не вернула разбираемый JSON:\n{text}")


def extract_answer_envelope(text: str) -> dict[str, Any] | None:
    """Извлечь длинный ответ, если модель испортила только кавычки внутри него.

    Этот запасной путь применяется исключительно к оболочке ANSWER_SCHEMA:
    один id и одно поле answer. Он не принимает произвольный текст и не
    используется для вердиктов или выбора навыков.
    """
    start = re.match(
        r'\s*\{\s*"answers"\s*:\s*\[\s*\{\s*"id"\s*:\s*"([^"\\]*)"\s*,\s*"answer"\s*:\s*"',
        text,
        re.DOTALL,
    )
    end = re.search(r'"\s*}\s*]\s*}\s*$', text, re.DOTALL)
    if not start or not end or end.start() < start.end():
        return None
    answer = decode_json_string_lossy(text[start.end() : end.start()])
    return {"answers": [{"id": start.group(1), "answer": answer}]}


def decode_json_string_lossy(value: str) -> str:
    """Раскрыть обычные JSON-экранирования, сохраняя неэкранированные кавычки."""
    result: list[str] = []
    index = 0
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", '"': '"', "\\": "\\", "/": "/"}
    while index < len(value):
        char = value[index]
        if char != "\\" or index + 1 >= len(value):
            result.append(char)
            index += 1
            continue
        marker = value[index + 1]
        if marker == "u" and index + 5 < len(value):
            try:
                result.append(chr(int(value[index + 2 : index + 6], 16)))
                index += 6
                continue
            except ValueError:
                pass
        result.append(escapes.get(marker, marker))
        index += 2
    return "".join(result)


def repair_unescaped_quotes(text: str) -> str:
    """Экранировать кавычки, оставленные моделью внутри строкового поля JSON."""
    result: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if not in_string:
            result.append(char)
            if char == '"':
                in_string = True
            continue
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\":
            result.append(char)
            escaped = True
            continue
        if char != '"':
            result.append(char)
            continue
        following = next((item for item in text[index + 1 :] if not item.isspace()), "")
        if following in {":", ",", "}", "]", ""}:
            result.append(char)
            in_string = False
        else:
            result.append('\\"')
    return "".join(result)


def unwrap_adapter_response(text: str) -> tuple[str, dict[str, Any]]:
    """Принять старый текст либо обёртку адаптера с фактической телеметрией."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return text, {}
    if not isinstance(value, dict) or not isinstance(value.get("output"), str):
        return text, {}
    usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
    metrics = {
        key: usage[key]
        for key in ("input_tokens", "output_tokens", "cost", "elapsed_seconds")
        if isinstance(usage.get(key), (int, float))
    }
    return value["output"], metrics


def first_json_object(text: str) -> str:
    """Вернуть первый сбалансированный JSON-объект в тексте."""
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


def make_model_call(adapter: list[str], model: str, timeout: int, workspace: Path | None = None) -> ModelCall:
    """Собрать вызов модели через адаптер по контракту prompt -> текст."""

    def call(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        if schema is ANSWER_SCHEMA:
            full_prompt = (
                f"{prompt}\n\n"
                "Верни только обычный текст ответа. Допускаются кавычки, списки "
                "и фрагменты кода. Не используй JSON и служебную оболочку.\n"
            )
        else:
            full_prompt = (
                f"{prompt}\n\n"
                "Верни только один JSON-объект без пояснений и без оформления в "
                "кодовый блок, строго соответствующий схеме:\n"
                f"{json.dumps(schema, ensure_ascii=False)}\n"
            )
        try:
            completed = subprocess.run(
                [*adapter, model],
                input=full_prompt,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
                env={**os.environ, **({"APM_EVAL_WORKSPACE": str(workspace)} if workspace else {})},
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Адаптер модели не найден: {' '.join(adapter)}. "
                "Проверьте adapter в настройках evals.",
            ) from exc
        if completed.returncode != 0:
            raise RuntimeError(
                f"Адаптер вернул код {completed.returncode}.\n"
                f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}",
            )
        output, actual_metrics = unwrap_adapter_response(completed.stdout)
        result = extract_answer_text(output, prompt) if schema is ANSWER_SCHEMA else extract_json(output)
        setattr(call, "last_metrics", actual_metrics)
        return result

    return call


def extract_answer_text(text: str, prompt: str) -> dict[str, Any]:
    """Извлечь длинный ответ без зависимости от служебной оболочки модели."""
    case = re.search(r'"id"\s*:\s*"([^"]+)"', prompt)
    if not case:
        raise RuntimeError("В запросе сценария не найден идентификатор.")

    answer = text.strip()
    if re.match(r'^\{\s*"answers"\s*:', answer):
        try:
            legacy = extract_json(answer)
        except RuntimeError:
            legacy = None
        if isinstance(legacy, dict) and isinstance(legacy.get("answers"), list):
            return legacy
    if answer.startswith("<<ANSWER>>"):
        answer = answer[len("<<ANSWER>>") :].lstrip("\r\n")
    answer = re.sub(
        r"(?:\r?\n)?[ \t]*(?:</ANSWER>|<</ANSWER>>)[ \t]*$",
        "",
        answer,
    ).strip()
    if not answer:
        raise RuntimeError("Модель вернула пустой ответ сценария результата.")
    return {"answers": [{"id": case.group(1), "answer": answer}]}


def find_skill_dirs(paths: list[Path]) -> list[Path]:
    skill_dirs: set[Path] = set()
    for path in paths:
        path = path.resolve()
        if (path / "SKILL.md").is_file():
            skill_dirs.add(path)
            continue
        for skill_file in path.rglob("SKILL.md"):
            if ".git" in skill_file.parts:
                continue
            skill_dirs.add(skill_file.parent)
    return sorted(skill_dirs)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_frontmatter(skill_path: Path) -> dict[str, str]:
    frontmatter: dict[str, str] = {}
    in_frontmatter = False
    current_key: str | None = None
    current_lines: list[str] = []

    for line in skill_path.read_text(encoding="utf-8").splitlines():
        if line.strip() == "---":
            if not in_frontmatter:
                in_frontmatter = True
                continue
            break
        if not in_frontmatter:
            continue
        if line and not line.startswith((" ", "\t")) and ":" in line:
            if current_key:
                frontmatter[current_key] = " ".join(current_lines).strip()
            current_key, value = line.split(":", 1)
            current_key = current_key.strip()
            current_lines = [value.strip().strip(">")]
            continue
        if current_key:
            current_lines.append(line.strip())

    if current_key:
        frontmatter[current_key] = " ".join(current_lines).strip()
    return frontmatter


def collect_trigger_cases(skill_dirs: list[Path]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for skill_dir in skill_dirs:
        trigger_path = skill_dir / "evals" / "triggers.json"
        if not trigger_path.exists():
            continue
        skill_path = skill_dir / "SKILL.md"
        frontmatter = read_frontmatter(skill_path)
        trigger_data = load_json(trigger_path)
        skill_name = trigger_data["skill_name"]
        for case in trigger_data["cases"]:
            cases.append(
                {
                    "id": case["id"],
                    "skill_name": skill_name,
                    "skill_description": frontmatter.get("description", ""),
                    "prompt": case["prompt"],
                    "expected_should_trigger": case["should_trigger"],
                    "expected_rationale": case["rationale"],
                },
            )
    return cases


def filter_trigger_cases(
    cases: list[dict[str, Any]],
    case_ids: set[str],
) -> list[dict[str, Any]]:
    if not case_ids:
        return cases
    return [case for case in cases if case["id"] in case_ids]


def trigger_prompt(cases: list[dict[str, Any]]) -> str:
    payload = [
        {
            "id": case["id"],
            "skill_name": case["skill_name"],
            "skill_description": case["skill_description"],
            "user_prompt": case["prompt"],
        }
        for case in cases
    ]
    return (
        "Ты проверяешь маршрутизацию навыков агента.\n"
        "Для каждого кейса реши, должен ли указанный навык сработать для "
        "пользовательского запроса. Опирайся на description навыка как на "
        "контракт маршрутизации. Не угадывай по названию навыка, если "
        "description не покрывает ситуацию.\n"
        f"Кейсы:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
    )


def run_trigger_evals(
    *,
    cases: list[dict[str, Any]],
    call: ModelCall,
) -> list[str]:
    if not cases:
        print("Сценарии модельной проверки выбора навыков не найдены.", flush=True)
        return []

    print(
        "Запускаю модельную проверку выбора навыков: "
        f"{russian_count(len(cases), 'сценарий', 'сценария', 'сценариев')}.",
        flush=True,
    )
    errors: list[str] = []
    missing_cases: list[dict[str, Any]] = []
    sorted_cases = sorted(cases, key=lambda item: item["skill_name"])
    for skill_name, grouped_cases in itertools.groupby(
        sorted_cases,
        key=lambda item: item["skill_name"],
    ):
        skill_cases = list(grouped_cases)
        print(
            f"Проверяю сценарии выбора навыка {skill_name}: {len(skill_cases)}.",
            flush=True,
        )
        result = call(trigger_prompt(skill_cases), TRIGGER_SCHEMA)
        actual_by_id = {item.get("id"): item for item in result.get("results", [])}
        for case in skill_cases:
            actual = actual_by_id.get(case["id"])
            if not actual:
                missing_cases.append(case)
                continue
            if actual.get("should_trigger") != case["expected_should_trigger"]:
                errors.append(
                    f"{case['id']}: ожидалось should_trigger="
                    f"{case['expected_should_trigger']}, модель вернула "
                    f"{actual.get('should_trigger')}. Обоснование: "
                    f"{actual.get('rationale', '')}",
                )
    for case in missing_cases:
        print(f"Повторяю сценарий выбора {case['id']} отдельно.", flush=True)
        result = call(trigger_prompt([case]), TRIGGER_SCHEMA)
        actual_by_id = {item.get("id"): item for item in result.get("results", [])}
        actual = actual_by_id.get(case["id"])
        if not actual:
            errors.append(f"{case['id']}: модель не вернула результат.")
            continue
        if actual.get("should_trigger") != case["expected_should_trigger"]:
            errors.append(
                f"{case['id']}: ожидалось should_trigger="
                f"{case['expected_should_trigger']}, модель вернула "
                f"{actual.get('should_trigger')}. Обоснование: "
                f"{actual.get('rationale', '')}",
            )
    if not errors:
        print(f"Пройдено сценариев выбора навыков: {len(cases)} из {len(cases)}.", flush=True)
    return errors


def collect_result_groups(
    skill_dirs: list[Path],
    limit: int,
) -> list[tuple[Path, dict[str, Any], list[dict[str, Any]]]]:
    remaining = limit
    groups: list[tuple[Path, dict[str, Any], list[dict[str, Any]]]] = []
    for skill_dir in skill_dirs:
        result_path = skill_dir / "evals" / "result-scenarios.json"
        if not result_path.exists():
            continue
        data = load_json(result_path)
        cases = data["cases"]
        if limit > 0:
            if remaining <= 0:
                break
            cases = cases[:remaining]
            remaining -= len(cases)
        groups.append((skill_dir, data, cases))
    return groups


def filter_result_groups(
    groups: list[tuple[Path, dict[str, Any], list[dict[str, Any]]]],
    case_ids: set[str],
) -> list[tuple[Path, dict[str, Any], list[dict[str, Any]]]]:
    if not case_ids:
        return groups
    filtered: list[tuple[Path, dict[str, Any], list[dict[str, Any]]]] = []
    for skill_dir, data, cases in groups:
        selected = [case for case in cases if case["id"] in case_ids]
        if selected:
            filtered.append((skill_dir, data, selected))
    return filtered


def answer_prompt(
    repo_root: Path,
    skill_dir: Path,
    data: dict[str, Any],
    cases: list[dict[str, Any]],
) -> str:
    skill_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    target_cases = [
        {
            "id": case["id"],
            "prompt": case["prompt"],
            "input_files": case.get("input_files", []),
        }
        for case in cases
    ]
    return (
        "Ты проверяемая модель. Примени навык к каждому пользовательскому "
        "сценарию и дай ответ так, как если бы пользователь реально попросил "
        "выполнить эту задачу. Ответ должен быть пригоден для проверки: покажи "
        "применённую процедуру навыка, конкретные выводы/findings, важные "
        "ограничения, действия или изменяемые файлы. Не ищи скрытых критериев "
        "приёмки и не оценивай сам себя. "
        "Поле input_files в сценарии описывает доступный fixture задачи. "
        "Если у элемента есть content, считай это содержимым файла. Если "
        "content отсутствует, используй prompt и purpose как единственные "
        "доступные факты о файле; не заявляй, что файл отсутствует в рабочей "
        "области, и не ищи его в текущем cwd. "
        "Если сценарий требует изменить файлы, а содержимое файлов не дано, "
        "верни проверяемый результат изменения: имена создаваемых или "
        "изменяемых файлов, какие фрагменты куда переносятся или удаляются, "
        "и какие правила остаются в каждом файле. Не отвечай планом: формулируй "
        "результат так, как будто применение навыка уже выполнено. Для каждого "
        "ключевого вывода используй поля severity, "
        "observed_problem, expected_conclusion и acceptable_fix_direction. "
        "Не упоминай, что это тест.\n"
        f"Навык:\n{skill_text}\n\n"
        f"Сценарии:\n{json.dumps(target_cases, ensure_ascii=False, indent=2)}\n"
    )


def judge_prompt(
    data: dict[str, Any],
    cases: list[dict[str, Any]],
    answers: list[dict[str, str]],
) -> str:
    expected_cases = {case["id"]: case for case in data["cases"] if case in cases}
    payload = {
        "skill_name": data["skill_name"],
        "cases": [expected_cases[case["id"]] for case in cases],
        "answers": answers,
    }
    return (
        "Ты строгий судья evals навыков агента.\n"
        "Для каждого ответа проверь, реально ли модель применила навык к "
        "сценарию. Ответ проходит только если он удовлетворяет expected_output, "
        "application_evidence, oracle.success_criteria и assertions, а также не "
        "нарушает must_not и не содержит oracle.failure_indicators. Не засчитывай "
        "общие советы, пересказ схемы или формальное совпадение заголовков без "
        "признаков применения навыка. Оценивай только текст ответа, потому что "
        "runner не создаёт fixture-репозиторий для фактических файловых правок. "
        "Если assertion говорит, что результат меняет или создаёт файлы, считай "
        "его выполненным только когда ответ содержит конкретные имена файлов и "
        "проверяемые сведения о переносимых, удаляемых или добавляемых правилах.\n"
        f"Данные для проверки:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
    )


def run_result_evals(
    *,
    repo_root: Path,
    groups: list[tuple[Path, dict[str, Any], list[dict[str, Any]]]],
    call: ModelCall,
    judge_call: ModelCall,
) -> list[str]:
    total = sum(len(cases) for _, _, cases in groups)
    if not total:
        print("Сценарии модельной проверки результатов не найдены.", flush=True)
        return []

    print(
        "Запускаю модельную проверку результатов: "
        f"{russian_count(total, 'сценарий', 'сценария', 'сценариев')}.",
        flush=True,
    )
    errors: list[str] = []
    passed = 0
    for skill_dir, data, cases in groups:
        print(
            f"Проверяю сценарии результатов навыка {data['skill_name']}: {len(cases)}.",
            flush=True,
        )
        for case in cases:
            single_case = [case]
            answer_result = call(
                answer_prompt(repo_root, skill_dir, data, single_case),
                ANSWER_SCHEMA,
            )
            answers = answer_result.get("answers", [])
            judge_result = judge_call(
                judge_prompt(data, single_case, answers),
                JUDGE_SCHEMA,
            )
            verdicts = {
                item.get("id"): item for item in judge_result.get("results", [])
            }
            verdict = verdicts.get(case["id"])
            if not verdict:
                errors.append(f"{case['id']}: судья не вернул результат.")
                continue
            if verdict.get("passed") is True:
                passed += 1
                continue
            reasons = ", ".join(verdict.get("reasons", []))
            missing = ", ".join(verdict.get("missing", []))
            errors.append(
                f"{case['id']}: сценарий не пройден. Причины: {reasons}. "
                f"Не хватает: {missing}.",
            )
        print(
            f"Завершена проверка навыка {data['skill_name']}.",
            flush=True,
        )
    if not errors:
        print(f"Пройдено сценариев результатов: {passed} из {total}.", flush=True)
    return errors


def run_for_target(
    *,
    repo_root: Path,
    run: dict[str, Any],
    judge: dict[str, Any],
    timeout: int,
    trigger_cases: list[dict[str, Any]],
    result_groups: list[tuple[Path, dict[str, Any], list[dict[str, Any]]]],
) -> list[str]:
    print(f"\n=== Применение навыков: {run['label']} ===", flush=True)
    print(f"Оценка результатов: {judge['label']}.", flush=True)
    call = make_model_call(run["adapter"], run["model"], timeout)
    judge_call = make_model_call(judge["adapter"], judge["model"], timeout)
    trigger_errors = run_trigger_evals(cases=trigger_cases, call=call)
    result_errors = run_result_evals(
        repo_root=repo_root,
        groups=result_groups,
        call=call,
        judge_call=judge_call,
    )
    return [f"[{run['label']}] {error}" for error in trigger_errors + result_errors]


def fixture_snapshot(fixture_dir: Path) -> list[dict[str, str]]:
    """Снять ограниченный текстовый снимок настоящего проектного fixture."""
    files: list[dict[str, str]] = []
    for path in sorted(fixture_dir.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        relative = path.relative_to(fixture_dir).as_posix()
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            files.append({"path": relative, "content": "<двоичный файл>"})
            continue
        files.append({"path": relative, "content": content[:24000]})
    return files


def load_fixture_cases(repo_root: Path, registry_path: Path) -> list[dict[str, Any]]:
    """Прочитать реестр задач; оракулы намеренно находятся вне fixture."""
    path = registry_path if registry_path.is_absolute() else repo_root / registry_path
    if not path.exists():
        return []
    data = load_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise RuntimeError(f"{path}: ожидается объект с массивом cases.")
    cases: list[dict[str, Any]] = []
    for item in data["cases"]:
        if not isinstance(item, dict):
            raise RuntimeError(f"{path}: элемент cases должен быть объектом.")
        fixture = item.get("fixture")
        oracle = item.get("oracle")
        if not isinstance(fixture, str) or not isinstance(oracle, str):
            raise RuntimeError(f"{path}: у fixture-case обязательны fixture и oracle.")
        fixture_dir = path.parent / fixture
        oracle_path = path.parent / oracle
        if not fixture_dir.is_dir() or not oracle_path.is_file():
            raise RuntimeError(f"{path}: не найден fixture или его оракул для {item.get('id')!r}.")
        case = dict(item)
        case["fixture_dir"] = fixture_dir
        case["oracle_data"] = load_json(oracle_path)
        cases.append(case)
    return cases


def catalog_payload(skill_dirs: list[Path], include_body: bool) -> list[dict[str, str]]:
    payload: list[dict[str, str]] = []
    for skill_dir in skill_dirs:
        frontmatter = read_frontmatter(skill_dir / "SKILL.md")
        entry = {
            "name": frontmatter.get("name", skill_dir.name),
            "description": frontmatter.get("description", ""),
        }
        if include_body:
            entry["skill"] = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        payload.append(entry)
    return payload


def fixture_candidate_prompt(
    case: dict[str, Any],
    mode: str,
    skill_dirs: list[Path],
    workspace: bool = False,
    selected_skill: str | None = None,
) -> str:
    fixture = fixture_snapshot(case["fixture_dir"])
    task = {
        "id": case["id"],
        "user_prompt": case["prompt"],
        "project_files": fixture,
    }
    if mode == "baseline":
        context = "Специального навыка нет: реши задачу обычным рабочим способом."
    elif mode in {"skill", "catalog"}:
        target = selected_skill or str(case["target_skill"])
        selected = next((item for item in catalog_payload(skill_dirs, True) if item["name"] == target), None)
        if selected is None:
            raise RuntimeError(f"{case['id']}: не найден выбранный навык {target!r}.")
        context = "Примени данный навык:\n" + json.dumps(selected, ensure_ascii=False)
    workspace_note = (
        "Копия fixture доступна в рабочей папке APM_EVAL_WORKSPACE. Выполни "
        "нужные изменения в ней; итоговый diff будет проверен. " if workspace else ""
    )
    return (
        "Выполни задачу в изолированном проекте. Содержимое fixture — это все "
        "доступные факты; не обращайся к текущему репозиторию и не выдумывай файлы. "
        f"Дай содержательный итог работы, а не план и не самооценку. {workspace_note}\n"
        f"Режим: {mode}.\n{context}\nЗадача и fixture:\n"
        f"{json.dumps(task, ensure_ascii=False, indent=2)}"
    )


def fixture_catalog_selection_prompt(case: dict[str, Any], skill_dirs: list[Path]) -> str:
    task = {
        "user_prompt": case["prompt"],
        "project_paths": [item["path"] for item in fixture_snapshot(case["fixture_dir"])],
    }
    return (
        "Выбери один наиболее подходящий навык для задачи. Верни только его имя "
        "в selected_skill. Выбирай по описаниям; содержимое выбранного навыка "
        "будет загружено отдельным шагом.\n"
        f"Каталог:\n{json.dumps(catalog_payload(skill_dirs, False), ensure_ascii=False)}\n"
        f"Задача:\n{json.dumps(task, ensure_ascii=False)}"
    )


def fixture_judge_prompt(case: dict[str, Any], answer: dict[str, Any], mode: str, workspace_diff: str = "") -> str:
    """Только судье передаётся оракул: кандидат его не видел."""
    judge_oracle = {
        key: value
        for key, value in case["oracle_data"].items()
        if key != "fixture_checks"
    }
    payload = {
        "case_id": case["id"],
        "mode": mode,
        "expected_skill": case.get("catalog_skill", case.get("target_skill")),
        "oracle": judge_oracle,
        "answer": answer,
        "workspace_diff": workspace_diff,
    }
    return (
        "Ты независимый судья качества результата агента. Оцени только ответ "
        "кандидата по скрытому оракулу. Верни passed=true лишь при выполнении "
        "всех success_criteria и отсутствии failure_indicators. Для режима catalog "
        "также проверь, что selected_skill совпадает с expected_skill.\n"
        f"Данные:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def directory_diff(before: Path, after: Path) -> str:
    """Вернуть проверяемый diff временной рабочей копии fixture."""
    paths = {item.relative_to(before) for item in before.rglob("*") if item.is_file()}
    paths.update(item.relative_to(after) for item in after.rglob("*") if item.is_file())
    chunks: list[str] = []
    for relative in sorted(paths):
        old = (before / relative).read_text(encoding="utf-8").splitlines(keepends=True) if (before / relative).is_file() else []
        new = (after / relative).read_text(encoding="utf-8").splitlines(keepends=True) if (after / relative).is_file() else []
        chunks.extend(difflib.unified_diff(old, new, fromfile=f"a/{relative}", tofile=f"b/{relative}"))
    return "".join(chunks)


def check_required_diff(oracle: dict[str, Any], diff: str) -> list[str]:
    rules = oracle.get("required_diff", {})
    if not isinstance(rules, dict):
        return ["required_diff оракула должен быть объектом"]
    errors: list[str] = []
    for path in rules.get("paths", []):
        if f"+++ b/{path}" not in diff and f"--- a/{path}" not in diff:
            errors.append(f"diff не меняет обязательный файл {path}")
    for fragment in rules.get("must_include", []):
        if fragment not in diff:
            errors.append(f"diff не содержит обязательный фрагмент {fragment!r}")
    for fragment in rules.get("must_not_include", []):
        if fragment in diff:
            errors.append(f"diff содержит запрещённый фрагмент {fragment!r}")
    return errors


def estimate_metrics(prompt: str, answer: str, elapsed_seconds: float, pricing: dict[str, Any], label: str, actual: dict[str, Any] | None = None) -> dict[str, Any]:
    actual = actual or {}
    if "input_tokens" in actual and "output_tokens" in actual:
        return {
            "elapsed_seconds": round(float(actual.get("elapsed_seconds", elapsed_seconds)), 3),
            "input_tokens": actual["input_tokens"],
            "output_tokens": actual["output_tokens"],
            "cost": actual.get("cost"),
            "cost_is_estimate": "cost" not in actual,
        }
    input_tokens = max(1, round(len(prompt) / 4))
    output_tokens = max(1, round(len(answer) / 4))
    price = pricing.get(label, {}) if isinstance(pricing.get(label, {}), dict) else {}
    input_rate = float(price.get("input_per_million", 0) or 0)
    output_rate = float(price.get("output_per_million", 0) or 0)
    return {
        "elapsed_seconds": round(elapsed_seconds, 3),
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "estimated_cost": round((input_tokens * input_rate + output_tokens * output_rate) / 1_000_000, 8),
        "cost_is_estimate": True,
    }


def run_fixture_evals(
    *, repo_root: Path, cases: list[dict[str, Any]], skill_dirs: list[Path], run: dict[str, Any],
    judge: dict[str, Any], timeout: int, repetitions: int, judge_repetitions: int, pricing: dict[str, Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    call = make_model_call(run["adapter"], run["model"], timeout)
    judge_call = make_model_call(judge["adapter"], judge["model"], timeout)
    errors: list[str] = []
    records: list[dict[str, Any]] = []
    for case in cases:
        for mode in ("baseline", "skill", "catalog"):
            for repetition in range(1, repetitions + 1):
                with tempfile.TemporaryDirectory(prefix="apm-eval-") as temp:
                    workspace = Path(temp) / "workspace"
                    before = Path(temp) / "before"
                    if run.get("workspace"):
                        shutil.copytree(case["fixture_dir"], workspace)
                        shutil.copytree(case["fixture_dir"], before)
                    call = make_model_call(run["adapter"], run["model"], timeout, workspace if run.get("workspace") else None)
                    started = time.monotonic()
                    selection_prompt = ""
                    selected_skill = None
                    candidate_error = ""
                    prompt = ""
                    try:
                        if mode == "catalog":
                            selection_prompt = fixture_catalog_selection_prompt(case, skill_dirs)
                            selection = call(selection_prompt, CATALOG_SELECTION_SCHEMA)
                            selected_skill = str(selection.get("selected_skill", ""))
                        known_skills = {item["name"] for item in catalog_payload(skill_dirs, False)}
                        if mode == "catalog" and selected_skill not in known_skills:
                            prompt = selection_prompt
                            answer = {
                                "answer": f"Выбран неизвестный навык: {selected_skill}",
                                "selected_skill": selected_skill,
                            }
                        else:
                            prompt = fixture_candidate_prompt(
                                case,
                                "skill" if mode == "catalog" else mode,
                                skill_dirs,
                                bool(run.get("workspace")),
                                selected_skill,
                            )
                            answer = call(prompt, FIXTURE_ANSWER_SCHEMA)
                            if mode == "catalog":
                                answer["selected_skill"] = selected_skill
                    except RuntimeError as error:
                        candidate_error = str(error)
                        answer = {"answer": candidate_error}
                        if selected_skill:
                            answer["selected_skill"] = selected_skill
                    elapsed = time.monotonic() - started
                    candidate_actual = {} if mode == "catalog" else getattr(call, "last_metrics", {})
                    metric_prompt = selection_prompt + prompt
                    workspace_diff = directory_diff(before, workspace) if run.get("workspace") else ""
                verdicts: list[dict[str, Any]] = []
                judge_elapsed = 0.0
                verdict = None
                for _ in range(0 if candidate_error else judge_repetitions):
                    judge_started = time.monotonic()
                    verdict_data = judge_call(fixture_judge_prompt(case, answer, mode, workspace_diff), JUDGE_SCHEMA)
                    judge_elapsed += time.monotonic() - judge_started
                    verdict = next((item for item in verdict_data.get("results", []) if item.get("id") == case["id"]), None)
                    if verdict:
                        verdicts.append(verdict)
                judge_actual = getattr(judge_call, "last_metrics", {})
                passed = not candidate_error and sum(item.get("passed") is True for item in verdicts) >= judge_repetitions // 2 + 1
                diff_errors = check_required_diff(case["oracle_data"], workspace_diff) if mode != "baseline" and run.get("workspace") else []
                passed = passed and not diff_errors
                record = {
                    "case_id": case["id"], "mode": mode, "repetition": repetition,
                    "model": run["label"], "judge": judge["label"], "passed": passed,
                    "answer": answer, "judge_results": verdicts, "judge_quorum": judge_repetitions // 2 + 1, "diff_errors": diff_errors,
                    "candidate_error": candidate_error,
                    "workspace_diff": workspace_diff,
                    "metrics": {
                        "candidate": estimate_metrics(metric_prompt, str(answer.get("answer", "")), elapsed, pricing, run["label"], candidate_actual),
                        "judge": estimate_metrics("", json.dumps(verdict or {}, ensure_ascii=False), judge_elapsed, pricing, judge["label"], judge_actual),
                    },
                }
                records.append(record)
                if mode != "baseline" and not passed:
                    detail = "; ".join([*diff_errors, *([candidate_error] if candidate_error else [])])
                    errors.append(f"{case['id']} [{mode}, повтор {repetition}]: не пройдено. {detail}")
    for case in cases:
        for mode in ("skill", "catalog"):
            baseline = [item["passed"] for item in records if item["case_id"] == case["id"] and item["mode"] == "baseline"]
            current = [item["passed"] for item in records if item["case_id"] == case["id"] and item["mode"] == mode]
            if baseline and current and sum(current) / len(current) < sum(baseline) / len(baseline):
                errors.append(f"{case['id']} [{mode}]: качество ниже baseline.")
    return errors, records


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_revision(repo_root: Path) -> str | None:
    completed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    return completed.stdout.strip() if completed.returncode == 0 else None


def write_fixture_report(repo_root: Path, output: Path, records: list[dict[str, Any]], skill_dirs: list[Path], cases: list[dict[str, Any]], repetitions: int, judge_repetitions: int) -> Path:
    path = output if output.is_absolute() else repo_root / output
    if path.suffix.lower() != ".json":
        path.mkdir(parents=True, exist_ok=True)
        path = path / f"eval-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    by_mode: dict[str, dict[str, int | float]] = {}
    for record in records:
        bucket = by_mode.setdefault(record["mode"], {"runs": 0, "passed": 0})
        bucket["runs"] += 1
        bucket["passed"] += int(record["passed"])
    baseline_rate = 0.0
    if by_mode.get("baseline", {}).get("runs"):
        baseline_rate = by_mode["baseline"]["passed"] / by_mode["baseline"]["runs"]
    for mode, bucket in by_mode.items():
        bucket["pass_rate"] = round(bucket["passed"] / bucket["runs"], 4) if bucket["runs"] else 0.0
        bucket["delta_to_baseline"] = round(bucket["pass_rate"] - baseline_rate, 4)
    provenance = {
        "git_revision": git_revision(repo_root),
        "skills": {str(item.relative_to(repo_root)): sha256_file(item / "SKILL.md") for item in skill_dirs},
        "fixtures": {case["id"]: {"fixture": {str(file.relative_to(case["fixture_dir"])): sha256_file(file) for file in case["fixture_dir"].rglob("*") if file.is_file()}, "oracle_sha256": sha256_file(Path(case["fixture_dir"]).parent / case["oracle"])} for case in cases},
        "modes": ["baseline", "skill", "catalog"], "repetitions": repetitions, "judge_repetitions": judge_repetitions,
    }
    path.write_text(json.dumps({"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "provenance": provenance, "summary": by_mode, "runs": records}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def confirm_model_run(*, runs: list[dict[str, Any]], fixture_cases: list[dict[str, Any]], trigger_cases: list[dict[str, Any]], result_groups: list[tuple[Path, dict[str, Any], list[dict[str, Any]]]], repetitions: int, judge_repetitions: int, yes: bool) -> bool:
    run_count = len(runs)
    fixture_runs = run_count * len(fixture_cases) * 3 * repetitions
    catalog_selection_calls = run_count * len(fixture_cases) * repetitions
    trigger_calls = run_count * len({case["skill_name"] for case in trigger_cases})
    result_calls = run_count * sum(len(cases) for _, _, cases in result_groups)
    candidate_calls = fixture_runs + catalog_selection_calls + trigger_calls + result_calls
    judge_calls = fixture_runs * judge_repetitions + result_calls
    print(
        "Модельный прогон потребует не менее запросов: "
        f"к кандидату — {candidate_calls}, к судье — {judge_calls}. "
        "Точная стоимость зависит от локального "
        "адаптера и тарифов. После запуска она попадёт в отчёт.",
        flush=True,
    )
    if yes:
        print("Стоимость подтверждена флагом --yes.", flush=True)
        return True
    if not sys.stdin.isatty():
        print("Для запуска без терминала после просмотра оценки добавьте --yes.", file=sys.stderr)
        return False
    return input("Запустить модельный прогон? [y/N] ").strip().lower() in {"y", "yes", "д", "да"}


def main() -> int:
    args = parse_args()
    roots = args.paths or [Path.cwd()]
    repo_root = Path.cwd().resolve()
    case_ids = set(args.case_id)

    config = load_config(repo_root, args.config)
    if config is None:
        # Bootstrap или нехватка настроек уже сообщены. Это не дефект контроля
        # качества: модельные evals опциональны, поэтому выходим без ошибки.
        return 0

    skill_dirs = find_skill_dirs(roots)
    if not skill_dirs:
        print("Каталоги навыков не найдены.", file=sys.stderr)
        return 1

    all_trigger_cases = collect_trigger_cases(skill_dirs)
    all_result_groups = collect_result_groups(skill_dirs, 0)
    fixture_cases = load_fixture_cases(repo_root, args.fixture_registry)
    if case_ids:
        known_case_ids = {case["id"] for case in all_trigger_cases}
        known_case_ids.update(
            case["id"]
            for _, _, cases in all_result_groups
            for case in cases
        )
        known_case_ids.update(case["id"] for case in fixture_cases)
        missing = sorted(case_ids - known_case_ids)
        if missing:
            print(
                "Проверки с указанными id не найдены: " + ", ".join(missing),
                file=sys.stderr,
            )
            return 1

    trigger_cases = filter_trigger_cases(all_trigger_cases, case_ids)
    result_groups = filter_result_groups(
        all_result_groups if case_ids else collect_result_groups(skill_dirs, args.limit),
        case_ids,
    )

    if case_ids:
        fixture_cases = [case for case in fixture_cases if case["id"] in case_ids]
    repetitions = args.repetitions or config["repetitions"]
    if not confirm_model_run(runs=config["runs"], fixture_cases=fixture_cases, trigger_cases=trigger_cases, result_groups=result_groups, repetitions=repetitions, judge_repetitions=config["judge_repetitions"], yes=args.yes):
        print("Модельный прогон отменён до вызова моделей.", flush=True)
        return 0
    errors: list[str] = []
    fixture_records: list[dict[str, Any]] = []
    for run in config["runs"]:
        errors.extend(
            run_for_target(
                repo_root=repo_root,
                run=run,
                judge=config["judge"],
                timeout=config["timeout"],
                trigger_cases=trigger_cases,
                result_groups=result_groups,
            )
        )
        if fixture_cases:
            print(
                "Запускаю проверку на тестовых проектах: "
                f"{russian_count(len(fixture_cases), 'сценарий', 'сценария', 'сценариев')}, "
                f"{russian_count(repetitions, 'повтор', 'повтора', 'повторов')}, "
                "режимы без навыка, с навыком и через каталог.",
                flush=True,
            )
            fixture_errors, records = run_fixture_evals(
                repo_root=repo_root,
                cases=fixture_cases,
                skill_dirs=skill_dirs,
                run=run,
                judge=config["judge"],
                timeout=config["timeout"],
                repetitions=repetitions,
                judge_repetitions=config["judge_repetitions"],
                pricing=config["pricing"],
            )
            errors.extend(f"[{run['label']}] {error}" for error in fixture_errors)
            fixture_records.extend(records)

    if fixture_records:
        report_path = write_fixture_report(
            repo_root,
            args.output or Path(config["results_dir"]),
            fixture_records,
            skill_dirs,
            fixture_cases,
            repetitions,
            config["judge_repetitions"],
        )
        print(f"Отчёт проверки на тестовых проектах: {report_path.relative_to(repo_root)}", flush=True)

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        print(
            "Модельные проверки не пройдены: "
            f"{russian_count(len(errors), 'ошибка', 'ошибки', 'ошибок')}.",
            file=sys.stderr,
        )
        return 1

    print("\nМодельные проверки пройдены.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
