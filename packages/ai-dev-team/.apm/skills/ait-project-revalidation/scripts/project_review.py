#!/usr/bin/env python3
"""Инвентаризация и локальное состояние полной проверки проекта."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote


STATE_SCHEMA_VERSION = 14
CLASSIFICATION_VERSION = 1
CONCEPT_CAPABILITY_ID = "skill-ait-docs-concept"
KNOWLEDGE_CAPABILITY_NAME = "kc-validation"
PROJECT_IMPACT_FILE = ".ai-dev-team/project-impact.json"
TERMINAL_STATES = {"complete", "complete_with_accepted_risks"}
PROCESS_STATES = {
    "running",
    "waiting_decision",
    "blocked",
    "interrupted",
    *TERMINAL_STATES,
}
STAGE_STATUSES = {"pending", "running", "complete"}
PARTICIPATION = {
    "check",
    "constraint",
    "preparation",
    "correction",
    "not_applicable",
}
APPLICATION_METHODS = {"command", "inspection", "review", "validation"}
APPLICATION_OUTCOMES = {"applied", "failed", "passed"}
REVIEW_DECISIONS = {"accept", "needs_human_decision", "reject", "revise"}
CHALLENGE_OUTCOMES = {"confirmed", "inconclusive", "refuted"}
OBSERVATION_RESULTS = {"not_applicable", "problem", "supports"}
CRITERION_COVERAGE = {"each_subject", "surface"}
KNOWLEDGE_REVIEW_CRITERIA = [
    {
        "id": "source-concept-fit",
        "description": (
            "Доступные источники относятся к проблеме, цели, границам "
            "и ограничениям концепции."
        ),
        "coverage": "surface",
    },
    {
        "id": "statement-consistency",
        "description": (
            "Утверждения согласованы с концепцией и другими источниками, "
            "а противоречия названы явно."
        ),
        "coverage": "surface",
    },
    {
        "id": "coverage-gaps",
        "description": (
            "Пробелы охвата и ограничения смысловых выводов установлены явно."
        ),
        "coverage": "surface",
    },
    {
        "id": "decision-value",
        "description": (
            "Ценность источников для принятия решений и их обоснования "
            "проверена."
        ),
        "coverage": "surface",
    },
]
TRANSITIONS = {
    "running": PROCESS_STATES,
    "waiting_decision": {"running", "blocked", "interrupted"},
    "blocked": {"running", "interrupted"},
    "interrupted": {"running", "blocked"},
    "complete": set(),
    "complete_with_accepted_risks": set(),
}
CLASSIFICATION = (
    Path(__file__).resolve().parents[1] / "references" / "capabilities.json"
)


class ReviewError(RuntimeError):
    """Нарушен договор полной проверки."""


def now() -> str:
    return datetime.now(UTC).isoformat()


def run_git(repo: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env={**os.environ, "LC_ALL": "C", "LANGUAGE": "C"},
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ReviewError(f"git {' '.join(arguments)}: {detail}")
    return completed.stdout


def repository_root(start: Path) -> Path:
    output = run_git(start.resolve(), "rev-parse", "--show-toplevel")
    return Path(output.decode().strip()).resolve()


def state_path(repo: Path) -> Path:
    relative = run_git(
        repo,
        "rev-parse",
        "--git-path",
        "ai-dev-team/project-review-state.json",
    ).decode().strip()
    path = Path(relative)
    if not path.is_absolute():
        path = repo / path
    return path.resolve()


def review_history_path(path: Path, state: dict[str, Any]) -> Path:
    timestamp = now().replace(":", "").replace("+", "_")
    snapshot = state.get("snapshot", {}).get("id", "unknown")[:12]
    return path.parent / "project-review-history" / f"{timestamp}-{snapshot}.json"


def file_hash(path: Path) -> str:
    if path.is_symlink():
        payload = os.readlink(path).encode("utf-8", errors="surrogateescape")
    else:
        payload = path.read_bytes()
    return hashlib.sha256(payload).hexdigest()


def repository_snapshot(repo: Path) -> dict[str, Any]:
    names_output = run_git(
        repo,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    )
    files: dict[str, str] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for raw_name in names_output.split(b"\0"):
        if not raw_name:
            continue
        name = raw_name.decode("utf-8", errors="surrogateescape")
        parts = Path(name).parts
        if ".git" in parts:
            continue
        path = repo / name
        if path.is_file() or path.is_symlink():
            files[name] = file_hash(path)
            metadata[name] = {
                "worktree_kind": "symlink" if path.is_symlink() else "file",
                "worktree_executable": (
                    False
                    if path.is_symlink()
                    else bool(path.stat().st_mode & 0o111)
                ),
                "index": [],
            }

    stage_output = run_git(repo, "ls-files", "--stage", "-z")
    for record in stage_output.split(b"\0"):
        if not record:
            continue
        try:
            raw_entry, raw_name = record.split(b"\t", 1)
            raw_mode, raw_object, raw_stage = raw_entry.split(b" ", 2)
        except ValueError as exc:
            raise ReviewError("git ls-files --stage вернул неверный формат") from exc
        name = raw_name.decode("utf-8", errors="surrogateescape")
        parts = Path(name).parts
        if ".git" in parts:
            continue
        entry = {
            "mode": raw_mode.decode(),
            "object": raw_object.decode(),
            "stage": raw_stage.decode(),
        }
        item = metadata.setdefault(
            name,
            {
                "worktree_kind": "gitlink",
                "worktree_executable": False,
                "index": [],
            },
        )
        item["index"].append(entry)

    for item in metadata.values():
        item["index"].sort(
            key=lambda entry: (
                entry["stage"],
                entry["mode"],
                entry["object"],
            ),
        )
    digest = stable_hash({"files": files, "metadata": metadata})
    return {"id": digest, "files": files, "metadata": metadata}


def content_files(repo: Path) -> set[str]:
    """Return project files eligible for ontology-defined content review.

    This intentionally does not use VCS inclusion rules.  A project ontology
    defines semantic scope; VCS is used separately for the technical snapshot.
    """
    files: set[str] = set()
    for root, directories, names in os.walk(repo):
        directories[:] = [name for name in directories if name != ".git"]
        root_path = Path(root)
        for name in names:
            path = root_path / name
            if path.is_file() or path.is_symlink():
                files.add(path.relative_to(repo).as_posix())
    return files


def snapshot_files_in_root(snapshot: dict[str, Any], root: str) -> set[str]:
    """Return files admitted by the VCS snapshot below a declared corpus root."""
    prefix = root.rstrip("/") + "/"
    return {
        reference
        for reference in snapshot["files"]
        if reference.startswith(prefix)
    }


def stable_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReviewError(f"не найден файл {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReviewError(f"ошибка JSON в {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReviewError(f"корень {path} должен быть объектом")
    return value


def classification_name(entry: dict[str, Any]) -> str:
    name = entry.get("name")
    if isinstance(name, str) and name:
        return name
    path = entry.get("path")
    if not isinstance(path, str):
        raise ReviewError("возможность должна иметь логическое имя")
    legacy = Path(path).name
    return legacy.removesuffix(".agent.md").removesuffix(".instructions.md")


def load_core_classification(path: Path | None = None) -> dict[str, Any]:
    if path is None:
        path = CLASSIFICATION
    data = load_json(path)
    if data.get("version") != CLASSIFICATION_VERSION:
        raise ReviewError("неподдерживаемая версия классификации")
    return data


def ordered_stages(state: dict[str, Any]) -> list[str]:
    configured = load_core_classification()["stages"]
    present = set(state["stages"])
    if present != set(configured):
        raise ReviewError("состав областей не совпадает с классификацией")
    return configured


def dependency_owners(lock_path: Path) -> dict[str, str]:
    if not lock_path.is_file():
        return {}
    owners: dict[str, str] = {}
    owner: str | None = None
    in_files = False
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if line.startswith("- repo_url:"):
            owner = stripped.split(":", 1)[1].strip().strip("'\"")
            in_files = False
        elif stripped == "deployed_files:":
            in_files = True
        elif stripped == "deployed_file_hashes:":
            in_files = False
        elif in_files and stripped.startswith("- ") and owner:
            owners[stripped[2:].strip().strip("'\"")] = owner
    return owners


def frontmatter_description(text: str) -> str | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    header = text[4:end]
    lines = header.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("description:"):
            continue
        value = line.split(":", 1)[1].strip()
        if value and value not in {">", "|"}:
            return value.strip("'\"")
        continuation: list[str] = []
        for nested in lines[index + 1 :]:
            if nested and not nested.startswith((" ", "\t")):
                break
            if nested.strip():
                continuation.append(nested.strip())
        return " ".join(continuation) or None
    return None


def first_paragraph(text: str) -> str | None:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end >= 0:
            text = text[end + 4 :]
    paragraphs = re.split(r"\n\s*\n", text)
    for paragraph in paragraphs:
        value = " ".join(
            line.strip()
            for line in paragraph.splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "---"))
        )
        if value:
            return value
    return None


def component_description(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if path.suffix == ".toml":
        try:
            value = tomllib.loads(text).get("description")
        except tomllib.TOMLDecodeError:
            value = None
        return value if isinstance(value, str) and value.strip() else None
    return frontmatter_description(text) or first_paragraph(text)


def candidate_components(
    repo: Path,
    classification: dict[str, Any],
) -> list[tuple[str, str, Path]]:
    candidates: list[tuple[str, str, Path]] = []
    deployed = dependency_owners(repo / "apm.lock.yaml")
    snapshot = repository_snapshot(repo)
    available = {
        repo / relative
        for relative in {*deployed, *snapshot["files"]}
        if (repo / relative).is_file()
    }
    for entry in classification["capabilities"]:
        kind = entry["kind"]
        name = classification_name(entry)
        for path in sorted(available):
            is_skill = path.name == "SKILL.md" and path.parent.name == name
            is_named_file = path.stem == name
            if path.is_file() and (
                (kind == "skill" and is_skill)
                or (kind in {"role", "rule"} and is_named_file)
            ):
                candidates.append((kind, name, path))
    known = {(kind, name, path) for kind, name, path in candidates}
    for path in sorted(available):
        if path.name == "SKILL.md":
            candidate = ("skill", path.parent.name, path)
            if candidate not in known:
                candidates.append(candidate)
    return candidates


def inventory(
    repo: Path,
    classification: Path | None = None,
) -> dict[str, Any]:
    core = load_core_classification(classification)
    core_by_key = {
        (entry["kind"], classification_name(entry)): entry
        for entry in core["capabilities"]
    }
    owners = dependency_owners(repo / "apm.lock.yaml")
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for kind, name, path in candidate_components(repo, core):
        key = (kind, name)
        relative = path.relative_to(repo).as_posix()
        item = merged.setdefault(
            key,
            {
                "id": f"{kind}:{name}",
                "kind": kind,
                "name": name,
                "paths": [],
                "origins": [],
                "descriptions": [],
                "input_hashes": {},
            },
        )
        item["paths"].append(relative)
        item["input_hashes"][relative] = file_hash(path)
        description = component_description(path)
        if description and description not in item["descriptions"]:
            item["descriptions"].append(description)
        if key in core_by_key:
            origin = "core"
        elif relative in owners:
            origin = f"dependency:{owners[relative]}"
        else:
            origin = "project"
        if origin not in item["origins"]:
            item["origins"].append(origin)

    result: list[dict[str, Any]] = []
    for key, item in sorted(merged.items()):
        core_entry = core_by_key.get(key)
        item["paths"].sort()
        item["origins"].sort()
        item["descriptions"].sort()
        component_input_hash = stable_hash(item.pop("input_hashes"))
        if core_entry:
            item["origin"] = "core"
            item["classification"] = {
                "status": "classified",
                "capability_id": core_entry["id"],
                "participation": core_entry["participation"],
                "stage": core_entry.get("stage"),
                "applicability": core_entry["applicability"],
                "purpose": core_entry["purpose"],
                "decision_paths": core_entry.get("decision_paths", []),
                "review_criteria": core_entry.get("review_criteria", []),
                "subject_discovery_required": core_entry.get(
                    "subject_discovery_required",
                    False,
                ),
                "activation_patterns": core_entry.get("activation_patterns", []),
                "required_subject_patterns": core_entry.get(
                    "required_subject_patterns",
                    [],
                ),
                "semantic_required": core_entry.get("semantic_required", False),
                "ontology_scope": core_entry.get("ontology_scope"),
            }
        else:
            dependency_origins = [
                value for value in item["origins"] if value.startswith("dependency:")
            ]
            item["origin"] = dependency_origins[0] if dependency_origins else "project"
            item["classification"] = {
                "status": "unclassified" if item["descriptions"] else "unknown",
                "capability_id": None,
                "participation": None,
                "stage": None,
                "applicability": None,
                "purpose": item["descriptions"][0] if item["descriptions"] else None,
            }
        item["component_input_hash"] = component_input_hash
        item["classification_hash"] = stable_hash(item["classification"])
        item["input_hash"] = stable_hash(
            {
                "components": component_input_hash,
                "classification": item["classification_hash"],
            },
        )
        result.append(item)
    payload = {
        "schema_version": CLASSIFICATION_VERSION,
        "classification_version": core["version"],
        "capabilities": result,
    }
    payload["fingerprint"] = stable_hash(payload)
    return payload


def workspace_id(repo: Path) -> str:
    git_dir = run_git(repo, "rev-parse", "--absolute-git-dir").decode().strip()
    return stable_hash({"repo": str(repo), "git_dir": git_dir})


def project_ontology(repo: Path) -> dict[str, Any] | None:
    path = repo / PROJECT_IMPACT_FILE
    if not path.is_file():
        return None
    graph = load_json(path)
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ReviewError("граф проекта должен содержать вершины и связи")
    by_id: dict[str, dict[str, Any]] = {}
    predecessors: dict[str, set[str]] = {}
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str):
            raise ReviewError("вершина графа должна иметь идентификатор")
        identifier = node["id"]
        if identifier in by_id:
            raise ReviewError("граф содержит повторяющийся идентификатор вершины")
        representations = node.get("representations")
        if representations is None:
            default_role = (
                "canonical"
                if node.get("authority") == "canonical"
                else "supporting"
            )
            representations = [
                {"path": item, "role": default_role}
                for item in node.get("paths", [])
            ]
        representation_items = [
            {
                "path": item["path"],
                "role": item.get("role", "supporting"),
            }
            for item in representations
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        ]
        paths = {item["path"] for item in representation_items}
        checks = node.get("checks")
        if not isinstance(checks, list) or not all(
            isinstance(item, str) for item in checks
        ):
            raise ReviewError("вершина графа должна перечислять профильные проверки")
        by_id[identifier] = {
            "id": identifier,
            "title": node.get("title", identifier),
            "kind": node.get("kind"),
            "review_stages": node.get("review_stages", []),
            "checks": checks,
            "paths": paths,
            "representations": representation_items,
        }
        predecessors[identifier] = set()
    for edge in edges:
        if not isinstance(edge, dict):
            raise ReviewError("связь графа должна быть объектом")
        source = edge.get("from")
        target = edge.get("to")
        if source not in by_id or target not in by_id:
            raise ReviewError("связь графа ссылается на неизвестную вершину")
        predecessors[target].add(source)
    return {"nodes": by_id, "predecessors": predecessors}


def upstream_nodes(
    ontology: dict[str, Any],
    targets: Iterable[str],
) -> set[str]:
    pending = list(targets)
    result: set[str] = set()
    predecessors = ontology["predecessors"]
    while pending:
        current = pending.pop()
        for predecessor in predecessors[current]:
            if predecessor not in result:
                result.add(predecessor)
                pending.append(predecessor)
    return result


def ontology_scope_for_decision(
    classification: dict[str, Any],
    ontology: dict[str, Any] | None,
    snapshot: dict[str, Any],
    repo: Path | None,
) -> dict[str, Any] | None:
    scope = classification.get("ontology_scope")
    if scope is None:
        return None
    if not isinstance(scope, dict) or not isinstance(scope.get("node_kinds"), list):
        raise ReviewError("область онтологии возможности задана неверно")
    if ontology is None:
        return {"targets": [], "prerequisites": [], "missing_graph": True}
    node_kinds = set(scope["node_kinds"])
    stage = classification.get("stage")
    candidates = {
        identifier
        for identifier, node in ontology["nodes"].items()
        if node["kind"] in node_kinds and stage in node["review_stages"]
    }
    targets = sorted(
        identifier
        for identifier in candidates
        if not any(
            identifier in ontology["predecessors"][other]
            for other in candidates
        )
    )
    if not targets:
        return {"targets": [], "prerequisites": []}
    prerequisites = sorted(upstream_nodes(ontology, targets) - set(targets))
    available_files = content_files(repo) if repo is not None else set(snapshot["files"])

    def scoped_node(identifier: str) -> dict[str, Any]:
        node = dict(ontology["nodes"][identifier])
        node["paths"] = sorted(node["paths"])
        node["subjects"] = sorted(
            reference
            for reference in available_files
            if any(
                fnmatch.fnmatch(reference, pattern)
                for pattern in node["paths"]
            )
        )
        semantic_paths = {
            item["path"]
            for item in node["representations"]
            if item["role"] != "navigation"
        }
        node["semantic_subjects"] = sorted(
            reference
            for reference in available_files
            if any(
                fnmatch.fnmatch(reference, pattern)
                for pattern in semantic_paths
            )
        )
        return node
    return {
        "targets": [scoped_node(identifier) for identifier in targets],
        "prerequisites": [scoped_node(identifier) for identifier in prerequisites],
    }


def initial_capability_decisions(
    capability_inventory: dict[str, Any],
    snapshot: dict[str, Any],
    repo: Path | None = None,
) -> dict[str, Any]:
    ontology = project_ontology(repo) if repo is not None else None
    ontology_checks = {
        check
        for node in (ontology or {"nodes": {}})["nodes"].values()
        for check in node["checks"]
    }
    decisions: dict[str, Any] = {}
    for item in capability_inventory["capabilities"]:
        classification = item["classification"]
        ontology_scope = ontology_scope_for_decision(
            classification,
            ontology,
            snapshot,
            repo,
        )
        activation_patterns = classification.get("activation_patterns", [])
        activated = any(
            fnmatch.fnmatch(reference, pattern)
            for reference in snapshot["files"]
            for pattern in activation_patterns
        )
        if item.get("name") in ontology_checks:
            activated = True
        if ontology_scope is not None:
            activated = bool(ontology_scope["targets"])
        decisions[item["id"]] = {
            "input_hash": stable_hash(
                {
                    "capability": item["input_hash"],
                    "ontology_scope": ontology_scope,
                },
            ),
            "origin": item["origin"],
            "status": classification["status"],
            "participation": classification["participation"],
            "stage": classification["stage"],
            "applicable": (
                True
                if classification.get("applicability") == "always" or activated
                else None
            ),
            "enforced_applicable": (
                classification.get("applicability") == "always" or activated
            ),
            "reason": (
                "Поставляемая классификация требует применения."
                if classification.get("applicability") == "always"
                else (
                    "В репозитории найдены предметы обязательной проверки."
                    if activated
                    else None
                )
            ),
            "decision_paths": classification.get("decision_paths", []),
            "review_criteria": classification.get("review_criteria", []),
            "subject_discovery_required": classification.get(
                "subject_discovery_required",
                False,
            ),
            "required_subject_patterns": classification.get(
                "required_subject_patterns",
                [],
            ),
            "semantic_required": classification.get("semantic_required", False),
            "ontology_scope": ontology_scope,
        }
    return decisions


def legacy_decision_matches_classification(
    previous: dict[str, Any],
    item: dict[str, Any],
) -> bool:
    classification = item["classification"]
    if previous.get("input_hash") != item.get("component_input_hash"):
        return False
    if classification.get("status") != "classified":
        return True
    return (
        previous.get("participation") == classification.get("participation")
        and previous.get("stage") == classification.get("stage")
        and previous.get("decision_paths", [])
        == classification.get("decision_paths", [])
        and previous.get("review_criteria", [])
        == classification.get("review_criteria", [])
        and previous.get("subject_discovery_required", False)
        == classification.get("subject_discovery_required", False)
    )


def new_state(
    repo: Path,
    mode: str,
    controller: str | None,
    controller_proven: bool,
) -> dict[str, Any]:
    if mode == "managed" and not controller_proven:
        raise ReviewError(
            "управляемый режим требует доказанного контроллера продолжения",
        )
    capability_inventory = inventory(repo)
    snapshot = repository_snapshot(repo)
    stages = load_core_classification()["stages"]
    timestamp = now()
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "workspace_id": workspace_id(repo),
        "repo": str(repo),
        "mode": mode,
        "controller": {
            "name": controller,
            "proven": controller_proven,
        },
        "status": "running",
        "current_stage": None,
        "next_action": (
            "Найти концепцию по указателю в корневых инструкциях и "
            "зарегистрировать предмет её проверки."
        ),
        "snapshot": snapshot,
        "capability_inventory": capability_inventory,
        "capability_decisions": initial_capability_decisions(
            capability_inventory,
            snapshot,
            repo,
        ),
        "stages": {
            stage: {
                "status": "pending",
                "input_snapshot": None,
                "capabilities": [],
            }
            for stage in stages
        },
        "findings": {},
        "concept_review": {
            "status": "pending",
            "capability": None,
            "instructions": None,
            "subjects": [],
            "evidence": [],
            "application": None,
            "outcome": None,
        },
        "knowledge_review": {
            "status": "pending",
            "root": None,
            "subjects": [],
            "evidence": [],
            "capability": None,
            "technical_application": None,
            "technical_outcome": None,
            "semantic_application": None,
            "semantic_outcome": None,
        },
        "decision_brief": None,
        "applications": {},
        "active_application": None,
        "checks": {},
        "history": [
            {
                "at": timestamp,
                "event": "initialized",
                "status": "running",
            },
        ],
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    return state


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    value["updated_at"] = now()
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_state(repo: Path) -> tuple[Path, dict[str, Any]]:
    path = state_path(repo)
    state = load_json(path)
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise ReviewError(
            "неподдерживаемая версия состояния, выполните migrate",
        )
    if state.get("workspace_id") != workspace_id(repo):
        raise ReviewError("состояние относится к другому рабочему каталогу")
    return path, state


def add_history(state: dict[str, Any], event: str, **details: Any) -> None:
    state["history"].append({"at": now(), "event": event, **details})


def require_nonterminal(state: dict[str, Any]) -> None:
    if state["status"] in TERMINAL_STATES:
        raise ReviewError(
            "конечное состояние неизменяемо, начните новый цикл через restart",
        )


def set_next(state: dict[str, Any], action: str) -> None:
    require_nonterminal(state)
    state["next_action"] = action
    add_history(state, "next_action", action=action)


def classify_capability(state: dict[str, Any], args: argparse.Namespace) -> None:
    require_nonterminal(state)
    if args.id not in state["capability_decisions"]:
        raise ReviewError(f"неизвестная возможность {args.id}")
    if args.participation not in PARTICIPATION:
        raise ReviewError(f"неизвестный вид участия {args.participation}")
    if args.stage and args.stage not in state["stages"]:
        raise ReviewError(f"неизвестная область {args.stage}")
    if args.participation == "not_applicable" and args.applicable != "no":
        raise ReviewError("not_applicable требует applicable no")
    if args.applicable == "yes" and not args.stage:
        raise ReviewError("применимая возможность требует область")
    if not args.reason or not args.reason.strip():
        raise ReviewError("классификация требует непустое основание")
    decision = state["capability_decisions"][args.id]
    if decision.get("enforced_applicable") and args.applicable != "yes":
        raise ReviewError(
            "обязательную или обнаруженную проверку нельзя объявить "
            "неприменимой",
        )
    required_subjects = required_subjects_for_decision(
        decision,
        state["snapshot"],
    )
    if required_subjects and args.applicable != "yes":
        raise ReviewError(
            "обнаруженный предмет требует профильную смысловую проверку: "
            + ", ".join(sorted(required_subjects)[:10]),
        )
    previous_stage = decision.get("stage")
    decision.update(
        {
            "status": "classified",
            "participation": args.participation,
            "stage": args.stage,
            "applicable": args.applicable == "yes",
            "reason": args.reason.strip(),
        },
    )
    if args.applicable == "unknown":
        decision["status"] = "unknown"
        decision["applicable"] = None
    if previous_stage and previous_stage in state["stages"]:
        previous_capabilities = state["stages"][previous_stage]["capabilities"]
        if args.id in previous_capabilities:
            previous_capabilities.remove(args.id)
    if decision["applicable"] and args.stage:
        capabilities = state["stages"][args.stage]["capabilities"]
        if args.id not in capabilities:
            capabilities.append(args.id)
            capabilities.sort()
        state["stages"][args.stage]["status"] = "pending"
        state["stages"][args.stage]["input_snapshot"] = None
        if state.get("current_stage") == args.stage:
            state["current_stage"] = None
    unresolved = [
        identifier
        for identifier, value in state["capability_decisions"].items()
        if value["status"] != "classified" or value["applicable"] is None
    ]
    if (
        state["status"] == "blocked"
        and state.get("next_action")
        == "Классифицировать изменившиеся возможности."
        and not unresolved
    ):
        state["status"] = "running"
        state["next_action"] = (
            "Оценить влияние классифицированных возможностей и выбрать "
            "наиболее ценную следующую работу."
        )
    add_history(state, "capability_classified", capability=args.id)


def problem_observation_ids(
    application: dict[str, Any],
) -> set[str]:
    return {
        observation["id"]
        for observation in application.get("observations", [])
        if observation.get("result") == "problem"
    }


def linked_problem_observation_ids(
    state: dict[str, Any],
    application_id: str,
) -> set[str]:
    linked: set[str] = set()
    for finding in state["findings"].values():
        if finding.get("source_application") == application_id:
            linked.update(finding.get("observation_ids", []))
    return linked


def record_finding(state: dict[str, Any], args: argparse.Namespace) -> None:
    require_nonterminal(state)
    if args.id in state["findings"]:
        raise ReviewError(f"проблема {args.id} уже существует")
    if args.stage not in state["stages"]:
        raise ReviewError(f"неизвестная область {args.stage}")
    if not args.summary or not args.summary.strip():
        raise ReviewError("проблема требует непустое описание")
    if not args.evidence or any(not value.strip() for value in args.evidence):
        raise ReviewError("проблема требует хотя бы одно непустое свидетельство")
    observation_ids = sorted(set(getattr(args, "observation", [])))
    source_application = None
    if observation_ids:
        source_application = state.get("active_application")
        application = state["applications"].get(source_application)
        if not application or application["status"] != "running":
            raise ReviewError(
                "наблюдение можно связать только с активным применением",
            )
        available = problem_observation_ids(application)
        unknown = sorted(set(observation_ids) - available)
        if unknown:
            raise ReviewError(
                "проблема ссылается не на problem-наблюдения: "
                + ", ".join(unknown),
            )
    required_capability_paths = required_capabilities_for_paths(
        state,
        args.allowed_path,
    )
    state["findings"][args.id] = {
        "stage": args.stage,
        "summary": args.summary.strip(),
        "blocking": args.blocking,
        "evidence": [value.strip() for value in args.evidence],
        "group": args.group,
        "allowed_paths": sorted(set(args.allowed_path)),
        "verification": args.verification,
        "source_application": source_application,
        "observation_ids": observation_ids,
        "required_capability_paths": required_capability_paths,
        "status": "open",
        "decision": None,
    }
    if args.blocking:
        missing = unready_finding_capabilities(state, [args.id])
        if missing:
            state["status"] = "running"
            state["next_action"] = (
                "Применить связанные проверки перед решением по проблеме "
                f"{args.id}: {', '.join(missing)}."
            )
        else:
            state["status"] = "running"
            state["next_action"] = (
                "Подготовить самодостаточный запрос решения по проблеме "
                f"{args.id}."
            )
    add_history(state, "finding_recorded", finding=args.id)


def required_capabilities_for_paths(
    state: dict[str, Any],
    paths: Iterable[str],
) -> dict[str, list[str]]:
    required: dict[str, list[str]] = {}
    for identifier, decision in state["capability_decisions"].items():
        patterns = decision.get("decision_paths", [])
        if not patterns:
            continue
        if decision["status"] != "classified" or decision["applicable"] is None:
            raise ReviewError(
                "не классифицирована связанная проверка поверхности: "
                + identifier,
            )
        if decision["applicable"] is not True:
            continue
        matched = sorted(
            {
                path
                for path in paths
                if any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)
            },
        )
        if matched:
            required[identifier] = matched
    return required


def completed_capability_applications(
    state: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    completed: dict[str, list[dict[str, Any]]] = {}
    for application in state["applications"].values():
        capability = application.get("capability")
        if not capability or application.get("status") != "complete":
            continue
        decision = state["capability_decisions"].get(capability)
        if not decision:
            continue
        if application.get("capability_input_hash") != decision["input_hash"]:
            continue
        if application.get("input_snapshot") != state["snapshot"]["id"]:
            continue
        if application.get("completed_snapshot") != state["snapshot"]["id"]:
            continue
        if not application.get("evidence_fingerprint"):
            continue
        if decision["participation"] == "check" and (
            not application.get("coverage")
            or not application.get("claims")
        ):
            continue
        if application.get("semantic_trace_required") and (
            application.get("semantic_contract_version") != 3
            or not application.get("subject_scope")
            or not application.get("observations")
        ):
            continue
        completed.setdefault(capability, []).append(application)
    return completed


def application_covers_paths(
    application: dict[str, Any],
    paths: Iterable[str],
) -> bool:
    covered = {
        item["reference"]
        for item in application.get("subject_scope", [])
    }
    covered.update(application.get("subject_artifacts", []))
    return set(paths).issubset(covered)


def knowledge_review_covers_paths(
    state: dict[str, Any],
    capability: str | None,
    paths: Iterable[str],
) -> bool:
    """Return whether the proven corpus barrier covers the requested paths.

    The technical corpus phase inventories every discovered corpus artifact,
    while the semantic phase deliberately focuses on source and statement
    representations.  A proven two-phase corpus review is therefore evidence
    for an ontology prerequisite that covers the whole discovered corpus.
    """
    knowledge = state.get("knowledge_review")
    if not isinstance(knowledge, dict):
        return False
    if capability != knowledge.get("capability"):
        return False
    if not knowledge_review_is_proven(state):
        return False
    covered = {
        item["reference"]
        for item in knowledge.get("subjects", [])
        if item.get("reference")
    }
    return set(paths).issubset(covered)


def unready_finding_capabilities(
    state: dict[str, Any],
    finding_ids: Iterable[str],
) -> list[str]:
    completed = completed_capability_applications(state)
    missing: set[str] = set()
    for finding_id in finding_ids:
        finding = state["findings"][finding_id]
        for capability, paths in finding.get(
            "required_capability_paths",
            {},
        ).items():
            applications = completed.get(capability, [])
            if not any(
                application_covers_paths(application, paths)
                for application in applications
            ):
                missing.add(capability)
    return sorted(missing)


def refresh_finding_requirements(
    state: dict[str, Any],
    finding_ids: Iterable[str],
) -> None:
    for finding_id in finding_ids:
        finding = state["findings"][finding_id]
        finding["required_capability_paths"] = required_capabilities_for_paths(
            state,
            finding["allowed_paths"],
        )


def finding_group_targets(
    state: dict[str, Any],
    finding_id: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    finding = state["findings"].get(finding_id)
    if not finding:
        raise ReviewError(f"неизвестная проблема {finding_id}")
    group = finding.get("group")
    targets = {
        identifier: value
        for identifier, value in state["findings"].items()
        if identifier == finding_id or (group and value.get("group") == group)
    }
    return finding, targets


def required_human_text(value: str | None, label: str) -> str:
    if value is None or not value.strip():
        raise ReviewError(f"запрос решения требует поле «{label}»")
    return value.strip()


def complete_sentence(value: str) -> str:
    text = value.strip()
    if text.endswith((".", "!", "?", "…")):
        return text
    return text + "."


def render_decision_message(brief: dict[str, Any]) -> str:
    problems = " ".join(
        complete_sentence(problem) for problem in brief["problems"]
    )
    checked_subject = complete_sentence(brief["checked_subject"])
    relation = complete_sentence(brief["relation"])
    return (
        f"{complete_sentence(brief['review_context'])}\n\n"
        f"Проблема: Проверялся {checked_subject} {relation} {problems}\n\n"
        f"Почему это важно: {complete_sentence(brief['impact'])}\n\n"
        f"Предлагаю: {complete_sentence(brief['proposed_change'])}\n\n"
        f"Нужно решение: {complete_sentence(brief['decision_question'])}\n\n"
        "Подробности проверки и границы изменения покажу по вашему запросу."
    )


def prepare_decision(
    state: dict[str, Any],
    args: argparse.Namespace,
    repo: Path,
) -> dict[str, Any]:
    if state["status"] != "running":
        raise ReviewError(
            "запрос решения готовится только из состояния running",
        )
    current_snapshot = repository_snapshot(repo)
    if current_snapshot["id"] != state["snapshot"]["id"]:
        raise ReviewError(
            "область Git изменилась, перед запросом решения выполните refresh",
        )
    _, targets = finding_group_targets(state, args.finding)
    refresh_finding_requirements(state, targets)
    missing = unready_finding_capabilities(state, targets)
    if missing:
        raise ReviewError(
            "группа не готова к решению, не применены связанные проверки "
            "поверхности: " + ", ".join(missing),
        )
    open_targets = {
        identifier: value
        for identifier, value in targets.items()
        if value["status"] == "open"
    }
    if not open_targets:
        raise ReviewError("в группе нет открытых проблем для решения")
    if len(open_targets) != len(targets):
        raise ReviewError(
            "группа содержит проблемы с разными состояниями и требует пересборки",
        )
    problems = [
        required_human_text(value, "понятное описание проблемы")
        for value in args.problem
    ]
    if not problems:
        raise ReviewError(
            "запрос решения требует хотя бы одно понятное описание проблемы",
        )
    allowed_paths = sorted(
        {
            path
            for finding in open_targets.values()
            for path in finding["allowed_paths"]
        },
    )
    verifications = sorted(
        {
            finding["verification"].strip()
            for finding in open_targets.values()
            if finding.get("verification")
            and finding["verification"].strip()
        },
    )
    brief = {
        "finding_ids": sorted(open_targets),
        "input_snapshot": state["snapshot"]["id"],
        "review_context": required_human_text(
            args.review_context,
            "контекст полной проверки",
        ),
        "checked_subject": required_human_text(
            args.checked_subject,
            "понятный предмет текущей проверки",
        ),
        "relation": required_human_text(
            args.relation,
            "связь предмета с полной проверкой",
        ),
        "problems": problems,
        "impact": required_human_text(
            args.impact,
            "последствия проблем",
        ),
        "proposed_change": required_human_text(
            args.proposed_change,
            "предлагаемое изменение",
        ),
        "decision_question": required_human_text(
            args.decision_question,
            "вопрос владельцу",
        ),
        "allowed_paths": allowed_paths,
        "verifications": verifications,
        "created_at": now(),
    }
    brief["message"] = render_decision_message(brief)
    state["decision_brief"] = brief
    state["status"] = "waiting_decision"
    state["next_action"] = brief["decision_question"]
    add_history(
        state,
        "decision_brief_prepared",
        findings=brief["finding_ids"],
        input_snapshot=brief["input_snapshot"],
    )
    return brief


def record_decision(state: dict[str, Any], args: argparse.Namespace) -> None:
    if state["status"] != "waiting_decision":
        raise ReviewError(
            "решение допустимо только после самодостаточного запроса владельцу",
        )
    brief = state.get("decision_brief")
    if not brief:
        raise ReviewError("для решения отсутствует подготовленный запрос")
    finding, targets = finding_group_targets(state, args.finding)
    if sorted(targets) != brief.get("finding_ids"):
        raise ReviewError("решение не соответствует подготовленной группе")
    if brief.get("input_snapshot") != state["snapshot"]["id"]:
        raise ReviewError("запрос решения относится к другому снимку")
    group = finding.get("group")
    refresh_finding_requirements(state, targets)
    missing = unready_finding_capabilities(state, targets)
    if missing:
        raise ReviewError(
            "группа не готова к решению, не применены связанные проверки "
            "поверхности: " + ", ".join(missing),
        )
    if args.decision in {"accept", "defer"} and any(
        value["blocking"] for value in targets.values()
    ):
        raise ReviewError("блокирующую проблему нельзя принять или отложить")
    if args.decision in {"accept", "defer"} and (
        not args.reason
        or not args.reason.strip()
        or not args.revisit_condition
        or not args.revisit_condition.strip()
    ):
        raise ReviewError(
            "принятие риска требует причины и условия пересмотра",
        )
    if args.decision == "fix" and any(
        not value["allowed_paths"]
        or not value["verification"]
        or not value["verification"].strip()
        for value in targets.values()
    ):
        raise ReviewError(
            "исправление требует разрешённых путей и способа проверки",
        )
    if args.decision == "not_applicable" and (
        not args.reason or not args.reason.strip()
    ):
        raise ReviewError("неприменимость требует причины")
    for value in targets.values():
        value["decision"] = {
            "value": args.decision,
            "reason": args.reason.strip() if args.reason else None,
            "revisit_condition": (
                args.revisit_condition.strip()
                if args.revisit_condition
                else None
            ),
            "at": now(),
        }
        if args.decision == "fix":
            value["status"] = "approved"
        elif args.decision in {"accept", "defer"}:
            value["status"] = "accepted"
        else:
            value["status"] = "not_applicable"
    if args.decision == "fix":
        state["status"] = "running"
        state["next_action"] = (
            f"Исправить группу {group}."
            if group
            else f"Исправить проблему {args.finding}."
        )
    elif args.decision in {"accept", "defer"}:
        state["status"] = "running"
    else:
        state["status"] = "running"
    state["decision_brief"] = None
    add_history(
        state,
        "decision_recorded",
        finding=args.finding,
        group=group,
        targets=sorted(targets),
        decision=args.decision,
    )


def inventory_item(state: dict[str, Any], capability: str) -> dict[str, Any]:
    for item in state["capability_inventory"]["capabilities"]:
        if item["id"] == capability:
            return item
    raise ReviewError(f"возможность {capability} отсутствует в инвентаризации")


def required_subjects_for_decision(
    decision: dict[str, Any],
    snapshot: dict[str, Any],
) -> set[str]:
    ontology_scope = decision.get("ontology_scope")
    if ontology_scope is not None:
        return {
            subject
            for node in ontology_scope["targets"]
            for subject in node.get("semantic_subjects", node["subjects"])
        }
    patterns = decision.get("required_subject_patterns", [])
    return {
        reference
        for reference in snapshot["files"]
        if any(fnmatch.fnmatch(reference, pattern) for pattern in patterns)
    }


def unverified_ontology_prerequisites(
    state: dict[str, Any],
    decision: dict[str, Any],
) -> list[str]:
    scope = decision.get("ontology_scope")
    if scope is None:
        return []
    identifiers_by_name = {
        item["name"]: item["id"]
        for item in state["capability_inventory"]["capabilities"]
    }
    completed = completed_capability_applications(state)
    missing: list[str] = []
    for node in scope["prerequisites"]:
        paths = node.get("semantic_subjects", node["subjects"])
        if not paths:
            missing.append(f"{node['title']} (нет обнаруженных представлений)")
            continue
        for check in node["checks"]:
            identifier = identifiers_by_name.get(check)
            applications = completed.get(identifier, []) if identifier else []
            if check == "kc-impact-audit":
                audited_changed_scope = any(
                    application.get("outcome") == "passed"
                    and application.get("subject_artifacts")
                    for application in applications
                )
                if not audited_changed_scope:
                    missing.append(f"{node['title']} ({check})")
                continue
            application_covers_scope = any(
                application.get("outcome") == "passed"
                and application_covers_paths(application, paths)
                for application in applications
            )
            if not application_covers_scope and not knowledge_review_covers_paths(
                state,
                identifier,
                paths,
            ):
                missing.append(f"{node['title']} ({check})")
    return sorted(set(missing))


def concept_capability(state: dict[str, Any]) -> str:
    matches = [
        item["id"]
        for item in state["capability_inventory"]["capabilities"]
        if item.get("classification", {}).get("capability_id")
        == CONCEPT_CAPABILITY_ID
    ]
    if len(matches) != 1:
        raise ReviewError(
            "полная проверка требует ровно одну доступную возможность "
            "проверки концепции",
        )
    identifier = matches[0]
    decision = state["capability_decisions"].get(identifier)
    if (
        not decision
        or decision.get("status") != "classified"
        or decision.get("applicable") is not True
        or decision.get("participation") != "check"
        or decision.get("stage") != "requirements"
    ):
        raise ReviewError(
            "возможность проверки концепции должна быть обязательной "
            "проверкой области requirements",
        )
    return identifier


def knowledge_capability(state: dict[str, Any]) -> str:
    matches = [
        item["id"]
        for item in state["capability_inventory"]["capabilities"]
        if item.get("name") == KNOWLEDGE_CAPABILITY_NAME
    ]
    if len(matches) != 1:
        raise ReviewError(
            "полная проверка требует ровно одну доступную возможность "
            "проверки корпуса знаний",
        )
    identifier = matches[0]
    decision = state["capability_decisions"].get(identifier)
    if (
        not decision
        or decision.get("status") != "classified"
        or decision.get("applicable") is not True
        or decision.get("participation") != "check"
        or decision.get("stage") != "repository"
    ):
        raise ReviewError(
            "возможность проверки корпуса знаний должна быть обязательной "
            "проверкой области repository",
        )
    return identifier


def repository_artifact(
    repo: Path,
    snapshot: dict[str, Any],
    reference: str,
    purpose: str,
) -> tuple[str, str]:
    path = artifact_path(repo, reference)
    try:
        relative = path.relative_to(repo).as_posix()
    except ValueError as exc:
        raise ReviewError(f"{purpose} находится вне репозитория") from exc
    if relative not in content_files(repo) or not path.is_file():
        raise ReviewError(
            f"{purpose} недоступен в рабочем дереве проекта: {reference}",
        )
    return relative, file_hash(path)


def record_concept_discovery(
    state: dict[str, Any],
    args: argparse.Namespace,
    repo: Path,
) -> None:
    require_nonterminal(state)
    if state.get("active_application"):
        raise ReviewError(
            "нельзя менять результат поиска концепции при активном применении",
        )
    concept = state.get("concept_review")
    if not isinstance(concept, dict):
        raise ReviewError("состояние не содержит барьер проверки концепции")
    if concept.get("status") not in {"pending", "blocked"}:
        raise ReviewError("результат поиска концепции уже зарегистрирован")
    evidence = [value.strip() for value in args.evidence if value.strip()]
    if not evidence:
        raise ReviewError("поиск концепции требует проверяемого свидетельства")
    if not args.instructions:
        raise ReviewError("поиск концепции требует корневые инструкции")
    current = repository_snapshot(repo)
    if current["id"] != state["snapshot"]["id"]:
        raise ReviewError(
            "область Git изменилась до регистрации поиска концепции, "
            "выполните refresh",
        )
    instructions, instructions_hash = repository_artifact(
        repo,
        current,
        args.instructions,
        "корневые инструкции",
    )
    if Path(instructions).parent != Path("."):
        raise ReviewError("указатель на концепцию должен быть в корне проекта")
    try:
        instruction_text = (repo / instructions).read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewError("корневые инструкции должны быть в UTF-8") from exc

    subjects: list[dict[str, str]] = []
    seen: set[str] = set()
    for reference in args.concept:
        relative, digest = repository_artifact(
            repo,
            current,
            reference,
            "документ концепции",
        )
        if relative in seen:
            continue
        seen.add(relative)
        subjects.append({"reference": relative, "sha256": digest})

    if args.result == "missing" and subjects:
        raise ReviewError("результат missing не допускает найденные документы")
    if args.result == "ambiguous" and len(subjects) < 2:
        raise ReviewError(
            "неоднозначность требует хотя бы два найденных представления",
        )
    if args.result in {"missing", "ambiguous"}:
        concept.update(
            {
                "status": "blocked",
                "capability": None,
                "instructions": {
                    "reference": instructions,
                    "sha256": instructions_hash,
                },
                "subjects": subjects,
                "evidence": evidence,
                "application": None,
                "outcome": args.result,
            },
        )
        state["status"] = "blocked"
        state["next_action"] = (
            "Сформулировать концепцию проекта и закрепить однозначный "
            "указатель в корневых инструкциях."
            if args.result == "missing"
            else "Уточнить однозначный указатель на концепцию проекта."
        )
        add_history(
            state,
            "concept_discovery_blocked",
            result=args.result,
            evidence=evidence,
            instructions=instructions,
            subjects=[item["reference"] for item in subjects],
        )
        return
    if not subjects:
        raise ReviewError("найденная концепция требует хотя бы один предмет")
    overview = subjects[0]["reference"]
    if overview not in instruction_text:
        raise ReviewError(
            "корневые инструкции не содержат точный путь к обзору концепции: "
            + overview,
        )

    identifier = concept_capability(state)
    concept.update(
        {
            "status": "located",
            "capability": identifier,
            "instructions": {
                "reference": instructions,
                "sha256": instructions_hash,
            },
            "subjects": subjects,
            "evidence": evidence,
            "application": None,
            "outcome": None,
        },
    )
    state["status"] = "running"
    state["next_action"] = (
        "Содержательно проверить найденную концепцию до любой другой "
        "предметной работы."
    )
    add_history(
        state,
        "concept_located",
        instructions=instructions,
        subjects=[item["reference"] for item in subjects],
        capability=identifier,
    )


def record_knowledge_discovery(
    state: dict[str, Any],
    args: argparse.Namespace,
    repo: Path,
) -> None:
    require_nonterminal(state)
    if not concept_review_is_proven(state):
        raise ReviewError(
            "корпус знаний проверяется только после успешной проверки концепции",
        )
    if state.get("active_application"):
        raise ReviewError(
            "нельзя менять результат поиска корпуса при активном применении",
        )
    knowledge = state.get("knowledge_review")
    if not isinstance(knowledge, dict):
        raise ReviewError("состояние не содержит барьер проверки корпуса знаний")
    if knowledge.get("status") not in {"pending", "blocked"}:
        raise ReviewError("результат поиска корпуса уже зарегистрирован")
    evidence = [value.strip() for value in args.evidence if value.strip()]
    if not evidence:
        raise ReviewError("поиск корпуса требует проверяемого свидетельства")
    current = repository_snapshot(repo)
    if current["id"] != state["snapshot"]["id"]:
        raise ReviewError(
            "область Git изменилась до регистрации корпуса, выполните refresh",
        )
    if args.result == "absent":
        if args.root:
            raise ReviewError("результат absent не допускает корень корпуса")
        knowledge.update(
            {
                "status": "absent",
                "root": None,
                "subjects": [],
                "evidence": evidence,
                "capability": None,
                "technical_application": None,
                "technical_outcome": None,
                "semantic_application": None,
                "semantic_outcome": None,
            },
        )
        state["next_action"] = (
            "Зафиксировать отсутствие корпуса как ограничение и выбрать "
            "наиболее ценную следующую работу."
        )
        add_history(state, "knowledge_corpus_absent", evidence=evidence)
        return
    if not args.root:
        raise ReviewError("найденный корпус требует путь к его корню")
    root_path = artifact_path(repo, args.root)
    try:
        root = root_path.relative_to(repo).as_posix()
    except ValueError as exc:
        raise ReviewError("корень корпуса находится вне репозитория") from exc
    prefix = root.rstrip("/") + "/"
    subjects = [
        {
            "reference": reference,
            "sha256": file_hash(repo / reference),
        }
        for reference in sorted(snapshot_files_in_root(current, root))
    ]
    if not root_path.is_dir() or not subjects:
        raise ReviewError(
            "корень корпуса не содержит файлов рабочего дерева",
        )
    identifier = knowledge_capability(state)
    knowledge.update(
        {
            "status": "located",
            "root": root,
            "subjects": subjects,
            "evidence": evidence,
            "capability": identifier,
            "technical_application": None,
            "technical_outcome": None,
            "semantic_application": None,
            "semantic_outcome": None,
        },
    )
    state["next_action"] = (
        "Выполнить минимальный технический допуск корпуса знаний."
    )
    add_history(
        state,
        "knowledge_corpus_located",
        root=root,
        files=len(subjects),
        capability=identifier,
    )


MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\((<[^>]+>|[^)\s]+)")


def linked_markdown_subjects(
    repo: Path,
    index_reference: str,
    current: dict[str, Any],
) -> list[str]:
    index_path = artifact_path(repo, index_reference)
    try:
        text = index_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewError(
            f"индекс предметной области должен быть UTF-8: {index_reference}",
        ) from exc
    linked: set[str] = set()
    for match in MARKDOWN_LINK.finditer(text):
        raw_target = match.group(1).strip("<>")
        target = unquote(raw_target.split("#", 1)[0])
        if not target or "://" in target or target.startswith(("mailto:", "/")):
            continue
        path = (index_path.parent / target).resolve()
        try:
            relative = path.relative_to(repo).as_posix()
        except ValueError:
            continue
        if path.suffix.lower() != ".md":
            continue
        if not path.is_file():
            raise ReviewError(
                "индекс предметной области ссылается на отсутствующий "
                f"Markdown-файл: {relative}",
            )
        linked.add(relative)
    return sorted(linked)


def resolve_subject_scope(
    repo: Path,
    current: dict[str, Any],
    explicit: Iterable[str],
    indexes: Iterable[str],
    patterns: Iterable[str],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    references: set[str] = set()
    index_links: dict[str, list[str]] = {}

    def add_reference(reference: str) -> str:
        path = artifact_path(repo, reference)
        try:
            relative = path.relative_to(repo).as_posix()
        except ValueError as exc:
            raise ReviewError(
                f"предмет проверки находится вне репозитория: {reference}",
            ) from exc
        if not path.is_file():
            raise ReviewError(
                "предмет проверки отсутствует в рабочем дереве: "
                f"{reference}",
            )
        references.add(relative)
        return relative

    explicit_items = [add_reference(value) for value in explicit]
    index_items: list[str] = []
    for value in indexes:
        relative = add_reference(value)
        index_items.append(relative)
        links = linked_markdown_subjects(repo, relative, current)
        index_links[relative] = links
        references.update(links)

    pattern_items: dict[str, list[str]] = {}
    for pattern in patterns:
        matches = sorted(
            path
            for path in content_files(repo)
            if fnmatch.fnmatchcase(path, pattern)
            and (repo / path).is_file()
        )
        if not matches:
            raise ReviewError(
                f"шаблон предметной области не нашёл файлов: {pattern}",
            )
        pattern_items[pattern] = matches
        references.update(matches)

    scope = [
        {
            "reference": relative,
            "sha256": file_hash(repo / relative),
        }
        for relative in sorted(references)
    ]
    discovery = {
        "explicit": sorted(set(explicit_items)),
        "indexes": sorted(set(index_items)),
        "index_links": index_links,
        "patterns": pattern_items,
    }
    return scope, discovery


def parse_subject_types(
    values: Iterable[str],
    subject_scope: Iterable[dict[str, str]],
    criteria: Iterable[dict[str, Any]],
) -> dict[str, str]:
    """Validate the semantic table declared for a mixed subject surface."""
    typed_criteria = [item for item in criteria if item.get("subject_types")]
    if not typed_criteria:
        if list(values):
            raise ReviewError(
                "семантические типы предметов допустимы только для "
                "критериев с ограниченной применимостью",
            )
        return {}
    types: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ReviewError(
                "семантический тип предмета задаётся как <файл>=<тип>",
            )
        reference, semantic_type = value.rsplit("=", 1)
        if not reference.strip() or not semantic_type.strip():
            raise ReviewError(
                "семантический тип предмета задаётся как <файл>=<тип>",
            )
        if reference in types:
            raise ReviewError(
                "для предмета задано несколько семантических типов: "
                + reference,
            )
        types[reference] = semantic_type.strip()
    subjects = {item["reference"] for item in subject_scope}
    if set(types) != subjects:
        missing = sorted(subjects - set(types))
        extra = sorted(set(types) - subjects)
        details = []
        if missing:
            details.append("не указаны: " + ", ".join(missing))
        if extra:
            details.append("не входят в область: " + ", ".join(extra))
        raise ReviewError(
            "семантическая таблица должна охватывать каждый предмет ровно один "
            "раз: " + "; ".join(details),
        )
    allowed = {
        semantic_type
        for criterion in typed_criteria
        for semantic_type in criterion["subject_types"]
    }
    unknown = sorted(set(types.values()) - allowed)
    if unknown:
        raise ReviewError(
            "семантическая таблица содержит тип без применимого критерия: "
            + ", ".join(unknown),
        )
    return types


def criterion_subjects(
    application: dict[str, Any],
    criterion: dict[str, Any],
) -> set[str]:
    types = criterion.get("subject_types")
    if not types:
        return {
            item["reference"]
            for item in application.get("subject_scope", [])
        }
    return {
        item["reference"]
        for item in application.get("subject_scope", [])
        if item.get("semantic_type") in types
    }


def start_application(
    state: dict[str, Any],
    args: argparse.Namespace,
    repo: Path,
) -> None:
    concept = state.get("concept_review")
    if not isinstance(concept, dict):
        raise ReviewError("состояние не содержит барьер проверки концепции")
    concept_status = concept.get("status")
    concept_proven = concept_review_is_proven(state)
    knowledge = state.get("knowledge_review")
    if not isinstance(knowledge, dict):
        raise ReviewError("состояние не содержит барьер проверки корпуса знаний")
    knowledge_status = knowledge.get("status")
    knowledge_proven = knowledge_review_is_proven(state)
    knowledge_phase = getattr(args, "knowledge_phase", None)
    if knowledge_phase is not None:
        if knowledge_phase not in {"technical", "semantic"}:
            raise ReviewError("неизвестная фаза проверки корпуса знаний")
        if knowledge_status == "absent":
            raise ReviewError(
                "--knowledge-phase недопустим, когда корпус знаний отсутствует",
            )
        if (
            args.capability != knowledge.get("capability")
            or args.capability != knowledge_capability(state)
            or args.stage != "repository"
        ):
            raise ReviewError(
                "фаза корпуса доступна только возможности kc-validation "
                "в области repository",
            )
        if not knowledge.get("root") or not knowledge.get("subjects"):
            raise ReviewError(
                "фаза корпуса требует найденный корень и состав корпуса",
            )
    is_concept_attempt = (
        args.capability is not None
        and args.capability == concept.get("capability")
    )
    is_concept_finding = False
    if args.finding:
        concept_finding = state.get("findings", {}).get(args.finding)
        is_concept_finding = bool(
            concept_finding
            and concept_finding.get("source_application")
            == concept.get("application")
        )
    if not concept_proven:
        if concept_status not in {"located", "failed"}:
            raise ReviewError(
                "до первого применения найдите концепцию по корневому "
                "указателю",
            )
        if concept_status == "located" and not is_concept_attempt:
            raise ReviewError(
                "первым применением должна быть проверка найденной концепции",
            )
        if (
            concept_status == "failed"
            and not is_concept_attempt
            and not is_concept_finding
        ):
            raise ReviewError(
                "после проблемы концепции допустимы только её исправление "
                "и повторная проверка",
            )
        if is_concept_attempt and (
            args.stage != "requirements" or args.method != "review"
        ):
            raise ReviewError(
                "первая проверка концепции требует область requirements "
                "и способ review",
            )
    elif not knowledge_proven:
        is_knowledge_attempt = (
            args.capability is not None
            and args.capability == knowledge.get("capability")
        )
        is_knowledge_finding = False
        if args.finding:
            finding = state.get("findings", {}).get(args.finding)
            knowledge_applications = {
                knowledge.get("technical_application"),
                knowledge.get("semantic_application"),
            }
            is_knowledge_finding = bool(
                finding
                and finding.get("source_application") in knowledge_applications
            )
        if knowledge_status == "pending":
            raise ReviewError(
                "вторым этапом найдите и зарегистрируйте корпус знаний",
            )
        if knowledge_status in {"located", "technical_running"}:
            required_phase = "technical"
        elif knowledge_status in {
            "admitted",
            "admitted_with_limits",
            "semantic_running",
        }:
            required_phase = "semantic"
        elif knowledge_status in {"technical_blocked", "semantic_failed"}:
            required_phase = (
                "technical"
                if knowledge_status == "technical_blocked"
                else "semantic"
            )
        else:
            required_phase = None
        if not is_knowledge_attempt and not is_knowledge_finding:
            raise ReviewError(
                "до других областей завершите обязательную проверку "
                "корпуса знаний",
            )
        if is_knowledge_attempt and knowledge_phase != required_phase:
            raise ReviewError(
                "проверка корпуса требует текущую фазу " + str(required_phase),
            )
        if is_knowledge_attempt and (
            args.stage != "repository"
            or (
                required_phase == "technical"
                and args.method not in {"inspection", "validation"}
            )
            or (
                required_phase == "semantic"
                and args.method != "review"
            )
        ):
            raise ReviewError(
                "технический допуск требует inspection или validation, "
                "а смысловая проверка корпуса требует review в области "
                "repository",
            )
    if bool(args.capability) == bool(args.finding):
        raise ReviewError(
            "применение должно ссылаться ровно на одну возможность или проблему",
        )
    if state.get("active_application"):
        raise ReviewError(
            "сначала завершите активное применение "
            + state["active_application"],
        )
    if args.id in state["applications"]:
        raise ReviewError(f"применение {args.id} уже существует")
    if args.stage not in state["stages"]:
        raise ReviewError(f"неизвестная область {args.stage}")
    if args.method not in APPLICATION_METHODS:
        raise ReviewError(f"неизвестный способ применения {args.method}")
    if not args.surface.strip() or not args.action.strip():
        raise ReviewError(
            "применение требует проверяемой поверхности и точного действия",
        )
    priority_rationale = getattr(args, "priority_rationale", None)
    if not priority_rationale or not priority_rationale.strip():
        raise ReviewError(
            "применение требует обоснования наибольшей ожидаемой пользы",
        )
    if state["stages"][args.stage]["status"] == "complete":
        raise ReviewError(
            f"область {args.stage} завершена, сначала откройте её повторно",
        )

    capability_hash: str | None = None
    contract_paths: list[str] = []
    if args.capability:
        decision = state["capability_decisions"].get(args.capability)
        if not decision:
            raise ReviewError(f"неизвестная возможность {args.capability}")
        ontology_scope = decision.get("ontology_scope")
        if isinstance(ontology_scope, dict) and ontology_scope.get("missing_graph"):
            raise ReviewError(
                "полная смысловая проверка требует .ai-dev-team/project-impact.json",
            )
        if (
            decision["status"] != "classified"
            or decision["applicable"] is not True
        ):
            raise ReviewError(
                f"возможность {args.capability} не классифицирована как применимая",
            )
        if decision.get("stage") != args.stage:
            raise ReviewError(
                f"возможность {args.capability} относится к другой области",
            )
        item = inventory_item(state, args.capability)
        capability_hash = decision["input_hash"]
        contract_paths = item["paths"]
    else:
        finding = state["findings"].get(args.finding)
        if not finding:
            raise ReviewError(f"неизвестная проблема {args.finding}")
        if finding["stage"] != args.stage:
            raise ReviewError(
                f"проблема {args.finding} относится к другой области",
            )
        decision = finding.get("decision") or {}
        if decision.get("value") != "fix":
            raise ReviewError("проверка исправления требует решения fix")
    if args.capability:
        ontology_scope = decision.get("ontology_scope")
        if isinstance(ontology_scope, dict):
            missing_targets = [
                node["title"]
                for node in ontology_scope.get("targets", [])
                if not node["subjects"]
            ]
            if missing_targets:
                raise ReviewError(
                    "целевая вершина не имеет обнаруженных представлений: "
                    + ", ".join(sorted(missing_targets)),
                )
        missing_prerequisites = unverified_ontology_prerequisites(state, decision)
        if missing_prerequisites:
            raise ReviewError(
                "до проверки зависимой вершины не подтверждены её основания: "
                + ", ".join(missing_prerequisites),
            )
    if state["status"] != "running":
        raise ReviewError("начать применение можно только в состоянии running")

    open_blocking_findings = [
        identifier
        for identifier, finding in state["findings"].items()
        if finding.get("blocking") and finding.get("status") == "open"
    ]
    if open_blocking_findings:
        required_capabilities = set(
            unready_finding_capabilities(state, open_blocking_findings),
        )
        if (
            args.finding not in open_blocking_findings
            and args.capability not in required_capabilities
        ):
            raise ReviewError(
                "открытая блокирующая проблема запрещает несвязанное "
                "применение: сначала подготовьте решение по проблеме "
                + ", ".join(sorted(open_blocking_findings))
                + " или выполните связанную проверку поверхности",
            )

    semantic_trace_required = knowledge_phase == "semantic" or (
        knowledge_phase is None
        and (
            args.method == "review"
            or (
                bool(args.capability)
                and decision["participation"] == "check"
                and decision.get("semantic_required", False)
            )
        )
    )
    current = repository_snapshot(repo)
    subject_indexes = getattr(args, "subject_index", [])
    subject_patterns = getattr(args, "subject_pattern", [])
    if (
        semantic_trace_required
        and decision.get("subject_discovery_required")
        and not subject_indexes
        and not subject_patterns
    ):
        raise ReviewError(
            "полная смысловая проверка требует обнаружить предметную область "
            "через --subject-index или --subject-pattern",
        )
    if knowledge_phase == "technical":
        subject_scope = [
            {
                "reference": item["reference"],
                "sha256": item["sha256"],
            }
            for item in knowledge.get("subjects", [])
        ]
        subject_discovery = {
            "registered_knowledge_subjects": [
                item["reference"] for item in subject_scope
            ],
            "requested_indexes": sorted(set(subject_indexes)),
            "requested_patterns": sorted(set(subject_patterns)),
        }
    else:
        subject_scope, subject_discovery = resolve_subject_scope(
            repo,
            current,
            getattr(args, "subject", []),
            subject_indexes,
            subject_patterns,
        )
    subject_types = parse_subject_types(
        getattr(args, "subject_type", []),
        subject_scope,
        decision.get("review_criteria", []),
    )
    for item in subject_scope:
        if item["reference"] in subject_types:
            item["semantic_type"] = subject_types[item["reference"]]
    if args.capability:
        required_subjects = required_subjects_for_decision(decision, current)
        actual_subjects = {
            item["reference"] for item in subject_scope
        }
        missing_subjects = sorted(required_subjects - actual_subjects)
        if missing_subjects:
            raise ReviewError(
                "обязательная проверка не охватывает все обнаруженные предметы: "
                + ", ".join(missing_subjects[:10]),
            )
    if is_concept_attempt:
        required_subjects = {
            item["reference"] for item in concept.get("subjects", [])
        }
        actual_subjects = {
            item["reference"] for item in subject_scope
        }
        missing_subjects = sorted(required_subjects - actual_subjects)
        if missing_subjects:
            raise ReviewError(
                "первая проверка не охватывает найденную концепцию: "
                + ", ".join(missing_subjects),
            )
    if knowledge_phase in {"technical", "semantic"}:
        root = knowledge.get("root")
        required_subjects = {
            item["reference"] for item in knowledge.get("subjects", [])
        }
        actual_subjects = {
            item["reference"] for item in subject_scope
        }
        if knowledge_phase == "technical":
            missing_subjects = sorted(required_subjects - actual_subjects)
            if missing_subjects:
                raise ReviewError(
                    "технический допуск не охватывает полный состав корпуса: "
                    + ", ".join(missing_subjects[:10]),
                )
        else:
            semantic_subjects = {
                reference
                for reference in required_subjects
                if Path(reference).name
                in {
                    "catalog.yml",
                    "concepts.yml",
                    "corpus.yml",
                    "derived-statements.yml",
                    "source.yml",
                    "statements.yml",
                }
            }
            nonsemantic_subjects = sorted(actual_subjects - semantic_subjects)
            if semantic_subjects and nonsemantic_subjects:
                raise ReviewError(
                    "смысловая проверка корпуса не должна охватывать "
                    "технические или двоичные представления: "
                    + ", ".join(nonsemantic_subjects[:10])
                )
            missing_subjects = sorted(semantic_subjects - actual_subjects)
            if missing_subjects:
                raise ReviewError(
                    "смысловая проверка не охватывает источники, утверждения "
                    "и договор корпуса: "
                    + ", ".join(missing_subjects[:10]),
                )
        if any(
            not reference.startswith(str(root).rstrip("/") + "/")
            for reference in actual_subjects
        ):
            raise ReviewError(
                "предмет обязательной проверки корпуса выходит за его корень",
            )
    contract_path_set = {
        artifact_path(repo, reference)
        for reference in contract_paths
    }
    for item in subject_scope:
        path = artifact_path(repo, item["reference"])
        if path in contract_path_set:
            raise ReviewError(
                "контракт возможности не является предметом проверки: "
                + item["reference"],
            )
    if semantic_trace_required and not subject_scope:
        raise ReviewError(
            "смысловая проверка требует явной предметной области через --subject",
        )

    for name, stage in state["stages"].items():
        if name != args.stage and stage["status"] == "running":
            stage["status"] = "pending"
    state["stages"][args.stage]["status"] = "running"
    state["current_stage"] = args.stage

    snapshot = current
    state["applications"][args.id] = {
        "stage": args.stage,
        "capability": args.capability,
        "finding": args.finding,
        "method": args.method,
        "surface": args.surface,
        "action": args.action,
        "priority_rationale": priority_rationale,
        "status": "running",
        "outcome": None,
        "decision": None,
        "evidence": [],
        "artifacts": [],
        "coverage": None,
        "claims": [],
        "claim_support": [],
        "subject_artifacts": [],
        "subject_scope": subject_scope,
        "subject_discovery": subject_discovery,
        "semantic_trace_required": semantic_trace_required,
        "semantic_contract_version": 3 if semantic_trace_required else None,
        "knowledge_phase": knowledge_phase,
        "review_criteria": (
            KNOWLEDGE_REVIEW_CRITERIA
            if knowledge_phase == "semantic"
            else decision.get("review_criteria", [])
        ),
        "observations": [],
        "challenge": None,
        "command": None,
        "input_snapshot": snapshot["id"],
        "completed_snapshot": None,
        "capability_input_hash": capability_hash,
        "contract_paths": contract_paths,
        "started_at": now(),
        "completed_at": None,
    }
    state["active_application"] = args.id
    if is_concept_attempt:
        concept["status"] = "running"
        concept["application"] = args.id
    if knowledge_phase == "technical":
        knowledge["status"] = "technical_running"
        knowledge["technical_application"] = args.id
        knowledge["technical_outcome"] = None
        knowledge["semantic_application"] = None
        knowledge["semantic_outcome"] = None
    elif knowledge_phase == "semantic":
        knowledge["status"] = "semantic_running"
        knowledge["semantic_application"] = args.id
        knowledge["semantic_outcome"] = None
    state["next_action"] = args.action
    add_history(
        state,
        "application_started",
        application=args.id,
        capability=args.capability,
        finding=args.finding,
        stage=args.stage,
    )


def record_observation(
    state: dict[str, Any],
    args: argparse.Namespace,
    repo: Path,
) -> dict[str, Any]:
    if state.get("active_application") != args.application:
        raise ReviewError(
            f"применение {args.application} не является активным",
        )
    application = state["applications"].get(args.application)
    if not application or application["status"] != "running":
        raise ReviewError(f"применение {args.application} не выполняется")
    if not application.get("semantic_trace_required"):
        raise ReviewError(
            "предметные наблюдения нужны только для смысловой проверки",
        )
    current = repository_snapshot(repo)
    if current["id"] != application["input_snapshot"]:
        raise ReviewError(
            "область Git изменилась после начала смысловой проверки",
        )

    path = artifact_path(repo, args.artifact)
    try:
        relative = path.relative_to(repo).as_posix()
    except ValueError as exc:
        raise ReviewError(
            f"наблюдаемый файл находится вне репозитория: {args.artifact}",
        ) from exc
    subject_paths = {
        item["reference"]
        for item in application["subject_scope"]
    }
    if relative not in subject_paths:
        raise ReviewError(
            f"файл не входит в заявленную предметную область: {args.artifact}",
        )
    criterion_id = getattr(args, "criterion_id", None)
    result = getattr(args, "result", None)
    if not criterion_id or not criterion_id.strip():
        raise ReviewError("наблюдение требует идентификатор критерия")
    if result not in OBSERVATION_RESULTS:
        raise ReviewError("наблюдение требует результат проверки критерия")
    criteria = {
        item["id"]: item
        for item in application.get("review_criteria", [])
    }
    criterion = getattr(args, "criterion", None)
    if criteria:
        if criterion_id not in criteria:
            raise ReviewError(
                f"критерий не входит в контракт проверки: {criterion_id}",
            )
        criterion = criteria[criterion_id]["description"]
        allowed_types = criteria[criterion_id].get("subject_types")
        subject_type = next(
            (
                item.get("semantic_type")
                for item in application.get("subject_scope", [])
                if item["reference"] == relative
            ),
            None,
        )
        if allowed_types and subject_type not in allowed_types:
            raise ReviewError(
                f"критерий {criterion_id} неприменим к предмету типа "
                f"{subject_type}",
            )
    if not criterion or not criterion.strip() or not args.note.strip():
        raise ReviewError(
            "наблюдение требует критерия и предметного описания",
        )

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        data = path.read_bytes()
        lines = [
            "Двоичный артефакт: "
            f"{len(data)} байт, SHA-256 {hashlib.sha256(data).hexdigest()}.",
        ]
    if (
        args.start_line < 1
        or args.end_line < args.start_line
        or args.end_line > len(lines)
    ):
        raise ReviewError(
            f"неверный диапазон строк {args.start_line}-{args.end_line} "
            f"для {relative}",
        )
    excerpt = "\n".join(lines[args.start_line - 1 : args.end_line])
    if not excerpt.strip():
        raise ReviewError("наблюдаемый фрагмент не должен быть пустым")

    identifier = f"observation-{len(application['observations']) + 1:03d}"
    duplicate_note = next(
        (
            observation
            for observation in application["observations"]
            if observation["artifact"] != relative
            and observation["note"].strip() == args.note.strip()
        ),
        None,
    )
    if duplicate_note:
        raise ReviewError(
            "одно описание наблюдения нельзя использовать для разных "
            "предметных файлов",
        )
    observation = {
        "id": identifier,
        "artifact": relative,
        "start_line": args.start_line,
        "end_line": args.end_line,
        "excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
        "criterion_id": criterion_id,
        "criterion": criterion,
        "result": result,
        "note": args.note,
    }
    application["observations"].append(observation)
    add_history(
        state,
        "semantic_observation_recorded",
        application=args.application,
        observation=identifier,
        artifact=relative,
    )
    return {**observation, "excerpt": excerpt}


def validate_observation_history(state: dict[str, Any]) -> None:
    """Reject semantic observations that were not recorded by the controller."""
    recorded = {
        (
            event.get("application"),
            event.get("observation"),
            event.get("artifact"),
        )
        for event in state.get("history", [])
        if event.get("event") == "semantic_observation_recorded"
    }
    for application_id, application in state.get("applications", {}).items():
        for observation in application.get("observations", []):
            receipt = (
                application_id,
                observation.get("id"),
                observation.get("artifact"),
            )
            if receipt not in recorded:
                raise ReviewError(
                    "смысловое наблюдение не записано через "
                    "record-observation",
                )


def artifact_evidence(repo: Path, references: list[str]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for reference in references:
        path = Path(reference)
        if not path.is_absolute():
            path = repo / path
        path = path.resolve()
        if not path.is_file():
            raise ReviewError(f"артефакт свидетельства не найден: {reference}")
        result.append(
            {
                "reference": reference,
                "sha256": file_hash(path),
            },
        )
    return result


def artifact_path(repo: Path, reference: str) -> Path:
    path = Path(reference)
    if not path.is_absolute():
        path = repo / path
    return path.resolve()


def validate_check_evidence(
    state: dict[str, Any],
    application: dict[str, Any],
    args: argparse.Namespace,
    repo: Path,
    current: dict[str, Any],
    artifacts: list[dict[str, str]],
    outcome: str,
) -> tuple[str | None, list[str], list[str]]:
    capability = application["capability"]
    if not capability:
        return None, [], []
    decision = state["capability_decisions"][capability]
    if decision["participation"] != "check" or outcome != "passed":
        return None, [], []

    coverage = getattr(args, "coverage", None)
    claims = getattr(args, "claim", [])
    if not coverage or not coverage.strip():
        raise ReviewError(
            "успешная предметная проверка требует описания охвата",
        )
    if not claims or any(not value.strip() for value in claims):
        raise ReviewError(
            "успешная предметная проверка требует проверяемых выводов",
        )

    contract_paths = {
        artifact_path(repo, reference)
        for reference in application["contract_paths"]
    }
    subject_artifacts: list[str] = []
    for artifact in artifacts:
        path = artifact_path(repo, artifact["reference"])
        try:
            relative = path.relative_to(repo).as_posix()
        except ValueError:
            continue
        if relative not in current["files"] or path in contract_paths:
            continue
        subject_artifacts.append(artifact["reference"])
    if not subject_artifacts:
        raise ReviewError(
            "успешная предметная проверка требует артефакт проверяемой "
            "поверхности из области Git, отличный от контракта возможности",
        )
    return coverage, claims, subject_artifacts


def validate_semantic_evidence(
    application: dict[str, Any],
    args: argparse.Namespace,
    outcome: str,
    coverage: str | None,
    claims: list[str],
) -> tuple[str | None, list[str], list[str], dict[str, Any] | None]:
    if not application.get("semantic_trace_required"):
        return coverage, claims, [], None

    if not coverage or not coverage.strip():
        raise ReviewError("смысловая проверка требует описания фактического охвата")
    if not claims or any(not value.strip() for value in claims):
        raise ReviewError("смысловая проверка требует проверяемых выводов")

    observations = application.get("observations", [])
    if not observations:
        raise ReviewError(
            "смысловая проверка требует наблюдений по предметным файлам",
        )
    observed_artifacts = {
        observation["artifact"]
        for observation in observations
    }
    subject_artifacts = {
        subject["reference"]
        for subject in application.get("subject_scope", [])
    }
    criteria = application.get("review_criteria", [])
    requires_each_subject = not criteria or any(
        criterion.get("coverage") == "each_subject"
        for criterion in criteria
    )
    if requires_each_subject:
        missing = sorted(subject_artifacts - observed_artifacts)
        if missing:
            raise ReviewError(
                "нет предметных наблюдений для заявленных файлов: "
                + ", ".join(missing),
            )
    substantive_artifacts = {
        observation["artifact"]
        for observation in observations
        if observation.get("result") != "not_applicable"
    }
    if requires_each_subject:
        missing_substantive = sorted(subject_artifacts - substantive_artifacts)
        if missing_substantive:
            raise ReviewError(
                "нет содержательного результата проверки для файлов: "
                + ", ".join(missing_substantive),
            )
    problems = [
        observation["id"]
        for observation in observations
        if observation.get("result") == "problem"
    ]
    if problems and outcome in {"applied", "passed"}:
        raise ReviewError(
            "найденная смысловая проблема не допускает успешный результат: "
            + ", ".join(problems),
        )
    if outcome == "failed" and not problems:
        raise ReviewError(
            "неуспешная смысловая проверка требует problem-наблюдение",
        )

    for criterion in application.get("review_criteria", []):
        criterion_id = criterion["id"]
        covered = {
            observation["artifact"]
            for observation in observations
            if observation.get("criterion_id") == criterion_id
        }
        expected_subjects = criterion_subjects(application, criterion)
        if criterion["coverage"] == "each_subject":
            criterion_missing = sorted(expected_subjects - covered)
            if criterion_missing:
                raise ReviewError(
                    f"критерий {criterion_id} не проверен для файлов: "
                    + ", ".join(criterion_missing),
                )
        elif expected_subjects and not covered:
            raise ReviewError(
                f"критерий {criterion_id} не проверен для предметной области",
            )

    observation_ids = {
        observation["id"]
        for observation in observations
    }
    claim_support = getattr(args, "claim_support", [])
    if len(claim_support) != len(claims):
        raise ReviewError(
            "каждый проверяемый вывод требует одно --claim-support",
        )
    unknown_support = sorted(set(claim_support) - observation_ids)
    if unknown_support:
        raise ReviewError(
            "вывод ссылается на неизвестные наблюдения: "
            + ", ".join(unknown_support),
        )

    challenge_text = getattr(args, "challenge", None)
    challenge_outcome = getattr(args, "challenge_outcome", None)
    challenge_support = getattr(args, "challenge_support", [])
    if (
        not challenge_text
        or not challenge_text.strip()
        or challenge_outcome not in CHALLENGE_OUTCOMES
        or not challenge_support
    ):
        raise ReviewError(
            "смысловая проверка требует попытку опровержения, её результат "
            "и опорные наблюдения",
        )
    unknown_challenge_support = sorted(
        set(challenge_support) - observation_ids,
    )
    if unknown_challenge_support:
        raise ReviewError(
            "попытка опровержения ссылается на неизвестные наблюдения: "
            + ", ".join(unknown_challenge_support),
        )
    if outcome in {"applied", "passed"} and challenge_outcome != "refuted":
        raise ReviewError(
            "подтверждённое или неразрешённое опровержение не допускает "
            "успешный результат смысловой проверки",
        )

    challenge = {
        "text": challenge_text,
        "outcome": challenge_outcome,
        "support": challenge_support,
    }
    return coverage, claims, claim_support, challenge


def execute_observation(repo: Path, command: str | None) -> dict[str, Any] | None:
    if not command:
        return None
    completed = subprocess.run(
        ["bash", "-lc", command],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return {
        "value": command,
        "exit_code": completed.returncode,
        "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest(),
    }


def finish_application(
    state: dict[str, Any],
    args: argparse.Namespace,
    repo: Path,
) -> None:
    if state.get("active_application") != args.application:
        raise ReviewError(
            f"применение {args.application} не является активным",
        )
    application = state["applications"].get(args.application)
    if not application or application["status"] != "running":
        raise ReviewError(f"применение {args.application} не выполняется")
    unlinked_problems = sorted(
        problem_observation_ids(application)
        - linked_problem_observation_ids(state, args.application),
    )
    if unlinked_problems:
        raise ReviewError(
            "problem-наблюдения требуют связанных находок: "
            + ", ".join(unlinked_problems),
        )
    if args.outcome not in APPLICATION_OUTCOMES:
        raise ReviewError(f"неизвестный результат применения {args.outcome}")
    if not args.evidence or any(not value.strip() for value in args.evidence):
        raise ReviewError("результат применения требует свидетельства")
    if not args.command and not args.artifact:
        raise ReviewError(
            "свободный текст не является доказательством, "
            "укажите команду или проверяемый артефакт",
        )
    if args.decision and args.decision not in REVIEW_DECISIONS:
        raise ReviewError(f"неизвестное решение проверки {args.decision}")
    if application["method"] == "review" and not args.decision:
        raise ReviewError("обзор требует структурированного решения")
    command = execute_observation(repo, args.command)
    current = repository_snapshot(repo)
    snapshot_changed = current["id"] != application["input_snapshot"]
    outcome = "failed" if snapshot_changed else args.outcome
    if args.decision and args.decision != "accept" and outcome in {
        "applied",
        "passed",
    }:
        raise ReviewError(
            f"решение {args.decision} не допускает успешный результат",
        )
    if command and command["exit_code"] != 0 and outcome in {
        "applied",
        "passed",
    }:
        raise ReviewError("успешный результат требует кода завершения 0")

    capability = application["capability"]
    if capability:
        decision = state["capability_decisions"][capability]
        expected = (
            {"failed", "passed"}
            if decision["participation"] == "check"
            else {"applied", "failed"}
        )
        if outcome not in expected:
            raise ReviewError(
                f"вид участия {decision['participation']} "
                f"не допускает результат {outcome}",
            )
    elif outcome not in {"failed", "passed"}:
        raise ReviewError("проверка исправления допускает passed или failed")

    artifacts = artifact_evidence(repo, args.artifact)
    coverage, claims, subject_artifacts = validate_check_evidence(
        state,
        application,
        args,
        repo,
        current,
        artifacts,
        outcome,
    )
    if application.get("semantic_trace_required") and not claims:
        coverage = getattr(args, "coverage", None)
        claims = getattr(args, "claim", [])
    coverage, claims, claim_support, challenge = validate_semantic_evidence(
        application,
        args,
        outcome,
        coverage,
        claims,
    )
    if application.get("semantic_trace_required") and not subject_artifacts:
        subject_artifacts = [
            item["reference"]
            for item in application["subject_scope"]
        ]
    evidence_fingerprint = stable_hash(
        {
            "application": args.application,
            "capability": capability,
            "finding": application["finding"],
            "surface": application["surface"],
            "evidence": args.evidence,
            "artifacts": artifacts,
            "coverage": coverage,
            "claims": claims,
            "claim_support": claim_support,
            "subject_artifacts": subject_artifacts,
            "subject_scope": application["subject_scope"],
            "observations": application["observations"],
            "challenge": challenge,
            "knowledge_phase": application.get("knowledge_phase"),
            "command": command,
            "requested_outcome": args.outcome,
            "outcome": outcome,
            "input_snapshot": application["input_snapshot"],
            "completed_snapshot": current["id"],
            "capability_input_hash": application["capability_input_hash"],
        },
    )
    application.update(
        {
            "status": "complete",
            "outcome": outcome,
            "decision": args.decision,
            "evidence": args.evidence,
            "artifacts": artifacts,
            "coverage": coverage,
            "claims": claims,
            "claim_support": claim_support,
            "subject_artifacts": subject_artifacts,
            "challenge": challenge,
            "command": command,
            "completed_snapshot": current["id"],
            "evidence_fingerprint": evidence_fingerprint,
            "completed_at": now(),
        },
    )

    finding_id = application["finding"]
    if capability and state["capability_decisions"][capability]["participation"] == "check":
        state["checks"][args.application] = {
            "stage": application["stage"],
            "capability": capability,
            "finding": None,
            "status": outcome,
            "evidence": args.evidence,
            "application": args.application,
            "input_snapshot": application["input_snapshot"],
        }
    elif finding_id:
        state["checks"][args.application] = {
            "stage": application["stage"],
            "capability": None,
            "finding": finding_id,
            "status": outcome,
            "evidence": args.evidence,
            "application": args.application,
            "input_snapshot": application["input_snapshot"],
        }
        if outcome == "passed":
            state["findings"][finding_id]["status"] = "resolved"

    state["active_application"] = None
    concept = state.get("concept_review", {})
    is_concept_application = concept.get("application") == args.application
    knowledge = state.get("knowledge_review", {})
    knowledge_phase = application.get("knowledge_phase")
    if snapshot_changed:
        if is_concept_application:
            concept["status"] = "located"
            concept["application"] = None
            concept["outcome"] = None
        if knowledge_phase == "technical":
            knowledge["status"] = "located"
            knowledge["technical_application"] = None
            knowledge["technical_outcome"] = None
        elif knowledge_phase == "semantic":
            knowledge["status"] = (
                "admitted_with_limits"
                if knowledge.get("technical_outcome") == "failed"
                else "admitted"
            )
            knowledge["semantic_application"] = None
            knowledge["semantic_outcome"] = None
        state["pending_snapshot"] = current
        state["status"] = "interrupted"
        state["next_action"] = (
            "Разобрать изменение области Git во время применения."
        )
        add_history(
            state,
            "application_input_changed",
            application=args.application,
            input_snapshot=application["input_snapshot"],
            completed_snapshot=current["id"],
        )
    else:
        if is_concept_application:
            concept["status"] = (
                "checked"
                if outcome == "passed" and args.decision == "accept"
                else "failed"
            )
            concept["outcome"] = outcome
            if concept["status"] == "checked":
                state["next_action"] = (
                    "Найти и зарегистрировать корпус знаний для обязательного "
                    "второго этапа."
                )
        if knowledge_phase == "technical":
            knowledge["technical_outcome"] = outcome
            related_findings = [
                finding
                for finding in state.get("findings", {}).values()
                if finding.get("source_application") == args.application
                and finding.get("status") == "open"
            ]
            blocking = any(
                finding.get("blocking") for finding in related_findings
            )
            if outcome == "passed":
                knowledge["status"] = "admitted"
            elif blocking or not related_findings:
                knowledge["status"] = "technical_blocked"
            else:
                knowledge["status"] = "admitted_with_limits"
            state["next_action"] = (
                "Исправить блокер технического допуска и повторить проверку."
                if blocking
                else "Содержательно проверить доступный корпус относительно "
                "концепции."
            )
        elif knowledge_phase == "semantic":
            knowledge["semantic_outcome"] = outcome
            knowledge["status"] = (
                "checked"
                if outcome == "passed" and args.decision == "accept"
                else "semantic_failed"
            )
            state["next_action"] = (
                "Заново выбрать наиболее ценную работу после обязательной "
                "проверки концепции и корпуса."
                if knowledge["status"] == "checked"
                else "Исправить смысловые проблемы корпуса и повторить его "
                "проверку."
            )
        if not is_concept_application and not knowledge_phase:
            state["next_action"] = (
                "Сохранить точное следующее действие перед новым предметным "
                "действием."
            )
        ready = [
            identifier
            for identifier, finding in state["findings"].items()
            if finding["blocking"]
            and finding["status"] == "open"
            and not unready_finding_capabilities(state, [identifier])
        ]
        if ready:
            state["status"] = "running"
            state["next_action"] = (
                "Подготовить самодостаточный запрос решения по проблеме "
                + ready[0]
                + "."
            )
    add_history(
        state,
        "application_finished",
        application=args.application,
        outcome=outcome,
        requested_outcome=args.outcome,
        decision=args.decision,
        evidence_fingerprint=evidence_fingerprint,
    )
    if is_concept_application and not snapshot_changed:
        add_history(
            state,
            "concept_checked",
            application=args.application,
            outcome=outcome,
            decision=args.decision,
        )
    if knowledge_phase and not snapshot_changed:
        add_history(
            state,
            "knowledge_phase_finished",
            phase=knowledge_phase,
            application=args.application,
            outcome=outcome,
            decision=args.decision,
            status=knowledge.get("status"),
        )


def record_check(
    state: dict[str, Any],
    args: argparse.Namespace,
    repo: Path | None = None,
) -> None:
    del state, args, repo
    raise ReviewError(
        "record-check без применения запрещён, "
        "используйте start-application и finish-application",
    )


def migrate_state(state: dict[str, Any]) -> None:
    source_version = state.get("schema_version")
    if source_version not in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13}:
        raise ReviewError(
            "миграция поддерживает состояния версий 1–13",
        )
    invalidated_applications = len(state.get("applications", {}))
    invalidated_checks = len(state.get("checks", {}))
    invalidated_findings = len(state.get("findings", {}))
    for finding in state.get("findings", {}).values():
        decision = finding.get("decision") or {}
        if finding.get("status") == "resolved" and decision.get("value") == "fix":
            finding["status"] = "approved"
    for stage in state["stages"].values():
        stage["status"] = "pending"
        stage["input_snapshot"] = None
    state["current_stage"] = None
    state["status"] = "running"
    state["checks"] = {}
    state["applications"] = {}
    state["active_application"] = None
    state["findings"] = {}
    state["concept_review"] = {
        "status": "pending",
        "capability": None,
        "instructions": None,
        "subjects": [],
        "evidence": [],
        "application": None,
        "outcome": None,
    }
    state["knowledge_review"] = {
        "status": "pending",
        "root": None,
        "subjects": [],
        "evidence": [],
        "capability": None,
        "technical_application": None,
        "technical_outcome": None,
        "semantic_application": None,
        "semantic_outcome": None,
    }
    state["decision_brief"] = None
    state["next_action"] = (
        "Найти концепцию по указателю в корневых инструкциях и "
        "зарегистрировать предмет её проверки."
    )
    state["schema_version"] = STATE_SCHEMA_VERSION
    add_history(
        state,
        "state_migrated",
        source_version=source_version,
        target_version=STATE_SCHEMA_VERSION,
        previous_checks_invalidated=True,
        invalidated_applications=invalidated_applications,
        invalidated_checks=invalidated_checks,
        invalidated_findings=invalidated_findings,
    )


def successful_capability_applications(
    state: dict[str, Any],
    stage: str | None = None,
) -> set[str]:
    successful: set[str] = set()
    for application in state["applications"].values():
        capability = application.get("capability")
        if not capability or application.get("status") != "complete":
            continue
        if stage and application.get("stage") != stage:
            continue
        decision = state["capability_decisions"].get(capability)
        if not decision:
            continue
        expected_outcome = (
            "passed" if decision["participation"] == "check" else "applied"
        )
        if application.get("outcome") != expected_outcome:
            continue
        if decision["participation"] == "check" and (
            not application.get("coverage")
            or not application.get("claims")
            or not application.get("subject_artifacts")
        ):
            continue
        if application.get("semantic_trace_required") and (
            application.get("semantic_contract_version") != 3
            or not application.get("subject_scope")
            or not application.get("observations")
            or not application.get("claim_support")
            or (application.get("challenge") or {}).get("outcome") != "refuted"
        ):
            continue
        if application.get("capability_input_hash") != decision["input_hash"]:
            continue
        application_stage = state["stages"].get(application.get("stage"))
        snapshot_is_current = (
            application.get("input_snapshot") == state["snapshot"]["id"]
            or (
                application_stage
                and application_stage["status"] == "complete"
                and application.get("input_snapshot")
                == application_stage.get("input_snapshot")
            )
        )
        if not snapshot_is_current:
            continue
        successful.add(capability)
    knowledge = state.get("knowledge_review", {})
    if (
        stage == "repository"
        and knowledge_review_is_proven(state)
        and knowledge.get("capability")
    ):
        successful.add(knowledge["capability"])
    return successful


def concept_review_is_proven(state: dict[str, Any]) -> bool:
    concept = state.get("concept_review")
    if not isinstance(concept, dict) or concept.get("status") != "checked":
        return False
    instructions = concept.get("instructions")
    if (
        not concept.get("evidence")
        or not isinstance(instructions, dict)
        or not instructions.get("reference")
        or not instructions.get("sha256")
        or any(
            not item.get("reference") or not item.get("sha256")
            for item in concept.get("subjects", [])
        )
    ):
        return False
    application_id = concept.get("application")
    application = state.get("applications", {}).get(application_id)
    if not isinstance(application, dict):
        return False
    if (
        application.get("status") != "complete"
        or application.get("outcome") != "passed"
        or application.get("decision") != "accept"
        or application.get("capability") != concept.get("capability")
        or application.get("stage") != "requirements"
        or application.get("method") != "review"
        or application.get("semantic_contract_version") != 3
        or not application.get("coverage")
        or not application.get("evidence")
        or not application.get("artifacts")
        or not application.get("subject_artifacts")
        or not application.get("input_snapshot")
        or application.get("input_snapshot")
        != application.get("completed_snapshot")
    ):
        return False
    if any(
        not item.get("reference") or not item.get("sha256")
        for item in application.get("artifacts", [])
    ):
        return False
    decision = state.get("capability_decisions", {}).get(
        concept.get("capability"),
    )
    if (
        not isinstance(decision, dict)
        or application.get("capability_input_hash") != decision.get("input_hash")
    ):
        return False
    required_subjects = {
        item.get("reference")
        for item in concept.get("subjects", [])
        if item.get("reference")
    }
    actual_subjects = {
        item.get("reference")
        for item in application.get("subject_scope", [])
        if item.get("reference")
    }
    if not required_subjects or not required_subjects.issubset(actual_subjects):
        return False
    scoped_hashes = {
        item.get("reference"): item.get("sha256")
        for item in application.get("subject_scope", [])
    }
    if any(
        not scoped_hashes.get(reference)
        for reference in required_subjects
    ):
        return False
    if not required_subjects.issubset(
        set(application.get("subject_artifacts", [])),
    ):
        return False
    observations = application.get("observations", [])
    if any(
        not item.get("id")
        or not item.get("artifact")
        or not isinstance(item.get("start_line"), int)
        or not isinstance(item.get("end_line"), int)
        or item["start_line"] < 1
        or item["end_line"] < item["start_line"]
        or not item.get("excerpt_sha256")
        or not item.get("criterion_id")
        or not item.get("criterion")
        or item.get("result") not in OBSERVATION_RESULTS
        or not item.get("note")
        for item in observations
    ):
        return False
    observation_ids = {
        item.get("id")
        for item in observations
        if item.get("id")
    }
    observed_subjects = {
        item.get("artifact")
        for item in observations
        if item.get("result") in {"supports", "problem"}
    }
    if not required_subjects.issubset(observed_subjects):
        return False
    for criterion in application.get("review_criteria", []):
        covered = {
            item.get("artifact")
            for item in observations
            if item.get("criterion_id") == criterion.get("id")
        }
        expected_subjects = criterion_subjects(application, criterion)
        if criterion.get("coverage") == "each_subject":
            if not expected_subjects.issubset(covered):
                return False
        elif criterion.get("coverage") == "surface":
            if expected_subjects and not covered:
                return False
        else:
            return False
    claims = application.get("claims", [])
    claim_support = application.get("claim_support", [])
    if (
        not claims
        or len(claims) != len(claim_support)
        or not set(claim_support).issubset(observation_ids)
    ):
        return False
    challenge = application.get("challenge")
    if (
        not isinstance(challenge, dict)
        or challenge.get("outcome") != "refuted"
        or not challenge.get("support")
        or not set(challenge["support"]).issubset(observation_ids)
    ):
        return False
    expected_fingerprint = stable_hash(
        {
            "application": application_id,
            "capability": application.get("capability"),
            "finding": application.get("finding"),
            "surface": application.get("surface"),
            "evidence": application.get("evidence"),
            "artifacts": application.get("artifacts"),
            "coverage": application.get("coverage"),
            "claims": application.get("claims"),
            "claim_support": application.get("claim_support"),
            "subject_artifacts": application.get("subject_artifacts"),
            "subject_scope": application.get("subject_scope"),
            "observations": application.get("observations"),
            "challenge": application.get("challenge"),
            "knowledge_phase": application.get("knowledge_phase"),
            "command": application.get("command"),
            "requested_outcome": application.get("outcome"),
            "outcome": application.get("outcome"),
            "input_snapshot": application.get("input_snapshot"),
            "completed_snapshot": application.get("completed_snapshot"),
            "capability_input_hash": application.get("capability_input_hash"),
        },
    )
    if application.get("evidence_fingerprint") != expected_fingerprint:
        return False
    return True


def knowledge_review_is_proven(state: dict[str, Any]) -> bool:
    knowledge = state.get("knowledge_review")
    if not isinstance(knowledge, dict):
        return False
    if knowledge.get("status") == "absent":
        return (
            bool(knowledge.get("evidence"))
            and knowledge.get("root") is None
            and not knowledge.get("subjects")
            and knowledge.get("capability") is None
            and knowledge.get("technical_application") is None
            and knowledge.get("technical_outcome") is None
            and knowledge.get("semantic_application") is None
            and knowledge.get("semantic_outcome") is None
        )
    if knowledge.get("status") != "checked":
        return False
    if (
        not knowledge.get("root")
        or not knowledge.get("subjects")
        or not knowledge.get("evidence")
        or not knowledge.get("technical_application")
        or not knowledge.get("semantic_application")
    ):
        return False
    technical = state.get("applications", {}).get(
        knowledge["technical_application"],
    )
    semantic = state.get("applications", {}).get(
        knowledge["semantic_application"],
    )
    if not isinstance(technical, dict) or not isinstance(semantic, dict):
        return False
    if (
        technical.get("status") != "complete"
        or technical.get("outcome") not in {"failed", "passed"}
        or technical.get("knowledge_phase") != "technical"
        or technical.get("stage") != "repository"
        or technical.get("method") not in {"inspection", "validation"}
        or semantic.get("status") != "complete"
        or semantic.get("outcome") != "passed"
        or semantic.get("decision") != "accept"
        or semantic.get("knowledge_phase") != "semantic"
        or semantic.get("stage") != "repository"
        or semantic.get("method") != "review"
        or semantic.get("semantic_contract_version") != 3
        or semantic.get("capability") != knowledge.get("capability")
    ):
        return False
    if technical.get("capability") != knowledge.get("capability"):
        return False
    if any(
        not item.get("reference") or not item.get("sha256")
        for item in knowledge.get("subjects", [])
    ):
        return False
    for application_id, application in (
        (knowledge["technical_application"], technical),
        (knowledge["semantic_application"], semantic),
    ):
        expected_fingerprint = stable_hash(
            {
                "application": application_id,
                "capability": application.get("capability"),
                "finding": application.get("finding"),
                "surface": application.get("surface"),
                "evidence": application.get("evidence"),
                "artifacts": application.get("artifacts"),
                "coverage": application.get("coverage"),
                "claims": application.get("claims"),
                "claim_support": application.get("claim_support"),
                "subject_artifacts": application.get("subject_artifacts"),
                "subject_scope": application.get("subject_scope"),
                "observations": application.get("observations"),
                "challenge": application.get("challenge"),
                "knowledge_phase": application.get("knowledge_phase"),
                "command": application.get("command"),
                "requested_outcome": application.get("outcome"),
                "outcome": application.get("outcome"),
                "input_snapshot": application.get("input_snapshot"),
                "completed_snapshot": application.get("completed_snapshot"),
                "capability_input_hash": application.get(
                    "capability_input_hash",
                ),
            },
        )
        if application.get("evidence_fingerprint") != expected_fingerprint:
            return False
    return True


def validate_knowledge_phase_application(
    state: dict[str, Any],
    knowledge: dict[str, Any],
    application_id: str | None,
    phase: str,
    expected_status: str,
) -> dict[str, Any]:
    if not application_id:
        raise ReviewError(f"фаза корпуса {phase} не связана с применением")
    application = state.get("applications", {}).get(application_id)
    if not isinstance(application, dict):
        raise ReviewError(f"фаза корпуса {phase} ссылается на неизвестное применение")
    if (
        application.get("knowledge_phase") != phase
        or application.get("capability") != knowledge.get("capability")
        or application.get("stage") != "repository"
        or application.get("status") != expected_status
    ):
        raise ReviewError(
            "фаза корпуса должна относиться к kc-validation в области "
            "repository",
        )
    if phase == "technical" and application.get("method") not in {
        "inspection",
        "validation",
    }:
        raise ReviewError("техническая фаза корпуса использует недопустимый способ")
    if phase == "semantic" and application.get("method") != "review":
        raise ReviewError("смысловая фаза корпуса требует способ review")
    return application


def validate_completion(state: dict[str, Any], target: str) -> None:
    if not concept_review_is_proven(state):
        raise ReviewError("не завершена обязательная первая проверка концепции")
    if not knowledge_review_is_proven(state):
        raise ReviewError(
            "не завершена обязательная вторая проверка корпуса знаний",
        )
    if state.get("active_application"):
        raise ReviewError("не завершено активное применение")
    incomplete_stages = [
        name
        for name in ordered_stages(state)
        if state["stages"][name]["status"] != "complete"
    ]
    if incomplete_stages:
        raise ReviewError(
            "не завершены области: " + ", ".join(incomplete_stages),
        )
    unknown = [
        name
        for name, value in state["capability_decisions"].items()
        if value["status"] != "classified" or value["applicable"] is None
    ]
    if unknown:
        raise ReviewError(
            "не классифицированы возможности: " + ", ".join(unknown),
        )
    applied_capabilities = successful_capability_applications(state)
    unapplied = [
        name
        for name, value in state["capability_decisions"].items()
        if value["applicable"]
        and value["participation"] != "not_applicable"
        and name not in applied_capabilities
    ]
    if unapplied:
        raise ReviewError(
            "нет доказанного применения возможностей: " + ", ".join(unapplied),
        )
    unresolved = [
        name
        for name, value in state["findings"].items()
        if value["status"] not in {"resolved", "not_applicable", "accepted"}
    ]
    if unresolved:
        raise ReviewError("не закрыты проблемы: " + ", ".join(unresolved))
    accepted = [
        name
        for name, value in state["findings"].items()
        if value["status"] == "accepted"
    ]
    if target == "complete" and accepted:
        raise ReviewError(
            "complete не допускает принятые риски: " + ", ".join(accepted),
        )
    if target == "complete_with_accepted_risks" and not accepted:
        raise ReviewError("нет принятого риска для выбранного конечного статуса")


def require_terminal_state(state: dict[str, Any]) -> None:
    """Отказать до передачи итога, если проход ещё не достиг конца."""
    if state["status"] not in TERMINAL_STATES:
        raise ReviewError(
            "полная проверка не завершена: состояние "
            f"{state['status']!r}; продолжите с сохранённого next_action",
        )


def transition(
    state: dict[str, Any],
    target: str,
    action: str | None,
    repo: Path | None = None,
) -> None:
    source = state["status"]
    if target not in PROCESS_STATES:
        raise ReviewError(f"неизвестное состояние {target}")
    if target not in TRANSITIONS[source]:
        raise ReviewError(f"недопустимый переход {source} → {target}")
    if target == "waiting_decision":
        raise ReviewError(
            "используйте prepare-decision, чтобы сформировать "
            "самодостаточный запрос решения",
        )
    if target in TERMINAL_STATES:
        validate_completion(state, target)
        if repo is None:
            raise ReviewError("конечный переход требует живой Git-репозиторий")
        current_snapshot = repository_snapshot(repo)
        if current_snapshot["id"] != state["snapshot"]["id"]:
            raise ReviewError(
                "область Git изменилась, перед завершением выполните refresh",
            )
        current_inventory = inventory(repo)
        if (
            current_inventory["fingerprint"]
            != state["capability_inventory"]["fingerprint"]
        ):
            raise ReviewError(
                "состав возможностей изменился, "
                "перед завершением выполните refresh",
            )
        add_history(
            state,
            "terminal_inputs_verified",
            snapshot=current_snapshot["id"],
            inventory=current_inventory["fingerprint"],
        )
        state["next_action"] = None
    elif action:
        state["next_action"] = action
    if source == "waiting_decision" and target != "waiting_decision":
        state["decision_brief"] = None
    state["status"] = target
    add_history(state, "transition", source=source, target=target)


def set_stage(
    state: dict[str, Any],
    name: str,
    status: str,
    repo: Path | None = None,
) -> None:
    if name not in state["stages"]:
        raise ReviewError(f"неизвестная область {name}")
    if status not in STAGE_STATUSES:
        raise ReviewError(f"неизвестное состояние области {status}")
    if (
        status in {"running", "complete"}
        and (
            not concept_review_is_proven(state)
            or not knowledge_review_is_proven(state)
        )
    ):
        raise ReviewError(
            "области недоступны до обязательных проверок концепции и "
            "корпуса знаний",
        )
    if status == "pending":
        if state.get("active_application"):
            raise ReviewError("нельзя открыть область при активном применении")
        state["stages"][name]["status"] = "pending"
        state["stages"][name]["input_snapshot"] = None
        if state.get("current_stage") == name:
            state["current_stage"] = None
        add_history(
            state,
            "area_reopened",
            stage=name,
        )
        return
    if status == "running":
        running = [
            stage
            for stage in ordered_stages(state)
            if stage != name and state["stages"][stage]["status"] == "running"
        ]
        for stage in running:
            state["stages"][stage]["status"] = "pending"
        if state["stages"][name]["status"] == "complete":
            raise ReviewError("завершённую область нельзя запустить повторно")
    if status == "complete":
        if state["stages"][name]["status"] != "running":
            raise ReviewError(f"область {name} не выполняется")
        if state.get("active_application"):
            raise ReviewError("область содержит незавершённое применение")
        if repo is not None:
            current_snapshot = repository_snapshot(repo)
            if current_snapshot["id"] != state["snapshot"]["id"]:
                raise ReviewError(
                    "область Git изменилась, перед закрытием области "
                    "выполните refresh",
                )
        unresolved = [
            identifier
            for identifier, finding in state["findings"].items()
            if finding["stage"] == name
            and finding["status"] not in {"resolved", "not_applicable", "accepted"}
        ]
        if unresolved:
            raise ReviewError(
                "область содержит нерешённые проблемы: " + ", ".join(unresolved),
            )
        undecided = [
            identifier
            for identifier, decision in state["capability_decisions"].items()
            if decision.get("stage") == name
            and (
                decision["status"] != "classified"
                or decision["applicable"] is None
            )
        ]
        if undecided:
            raise ReviewError(
                "область содержит неклассифицированные возможности: "
                + ", ".join(undecided),
            )
        applied = successful_capability_applications(state, name)
        unapplied = [
            identifier
            for identifier, decision in state["capability_decisions"].items()
            if decision.get("stage") == name
            and decision["applicable"]
            and decision["participation"] != "not_applicable"
            and identifier not in applied
        ]
        if unapplied:
            raise ReviewError(
                "область не имеет доказанного применения: "
                + ", ".join(unapplied),
            )
        state["stages"][name]["input_snapshot"] = state["snapshot"]["id"]
    state["stages"][name]["status"] = status
    if status == "running":
        state["current_stage"] = name
    add_history(state, "stage_status", stage=name, status=status)


def changed_paths(
    old_snapshot: dict[str, Any],
    new_snapshot: dict[str, Any],
) -> list[str]:
    old_files = old_snapshot["files"]
    new_files = new_snapshot["files"]
    changed = {
        name
        for name in set(old_files) | set(new_files)
        if old_files.get(name) != new_files.get(name)
    }
    old_metadata = old_snapshot.get("metadata")
    new_metadata = new_snapshot.get("metadata")
    if old_metadata is not None and new_metadata is not None:
        changed.update(
            name
            for name in set(old_metadata) | set(new_metadata)
            if old_metadata.get(name) != new_metadata.get(name)
        )
    return sorted(changed)


def approved_paths(state: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for finding in state["findings"].values():
        if finding["status"] == "approved":
            result.update(finding["allowed_paths"])
    return result


def invalidate_active_application(
    state: dict[str, Any],
    accepted_snapshot: str,
) -> str | None:
    application_id = state.get("active_application")
    if not application_id:
        return None
    application = state["applications"].get(application_id)
    if not application or application.get("status") != "running":
        raise ReviewError("активное применение имеет недопустимое состояние")

    application.update(
        {
            "status": "invalidated",
            "invalidated_at": now(),
            "invalidated_snapshot": accepted_snapshot,
            "invalidation_reason": "accepted_snapshot_changed",
        },
    )
    state["active_application"] = None

    concept = state.get("concept_review", {})
    if concept.get("application") == application_id:
        concept["status"] = (
            "located"
            if concept.get("instructions") and concept.get("subjects")
            else "pending"
        )
        concept["application"] = None
        concept["outcome"] = None

    knowledge = state.get("knowledge_review", {})
    if knowledge.get("technical_application") == application_id:
        knowledge["status"] = "located"
        knowledge["technical_application"] = None
        knowledge["technical_outcome"] = None
    elif knowledge.get("semantic_application") == application_id:
        knowledge["status"] = (
            "admitted_with_limits"
            if knowledge.get("technical_outcome") == "failed"
            else "admitted"
        )
        knowledge["semantic_application"] = None
        knowledge["semantic_outcome"] = None

    add_history(
        state,
        "active_application_invalidated",
        application=application_id,
        input_snapshot=application["input_snapshot"],
        accepted_snapshot=accepted_snapshot,
    )
    return application_id


def invalidate_stale_active_application(state: dict[str, Any]) -> str | None:
    application_id = state.get("active_application")
    if not application_id:
        return None
    application = state["applications"].get(application_id)
    if not application or application.get("status") != "running":
        raise ReviewError("активное применение имеет недопустимое состояние")
    accepted_snapshot = state["snapshot"]["id"]
    if application.get("input_snapshot") == accepted_snapshot:
        return None
    return invalidate_active_application(state, accepted_snapshot)


def accept_pending_snapshot(
    state: dict[str, Any],
    repo: Path,
    finding_ids: list[str] | None = None,
    external_paths: list[str] | None = None,
    external_reason: str | None = None,
    reopen_stages: list[str] | None = None,
    impact_rationale: str | None = None,
) -> None:
    if state["status"] != "interrupted":
        raise ReviewError("принятие снимка допустимо только после прерывания")
    pending = state.get("pending_snapshot")
    if not pending:
        raise ReviewError("нет снимка, ожидающего принятия")
    finding_ids = sorted(set(finding_ids or []))
    external_paths = sorted(set(external_paths or []))
    reopen_stages = sorted(set(reopen_stages or []))
    if not finding_ids and not external_paths:
        raise ReviewError(
            "нужно указать проверенную проблему или подтверждённый внешний путь",
        )
    if not reopen_stages:
        raise ReviewError(
            "принятие снимка требует хотя бы одну затронутую область",
        )
    unknown_stages = sorted(set(reopen_stages) - set(state["stages"]))
    if unknown_stages:
        raise ReviewError(
            "неизвестные затронутые области: " + ", ".join(unknown_stages),
        )
    if not impact_rationale or not impact_rationale.strip():
        raise ReviewError(
            "принятие снимка требует обоснование влияния на области",
        )
    running_stages = {
        name
        for name, stage in state["stages"].items()
        if stage["status"] == "running"
    }
    omitted_running = sorted(running_stages - set(reopen_stages))
    if omitted_running:
        raise ReviewError(
            "выполнявшиеся области должны быть открыты повторно: "
            + ", ".join(omitted_running),
        )
    if external_reason is not None and not external_paths:
        raise ReviewError(
            "причина внешнего изменения допустима только с внешним путём",
        )
    if external_paths and (
        external_reason is None or not external_reason.strip()
    ):
        raise ReviewError(
            "для подтверждённых внешних путей нужна причина решения человека",
        )
    normalized_external_reason = (
        external_reason.strip() if external_reason is not None else None
    )

    allowed: set[str] = set()
    for identifier in finding_ids:
        finding = state["findings"].get(identifier)
        if not finding:
            raise ReviewError(f"неизвестная проблема {identifier}")
        decision = finding.get("decision") or {}
        if decision.get("value") != "fix" or finding["status"] != "resolved":
            raise ReviewError(
                f"проблема {identifier} не имеет проверенного исправления",
            )
        if not any(
            check["finding"] == identifier
            and check["status"] == "passed"
            and check["input_snapshot"] == pending["id"]
            for check in state["checks"].values()
        ):
            raise ReviewError(
                f"для проблемы {identifier} нет проверки принимаемого снимка",
            )
        allowed.update(finding["allowed_paths"])

    current = repository_snapshot(repo)
    if current["id"] != pending["id"]:
        raise ReviewError("область Git изменилась после сохранения снимка")
    changed = changed_paths(state["snapshot"], current)
    unchanged_external = [
        name for name in external_paths if name not in changed
    ]
    if unchanged_external:
        raise ReviewError(
            "подтверждённые внешние пути отсутствуют в изменении снимка: "
            + ", ".join(unchanged_external),
        )
    allowed.update(external_paths)
    external = [name for name in changed if name not in allowed]
    if external:
        raise ReviewError(
            "снимок содержит неразрешённые пути: " + ", ".join(external),
        )

    previous_snapshot = state["snapshot"]["id"]
    invalidate_knowledge_review_for_changed_paths(state, repo, changed)
    state["snapshot"] = current
    state["pending_snapshot"] = None
    state["decision_brief"] = None
    state["status"] = "running"
    invalidated_application = invalidate_active_application(
        state,
        current["id"],
    )
    for stage in reopen_stages:
        state["stages"][stage]["status"] = "pending"
        state["stages"][stage]["input_snapshot"] = None
    state["current_stage"] = None
    state["next_action"] = (
        "Переоценить влияние и выбрать следующую работу по принятому снимку."
    )
    add_history(
        state,
        "pending_snapshot_accepted",
        findings=finding_ids,
        confirmed_external_paths=external_paths,
        external_reason=normalized_external_reason,
        reopened_stages=reopen_stages,
        impact_rationale=impact_rationale.strip(),
        previous_snapshot=previous_snapshot,
        accepted_snapshot=current["id"],
        changed_paths=changed,
        previous_results_invalidated=reopen_stages,
        invalidated_application=invalidated_application,
    )
    refresh(state, repo)


def invalidate_knowledge_review_for_changed_paths(
    state: dict[str, Any],
    repo: Path,
    changed: list[str],
) -> None:
    """Reset the corpus barrier when the accepted corpus content changed."""
    knowledge = state.get("knowledge_review")
    if (
        not isinstance(knowledge, dict)
        or knowledge.get("status") == "absent"
        or not isinstance(knowledge.get("root"), str)
    ):
        return
    prefix = knowledge["root"].rstrip("/") + "/"
    affected_paths = [path for path in changed if path.startswith(prefix)]
    current_subjects = [
        {"reference": reference, "sha256": file_hash(repo / reference)}
        for reference in sorted(
            snapshot_files_in_root(repository_snapshot(repo), knowledge["root"]),
        )
    ]
    if not affected_paths and knowledge.get("subjects") == current_subjects:
        return
    knowledge["status"] = "located"
    knowledge["subjects"] = current_subjects
    knowledge["technical_application"] = None
    knowledge["technical_outcome"] = None
    knowledge["semantic_application"] = None
    knowledge["semantic_outcome"] = None
    add_history(
        state,
        "knowledge_review_invalidated",
        root=knowledge["root"],
        changed_paths=affected_paths,
    )


def refresh(state: dict[str, Any], repo: Path) -> None:
    require_nonterminal(state)
    invalidate_stale_active_application(state)
    new_snapshot = repository_snapshot(repo)
    changed = changed_paths(state["snapshot"], new_snapshot)
    allowed = approved_paths(state)
    external = [name for name in changed if name not in allowed]
    if external:
        state["pending_snapshot"] = new_snapshot
        state["decision_brief"] = None
        state["status"] = "interrupted"
        state["next_action"] = "Разобрать внешние изменения области Git."
        add_history(state, "external_change", paths=external)
        return
    state["snapshot"] = new_snapshot

    invalidate_knowledge_review_for_changed_paths(state, repo, changed)

    old_decisions = state["capability_decisions"]
    new_inventory = inventory(repo)
    new_decisions = initial_capability_decisions(
        new_inventory,
        new_snapshot,
        repo,
    )
    state["capability_inventory"] = new_inventory
    state["capability_decisions"] = new_decisions
    new_items = {
        item["id"]: item
        for item in new_inventory["capabilities"]
    }
    for identifier, decision in state["capability_decisions"].items():
        previous = old_decisions.get(identifier)
        if previous and (
            previous["input_hash"] == decision["input_hash"]
            or legacy_decision_matches_classification(
                previous,
                new_items[identifier],
            )
        ):
            previous["input_hash"] = decision["input_hash"]
            previous["decision_paths"] = decision.get("decision_paths", [])
            previous["review_criteria"] = decision.get("review_criteria", [])
            previous["subject_discovery_required"] = decision.get(
                "subject_discovery_required",
                False,
            )
            previous["required_subject_patterns"] = decision.get(
                "required_subject_patterns",
                [],
            )
            previous["semantic_required"] = decision.get("semantic_required", False)
            previous["ontology_scope"] = decision.get("ontology_scope")
            previous["enforced_applicable"] = decision.get(
                "enforced_applicable",
                False,
            )
            state["capability_decisions"][identifier] = previous

    changed_capabilities = [
        identifier
        for identifier, decision in state["capability_decisions"].items()
        if identifier not in old_decisions
        or old_decisions[identifier]["input_hash"] != decision["input_hash"]
    ]
    if not changed_capabilities:
        add_history(state, "refreshed", changed_paths=changed)
        return

    invalidated_mandatory_reviews: list[str] = []
    concept = state.get("concept_review", {})
    if (
        concept.get("status") == "checked"
        and concept.get("capability") in changed_capabilities
    ):
        concept["status"] = (
            "located"
            if concept.get("instructions") and concept.get("subjects")
            else "pending"
        )
        concept["application"] = None
        concept["outcome"] = None
        invalidated_mandatory_reviews.append("concept")
    unknown = [
        identifier
        for identifier in changed_capabilities
        if state["capability_decisions"][identifier]["status"] != "classified"
    ]
    changed_stages: set[str] = set()
    for identifier in changed_capabilities:
        decision = state["capability_decisions"][identifier]
        stage = decision.get("stage")
        if stage and stage in state["stages"]:
            changed_stages.add(stage)
    for stage in changed_stages:
        state["stages"][stage]["status"] = "pending"
        state["stages"][stage]["input_snapshot"] = None
        if state.get("current_stage") == stage:
            state["current_stage"] = None
    if unknown:
        state["status"] = "blocked"
        state["next_action"] = "Классифицировать изменившиеся возможности."
    else:
        state["status"] = "running"
        state["next_action"] = (
            "Оценить влияние изменившихся возможностей, открыть установленные "
            "зависимые области и заново выбрать наиболее ценную работу."
        )
    add_history(
        state,
        "capabilities_changed",
        capabilities=changed_capabilities,
        invalidated_mandatory_reviews=invalidated_mandatory_reviews,
    )


def validate_state(state: dict[str, Any]) -> None:
    validate_observation_history(state)
    concept = state.get("concept_review")
    if not isinstance(concept, dict) or concept.get("status") not in {
        "pending",
        "located",
        "running",
        "checked",
        "failed",
        "blocked",
    }:
        raise ReviewError("неизвестно состояние обязательной проверки концепции")
    if concept.get("status") == "running":
        if not concept.get("application"):
            raise ReviewError("проверка концепции не связана с применением")
        if state.get("active_application") != concept.get("application"):
            raise ReviewError("активное применение не совпадает с концепцией")
    if concept.get("status") == "blocked" and state.get("status") != "blocked":
        raise ReviewError("проблема поиска концепции должна блокировать процесс")
    if concept.get("status") == "checked" and not concept_review_is_proven(state):
        raise ReviewError(
            "статус checked требует доказанное применение проверки концепции",
        )
    knowledge = state.get("knowledge_review")
    if not isinstance(knowledge, dict) or knowledge.get("status") not in {
        "pending",
        "located",
        "technical_running",
        "technical_blocked",
        "admitted",
        "admitted_with_limits",
        "semantic_running",
        "semantic_failed",
        "checked",
        "absent",
    }:
        raise ReviewError("неизвестно состояние обязательной проверки корпуса")
    knowledge_status = knowledge["status"]
    if knowledge_status == "absent":
        forbidden = (
            knowledge.get("root") is not None
            or bool(knowledge.get("subjects"))
            or knowledge.get("capability") is not None
            or knowledge.get("technical_application") is not None
            or knowledge.get("technical_outcome") is not None
            or knowledge.get("semantic_application") is not None
            or knowledge.get("semantic_outcome") is not None
        )
        if forbidden or not knowledge.get("evidence"):
            raise ReviewError(
                "отсутствующий корпус не допускает техническую или "
                "смысловую фазу",
            )
    elif knowledge_status != "pending":
        if (
            not knowledge.get("root")
            or not knowledge.get("subjects")
            or not knowledge.get("evidence")
            or knowledge.get("capability") != knowledge_capability(state)
        ):
            raise ReviewError(
                "найденный корпус требует корень, состав и возможность "
                "kc-validation",
            )
        if any(
            not item.get("reference") or not item.get("sha256")
            for item in knowledge["subjects"]
        ):
            raise ReviewError("состав корпуса содержит неполное свидетельство")
        technical_states = {
            "technical_running",
            "technical_blocked",
            "admitted",
            "admitted_with_limits",
            "semantic_running",
            "semantic_failed",
            "checked",
        }
        if knowledge_status in technical_states:
            technical = validate_knowledge_phase_application(
                state,
                knowledge,
                knowledge.get("technical_application"),
                "technical",
                "running" if knowledge_status == "technical_running" else "complete",
            )
            if knowledge_status == "technical_blocked" and (
                technical.get("outcome") != "failed"
            ):
                raise ReviewError("technical_blocked требует неуспешный допуск")
            if knowledge_status == "admitted" and (
                technical.get("outcome") != "passed"
            ):
                raise ReviewError("admitted требует успешный технический допуск")
            if knowledge_status == "admitted_with_limits" and (
                technical.get("outcome") != "failed"
            ):
                raise ReviewError(
                    "admitted_with_limits требует допуск с ограничениями",
                )
        if knowledge_status in {"semantic_running", "semantic_failed", "checked"}:
            semantic = validate_knowledge_phase_application(
                state,
                knowledge,
                knowledge.get("semantic_application"),
                "semantic",
                "running" if knowledge_status == "semantic_running" else "complete",
            )
            if knowledge_status == "checked" and (
                semantic.get("outcome") != "passed"
                or semantic.get("decision") != "accept"
            ):
                raise ReviewError(
                    "checked требует принятую успешную смысловую проверку",
                )
    if knowledge_status in {"pending", "located"} and any(
        knowledge.get(field) is not None
        for field in (
            "technical_application",
            "technical_outcome",
            "semantic_application",
            "semantic_outcome",
        )
    ):
        raise ReviewError(
            "корпус без запущенной фазы не допускает сохранённое применение",
        )
    if knowledge_status == "technical_running" and (
        knowledge.get("technical_outcome") is not None
        or knowledge.get("semantic_application") is not None
        or knowledge.get("semantic_outcome") is not None
    ):
        raise ReviewError(
            "техническая фаза не допускает результат или смысловое применение",
        )
    if knowledge_status in {
        "technical_blocked",
        "admitted",
        "admitted_with_limits",
    } and (
        knowledge.get("semantic_application") is not None
        or knowledge.get("semantic_outcome") is not None
    ):
        raise ReviewError(
            "смысловая фаза не начата, но уже сохранена в состоянии корпуса",
        )
    if knowledge_status == "semantic_running" and (
        knowledge.get("semantic_outcome") is not None
    ):
        raise ReviewError("активная смысловая фаза не может иметь результат")
    if knowledge.get("status") == "checked" and not knowledge_review_is_proven(
        state,
    ):
        raise ReviewError(
            "статус checked требует доказанные техническую и смысловую "
            "проверки корпуса",
        )
    if state.get("status") not in PROCESS_STATES:
        raise ReviewError("состояние процесса неизвестно")
    if state["status"] in TERMINAL_STATES:
        validate_completion(state, state["status"])
    elif not state.get("next_action"):
        raise ReviewError("активное состояние требует next_action")
    if state["status"] == "waiting_decision":
        brief = state.get("decision_brief")
        if not isinstance(brief, dict) or not brief.get("message"):
            raise ReviewError(
                "ожидание решения требует самодостаточный запрос владельцу",
            )
        if brief.get("input_snapshot") != state["snapshot"]["id"]:
            raise ReviewError("запрос решения относится к другому снимку")


def restart_review(
    repo: Path,
    path: Path,
    state: dict[str, Any],
    mode: str,
    controller: str | None,
    controller_proven: bool,
    discard_incomplete: bool = False,
) -> tuple[dict[str, Any], Path]:
    if state["status"] not in TERMINAL_STATES and not discard_incomplete:
        raise ReviewError("restart допустим только после завершения проверки")
    archive = review_history_path(path, state)
    archived_state = json.loads(json.dumps(state))
    archived_state["archived_at"] = now()
    if state["status"] not in TERMINAL_STATES:
        archived_state["discarded_at"] = now()
    atomic_write(archive, archived_state)
    restarted = new_state(repo, mode, controller, controller_proven)
    restarted["previous_review"] = {
        "archive": str(archive),
        "status": state["status"],
        "snapshot": state["snapshot"]["id"],
    }
    add_history(
        restarted,
        "review_restarted",
        previous_status=state["status"],
        previous_snapshot=state["snapshot"]["id"],
        archive=str(archive),
        discarded_incomplete=(
            state["status"] not in TERMINAL_STATES and discard_incomplete
        ),
    )
    return restarted, archive


def add_common_repo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=Path, default=Path.cwd())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="operation", required=True)

    inventory_parser = commands.add_parser("inventory")
    add_common_repo(inventory_parser)
    inventory_parser.add_argument("--classification", type=Path, default=CLASSIFICATION)

    init_parser = commands.add_parser("init")
    add_common_repo(init_parser)
    init_parser.add_argument("--mode", choices=("managed", "manual"), required=True)
    init_parser.add_argument("--controller")
    init_parser.add_argument("--controller-proven", action="store_true")

    restart_parser = commands.add_parser("restart")
    add_common_repo(restart_parser)
    restart_parser.add_argument(
        "--mode",
        choices=("managed", "manual"),
        required=True,
    )
    restart_parser.add_argument("--controller")
    restart_parser.add_argument("--controller-proven", action="store_true")
    restart_parser.add_argument("--discard-incomplete", action="store_true")

    for name in ("show", "validate", "assert-terminal", "refresh"):
        subparser = commands.add_parser(name)
        add_common_repo(subparser)

    accept_snapshot_parser = commands.add_parser("accept-snapshot")
    add_common_repo(accept_snapshot_parser)
    accept_snapshot_parser.add_argument(
        "--finding",
        action="append",
        default=[],
    )
    accept_snapshot_parser.add_argument(
        "--external-path",
        action="append",
        default=[],
    )
    accept_snapshot_parser.add_argument("--external-reason")
    accept_snapshot_parser.add_argument(
        "--reopen-stage",
        action="append",
        default=[],
    )
    accept_snapshot_parser.add_argument("--impact-rationale")

    next_parser = commands.add_parser("next")
    add_common_repo(next_parser)
    next_parser.add_argument("--action", required=True)

    transition_parser = commands.add_parser("transition")
    add_common_repo(transition_parser)
    transition_parser.add_argument("--to", required=True)
    transition_parser.add_argument("--next-action")

    stage_parser = commands.add_parser("stage")
    add_common_repo(stage_parser)
    stage_parser.add_argument("--name", required=True)
    stage_parser.add_argument("--status", required=True)

    classify_parser = commands.add_parser("classify")
    add_common_repo(classify_parser)
    classify_parser.add_argument("--id", required=True)
    classify_parser.add_argument("--participation", required=True)
    classify_parser.add_argument("--stage")
    classify_parser.add_argument(
        "--applicable",
        choices=("yes", "no", "unknown"),
        required=True,
    )
    classify_parser.add_argument("--reason", required=True)

    concept_parser = commands.add_parser("record-concept")
    add_common_repo(concept_parser)
    concept_parser.add_argument(
        "--result",
        choices=("found", "missing", "ambiguous"),
        required=True,
    )
    concept_parser.add_argument("--instructions")
    concept_parser.add_argument("--concept", action="append", default=[])
    concept_parser.add_argument("--evidence", action="append", default=[])

    knowledge_parser = commands.add_parser("record-knowledge")
    add_common_repo(knowledge_parser)
    knowledge_parser.add_argument(
        "--result",
        choices=("found", "absent"),
        required=True,
    )
    knowledge_parser.add_argument("--root")
    knowledge_parser.add_argument("--evidence", action="append", default=[])

    finding_parser = commands.add_parser("record-finding")
    add_common_repo(finding_parser)
    finding_parser.add_argument("--id", required=True)
    finding_parser.add_argument("--stage", required=True)
    finding_parser.add_argument("--summary", required=True)
    finding_parser.add_argument("--blocking", action="store_true")
    finding_parser.add_argument("--evidence", action="append", default=[])
    finding_parser.add_argument("--observation", action="append", default=[])
    finding_parser.add_argument("--group")
    finding_parser.add_argument("--allowed-path", action="append", default=[])
    finding_parser.add_argument("--verification")

    decision_parser = commands.add_parser("record-decision")
    add_common_repo(decision_parser)
    decision_parser.add_argument("--finding", required=True)
    decision_parser.add_argument(
        "--decision",
        choices=("fix", "accept", "defer", "not_applicable"),
        required=True,
    )
    decision_parser.add_argument("--reason")
    decision_parser.add_argument("--revisit-condition")

    prepare_decision_parser = commands.add_parser("prepare-decision")
    add_common_repo(prepare_decision_parser)
    prepare_decision_parser.add_argument("--finding", required=True)
    prepare_decision_parser.add_argument("--review-context", required=True)
    prepare_decision_parser.add_argument("--checked-subject", required=True)
    prepare_decision_parser.add_argument("--relation", required=True)
    prepare_decision_parser.add_argument(
        "--problem",
        action="append",
        default=[],
    )
    prepare_decision_parser.add_argument("--impact", required=True)
    prepare_decision_parser.add_argument("--proposed-change", required=True)
    prepare_decision_parser.add_argument("--decision-question", required=True)

    migrate_parser = commands.add_parser("migrate")
    add_common_repo(migrate_parser)

    start_parser = commands.add_parser("start-application")
    add_common_repo(start_parser)
    start_parser.add_argument("--id", required=True)
    start_parser.add_argument("--stage", required=True)
    start_parser.add_argument("--capability")
    start_parser.add_argument("--finding")
    start_parser.add_argument(
        "--method",
        choices=sorted(APPLICATION_METHODS),
        required=True,
    )
    start_parser.add_argument("--surface", required=True)
    start_parser.add_argument("--action", required=True)
    start_parser.add_argument("--priority-rationale", required=True)
    start_parser.add_argument(
        "--knowledge-phase",
        choices=("technical", "semantic"),
    )
    start_parser.add_argument("--subject", action="append", default=[])
    start_parser.add_argument("--subject-index", action="append", default=[])
    start_parser.add_argument("--subject-pattern", action="append", default=[])
    start_parser.add_argument("--subject-type", action="append", default=[])

    observation_parser = commands.add_parser("record-observation")
    add_common_repo(observation_parser)
    observation_parser.add_argument("--application", required=True)
    observation_parser.add_argument("--artifact", required=True)
    observation_parser.add_argument("--start-line", type=int, required=True)
    observation_parser.add_argument("--end-line", type=int, required=True)
    observation_parser.add_argument("--criterion-id", required=True)
    observation_parser.add_argument("--criterion")
    observation_parser.add_argument(
        "--result",
        choices=sorted(OBSERVATION_RESULTS),
        required=True,
    )
    observation_parser.add_argument("--note", required=True)

    finish_parser = commands.add_parser("finish-application")
    add_common_repo(finish_parser)
    finish_parser.add_argument("--application", required=True)
    finish_parser.add_argument(
        "--outcome",
        choices=sorted(APPLICATION_OUTCOMES),
        required=True,
    )
    finish_parser.add_argument("--decision", choices=sorted(REVIEW_DECISIONS))
    finish_parser.add_argument("--evidence", action="append", default=[])
    finish_parser.add_argument("--artifact", action="append", default=[])
    finish_parser.add_argument("--coverage")
    finish_parser.add_argument("--claim", action="append", default=[])
    finish_parser.add_argument("--claim-support", action="append", default=[])
    finish_parser.add_argument("--challenge")
    finish_parser.add_argument(
        "--challenge-outcome",
        choices=sorted(CHALLENGE_OUTCOMES),
    )
    finish_parser.add_argument(
        "--challenge-support",
        action="append",
        default=[],
    )
    finish_parser.add_argument("--command")
    return parser


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    args = build_parser().parse_args()
    try:
        repo = repository_root(args.repo)
        if args.operation == "inventory":
            print_json(inventory(repo, args.classification.resolve()))
            return
        path = state_path(repo)
        if args.operation == "init":
            if path.exists():
                raise ReviewError(f"состояние уже существует: {path}")
            state = new_state(
                repo,
                args.mode,
                args.controller,
                args.controller_proven,
            )
            atomic_write(path, state)
            print_json({"state_path": str(path), "state": state})
            return
        if args.operation == "migrate":
            state = load_json(path)
            if state.get("workspace_id") != workspace_id(repo):
                raise ReviewError(
                    "состояние относится к другому рабочему каталогу",
                )
            archive = review_history_path(path, state)
            archived_state = json.loads(json.dumps(state))
            archived_state["archived_at"] = now()
            archived_state["archive_reason"] = "schema_migration"
            atomic_write(archive, archived_state)
            migrate_state(state)
            validate_state(state)
            atomic_write(path, state)
            print_json(
                {
                    "state_path": str(path),
                    "archive": str(archive),
                    "status": state["status"],
                },
            )
            return

        path, state = load_state(repo)
        if args.operation == "show":
            print_json(state)
            return
        if args.operation == "validate":
            validate_state(state)
            if state["status"] in TERMINAL_STATES:
                print("Project review is complete")
            else:
                print("Project review state is internally consistent; "
                      "review remains in progress")
            return
        if args.operation == "assert-terminal":
            validate_state(state)
            require_terminal_state(state)
            print(f"Project review has terminal status: {state['status']}")
            return
        if args.operation == "restart":
            restarted, archive = restart_review(
                repo,
                path,
                state,
                args.mode,
                args.controller,
                args.controller_proven,
                args.discard_incomplete,
            )
            atomic_write(path, restarted)
            print_json(
                {
                    "state_path": str(path),
                    "archive": str(archive),
                    "status": restarted["status"],
                },
            )
            return
        require_nonterminal(state)
        if args.operation == "refresh":
            refresh(state, repo)
        elif args.operation == "accept-snapshot":
            accept_pending_snapshot(
                state,
                repo,
                args.finding,
                args.external_path,
                args.external_reason,
                args.reopen_stage,
                args.impact_rationale,
            )
        elif args.operation == "next":
            set_next(state, args.action)
        elif args.operation == "transition":
            transition(state, args.to, args.next_action, repo)
        elif args.operation == "stage":
            set_stage(state, args.name, args.status, repo)
        elif args.operation == "classify":
            classify_capability(state, args)
        elif args.operation == "record-concept":
            record_concept_discovery(state, args, repo)
        elif args.operation == "record-knowledge":
            record_knowledge_discovery(state, args, repo)
        elif args.operation == "record-finding":
            record_finding(state, args)
        elif args.operation == "record-decision":
            record_decision(state, args)
        elif args.operation == "prepare-decision":
            brief = prepare_decision(state, args, repo)
            validate_state(state)
            atomic_write(path, state)
            print_json(
                {
                    "state_path": str(path),
                    "status": state["status"],
                    "decision_brief": brief,
                },
            )
            return
        elif args.operation == "start-application":
            start_application(state, args, repo)
        elif args.operation == "record-observation":
            observation = record_observation(state, args, repo)
            validate_state(state)
            atomic_write(path, state)
            print_json(
                {
                    "state_path": str(path),
                    "status": state["status"],
                    "observation": observation,
                },
            )
            return
        elif args.operation == "finish-application":
            finish_application(state, args, repo)
        validate_state(state)
        atomic_write(path, state)
        print_json({"state_path": str(path), "status": state["status"]})
    except ReviewError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
