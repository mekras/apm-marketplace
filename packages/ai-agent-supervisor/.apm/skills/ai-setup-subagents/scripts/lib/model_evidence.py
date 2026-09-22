"""Собрать проверяемые свидетельства параметров запуска подагента."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import quote
from typing import Any


EVIDENCE_SCHEMA_VERSION = 2
CLIENT_BOUNDARY = "client_execution"
SERVER_BOUNDARY = "server_execution"


def _empty_fact(reason: str = "missing_evidence") -> dict[str, Any]:
    return {
        "value": None,
        "status": "unconfirmed",
        "reason": reason,
        "confirmation_boundary": None,
        "sources": [],
    }


def _value(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _containers(event: dict[str, Any]) -> list[tuple[dict[str, Any], list[str]]]:
    result = [(event, [])]
    payload = event.get("payload")
    if isinstance(payload, dict):
        result.append((payload, ["payload"]))
    return result


def _reference(
    source: str,
    boundary: str,
    *,
    thread_id: str | None = None,
    **details: object,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source": source,
        "confirmation_boundary": boundary,
    }
    if thread_id is not None:
        result["thread_id"] = thread_id
    result.update(details)
    return result


def _append_observation(
    observations: dict[str, list[tuple[str, dict[str, Any]]]],
    name: str,
    value: object,
    reference: dict[str, Any],
) -> None:
    normalized = _value(value)
    if normalized is not None:
        observations[name].append((normalized, reference))


def _read_jsonl(path: Path) -> tuple[list[tuple[int, dict[str, Any]]], str] | None:
    try:
        raw = path.read_bytes()
    except (OSError, UnicodeError):
        return None
    events: list[tuple[int, dict[str, Any]]] = []
    for line_number, line in enumerate(raw.splitlines(), 1):
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, UnicodeError):
            continue
        if isinstance(event, dict):
            events.append((line_number, event))
    return events, hashlib.sha256(raw).hexdigest()


def _thread_ids(events: list[tuple[int, dict[str, Any]]]) -> set[str]:
    result: set[str] = set()
    for _, event in events:
        if event.get("type") != "thread.started":
            continue
        for container, _ in _containers(event):
            thread_id = _value(container.get("thread_id"))
            if thread_id is not None:
                result.add(thread_id)
    return result


def _rollout_observations(
    path: Path,
    *,
    source: str,
    thread_id: str | None,
    observations: dict[str, list[tuple[str, dict[str, Any]]]],
    server_observations: dict[str, list[tuple[str, dict[str, Any]]]],
) -> set[str]:
    loaded = _read_jsonl(path)
    if loaded is None:
        return set()
    events, digest = loaded
    for line_number, event in events:
        event_type = event.get("type")
        for container, prefix in _containers(event):
            base = {
                "journal": str(path),
                "line": line_number,
                "sha256": digest,
                "event_type": event_type,
                "field": [],
            }
            if event_type == "turn_context":
                for name in ("model", "effort", "reasoning_effort"):
                    target = "effort" if name == "reasoning_effort" else name
                    reference = _reference(
                        source,
                        CLIENT_BOUNDARY,
                        thread_id=thread_id,
                        **{**base, "field": prefix + [name]},
                    )
                    _append_observation(observations, target, container.get(name), reference)

                collaboration = container.get("collaboration_mode")
                settings = collaboration.get("settings") if isinstance(collaboration, dict) else None
                if isinstance(settings, dict):
                    reference = _reference(
                        source,
                        CLIENT_BOUNDARY,
                        thread_id=thread_id,
                        **{
                            **base,
                            "field": prefix + ["collaboration_mode", "settings", "reasoning_effort"],
                        },
                    )
                    _append_observation(
                        observations,
                        "effort",
                        settings.get("reasoning_effort"),
                        reference,
                    )

            if event_type == "world_state":
                state = container.get("state")
                if isinstance(state, dict):
                    reference = _reference(
                        source,
                        CLIENT_BOUNDARY,
                        thread_id=thread_id,
                        **{**base, "field": prefix + ["state", "model"]},
                    )
                    _append_observation(observations, "model", state.get("model"), reference)

            server = container.get("server")
            if isinstance(server, dict):
                for name, aliases in (
                    ("model", ("model",)),
                    ("effort", ("effort", "reasoning_effort")),
                ):
                    for alias in aliases:
                        if alias not in server:
                            continue
                        reference = _reference(
                            "server",
                            SERVER_BOUNDARY,
                            thread_id=thread_id,
                            **{
                                **base,
                                "field": prefix + ["server", alias],
                            },
                        )
                        _append_observation(
                            server_observations,
                            name,
                            server.get(alias),
                            reference,
                        )
                        break

            for name, aliases in (
                ("model", ("server_model",)),
                ("effort", ("server_effort", "server_reasoning_effort")),
            ):
                for alias in aliases:
                    if alias not in container:
                        continue
                    reference = _reference(
                        "server",
                        SERVER_BOUNDARY,
                        thread_id=thread_id,
                        **{**base, "field": prefix + [alias]},
                    )
                    _append_observation(
                        server_observations,
                        name,
                        container.get(alias),
                        reference,
                    )
                    break
    return _thread_ids(events)


def _database_paths(codex_home: Path) -> list[Path]:
    if not codex_home.is_dir():
        return []
    result: set[Path] = set()
    for suffix in (".sqlite", ".sqlite3", ".db"):
        try:
            result.update(path for path in codex_home.rglob(f"*{suffix}") if path.is_file())
        except OSError:
            continue
    return sorted(result)


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sqlite_rows(
    database: Path,
    thread_id: str,
) -> tuple[list[dict[str, Any]], set[Path]]:
    rows: list[dict[str, Any]] = []
    rollout_paths: set[Path] = set()
    uri = f"file:{quote(str(database), safe='/')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except (OSError, sqlite3.Error):
        return rows, rollout_paths
    try:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        for table_row in tables:
            table = table_row[0] if table_row else None
            if not isinstance(table, str) or table.startswith("sqlite_"):
                continue
            try:
                columns = connection.execute(
                    f"PRAGMA table_info({_quoted_identifier(table)})"
                ).fetchall()
            except sqlite3.Error:
                continue
            names = {
                str(column[1]).lower(): str(column[1])
                for column in columns
                if len(column) > 1 and isinstance(column[1], str)
            }
            id_column = names.get("id")
            model_column = names.get("model")
            effort_column = names.get("reasoning_effort") or names.get("effort")
            rollout_column = names.get("rollout_path")
            if id_column is None or (model_column is None and effort_column is None):
                continue
            selected = [id_column]
            for column in (model_column, effort_column, rollout_column):
                if column is not None and column not in selected:
                    selected.append(column)
            query = (
                f"SELECT {', '.join(_quoted_identifier(column) for column in selected)} "
                f"FROM {_quoted_identifier(table)} WHERE {_quoted_identifier(id_column)} = ?"
            )
            try:
                matches = connection.execute(query, (thread_id,)).fetchall()
            except sqlite3.Error:
                continue
            for match in matches:
                values = dict(zip(selected, match, strict=False))
                rollout_path = values.get(rollout_column) if rollout_column else None
                if isinstance(rollout_path, str) and rollout_path:
                    rollout_paths.add(Path(rollout_path))
                rows.append(
                    {
                        "database": str(database),
                        "table": table,
                        "row_id": values.get(id_column),
                        "model": values.get(model_column) if model_column else None,
                        "model_field": model_column,
                        "effort": values.get(effort_column) if effort_column else None,
                        "effort_field": effort_column,
                        "rollout_path": rollout_path,
                    }
                )
    finally:
        connection.close()
    return rows, rollout_paths


def _resolve_rollout_path(path: Path, codex_home: Path, journal: Path) -> Path | None:
    candidates = [path] if path.is_absolute() else [
        codex_home / path,
        codex_home / "sessions" / path,
        journal.parent / path,
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _fact(
    observations: list[tuple[str, dict[str, Any]]],
    *,
    boundary: str,
    binding_ok: bool,
    binding_reason: str | None = None,
) -> dict[str, Any]:
    if not binding_ok:
        result = _empty_fact(binding_reason or "ambiguous_thread")
        result["sources"] = [reference for _, reference in observations]
        return result
    if not observations:
        return _empty_fact()
    values = {value for value, _ in observations}
    references = [reference for _, reference in observations]
    if len(values) != 1:
        return {
            "value": None,
            "status": "conflict",
            "reason": "conflicting_evidence",
            "confirmation_boundary": boundary,
            "sources": references,
        }
    return {
        "value": next(iter(values)),
        "status": "confirmed",
        "reason": None,
        "confirmation_boundary": boundary,
        "sources": references,
    }


def _merge_facts(client: dict[str, Any], server: dict[str, Any]) -> dict[str, Any]:
    if client["status"] == "conflict" or server["status"] == "conflict":
        return {
            "value": None,
            "status": "conflict",
            "reason": "conflicting_evidence",
            "confirmation_boundary": "multiple",
            "sources": client["sources"] + server["sources"],
        }
    if client["status"] == "confirmed" and server["status"] == "confirmed":
        if client["value"] != server["value"]:
            return {
                "value": None,
                "status": "conflict",
                "reason": "conflicting_evidence",
                "confirmation_boundary": "multiple",
                "sources": client["sources"] + server["sources"],
            }
        return {
            "value": client["value"],
            "status": "confirmed",
            "reason": None,
            "confirmation_boundary": "multiple",
            "sources": client["sources"] + server["sources"],
        }
    if client["status"] == "confirmed":
        return client
    if server["status"] == "confirmed":
        return server
    if client["reason"] not in (None, "missing_evidence"):
        return client
    return server


def _public_evidence(fact: dict[str, Any]) -> dict[str, Any] | None:
    if fact["status"] not in ("confirmed", "conflict"):
        return None
    result = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "value": fact["value"],
        "status": fact["status"],
        "confirmation_boundary": fact["confirmation_boundary"],
        "sources": fact["sources"],
    }
    if fact["reason"] is not None:
        result["reason"] = fact["reason"]
    if len(fact["sources"]) == 1:
        result.update(fact["sources"][0])
    return result


def _legacy_observation(
    header: object,
    journal: Path,
    name: str,
    observations: dict[str, list[tuple[str, dict[str, Any]]]],
) -> str | None:
    if not isinstance(header, dict):
        return None
    value = _value(header.get(name))
    evidence = header.get(f"{name}_evidence")
    if value is None and not isinstance(evidence, dict):
        return None
    if isinstance(evidence, dict) and evidence.get("schema_version") == EVIDENCE_SCHEMA_VERSION:
        return None
    if not isinstance(evidence, dict):
        return "missing_evidence"
    line = evidence.get("line")
    field = evidence.get("field")
    if (
        evidence.get("source") != "journal"
        or type(line) is not int
        or line < 1
        or not isinstance(field, list)
        or not field
        or not all(isinstance(key, str) and key for key in field)
    ):
        return "invalid_reference"
    loaded = _read_jsonl(journal)
    if loaded is None:
        return "unverifiable_reference"
    _, digest = loaded
    try:
        raw_lines = journal.read_bytes().splitlines()
        current: object = json.loads(raw_lines[line - 1])
        for key in field:
            if not isinstance(current, dict):
                raise ValueError("свидетельство указывает не на объект")
            current = current[key]
    except (OSError, IndexError, KeyError, ValueError, UnicodeError, json.JSONDecodeError):
        return "unverifiable_reference"
    if value is None or current != value:
        return "unverifiable_reference"
    observations[name].append(
        (
            value,
            _reference(
                "journal",
                CLIENT_BOUNDARY,
                **{
                    "journal": str(journal),
                    "line": line,
                    "field": field,
                    "sha256": digest,
                },
            ),
        )
    )
    return None


def _route_status(
    client: dict[str, dict[str, Any]],
    model_matches: bool | None,
    effort_matches: bool | None,
) -> str:
    if (
        client["model"]["status"] == "conflict"
        or client["effort"]["status"] == "conflict"
        or model_matches is False
        or effort_matches is False
    ):
        return "conflict" if client["model"]["status"] == "conflict" or client["effort"]["status"] == "conflict" else "mismatch"
    if client["model"]["status"] != "confirmed" or model_matches is not True:
        return "unconfirmed"
    if client["effort"]["status"] == "confirmed" and effort_matches is True:
        return "confirmed"
    return "model_only"


def collect_execution_evidence(
    journal: Path,
    *,
    header: object = None,
    assigned_model: str | None = None,
    assigned_effort: str | None = None,
    codex_home: Path | None = None,
) -> dict[str, Any]:
    loaded = _read_jsonl(journal)
    primary_events = loaded[0] if loaded is not None else []
    thread_ids = _thread_ids(primary_events)
    thread_id = next(iter(thread_ids)) if len(thread_ids) == 1 else None
    thread_status = "resolved" if len(thread_ids) == 1 else (
        "conflict" if thread_ids else "missing"
    )
    observations = {"model": [], "effort": []}
    server_observations = {"model": [], "effort": []}
    ambiguous_linked_rollout = False
    if loaded is not None:
        _rollout_observations(
            journal,
            source="codex_rollout",
            thread_id=thread_id,
            observations=observations,
            server_observations=server_observations,
        )

    legacy_reasons = {
        name: _legacy_observation(header, journal, name, observations)
        for name in ("model", "effort")
    }
    if thread_id is not None:
        configured_home = os.environ.get("CODEX_HOME")
        home = codex_home or Path(configured_home or (Path.home() / ".codex"))
        home = home.expanduser()
        linked_rollouts: set[Path] = set()
        for database in _database_paths(home):
            rows, rollout_paths = _sqlite_rows(database, thread_id)
            for rollout_path in rollout_paths:
                resolved = _resolve_rollout_path(rollout_path, home, journal)
                if resolved is not None and resolved != journal:
                    linked_rollouts.add(resolved)
            for row in rows:
                reference_base = {
                    "database": row["database"],
                    "table": row["table"],
                    "row_id": row["row_id"],
                    "fields": [],
                }
                if _value(row.get("model")) is not None:
                    reference = _reference(
                        "codex_sqlite",
                        CLIENT_BOUNDARY,
                        thread_id=thread_id,
                        **{
                            **reference_base,
                            "fields": [row.get("model_field") or "model"],
                        },
                    )
                    _append_observation(observations, "model", row["model"], reference)
                if _value(row.get("effort")) is not None:
                    reference = _reference(
                        "codex_sqlite",
                        CLIENT_BOUNDARY,
                        thread_id=thread_id,
                        **{
                            **reference_base,
                            "fields": [row.get("effort_field") or "effort"],
                        },
                    )
                    _append_observation(observations, "effort", row["effort"], reference)

        for rollout in sorted(linked_rollouts):
            linked_thread_ids = _rollout_observations(
                rollout,
                source="codex_rollout",
                thread_id=thread_id,
                observations=observations,
                server_observations=server_observations,
            )
            if linked_thread_ids and linked_thread_ids != {thread_id}:
                ambiguous_linked_rollout = True

    has_codex_rollout = any(
        reference.get("source") == "codex_rollout"
        for values in observations.values()
        for _, reference in values
    ) or any(
        reference.get("source") == "server"
        for values in server_observations.values()
        for _, reference in values
    )
    binding_ok = (
        (thread_status == "resolved" and not ambiguous_linked_rollout)
        or (thread_status == "missing" and not has_codex_rollout)
    )
    binding_reason = None if binding_ok else "ambiguous_thread"
    client_facts = {
        name: _fact(
            observations[name],
            boundary=CLIENT_BOUNDARY,
            binding_ok=binding_ok,
            binding_reason=binding_reason,
        )
        for name in ("model", "effort")
    }
    server_facts = {
        name: _fact(
            server_observations[name],
            boundary=SERVER_BOUNDARY,
            binding_ok=binding_ok,
            binding_reason=binding_reason,
        )
        for name in ("model", "effort")
    }
    facts = {
        name: _merge_facts(client_facts[name], server_facts[name])
        for name in ("model", "effort")
    }
    model_matches = (
        None
        if facts["model"]["status"] != "confirmed" or assigned_model is None
        else facts["model"]["value"] == assigned_model
    )
    effort_matches = (
        None
        if facts["effort"]["status"] != "confirmed" or assigned_effort is None
        else facts["effort"]["value"] == assigned_effort
    )
    client_model_matches = (
        None
        if client_facts["model"]["status"] != "confirmed" or assigned_model is None
        else client_facts["model"]["value"] == assigned_model
    )
    client_effort_matches = (
        None
        if client_facts["effort"]["status"] != "confirmed" or assigned_effort is None
        else client_facts["effort"]["value"] == assigned_effort
    )
    return {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "thread_id": thread_id,
        "thread_status": thread_status,
        "assigned": {
            "model": assigned_model,
            "effort": assigned_effort,
            "source": "configuration",
            "confirmation_boundary": "assignment",
        },
        "execution_evidence": {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "thread_id": thread_id,
            "client": {
                name: {
                    **client_facts[name],
                    "evidence": _public_evidence(client_facts[name]),
                }
                for name in ("model", "effort")
            },
            "server": {
                name: {
                    **server_facts[name],
                    "evidence": _public_evidence(server_facts[name]),
                }
                for name in ("model", "effort")
            },
        },
        "actual_model": facts["model"]["value"] if facts["model"]["status"] == "confirmed" else None,
        "model_status": facts["model"]["status"],
        "model_evidence": _public_evidence(facts["model"]),
        "model_evidence_reason": facts["model"]["reason"] or legacy_reasons["model"],
        "actual_effort": facts["effort"]["value"] if facts["effort"]["status"] == "confirmed" else None,
        "effort_status": facts["effort"]["status"],
        "effort_evidence": _public_evidence(facts["effort"]),
        "effort_evidence_reason": facts["effort"]["reason"] or legacy_reasons["effort"],
        "client_model_status": client_facts["model"]["status"],
        "client_effort_status": client_facts["effort"]["status"],
        "server_model_status": server_facts["model"]["status"],
        "server_effort_status": server_facts["effort"]["status"],
        "client_model_matches": client_model_matches,
        "client_effort_matches": client_effort_matches,
        "server_model_matches": (
            None
            if server_facts["model"]["status"] != "confirmed" or assigned_model is None
            else server_facts["model"]["value"] == assigned_model
        ),
        "server_effort_matches": (
            None
            if server_facts["effort"]["status"] != "confirmed" or assigned_effort is None
            else server_facts["effort"]["value"] == assigned_effort
        ),
        "model_matches": model_matches,
        "effort_matches": effort_matches,
        "client_route_status": _route_status(
            client_facts,
            client_model_matches,
            client_effort_matches,
        ),
    }


def verify_model(
    header: object,
    journal: Path,
    assigned_model: str | None = None,
    assigned_effort: str | None = None,
    codex_home: Path | None = None,
) -> dict[str, Any]:
    """Совместимый вход для запускателей старой и новой схемы свидетельств."""

    return collect_execution_evidence(
        journal,
        header=header,
        assigned_model=assigned_model,
        assigned_effort=assigned_effort,
        codex_home=codex_home,
    )
