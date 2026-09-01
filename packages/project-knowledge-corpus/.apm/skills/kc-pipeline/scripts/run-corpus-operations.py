#!/usr/bin/env python3
"""Plan corpus work, run explicitly configured commands, and rebuild indexes.

The script supports the optional portable corpus layout. Project-specific
adapters remain project code: this controller only reads their declarative
commands and never invokes them unless --run-commands is given.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable

try:
    import yaml
except ImportError:  # pragma: no cover - depends on the target project environment.
    yaml = None


DEFAULT_NORMALIZED_ARTIFACTS = ("normalized.md", "message.md", "stenogram.txt")
QUEUE_ORDER = (
    "content_selection",
    "fetch",
    "transcribe",
    "normalize",
    "statements",
    "traceability",
    "semantic_review",
    "strong_review",
    "corroboration",
    "source_check",
    "concepts",
    "impact_audit",
    "apply_changes",
    "corpus_validation",
    "human_decision",
)
GLOBAL_STAGES = (
    "concepts",
    "impact_audit",
    "apply_changes",
    "corpus_validation",
)
PRIMARY_QUEUES = tuple(
    name for name in QUEUE_ORDER if name not in {*GLOBAL_STAGES, "human_decision"}
)
AUTOMATED_QUEUES = tuple(name for name in QUEUE_ORDER if name != "human_decision")
RUN_STATUSES = {
    "running",
    "paused_limit",
    "paused_resources",
    "waiting_external",
    "failed",
    "completed",
}
RUN_EXIT_CODES = {
    "completed": 0,
    "paused_limit": 10,
    "paused_resources": 11,
    "waiting_external": 20,
    "failed": 1,
}
BLOCKER_CODES = {
    "access_unavailable",
    "source_unavailable",
    "provenance_missing",
    "write_scope_violation",
    "storage_not_permitted",
    "publication_not_permitted",
    "credential_exposure",
    "conflicting_change",
    "validation_failed",
    "user_prohibited",
    "owner_decision_required",
}
ACCESS_BLOCKER_CODES = {"access_unavailable", "source_unavailable"}
BLOCKER_ACTIONS = {
    "access_unavailable": "Предоставить доступ или выбрать разрешённый маршрут получения.",
    "source_unavailable": "Указать доступный экземпляр источника или исключить его из области прохода.",
    "provenance_missing": "Подтвердить происхождение материала или запретить его использование.",
    "write_scope_violation": "Разрешить точную область записи или изменить исполнитель.",
    "storage_not_permitted": "Выбрать разрешённый способ хранения.",
    "publication_not_permitted": "Разрешить публикацию либо оставить материал во внутреннем слое.",
    "credential_exposure": "Удалить секрет из отслеживаемого слоя и заменить способ доступа.",
    "conflicting_change": "Выбрать способ совместить конфликтующие изменения.",
    "validation_failed": "Устранить ошибку проверки или принять документированное исключение.",
    "user_prohibited": "Изменить явный запрет пользователя или исключить действие.",
    "owner_decision_required": "Принять указанное решение владельца проекта.",
}
DEFAULT_MAX_ACTIVE_DECISION_GROUPS = 20
ADAPTER_STATUSES = {
    "synced",
    "partial",
    "changed",
    "unchanged",
    "new",
    "removed",
    "manual-required",
    "access-limited",
    "fetch-error",
    "unsupported-adapter",
    "invalid-registry",
}
SENSITIVE_SETTING_NAMES = {"token", "password", "cookie", "secret", "authorization", "api_key", "apikey"}


class OperationsError(RuntimeError):
    """The project operations contract or its observable state is invalid."""


def process_is_alive(pid: int) -> bool:
    """Return whether a locally recorded child process is still alive."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_started_ticks(pid: int) -> str | None:
    """Return the Linux process start tick when it is available."""
    try:
        payload = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = payload.rsplit(") ", maxsplit=1)[1].split()
        return fields[19]
    except (IndexError, OSError):
        return None


def active_process_matches(active: dict[str, Any]) -> bool:
    pid = active.get("pid")
    if not isinstance(pid, int) or not process_is_alive(pid):
        return False
    expected_ticks = active.get("process_started_ticks")
    if not isinstance(expected_ticks, str) or not expected_ticks:
        return False
    return process_started_ticks(pid) == expected_ticks


@dataclass(frozen=True)
class CorpusItem:
    source_id: str
    source_dir: Path
    source_card: dict[str, Any]
    index_item: dict[str, Any]
    item_dir: Path | None
    item_card: dict[str, Any] | None

    def value(self, key: str, default: Any = None) -> Any:
        if self.item_card is not None and key in self.item_card:
            return self.item_card[key]
        return self.index_item.get(key, default)

    @property
    def item_id(self) -> str:
        value = self.value("id")
        return value if isinstance(value, str) else "<unknown>"

    @property
    def stage(self) -> str:
        value = self.value("workflow_stage")
        return value if isinstance(value, str) else ""

    @property
    def storage_strategy(self) -> str:
        value = self.source_card.get("storage_strategy")
        return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class CorpusSource:
    source_id: str
    source_dir: Path
    card: dict[str, Any]

    @property
    def adapter(self) -> str:
        value = self.card.get("adapter")
        return value if isinstance(value, str) else ""

    @property
    def locator(self) -> str:
        value = self.card.get("locator", self.card.get("url", ""))
        return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    returncode: int
    changed_paths: tuple[str, ...]
    output: str


@dataclass(frozen=True)
class AdapterResult:
    source_id: str
    adapter: str
    status: str
    message: str
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class OperationalCheckResult:
    returncode: int
    contract_errors: tuple[str, ...]
    blockers: tuple[dict[str, Any], ...]
    quality_warnings: tuple[dict[str, Any], ...]
    suppressed: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class PipelineResult:
    status: str
    reason_code: str
    queues: dict[str, list[dict[str, str]]]
    command_results: tuple[CommandResult, ...]
    steps: int
    message: str
    completed_global_stages: tuple[str, ...] = ()
    resource_waiting: tuple[dict[str, str], ...] = ()


def require_yaml() -> None:
    if yaml is None:
        raise OperationsError("Для работы нужен пакет PyYAML.")


def load_yaml(path: Path) -> Any:
    require_yaml()
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise OperationsError(f"Не удалось прочитать {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise OperationsError(f"Файл YAML содержит ошибку: {path}: {exc}") from exc


def dump_yaml_atomically(path: Path, data: Any) -> None:
    require_yaml()
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        stream.write(rendered)
        temporary_path = Path(stream.name)
    try:
        os.replace(temporary_path, path)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise


def repo_relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise OperationsError(f"Путь выходит за пределы проекта: {path}") from exc


def resolve_inside(root: Path, raw_path: str, label: str) -> Path:
    candidate = (root / raw_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise OperationsError(f"{label} выходит за пределы проекта: {raw_path}") from exc
    return candidate


def relative_path(raw_path: str, label: str) -> PurePosixPath:
    path = PurePosixPath(raw_path)
    if path.is_absolute() or ".." in path.parts:
        raise OperationsError(f"{label} должен быть относительным путём внутри корпуса: {raw_path}")
    return path


def corpus_paths(corpus_root: Path) -> tuple[Path, Path, Path]:
    contract_path = corpus_root / "corpus.yml"
    catalog_path = corpus_root / "catalog.yml"
    if not contract_path.is_file() or not catalog_path.is_file():
        raise OperationsError("В корне корпуса нужны corpus.yml и catalog.yml.")
    contract = load_yaml(contract_path)
    if not isinstance(contract, dict):
        raise OperationsError("corpus.yml должен быть словарём YAML.")
    tracked_data = contract.get("tracked_data")
    if not isinstance(tracked_data, dict) or not isinstance(tracked_data.get("root"), str):
        raise OperationsError("corpus.yml должен задавать tracked_data.root.")
    return contract_path, catalog_path, corpus_root / tracked_data["root"]


def source_directories(corpus_root: Path) -> list[Path]:
    _, _, data_root = corpus_paths(corpus_root)
    if not data_root.exists():
        return []
    return sorted(path.parent for path in data_root.glob("*/source.yml"))


def load_sources(corpus_root: Path) -> list[CorpusSource]:
    sources: list[CorpusSource] = []
    for source_dir in source_directories(corpus_root):
        source = load_yaml(source_dir / "source.yml")
        if not isinstance(source, dict) or not isinstance(source.get("id"), str):
            raise OperationsError(f"Карточка источника должна задавать строковый id: {source_dir / 'source.yml'}")
        sources.append(CorpusSource(source["id"], source_dir, source))
    return sources


def load_items(corpus_root: Path) -> list[CorpusItem]:
    items: list[CorpusItem] = []
    for source_dir in source_directories(corpus_root):
        source = load_yaml(source_dir / "source.yml")
        if not isinstance(source, dict) or not isinstance(source.get("id"), str):
            continue
        source_items_path = source_dir / "items.yml"
        if not source_items_path.is_file():
            continue
        source_items = load_yaml(source_items_path)
        rows = source_items.get("items") if isinstance(source_items, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            item_dir: Path | None = None
            item_card: dict[str, Any] | None = None
            raw_item_path = row.get("path")
            if isinstance(raw_item_path, str):
                item_dir = source_dir / relative_path(raw_item_path, "path единицы")
                item_path = item_dir / "item.yml"
                if not item_path.is_file():
                    item_id = row.get("id") if isinstance(row.get("id"), str) else "<unknown>"
                    if item_dir.is_file():
                        raise OperationsError(
                            f"path единицы {source['id']}/{item_id} указывает на файл "
                            f"({raw_item_path}), а должен указывать на папку единицы, "
                            "содержащую item.yml."
                        )
                    raise OperationsError(
                        f"path единицы {source['id']}/{item_id} не находит item.yml: "
                        f"{item_path}"
                    )
                loaded = load_yaml(item_path)
                if not isinstance(loaded, dict):
                    raise OperationsError(f"item.yml должен быть словарём YAML: {item_path}")
                item_card = loaded
            items.append(CorpusItem(source["id"], source_dir, source, row, item_dir, item_card))
    return items


def load_operations(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    data = load_yaml(path)
    if not isinstance(data, dict):
        raise OperationsError("Файл настроек операций должен быть словарём YAML.")
    version = data.get("operations_version")
    if version != 1:
        raise OperationsError("Поддерживается только operations_version: 1.")
    reject_sensitive_settings(data)
    validate_operation_extensions(data)
    return data


def validate_operation_extensions(data: dict[str, Any]) -> None:
    attention = data.get("human_attention")
    if attention is not None:
        if not isinstance(attention, dict):
            raise OperationsError("human_attention должен быть словарём.")
        maximum = attention.get(
            "max_active_groups", DEFAULT_MAX_ACTIVE_DECISION_GROUPS
        )
        if (
            not isinstance(maximum, int)
            or isinstance(maximum, bool)
            or not 1 <= maximum <= 100
        ):
            raise OperationsError(
                "human_attention.max_active_groups должен быть целым числом от 1 до 100."
            )
    stages = data.get("stages")
    if stages is None:
        return
    if not isinstance(stages, dict):
        raise OperationsError("stages должен быть словарём.")
    for stage, settings in stages.items():
        if not isinstance(stage, str) or not isinstance(settings, dict):
            raise OperationsError("Каждая стадия должна иметь строковое имя и словарь настроек.")
        task_contract = settings.get("task_contract")
        if task_contract is not None and task_contract != "compound_media":
            raise OperationsError(
                f"stages.{stage}.task_contract поддерживает только compound_media."
            )
        resources = settings.get("resources")
        if resources is None:
            continue
        if not isinstance(resources, dict):
            raise OperationsError(f"stages.{stage}.resources должен быть словарём.")
        for name in ("min_free_disk_bytes", "estimated_peak_disk_bytes"):
            value = resources.get(name, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise OperationsError(
                    f"stages.{stage}.resources.{name} должен быть целым числом байтов."
                )


def reject_sensitive_settings(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.lower().replace("-", "_") in SENSITIVE_SETTING_NAMES:
                raise OperationsError(f"В настройках операций запрещено поле с секретом: {path}{key}")
            reject_sensitive_settings(child, f"{path}{key}.")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_sensitive_settings(child, f"{path}{index}.")
    elif isinstance(value, str) and ("?token=" in value.lower() or "authorization:" in value.lower()):
        raise OperationsError(f"В настройках операций найдено значение, похожее на секрет: {path.rstrip('.')}")


def normalized_artifacts(operations: dict[str, Any]) -> tuple[str, ...]:
    value = operations.get("normalized_artifacts")
    if value is None:
        return DEFAULT_NORMALIZED_ARTIFACTS
    if not isinstance(value, list) or not value or not all(isinstance(name, str) and name for name in value):
        raise OperationsError("normalized_artifacts должен быть непустым списком имён файлов.")
    return tuple(value)


def has_normalized_artifact(item: CorpusItem, names: tuple[str, ...]) -> bool:
    return bool(item.item_dir and any((item.item_dir / name).is_file() for name in names))


def has_raw_transcript(item: CorpusItem) -> bool:
    return bool(item.item_dir and (item.item_dir / "transcript.txt").is_file())


def has_statements(item: CorpusItem) -> bool:
    return bool(item.item_dir and (item.item_dir / "statements.yml").is_file())


def validate_access_escalation(
    blocker_code: Any,
    automatic_attempts: Any,
    subject: str,
) -> None:
    """Reject a one-shot access failure presented as a human blocker."""
    if blocker_code not in ACCESS_BLOCKER_CODES:
        return
    if not isinstance(automatic_attempts, list):
        raise OperationsError(
            f"{subject} с blocker_code={blocker_code} должен сохранять не менее двух автоматических попыток доступа."
        )
    attempts = {
        attempt.strip()
        for attempt in automatic_attempts
        if isinstance(attempt, str) and attempt.strip()
    }
    if len(attempts) < 2:
        raise OperationsError(
            f"{subject} с blocker_code={blocker_code} должен сохранять не менее двух разных автоматических попыток доступа."
        )


def statement_processing_tasks(item: CorpusItem, root: Path) -> list[tuple[str, dict[str, str]]]:
    if not item.item_dir or item.stage in {"blocked", "rejected"}:
        return []
    path = item.item_dir / "statements.yml"
    if not path.is_file():
        return []
    data = load_yaml(path)
    statements = data.get("statements") if isinstance(data, dict) else None
    if not isinstance(statements, list):
        return []
    tasks: list[tuple[str, dict[str, str]]] = []
    contract_version = data.get("statement_contract_version", 1) if isinstance(data, dict) else 1
    if contract_version not in {1, 2}:
        raise OperationsError(
            f"Неподдерживаемая statement_contract_version в {repo_relative(root, path)}: "
            f"{contract_version}"
        )
    for position, statement in enumerate(statements, start=1):
        if not isinstance(statement, dict):
            continue
        statement_id = statement.get("id")
        task_id = statement_id if isinstance(statement_id, str) else f"{item.item_id}#statement-{position}"
        base = {
            "id": task_id,
            "source_id": item.source_id,
            "path": repo_relative(root, path),
            "title": str(statement.get("text", "")),
        }
        escalation = {
            key: statement[key]
            for key in ("action_required", "automatic_attempts")
            if key in statement
        }
        if contract_version != 2:
            status = statement.get("status")
            if status == "candidate":
                tasks.append(
                    (
                        "semantic_review",
                        {
                            **base,
                            "reason": "устаревший status=candidate требует переноса в раздельную оценку",
                        },
                    )
                )
            elif status == "blocked":
                blocker_code = statement.get("blocker_code")
                if blocker_code not in BLOCKER_CODES:
                    raise OperationsError(
                        f"Заблокированное утверждение {task_id} должно задавать blocker_code."
                    )
                validate_access_escalation(
                    blocker_code, statement.get("automatic_attempts"), f"Заблокированное утверждение {task_id}"
                )
                tasks.append(
                    (
                        "human_decision",
                        {
                            **base,
                            **escalation,
                            "reason": "устаревший status=blocked требует конкретного решения или блокера",
                            "blocker_code": blocker_code,
                        },
                    )
                )
            continue
        processing = statement.get("processing_status")
        if not isinstance(processing, dict):
            tasks.append(("traceability", {**base, "reason": "нет processing_status"}))
            continue
        blocked_stages = [name for name, value in processing.items() if value == "blocked"]
        if blocked_stages:
            blocker_code = statement.get("blocker_code")
            if blocker_code not in BLOCKER_CODES:
                raise OperationsError(
                    f"Заблокированное утверждение {task_id} должно задавать blocker_code."
                )
            validate_access_escalation(
                blocker_code, statement.get("automatic_attempts"), f"Заблокированное утверждение {task_id}"
            )
            tasks.append(
                (
                    "human_decision",
                    {
                        **base,
                        **escalation,
                        "reason": f"заблокированы проверки: {', '.join(sorted(blocked_stages))}",
                        "blocker_code": blocker_code,
                    },
                )
            )
            continue
        if processing.get("extraction") != "complete" or processing.get("traceability") != "passed":
            tasks.append(("traceability", {**base, "reason": "извлечение или прослеживаемость не проверены"}))
            continue
        if processing.get("semantic_review") == "pending":
            tasks.append(("semantic_review", {**base, "reason": "смысловая проверка не завершена"}))
            continue
        if processing.get("semantic_review") == "failed":
            tasks.append(
                (
                    "strong_review",
                    {**base, "reason": "обычная смысловая проверка выявила спорный случай"},
                )
            )
            continue
        if processing.get("strong_review") not in {"not_required", "passed"}:
            tasks.append(("strong_review", {**base, "reason": "усиленная проверка не завершена"}))
            continue
        if processing.get("corroboration_check") != "complete":
            tasks.append(("corroboration", {**base, "reason": "сопоставление источников не завершено"}))
    return tasks


def queue_name(
    item: CorpusItem,
    normalized_names: tuple[str, ...],
) -> tuple[str, str, str | None] | None:
    stage = item.stage
    if stage == "needs_fetch":
        return "fetch", "workflow_stage=needs_fetch", None
    if stage == "indexed":
        processing_scope = item.value("processing_scope")
        if processing_scope in {"selected_fragments", "full", "full_redacted"}:
            return (
                "fetch",
                f"единица выбрана для точечного получения: processing_scope={processing_scope}",
                None,
            )
        if item.storage_strategy == "index_only":
            return None
        return (
            "content_selection",
            "проиндексированная единица ожидает содержательного отбора",
            None,
        )
    if stage == "needs_transcript":
        if has_statements(item):
            return "source_check", "утверждения уже есть, требуется сверка стадии", None
        if has_normalized_artifact(item, normalized_names):
            return "statements", "подготовленный артефакт уже есть", None
        if has_raw_transcript(item):
            return "normalize", "сырая расшифровка уже есть", None
        return "transcribe", "workflow_stage=needs_transcript", None
    if stage in {"fetched", "raw_transcribed"}:
        return "normalize", f"workflow_stage={stage}", None
    if stage == "normalized":
        return (
            "source_check",
            "утверждения уже есть, требуется сверка стадии",
            None,
        ) if has_statements(item) else (
            "statements",
            "материал нормализован, утверждения отсутствуют",
            None,
        )
    if stage == "statements_extracted":
        return "source_check", "workflow_stage=statements_extracted", None
    if stage == "blocked":
        blocker_code = item.value("blocker_code")
        if blocker_code not in BLOCKER_CODES:
            raise OperationsError(
                f"Заблокированная единица {item.item_id} должна задавать blocker_code."
            )
        validate_access_escalation(
            blocker_code, item.value("automatic_attempts"), f"Заблокированная единица {item.item_id}"
        )
        return "human_decision", "workflow_stage=blocked", blocker_code
    if stage in {"source_checked", "rejected", ""}:
        return None
    raise OperationsError(
        f"Единица {item.item_id} содержит неизвестную или неподдерживаемую стадию: {stage}"
    )


def empty_queues() -> dict[str, list[dict[str, str]]]:
    return {name: [] for name in QUEUE_ORDER}


def build_queues(items: list[CorpusItem], normalized_names: tuple[str, ...], root: Path) -> dict[str, list[dict[str, str]]]:
    queues = empty_queues()
    for item in items:
        statement_tasks = statement_processing_tasks(item, root)
        if statement_tasks:
            for name, task in statement_tasks:
                queues[name].append(task)
            continue
        result = queue_name(item, normalized_names)
        if result is None:
            continue
        name, reason, blocker_code = result
        relative_path = repo_relative(root, item.item_dir) if item.item_dir else ""
        task = {
            "id": item.item_id,
            "source_id": item.source_id,
            "path": relative_path,
            "title": str(item.value("title", "")),
            "reason": reason,
        }
        if blocker_code is not None:
            task["blocker_code"] = blocker_code
            action_required = item.value("action_required")
            automatic_attempts = item.value("automatic_attempts")
            if isinstance(action_required, str) and action_required:
                task["action_required"] = action_required
            if isinstance(automatic_attempts, list) and all(
                isinstance(attempt, str) and attempt for attempt in automatic_attempts
            ):
                task["automatic_attempts"] = automatic_attempts
        queues[name].append(task)
    return queues


def build_run_queues(
    corpus_root: Path,
    normalized_names: tuple[str, ...],
    root: Path,
    completed_global_stages: set[str],
) -> dict[str, list[dict[str, str]]]:
    queues = build_queues(load_items(corpus_root), normalized_names, root)
    for stage in GLOBAL_STAGES:
        if stage in completed_global_stages:
            continue
        queues[stage].append(
            {
                "id": f"global:{stage}",
                "source_id": "",
                "path": "",
                "title": stage,
                "reason": "обязательная глобальная стадия полного прохода не завершена",
            }
        )
    return queues


def available_task_count(queues: dict[str, list[dict[str, str]]]) -> int:
    return sum(len(queues[name]) for name in AUTOMATED_QUEUES)


def stage_fingerprint(queues: dict[str, list[dict[str, str]]], stage: str) -> str:
    return json.dumps(queues[stage], ensure_ascii=False, sort_keys=True)


def next_automated_queue(queues: dict[str, list[dict[str, str]]]) -> str | None:
    return next((name for name in AUTOMATED_QUEUES if queues[name]), None)


def index_paths(corpus_root: Path) -> tuple[Path, Path]:
    contract = load_yaml(corpus_root / "corpus.yml")
    indexes = contract.get("indexes") if isinstance(contract, dict) else None
    if not isinstance(indexes, dict):
        return corpus_root / "index" / "items.yml", corpus_root / "index" / "statements.yml"
    items = indexes.get("items", "index/items.yml")
    statements = indexes.get("statements", "index/statements.yml")
    if not isinstance(items, str) or not isinstance(statements, str):
        raise OperationsError("Пути indexes.items и indexes.statements должны быть строками.")
    return corpus_root / relative_path(items, "indexes.items"), corpus_root / relative_path(
        statements,
        "indexes.statements",
    )


def rebuild_indexes(corpus_root: Path, root: Path) -> tuple[int, int]:
    item_rows: list[dict[str, Any]] = []
    statement_rows: list[dict[str, Any]] = []
    seen_item_ids: set[str] = set()
    seen_statement_ids: set[str] = set()
    for item in load_items(corpus_root):
        item_id = item.index_item.get("id")
        if not isinstance(item_id, str):
            raise OperationsError("В индексе источника найдена единица без строкового id.")
        if item_id in seen_item_ids:
            raise OperationsError(f"Повторяющийся id единицы: {item_id}")
        seen_item_ids.add(item_id)
        path = repo_relative(corpus_root, item.item_dir) if item.item_dir else None
        item_rows.append(
            {
                "id": item_id,
                "source_id": item.source_id,
                "path": path,
                "title": item.index_item.get("title"),
                "date_published": item.index_item.get("date_published"),
                "workflow_stage": item.index_item.get("workflow_stage"),
                "access": item.index_item.get("access"),
            }
        )
        if not item.item_dir or not (item.item_dir / "statements.yml").is_file():
            continue
        data = load_yaml(item.item_dir / "statements.yml")
        statements = data.get("statements") if isinstance(data, dict) else None
        if not isinstance(statements, list):
            raise OperationsError(f"statements.yml должен содержать список statements: {item.item_id}")
        for statement in statements:
            if not isinstance(statement, dict) or not isinstance(statement.get("id"), str):
                raise OperationsError(f"В statements.yml найдена запись без строкового id: {item.item_id}")
            statement_id = statement["id"]
            if statement_id in seen_statement_ids:
                raise OperationsError(f"Повторяющийся id утверждения: {statement_id}")
            seen_statement_ids.add(statement_id)
            statement_rows.append(
                {
                    "id": statement_id,
                    "source_id": statement.get("source_id", item.source_id),
                    "item_id": statement.get("item_id", item.item_id),
                    "path": repo_relative(root, item.item_dir / "statements.yml"),
                    "status": statement.get("status"),
                    "kind": statement.get("kind"),
                    "text": statement.get("text"),
                    "artifact": statement.get("artifact"),
                    "checked_at": statement.get("checked_at"),
                    "processing_status": statement.get("processing_status"),
                    "source_role": statement.get("source_role"),
                    "evidence_strength": statement.get("evidence_strength"),
                    "confidence": statement.get("confidence"),
                    "temporal_status": statement.get("temporal_status"),
                    "corroboration": statement.get("corroboration"),
                    "limitations": statement.get("limitations"),
                }
            )
    items_path, statements_path = index_paths(corpus_root)
    dump_yaml_atomically(items_path, {"items": item_rows})
    dump_yaml_atomically(statements_path, {"statements": statement_rows})
    return len(item_rows), len(statement_rows)


def git_file_paths(root: Path, *arguments: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", *arguments],
        cwd=root,
        capture_output=True,
        text=False,
    )
    if result.returncode != 0:
        raise OperationsError("Для --run-commands проект должен быть рабочей областью Git.")
    paths: list[str] = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative = raw_path.decode("utf-8", errors="strict")
        relative_path(relative, "Путь файла Git")
        paths.append(relative)
    return paths


def git_file_fingerprints(root: Path) -> dict[str, str]:
    visible_paths = git_file_paths(root, "--cached", "--others", "--exclude-standard")
    ignored_paths = set(git_file_paths(root, "--others", "--ignored", "--exclude-standard"))
    fingerprints: dict[str, str] = {}
    for relative in visible_paths + sorted(ignored_paths):
        if relative == ".local" or relative.startswith(".local/"):
            continue
        path = root / relative
        try:
            path.parent.resolve().relative_to(root)
        except ValueError as exc:
            raise OperationsError(f"Родительский путь файла Git выходит из проекта: {relative}") from exc
        try:
            metadata = path.lstat()
            if path.is_symlink():
                payload = os.readlink(path).encode("utf-8", errors="surrogateescape")
                kind = b"symlink\0"
            elif path.is_file():
                if relative in ignored_paths:
                    payload = "\0".join(
                        str(value)
                        for value in (
                            metadata.st_dev,
                            metadata.st_ino,
                            metadata.st_nlink,
                            metadata.st_size,
                            metadata.st_mtime_ns,
                            metadata.st_ctime_ns,
                            getattr(metadata, "st_blocks", 0),
                        )
                    ).encode("ascii")
                    kind = b"ignored-file-metadata\0"
                else:
                    payload = path.read_bytes()
                    kind = b"file\0"
            else:
                continue
        except OSError as exc:
            raise OperationsError(f"Не удалось получить снимок файла {relative}: {exc}") from exc
        mode = str(metadata.st_mode).encode("ascii")
        fingerprints[relative] = hashlib.sha256(kind + mode + b"\0" + payload).hexdigest()
    return fingerprints


def changed_fingerprint_paths(before: dict[str, str], after: dict[str, str]) -> set[str]:
    return {
        path
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    }


def configured_commands(operations: dict[str, Any], stage: str) -> list[dict[str, Any]]:
    stages = operations.get("stages")
    if not isinstance(stages, dict):
        return []
    stage_data = stages.get(stage)
    if not isinstance(stage_data, dict):
        return []
    commands = stage_data.get("commands", [])
    if not isinstance(commands, list):
        raise OperationsError(f"stages.{stage}.commands должен быть списком.")
    return [command for command in commands if isinstance(command, dict)]


def stage_resource_reason(root: Path, operations: dict[str, Any], stage: str) -> str | None:
    stages = operations.get("stages")
    stage_data = stages.get(stage) if isinstance(stages, dict) else None
    resources = stage_data.get("resources") if isinstance(stage_data, dict) else None
    if resources is None:
        return None
    if not isinstance(resources, dict):
        raise OperationsError(f"stages.{stage}.resources должен быть словарём.")
    values: dict[str, int] = {}
    for name in ("min_free_disk_bytes", "estimated_peak_disk_bytes"):
        value = resources.get(name, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise OperationsError(f"stages.{stage}.resources.{name} должен быть целым числом байтов.")
        values[name] = value
    required = values["min_free_disk_bytes"] + values["estimated_peak_disk_bytes"]
    available = shutil.disk_usage(root).free
    if available < required:
        return f"disk_bytes_required={required}, disk_bytes_available={available}"
    return None


def runnable_queue(
    root: Path,
    operations: dict[str, Any],
    queues: dict[str, list[dict[str, str]]],
) -> tuple[str | None, tuple[dict[str, str], ...]]:
    pending_primary = [name for name in PRIMARY_QUEUES if queues[name]]
    candidates = pending_primary
    if not candidates:
        candidates = [name for name in GLOBAL_STAGES if queues[name]][:1]
    waiting: list[dict[str, str]] = []
    for stage in candidates:
        if not configured_commands(operations, stage):
            waiting.append({"queue": stage, "reason": "executor_not_configured"})
            continue
        reason = stage_resource_reason(root, operations, stage)
        if reason is not None:
            waiting.append({"queue": stage, "reason": reason})
            continue
        return stage, tuple(waiting)
    return None, tuple(waiting)


def command_paths_allowed(paths: set[str], allowed_prefixes: list[str]) -> bool:
    return all(any(path == prefix or path.startswith(f"{prefix}/") for prefix in allowed_prefixes) for path in paths)


def run_commands(
    root: Path,
    operations: dict[str, Any],
    stage: str,
    activity_callback: Callable[[dict[str, Any] | None], None] | None = None,
) -> list[CommandResult]:
    results: list[CommandResult] = []
    for position, command in enumerate(configured_commands(operations, stage), start=1):
        command_id = command.get("id")
        argv = command.get("argv")
        write_paths = command.get("write_paths")
        cwd = command.get("working_directory", ".")
        if not isinstance(command_id, str) or not command_id:
            raise OperationsError(f"Команда #{position} должна иметь непустой id.")
        if not isinstance(argv, list) or not argv or not all(isinstance(part, str) and part for part in argv):
            raise OperationsError(f"Команда {command_id} должна задавать непустой argv.")
        if not isinstance(write_paths, list) or not write_paths or not all(isinstance(path, str) and path for path in write_paths):
            raise OperationsError(f"Команда {command_id} должна задавать write_paths.")
        if not isinstance(cwd, str):
            raise OperationsError(f"Команда {command_id} должна задавать working_directory строкой.")
        for path in write_paths:
            resolve_inside(root, path, f"write_paths команды {command_id}")
        command_cwd = resolve_inside(root, cwd, f"working_directory команды {command_id}")
        before = git_file_fingerprints(root)
        started_at = datetime.now(UTC).isoformat()
        if activity_callback is not None:
            activity_callback(
                {
                    "pid": None,
                    "command_id": command_id,
                    "started_at": started_at,
                    "heartbeat_at": started_at,
                    "launch_state": "starting",
                }
            )
        with (
            tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stdout_stream,
            tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_stream,
        ):
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=command_cwd,
                    stdout=stdout_stream,
                    stderr=stderr_stream,
                    text=True,
                )
            except OSError as exc:
                if activity_callback is not None:
                    activity_callback(None)
                raise OperationsError(f"Не удалось запустить команду {command_id}: {exc}") from exc
            identity = {
                "pid": process.pid,
                "command_id": command_id,
                "started_at": started_at,
                "heartbeat_at": started_at,
                "process_started_ticks": process_started_ticks(process.pid),
            }
            if activity_callback is not None:
                activity_callback(identity)
            while process.poll() is None:
                if activity_callback is not None:
                    activity_callback({**identity, "heartbeat_at": datetime.now(UTC).isoformat()})
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    continue
            stdout_stream.seek(0)
            stderr_stream.seek(0)
            stdout = stdout_stream.read()
            stderr = stderr_stream.read()
            if activity_callback is not None:
                activity_callback(None)
        after = git_file_fingerprints(root)
        changed = changed_fingerprint_paths(before, after)
        if not command_paths_allowed(changed, write_paths):
            paths = ", ".join(sorted(changed)) or "нет"
            raise OperationsError(f"Команда {command_id} изменила файлы вне write_paths: {paths}")
        output = "\n".join(part for part in (stdout.strip(), stderr.strip()) if part)
        results.append(CommandResult(command_id, process.returncode, tuple(sorted(changed)), output))
        if process.returncode != 0 and command.get("required", True):
            break
    return results


def adapter_definitions(operations: dict[str, Any]) -> dict[str, dict[str, Any]]:
    adapters = operations.get("adapters", {})
    if not isinstance(adapters, dict):
        raise OperationsError("adapters должен быть словарём определений адаптеров.")
    definitions: dict[str, dict[str, Any]] = {}
    for name, definition in adapters.items():
        if not isinstance(name, str) or not name or not isinstance(definition, dict):
            raise OperationsError("Каждый адаптер должен иметь строковое имя и словарь настроек.")
        definitions[name] = definition
    return definitions


def format_adapter_argv(argv: list[str], source: CorpusSource, root: Path) -> list[str]:
    values = {
        "source_id": source.source_id,
        "source_dir": repo_relative(root, source.source_dir),
        "locator": source.locator,
    }
    try:
        return [part.format(**values) for part in argv]
    except KeyError as exc:
        raise OperationsError(f"В argv адаптера используется неизвестный параметр: {exc.args[0]}") from exc


def validate_adapter_result(data: Any, source: CorpusSource, changed_paths: set[str]) -> AdapterResult:
    if not isinstance(data, dict):
        raise OperationsError(f"Адаптер {source.adapter} источника {source.source_id} должен вернуть JSON-объект.")
    if data.get("contract_version") != 1:
        raise OperationsError(f"Адаптер {source.adapter} источника {source.source_id} вернул неподдерживаемую версию договора.")
    if data.get("source_id") != source.source_id or data.get("adapter") != source.adapter:
        raise OperationsError(f"Адаптер {source.adapter} вернул результат для другого источника.")
    status = data.get("status")
    message = data.get("message")
    if status not in ADAPTER_STATUSES or not isinstance(message, str) or not message:
        raise OperationsError(f"Адаптер {source.adapter} источника {source.source_id} вернул неполный статус.")
    artifacts = data.get("artifacts", [])
    if not isinstance(artifacts, list) or not all(isinstance(path, str) for path in artifacts):
        raise OperationsError(f"Адаптер {source.adapter} источника {source.source_id} вернул неверный список artifacts.")
    return AdapterResult(source.source_id, source.adapter, status, message, tuple(sorted(changed_paths)))


def run_adapters(root: Path, corpus_root: Path, operations: dict[str, Any], selected_ids: set[str]) -> list[AdapterResult]:
    definitions = adapter_definitions(operations)
    sources = load_sources(corpus_root)
    known_ids = {source.source_id for source in sources}
    unknown_ids = selected_ids - known_ids
    if unknown_ids:
        raise OperationsError(f"Не найден источник для --source: {', '.join(sorted(unknown_ids))}")
    results: list[AdapterResult] = []
    for source in sources:
        if selected_ids and source.source_id not in selected_ids:
            continue
        definition = definitions.get(source.adapter)
        if definition is None:
            results.append(AdapterResult(source.source_id, source.adapter, "unsupported-adapter", "Адаптер не зарегистрирован в настройках операций.", ()))
            continue
        argv = definition.get("argv")
        write_paths = definition.get("write_paths")
        cwd = definition.get("working_directory", ".")
        if not isinstance(argv, list) or not argv or not all(isinstance(part, str) and part for part in argv):
            raise OperationsError(f"Адаптер {source.adapter} должен задавать непустой argv.")
        if not isinstance(write_paths, list) or not write_paths or not all(isinstance(path, str) and path for path in write_paths):
            raise OperationsError(f"Адаптер {source.adapter} должен задавать write_paths.")
        if not isinstance(cwd, str):
            raise OperationsError(f"Адаптер {source.adapter} должен задавать working_directory строкой.")
        for path in write_paths:
            resolve_inside(root, path, f"write_paths адаптера {source.adapter}")
        command_cwd = resolve_inside(root, cwd, f"working_directory адаптера {source.adapter}")
        before = git_file_fingerprints(root)
        try:
            process = subprocess.run(
                format_adapter_argv(argv, source, root),
                cwd=command_cwd,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise OperationsError(
                f"Не удалось запустить адаптер {source.adapter} источника {source.source_id}: {exc}"
            ) from exc
        after = git_file_fingerprints(root)
        changed = changed_fingerprint_paths(before, after)
        if not command_paths_allowed(changed, write_paths):
            paths = ", ".join(sorted(changed)) or "нет"
            raise OperationsError(f"Адаптер {source.adapter} изменил файлы вне write_paths: {paths}")
        if process.returncode != 0:
            message = process.stderr.strip() or process.stdout.strip() or f"Команда завершилась с кодом {process.returncode}."
            results.append(AdapterResult(source.source_id, source.adapter, "fetch-error", message, tuple(sorted(changed))))
            continue
        try:
            result_data = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise OperationsError(f"Адаптер {source.adapter} источника {source.source_id} вернул не JSON: {exc.msg}") from exc
        results.append(validate_adapter_result(result_data, source, changed))
    return results


def run_operational_check(
    root: Path, corpus_root: Path, policy: Path | None
) -> OperationalCheckResult:
    validator = Path(__file__).resolve().parents[2] / "kc-inventory" / "scripts" / "validate-corpus-layout.py"
    argv = [sys.executable, str(validator), str(corpus_root), "--operational", "--output", "json"]
    if policy is not None:
        argv.extend(["--operational-policy", repo_relative(corpus_root, policy)])
    process = subprocess.run(argv, cwd=root, capture_output=True, text=True)
    try:
        data = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise OperationsError(f"Операционная проверка корпуса не вернула JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise OperationsError("Операционная проверка корпуса вернула неверный JSON.")
    def findings(name: str) -> tuple[dict[str, Any], ...]:
        value = data.get(name, [])
        if not isinstance(value, list) or not all(isinstance(entry, dict) for entry in value):
            raise OperationsError(f"Операционная проверка вернула неверное поле {name}.")
        return tuple(value)
    errors = data.get("contract_errors", [])
    if not isinstance(errors, list) or not all(isinstance(entry, str) for entry in errors):
        raise OperationsError("Операционная проверка вернула неверные ошибки договора.")
    blockers = findings("blockers")
    if any(finding.get("blocker_code") not in BLOCKER_CODES for finding in blockers):
        raise OperationsError("Операционная проверка вернула блокер вне закрытого перечня.")
    return OperationalCheckResult(process.returncode, tuple(errors), blockers, findings("quality_warnings"), findings("suppressed"))


def render_report(
    corpus_root: Path,
    queues: dict[str, list[dict[str, str]]],
    command_results: list[CommandResult],
    index_counts: tuple[int, int] | None,
    adapter_results: list[AdapterResult] | None = None,
    operational_check: OperationalCheckResult | None = None,
    run_state: dict[str, Any] | None = None,
) -> str:
    lines = [
        "# Операционный отчёт корпуса",
        "",
        f"Создан: {datetime.now(UTC).isoformat()}",
        f"Корень корпуса: {corpus_root}",
        "",
        "## Очереди",
        "",
    ]
    if run_state is not None:
        lines[5:5] = [
            "## Состояние прохода",
            "",
            f"- run_id: {run_state['run_id']}",
            f"- status: {run_state['status']}",
            f"- reason_code: {run_state['reason_code']}",
            f"- доступных задач: {run_state['available_task_count']}",
            f"- задач с внешним блокером: {run_state['blocked_task_count']}",
            f"- коды внешних блокеров: {', '.join(run_state.get('blocker_codes', [])) or 'нет'}",
            (
                "- активных групп решений: "
                f"{len(run_state.get('human_decision_groups', []))} "
                f"из {run_state.get('human_decision_group_count', 0)}"
            ),
            (
                "- групп за пределами бюджета внимания: "
                f"{run_state.get('human_decision_group_overflow', 0)}"
            ),
            (
                "- очередей в ожидании ресурсов: "
                f"{len(run_state.get('resource_waiting', []))}"
            ),
            (
                "- завершённые глобальные стадии: "
                f"{', '.join(run_state.get('completed_global_stages', [])) or 'нет'}"
            ),
            (
                "- активный исполнитель: "
                + (
                    f"{run_state['active_executor'].get('queue', 'неизвестная очередь')} "
                    f"({run_state['active_executor'].get('command_id', 'неизвестная команда')}, "
                    f"PID {run_state['active_executor'].get('pid', 'неизвестен')}, "
                    f"heartbeat {run_state['active_executor'].get('heartbeat_at', 'неизвестен')})"
                    if isinstance(run_state.get('active_executor'), dict)
                    else "нет"
                )
            ),
            "",
        ]
    for name in QUEUE_ORDER:
        entries = queues[name]
        lines.append(f"- {name}: {len(entries)}")
        for entry in entries:
            location = f" ({entry['path']})" if entry["path"] else ""
            blocker = (
                f", blocker_code={entry['blocker_code']}"
                if entry.get("blocker_code")
                else ""
            )
            lines.append(f"  - {entry['id']}{location}: {entry['reason']}{blocker}")
    if command_results:
        lines.extend(["", "## Команды", ""])
        for result in command_results:
            changed = ", ".join(result.changed_paths) or "нет"
            lines.append(f"- {result.command_id}: код {result.returncode}; изменено: {changed}")
    if adapter_results:
        lines.extend(["", "## Адаптеры", ""])
        for result in adapter_results:
            changed = ", ".join(result.changed_paths) or "нет"
            lines.append(f"- {result.source_id} ({result.adapter}): {result.status}; {result.message}; изменено: {changed}")
    if index_counts is not None:
        lines.extend(["", "## Индексы", "", f"- единиц: {index_counts[0]}", f"- утверждений: {index_counts[1]}"])
    if operational_check is not None:
        lines.extend(
            [
                "",
                "## Операционная проверка текущего состояния",
                "",
                f"- ошибки договора: {len(operational_check.contract_errors)}",
                f"- блокеры доступа: {len(operational_check.blockers)}",
                f"- предупреждения качества: {len(operational_check.quality_warnings)}",
                f"- подавлено правилом или метаданными: {len(operational_check.suppressed)}",
            ]
        )
        for finding in (*operational_check.blockers, *operational_check.quality_warnings)[:10]:
            blocker = (
                f", blocker_code={finding.get('blocker_code')}"
                if finding.get("blocker_code")
                else ""
            )
            lines.append(
                f"  - {finding.get('path')}:{finding.get('line')}: "
                f"{finding.get('kind')}{blocker}"
            )
        for error in operational_check.contract_errors:
            lines.append(f"  - ошибка договора: {error}")
    lines.extend(["", "## Продолжение", "", "Следующий запуск начинает с указанных очередей. Необработанная единица остаётся в своей стадии, пока проектная команда или человек не изменят её состояние.", ""])
    return "\n".join(lines)


def report_path(root: Path, operations: dict[str, Any], explicit: Path | None) -> Path | None:
    if explicit is not None:
        return resolve_inside(root, str(explicit), "Путь отчёта")
    report = operations.get("report")
    if not isinstance(report, dict) or not isinstance(report.get("path"), str):
        return None
    return resolve_inside(root, report["path"], "report.path")


def state_path(root: Path, operations: dict[str, Any], explicit: Path | None) -> Path:
    if explicit is not None:
        return resolve_inside(root, str(explicit), "Путь состояния прохода")
    run_state = operations.get("run_state")
    if isinstance(run_state, dict) and isinstance(run_state.get("path"), str):
        return resolve_inside(root, run_state["path"], "run_state.path")
    return resolve_inside(root, ".local/state/corpus-pipeline.json", "Путь состояния прохода")


def read_run_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationsError(f"Не удалось прочитать состояние прохода {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("contract_version") != 1:
        raise OperationsError(f"Состояние прохода {path} имеет неподдерживаемый договор.")
    if data.get("status") not in RUN_STATUSES:
        raise OperationsError(f"Состояние прохода {path} содержит неизвестный status.")
    if not isinstance(data.get("run_id"), str) or not data["run_id"]:
        raise OperationsError(f"Состояние прохода {path} не содержит строковый run_id.")
    if not isinstance(data.get("attempts"), int) or not isinstance(data.get("steps"), int):
        raise OperationsError(f"Состояние прохода {path} содержит неверные счётчики.")
    queues = data.get("queues")
    if isinstance(queues, dict):
        for stage in GLOBAL_STAGES:
            queues.setdefault(stage, [])
    if not isinstance(queues, dict) or any(
        not isinstance(queues.get(name), list)
        or any(not isinstance(entry, dict) for entry in queues[name])
        for name in QUEUE_ORDER
    ):
        raise OperationsError(f"Состояние прохода {path} содержит неполные очереди.")
    completed_global_stages = data.get("completed_global_stages", [])
    if (
        not isinstance(completed_global_stages, list)
        or not all(stage in GLOBAL_STAGES for stage in completed_global_stages)
        or len(set(completed_global_stages)) != len(completed_global_stages)
    ):
        raise OperationsError(
            f"Состояние прохода {path} содержит неверные глобальные стадии."
        )
    return data


def reconcile_interrupted_run_state(
    path: Path,
    previous: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Turn an orphaned running state into a resumable pause before a new run."""
    if previous is None or previous.get("status") != "running":
        return previous
    active = previous.get("active_executor")
    if isinstance(active, dict) and active_process_matches(active):
        raise OperationsError(
            "В сохранённом состоянии указан живой исполнитель. Дождитесь его завершения, "
            "чтобы не создать дублирующий запуск."
        )
    reason_code = "executor_interrupted"
    message = (
        "Предыдущий исполнитель исчез без итоговой записи. Проход поставлен на "
        "возобновляемую паузу, очередь сохранена без заявления о работе."
    )
    if isinstance(active, dict) and (
        not isinstance(active.get("pid"), int)
        or (
            process_is_alive(active["pid"])
            and (
                not isinstance(active.get("process_started_ticks"), str)
                or process_started_ticks(active["pid"]) is None
            )
        )
    ):
        reason_code = "executor_identity_unknown"
        message = (
            "Контроллер был прерван в момент запуска команды, до фиксации PID. "
            "Автоматическое продолжение запрещено, чтобы не создать дублирующий запуск."
        )
    recovered = {
        **previous,
        "status": "failed" if reason_code == "executor_identity_unknown" else "paused_limit",
        "reason_code": reason_code,
        "updated_at": datetime.now(UTC).isoformat(),
        "active_executor": None,
        "message": message,
    }
    write_run_state(path, recovered)
    return recovered


def write_run_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        temporary_path = Path(stream.name)
    try:
        os.replace(temporary_path, path)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise


@contextmanager
def run_state_lock(path: Path) -> Any:
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        stream = lock_path.open("a+", encoding="utf-8")
    except OSError as exc:
        raise OperationsError(f"Не удалось открыть блокировку прохода {lock_path}: {exc}") from exc
    try:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OperationsError(
                f"Проход с состоянием {path} уже выполняется другим процессом."
            ) from exc
        yield
    finally:
        stream.close()


def blocker_codes(queues: dict[str, list[dict[str, str]]]) -> list[str]:
    return sorted(
        {
            entry["blocker_code"]
            for entry in queues["human_decision"]
            if entry.get("blocker_code") in BLOCKER_CODES
        }
    )


def max_active_decision_groups(operations: dict[str, Any]) -> int:
    settings = operations.get("human_attention")
    if settings is None:
        return DEFAULT_MAX_ACTIVE_DECISION_GROUPS
    if not isinstance(settings, dict):
        raise OperationsError("human_attention должен быть словарём.")
    value = settings.get("max_active_groups", DEFAULT_MAX_ACTIVE_DECISION_GROUPS)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 100:
        raise OperationsError("human_attention.max_active_groups должен быть целым числом от 1 до 100.")
    return value


def decision_groups(
    queues: dict[str, list[dict[str, str]]],
    operations: dict[str, Any],
) -> tuple[list[dict[str, Any]], int, int]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in queues["human_decision"]:
        blocker_code = entry.get("blocker_code", "owner_decision_required")
        reason = entry.get("reason", "")
        action_required = entry.get("action_required")
        if not isinstance(action_required, str) or not action_required:
            action_required = BLOCKER_ACTIONS.get(
                blocker_code, "Принять решение, недоступное автоматическому исполнителю."
            )
        decision_material = f"{action_required}\n{reason}"
        key = (blocker_code, decision_material)
        group = grouped.setdefault(
            key,
            {
                "decision_key": (
                    f"{blocker_code}:"
                    f"{hashlib.sha256(decision_material.encode('utf-8')).hexdigest()[:12]}"
                ),
                "blocker_code": blocker_code,
                "action_required": action_required,
                "reason": reason,
                "affected_count": 0,
                "automatic_attempts": [],
                "examples": [],
            },
        )
        group["affected_count"] += 1
        attempts = entry.get("automatic_attempts")
        if isinstance(attempts, list):
            for attempt in attempts:
                if (
                    isinstance(attempt, str)
                    and attempt
                    and attempt not in group["automatic_attempts"]
                ):
                    group["automatic_attempts"].append(attempt)
        if len(group["examples"]) < 3:
            group["examples"].append(
                {
                    "id": entry.get("id", ""),
                    "path": entry.get("path", ""),
                }
            )
    groups = [grouped[key] for key in sorted(grouped)]
    maximum = max_active_decision_groups(operations)
    return groups[:maximum], len(groups), max(0, len(groups) - maximum)


def start_run_state(
    previous: dict[str, Any] | None,
    queues: dict[str, list[dict[str, str]]],
    attempt_started_at: str,
    operations: dict[str, Any],
) -> dict[str, Any]:
    resumable = previous is not None and previous.get("status") != "completed"
    completed_global_stages = (
        list(previous.get("completed_global_stages", [])) if resumable else []
    )
    active_groups, group_count, overflow = decision_groups(queues, operations)
    return {
        "contract_version": 1,
        "run_id": previous["run_id"] if resumable else str(uuid.uuid4()),
        "status": "running",
        "reason_code": "attempt_started",
        "started_at": previous.get("started_at", attempt_started_at) if resumable else attempt_started_at,
        "updated_at": attempt_started_at,
        "completed_at": None,
        "attempts": int(previous.get("attempts", 0)) + 1 if resumable else 1,
        "steps": int(previous.get("steps", 0)) if resumable else 0,
        "available_task_count": available_task_count(queues),
        "blocked_task_count": len(queues["human_decision"]),
        "blocker_codes": blocker_codes(queues),
        "human_decision_groups": active_groups,
        "human_decision_group_count": group_count,
        "human_decision_group_overflow": overflow,
        "completed_global_stages": completed_global_stages,
        "resource_waiting": [],
        "active_executor": None,
        "queues": queues,
        "message": "Попытка автономного прохода начата.",
    }


def finish_run_state(
    running: dict[str, Any],
    result: PipelineResult,
    operations: dict[str, Any],
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    active_groups, group_count, overflow = decision_groups(result.queues, operations)
    return {
        **running,
        "status": result.status,
        "reason_code": result.reason_code,
        "updated_at": now,
        "completed_at": now if result.status == "completed" else None,
        "steps": int(running.get("steps", 0)) + result.steps,
        "available_task_count": available_task_count(result.queues),
        "blocked_task_count": len(result.queues["human_decision"]),
        "blocker_codes": blocker_codes(result.queues),
        "human_decision_groups": active_groups,
        "human_decision_group_count": group_count,
        "human_decision_group_overflow": overflow,
        "completed_global_stages": list(result.completed_global_stages),
        "resource_waiting": list(result.resource_waiting),
        "active_executor": None,
        "queues": result.queues,
        "message": result.message,
    }


def run_pipeline(
    root: Path,
    corpus_root: Path,
    operations: dict[str, Any],
    max_steps: int | None,
    completed_global_stages: set[str],
    activity_callback: Callable[[dict[str, Any] | None, dict[str, list[dict[str, str]]]], None] | None = None,
) -> PipelineResult:
    normalized_names = normalized_artifacts(operations)
    queues = build_run_queues(
        corpus_root,
        normalized_names,
        root,
        completed_global_stages,
    )
    results: list[CommandResult] = []
    steps = 0
    resource_waiting: dict[str, str] = {}

    def completed_stages() -> tuple[str, ...]:
        return tuple(stage for stage in GLOBAL_STAGES if stage in completed_global_stages)

    while True:
        if (
            max_steps is not None
            and steps >= max_steps
            and any(queues[name] for name in AUTOMATED_QUEUES)
        ):
            return PipelineResult(
                "paused_limit",
                "step_limit_reached",
                queues,
                tuple(results),
                steps,
                "Лимит попытки исчерпан. Проход не завершён и будет продолжен из сохранённой очереди.",
                completed_stages(),
                tuple(
                    {"queue": name, "reason": reason}
                    for name, reason in sorted(resource_waiting.items())
                    if queues[name]
                ),
            )
        queue, waiting = runnable_queue(root, operations, queues)
        resource_waiting.update(
            {entry["queue"]: entry["reason"] for entry in waiting}
        )
        if queue is not None:
            resource_waiting.pop(queue, None)
        if queue is None:
            automatic_tail = any(queues[name] for name in AUTOMATED_QUEUES)
            if automatic_tail:
                waiting_entries = tuple(
                    {"queue": name, "reason": reason}
                    for name, reason in sorted(resource_waiting.items())
                    if queues[name]
                )
                return PipelineResult(
                    "paused_resources",
                    "no_runnable_automatic_task",
                    queues,
                    tuple(results),
                    steps,
                    "Автоматический хвост остался, но ни одна готовая очередь сейчас не исполнима.",
                    completed_stages(),
                    waiting_entries,
                )
            if queues["human_decision"]:
                return PipelineResult(
                    "waiting_external",
                    "external_blockers_remaining",
                    queues,
                    tuple(results),
                    steps,
                    "Доступная работа исчерпана. Проход ждёт перечисленных внешних решений.",
                    completed_stages(),
                    tuple(
                        {"queue": name, "reason": reason}
                        for name, reason in sorted(resource_waiting.items())
                        if queues[name]
                    ),
                )
            return PipelineResult(
                "completed",
                "all_queues_empty",
                queues,
                tuple(results),
                steps,
                "Все очереди прохода пусты.",
                completed_stages(),
                (),
            )
        before = stage_fingerprint(queues, queue)
        try:
            stage_results = run_commands(
                root,
                operations,
                queue,
                (
                    lambda activity: activity_callback(
                        {**activity, "queue": queue} if activity is not None else None,
                        queues,
                    )
                    if activity_callback is not None
                    else None
                ),
            )
        except OperationsError as exc:
            queues = build_run_queues(
                corpus_root,
                normalized_names,
                root,
                completed_global_stages,
            )
            return PipelineResult(
                "failed",
                "execution_contract_error",
                queues,
                tuple(results),
                steps,
                f"Исполнитель очереди {queue} нарушил договор операций: {exc}",
                completed_stages(),
            )
        results.extend(stage_results)
        steps += 1
        if any(result.returncode != 0 for result in stage_results):
            queues = build_run_queues(
                corpus_root,
                normalized_names,
                root,
                completed_global_stages,
            )
            return PipelineResult(
                "failed",
                "stage_command_failed",
                queues,
                tuple(results),
                steps,
                f"Исполнитель очереди {queue} завершился с ошибкой.",
                completed_stages(),
            )
        if queue in GLOBAL_STAGES:
            completed_global_stages.add(queue)
        queues = build_run_queues(
            corpus_root,
            normalized_names,
            root,
            completed_global_stages,
        )
        if activity_callback is not None:
            activity_callback(None, queues)
        if stage_fingerprint(queues, queue) == before:
            return PipelineResult(
                "failed",
                "no_progress",
                queues,
                tuple(results),
                steps,
                f"Исполнитель очереди {queue} не изменил машиночитаемую очередь.",
                completed_stages(),
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Спланировать или выполнить операции переносимого корпуса знаний.")
    parser.add_argument("corpus", type=Path, help="Корень корпуса с corpus.yml.")
    parser.add_argument("--operations", type=Path, help="Необязательный файл настроек операций.")
    parser.add_argument("--stage", default="source_sync", help="Стадия проектных команд для --run-commands.")
    parser.add_argument("--run-commands", action="store_true", help="Явно выполнить команды указанной стадии.")
    parser.add_argument("--run-adapters", action="store_true", help="Явно выполнить зарегистрированные адаптеры источников.")
    parser.add_argument("--source", action="append", default=[], help="Идентификатор источника для --run-adapters; можно повторять.")
    parser.add_argument("--rebuild-indexes", action="store_true", help="Атомарно пересобрать производные индексы.")
    parser.add_argument(
        "--run-pipeline",
        action="store_true",
        help="Продолжать автономный проход по очередям до терминального состояния.",
    )
    parser.add_argument(
        "--reconcile-state",
        action="store_true",
        help="Сверить сохранённое running-состояние с живым исполнителем без запуска очереди.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        help="Ограничить число стадий в одной попытке, сохранив проход незавершённым.",
    )
    parser.add_argument("--state", type=Path, help="Репо-относительный путь состояния прохода.")
    parser.add_argument(
        "--operational-check",
        action="store_true",
        help="Запустить переносимую проверку tracked-слоя и добавить безопасную сводку в отчёт.",
    )
    parser.add_argument(
        "--operational-policy",
        type=Path,
        help="Репо-относительный YAML-файл правил подавления для --operational-check.",
    )
    parser.add_argument("--report", type=Path, help="Репо-относительный путь локального отчёта.")
    parser.add_argument(
        "--write-report",
        action="store_true",
        help="Записать отчёт по пути report.path из настроек операций.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path.cwd().resolve()
    corpus_root = resolve_inside(root, str(args.corpus), "Корень корпуса")
    corpus_paths(corpus_root)
    operations_path = resolve_inside(root, str(args.operations), "Файл настроек операций") if args.operations else None
    operations = load_operations(operations_path)
    items: list[CorpusItem] = []
    queues = empty_queues()
    command_results: list[CommandResult] = []
    adapter_results: list[AdapterResult] = []
    operational_check: OperationalCheckResult | None = None
    if args.max_steps is not None and args.max_steps < 1:
        raise OperationsError("--max-steps должен быть положительным числом.")
    if args.max_steps is not None and not args.run_pipeline:
        raise OperationsError("--max-steps требует --run-pipeline.")
    if args.run_pipeline and (args.run_commands or args.run_adapters):
        raise OperationsError("--run-pipeline нельзя совмещать с запуском одной стадии или адаптеров.")
    if args.reconcile_state and (args.run_pipeline or args.run_commands or args.run_adapters):
        raise OperationsError("--reconcile-state нельзя совмещать с запуском операций.")
    if args.operational_policy and not (args.operational_check or args.run_pipeline):
        raise OperationsError("--operational-policy требует --operational-check или --run-pipeline.")
    if not args.run_pipeline:
        items = load_items(corpus_root)
        queues = build_run_queues(
            corpus_root,
            normalized_artifacts(operations),
            root,
            set(),
        )
    if args.operational_check and not args.run_pipeline:
        policy = resolve_inside(root, str(args.operational_policy), "Файл правил операционной проверки") if args.operational_policy else None
        operational_check = run_operational_check(root, corpus_root, policy)
        if operational_check.returncode:
            report = render_report(corpus_root, queues, command_results, None, adapter_results, operational_check)
            destination = report_path(root, operations, args.report) if args.write_report or args.report else None
            if destination:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(report, encoding="utf-8")
                print(f"Отчёт записан: {repo_relative(root, destination)}")
            else:
                print(report)
            return 1
    if args.run_pipeline:
        if not operations_path:
            raise OperationsError("Для --run-pipeline нужен параметр --operations.")
        destination_state = state_path(root, operations, args.state)
        policy = resolve_inside(root, str(args.operational_policy), "Файл правил операционной проверки") if args.operational_policy else None
        with run_state_lock(destination_state):
            attempt_started_at = datetime.now(UTC).isoformat()
            previous_state = read_run_state(destination_state)
            previous_state = reconcile_interrupted_run_state(destination_state, previous_state)
            if (
                previous_state is not None
                and previous_state.get("reason_code") == "executor_identity_unknown"
            ):
                raise OperationsError(
                    "Нельзя автоматически продолжить проход без надёжной идентичности "
                    "предыдущего исполнителя. Сначала подтвердите отсутствие его последствий."
                )
            resumable = previous_state is not None and previous_state.get("status") != "completed"
            completed_global_stages = set(
                previous_state.get("completed_global_stages", []) if resumable else []
            )
            initial_queues = (
                previous_state["queues"] if previous_state is not None else empty_queues()
            )
            running_state = start_run_state(
                previous_state, initial_queues, attempt_started_at, operations
            )
            write_run_state(destination_state, running_state)
            try:
                operational_check = run_operational_check(root, corpus_root, policy)
            except OperationsError as exc:
                operational_check = OperationalCheckResult(
                    1,
                    (f"Не удалось выполнить предзапусковую проверку: {exc}",),
                    (),
                    (),
                    (),
                )
            if not operational_check.contract_errors:
                initial_queues = build_run_queues(
                    corpus_root,
                    normalized_artifacts(operations),
                    root,
                    completed_global_stages,
                )
                running_state = {
                    **running_state,
                    "available_task_count": available_task_count(initial_queues),
                    "blocked_task_count": len(initial_queues["human_decision"]),
                    "blocker_codes": blocker_codes(initial_queues),
                    "queues": initial_queues,
                }
                write_run_state(destination_state, running_state)
            if operational_check.returncode:
                pipeline_result = PipelineResult(
                    "failed",
                    "preflight_failed",
                    initial_queues,
                    (),
                    0,
                    "Предзапусковая проверка обнаружила ошибки договора или блокеры публикации.",
                    tuple(
                        stage
                        for stage in GLOBAL_STAGES
                        if stage in completed_global_stages
                    ),
                )
            else:
                try:
                    def persist_activity(
                        activity: dict[str, Any] | None,
                        current_queues: dict[str, list[dict[str, str]]],
                    ) -> None:
                        running_state["active_executor"] = activity
                        running_state["queues"] = current_queues
                        running_state["available_task_count"] = available_task_count(current_queues)
                        running_state["blocked_task_count"] = len(current_queues["human_decision"])
                        running_state["blocker_codes"] = blocker_codes(current_queues)
                        running_state["updated_at"] = datetime.now(UTC).isoformat()
                        write_run_state(destination_state, running_state)

                    pipeline_result = run_pipeline(
                        root,
                        corpus_root,
                        operations,
                        args.max_steps,
                        completed_global_stages,
                        persist_activity,
                    )
                except OperationsError as exc:
                    current_queues = build_run_queues(
                        corpus_root,
                        normalized_artifacts(operations),
                        root,
                        completed_global_stages,
                    )
                    pipeline_result = PipelineResult(
                        "failed",
                        "execution_contract_error",
                        current_queues,
                        (),
                        0,
                        f"Исполнитель нарушил договор операций: {exc}",
                        tuple(
                            stage
                            for stage in GLOBAL_STAGES
                            if stage in completed_global_stages
                        ),
                    )
                if pipeline_result.status == "completed":
                    try:
                        postflight = run_operational_check(root, corpus_root, policy)
                    except OperationsError as exc:
                        postflight = OperationalCheckResult(
                            1,
                            (f"Не удалось выполнить итоговую проверку: {exc}",),
                            (),
                            (),
                            (),
                        )
                    operational_check = postflight
                    if postflight.returncode:
                        pipeline_result = PipelineResult(
                            "failed",
                            "postflight_failed",
                            pipeline_result.queues,
                            pipeline_result.command_results,
                            pipeline_result.steps,
                            "Итоговая проверка обнаружила ошибки договора или блокеры публикации.",
                            pipeline_result.completed_global_stages,
                        )
            run_state = finish_run_state(running_state, pipeline_result, operations)
            write_run_state(destination_state, run_state)
        report = render_report(
            corpus_root,
            pipeline_result.queues,
            list(pipeline_result.command_results),
            None,
            [],
            operational_check,
            run_state,
        )
        destination = report_path(root, operations, args.report) if args.write_report or args.report else None
        if destination:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(report, encoding="utf-8")
            print(f"Отчёт записан: {repo_relative(root, destination)}")
        else:
            print(report)
        print(f"Состояние прохода записано: {repo_relative(root, destination_state)}")
        return RUN_EXIT_CODES[pipeline_result.status]
    if args.reconcile_state:
        if not operations_path:
            raise OperationsError("Для --reconcile-state нужен параметр --operations.")
        destination_state = state_path(root, operations, args.state)
        with run_state_lock(destination_state):
            state = reconcile_interrupted_run_state(destination_state, read_run_state(destination_state))
        if state is None:
            print("Состояние прохода ещё не создавалось.")
            return 0
        print(render_report(corpus_root, state["queues"], [], None, [], None, state))
        print(f"Состояние прохода записано: {repo_relative(root, destination_state)}")
        return RUN_EXIT_CODES[state["status"]]
    if args.run_commands:
        if not operations_path:
            raise OperationsError("Для --run-commands нужен параметр --operations.")
        command_results = run_commands(root, operations, args.stage)
        if any(result.returncode != 0 for result in command_results):
            print(render_report(corpus_root, queues, command_results, None, adapter_results))
            return 1
        items = load_items(corpus_root)
        queues = build_run_queues(
            corpus_root,
            normalized_artifacts(operations),
            root,
            set(),
        )
    if args.run_adapters:
        if not operations_path:
            raise OperationsError("Для --run-adapters нужен параметр --operations.")
        adapter_results = run_adapters(root, corpus_root, operations, set(args.source))
        items = load_items(corpus_root)
        queues = build_run_queues(
            corpus_root,
            normalized_artifacts(operations),
            root,
            set(),
        )
    index_counts = rebuild_indexes(corpus_root, root) if args.rebuild_indexes else None
    report = render_report(corpus_root, queues, command_results, index_counts, adapter_results, operational_check)
    destination = report_path(root, operations, args.report) if args.write_report or args.report else None
    if destination:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(report, encoding="utf-8")
        print(f"Отчёт записан: {repo_relative(root, destination)}")
    else:
        print(report)
    return 1 if operational_check is not None and operational_check.returncode else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OperationsError as exc:
        print(f"Ошибка операций корпуса: {exc}", file=sys.stderr)
        raise SystemExit(2)
