#!/usr/bin/env python3
"""Plan corpus work, run explicitly configured commands, and rebuild indexes.

The script supports the optional portable corpus layout. Project-specific
adapters remain project code: this controller only reads their declarative
commands and never invokes them unless --run-commands is given.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
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
    "coverage_gap",
    "traceability",
    "semantic_review",
    "strong_review",
    "corroboration",
    "source_check",
    "verification",
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
    "awaiting_agent_task",
    "waiting_external",
    "failed",
    "completed",
}
RUN_EXIT_CODES = {
    "completed": 0,
    "paused_limit": 10,
    "paused_resources": 11,
    "awaiting_agent_task": 12,
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
ADAPTER_OPERATIONS = {"probe", "index", "fetch", "verify", "authorize"}
AGENT_ADAPTER = "agent"
LEGACY_MANUAL_ADAPTER = "manual"
TRANSFER_POLICIES = {"apply_now", "propose_only"}
DEFAULT_TECHNICAL_INDEX_FIELDS = {
    "content_hash",
    "hash",
    "hash_algorithm",
    "etag",
    "format",
    "size",
    "size_bytes",
    "byte_size",
    "content_length",
    "metadata_hash",
    "index_hash",
    "updated_at",
    "last_checked_at",
    "retrieved_at",
    "path",
    "workflow_stage",
    "processing_scope",
}
PROCESSING_INDEX_FIELDS = {
    "path",
    "workflow_stage",
    "processing_scope",
    "checked_by",
    "retrieved_at",
    "last_checked_at",
}
REMOVED_PUBLICATION_STATUSES = {"removed", "unpublished", "archived", "withdrawn"}
ALLOWED_IMPACT_SURFACES = {
    "requirements",
    "decisions",
    "skills_and_rules",
    "code",
    "configuration",
    "data",
    "tests_and_model_scenarios",
    "build",
    "delivery",
    "user_documentation",
}
ADAPTER_PROBE_STATUSES = {
    "ready",
    "profile-missing",
    "interactive-login-required",
    "permission-denied",
    "terms-decision-required",
    "technical-unavailable",
    "unsupported-adapter",
}
ADAPTER_VERIFY_STATUSES = {
    "verified",
    "partially-verified",
    "unverified",
    "mismatch",
    "access-limited",
    "fetch-error",
}
ADAPTER_AUTHORIZE_STATUSES = {
    "ready",
    "interactive-login-required",
    "permission-denied",
    "technical-unavailable",
}
ADAPTER_SUCCESS_STATUSES = {
    "probe": {"ready"},
    "index": {"synced", "partial", "changed", "unchanged", "no_change", "new", "removed"},
    "fetch": {"synced", "partial", "changed", "unchanged", "new", "removed"},
    "verify": {"verified", "partially-verified", "unverified"},
    "authorize": {"ready"},
}
NEGATIVE_SELECTION_VALUES = {
    "rejected",
    "irrelevant",
    "not_relevant",
    "non_relevant",
    "excluded",
    "not_selected",
}
SENSITIVE_SETTING_NAMES = {"token", "password", "cookie", "secret", "authorization", "api_key", "apikey"}
SENSITIVE_OUTPUT_PATTERN = re.compile(
    r"(?i)\b(token|password|secret|cookie|authorization|api[_-]?key)\b\s*[:=]\s*\S+"
)


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

    @property
    def contract_version(self) -> int:
        value = self.value("item_contract_version", 1)
        if value not in {1, 2}:
            raise OperationsError(
                f"Единица {self.item_id} содержит неподдерживаемую item_contract_version: {value}"
            )
        return value


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

    @property
    def profile_name(self) -> str:
        requirements = self.card.get("access_requirements")
        value = requirements.get("profile_name") if isinstance(requirements, dict) else ""
        return value if isinstance(value, str) else ""

    @property
    def agent_route(self) -> dict[str, Any] | None:
        value = self.card.get("agent_route")
        return value if isinstance(value, dict) else None


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    returncode: int
    changed_paths: tuple[str, ...]
    output: str
    executor: str = "trusted_project_executor"


@dataclass(frozen=True)
class AdapterResult:
    source_id: str
    adapter: str
    operation: str
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
    index_sync: dict[str, Any] | None = None
    owner_summary: dict[str, Any] | None = None
    transfer_evidence: tuple[dict[str, Any], ...] = ()


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
    transfer_policy = data.get("transfer_policy")
    if transfer_policy is not None:
        if not isinstance(transfer_policy, dict):
            raise OperationsError("transfer_policy должен быть словарём.")
        mode = transfer_policy.get("mode", "apply_now")
        if mode not in TRANSFER_POLICIES:
            raise OperationsError(
                "transfer_policy.mode должен быть apply_now или propose_only."
            )
    impact_report = data.get("impact_report")
    if impact_report is not None:
        if not isinstance(impact_report, dict) or not isinstance(impact_report.get("path"), str):
            raise OperationsError("impact_report должен задавать строковый path.")
        report_path = relative_path(impact_report["path"], "impact_report.path")
        if ".local" not in report_path.parts and not any(
            part.endswith(".local") for part in report_path.parts
        ):
            raise OperationsError("impact_report.path должен находиться в локальном *.local слое.")
    profiles = data.get("access_profiles")
    if profiles is not None:
        if not isinstance(profiles, dict) or not isinstance(profiles.get("path"), str):
            raise OperationsError("access_profiles должен задавать строковый path.")
        profile_path = relative_path(profiles["path"], "access_profiles.path")
        if ".local" not in profile_path.parts and not any(
            part.endswith(".local") for part in profile_path.parts
        ):
            raise OperationsError("access_profiles.path должен находиться в локальном *.local слое.")
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


def record_is_negatively_selected(record: dict[str, Any]) -> bool:
    if record.get("rejected") is True:
        return True
    for field in ("selection_status", "relevance", "selection_result", "selection"):
        value = record.get(field)
        if isinstance(value, str) and value.strip().lower() in NEGATIVE_SELECTION_VALUES:
            return True
    return isinstance(record.get("status"), str) and record["status"].strip().lower() in NEGATIVE_SELECTION_VALUES


def unit_eligibility(record: dict[str, Any] | None) -> tuple[bool, str]:
    """The single admission gate for source units at every content boundary."""
    if not isinstance(record, dict):
        return False, "missing"
    if record_is_negatively_selected(record):
        return False, "rejected_or_irrelevant"
    return True, "eligible"


def item_is_negatively_selected(item: CorpusItem) -> bool:
    values = {
        key: item.value(key)
        for key in ("rejected", "selection_status", "relevance", "selection_result", "selection", "status")
    }
    return not unit_eligibility(values)[0]


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
    if not item.item_dir or item.stage in {"blocked", "rejected"} or item_is_negatively_selected(item):
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
    if item_is_negatively_selected(item):
        return None
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
        reason = (
            "нужно определить и записать проверку происхождения снимка"
            if item.contract_version == 2
            else "workflow_stage=statements_extracted"
        )
        return "source_check", reason, None
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
    if stage == "verification_assessed":
        if item.contract_version != 2:
            raise OperationsError(
                f"Единица {item.item_id} использует verification_assessed без item_contract_version: 2"
            )
        return None
    if stage == "source_checked" and item.contract_version == 2:
        raise OperationsError(
            f"Единица {item.item_id} договора версии 2 должна использовать verification_assessed."
        )
    if stage == "source_checked":
        if item.item_dir is None or not (item.item_dir / "verification.yml").is_file():
            return "verification", "внешняя сверка и актуальность снимка не записаны", None
        return None
    if stage in {"rejected", ""}:
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


def coverage_gap_tasks(
    corpus_root: Path, root: Path
) -> list[tuple[str, dict[str, str]]]:
    """Build coverage_gap/human_decision tasks from long-source coverage maps.

    Only sources with long_source: true and an existing source-map.yml are
    inspected. Structure units without a matching coverage.units entry, with
    a status outside the closed set, or postponed without a valid blocker
    code become coverage_gap tasks. Postponed units with a valid blocker code
    escalate to human_decision, mirroring the item.yml/statements.yml
    escalation path.
    """
    tasks: list[tuple[str, dict[str, str]]] = []
    for source in load_sources(corpus_root):
        if source.card.get("long_source") is not True:
            continue
        source_map_path = source.source_dir / "source-map.yml"
        if not source_map_path.is_file():
            continue
        data = load_yaml(source_map_path)
        if not isinstance(data, dict):
            raise OperationsError(f"source-map.yml должен быть словарём YAML: {source_map_path}")
        structure = data.get("structure")
        structure_units = structure.get("units") if isinstance(structure, dict) else None
        if not isinstance(structure_units, list):
            continue
        expected_ids = [
            unit["id"]
            for unit in structure_units
            if isinstance(unit, dict) and isinstance(unit.get("id"), str)
        ]
        coverage = data.get("coverage")
        coverage_units = coverage.get("units") if isinstance(coverage, dict) else None
        coverage_by_id: dict[str, dict[str, Any]] = {}
        if isinstance(coverage_units, list):
            for unit in coverage_units:
                if isinstance(unit, dict) and isinstance(unit.get("unit_id"), str):
                    coverage_by_id[unit["unit_id"]] = unit
        relative_map_path = repo_relative(root, source_map_path)
        for unit_id in expected_ids:
            task_id = f"{source.source_id}:{unit_id}"
            title = f"{source.source_id}: {unit_id}"
            record = coverage_by_id.get(unit_id)
            if record is None:
                tasks.append(
                    (
                        "coverage_gap",
                        {
                            "id": task_id,
                            "source_id": source.source_id,
                            "path": relative_map_path,
                            "title": title,
                            "reason": "структурная единица отсутствует в coverage.units",
                        },
                    )
                )
                continue
            status = record.get("status")
            if status not in {"extracted", "no_significant_content", "postponed"}:
                tasks.append(
                    (
                        "coverage_gap",
                        {
                            "id": task_id,
                            "source_id": source.source_id,
                            "path": relative_map_path,
                            "title": title,
                            "reason": f"недопустимый статус охвата: {status!r}",
                        },
                    )
                )
                continue
            if status != "postponed":
                continue
            blocker_code = record.get("blocker_code")
            if blocker_code not in BLOCKER_CODES:
                tasks.append(
                    (
                        "coverage_gap",
                        {
                            "id": task_id,
                            "source_id": source.source_id,
                            "path": relative_map_path,
                            "title": title,
                            "reason": "статус postponed без кода блокера из закрытого перечня",
                        },
                    )
                )
                continue
            validate_access_escalation(
                blocker_code,
                record.get("automatic_attempts"),
                f"Отложенная структурная единица {task_id}",
            )
            escalated = {
                "id": task_id,
                "source_id": source.source_id,
                "path": relative_map_path,
                "title": title,
                "reason": "структурная единица отложена с кодом блокера, требуется решение владельца",
                "blocker_code": blocker_code,
            }
            action_required = record.get("action_required")
            automatic_attempts = record.get("automatic_attempts")
            if isinstance(action_required, str) and action_required:
                escalated["action_required"] = action_required
            if isinstance(automatic_attempts, list) and all(
                isinstance(attempt, str) and attempt for attempt in automatic_attempts
            ):
                escalated["automatic_attempts"] = automatic_attempts
            tasks.append(("human_decision", escalated))
    return tasks


def build_run_queues(
    corpus_root: Path,
    normalized_names: tuple[str, ...],
    root: Path,
    completed_global_stages: set[str],
) -> dict[str, list[dict[str, str]]]:
    queues = build_queues(load_items(corpus_root), normalized_names, root)
    for name, task in coverage_gap_tasks(corpus_root, root):
        queues[name].append(task)
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


def canonical_data(value: Any) -> Any:
    """Make YAML values safe and stable for persisted JSON snapshots."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError) as exc:
        raise OperationsError(f"Не удалось нормализовать данные индекса: {exc}") from exc


def source_index_snapshot(corpus_root: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """Read the provider index without treating processing state as source content."""
    snapshot: dict[str, dict[str, dict[str, Any]]] = {}
    for source in load_sources(corpus_root):
        path = source.source_dir / "items.yml"
        if not path.is_file():
            continue
        data = load_yaml(path)
        rows = data.get("items") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            continue
        source_rows: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            if row["id"] in source_rows:
                raise OperationsError(
                    f"Источник {source.source_id} содержит повторяющийся id единицы: {row['id']}"
                )
            record = canonical_data(row)
            item_path = row.get("path")
            if isinstance(item_path, str):
                item_card_path = source.source_dir / relative_path(item_path, "path единицы") / "item.yml"
                if item_card_path.is_file():
                    item_card = load_yaml(item_card_path)
                    if isinstance(item_card, dict):
                        record["_item_card"] = canonical_data(item_card)
            source_rows[row["id"]] = record
        snapshot[source.source_id] = source_rows
    return snapshot


def source_change_policy(source: CorpusSource) -> set[str]:
    policy = source.card.get("change_policy")
    if policy is None:
        return set(DEFAULT_TECHNICAL_INDEX_FIELDS)
    if not isinstance(policy, dict):
        raise OperationsError(f"change_policy источника {source.source_id} должен быть словарём.")
    fields = policy.get("technical_fields", [])
    if not isinstance(fields, list) or not all(isinstance(field, str) and field for field in fields):
        raise OperationsError(
            f"change_policy.technical_fields источника {source.source_id} должен быть списком строк."
        )
    return set(DEFAULT_TECHNICAL_INDEX_FIELDS) | set(fields)


def flatten_values(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        flattened: dict[str, Any] = {}
        for key, child in value.items():
            key_prefix = f"{prefix}.{key}" if prefix else str(key)
            flattened.update(flatten_values(child, key_prefix))
        return flattened
    if isinstance(value, list):
        return {prefix: canonical_data(value)}
    return {prefix: canonical_data(value)}


def item_is_unpublished(record: dict[str, Any]) -> bool:
    published = record.get("published")
    if published is False:
        return True
    status = record.get("publication_status", record.get("status"))
    return isinstance(status, str) and status.lower() in REMOVED_PUBLICATION_STATUSES


def source_index_delta(
    source: CorpusSource,
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    technical_fields = source_change_policy(source)
    added: list[str] = []
    changed: list[str] = []
    unchanged: list[str] = []
    removed: list[str] = []
    technical_only: list[str] = []
    rejected: list[str] = []
    for item_id in sorted(set(before) | set(after)):
        previous = before.get(item_id)
        current = after.get(item_id)
        if current is None or item_is_unpublished(current):
            if previous is not None:
                removed.append(item_id)
            continue
        if not unit_eligibility(current)[0]:
            rejected.append(item_id)
        if previous is None:
            added.append(item_id)
            continue
        previous_flat = flatten_values(previous)
        current_flat = flatten_values(current)
        differences = {
            key
            for key in previous_flat.keys() | current_flat.keys()
            if previous_flat.get(key) != current_flat.get(key)
        }
        if not differences:
            unchanged.append(item_id)
            continue
        changed.append(item_id)
        if all(key.rsplit(".", 1)[-1] in technical_fields for key in differences):
            technical_only.append(item_id)
    return {
        "source_id": source.source_id,
        "added": added,
        "changed": changed,
        "removed": removed,
        "unchanged": unchanged,
        "technical_only": technical_only,
        "rejected": rejected,
        "counts": {
            "added": len(added),
            "changed": len(changed),
            "removed": len(removed),
            "unchanged": len(unchanged),
            "technical_only": len(technical_only),
            "rejected": len(rejected),
        },
    }


def aggregate_index_delta(deltas: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {name: 0 for name in ("added", "changed", "removed", "unchanged", "technical_only", "rejected")}
    for delta in deltas:
        for name in counts:
            counts[name] += int(delta.get("counts", {}).get(name, 0))
    return {"sources": deltas, "counts": counts}


def statement_snapshot(corpus_root: Path) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    for item in load_items(corpus_root):
        if not item.item_dir or item_is_negatively_selected(item):
            continue
        path = item.item_dir / "statements.yml"
        if not path.is_file():
            continue
        data = load_yaml(path)
        statements = data.get("statements") if isinstance(data, dict) else None
        if not isinstance(statements, list):
            continue
        for statement in statements:
            if isinstance(statement, dict) and isinstance(statement.get("id"), str):
                snapshot[statement["id"]] = canonical_data(statement)
    return snapshot


def statement_delta(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(
        statement_id
        for statement_id in set(before) & set(after)
        if before[statement_id] != after[statement_id]
    )
    obsolete = sorted(
        statement_id
        for statement_id, statement in after.items()
        if statement.get("status") in {"obsolete", "superseded", "rejected"}
    )
    unverified = sorted(
        statement_id
        for statement_id, statement in after.items()
        if statement.get("status") in {"unverified", "candidate"}
        or (
            isinstance(statement.get("processing_status"), dict)
            and any(
                value in {"pending", "failed", "blocked"}
                for value in statement["processing_status"].values()
            )
        )
        or statement.get("evidence_strength") in {"weak", "unknown"}
    )
    return {
        "added": added,
        "changed": changed,
        "obsolete": obsolete,
        "unverified": unverified,
        "counts": {
            "added": len(added),
            "changed": len(changed),
            "obsolete": len(obsolete),
            "unverified": len(unverified),
        },
        "approximate": False,
    }


def effective_transfer_policy(operations: dict[str, Any], override: str | None = None) -> str:
    if override is not None:
        if override not in TRANSFER_POLICIES:
            raise OperationsError(f"Неизвестная политика переноса: {override}")
        return override
    policy = operations.get("transfer_policy")
    if not isinstance(policy, dict):
        return "apply_now"
    mode = policy.get("mode", "apply_now")
    if mode not in TRANSFER_POLICIES:
        raise OperationsError("transfer_policy.mode должен быть apply_now или propose_only.")
    return mode


def impact_report_path(root: Path, operations: dict[str, Any]) -> Path | None:
    report = operations.get("impact_report")
    if not isinstance(report, dict) or not isinstance(report.get("path"), str):
        return None
    return resolve_inside(root, report["path"], "impact_report.path")


def surface_path_contract(operations: dict[str, Any]) -> dict[str, list[str]]:
    """Read consumer-owned surface -> path mappings without guessing them."""
    mapping = operations.get("surface_paths")
    if not isinstance(mapping, dict) or not mapping:
        raise OperationsError(
            "Для переноса нужен surface_paths: проект-потребитель должен явно сопоставить поверхности с путями."
        )
    validated: dict[str, list[str]] = {}
    for surface, patterns in mapping.items():
        if surface not in ALLOWED_IMPACT_SURFACES:
            raise OperationsError(f"surface_paths содержит неизвестную поверхность: {surface!r}.")
        if not isinstance(patterns, list) or not patterns:
            raise OperationsError(f"surface_paths.{surface} должен быть непустым списком путей или шаблонов.")
        checked: list[str] = []
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern or pattern in {".", "*", "**", "/"}:
                raise OperationsError(f"surface_paths.{surface} содержит пустую или чрезмерно широкую область.")
            relative_path(pattern.replace("*", "safe"), f"surface_paths.{surface}")
            checked.append(pattern.rstrip("/"))
        validated[surface] = checked
    return validated


def path_matches_surface(path: str, patterns: list[str]) -> bool:
    candidate = PurePosixPath(path)
    return any(
        candidate.match(pattern)
        or ("*" not in pattern and (path == pattern or path.startswith(f"{pattern}/")))
        for pattern in patterns
    )


def finding_path_surfaces(
    finding: dict[str, Any], operations: dict[str, Any]
) -> dict[str, str]:
    """Make the finding -> surface -> target path chain explicit and checkable."""
    targets = finding.get("target_paths", [])
    if not targets:
        return {}
    contract = surface_path_contract(operations)
    declared = finding.get("target_surfaces")
    if declared is None and len(finding["affected_surfaces"]) == 1:
        declared = {path: finding["affected_surfaces"][0] for path in targets}
    if not isinstance(declared, dict) or set(declared) != set(targets):
        raise OperationsError(
            f"Находка {finding['id']} должна однозначно сопоставить каждый target_path с affected_surface."
        )
    result: dict[str, str] = {}
    for path, surface in declared.items():
        if not isinstance(surface, str) or surface not in finding["affected_surfaces"]:
            raise OperationsError(f"Находка {finding['id']} содержит target_path без объявленной поверхности.")
        relative_path(path, f"target_paths находки {finding['id']}")
        if not path_matches_surface(path, contract.get(surface, [])):
            raise OperationsError(
                f"target_path {path} находки {finding['id']} не разрешён поверхностью {surface}."
            )
        result[path] = surface
    return result


def validate_finding_source_units(
    corpus_root: Path,
    finding: dict[str, Any],
    index_sync: dict[str, Any] | None,
) -> None:
    """Apply the same eligibility gate to impact, proposals and product changes."""
    delta = finding.get("source_delta")
    if (
        not isinstance(delta, dict)
        or not isinstance(delta.get("source_id"), str)
        or not isinstance(delta.get("categories"), list)
        or not delta["categories"]
        or not isinstance(delta.get("unit_ids"), list)
        or not delta["unit_ids"]
        or not all(isinstance(value, str) and value for value in delta["unit_ids"])
    ):
        raise OperationsError(f"Находка {finding['id']} должна ссылаться на устойчивые unit_ids текущей дельты.")
    source_delta = next(
        (
            entry for entry in (index_sync or {}).get("delta", {}).get("sources", [])
            if isinstance(entry, dict) and entry.get("source_id") == delta["source_id"]
        ),
        None,
    )
    if source_delta is None:
        raise OperationsError(f"Находка {finding['id']} не связана с дельтой источника.")
    category_ids = {
        unit_id for category in delta["categories"]
        for unit_id in source_delta.get(category, []) if isinstance(unit_id, str)
    }
    if not set(delta["unit_ids"]) <= category_ids:
        raise OperationsError(f"Находка {finding['id']} ссылается на единицы вне текущей дельты источника.")
    current = source_index_snapshot(corpus_root).get(delta["source_id"], {})
    for unit_id in delta["unit_ids"]:
        eligible, reason = unit_eligibility(current.get(unit_id))
        if not eligible:
            raise OperationsError(
                f"Находка {finding['id']} использует недопустимую единицу {unit_id}: {reason}."
            )


def validate_impact_findings(
    root: Path,
    corpus_root: Path,
    operations: dict[str, Any],
    findings: list[dict[str, Any]],
    index_sync: dict[str, Any] | None,
) -> None:
    for finding in findings:
        validate_finding_source_units(corpus_root, finding, index_sync)
        if finding["status"] in {"apply_now", "owner_decision"}:
            try:
                finding["path_surfaces"] = finding_path_surfaces(finding, operations)
            except OperationsError as exc:
                # A proposal remains useful when a consumer has not yet mapped a
                # product surface.  It is deliberately not made eligible for a
                # later apply: the missing mapping is part of its evidence.
                if (
                    effective_transfer_policy(operations) == "propose_only"
                    and finding["status"] == "apply_now"
                ):
                    finding["path_surfaces"] = {}
                    finding["apply_status"] = "needs_configuration"
                    finding["configuration_gap"] = str(exc)
                else:
                    raise


def normalize_impact_finding(finding: dict[str, Any]) -> dict[str, Any]:
    status = finding.get("status")
    if status not in {"apply_now", "owner_decision", "no_change"}:
        raise OperationsError(f"Отчёт влияния содержит неизвестный статус находки: {status!r}.")
    source = finding.get("source") or finding.get("source_id")
    basis = finding.get("basis") or finding.get("evidence")
    surfaces = finding.get("affected_surfaces", finding.get("surfaces", []))
    if isinstance(surfaces, str):
        surfaces = [surfaces]
    if not isinstance(surfaces, list) or not all(isinstance(surface, str) and surface for surface in surfaces):
        raise OperationsError("Каждая находка влияния должна задавать список affected_surfaces.")
    unknown_surfaces = set(surfaces) - ALLOWED_IMPACT_SURFACES
    if unknown_surfaces:
        raise OperationsError(
            "Отчёт влияния содержит неизвестные поверхности: " + ", ".join(sorted(unknown_surfaces))
        )
    normalized = {
        "id": finding.get("id", finding.get("finding_id", "")),
        "status": status,
        "basis": basis,
        "source": source,
        "affected_surfaces": surfaces,
        "expected_result": finding.get("expected_result", ""),
        "recommended_change": finding.get("recommended_change", finding.get("recommendation", "")),
        "decision_required": finding.get("decision_required", finding.get("question", "")),
        "no_change_reason": finding.get("no_change_reason", finding.get("reason", "")),
        "source_delta": finding.get("source_delta"),
        "target_paths": finding.get("target_paths", finding.get("expected_paths", [])),
        "target_surfaces": finding.get("target_surfaces"),
        "expected_hashes": finding.get("expected_hashes", {}),
        "change_set": finding.get("change_set"),
    }
    if not all(
        isinstance(normalized[field], str) and normalized[field].strip()
        for field in ("basis", "source")
    ):
        raise OperationsError("Каждая находка должна содержать отдельные source и basis.")
    if status == "apply_now" and not all(
        isinstance(normalized[field], str) and normalized[field].strip()
        for field in ("expected_result", "recommended_change")
    ):
        raise OperationsError("Находка apply_now должна содержать expected_result и recommended_change.")
    if status == "owner_decision" and not (
        isinstance(normalized["decision_required"], str) and normalized["decision_required"].strip()
    ):
        raise OperationsError("Находка owner_decision должна содержать decision_required или question.")
    if status == "no_change":
        if not all(
            isinstance(normalized[field], str) and normalized[field].strip()
            for field in ("no_change_reason",)
        ):
            raise OperationsError(
                "Находка no_change должна содержать конкретную причину отсутствия изменения."
            )
        if not surfaces:
            raise OperationsError("Находка no_change должна содержать затронутую поверхность.")
    delta = normalized["source_delta"]
    if status in {"apply_now", "owner_decision", "no_change"}:
        if (
            not isinstance(delta, dict)
            or not isinstance(delta.get("source_id"), str)
            or not isinstance(delta.get("categories"), list)
            or not delta["categories"]
            or not isinstance(delta.get("unit_ids"), list)
            or not delta["unit_ids"]
            or not all(isinstance(item, str) and item for item in delta["unit_ids"])
        ):
            raise OperationsError(
                "Каждая находка должна связать source_delta с устойчивыми unit_ids текущей дельты."
            )
    if status == "apply_now":
        targets = normalized["target_paths"]
        hashes = normalized["expected_hashes"]
        if (
            not isinstance(delta, dict)
            or not isinstance(delta.get("source_id"), str)
            or not isinstance(delta.get("categories"), list)
            or not delta["categories"]
            or not isinstance(delta.get("unit_ids"), list)
            or not delta["unit_ids"]
            or not all(isinstance(item, str) and item for item in delta["unit_ids"])
            or not isinstance(targets, list)
            or not targets
            or not all(isinstance(path, str) and path for path in targets)
            or not isinstance(hashes, dict)
            or not all(isinstance(path, str) and isinstance(value, str) and value for path, value in hashes.items())
        ):
            raise OperationsError(
                "Находка apply_now должна связать source_delta с unit_ids и target_paths; "
                "expected_hashes нужны для идемпотентного результата."
            )
    if not isinstance(normalized["id"], str) or not normalized["id"]:
        raise OperationsError("Каждая находка влияния должна содержать непустой id.")
    return normalized


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def controlled_path_parts(raw_path: str, label: str) -> tuple[str, ...]:
    """Validate a path before any filesystem call can resolve a link."""
    path = relative_path(raw_path, label)
    if not path.parts or path.parts == (".",):
        raise OperationsError(f"{label} не должен указывать на корень проекта.")
    return path.parts


def safe_parent_fd(root: Path, raw_path: str, label: str) -> tuple[int, str]:
    """Open the parent through directory descriptors without following links."""
    parts = controlled_path_parts(raw_path, label)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(root, flags)
    except OSError as exc:
        raise OperationsError(f"Невозможно безопасно открыть корень проекта: {exc}") from exc
    try:
        for component in parts[:-1]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                raise OperationsError(
                    f"{label} содержит отсутствующий или символьный компонент пути: {raw_path}"
                ) from exc
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1]
    except Exception:
        os.close(descriptor)
        raise


def safe_file_bytes(root: Path, raw_path: str, label: str) -> bytes | None:
    parent_fd, leaf = safe_parent_fd(root, raw_path, label)
    try:
        try:
            descriptor = os.open(leaf, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise OperationsError(f"{label} нельзя безопасно прочитать: {raw_path}") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise OperationsError(f"{label} не является отдельным обычным файлом: {raw_path}")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def safe_replace_bytes(root: Path, raw_path: str, data: bytes, label: str) -> None:
    """Atomically replace one regular file, never dereferencing a symlink."""
    parent_fd, leaf = safe_parent_fd(root, raw_path, label)
    temporary = f".kc-apply-{uuid.uuid4().hex}.tmp"
    try:
        existing = safe_file_bytes(root, raw_path, label)
        # ``safe_file_bytes`` has checked a pre-existing final entry.  The
        # descriptor walk above is repeated for the write so a temporary link
        # replacement cannot redirect the actual output.
        _ = existing
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        try:
            offset = 0
            while offset < len(data):
                offset += os.write(descriptor, data[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except Exception:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(parent_fd)


def safe_delete_file(root: Path, raw_path: str, label: str) -> None:
    if safe_file_bytes(root, raw_path, label) is None:
        raise OperationsError(f"{label} не найден для удаления: {raw_path}")
    parent_fd, leaf = safe_parent_fd(root, raw_path, label)
    try:
        os.unlink(leaf, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def normalize_change_set(finding: dict[str, Any]) -> list[dict[str, Any]]:
    change_set = finding.get("change_set")
    if not isinstance(change_set, list) or not change_set:
        raise OperationsError(
            f"Находка {finding['id']} должна содержать непустой декларативный change_set; "
            "командный исполнитель apply_now небезопасен."
        )
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, operation in enumerate(change_set, start=1):
        if not isinstance(operation, dict):
            raise OperationsError(f"change_set находки {finding['id']} содержит несловари.")
        kind = operation.get("op")
        path = operation.get("path")
        before_hash = operation.get("before_sha256")
        after_hash = operation.get("after_sha256")
        if kind not in {"create", "update", "delete"} or not isinstance(path, str):
            raise OperationsError(f"change_set #{position} находки {finding['id']} имеет неверную операцию или путь.")
        controlled_path_parts(path, f"change_set #{position}")
        if path in seen or path not in finding["target_paths"]:
            raise OperationsError(f"change_set находки {finding['id']} содержит неразрешённый или повторный путь: {path}")
        seen.add(path)
        if not isinstance(before_hash, str) or not before_hash:
            raise OperationsError(f"change_set #{position} должен задавать before_sha256.")
        content: bytes | None = None
        if kind == "delete":
            if after_hash != "absent" or "content_base64" in operation:
                raise OperationsError(f"delete в change_set #{position} должен задавать after_sha256: absent.")
        else:
            encoded = operation.get("content_base64")
            if not isinstance(encoded, str) or not isinstance(after_hash, str) or not after_hash:
                raise OperationsError(f"change_set #{position} должен задавать content_base64 и after_sha256.")
            try:
                content = base64.b64decode(encoded.encode("ascii"), validate=True)
            except (UnicodeEncodeError, ValueError) as exc:
                raise OperationsError(f"change_set #{position} содержит недопустимый content_base64.") from exc
            if sha256_bytes(content) != after_hash:
                raise OperationsError(f"change_set #{position} не совпадает с after_sha256.")
        if kind == "create" and before_hash != "absent":
            raise OperationsError(f"create в change_set #{position} должен ожидать отсутствующий файл.")
        if kind in {"update", "delete"} and before_hash == "absent":
            raise OperationsError(f"{kind} в change_set #{position} не может ожидать отсутствующий файл.")
        normalized.append({"op": kind, "path": path, "before_sha256": before_hash, "after_sha256": after_hash, "content": content})
    if seen != set(finding["target_paths"]):
        raise OperationsError(f"change_set находки {finding['id']} обязан покрывать каждый target_path ровно один раз.")
    return normalized


def apply_controlled_change_sets(root: Path, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply checked bytes transactionally; shell commands never enter this path."""
    operations = [
        (finding, operation)
        for finding in findings if finding["status"] == "apply_now"
        for operation in normalize_change_set(finding)
    ]
    before_files = git_file_fingerprints(root)
    backups: list[tuple[str, bytes | None]] = []
    try:
        for finding, operation in operations:
            actual = safe_file_bytes(root, operation["path"], f"change_set находки {finding['id']}")
            actual_hash = "absent" if actual is None else sha256_bytes(actual)
            if actual_hash != operation["before_sha256"]:
                raise OperationsError(f"change_set находки {finding['id']} не совпал с before_sha256: {operation['path']}")
            backups.append((operation["path"], actual))
        for finding, operation in operations:
            if operation["op"] == "delete":
                safe_delete_file(root, operation["path"], f"change_set находки {finding['id']}")
            else:
                safe_replace_bytes(root, operation["path"], operation["content"], f"change_set находки {finding['id']}")
            actual = safe_file_bytes(root, operation["path"], f"change_set находки {finding['id']}")
            actual_hash = "absent" if actual is None else sha256_bytes(actual)
            if actual_hash != operation["after_sha256"]:
                raise OperationsError(f"change_set находки {finding['id']} не совпал с after_sha256: {operation['path']}")
        after_files = git_file_fingerprints(root)
        declared = {operation["path"] for _, operation in operations}
        unexpected = changed_fingerprint_paths(before_files, after_files) - declared
        if unexpected:
            raise OperationsError("Контролируемый apply изменил файлы вне change_set: " + ", ".join(sorted(unexpected)))
    except Exception:
        for path, original in reversed(backups):
            try:
                if original is None:
                    if safe_file_bytes(root, path, "rollback") is not None:
                        safe_delete_file(root, path, "rollback")
                else:
                    safe_replace_bytes(root, path, original, "rollback")
            except Exception:
                # The original exception remains the primary diagnostic.  A
                # second inability to restore is still surfaced by the caller
                # through the preserved failing run state.
                pass
        raise
    evidence: list[dict[str, Any]] = []
    for finding in findings:
        if finding["status"] == "apply_now":
            evidence.append({
                "finding_id": finding["id"],
                "source_delta": finding["source_delta"],
                "target_paths": list(finding["target_paths"]),
                "path_surfaces": finding["path_surfaces"],
                "changed_paths": list(finding["target_paths"]),
                "executor": "controlled_change_set",
            })
    return evidence


def load_impact_findings(root: Path, operations: dict[str, Any], *, required: bool = False) -> list[dict[str, Any]]:
    path = impact_report_path(root, operations)
    if path is None:
        if required:
            raise OperationsError("Для режима propose_only нужен impact_report.path в локальном слое.")
        return []
    if not path.is_file():
        if required:
            raise OperationsError(f"Отчёт влияния не найден: {repo_relative(root, path)}")
        return []
    data = load_yaml(path)
    findings = data.get("findings") if isinstance(data, dict) else data
    if not isinstance(findings, list) or not all(isinstance(finding, dict) for finding in findings):
        raise OperationsError("Отчёт влияния должен содержать список findings.")
    normalized = [normalize_impact_finding(finding) for finding in findings]
    ids = [finding["id"] for finding in normalized]
    if len(ids) != len(set(ids)):
        raise OperationsError("Отчёт влияния содержит дублирующиеся id находок.")
    return normalized


def validate_apply_findings(
    root: Path,
    corpus_root: Path,
    findings: list[dict[str, Any]],
    index_sync: dict[str, Any] | None,
    before: dict[str, str],
    after: dict[str, str],
    command_results: list[CommandResult],
    operations: dict[str, Any],
) -> list[dict[str, Any]]:
    # Re-check after the command: an executor must not turn its own basis into
    # rejected/irrelevant state between the proposal and the product diff.
    for finding in findings:
        validate_finding_source_units(corpus_root, finding, index_sync)
    changed_paths = {
        path
        for result in command_results
        for path in result.changed_paths
    }
    deltas = (index_sync or {}).get("delta", {}).get("sources", [])
    delta_by_source = {
        entry.get("source_id"): entry
        for entry in deltas
        if isinstance(entry, dict) and isinstance(entry.get("source_id"), str)
    }
    evidence: list[dict[str, Any]] = []
    declared_paths = {
        path for finding in findings if finding["status"] == "apply_now"
        for path in finding["target_paths"]
    }
    actual_paths = changed_fingerprint_paths(before, after)
    if not actual_paths <= declared_paths:
        unexpected = ", ".join(sorted(actual_paths - declared_paths))
        raise OperationsError(f"apply_now изменил незаявленные файлы: {unexpected}")
    for finding in findings:
        if finding["status"] != "apply_now":
            continue
        delta = finding["source_delta"]
        source_delta = delta_by_source.get(delta["source_id"])
        if source_delta is None:
            raise OperationsError(f"Находка {finding['id']} не связана с дельтой источника.")
        category_ids = {
            item_id
            for category in delta["categories"]
            for item_id in source_delta.get(category, [])
        }
        if not set(delta["unit_ids"]) <= category_ids:
            raise OperationsError(f"Находка {finding['id']} ссылается на единицы вне текущей дельты источника.")
        path_surfaces = finding_path_surfaces(finding, operations)
        changed: list[str] = []
        preexisting: list[str] = []
        for raw_path in finding["target_paths"]:
            target = resolve_inside(root, raw_path, f"target_paths находки {finding['id']}")
            relative = repo_relative(root, target)
            if relative in changed_paths and target.is_file():
                changed.append(relative)
                continue
            expected_hash = finding["expected_hashes"].get(raw_path)
            if expected_hash and target.is_file() and file_content_digest(target) == expected_hash:
                preexisting.append(relative)
                continue
            raise OperationsError(
                f"Находка {finding['id']} не получила требуемый результат: {raw_path} "
                "не изменён и не подтверждён существовавшим состоянием."
            )
        evidence.append(
            {
                "finding_id": finding["id"],
                "source_delta": delta,
                "target_paths": list(finding["target_paths"]),
                "path_surfaces": path_surfaces,
                "changed_paths": changed,
                "preexisting_paths": preexisting,
            }
        )
    return evidence


def owner_unit_entries(index_sync: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Enumerate exactly the unit memberships from which summary counters derive."""
    entries: list[dict[str, Any]] = []
    sync = index_sync or {}
    before = sync.get("baseline", {}) if isinstance(sync.get("baseline"), dict) else {}
    after = sync.get("after", {}) if isinstance(sync.get("after"), dict) else {}
    for delta in sync.get("delta", {}).get("sources", []):
        if not isinstance(delta, dict) or not isinstance(delta.get("source_id"), str):
            continue
        source_id = delta["source_id"]
        for category in ("added", "changed", "removed", "unchanged", "technical_only", "rejected"):
            for unit_id in delta.get(category, []):
                record = after.get(source_id, {}).get(unit_id) or before.get(source_id, {}).get(unit_id) or {}
                eligible, classification = unit_eligibility(record)
                entries.append(
                    {
                        "id": unit_id,
                        "title": str(record.get("title", "")),
                        "source_id": source_id,
                        "category": category,
                        "classification": classification if not eligible else "eligible",
                    }
                )
    return entries


def build_owner_summary(
    root: Path,
    corpus_root: Path,
    result: PipelineResult,
    index_sync: dict[str, Any] | None,
    statement_counts: dict[str, Any],
    statement_snapshot_after: dict[str, dict[str, Any]],
    operations: dict[str, Any],
    transfer_policy: str,
    operational_check: OperationalCheckResult | None,
    source_quality: dict[str, Any],
) -> dict[str, Any]:
    findings = load_impact_findings(root, operations, required=False)
    validate_impact_findings(root, corpus_root, operations, findings, index_sync)
    unit_entries = owner_unit_entries(index_sync)
    index_counts = {
        category: sum(entry["category"] == category for entry in unit_entries)
        for category in ("added", "changed", "removed", "unchanged", "technical_only", "rejected")
    }
    significant = [
        finding for finding in findings if finding["status"] in {"apply_now", "owner_decision"}
    ]
    proposed = [finding for finding in findings if finding["status"] == "apply_now"] if transfer_policy == "propose_only" else []
    applied_ids = {
        evidence.get("finding_id")
        for evidence in result.transfer_evidence
        if isinstance(evidence, dict)
    }
    applied = [
        finding
        for finding in findings
        if transfer_policy == "apply_now"
        and finding["status"] == "apply_now"
        and finding["id"] in applied_ids
    ]
    no_change = [
        {
            "id": finding["id"],
            "source": finding["source"],
            "basis": finding["basis"],
            "affected_surfaces": finding["affected_surfaces"],
            "reason": finding["no_change_reason"],
        }
        for finding in findings
        if finding["status"] == "no_change"
    ]
    owner_decisions = [
        {
            "id": finding["id"],
            "decision": finding["decision_required"],
            "basis": finding["basis"],
            "source": finding["source"],
            "affected_surfaces": finding["affected_surfaces"],
        }
        for finding in findings
        if finding["status"] == "owner_decision"
    ]
    surfaces = sorted({surface for finding in significant for surface in finding["affected_surfaces"]})
    blockers: list[dict[str, Any]] = []
    if operational_check is not None:
        blockers.extend(operational_check.blockers)
        blockers.extend({"kind": "contract_error", "message": error} for error in operational_check.contract_errors)
    blockers.extend(
        {"kind": "queue", "queue": entry.get("queue"), "reason": entry.get("reason")}
        for entry in result.resource_waiting
    )
    limitations: list[dict[str, Any]] = []
    if isinstance(index_sync, dict) and index_sync.get("status") != "completed":
        limitations.extend(
            {
                "kind": "index_refresh",
                "source_id": entry.get("source_id"),
                "reason": entry.get("reason"),
            }
            for entry in index_sync.get("incomplete_sources", [])
            if isinstance(entry, dict)
        )
    available_tail = available_task_count(result.queues)
    if available_tail:
        limitations.append(
            {
                "kind": "available_queue",
                "count": available_tail,
                "reason": "Доступный хвост сохранён в очередях прохода.",
            }
        )
    if result.status != "completed" and not limitations:
        limitations.append({"kind": "run_state", "reason": result.message})
    notable_statement_ids = set(statement_counts.get("added", [])) | set(statement_counts.get("changed", []))
    statement_entries = [
        {
            "id": statement_id,
            "text": str(statement_snapshot_after[statement_id].get("text", "")),
            "source_id": statement_snapshot_after[statement_id].get("source_id"),
            "item_id": statement_snapshot_after[statement_id].get("item_id"),
            "artifact": statement_snapshot_after[statement_id].get("artifact"),
        }
        for statement_id in sorted(notable_statement_ids)
        if statement_id in statement_snapshot_after
    ]
    summary = {
        "run_id": None,
        "status": result.status,
        "reason_code": result.reason_code,
        "transfer_policy": transfer_policy,
        "sources_checked": int((index_sync or {}).get("sources_checked", 0)),
        "sources_total": int((index_sync or {}).get("sources_total", 0)),
        "source_count_approximate": False,
        "unit_counts_are_unique_ids": False,
        "primary_unit_categories": ["added", "changed", "removed", "unchanged"],
        "additional_unit_dimensions": {
            "rejected": int(index_counts.get("rejected", 0)),
            "technical_only": int(index_counts.get("technical_only", 0)),
        },
        "technical_only_is_subset_of_changed": True,
        "units": {
            name: int(index_counts.get(name, 0))
            for name in ("added", "changed", "removed", "unchanged")
        },
        "technical_only_units": int(index_counts.get("technical_only", 0)),
        "unit_entries": unit_entries,
        "no_change_findings": no_change,
        "statements": statement_counts,
        "statement_entries": statement_entries,
        "significant_changes": significant,
        "affected_surfaces": surfaces,
        "applied_changes": applied,
        "unapplied_changes": [
            finding
            for finding in findings
            if transfer_policy == "apply_now"
            and finding["status"] == "apply_now"
            and finding["id"] not in applied_ids
        ],
        "proposed_changes": proposed,
        "owner_decisions": owner_decisions,
        "transfer_evidence": list(result.transfer_evidence),
        "executors": sorted({entry.executor for entry in result.command_results}),
        "source_quality": source_quality,
        "blockers": blockers,
        "limitations": limitations,
        "model_evals": {"status": "not_configured", "residual_risk": "Модельные evals не запускались без настроенных моделей."},
    }
    if result.status == "completed" and not proposed and not applied and not owner_decisions and not significant and not no_change:
        summary["no_change_reason"] = "В отчёте влияния нет находок; техническая дельта индекса не является рекомендацией."
    return summary


def source_quality_result(root: Path, operations: dict[str, Any]) -> dict[str, Any]:
    """Run consumer-owned quality policy once and preserve its exact result.

    This is deliberately an advisory gate: policy decides whether a degraded
    area blocks a consumer's completion, while the owner always sees the same
    structured matrix from which summary counts and recommendations are read.
    """
    config = operations.get("source_quality")
    if not isinstance(config, dict):
        return {
            "status": "not_configured",
            "recommendation": "Настройте source_quality.policy, sources и events в operations.yml.",
            "coverage": [], "events": [], "recommendations": [],
        }
    required = ("policy", "sources", "events")
    if not all(isinstance(config.get(key), str) and config[key] for key in required):
        return {
            "status": "not_configured",
            "recommendation": "Укажите source_quality.policy, sources и events в operations.yml.",
            "coverage": [], "events": [], "recommendations": [],
        }
    paths = {key: resolve_inside(root, config[key], f"source_quality.{key}") for key in required}
    missing = [key for key, path in paths.items() if not path.is_file()]
    if missing:
        return {
            "status": "missing", "missing_inputs": missing,
            "recommendation": "Добавьте отсутствующие входы source-quality policy.",
            "coverage": [], "events": [], "recommendations": [],
        }
    checker = Path(__file__).resolve().parents[2] / "kc-inventory" / "scripts" / "check-source-quality.py"
    command = [sys.executable, str(checker), "--policy", str(paths["policy"]), "--sources", str(paths["sources"]), "--events", str(paths["events"])]
    if isinstance(config.get("today"), str):
        command.extend(["--today", config["today"]])
    completed = subprocess.run(command, cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"status": "missing", "message": completed.stderr.strip() or "source-quality checker did not return JSON", "coverage": [], "events": [], "recommendations": []}
    if completed.returncode == 2:
        return {"status": "missing", "message": result.get("message", "invalid policy"), "coverage": [], "events": [], "recommendations": []}
    coverage = result.get("coverage", []) if isinstance(result.get("coverage"), list) else []
    problems = {problem for row in coverage if isinstance(row, dict) for problem in row.get("problems", []) if isinstance(problem, str)}
    status = "healthy" if result.get("healthy") else ("stale" if "stale_source" in problems else "degraded")
    return {**result, "status": status}


def index_rebuild_transaction_path(root: Path) -> Path:
    return root / ".local" / "state" / "index-rebuild.json"


def write_json_atomically(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        temporary_path = Path(stream.name)
    try:
        os.replace(temporary_path, path)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise


def recover_index_rebuild(root: Path) -> None:
    transaction_path = index_rebuild_transaction_path(root)
    if not transaction_path.is_file():
        return
    try:
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationsError(f"Не удалось прочитать транзакцию пересборки индекса: {exc}") from exc
    if not isinstance(transaction, dict) or transaction.get("contract_version") != 1:
        raise OperationsError("Транзакция пересборки индекса имеет неподдерживаемый договор.")
    entries = transaction.get("entries")
    if not isinstance(entries, list) or not entries:
        raise OperationsError("Транзакция пересборки индекса не содержит производных файлов.")
    for entry in entries:
        if not isinstance(entry, dict) or not all(isinstance(entry.get(key), str) for key in ("target", "temporary", "sha256")):
            raise OperationsError("Транзакция пересборки индекса содержит неполную запись.")
        target = resolve_inside(root, entry["target"], "Целевой путь транзакции индекса")
        temporary = resolve_inside(root, entry["temporary"], "Временный путь транзакции индекса")
        expected = entry["sha256"]
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            temporary.unlink(missing_ok=True)
            continue
        if not temporary.is_file():
            raise OperationsError(
                f"Нельзя восстановить пересборку индекса: отсутствуют {entry['target']} и временный результат."
            )
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != expected:
            raise OperationsError(f"Временный результат пересборки индекса повреждён: {entry['temporary']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, target)
    transaction_path.unlink(missing_ok=True)


def rebuild_indexes(corpus_root: Path, root: Path) -> tuple[int, int]:
    """Rebuild both derived indexes through a recoverable commit transaction."""
    recover_index_rebuild(root)
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
        if item_is_negatively_selected(item) or not item.item_dir or not (item.item_dir / "statements.yml").is_file():
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
    payloads = (
        (items_path, {"items": item_rows}),
        (statements_path, {"statements": statement_rows}),
    )
    transaction_path = index_rebuild_transaction_path(root)
    transaction_id = uuid.uuid4().hex
    entries: list[dict[str, str]] = []
    for target, payload in payloads:
        rendered = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).encode("utf-8")
        temporary = transaction_path.parent / f"index-rebuild-{transaction_id}-{target.name}.tmp"
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(rendered)
        entries.append(
            {
                "target": repo_relative(root, target),
                "temporary": repo_relative(root, temporary),
                "sha256": hashlib.sha256(rendered).hexdigest(),
            }
        )
    write_json_atomically(
        transaction_path,
        {"contract_version": 1, "transaction_id": transaction_id, "status": "staged", "entries": entries},
    )
    try:
        recover_index_rebuild(root)
    except BaseException:
        # The transaction record and any uncommitted temporary remain for the next pass.
        raise
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
    transfer_policy: str = "apply_now",
) -> tuple[str | None, tuple[dict[str, str], ...]]:
    pending_primary = [name for name in PRIMARY_QUEUES if queues[name]]
    candidates = pending_primary
    if not candidates:
        candidates = [name for name in GLOBAL_STAGES if queues[name]][:1]
    waiting: list[dict[str, str]] = []
    for stage in candidates:
        if stage == "apply_changes":
            # Product writes are never delegated to a project shell command.
            # The pipeline either records a proposal or applies its checked
            # declarative change set itself.
            return stage, tuple(waiting)
        if not configured_commands(operations, stage):
            waiting.append({"queue": stage, "reason": "executor_not_configured"})
            continue
        reason = stage_resource_reason(root, operations, stage)
        if reason is not None:
            waiting.append({"queue": stage, "reason": reason})
            continue
        return stage, tuple(waiting)
    return None, tuple(waiting)


def build_agent_task_packet(
    root: Path,
    corpus_root: Path,
    run_id: str,
    queue: str,
    entries: list[dict[str, str]],
) -> dict[str, Any]:
    """Create a bounded hand-off for the agent that owns semantic work."""
    units = [entry["id"] for entry in entries if entry.get("id")]
    inputs = sorted({entry.get("path", "") for entry in entries if entry.get("path")})
    payload = {
        "contract_version": 1,
        "run_id": run_id,
        "queue": queue,
        "unit_ids": units,
        "inputs": inputs,
        "provenance": {"corpus_root": repo_relative(root, corpus_root)},
        "write_scope": [repo_relative(root, corpus_root), ".local"],
        "allowed_outputs": ["item.yml", "normalized.md", "statements.yml", "verification.yml", "impact-findings.yml"],
        "completion_criteria": [
            "Предметный артефакт записан в declared write_scope.",
            "Свидетельство связывает артефакт с unit_ids и SHA-256.",
            "Очередь повторно построена контроллером.",
        ],
    }
    payload["packet_id"] = hashlib.sha256(
        json.dumps(canonical_data(payload), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return payload


def accept_agent_task_evidence(root: Path, packet: dict[str, Any], evidence_path: Path) -> dict[str, Any]:
    """Accept only a packet-bound manifest of real output bytes."""
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationsError(f"Не удалось прочитать evidence агентной задачи: {exc}") from exc
    if not isinstance(evidence, dict) or evidence.get("packet_id") != packet.get("packet_id"):
        raise OperationsError("Evidence не связано с выданным task packet.")
    outputs = evidence.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise OperationsError("Evidence агентной задачи должно содержать outputs.")
    scopes = packet.get("write_scope", [])
    checked: list[dict[str, str]] = []
    for output in outputs:
        if not isinstance(output, dict) or not isinstance(output.get("path"), str) or not isinstance(output.get("sha256"), str):
            raise OperationsError("Каждый output evidence должен задавать path и sha256.")
        path = output["path"]
        if not command_paths_allowed({path}, scopes):
            raise OperationsError("Evidence агентной задачи указывает путь вне write_scope.")
        data = safe_file_bytes(root, path, "output evidence")
        if data is None or sha256_bytes(data) != output["sha256"]:
            raise OperationsError("Evidence агентной задачи не совпадает с существующим артефактом.")
        checked.append({"path": path, "sha256": output["sha256"]})
    return {"packet_id": packet["packet_id"], "outputs": checked, "unit_ids": packet["unit_ids"]}


def command_paths_allowed(paths: set[str], allowed_prefixes: list[str]) -> bool:
    return all(
        any(
            prefix in {"", "."}
            or path == prefix.rstrip("/")
            or path.startswith(f"{prefix.rstrip('/')}/")
            for prefix in allowed_prefixes
        )
        for path in paths
    )


def run_commands(
    root: Path,
    operations: dict[str, Any],
    stage: str,
    activity_callback: Callable[[dict[str, Any] | None], None] | None = None,
    *,
    transfer_policy: str = "apply_now",
    corpus_root: Path | None = None,
) -> list[CommandResult]:
    if stage == "apply_changes":
        raise OperationsError(
            "unsafe_executor: apply_changes допускает только декларативный controlled change_set; "
            "произвольная команда не имеет проверенной файловой песочницы."
        )
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
        if transfer_policy == "propose_only" and corpus_root is not None:
            corpus_prefix = repo_relative(root, corpus_root)
            outside_product = {
                path
                for path in changed
                if not (
                    path == corpus_prefix
                    or path.startswith(f"{corpus_prefix}/")
                    or path == ".local"
                    or path.startswith(".local/")
                )
            }
            if outside_product:
                paths = ", ".join(sorted(outside_product))
                raise OperationsError(
                    "propose_only запрещает изменения производных файлов продукта: " + paths
                )
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
        "profile_name": source.profile_name,
    }
    try:
        return [part.format(**values) for part in argv]
    except KeyError as exc:
        raise OperationsError(f"В argv адаптера используется неизвестный параметр: {exc.args[0]}") from exc


def adapter_contract_version(definition: dict[str, Any]) -> int:
    version = definition.get("contract_version", 1)
    if version not in {1, 2}:
        raise OperationsError(f"Неподдерживаемая версия договора адаптера: {version}.")
    return version


def adapter_operation_definition(
    definition: dict[str, Any], operation: str
) -> dict[str, Any] | None:
    version = adapter_contract_version(definition)
    if version == 1:
        # Version 1 has no index operation. The caller may use its fetch
        # operation as a compatibility fallback only for sources that allow a
        # local/full retrieval strategy.
        return definition if operation == "fetch" else None
    operations = definition.get("operations")
    if not isinstance(operations, dict):
        raise OperationsError("Адаптер версии 2 должен задавать словарь operations.")
    value = operations.get(operation)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise OperationsError(f"Операция адаптера {operation} должна быть словарём.")
    return value


def safe_adapter_message(value: str) -> str:
    sanitized = SENSITIVE_OUTPUT_PATTERN.sub(lambda match: f"{match.group(1)}=[скрыто]", value)
    sanitized = re.sub(r"(?i)([?&](?:token|key|secret|session)=)[^\s&]+", r"\1[скрыто]", sanitized)
    return sanitized


def verification_fingerprints(corpus_root: Path) -> dict[str, str]:
    return {
        path.relative_to(corpus_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in corpus_root.glob("data/*/*/*/verification.yml")
        if path.is_file()
    }


def validate_adapter_result(
    data: Any,
    source: CorpusSource,
    changed_paths: set[str],
    *,
    contract_version: int,
    operation: str,
) -> AdapterResult:
    if not isinstance(data, dict):
        raise OperationsError(f"Адаптер {source.adapter} источника {source.source_id} должен вернуть JSON-объект.")
    if data.get("contract_version") != contract_version:
        raise OperationsError(f"Адаптер {source.adapter} источника {source.source_id} вернул неподдерживаемую версию договора.")
    if data.get("source_id") != source.source_id or data.get("adapter") != source.adapter:
        raise OperationsError(f"Адаптер {source.adapter} вернул результат для другого источника.")
    if contract_version == 2 and data.get("operation") != operation:
        raise OperationsError(f"Адаптер {source.adapter} вернул результат другой операции.")
    status = data.get("status")
    message = data.get("message")
    allowed_statuses = ADAPTER_STATUSES
    if contract_version == 2 and operation == "probe":
        allowed_statuses = ADAPTER_PROBE_STATUSES
    elif contract_version == 2 and operation == "verify":
        allowed_statuses = ADAPTER_VERIFY_STATUSES
    elif contract_version == 2 and operation == "authorize":
        allowed_statuses = ADAPTER_AUTHORIZE_STATUSES
    if status not in allowed_statuses or not isinstance(message, str) or not message:
        raise OperationsError(f"Адаптер {source.adapter} источника {source.source_id} вернул неполный статус.")
    artifacts = data.get("artifacts", [])
    if not isinstance(artifacts, list) or not all(isinstance(path, str) for path in artifacts):
        raise OperationsError(f"Адаптер {source.adapter} источника {source.source_id} вернул неверный список artifacts.")
    reject_sensitive_settings(data)
    return AdapterResult(
        source.source_id,
        source.adapter,
        operation,
        status,
        safe_adapter_message(message),
        tuple(sorted(changed_paths)),
    )


def run_adapter_operation(
    root: Path,
    corpus_root: Path,
    source: CorpusSource,
    definition: dict[str, Any],
    operation: str,
) -> AdapterResult:
    version = adapter_contract_version(definition)
    operation_definition = adapter_operation_definition(definition, operation)
    if operation_definition is None:
        return AdapterResult(
            source.source_id,
            source.adapter,
            operation,
            "unsupported-adapter",
            f"Адаптер не объявляет операцию {operation}.",
            (),
        )
    argv = operation_definition.get("argv")
    write_paths = operation_definition.get("write_paths", [])
    cwd = operation_definition.get("working_directory", ".")
    if not isinstance(argv, list) or not argv or not all(
        isinstance(part, str) and part for part in argv
    ):
        raise OperationsError(f"Адаптер {source.adapter} должен задавать непустой argv.")
    if not isinstance(write_paths, list) or not all(
        isinstance(path, str) and path for path in write_paths
    ):
        raise OperationsError(f"Адаптер {source.adapter} должен задавать write_paths списком.")
    if operation != "probe" and not write_paths and operation != "authorize":
        raise OperationsError(f"Операция {operation} адаптера {source.adapter} должна задавать write_paths.")
    if not isinstance(cwd, str):
        raise OperationsError(f"Адаптер {source.adapter} должен задавать working_directory строкой.")
    for path in write_paths:
        resolve_inside(root, path, f"write_paths адаптера {source.adapter}")
    command_cwd = resolve_inside(root, cwd, f"working_directory адаптера {source.adapter}")
    before = git_file_fingerprints(root)
    before_verification = verification_fingerprints(corpus_root)
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
    after_verification = verification_fingerprints(corpus_root)
    changed = changed_fingerprint_paths(before, after)
    if operation == "probe" and changed:
        paths = ", ".join(sorted(changed))
        raise OperationsError(f"probe адаптера {source.adapter} изменил корпус или Git: {paths}")
    if not command_paths_allowed(changed, write_paths):
        paths = ", ".join(sorted(changed)) or "нет"
        raise OperationsError(f"Адаптер {source.adapter} изменил файлы вне write_paths: {paths}")
    if process.returncode != 0:
        if before_verification != after_verification:
            raise OperationsError(
                f"Неудачная операция {operation} адаптера {source.adapter} изменила verification.yml."
            )
        raw_message = process.stderr.strip() or process.stdout.strip()
        message = safe_adapter_message(raw_message or f"Команда завершилась с кодом {process.returncode}.")
        return AdapterResult(
            source.source_id,
            source.adapter,
            operation,
            "fetch-error" if operation != "probe" else "technical-unavailable",
            message,
            tuple(sorted(changed)),
        )
    try:
        result_data = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise OperationsError(
            f"Адаптер {source.adapter} источника {source.source_id} вернул не JSON: {exc.msg}"
        ) from exc
    result = validate_adapter_result(
        result_data,
        source,
        changed,
        contract_version=version,
        operation=operation,
    )
    if result.status not in ADAPTER_SUCCESS_STATUSES[operation] and before_verification != after_verification:
        raise OperationsError(
            f"Неуспешная операция {operation} адаптера {source.adapter} изменила verification.yml."
        )
    return result


def run_adapters(
    root: Path,
    corpus_root: Path,
    operations: dict[str, Any],
    selected_ids: set[str],
    operation: str,
) -> list[AdapterResult]:
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
            results.append(
                AdapterResult(
                    source.source_id,
                    source.adapter,
                    operation,
                    "unsupported-adapter",
                    "Адаптер не зарегистрирован в настройках операций.",
                    (),
                )
            )
            continue
        version = adapter_contract_version(definition)
        if version == 1:
            if operation != "fetch":
                results.append(
                    AdapterResult(
                        source.source_id,
                        source.adapter,
                        operation,
                        "unsupported-adapter",
                        "Адаптер версии 1 поддерживает только получение.",
                        (),
                    )
                )
                continue
            results.append(run_adapter_operation(root, corpus_root, source, definition, "fetch"))
            continue
        if operation in {"index", "fetch", "verify"}:
            probe = run_adapter_operation(root, corpus_root, source, definition, "probe")
            results.append(probe)
            if probe.status != "ready":
                continue
        results.append(run_adapter_operation(root, corpus_root, source, definition, operation))
    return results


def valid_agent_write_scope(root: Path, source: CorpusSource, value: Any) -> list[str] | None:
    if not isinstance(value, list) or not value:
        return None
    source_prefix = repo_relative(root, source.source_dir)
    result: list[str] = []
    for raw_path in value:
        if not isinstance(raw_path, str) or not raw_path or raw_path in {".", "/"}:
            return None
        try:
            relative_path(raw_path, "agent_route.write_scope")
        except OperationsError:
            return None
        path = raw_path.rstrip("/")
        if path != source_prefix and not path.startswith(f"{source_prefix}/"):
            return None
        result.append(path)
    return result


def valid_agent_route(root: Path, source: CorpusSource) -> bool:
    route = source.agent_route
    if not isinstance(route, dict) or route.get("mode") != "agent":
        return False
    instructions = route.get("instructions")
    allowed_operations = route.get("allowed_operations")
    write_scope = valid_agent_write_scope(root, source, route.get("write_scope"))
    return (
        isinstance(instructions, str)
        and bool(instructions.strip())
        and isinstance(allowed_operations, list)
        and all(isinstance(operation, str) and operation in {"index", "fetch", "verify"} for operation in allowed_operations)
        and "index" in allowed_operations
        and write_scope is not None
    )


def snapshot_digest(snapshot: Any) -> str:
    payload = json.dumps(canonical_data(snapshot), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def content_file_digest(root: Path, relative: str) -> str:
    path = resolve_inside(root, relative, "Путь в свидетельстве агентного индекса")
    if not path.is_file():
        raise OperationsError(f"Путь из свидетельства агентного индекса не является файлом: {relative}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_content_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scoped_file_manifest(root: Path, scopes: list[str]) -> dict[str, str]:
    """Build the complete map promised by an agent index witness."""
    manifest: dict[str, str] = {}
    for raw_scope in scopes:
        scope = resolve_inside(root, raw_scope, "agent_route.write_scope")
        if scope.is_symlink() or (scope.exists() and not scope.is_dir()):
            raise OperationsError("agent_route.write_scope должен указывать на каталог внутри проекта.")
        if not scope.exists():
            continue
        for path in scope.rglob("*"):
            if path.is_symlink():
                try:
                    path.resolve().relative_to(root)
                except ValueError as exc:
                    raise OperationsError("Симлинк в agent_route.write_scope выходит из проекта.") from exc
            elif path.is_file():
                manifest[repo_relative(root, path)] = file_content_digest(path)
    return dict(sorted(manifest.items()))


def validate_agent_index_evidence(
    root: Path,
    source: CorpusSource,
    evidence_path: Path,
    run_id: str,
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    data = load_yaml(evidence_path)
    if not isinstance(data, dict) or data.get("contract_version") != 1:
        raise OperationsError(f"Свидетельство агентного индекса {repo_relative(root, evidence_path)} имеет неподдерживаемый договор.")
    if data.get("run_id") != run_id or data.get("source_id") != source.source_id:
        raise OperationsError("Свидетельство агентного индекса не связано с текущим проходом и источником.")
    if data.get("source_locator") != source.locator or data.get("operation") != "index":
        raise OperationsError("Свидетельство агентного индекса не связано с конкретной операцией источника.")
    route = source.agent_route or {}
    allowed_scope = valid_agent_write_scope(root, source, route.get("write_scope"))
    if allowed_scope is None:
        raise OperationsError("agent_route.write_scope отсутствует, выходит за источник или чрезмерно широк.")
    if data.get("allowed_write_scope") != allowed_scope:
        raise OperationsError("Свидетельство агентного индекса не подтверждает разрешённую область записи.")
    checked = data.get("checked_paths")
    changed = data.get("changed_paths")
    if (
        not isinstance(checked, list)
        or not checked
        or not all(isinstance(path, str) and path for path in checked)
        or not isinstance(changed, list)
        or not all(isinstance(path, str) and path for path in changed)
        or len(checked) != len(set(checked))
        or len(changed) != len(set(changed))
    ):
        raise OperationsError("Свидетельство агентного индекса должно содержать проверенные и изменённые пути.")
    if not command_paths_allowed(set(checked), allowed_scope):
        raise OperationsError("Свидетельство агентного индекса содержит путь вне разрешённой области записи.")
    before_data = data.get("before")
    after_data = data.get("after")
    if not isinstance(before_data, dict) or not isinstance(after_data, dict):
        raise OperationsError("Свидетельство агентного индекса должно содержать снимки before и after.")
    if before_data.get("snapshot_hash") != snapshot_digest(before.get(source.source_id, {})):
        raise OperationsError("Хэш before в свидетельстве агентного индекса не совпадает с последним успешным снимком.")
    actual_after = after.get(source.source_id, {})
    if after_data.get("snapshot_hash") != snapshot_digest(actual_after):
        raise OperationsError("Хэш after в свидетельстве агентного индекса не совпадает с текущим индексом источника.")
    before_paths = before_data.get("paths")
    after_paths = after_data.get("paths")
    if not isinstance(before_paths, dict) or not isinstance(after_paths, dict):
        raise OperationsError("Снимки агентного индекса должны содержать хэши проверенных путей.")
    actual_manifest = scoped_file_manifest(root, allowed_scope)
    if after_paths != actual_manifest or set(checked) != set(actual_manifest):
        raise OperationsError("after-карта агентного индекса не является полной проверяемой картой write_scope.")
    if not all(value is None or isinstance(value, str) for value in before_paths.values()):
        raise OperationsError("before-карта агентного индекса содержит некорректные хэши.")
    if not all(isinstance(value, str) for value in after_paths.values()):
        raise OperationsError("after-карта агентного индекса содержит некорректные хэши.")
    actual_changed = {
        path for path in set(before_paths) | set(after_paths)
        if before_paths.get(path) != after_paths.get(path)
    }
    if set(changed) != actual_changed:
        raise OperationsError("changed_paths агентного индекса не совпадает с различиями before/after.")
    status = data.get("result")
    if status not in {"changed", "no_change"}:
        raise OperationsError("Свидетельство агентного индекса должно иметь результат changed или no_change.")
    snapshots_differ = before.get(source.source_id, {}) != actual_after
    if status == "changed" and (not snapshots_differ or not changed):
        raise OperationsError("changed требует различающиеся снимки и непустой changed_paths.")
    if status == "no_change" and (snapshots_differ or changed):
        raise OperationsError("no_change требует эквивалентные снимки и пустой changed_paths.")
    return {
        "path": repo_relative(root, evidence_path),
        "source_id": source.source_id,
        "run_id": run_id,
        "result": status,
        "checked_paths": checked,
        "changed_paths": changed,
        "allowed_write_scope": allowed_scope,
        "before_hash": before_data["snapshot_hash"],
        "after_hash": after_data["snapshot_hash"],
    }


def run_index_refresh(
    root: Path,
    corpus_root: Path,
    operations: dict[str, Any],
    baseline: dict[str, dict[str, dict[str, Any]]],
    agent_index_confirmed: set[str] | None = None,
    agent_index_evidence: dict[str, Path] | None = None,
    run_id: str = "",
) -> tuple[list[AdapterResult], dict[str, Any]]:
    """Refresh provider indexes before queues are built and persist their delta."""
    definitions = adapter_definitions(operations)
    confirmed = agent_index_confirmed or set()
    evidence_paths = agent_index_evidence or {}
    results: list[AdapterResult] = []
    deltas: list[dict[str, Any]] = []
    incomplete: list[dict[str, str]] = []
    failed_attempts: list[dict[str, Any]] = []
    evidence_records: list[dict[str, Any]] = []
    confirmed_after = canonical_data(baseline)
    sources = load_sources(corpus_root)
    refreshable = [
        source
        for source in sources
        if (source.source_dir / "items.yml").is_file()
        and source.card.get("refresh_policy", "manual") != "none"
        and source.card.get("stability", "mutable") != "stable"
    ]
    for source in refreshable:
        previous = baseline.get(source.source_id, {})
        try:
            evidence_record: dict[str, Any] | None = None
            if source.adapter in {AGENT_ADAPTER, LEGACY_MANUAL_ADAPTER}:
                message = (
                    "Устаревшее adapter: manual: способ получения не зарегистрирован; "
                    "перенесите карточку на adapter: agent с agent_route или зарегистрированный адаптер."
                    if source.adapter == LEGACY_MANUAL_ADAPTER
                    else "Индекс должен обновить агент по agent_route."
                )
                if not valid_agent_route(root, source):
                    result = AdapterResult(source.source_id, source.adapter, "index", "invalid-registry", message, ())
                elif source.source_id not in evidence_paths:
                    result = AdapterResult(
                        source.source_id,
                        source.adapter,
                        "index",
                        "manual-required",
                        message + " Требуется проверяемое свидетельство, а не один флаг подтверждения.",
                        (),
                    )
                else:
                    observed = source_index_snapshot(corpus_root)
                    evidence_record = validate_agent_index_evidence(
                        root,
                        source,
                        evidence_paths[source.source_id],
                        run_id,
                        baseline,
                        observed,
                    )
                    result = AdapterResult(
                        source.source_id,
                        source.adapter,
                        "index",
                        evidence_record["result"],
                        message + " Свидетельство проверено контроллером.",
                        tuple(evidence_record["changed_paths"]),
                    )
            else:
                definition = definitions.get(source.adapter)
                if definition is None:
                    result = AdapterResult(
                        source.source_id,
                        source.adapter,
                        "index",
                        "unsupported-adapter",
                        "Адаптер не зарегистрирован в настройках операций.",
                        (),
                    )
                else:
                    version = adapter_contract_version(definition)
                    if version == 1:
                        if source.card.get("storage_strategy") in {"index_only", "external_reference"}:
                            result = AdapterResult(
                                source.source_id,
                                source.adapter,
                                "index",
                                "unsupported-adapter",
                                "Адаптер версии 1 не объявляет безопасную операцию index для index_only/external_reference.",
                                (),
                            )
                        else:
                            legacy = run_adapter_operation(root, corpus_root, source, definition, "fetch")
                            result = AdapterResult(
                                legacy.source_id,
                                legacy.adapter,
                                "index",
                                legacy.status,
                                "Совместимость: операция index выполнена через legacy fetch. " + legacy.message,
                                legacy.changed_paths,
                            )
                    else:
                        probe = run_adapter_operation(root, corpus_root, source, definition, "probe")
                        if probe.status != "ready":
                            result = probe
                        else:
                            result = run_adapter_operation(root, corpus_root, source, definition, "index")
            results.append(result)
            if result.status in ADAPTER_SUCCESS_STATUSES["index"]:
                after_source = source_index_snapshot(corpus_root).get(source.source_id, {})
                confirmed_after[source.source_id] = after_source
                deltas.append(source_index_delta(source, previous, after_source))
                if evidence_record is not None:
                    evidence_records.append(evidence_record)
            else:
                incomplete.append({"source_id": source.source_id, "reason": result.message})
        except OperationsError as exc:
            message = safe_adapter_message(str(exc))
            result = AdapterResult(source.source_id, source.adapter, "index", "fetch-error", message, ())
            results.append(result)
            failed_attempts.append({"source_id": source.source_id, "operation": "index", "error": message})
            incomplete.append({"source_id": source.source_id, "reason": message})
    try:
        observed_after = source_index_snapshot(corpus_root)
    except OperationsError as exc:
        observed_after = confirmed_after
        failed_attempts.append({"source_id": "__pipeline__", "operation": "snapshot", "error": safe_adapter_message(str(exc))})
    delta = aggregate_index_delta(deltas)
    index_sync = {
        "status": "completed" if not incomplete else "incomplete",
        "sources_checked": len(deltas),
        "sources_total": len(refreshable),
        "incomplete_sources": incomplete,
        "delta": delta,
        "baseline": baseline,
        "after": confirmed_after,
        "last_successful_after": confirmed_after,
        "observed_after": observed_after,
        "adapter_results": [
            {
                "source_id": result.source_id,
                "adapter": result.adapter,
                "operation": result.operation,
                "status": result.status,
                "message": result.message,
                "changed_paths": list(result.changed_paths),
            }
            for result in results
        ],
        "failed_attempts": failed_attempts,
        "agent_evidence": evidence_records,
    }
    return results, index_sync


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


EXECUTOR_NOT_CONFIGURED_HINT = (
    "закрыть вручную после содержательной работы можно через --complete-global-stage"
)


def format_completed_global_stages(run_state: dict[str, Any]) -> str:
    completed = run_state.get("completed_global_stages", [])
    if not completed:
        return "нет"
    completions = run_state.get("global_stage_completions", {})
    if not isinstance(completions, dict):
        completions = {}
    parts = []
    for stage in completed:
        manual = completions.get(stage)
        if isinstance(manual, dict) and manual.get("evidence"):
            evidence = ", ".join(manual["evidence"])
            parts.append(f"{stage} (вручную: {evidence})")
        else:
            parts.append(stage)
    return ", ".join(parts)


def format_resource_waiting_lines(run_state: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for entry in run_state.get("resource_waiting", []):
        if not isinstance(entry, dict):
            continue
        hint = f"; {EXECUTOR_NOT_CONFIGURED_HINT}" if entry.get("reason") == "executor_not_configured" else ""
        lines.append(f"  - {entry.get('queue')}: {entry.get('reason')}{hint}")
    return lines


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
            *format_resource_waiting_lines(run_state),
            f"- завершённые глобальные стадии: {format_completed_global_stages(run_state)}",
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
        summary = run_state.get("owner_summary")
        if isinstance(summary, dict):
            units = summary.get("units", {})
            statements = summary.get("statements", {}).get("counts", {}) if isinstance(summary.get("statements"), dict) else {}
            lines[5:5] = [
                "## Итоговая сводка для владельца",
                "",
                f"- run_id: {summary.get('run_id', run_state.get('run_id'))}",
                f"- конечное состояние: {summary.get('status', run_state.get('status'))} ({summary.get('reason_code', '')})",
                f"- политика переноса: {summary.get('transfer_policy', 'apply_now')}",
                f"- источников проверено: {summary.get('sources_checked', 0)} из {summary.get('sources_total', summary.get('sources_checked', 0))}",
                (
                    "- единицы: "
                    f"добавлено {units.get('added', 0)}, изменено {units.get('changed', 0)}, "
                    f"исчезло {units.get('removed', 0)}, неизменно {units.get('unchanged', 0)}; "
                    f"дополнительные измерения: нерелевантные {summary.get('additional_unit_dimensions', {}).get('rejected', 0)}, "
                    f"только технические {summary.get('additional_unit_dimensions', {}).get('technical_only', 0)}"
                ),
                f"- только технических изменений: {summary.get('technical_only_units', 0)} (подмножество изменённых)",
                (
                    "- утверждения: "
                    f"добавлено {statements.get('added', 0)}, изменено {statements.get('changed', 0)}, "
                    f"устарело {statements.get('obsolete', 0)}, непроверено {statements.get('unverified', 0)}"
                ),
                f"- существенные сведения: {len(summary.get('significant_changes', []))}",
                f"- затронутые поверхности: {', '.join(summary.get('affected_surfaces', [])) or 'нет'}",
                f"- применено изменений: {len(summary.get('applied_changes', []))}",
                f"- предложено изменений: {len(summary.get('proposed_changes', []))}",
                f"- решений владельца требуется: {len(summary.get('owner_decisions', []))}",
                f"- блокеров и ограничений: {len(summary.get('blockers', [])) + len(summary.get('limitations', []))}",
            ]
            for blocker in summary.get("blockers", []):
                if isinstance(blocker, dict):
                    lines.append(
                        "  - блокер: "
                        + "; ".join(f"{key}={value}" for key, value in blocker.items())
                    )
            for limitation in summary.get("limitations", []):
                if isinstance(limitation, dict):
                    lines.append(
                        "  - ограничение: "
                        + "; ".join(f"{key}={value}" for key, value in limitation.items())
                    )
            if summary.get("no_change_reason"):
                lines.append(f"- почему нет существенных предложений: {summary['no_change_reason']}")
            for unit in summary.get("unit_entries", []):
                lines.append(
                    "  - единица "
                    f"{unit.get('id')}: {unit.get('title') or 'без названия'}; "
                    f"source_id={unit.get('source_id')}; категория={unit.get('category')}; "
                    f"классификация={unit.get('classification')}"
                )
            for statement in summary.get("statement_entries", []):
                lines.append(
                    "  - утверждение "
                    f"{statement.get('id')}: {statement.get('text')}; "
                    f"provenance={statement.get('source_id')}/{statement.get('item_id')}; "
                    f"артефакт={statement.get('artifact')}"
                )
            for finding in summary.get("no_change_findings", []):
                lines.append(
                    f"  - no_change {finding.get('id')}: причина={finding.get('reason')}; "
                    f"основание={finding.get('basis')}; источник={finding.get('source')}; "
                    f"поверхности={', '.join(finding.get('affected_surfaces', []))}"
                )
            for finding in summary.get("applied_changes", []):
                evidence = next((entry for entry in summary.get("transfer_evidence", []) if entry.get("finding_id") == finding.get("id")), {})
                lines.append(
                    "  - применено "
                    f"{finding.get('id')}: основание={finding.get('basis')}; "
                    f"единицы={finding.get('source_delta', {}).get('unit_ids', [])}; "
                    f"поверхности={', '.join(finding.get('affected_surfaces', []))}; "
                    f"результат={finding.get('expected_result')}; правка={finding.get('recommended_change')}; "
                    f"фактические пути={evidence.get('changed_paths', []) + evidence.get('preexisting_paths', [])}"
                )
            for finding in summary.get("proposed_changes", []):
                lines.append(
                    "  - предложение "
                    f"{finding.get('id')}: основание={finding.get('basis')}; "
                    f"источник={finding.get('source')}; поверхности={', '.join(finding.get('affected_surfaces', []))}; "
                    f"единицы={finding.get('source_delta', {}).get('unit_ids', [])}; "
                    f"результат={finding.get('expected_result')}; правка={finding.get('recommended_change')}; "
                    f"готовность применения={finding.get('apply_status', 'ready')}"
                )
                if finding.get("configuration_gap"):
                    lines.append(f"    - требуется настройка: {finding['configuration_gap']}")
            for decision in summary.get("owner_decisions", []):
                lines.append(
                    f"  - решение владельца {decision.get('id')}: {decision.get('decision')}; "
                    f"основание={decision.get('basis')}; источник={decision.get('source')}; "
                    f"поверхности={', '.join(decision.get('affected_surfaces', []))}"
                )
            model_evals = summary.get("model_evals")
            if isinstance(model_evals, dict):
                lines.append(f"- модельные evals: {model_evals.get('status')}; {model_evals.get('residual_risk')}")
            lines.append("")
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
            operation = "" if result.operation == "fetch" else f", {result.operation}"
            lines.append(
                f"- {result.source_id} ({result.adapter}{operation}): "
                f"{result.status}; {result.message}; изменено: {changed}"
            )
    if index_counts is not None:
        lines.extend(["", "## Индексы", "", f"- единиц: {index_counts[0]}", f"- утверждений: {index_counts[1]}"])
    if run_state is not None and isinstance(run_state.get("index_sync"), dict):
        index_sync = run_state["index_sync"]
        delta_counts = index_sync.get("delta", {}).get("counts", {})
        lines.extend(
            [
                "",
                "## Дельта индексов источников",
                "",
                f"- состояние обновления: {index_sync.get('status')}",
                f"- источников проверено: {index_sync.get('sources_checked', 0)} из {index_sync.get('sources_total', 0)}",
                (
                    "- единицы: "
                    f"добавлено {delta_counts.get('added', 0)}, изменено {delta_counts.get('changed', 0)}, "
                    f"исчезло {delta_counts.get('removed', 0)}, неизменно {delta_counts.get('unchanged', 0)}, "
                    f"только технически {delta_counts.get('technical_only', 0)}, "
                    f"дополнительное измерение rejected {delta_counts.get('rejected', 0)}"
                ),
            ]
        )
        for entry in index_sync.get("incomplete_sources", []):
            lines.append(f"  - источник {entry.get('source_id')}: {entry.get('reason')}")
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
    if data.get("transfer_policy") is not None and data.get("transfer_policy") not in TRANSFER_POLICIES:
        raise OperationsError(f"Состояние прохода {path} содержит неизвестную transfer_policy.")
    if data.get("index_sync") is not None and not isinstance(data.get("index_sync"), dict):
        raise OperationsError(f"Состояние прохода {path} содержит неверную index_sync.")
    queues = data.get("queues")
    if isinstance(queues, dict):
        for stage in QUEUE_ORDER:
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
    global_stage_completions = data.get("global_stage_completions")
    if global_stage_completions is not None:
        if not isinstance(global_stage_completions, dict):
            raise OperationsError(
                f"Состояние прохода {path} содержит неверный global_stage_completions."
            )
        for stage, completion in global_stage_completions.items():
            if stage not in GLOBAL_STAGES or stage not in completed_global_stages:
                raise OperationsError(
                    f"Состояние прохода {path} содержит запись global_stage_completions "
                    f"для стадии вне completed_global_stages: {stage}."
                )
            if not isinstance(completion, dict):
                raise OperationsError(
                    f"Состояние прохода {path} содержит неверную запись global_stage_completions "
                    f"для стадии {stage}."
                )
            completed_by = completion.get("completed_by")
            completed_at = completion.get("completed_at")
            evidence = completion.get("evidence")
            note = completion.get("note")
            if (
                not isinstance(completed_by, str)
                or not completed_by
                or not isinstance(completed_at, str)
                or not completed_at
                or not isinstance(evidence, list)
                or not evidence
                or not all(isinstance(item, str) and item for item in evidence)
                or (note is not None and not isinstance(note, str))
            ):
                raise OperationsError(
                    f"Состояние прохода {path} содержит неполную запись global_stage_completions "
                    f"для стадии {stage}."
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
    global_stage_completions = (
        dict(previous.get("global_stage_completions", {})) if resumable else {}
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
        "global_stage_completions": global_stage_completions,
        "resource_waiting": [],
        "active_executor": None,
        "queues": queues,
        "transfer_policy": effective_transfer_policy(operations),
        "index_sync": previous.get("index_sync") if resumable else None,
        "owner_summary": previous.get("owner_summary") if resumable else None,
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


def complete_global_stage(
    root: Path,
    corpus_root: Path,
    operations: dict[str, Any],
    destination_state: Path,
    stage: str,
    evidence_args: list[str],
    note: str | None,
) -> int:
    """Manually mark a global stage as done when no project command is configured for it.

    This is the only sanctioned way to close ``concepts``, ``impact_audit``,
    ``apply_changes`` or ``corpus_validation`` when the stage has no registered
    executor: see ADR-0008. It never runs a command and never touches the
    corpus; it only records who closed the stage and which artefact proves it.
    """
    if configured_commands(operations, stage):
        raise OperationsError(
            f"Для стадии {stage} зарегистрирован исполнитель; ручное закрытие запрещено. "
            "Исправьте команду или явно снимите её из настроек операций через kc-setup."
        )
    with run_state_lock(destination_state):
        previous_state = read_run_state(destination_state)
        if previous_state is None:
            raise OperationsError(
                f"Состояние прохода {destination_state} не найдено; закрывать глобальную стадию нечем."
            )
        previous_state = reconcile_interrupted_run_state(destination_state, previous_state)
        if previous_state.get("reason_code") == "executor_identity_unknown":
            raise OperationsError(
                "Нельзя закрыть глобальную стадию без надёжной идентичности предыдущего "
                "исполнителя. Сначала подтвердите отсутствие его последствий."
            )
        if previous_state.get("status") == "completed":
            raise OperationsError(
                "Проход уже в статусе completed; закрывать отдельную глобальную стадию не требуется."
            )
        completed_global_stages = list(previous_state.get("completed_global_stages", []))
        if stage in completed_global_stages:
            print(f"Глобальная стадия {stage} уже закрыта; изменений не потребовалось.")
            return 0
        if not evidence_args:
            raise OperationsError(
                "--complete-global-stage требует минимум один --evidence с существующим артефактом."
            )
        evidence_paths: list[str] = []
        for raw_path in evidence_args:
            resolved = resolve_inside(root, raw_path, "Путь --evidence")
            if not resolved.exists():
                raise OperationsError(f"Путь --evidence не существует: {raw_path}")
            evidence_paths.append(raw_path)
        normalized_names = normalized_artifacts(operations)
        queues_before = build_run_queues(
            corpus_root, normalized_names, root, set(completed_global_stages)
        )
        pending_primary = [name for name in PRIMARY_QUEUES if queues_before[name]]
        if pending_primary:
            raise OperationsError(
                "Первичный контур очередей не пуст: "
                f"{', '.join(pending_primary)}. Ручное закрытие глобальной стадии допустимо "
                "только когда первичный контур пуст."
            )
        pending_global = [name for name in GLOBAL_STAGES if queues_before[name]]
        if not pending_global or pending_global[0] != stage:
            expected = pending_global[0] if pending_global else "ни одной (все стадии уже закрыты)"
            raise OperationsError(
                f"Нарушен порядок глобальных стадий: следующая незакрытая стадия — {expected}, "
                f"а не {stage}."
            )
        completed_at = datetime.now(UTC).isoformat()
        completed_global_stages.append(stage)
        completions = dict(previous_state.get("global_stage_completions", {}))
        completions[stage] = {
            "completed_by": "manual",
            "completed_at": completed_at,
            "evidence": evidence_paths,
            "note": note,
        }
        new_queues = build_run_queues(
            corpus_root, normalized_names, root, set(completed_global_stages)
        )
        active_groups, group_count, overflow = decision_groups(new_queues, operations)
        resource_waiting = [
            entry
            for entry in previous_state.get("resource_waiting", [])
            if isinstance(entry, dict) and new_queues.get(entry.get("queue"))
        ]
        updated_state = {
            **previous_state,
            "completed_global_stages": completed_global_stages,
            "global_stage_completions": completions,
            "queues": new_queues,
            "available_task_count": available_task_count(new_queues),
            "blocked_task_count": len(new_queues["human_decision"]),
            "blocker_codes": blocker_codes(new_queues),
            "human_decision_groups": active_groups,
            "human_decision_group_count": group_count,
            "human_decision_group_overflow": overflow,
            "resource_waiting": resource_waiting,
            "updated_at": completed_at,
            "message": (
                f"Глобальная стадия {stage} закрыта вручную (evidence: "
                f"{', '.join(evidence_paths)})."
            ),
        }
        write_run_state(destination_state, updated_state)
    print(f"Глобальная стадия {stage} закрыта вручную.")
    print(render_report(corpus_root, updated_state["queues"], [], None, [], None, updated_state))
    print(f"Состояние прохода записано: {repo_relative(root, destination_state)}")
    return 0


def run_pipeline(
    root: Path,
    corpus_root: Path,
    operations: dict[str, Any],
    max_steps: int | None,
    completed_global_stages: set[str],
    activity_callback: Callable[[dict[str, Any] | None, dict[str, list[dict[str, str]]]], None] | None = None,
    *,
    transfer_policy: str = "apply_now",
    index_sync: dict[str, Any] | None = None,
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
    transfer_evidence: list[dict[str, Any]] = []

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
        queue, waiting = runnable_queue(root, operations, queues, transfer_policy)
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
                if waiting_entries and all(entry["reason"] == "executor_not_configured" for entry in waiting_entries):
                    return PipelineResult(
                        "awaiting_agent_task",
                        "agent_task_required",
                        queues,
                        tuple(results),
                        steps,
                        "Содержательная очередь ожидает ограниченный task packet штатного агента.",
                        completed_stages(),
                        waiting_entries,
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
                transfer_evidence=tuple(transfer_evidence),
            )
        before = stage_fingerprint(queues, queue)
        apply_findings: list[dict[str, Any]] = []
        apply_before: dict[str, str] = {}
        if queue == "apply_changes":
            try:
                apply_findings = load_impact_findings(
                    root,
                    operations,
                    required=transfer_policy == "propose_only",
                )
                validate_impact_findings(
                    root, corpus_root, operations, apply_findings, index_sync
                )
            except OperationsError as exc:
                return PipelineResult(
                    "failed",
                    "proposal_report_missing",
                    queues,
                    tuple(results),
                    steps,
                    f"Стадия apply_changes не может использовать отчёт влияния: {exc}",
                    completed_stages(),
                )
            if transfer_policy == "apply_now":
                apply_before = git_file_fingerprints(root)
        if queue == "apply_changes" and transfer_policy == "propose_only":
            proposal_count = sum(finding["status"] == "apply_now" for finding in apply_findings)
            results.append(
                CommandResult(
                    "apply_changes:propose_only",
                    0,
                    (),
                    f"Изменения продукта не применялись; предложений: {proposal_count}.",
                )
            )
            completed_global_stages.add(queue)
            steps += 1
            queues = build_run_queues(
                corpus_root,
                normalized_names,
                root,
                completed_global_stages,
            )
            if activity_callback is not None:
                activity_callback(None, queues)
            continue
        if queue == "apply_changes" and transfer_policy == "apply_now":
            try:
                transfer_evidence.extend(apply_controlled_change_sets(root, apply_findings))
            except OperationsError as exc:
                queues = build_run_queues(
                    corpus_root,
                    normalized_names,
                    root,
                    completed_global_stages,
                )
                return PipelineResult(
                    "failed",
                    "apply_evidence_missing",
                    queues,
                    tuple(results),
                    steps,
                    f"Стадия apply_changes не подтверждена: {exc}",
                    completed_stages(),
                    transfer_evidence=tuple(transfer_evidence),
                )
            results.append(
                CommandResult(
                    "apply_changes:controlled_change_set",
                    0,
                    tuple(sorted(path for evidence in transfer_evidence for path in evidence["target_paths"])),
                    "Декларативные изменения проверены и применены контроллером.",
                )
            )
            steps += 1
            completed_global_stages.add(queue)
            queues = build_run_queues(
                corpus_root,
                normalized_names,
                root,
                completed_global_stages,
            )
            if activity_callback is not None:
                activity_callback(None, queues)
            continue
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
                transfer_policy=transfer_policy,
                corpus_root=corpus_root,
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
        if queue == "apply_changes" and transfer_policy == "apply_now":
            try:
                transfer_evidence.extend(
                    validate_apply_findings(
                        root,
                        corpus_root,
                        apply_findings,
                        index_sync,
                        apply_before,
                        git_file_fingerprints(root),
                        stage_results,
                        operations,
                    )
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
                    "apply_evidence_missing",
                    queues,
                    tuple(results),
                    steps,
                    f"Стадия apply_changes не подтверждена: {exc}",
                    completed_stages(),
                    transfer_evidence=tuple(transfer_evidence),
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
                transfer_evidence=tuple(transfer_evidence),
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Спланировать или выполнить операции переносимого корпуса знаний.")
    parser.add_argument("corpus", type=Path, help="Корень корпуса с corpus.yml.")
    parser.add_argument("--operations", type=Path, help="Необязательный файл настроек операций.")
    parser.add_argument(
        "--transfer-policy",
        choices=sorted(TRANSFER_POLICIES),
        help="Политика переноса результатов: применить безопасные изменения или только предложить их.",
    )
    parser.add_argument(
        "--agent-index-refreshed",
        action="append",
        default=[],
        metavar="SOURCE_ID",
        help="Устаревшая отметка агента; сама по себе никогда не подтверждает обновление.",
    )
    parser.add_argument(
        "--agent-index-evidence",
        action="append",
        nargs=2,
        default=[],
        metavar=("SOURCE_ID", "PATH"),
        help="Проверяемое свидетельство обновления индекса: источник и локальный YAML-путь.",
    )
    parser.add_argument("--stage", default="source_sync", help="Стадия проектных команд для --run-commands.")
    parser.add_argument("--run-commands", action="store_true", help="Явно выполнить команды указанной стадии.")
    parser.add_argument("--run-adapters", action="store_true", help="Явно выполнить зарегистрированные адаптеры источников.")
    parser.add_argument(
        "--adapter-operation",
        choices=sorted(ADAPTER_OPERATIONS),
        default="fetch",
        help=(
            "Операция адаптера для --run-adapters. fetch и verify сначала выполняют probe; "
            "authorize запускается только при явном выборе."
        ),
    )
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
        "--complete-global-stage",
        choices=GLOBAL_STAGES,
        help=(
            "Пометить глобальную стадию без зарегистрированного исполнителя закрытой вручную, "
            "не выполняя команд и не меняя корпус. Требует --evidence."
        ),
    )
    parser.add_argument(
        "--evidence",
        action="append",
        default=[],
        dest="evidence",
        help=(
            "Репо-относительный путь к оставленному артефакту ручной работы для "
            "--complete-global-stage; можно повторять."
        ),
    )
    parser.add_argument(
        "--note",
        help="Необязательный поясняющий текст к ручному закрытию стадии для --complete-global-stage.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        help="Ограничить число стадий в одной попытке, сохранив проход незавершённым.",
    )
    parser.add_argument("--state", type=Path, help="Репо-относительный путь состояния прохода.")
    parser.add_argument(
        "--agent-task-evidence", type=Path,
        help="Репо-относительный JSON evidence для ранее выданного task packet; продолжает тот же run.",
    )
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
    transfer_policy = effective_transfer_policy(operations, args.transfer_policy)
    agent_index_evidence_args = getattr(args, "agent_index_evidence", [])
    items: list[CorpusItem] = []
    queues = empty_queues()
    command_results: list[CommandResult] = []
    adapter_results: list[AdapterResult] = []
    operational_check: OperationalCheckResult | None = None
    if args.max_steps is not None and args.max_steps < 1:
        raise OperationsError("--max-steps должен быть положительным числом.")
    if args.max_steps is not None and not args.run_pipeline:
        raise OperationsError("--max-steps требует --run-pipeline.")
    if args.transfer_policy is not None and not args.run_pipeline:
        raise OperationsError("--transfer-policy требует --run-pipeline.")
    if args.agent_index_refreshed and not args.run_pipeline:
        raise OperationsError("--agent-index-refreshed требует --run-pipeline.")
    if agent_index_evidence_args and not args.run_pipeline:
        raise OperationsError("--agent-index-evidence требует --run-pipeline.")
    if args.agent_task_evidence and not args.run_pipeline:
        raise OperationsError("--agent-task-evidence требует --run-pipeline.")
    if args.run_pipeline and (args.run_commands or args.run_adapters):
        raise OperationsError("--run-pipeline нельзя совмещать с запуском одной стадии или адаптеров.")
    if args.reconcile_state and (args.run_pipeline or args.run_commands or args.run_adapters):
        raise OperationsError("--reconcile-state нельзя совмещать с запуском операций.")
    if args.operational_policy and not (args.operational_check or args.run_pipeline):
        raise OperationsError("--operational-policy требует --operational-check или --run-pipeline.")
    if args.complete_global_stage and (
        args.run_pipeline
        or args.run_commands
        or args.run_adapters
        or args.reconcile_state
        or args.rebuild_indexes
    ):
        raise OperationsError(
            "--complete-global-stage нельзя совмещать с --run-pipeline, --run-commands, "
            "--run-adapters, --reconcile-state или --rebuild-indexes."
        )
    if args.complete_global_stage and not operations_path:
        raise OperationsError("Для --complete-global-stage нужен параметр --operations.")
    if args.complete_global_stage:
        destination_state = state_path(root, operations, args.state)
        return complete_global_stage(
            root,
            corpus_root,
            operations,
            destination_state,
            args.complete_global_stage,
            args.evidence,
            args.note,
        )
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
            running_state["transfer_policy"] = transfer_policy
            if args.agent_task_evidence:
                packet = previous_state.get("agent_task_packet") if isinstance(previous_state, dict) else None
                if not isinstance(packet, dict):
                    raise OperationsError("Нет ожидающего agent_task_packet для предъявленного evidence.")
                evidence_path = resolve_inside(root, str(args.agent_task_evidence), "Путь evidence агентной задачи")
                if not evidence_path.is_file():
                    raise OperationsError("Evidence агентной задачи не найден.")
                running_state["accepted_agent_task_evidence"] = accept_agent_task_evidence(root, packet, evidence_path)
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
            index_sync: dict[str, Any] | None = None
            index_counts: tuple[int, int] | None = None
            previous_index_sync = previous_state.get("index_sync") if isinstance(previous_state, dict) else None
            previous_statement_snapshot = previous_state.get("statement_snapshot") if isinstance(previous_state, dict) else None
            if not isinstance(previous_statement_snapshot, dict):
                previous_statement_snapshot = statement_snapshot(corpus_root)
            if not operational_check.contract_errors:
                baseline: dict[str, dict[str, dict[str, Any]]] = {}
                agent_evidence: dict[str, Path] = {}
                for raw_source_id, raw_path in agent_index_evidence_args:
                    if raw_source_id in agent_evidence:
                        raise OperationsError(f"Для источника {raw_source_id} передано несколько свидетельств агентного индекса.")
                    evidence_path = resolve_inside(root, raw_path, "Путь свидетельства агентного индекса")
                    if not evidence_path.is_file():
                        raise OperationsError(f"Свидетельство агентного индекса не найдено: {raw_path}")
                    agent_evidence[raw_source_id] = evidence_path
                try:
                    if (
                        isinstance(previous_index_sync, dict)
                        and isinstance(
                            previous_index_sync.get("last_successful_after", previous_index_sync.get("after")),
                            dict,
                        )
                    ):
                        baseline = previous_index_sync.get(
                            "last_successful_after", previous_index_sync.get("after", {})
                        )
                    else:
                        baseline = source_index_snapshot(corpus_root)
                    adapter_results, index_sync = run_index_refresh(
                        root,
                        corpus_root,
                        operations,
                        baseline,
                        set(args.agent_index_refreshed),
                        agent_evidence,
                        running_state["run_id"],
                    )
                    if index_sync["status"] == "completed":
                        try:
                            index_counts = rebuild_indexes(corpus_root, root)
                        except OperationsError as exc:
                            index_sync["status"] = "incomplete"
                            index_sync.setdefault("incomplete_sources", []).append(
                                {"source_id": "__index__", "reason": str(exc)}
                            )
                            index_sync["index_rebuild_error"] = str(exc)
                except OperationsError as exc:
                    adapter_results = adapter_results or []
                    index_sync = {
                        "status": "incomplete",
                        "sources_checked": 0,
                        "sources_total": 0,
                        "incomplete_sources": [{"source_id": "__pipeline__", "reason": str(exc)}],
                        "delta": aggregate_index_delta([]),
                        "baseline": baseline,
                        "after": canonical_data(baseline),
                        "last_successful_after": canonical_data(baseline),
                        "failed_attempts": [{"source_id": "__pipeline__", "operation": "index", "error": str(exc)}],
                    }
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
                    "index_sync": index_sync,
                    "statement_snapshot": previous_statement_snapshot,
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
            elif index_sync is not None and index_sync.get("status") != "completed":
                pipeline_result = PipelineResult(
                    "failed",
                    "index_refresh_incomplete",
                    initial_queues,
                    (),
                    0,
                    "Индекс источников обновлён не полностью; содержательный проход не заявлен завершённым.",
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
                        transfer_policy=transfer_policy,
                        index_sync=index_sync,
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
            if index_sync is not None and index_sync.get("status") == "completed":
                try:
                    index_counts = rebuild_indexes(corpus_root, root)
                except OperationsError as exc:
                    pipeline_result = PipelineResult(
                        "failed",
                        "index_rebuild_failed",
                        pipeline_result.queues,
                        pipeline_result.command_results,
                        pipeline_result.steps,
                        f"Индексы не удалось пересобрать после прохода: {exc}",
                        pipeline_result.completed_global_stages,
                    )
            current_statement_snapshot = statement_snapshot(corpus_root)
            statement_counts = statement_delta(previous_statement_snapshot, current_statement_snapshot)
            source_quality = source_quality_result(root, operations)
            try:
                owner_summary = build_owner_summary(
                    root,
                    corpus_root,
                    pipeline_result,
                    index_sync,
                    statement_counts,
                    current_statement_snapshot,
                    operations,
                    transfer_policy,
                    operational_check,
                    source_quality,
                )
            except OperationsError as exc:
                owner_summary = {
                    "status": "failed",
                    "reason_code": "impact_report_invalid",
                    "transfer_policy": transfer_policy,
                    "blockers": [{"kind": "impact_report", "message": str(exc)}],
                    "limitations": [],
                    "units": {},
                    "statements": statement_counts,
                    "significant_changes": [],
                    "affected_surfaces": [],
                    "applied_changes": [],
                    "proposed_changes": [],
                    "owner_decisions": [],
                    "source_quality": source_quality,
                }
                if pipeline_result.status == "completed":
                    pipeline_result = PipelineResult(
                        "failed",
                        "impact_report_invalid",
                        pipeline_result.queues,
                        pipeline_result.command_results,
                        pipeline_result.steps,
                        f"Итоговая сводка не может подтвердить отчёт влияния: {exc}",
                        pipeline_result.completed_global_stages,
                    )
            owner_summary["run_id"] = running_state["run_id"]
            running_state["index_sync"] = index_sync
            running_state["statement_snapshot"] = current_statement_snapshot
            running_state["source_quality"] = source_quality
            running_state["owner_summary"] = owner_summary
            run_state = finish_run_state(running_state, pipeline_result, operations)
            if pipeline_result.status == "awaiting_agent_task":
                queue = next((entry["queue"] for entry in pipeline_result.resource_waiting if entry.get("queue")), None)
                if isinstance(queue, str):
                    run_state["agent_task_packet"] = build_agent_task_packet(
                        root, corpus_root, running_state["run_id"], queue, pipeline_result.queues[queue]
                    )
            run_state["owner_summary"] = owner_summary
            run_state["index_sync"] = index_sync
            run_state["statement_snapshot"] = current_statement_snapshot
            run_state["source_quality"] = source_quality
            write_run_state(destination_state, run_state)
        report = render_report(
            corpus_root,
            pipeline_result.queues,
            list(pipeline_result.command_results),
            index_counts,
            adapter_results,
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
        adapter_results = run_adapters(
            root,
            corpus_root,
            operations,
            set(args.source),
            args.adapter_operation,
        )
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
