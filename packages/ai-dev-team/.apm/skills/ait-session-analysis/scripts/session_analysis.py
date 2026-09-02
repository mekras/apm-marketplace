#!/usr/bin/env python3
"""Select local Codex and Claude Code session fragments for analysis."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


STATE_VERSION = 1
DEFAULT_LIMIT = 8
DEFAULT_MAX_CHARS = 120_000
DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_FILES = 20_000
DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_UNIT_CHARS = 12_000
DEFAULT_MAX_TEXT_FRAGMENT_CHARS = 8_000
DEFAULT_MAX_TOOL_FRAGMENT_CHARS = 4_000
MAX_METADATA_BYTES = 32 * 1024 * 1024
MAX_HEADER_BYTES = 1_048_576
ADAPTERS = {"codex": 1, "claude-code": 1}
CODEX_ROW_TYPES = {
    "session_meta",
    "event_msg",
    "response_item",
    "world_state",
    "turn_context",
    "compacted",
    "inter_agent_communication_metadata",
}
CODEX_PAYLOAD_TYPES = {
    None,
    "task_started",
    "message",
    "user_message",
    "reasoning",
    "agent_message",
    "custom_tool_call",
    "custom_tool_call_output",
    "token_count",
    "function_call",
    "function_call_output",
    "task_complete",
    "patch_apply_end",
    "thread_settings_applied",
    "web_search_end",
    "context_compacted",
    "turn_aborted",
    "tool_search_call",
    "tool_search_output",
    "mcp_tool_call_end",
    "web_search_call",
    "thread_rolled_back",
    "item_completed",
    "thread_goal_updated",
    "sub_agent_activity",
}
CLAUDE_ROW_TYPES = {
    "mode",
    "permission-mode",
    "file-history-snapshot",
    "user",
    "attachment",
    "last-prompt",
    "ai-title",
    "assistant",
    "system",
    "queue-operation",
}
CLAUDE_CONTENT_TYPES = {"text", "image", "thinking", "tool_use", "tool_result"}
CODEX_ITEM_TYPES = {
    "Plan",
    "UserMessage",
    "Reasoning",
    "AgentMessage",
    "CommandExecution",
    "Extension",
    "FileChange",
    "ContextCompaction",
    "SubAgentActivity",
    "CollabAgentToolCall",
}
SECRET_KEYS = re.compile(
    r"(?i)(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|client[_-]?secret|token|secret|password|passwd|"
    r"private[_-]?key|credential|cookie|session[_-]?key|"
    r"[a-z0-9_-]+[_-](?:secret|token|password|credential|cookie))"
)
HEX_64 = re.compile(r"[0-9a-f]{64}")
HEX_32 = re.compile(r"[0-9a-f]{32}")
STATE_KEYS = {
    "version",
    "project",
    "generation",
    "baseline_at",
    "handled",
    "last_success_at",
    "adapters",
    "epoch",
}
CANDIDATE_KEYS = {
    "version",
    "mode",
    "project",
    "generation",
    "baseline_at",
    "handled",
    "committable",
    "prepared_at",
    "snapshot_at",
    "adapters",
    "epoch",
    "mac",
}

SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)(authorization\s*[:=]\s*basic\s+)[^\s,;]+"),
    re.compile(
        r"(?i)((?:api[_-]?key|token|secret|password|passwd|credential|"
        r"cookie|private[_-]?key|session[_-]?key)\s*[:=]\s*)[^\s,;]+"
    ),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://[^\s:/@]+:)[^\s/@]+@"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"(?<!\d)(?:\+?\d[\s().-]*){10,15}(?!\d)"),
    re.compile(
        r"-----BEGIN [^-]+PRIVATE KEY-----.*?"
        r"-----END [^-]+PRIVATE KEY-----",
        re.DOTALL,
    ),
)


class SessionError(RuntimeError):
    """A safe, user-facing session selection error."""


@dataclass(frozen=True)
class Unit:
    client: str
    session_id: str
    unit_id: str
    completed_at: str
    source: str
    content: str
    content_digest: str

    @property
    def stable_hash(self) -> str:
        value = f"{self.client}\0{self.session_id}\0{self.unit_id}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @property
    def completed_part_id(self) -> str:
        """Return the identifier shared by normalized fragments of one part."""
        return self.unit_id.split(":chunk:", 1)[0]

    @property
    def completed_part_hash(self) -> str:
        """Return a disclosure-safe identifier of the completed part."""
        value = f"{self.client}\0{self.session_id}\0{self.completed_part_id}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    device: int
    inode: int
    size: int
    modified_at: float
    mode: int


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def strict_time(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise SessionError(f"{label} должен содержать время")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SessionError(f"{label} содержит неверное время") from exc
    if parsed.tzinfo is None:
        raise SessionError(f"{label} должен содержать часовой пояс")
    return parsed.astimezone(dt.timezone.utc)


def parse_time(value: Any, fallback: float) -> str:
    if isinstance(value, str) and value:
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = dt.datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc).isoformat()
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(fallback, dt.timezone.utc).isoformat()


def redact(text: str) -> tuple[str, int]:
    count = 0
    result = text
    for pattern in SECRET_PATTERNS:
        result, replacements = pattern.subn(
            lambda match: (
                f"{match.group(1)}[REDACTED]"
                if match.lastindex
                else "[REDACTED]"
            ),
            result,
        )
        count += replacements
    return result, count


def public_error(message: str) -> str:
    prefixes = (
        "Каталог Codex недоступен",
        "Каталог Claude Code недоступен",
        "Число записей журналов превышает предел",
        "Совокупный размер журналов проекта превышает предел",
        "Символическая ссылка на журнал запрещена",
        "Журнал должен быть обычным файлом",
        "Журнал превышает предел",
        "Журнал изменён",
        "Непонятная строка",
        "Неподдерживаемая запись",
        "Неподдерживаемая сигнатура журнала Codex",
        "Неподдерживаемая сигнатура Claude Code",
        "Журнал Codex содержит неизвестную сигнатуру",
        "Журнал Claude Code содержит неизвестную сигнатуру",
        "Повреждена запись session_meta Codex",
        "Повреждена структурная запись Codex",
        "Повреждено сообщение Codex",
        "Повреждён маркер завершения Codex",
        "Повреждён завершённый элемент Codex",
        "Неизвестна фаза сообщения Codex",
        "Повреждено сообщение Claude Code",
        "Повреждено содержимое Claude Code",
        "Повреждён маркер Claude Code",
        "Кандидат уже существует",
        "Другое подтверждение состояния уже выполняется",
        "Кандидат подтверждения недоступен",
        "Кандидат относится к прежней эпохе состояния",
        "Целостность кандидата не подтверждена",
        "Локальное состояние не может быть символической ссылкой",
        "Ограниченная история не содержит завершённых частей",
    )
    for prefix in prefixes:
        if message.startswith(prefix):
            return prefix
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()[:12]
    return f"Ошибка локального анализа [{digest}]"


def sanitize_value(value: Any) -> tuple[Any, int]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        count = 0
        for key, item in value.items():
            text_key = str(key)
            if SECRET_KEYS.fullmatch(text_key):
                result[text_key] = "[REDACTED]"
                count += 1
            else:
                result[text_key], nested = sanitize_value(item)
                count += nested
        return result, count
    if isinstance(value, list):
        result_list: list[Any] = []
        count = 0
        for item in value:
            clean, nested = sanitize_value(item)
            result_list.append(clean)
            count += nested
        return result_list, count
    if isinstance(value, str):
        return redact(value)
    return value, 0


def content_chunks(parts: list[str]) -> list[str]:
    content = "\n\n".join(parts)
    if len(content) <= DEFAULT_MAX_UNIT_CHARS:
        return [content]
    chunks: list[str] = []
    for offset in range(0, len(content), DEFAULT_MAX_UNIT_CHARS):
        number = len(chunks) + 1
        chunks.append(
            f"[NORMALIZED CHUNK {number}]\n"
            + content[offset : offset + DEFAULT_MAX_UNIT_CHARS]
        )
    total = len(chunks)
    return [chunk.replace("]\n", f" OF {total}]\n", 1) for chunk in chunks]


def bounded_fragment(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return (
        text[:half]
        + "\n[VALUE ABBREVIATED BY NORMALIZATION]\n"
        + text[-half:]
    )


def canonical_project(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise SessionError(f"Корень проекта недоступен: {path}: {exc}") from exc


def belongs_to_project(cwd: Any, project: Path) -> bool:
    if not isinstance(cwd, str) or not cwd:
        return False
    try:
        candidate = Path(cwd).expanduser().resolve(strict=False)
        candidate.relative_to(project)
        if (project / ".git").exists():
            current = candidate
            while current != project:
                if (current / ".git").exists():
                    return False
                current = current.parent
        return True
    except (OSError, ValueError):
        return False


def open_snapshot(snapshot: FileSnapshot) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(snapshot.path, flags)
    except OSError as exc:
        raise SessionError(f"Не удалось открыть {snapshot.path}: {exc}") from exc
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_dev != snapshot.device
        or info.st_ino != snapshot.inode
        or info.st_size < snapshot.size
    ):
        os.close(descriptor)
        raise SessionError(f"Журнал изменён после снимка: {snapshot.path}")
    return descriptor


def read_snapshot(
    snapshot: FileSnapshot,
    max_file_bytes: int,
    expected_digest: str | None = None,
) -> list[dict[str, Any]]:
    if snapshot.size > max_file_bytes:
        raise SessionError(
            f"Журнал превышает предел {max_file_bytes} байт: {snapshot.path}"
        )
    descriptor = open_snapshot(snapshot)
    try:
        with os.fdopen(descriptor, "rb") as source:
            data = source.read(snapshot.size)
    except OSError as exc:
        raise SessionError(f"Не удалось прочитать {snapshot.path}: {exc}") from exc
    if len(data) != snapshot.size:
        raise SessionError(f"Журнал изменён во время чтения: {snapshot.path}")
    if (
        expected_digest is not None
        and hashlib.sha256(data).hexdigest() != expected_digest
    ):
        raise SessionError(f"Журнал изменён после снимка: {snapshot.path}")

    rows: list[dict[str, Any]] = []
    for number, raw_line in enumerate(data.splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise SessionError(
                f"Непонятная строка {number} в журнале {snapshot.path}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise SessionError(
                f"Неподдерживаемая запись {number} в журнале {snapshot.path}"
            )
        rows.append(value)
    return rows


def first_json_row(
    snapshot: FileSnapshot,
    required_key: str | None = None,
    bytes_read: list[int] | None = None,
) -> dict[str, Any] | None:
    descriptor = -1
    try:
        descriptor = open_snapshot(snapshot)
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            remaining = MAX_HEADER_BYTES
            while remaining > 0:
                raw_line = source.readline(remaining + 1)
                if not raw_line:
                    return None
                if bytes_read is not None:
                    bytes_read[0] += len(raw_line)
                if len(raw_line) > remaining:
                    return None
                remaining -= len(raw_line)
                if not raw_line.strip():
                    continue
                value = json.loads(raw_line.decode("utf-8"))
                if not isinstance(value, dict):
                    return None
                if required_key is None or required_key in value:
                    return value
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return None


def text_parts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [bounded_fragment(value, DEFAULT_MAX_TEXT_FRAGMENT_CHARS)]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(text_parts(item))
        return result
    if not isinstance(value, dict):
        return []

    kind = value.get("type")
    if kind in {"text", "input_text", "output_text"}:
        text = value.get("text")
        return [text] if isinstance(text, str) else []
    if kind in {"tool_use", "function_call"}:
        name = value.get("name", "unknown")
        arguments = value.get("input", value.get("arguments"))
        safe_arguments, _ = sanitize_value(arguments)
        rendered = json.dumps(safe_arguments, ensure_ascii=False)
        return [
            f"[tool {name}] "
            f"{bounded_fragment(rendered, DEFAULT_MAX_TOOL_FRAGMENT_CHARS)}"
        ]
    if kind in {"tool_result", "function_call_output"}:
        content = value.get("content", value.get("output"))
        rendered = "\n".join(text_parts(content))
        return [
            "[tool result] "
            + bounded_fragment(rendered, DEFAULT_MAX_TOOL_FRAGMENT_CHARS)
        ]

    result: list[str] = []
    for key in (
        "message",
        "content",
        "text",
        "output",
        "arguments",
        "last_agent_message",
        "item",
    ):
        if key in value:
            result.extend(text_parts(value[key]))
    return result


def codex_event_text(row: dict[str, Any]) -> str | None:
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return None
    row_type = row.get("type", "event")
    event_type = payload.get("type", "event")
    role = payload.get("role", event_type)

    parts: list[str] = []
    if row_type == "response_item":
        parts = text_parts(payload)
    elif event_type in {
        "user_message",
        "agent_message",
        "task_complete",
        "item_completed",
    }:
        parts = text_parts(payload)
    if not parts:
        return None
    return f"[{role}] " + "\n".join(part for part in parts if part)


def validate_codex_structure(rows: list[dict[str, Any]], path: Path) -> None:
    for row in rows:
        row_type = row.get("type")
        payload = row.get("payload")
        if row_type == "session_meta":
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("id"), str)
                or not isinstance(payload.get("cwd"), str)
            ):
                raise SessionError(f"Повреждена запись session_meta Codex: {path}")
            continue
        if row_type not in {"event_msg", "response_item"}:
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
            raise SessionError(f"Повреждена структурная запись Codex: {path}")
        event_type = payload["type"]
        if (
            event_type in {"user_message", "agent_message"}
            and "message" in payload
            and payload.get("message") is not None
            and not isinstance(payload.get("message"), (str, list, dict))
        ):
            raise SessionError(f"Повреждено сообщение Codex: {path}")
        if event_type == "task_complete" and (
            not isinstance(
                payload.get("turn_id", payload.get("task_id")),
                str,
            )
            or (
                payload.get("last_agent_message") is not None
                and not isinstance(
                    payload.get("last_agent_message"),
                    (str, list, dict),
                )
            )
        ):
            raise SessionError(f"Повреждён маркер завершения Codex: {path}")
        if event_type == "item_completed":
            item = payload.get("item")
            if (
                not isinstance(payload.get("turn_id"), str)
                or not isinstance(item, dict)
                or item.get("type") not in CODEX_ITEM_TYPES
            ):
                raise SessionError(f"Повреждён завершённый элемент Codex: {path}")
            if item.get("type") == "AgentMessage" and item.get("phase") not in {
                "commentary",
                "final_answer",
            }:
                raise SessionError(f"Неизвестна фаза сообщения Codex: {path}")


def codex_units(
    snapshot: FileSnapshot,
    project: Path,
    max_file_bytes: int,
    expected_digest: str | None = None,
) -> list[Unit]:
    rows = read_snapshot(snapshot, max_file_bytes, expected_digest)
    first = rows[0] if rows else None
    meta = first.get("payload") if isinstance(first, dict) else None
    if not isinstance(meta, dict) or not belongs_to_project(meta.get("cwd"), project):
        return []
    if first.get("type") != "session_meta":
        raise SessionError(
            f"Неподдерживаемая сигнатура журнала Codex: {snapshot.path}"
        )
    unknown_rows = sorted(
        {str(row.get("type")) for row in rows if row.get("type") not in CODEX_ROW_TYPES}
    )
    unknown_payloads = sorted(
        {
            str(row["payload"].get("type"))
            for row in rows
            if isinstance(row.get("payload"), dict)
            and row["payload"].get("type") not in CODEX_PAYLOAD_TYPES
        }
    )
    if unknown_rows or unknown_payloads:
        raise SessionError(
            "Журнал Codex содержит неизвестную сигнатуру: "
            f"rows={unknown_rows}, payloads={unknown_payloads}: {snapshot.path}"
        )
    validate_codex_structure(rows, snapshot.path)
    if not any(row.get("type") in {"event_msg", "response_item"} for row in rows):
        raise SessionError(
            f"Неподдерживаемая сигнатура журнала Codex: {snapshot.path}"
        )

    session_id = str(meta.get("id") or snapshot.path.stem)
    task_complete_turns = {
        str(row["payload"].get("turn_id"))
        for row in rows
        if row.get("type") == "event_msg"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("type") == "task_complete"
        and row["payload"].get("turn_id") is not None
    }
    buffer: list[str] = []
    units: list[Unit] = []
    for index, row in enumerate(rows, 1):
        rendered = codex_event_text(row)
        if rendered:
            buffer.append(rendered)
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        event_type = payload.get("type")
        completed = event_type == "task_complete"
        if event_type == "item_completed":
            item = payload.get("item")
            completed = (
                isinstance(item, dict)
                and item.get("phase") == "final_answer"
                and str(payload.get("turn_id")) not in task_complete_turns
            )
        if not completed:
            continue
        unit_id = str(
            payload.get("turn_id")
            or payload.get("task_id")
            or payload.get("id")
            or index
        )
        chunks = content_chunks(buffer)
        for part, content in enumerate(chunks, 1):
            chunk_id = (
                unit_id
                if len(chunks) == 1
                else f"{unit_id}:chunk:{part:06d}:{len(chunks):06d}"
            )
            units.append(
                Unit(
                    client="codex",
                    session_id=session_id,
                    unit_id=chunk_id,
                    completed_at=parse_time(
                        row.get("timestamp"),
                        snapshot.modified_at,
                    ),
                    source=str(snapshot.path),
                    content=content,
                    content_digest=hashlib.sha256(
                        content.encode("utf-8")
                    ).hexdigest(),
                )
            )
        buffer = []
    return units


def claude_event_text(row: dict[str, Any]) -> str | None:
    row_type = row.get("type")
    if row_type not in {"user", "assistant", "system"}:
        return None
    message = row.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role", row_type)
    parts = text_parts(message.get("content"))
    if not parts:
        return None
    return f"[{role}] " + "\n".join(part for part in parts if part)


def validate_claude_structure(rows: list[dict[str, Any]], path: Path) -> None:
    for row in rows:
        if row.get("type") not in {"user", "assistant", "system"}:
            continue
        message = row.get("message")
        if row.get("type") == "system" and message is None:
            if row.get("content") is not None and not isinstance(
                row.get("content"),
                (str, list, dict),
            ):
                raise SessionError(f"Повреждено содержимое Claude Code: {path}")
            continue
        if (
            not isinstance(message, dict)
            or (
                message.get("role") is not None
                and not isinstance(message.get("role"), str)
            )
            or (
                "content" not in message
                or not isinstance(message.get("content"), (str, list))
            )
        ):
            raise SessionError(f"Повреждено сообщение Claude Code: {path}")
        content = message["content"]
        if isinstance(content, list) and any(
            not isinstance(item, dict)
            or item.get("type") not in CLAUDE_CONTENT_TYPES
            for item in content
        ):
            raise SessionError(f"Повреждено содержимое Claude Code: {path}")
        if row.get("type") == "assistant" and message.get("stop_reason") is not None:
            if not isinstance(message.get("stop_reason"), str):
                raise SessionError(f"Повреждён маркер Claude Code: {path}")


def claude_units(
    snapshot: FileSnapshot,
    project: Path,
    max_file_bytes: int,
    expected_digest: str | None = None,
) -> list[Unit]:
    rows = read_snapshot(snapshot, max_file_bytes, expected_digest)
    first = next((row for row in rows if "cwd" in row), None)
    if not isinstance(first, dict) or not belongs_to_project(first.get("cwd"), project):
        return []
    matching = [row for row in rows if belongs_to_project(row.get("cwd"), project)]
    if not matching:
        return []
    unknown_rows = sorted(
        {
            str(row.get("type"))
            for row in rows
            if row.get("type") not in CLAUDE_ROW_TYPES
        }
    )
    unknown_content: set[str] = set()
    for row in rows:
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        items = content if isinstance(content, list) else [content]
        unknown_content.update(
            str(item.get("type"))
            for item in items
            if isinstance(item, dict)
            and item.get("type") not in CLAUDE_CONTENT_TYPES
        )
    if unknown_rows or unknown_content:
        raise SessionError(
            "Журнал Claude Code содержит неизвестную сигнатуру: "
            f"rows={unknown_rows}, content={sorted(unknown_content)}: "
            f"{snapshot.path}"
        )
    validate_claude_structure(rows, snapshot.path)
    if not any(row.get("type") in {"user", "assistant", "system"} for row in matching):
        raise SessionError(
            f"Неподдерживаемая сигнатура Claude Code: {snapshot.path}"
        )
    session_id = str(
        next(
            (
                row.get("sessionId")
                for row in matching
                if row.get("sessionId")
            ),
            snapshot.path.stem,
        )
    )
    buffer: list[str] = []
    units: list[Unit] = []
    for index, row in enumerate(matching, 1):
        rendered = claude_event_text(row)
        if rendered:
            buffer.append(rendered)
        message = row.get("message")
        if row.get("type") != "assistant" or not isinstance(message, dict):
            continue
        if message.get("stop_reason") != "end_turn":
            continue
        unit_id = str(row.get("uuid") or message.get("id") or index)
        chunks = content_chunks(buffer)
        for part, content in enumerate(chunks, 1):
            chunk_id = (
                unit_id
                if len(chunks) == 1
                else f"{unit_id}:chunk:{part:06d}:{len(chunks):06d}"
            )
            units.append(
                Unit(
                    client="claude-code",
                    session_id=session_id,
                    unit_id=chunk_id,
                    completed_at=parse_time(
                        row.get("timestamp"),
                        snapshot.modified_at,
                    ),
                    source=str(snapshot.path),
                    content=content,
                    content_digest=hashlib.sha256(
                        content.encode("utf-8")
                    ).hexdigest(),
                )
            )
        buffer = []
    return units


def discover_roots(explicit: Iterable[str], defaults: Iterable[Path]) -> list[Path]:
    roots = [Path(value).expanduser() for value in explicit]
    if not roots:
        roots = [path for path in defaults if path.is_dir()]
    return roots


def file_snapshot(path: Path) -> FileSnapshot:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SessionError(f"Не удалось зафиксировать журнал {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise SessionError(f"Символическая ссылка на журнал запрещена: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise SessionError(f"Журнал должен быть обычным файлом: {path}")
    return FileSnapshot(
        path=path,
        device=info.st_dev,
        inode=info.st_ino,
        size=info.st_size,
        modified_at=info.st_mtime,
        mode=info.st_mode,
    )


def snapshot_belongs(
    client: str,
    snapshot: FileSnapshot,
    project: Path,
    bytes_read: list[int] | None = None,
) -> bool:
    if client == "Codex":
        first = first_json_row(snapshot, bytes_read=bytes_read)
        payload = first.get("payload") if isinstance(first, dict) else None
        return isinstance(payload, dict) and belongs_to_project(
            payload.get("cwd"),
            project,
        )
    first = first_json_row(snapshot, "cwd", bytes_read)
    return isinstance(first, dict) and belongs_to_project(first.get("cwd"), project)


def iter_jsonl(
    root: Path,
    max_entries: int,
    entry_counter: list[int],
) -> Iterable[Path]:
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as stream:
                for entry in stream:
                    entry_counter[0] += 1
                    if entry_counter[0] > max_entries:
                        raise SessionError(
                            "Число записей журналов превышает предел "
                            f"{max_entries}"
                        )
                    if entry.is_symlink():
                        if entry.name.endswith(".jsonl"):
                            raise SessionError(
                                "Символическая ссылка на журнал запрещена: "
                                f"{entry.path}"
                            )
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif (
                        entry.is_file(follow_symlinks=False)
                        and entry.name.endswith(".jsonl")
                    ):
                        yield Path(entry.path)
        except OSError as exc:
            raise SessionError(f"Каталог журналов недоступен: {exc}") from exc


def snapshot_digest(snapshot: FileSnapshot) -> str:
    descriptor = open_snapshot(snapshot)
    digest = hashlib.sha256()
    remaining = snapshot.size
    try:
        with os.fdopen(descriptor, "rb") as source:
            while remaining:
                chunk = source.read(min(1_048_576, remaining))
                if not chunk:
                    raise SessionError(
                        f"Журнал изменён во время снимка: {snapshot.path}"
                    )
                digest.update(chunk)
                remaining -= len(chunk)
    except OSError as exc:
        raise SessionError(f"Не удалось прочитать журнал: {exc}") from exc
    return digest.hexdigest()


def scan_units(
    project: Path,
    codex_roots: list[Path],
    claude_roots: list[Path],
    max_file_bytes: int,
    max_files: int,
    max_total_bytes: int,
    matching_file_limit: int | None = None,
) -> tuple[list[Unit], list[str], str, bool]:
    units: list[Unit] = []
    errors: list[str] = []
    file_count = 0
    entry_counter = [0]
    snapshots: list[tuple[str, FileSnapshot, Any]] = []
    for client, roots, adapter in (
        ("Codex", codex_roots, codex_units),
        ("Claude Code", claude_roots, claude_units),
    ):
        for root in roots:
            if not root.is_dir():
                errors.append(f"Каталог {client} недоступен: {root}")
                continue
            try:
                for path in iter_jsonl(root, max_files, entry_counter):
                    file_count += 1
                    if file_count > max_files:
                        raise SessionError(
                            f"Число журналов превышает предел {max_files}"
                        )
                    snapshots.append((client, file_snapshot(path), adapter))
            except SessionError as exc:
                errors.append(str(exc))
                return units, errors, now_iso(), False
    snapshots.sort(key=lambda item: item[1].modified_at, reverse=True)
    total_bytes = 0
    verified: list[tuple[FileSnapshot, Any, str]] = []
    history_limited = False
    matching_files = 0
    for index, (client, snapshot, adapter) in enumerate(snapshots):
        try:
            header_bytes = [0]
            belongs = snapshot_belongs(
                client,
                snapshot,
                project,
                header_bytes,
            )
            total_bytes += header_bytes[0]
            if total_bytes > max_total_bytes:
                raise SessionError(
                    "Совокупный размер журналов проекта превышает предел "
                    f"{max_total_bytes} байт"
                )
            if not belongs:
                continue
            matching_files += 1
            if snapshot.size > max_file_bytes:
                raise SessionError(
                    f"Журнал превышает предел {max_file_bytes} байт: "
                    f"{snapshot.path}"
                )
            total_bytes += 2 * snapshot.size
            if total_bytes > max_total_bytes:
                raise SessionError(
                    "Совокупный размер журналов проекта превышает предел "
                    f"{max_total_bytes} байт"
                )
            digest = snapshot_digest(snapshot)
            verified.append((snapshot, adapter, digest))
            if (
                matching_file_limit is not None
                and matching_files >= matching_file_limit
                and index < len(snapshots) - 1
            ):
                history_limited = True
                break
        except SessionError as exc:
            errors.append(str(exc))
            if str(exc).startswith(
                "Совокупный размер журналов проекта превышает предел"
            ):
                break
    snapshot_at = now_iso()
    for snapshot, adapter, digest in verified:
        try:
            units.extend(adapter(snapshot, project, max_file_bytes, digest))
        except SessionError as exc:
            errors.append(str(exc))
    units.sort(key=lambda item: (item.completed_at, item.client, item.session_id, item.unit_id))
    return units, errors, snapshot_at, history_limited


def load_state(path: Path, project_fingerprint: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "version": STATE_VERSION,
            "project": project_fingerprint,
            "generation": 0,
            "baseline_at": None,
            "handled": {},
            "last_success_at": None,
            "adapters": ADAPTERS,
            "epoch": None,
        }
    secure_existing_file(path, "локальное состояние")
    try:
        value = json.loads(secure_read_text(path, "локальное состояние"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionError(f"Локальное состояние повреждено: {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("version") != STATE_VERSION:
        raise SessionError(f"Неподдерживаемая версия состояния: {path}")
    if set(value) != STATE_KEYS:
        raise SessionError("Локальное состояние не соответствует строгой схеме")
    if value.get("project") != project_fingerprint:
        raise SessionError("Локальное состояние относится к другому проекту")
    if not HEX_64.fullmatch(str(value.get("project"))):
        raise SessionError("В локальном состоянии повреждена идентичность проекта")
    if value.get("adapters") != ADAPTERS:
        raise SessionError("Состояние относится к другой версии адаптеров")
    epoch = value.get("epoch")
    if not isinstance(epoch, str) or not HEX_32.fullmatch(epoch):
        raise SessionError("В локальном состоянии отсутствует эпоха сброса")
    generation = value.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise SessionError("В локальном состоянии повреждено поколение")
    baseline_at = value.get("baseline_at")
    if baseline_at is not None:
        strict_time(baseline_at, "baseline_at состояния")
    last_success_at = value.get("last_success_at")
    if last_success_at is not None:
        strict_time(last_success_at, "last_success_at состояния")
    handled = value.get("handled")
    if not isinstance(handled, dict) or not all(
        isinstance(key, str)
        and HEX_64.fullmatch(key)
        and isinstance(item, str)
        and HEX_64.fullmatch(item)
        for key, item in handled.items()
    ):
        raise SessionError("В локальном состоянии повреждены обработанные части")
    return value


def secure_existing_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SessionError(f"Не удалось проверить {label}: {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise SessionError(f"{label.capitalize()} не может быть символической ссылкой: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise SessionError(f"{label.capitalize()} должен быть обычным файлом: {path}")
    if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
        raise SessionError(f"{label.capitalize()} доступен не только владельцу: {path}")


def secure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        info = path.parent.lstat()
    except OSError as exc:
        raise SessionError(f"Не удалось проверить каталог состояния: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SessionError("Каталог состояния должен быть обычным каталогом")
    if path.parent.absolute() != path.parent.resolve(strict=True):
        raise SessionError("Путь к каталогу состояния содержит символическую ссылку")
    if os.name == "posix":
        os.chmod(path.parent, 0o700)


def secure_read_text(path: Path, label: str) -> str:
    secure_existing_file(path, label)
    snapshot = file_snapshot(path)
    if os.name == "posix" and stat.S_IMODE(snapshot.mode) & 0o077:
        raise SessionError(f"{label.capitalize()} доступен не только владельцу")
    if snapshot.size > MAX_METADATA_BYTES:
        raise SessionError(f"{label.capitalize()} превышает допустимый размер")
    descriptor = open_snapshot(snapshot)
    try:
        with os.fdopen(descriptor, "rb") as source:
            data = source.read(snapshot.size)
    except OSError as exc:
        raise SessionError(f"Не удалось прочитать {label}: {exc}") from exc
    if len(data) != snapshot.size:
        raise SessionError(f"{label.capitalize()} изменён во время чтения")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SessionError(f"{label.capitalize()} должен быть UTF-8") from exc


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    secure_parent(path)
    if path.exists() or path.is_symlink():
        secure_existing_file(path, "целевой файл")
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor = -1
    temporary_name = ""
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            descriptor = -1
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise SessionError(f"Не удалось записать {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()


def exclusive_json(path: Path, value: dict[str, Any]) -> None:
    secure_parent(path)
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        if os.name == "posix":
            os.chmod(path, 0o600)
    except FileExistsError as exc:
        raise SessionError(f"Кандидат уже существует: {path}") from exc
    except OSError as exc:
        raise SessionError(f"Не удалось создать кандидата {path}: {exc}") from exc


def integrity_key(directory: Path, create: bool) -> bytes:
    path = directory / "integrity.key"
    if path.exists() or path.is_symlink():
        value = secure_read_text(path, "ключ целостности").strip()
        if HEX_64.fullmatch(value):
            return bytes.fromhex(value)
        try:
            record = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SessionError("Ключ целостности повреждён") from exc
        key = record.get("key") if isinstance(record, dict) else None
        if not isinstance(key, str) or not HEX_64.fullmatch(key):
            raise SessionError("Ключ целостности повреждён")
        return bytes.fromhex(key)
    if not create:
        raise SessionError("Ключ целостности отсутствует")
    secure_parent(path)
    value = secrets.token_bytes(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as target:
            target.write(value.hex() + "\n")
            target.flush()
            os.fsync(target.fileno())
    except FileExistsError:
        return integrity_key(directory, create=False)
    except OSError as exc:
        raise SessionError(f"Не удалось создать ключ целостности: {exc}") from exc
    return value


def rotate_integrity_key(directory: Path) -> None:
    path = directory / "integrity.key"
    if not path.exists() and not path.is_symlink():
        raise SessionError("Ключ целостности отсутствует")
    secure_existing_file(path, "ключ целостности")
    atomic_json(
        path,
        {
            "key": secrets.token_hex(32),
        },
    )
    integrity_key(directory, create=False)


def candidate_mac(candidate: dict[str, Any], key: bytes) -> str:
    unsigned = {name: value for name, value in candidate.items() if name != "mac"}
    payload = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def project_from_state_path(state_path: Path) -> Path:
    parent = state_path.absolute().parent
    if (
        parent.name != "session-analysis"
        or parent.parent.name != "local"
        or parent.parent.parent.name != ".ai-dev-team"
    ):
        raise SessionError("Путь состояния не соответствует локальному каталогу проекта")
    project = parent.parent.parent.parent
    try:
        resolved = project.resolve(strict=True)
    except OSError as exc:
        raise SessionError(f"Не удалось установить корень проекта: {exc}") from exc
    if project != resolved:
        raise SessionError("Путь проекта содержит символическую ссылку")
    return resolved


def validate_candidate(candidate: Any, state_path: Path) -> dict[str, Any]:
    if not isinstance(candidate, dict) or candidate.get("version") != STATE_VERSION:
        raise SessionError("Кандидат подтверждения имеет неподдерживаемый формат")
    if set(candidate) != CANDIDATE_KEYS:
        raise SessionError("Кандидат подтверждения не соответствует строгой схеме")
    key = integrity_key(state_path.absolute().parent, create=False)
    mac = candidate.get("mac")
    if not isinstance(mac, str) or not hmac.compare_digest(mac, candidate_mac(candidate, key)):
        raise SessionError("Целостность кандидата не подтверждена")
    if candidate.get("mode") != "incremental" or candidate.get("committable") is not True:
        raise SessionError("Эту выборку нельзя подтверждать")
    if candidate.get("adapters") != ADAPTERS:
        raise SessionError("Кандидат относится к другой версии адаптеров")
    project = project_from_state_path(state_path)
    fingerprint = hashlib.sha256(str(project).encode("utf-8")).hexdigest()
    if candidate.get("project") != fingerprint:
        raise SessionError("Кандидат относится к другому корню проекта")
    generation = candidate.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise SessionError("Кандидат содержит неверное поколение")
    epoch = candidate.get("epoch")
    if not isinstance(epoch, str) or not HEX_32.fullmatch(epoch):
        raise SessionError("Кандидат не содержит допустимую эпоху состояния")
    handled = candidate.get("handled")
    if (
        not isinstance(handled, dict)
        or len(handled) > 100_000
        or not all(
            isinstance(key, str)
            and HEX_64.fullmatch(key)
            and isinstance(value, str)
            and HEX_64.fullmatch(value)
            for key, value in handled.items()
        )
    ):
        raise SessionError("Кандидат содержит повреждённые дайджесты")
    snapshot_at = strict_time(candidate.get("snapshot_at"), "snapshot_at кандидата")
    prepared_at = strict_time(candidate.get("prepared_at"), "prepared_at кандидата")
    baseline_at = strict_time(candidate.get("baseline_at"), "baseline_at кандидата")
    if baseline_at > snapshot_at or snapshot_at > prepared_at:
        raise SessionError("Временные границы кандидата противоречат друг другу")
    if prepared_at > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5):
        raise SessionError("Кандидат подготовлен в недопустимом будущем")
    return candidate


def select_units(
    units: list[Unit],
    state: dict[str, Any],
    mode: str,
    session: str | None,
    period_start: dt.datetime | None,
    period_end: dt.datetime | None,
    limit: int,
    offset: int,
) -> tuple[list[Unit], bool, bool, str | None]:
    if mode == "session":
        candidates = [unit for unit in units if unit.session_id == session]
        selected = candidates[offset : offset + limit]
        return selected, False, offset + len(selected) < len(candidates), None
    if mode == "all":
        selected = units[offset : offset + limit]
        return selected, False, offset + len(selected) < len(units), None
    if mode == "period":
        candidates = [
            unit
            for unit in units
            if (
                period_start is None
                or strict_time(unit.completed_at, "время завершения части")
                >= period_start
            )
            and (
                period_end is None
                or strict_time(unit.completed_at, "время завершения части")
                < period_end
            )
        ]
        selected = candidates[offset : offset + limit]
        return selected, False, offset + len(selected) < len(candidates), None

    baseline = state.get("baseline_at")
    handled = state.get("handled", {})
    if baseline is None:
        groups: list[list[Unit]] = []
        for unit in units:
            base_id = unit.unit_id.split(":chunk:", 1)[0]
            key = (unit.client, unit.session_id, unit.completed_at, base_id)
            if not groups:
                groups.append([unit])
                previous_key = key
            elif key == previous_key:
                groups[-1].append(unit)
            else:
                groups.append([unit])
                previous_key = key
        selected_groups: list[list[Unit]] = []
        remaining = limit
        for group in reversed(groups):
            if remaining == 0:
                break
            selected_groups.append(group[:remaining])
            remaining -= min(len(group), remaining)
        selected = [
            unit
            for group in reversed(selected_groups)
            for unit in group
        ]
        baseline = selected[0].completed_at if selected else now_iso()
        return selected, len(units) > len(selected), False, baseline
    candidates = [
        unit
        for unit in units
        if unit.completed_at >= baseline
        and handled.get(unit.stable_hash)
        != unit.content_digest
    ]
    selected = candidates[:limit]
    return selected, False, len(candidates) > len(selected), baseline


def completed_part_count(units: Iterable[Unit]) -> int:
    """Count logical completed parts without multiplying their fragments."""
    return len(
        {
            (unit.client, unit.session_id, unit.completed_part_id)
            for unit in units
        }
    )


def prepare(args: argparse.Namespace) -> int:
    project = canonical_project(Path(args.project_root))
    fingerprint = hashlib.sha256(str(project).encode("utf-8")).hexdigest()
    state_path = Path(args.state).expanduser()
    candidate_path = Path(args.candidate).expanduser()
    expected_parent = project / ".ai-dev-team" / "local" / "session-analysis"
    if state_path.absolute().parent != expected_parent:
        raise SessionError("Состояние должно находиться в локальном каталоге проекта")
    if candidate_path.absolute().parent != expected_parent:
        raise SessionError("Кандидат должен находиться в локальном каталоге проекта")
    if not candidate_path.name.startswith("candidate-"):
        raise SessionError("Имя кандидата должно начинаться с candidate-")
    state = load_state(state_path, fingerprint)
    codex_roots = discover_roots(
        args.codex_root,
        (Path.home() / ".codex" / "sessions",),
    )
    claude_roots = discover_roots(
        args.claude_root,
        (Path.home() / ".claude" / "projects",),
    )
    if not codex_roots and not claude_roots:
        raise SessionError(
            "Журналы Codex и Claude Code не найдены. "
            "Укажите каталоги явно или анализируйте текущий диалог без сценария."
        )

    first_incremental_run = (
        args.mode == "incremental" and state.get("baseline_at") is None
    )
    units, errors, snapshot_at, history_limited = scan_units(
        project,
        codex_roots,
        claude_roots,
        args.max_file_bytes,
        args.max_files,
        args.max_total_bytes,
        max(32, args.limit * 4) if first_incremental_run else None,
    )
    period_start = (
        strict_time(args.period_start, "Начало периода")
        if args.period_start is not None
        else None
    )
    period_end = (
        strict_time(args.period_end, "Конец периода")
        if args.period_end is not None
        else None
    )
    if period_start is not None and period_end is not None and period_start >= period_end:
        raise SessionError("Начало периода должно быть раньше конца периода")
    if history_limited and not units:
        errors.append(
            "Ограниченная история не содержит завершённых частей"
        )
    selected, earlier_unseen, more_available, baseline = select_units(
        units,
        state,
        args.mode,
        args.session,
        period_start,
        period_end,
        args.limit,
        args.offset,
    )
    earlier_unseen = earlier_unseen or history_limited
    if not selected and state.get("baseline_at") is None:
        baseline = snapshot_at
    if args.mode == "session" and not selected:
        raise SessionError(f"Завершённые части сессии {args.session} не найдены")

    output_units: list[dict[str, Any]] = []
    total_chars = 0
    redactions = 0
    abbreviations = 0
    truncated = False
    committed_hashes: dict[str, str] = {}
    for unit in selected:
        clean, replacements = redact(unit.content)
        redactions += replacements + clean.count("[REDACTED]")
        abbreviations += clean.count("[VALUE ABBREVIATED BY NORMALIZATION]")
        if total_chars + len(clean) > args.max_chars:
            truncated = True
            break
        total_chars += len(clean)
        committed_hashes[unit.stable_hash] = unit.content_digest
        output_units.append(
            {
                "client": unit.client,
                "session": hashlib.sha256(
                    f"{unit.client}\0{unit.session_id}".encode("utf-8")
                ).hexdigest()[:12],
                "completed_part": unit.completed_part_hash,
                "completed_at": unit.completed_at,
                "content": clean,
            }
        )

    committable = args.mode == "incremental" and not errors and not truncated
    candidate = {
        "version": STATE_VERSION,
        "mode": args.mode,
        "project": fingerprint,
        "generation": state["generation"],
        "baseline_at": baseline,
        "handled": committed_hashes,
        "committable": committable,
        "prepared_at": now_iso(),
        "snapshot_at": snapshot_at,
        "adapters": ADAPTERS,
        "epoch": state.get("epoch") or secrets.token_hex(16),
    }
    key = integrity_key(candidate_path.parent, create=True)
    candidate["mac"] = candidate_mac(candidate, key)
    exclusive_json(candidate_path, candidate)

    result = {
        "status": "ready" if not errors and not truncated else "incomplete",
        "mode": args.mode,
        "coverage": {
            "completed_parts_found": completed_part_count(units),
            "normalized_fragments_found": len(units),
            "selected_parts": completed_part_count(selected),
            "normalized_fragments_selected": len(output_units),
            "earlier_history_not_selected": earlier_unseen,
            "history_files_not_read": history_limited,
            "more_parts_available": more_available,
            "next_offset": (
                args.offset + len(output_units)
                if more_available and args.mode in {"all", "period", "session"}
                else None
            ),
            "truncated_by_context_limit": truncated,
            "snapshot_at": snapshot_at,
            "errors": [public_error(error) for error in errors],
            "redactions": redactions,
            "abbreviated_values": abbreviations,
            "period": {
                "start": period_start.isoformat() if period_start else None,
                "end": period_end.isoformat() if period_end else None,
            }
            if args.mode == "period"
            else None,
        },
        "commit_allowed_after_successful_report": committable,
        "units": output_units,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors and not truncated else 2


def commit(args: argparse.Namespace) -> int:
    state_path = Path(args.state).expanduser()
    candidate_path = Path(args.candidate).expanduser()
    if state_path.absolute().parent != candidate_path.absolute().parent:
        raise SessionError("Состояние и кандидат должны находиться в одном каталоге")
    lock_path = state_path.with_name(state_path.name + ".lock")
    secure_parent(lock_path)
    try:
        lock_descriptor = os.open(
            lock_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.close(lock_descriptor)
    except FileExistsError as exc:
        raise SessionError("Другое подтверждение состояния уже выполняется") from exc
    except OSError as exc:
        raise SessionError(f"Не удалось заблокировать состояние: {exc}") from exc
    try:
        candidate = json.loads(
            secure_read_text(candidate_path, "кандидат подтверждения")
        )
    except (OSError, json.JSONDecodeError, SessionError) as exc:
        if lock_path.exists():
            lock_path.unlink()
        raise SessionError(f"Кандидат подтверждения недоступен: {exc}") from exc
    try:
        candidate = validate_candidate(candidate, state_path)
        state = load_state(state_path, str(candidate.get("project")))
        if state.get("generation") != candidate.get("generation"):
            raise SessionError("Состояние изменилось после подготовки выборки")
        state_epoch = state.get("epoch")
        if state_epoch is not None and state_epoch != candidate.get("epoch"):
            raise SessionError("Кандидат относится к прежней эпохе состояния")
        candidate_handled = candidate["handled"]
        handled = dict(state.get("handled", {}))
        handled.update(candidate_handled)
        next_state = {
            "version": STATE_VERSION,
            "project": candidate["project"],
            "generation": int(state["generation"]) + 1,
            "baseline_at": candidate.get("baseline_at"),
            "handled": dict(sorted(handled.items())),
            "last_success_at": now_iso(),
            "adapters": ADAPTERS,
            "epoch": candidate["epoch"],
        }
        rotate_integrity_key(state_path.parent)
        atomic_json(state_path, next_state)
        candidate_path.unlink()
        print(
            json.dumps(
                {"status": "committed", "generation": next_state["generation"]}
            )
        )
        return 0
    finally:
        if lock_path.exists():
            lock_path.unlink()


def reset(args: argparse.Namespace) -> int:
    state_path = Path(args.state).expanduser()
    lock_path = state_path.with_name(state_path.name + ".lock")
    secure_parent(lock_path)
    try:
        descriptor = os.open(
            lock_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.close(descriptor)
    except FileExistsError as exc:
        raise SessionError("Состояние уже изменяется другим процессом") from exc
    except OSError as exc:
        raise SessionError(f"Не удалось заблокировать состояние: {exc}") from exc
    try:
        if state_path.exists() or state_path.is_symlink():
            try:
                value = json.loads(
                    secure_read_text(state_path, "локальное состояние")
                )
                if (
                    not isinstance(value, dict)
                    or value.get("version") != STATE_VERSION
                    or value.get("adapters") != ADAPTERS
                    or not isinstance(value.get("project"), str)
                ):
                    raise SessionError(
                        "Локальное состояние нельзя безопасно сбросить"
                    )
                rotate_integrity_key(state_path.parent)
                atomic_json(
                    state_path,
                    {
                        "version": STATE_VERSION,
                        "project": value["project"],
                        "generation": 0,
                        "baseline_at": None,
                        "handled": {},
                        "last_success_at": None,
                        "adapters": ADAPTERS,
                        "epoch": secrets.token_hex(16),
                    },
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise SessionError(f"Не удалось сбросить состояние: {exc}") from exc
            status = "reset"
        else:
            project = project_from_state_path(state_path)
            fingerprint = hashlib.sha256(str(project).encode("utf-8")).hexdigest()
            integrity_key(state_path.parent, create=True)
            rotate_integrity_key(state_path.parent)
            atomic_json(
                state_path,
                {
                    "version": STATE_VERSION,
                    "project": fingerprint,
                    "generation": 0,
                    "baseline_at": None,
                    "handled": {},
                    "last_success_at": None,
                    "adapters": ADAPTERS,
                    "epoch": secrets.token_hex(16),
                },
            )
            status = "reset"
        print(json.dumps({"status": status}))
        return 0
    finally:
        if lock_path.exists():
            lock_path.unlink()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Выбор завершённых частей локальных сессий Codex и Claude Code"
    )
    subparsers = result.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--project-root", required=True)
    prepare_parser.add_argument("--state", required=True)
    prepare_parser.add_argument("--candidate", required=True)
    prepare_parser.add_argument(
        "--mode",
        choices=("incremental", "session", "period", "all"),
        default="incremental",
    )
    prepare_parser.add_argument("--session")
    prepare_parser.add_argument("--period-start")
    prepare_parser.add_argument("--period-end")
    prepare_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    prepare_parser.add_argument("--offset", type=int, default=0)
    prepare_parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    prepare_parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=DEFAULT_MAX_FILE_BYTES,
    )
    prepare_parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    prepare_parser.add_argument(
        "--max-total-bytes",
        type=int,
        default=DEFAULT_MAX_TOTAL_BYTES,
    )
    prepare_parser.add_argument("--codex-root", action="append", default=[])
    prepare_parser.add_argument("--claude-root", action="append", default=[])
    prepare_parser.set_defaults(handler=prepare)

    commit_parser = subparsers.add_parser("commit")
    commit_parser.add_argument("--state", required=True)
    commit_parser.add_argument("--candidate", required=True)
    commit_parser.set_defaults(handler=commit)

    reset_parser = subparsers.add_parser("reset")
    reset_parser.add_argument("--state", required=True)
    reset_parser.set_defaults(handler=reset)
    return result


def main() -> int:
    args = parser().parse_args()
    numeric_limits = (
        getattr(args, "limit", 1),
        getattr(args, "max_chars", 1),
        getattr(args, "max_file_bytes", 1),
        getattr(args, "max_files", 1),
        getattr(args, "max_total_bytes", 1),
    )
    if any(value < 1 for value in numeric_limits) or getattr(args, "offset", 0) < 0:
        print("Числовые пределы должны быть положительными", file=sys.stderr)
        return 2
    if getattr(args, "mode", None) == "session" and not args.session:
        print("Для режима session нужен --session", file=sys.stderr)
        return 2
    if getattr(args, "mode", None) == "period" and not (
        args.period_start or args.period_end
    ):
        print(
            "Для режима period нужна --period-start или --period-end",
            file=sys.stderr,
        )
        return 2
    try:
        return int(args.handler(args))
    except SessionError as exc:
        print(public_error(str(exc)), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
