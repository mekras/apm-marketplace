#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from collections import deque
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = {1, 2}
NODE_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
NODE_KINDS = {
    "concept",
    "knowledge",
    "requirements",
    "architecture",
    "decision",
    "implementation",
    "test",
    "documentation",
    "operation",
    "process",
    "configuration",
    "delivery",
    "other",
}
RELATIONS = {
    "constrains",
    "informs",
    "refines",
    "realizes",
    "verifies",
    "documents",
    "configures",
    "packages",
    "operates",
    "governs",
}
FACETS = {
    "semantic",
    "interface",
    "data",
    "quality",
    "security",
    "reliability",
    "operation",
    "structure",
    "documentation",
}
REVIEW_STAGES = {
    "repository",
    "requirements",
    "design",
    "code",
    "tests",
    "assurance",
    "impact",
}
AUTHORITIES = {"canonical", "supporting", "derived"}
REPRESENTATION_ROLES = {
    "canonical",
    "supporting",
    "navigation",
    "implementation",
    "verification",
}
STATUSES = {
    "updated",
    "verified_no_impact",
    "human_decision",
    "blocked",
}
PASSING_STATUSES = {"updated", "verified_no_impact"}
SEMANTIC_GRAPH_SCHEMA_VERSION = 2


class ContractError(ValueError):
    pass


def require_mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{location}: expected object")
    return value


def require_list(value: Any, location: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list):
        raise ContractError(f"{location}: expected array")
    if nonempty and not value:
        raise ContractError(f"{location}: must not be empty")
    return value


def require_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{location}: expected non-empty string")
    return value


def reject_unknown(
    value: dict[str, Any],
    allowed: set[str],
    location: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ContractError(
            f"{location}: unknown fields: {', '.join(unknown)}",
        )


def normalize_path(raw_path: str, location: str) -> str:
    value = require_string(raw_path, location).replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ContractError(f"{location}: path must stay inside the project")
    normalized = str(path)
    if normalized in {"", "."}:
        raise ContractError(f"{location}: path must identify an artifact")
    return normalized


def validate_string_list(
    value: Any,
    location: str,
    *,
    nonempty: bool = False,
) -> list[str]:
    items = require_list(value, location, nonempty=nonempty)
    result = [
        require_string(item, f"{location}[{index}]")
        for index, item in enumerate(items)
    ]
    if len(result) != len(set(result)):
        raise ContractError(f"{location}: duplicate values")
    return result


def load_graph(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"graph not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(
            f"{path}:{exc.lineno}:{exc.colno}: {exc.msg}",
        ) from exc
    return validate_graph(data)


def validate_graph(data: Any) -> dict[str, Any]:
    root = require_mapping(data, "root")
    reject_unknown(root, {"schema_version", "graph", "nodes", "edges"}, "root")
    schema_version = root.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ContractError(
            "root.schema_version: expected one of "
            + ", ".join(str(version) for version in sorted(SUPPORTED_SCHEMA_VERSIONS)),
        )

    graph = require_mapping(root.get("graph"), "root.graph")
    reject_unknown(
        graph,
        {
            "name",
            "description",
            "default_facet",
            "unmapped_paths",
            "ignore",
        },
        "root.graph",
    )
    require_string(graph.get("name"), "root.graph.name")
    require_string(graph.get("description"), "root.graph.description")
    default_facet = require_string(
        graph.get("default_facet"),
        "root.graph.default_facet",
    )
    if default_facet not in FACETS:
        raise ContractError(
            "root.graph.default_facet: unsupported facet "
            f"{default_facet!r}",
        )
    if graph.get("unmapped_paths") not in {"block", "warn"}:
        raise ContractError(
            "root.graph.unmapped_paths: expected 'block' or 'warn'",
        )
    graph["ignore"] = [
        normalize_path(item, f"root.graph.ignore[{index}]")
        for index, item in enumerate(
            require_list(graph.get("ignore"), "root.graph.ignore"),
        )
    ]
    if len(graph["ignore"]) != len(set(graph["ignore"])):
        raise ContractError("root.graph.ignore: duplicate values")

    nodes = require_list(root.get("nodes"), "root.nodes", nonempty=True)
    node_ids: set[str] = set()
    for index, raw_node in enumerate(nodes):
        location = f"root.nodes[{index}]"
        node = require_mapping(raw_node, location)
        allowed_fields = {
            "id",
            "title",
            "kind",
            "checks",
            "review_stages",
        }
        if schema_version == 1:
            allowed_fields.add("paths")
        else:
            allowed_fields.update(
                {
                    "semantic_type",
                    "authority",
                    "paths",
                    "representations",
                },
            )
        reject_unknown(node, allowed_fields, location)
        node_id = require_string(node.get("id"), f"{location}.id")
        if not NODE_ID_RE.fullmatch(node_id):
            raise ContractError(f"{location}.id: unsupported identifier")
        if node_id in node_ids:
            raise ContractError(f"{location}.id: duplicate {node_id!r}")
        node_ids.add(node_id)
        require_string(node.get("title"), f"{location}.title")
        kind = require_string(node.get("kind"), f"{location}.kind")
        if kind not in NODE_KINDS:
            raise ContractError(f"{location}.kind: unsupported kind {kind!r}")
        if schema_version == 1:
            node["paths"] = [
                normalize_path(item, f"{location}.paths[{path_index}]")
                for path_index, item in enumerate(
                    require_list(
                        node.get("paths"),
                        f"{location}.paths",
                        nonempty=True,
                    ),
                )
            ]
            if len(node["paths"]) != len(set(node["paths"])):
                raise ContractError(f"{location}.paths: duplicate values")
        else:
            require_string(node.get("semantic_type"), f"{location}.semantic_type")
            authority = require_string(node.get("authority"), f"{location}.authority")
            if authority not in AUTHORITIES:
                raise ContractError(
                    f"{location}.authority: unsupported authority {authority!r}",
                )
            representations = node.get("representations")
            if representations is None:
                paths = require_list(
                    node.get("paths"),
                    f"{location}.paths",
                    nonempty=True,
                )
                default_role = (
                    "canonical" if authority == "canonical" else "supporting"
                )
                representations = [
                    {"path": path, "role": default_role}
                    for path in paths
                ]
            else:
                representations = require_list(
                    representations,
                    f"{location}.representations",
                    nonempty=True,
                )
            normalized_representations = []
            representation_paths: set[str] = set()
            for representation_index, raw_representation in enumerate(representations):
                representation_location = (
                    f"{location}.representations[{representation_index}]"
                )
                representation = require_mapping(raw_representation, representation_location)
                reject_unknown(
                    representation,
                    {"path", "role"},
                    representation_location,
                )
                path = normalize_path(
                    representation.get("path"),
                    f"{representation_location}.path",
                )
                role = require_string(
                    representation.get("role"),
                    f"{representation_location}.role",
                )
                if role not in REPRESENTATION_ROLES:
                    raise ContractError(
                        f"{representation_location}.role: unsupported role {role!r}",
                    )
                if path in representation_paths:
                    raise ContractError(
                        f"{location}.representations: duplicate path {path!r}",
                    )
                representation_paths.add(path)
                normalized_representations.append({"path": path, "role": role})
            if authority == "canonical" and not any(
                item["role"] == "canonical"
                for item in normalized_representations
            ):
                raise ContractError(
                    f"{location}: canonical artifact requires a canonical representation",
                )
            node["representations"] = normalized_representations
            node["paths"] = [item["path"] for item in normalized_representations]
        validate_string_list(
            node.get("checks"),
            f"{location}.checks",
            nonempty=True,
        )
        stages = validate_string_list(
            node.get("review_stages", []),
            f"{location}.review_stages",
        )
        unsupported_stages = sorted(set(stages) - REVIEW_STAGES)
        if unsupported_stages:
            raise ContractError(
                f"{location}.review_stages: unsupported stages: "
                + ", ".join(unsupported_stages),
            )

    edges = require_list(root.get("edges"), "root.edges")
    edge_keys: set[tuple[str, str, str]] = set()
    for index, raw_edge in enumerate(edges):
        location = f"root.edges[{index}]"
        edge = require_mapping(raw_edge, location)
        reject_unknown(
            edge,
            {"from", "to", "relation", "facets", "rationale"},
            location,
        )
        source = require_string(edge.get("from"), f"{location}.from")
        target = require_string(edge.get("to"), f"{location}.to")
        if source not in node_ids:
            raise ContractError(f"{location}.from: unknown node {source!r}")
        if target not in node_ids:
            raise ContractError(f"{location}.to: unknown node {target!r}")
        if source == target:
            raise ContractError(f"{location}: self-edge is not allowed")
        relation = require_string(edge.get("relation"), f"{location}.relation")
        if relation not in RELATIONS:
            raise ContractError(
                f"{location}.relation: unsupported relation {relation!r}",
            )
        facets = validate_string_list(
            edge.get("facets"),
            f"{location}.facets",
            nonempty=True,
        )
        unsupported_facets = sorted(set(facets) - FACETS - {"any"})
        if unsupported_facets:
            raise ContractError(
                f"{location}.facets: unsupported facets: "
                + ", ".join(unsupported_facets),
            )
        require_string(edge.get("rationale"), f"{location}.rationale")
        key = (source, target, relation)
        if key in edge_keys:
            raise ContractError(
                f"{location}: duplicate edge {source} -> {target} ({relation})",
            )
        edge_keys.add(key)

    return root


def matches(path: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(path, pattern)


def ignored(graph: dict[str, Any], path: str) -> bool:
    return any(matches(path, pattern) for pattern in graph["graph"]["ignore"])


def nodes_for_path(graph: dict[str, Any], path: str) -> list[str]:
    return sorted(
        node["id"]
        for node in graph["nodes"]
        if any(matches(path, pattern) for pattern in node["paths"])
    )


def parse_changed_node(raw: str, default_facet: str) -> tuple[str, str]:
    node_id, separator, facet = raw.partition(":")
    if not separator:
        facet = default_facet
    if not NODE_ID_RE.fullmatch(node_id):
        raise ContractError(f"invalid changed node: {raw!r}")
    if facet not in FACETS:
        raise ContractError(f"invalid change facet in {raw!r}")
    return node_id, facet


def changed_states(
    graph: dict[str, Any],
    changed_nodes: list[str],
    changed_paths: list[str],
    facet: str | None,
) -> tuple[set[tuple[str, str]], list[dict[str, Any]], list[str]]:
    default_facet = facet or graph["graph"]["default_facet"]
    if default_facet not in FACETS:
        raise ContractError(f"unsupported facet: {default_facet!r}")
    node_by_id = {node["id"]: node for node in graph["nodes"]}
    states: set[tuple[str, str]] = set()
    sources: list[dict[str, Any]] = []
    unmapped: list[str] = []

    for raw in changed_nodes:
        node_id, node_facet = parse_changed_node(raw, default_facet)
        if node_id not in node_by_id:
            raise ContractError(f"unknown changed node: {node_id!r}")
        states.add((node_id, node_facet))
        sources.append(
            {"input": raw, "nodes": [node_id], "facet": node_facet},
        )

    for index, raw_path in enumerate(changed_paths):
        path = normalize_path(raw_path, f"changed_paths[{index}]")
        if ignored(graph, path):
            sources.append(
                {"input": path, "nodes": [], "facet": default_facet, "ignored": True},
            )
            continue
        node_ids = nodes_for_path(graph, path)
        if not node_ids:
            unmapped.append(path)
        for node_id in node_ids:
            states.add((node_id, default_facet))
        sources.append(
            {"input": path, "nodes": node_ids, "facet": default_facet},
        )

    if not states and not sources:
        raise ContractError(
            "at least one --changed-node or --changed-path is required",
        )
    if unmapped and graph["graph"]["unmapped_paths"] == "block":
        raise ContractError("unmapped changed paths: " + ", ".join(unmapped))
    return states, sources, unmapped


def trace_states(
    graph: dict[str, Any],
    initial: set[tuple[str, str]],
) -> dict[tuple[str, str], list[str]]:
    outgoing: dict[str, list[dict[str, Any]]] = {}
    for edge in graph["edges"]:
        outgoing.setdefault(edge["from"], []).append(edge)
    for edges in outgoing.values():
        edges.sort(key=lambda edge: (edge["to"], edge["relation"]))

    paths = {
        state: [state[0]]
        for state in sorted(initial)
    }
    queue = deque(sorted(initial))
    while queue:
        node_id, facet = queue.popleft()
        for edge in outgoing.get(node_id, []):
            if facet not in edge["facets"] and "any" not in edge["facets"]:
                continue
            state = (edge["to"], facet)
            if state in paths:
                continue
            paths[state] = [*paths[(node_id, facet)], edge["to"]]
            queue.append(state)
    return paths


def trace_result(
    graph: dict[str, Any],
    changed_nodes: list[str],
    changed_paths: list[str],
    facet: str | None,
) -> dict[str, Any]:
    initial, sources, unmapped = changed_states(
        graph,
        changed_nodes,
        changed_paths,
        facet,
    )
    paths = trace_states(graph, initial)
    nodes = {node["id"]: node for node in graph["nodes"]}
    affected_states = sorted(set(paths) - initial)
    affected_by_node: dict[str, dict[str, Any]] = {}
    for node_id, state_facet in affected_states:
        entry = affected_by_node.setdefault(
            node_id,
            {
                "id": node_id,
                "title": nodes[node_id]["title"],
                "kind": nodes[node_id]["kind"],
                "semantic_type": nodes[node_id].get("semantic_type"),
                "authority": nodes[node_id].get("authority"),
                "facets": [],
                "paths": [],
                "checks": nodes[node_id]["checks"],
                "review_stages": nodes[node_id].get("review_stages", []),
                "status": "unresolved",
            },
        )
        entry["facets"].append(state_facet)
        entry["paths"].append(paths[(node_id, state_facet)])

    review_stages = sorted(
        {
            stage
            for entry in affected_by_node.values()
            for stage in entry["review_stages"]
        },
    )
    changed = [
        {
            "id": node_id,
            "title": nodes[node_id]["title"],
            "facet": state_facet,
        }
        for node_id, state_facet in sorted(initial)
    ]
    return {
        "graph": graph["graph"]["name"],
        "changed_inputs": sources,
        "changed": changed,
        "affected": list(affected_by_node.values()),
        "review_stages": review_stages,
        "unmapped_paths": unmapped,
        "complete": not affected_by_node,
    }


def parse_statuses(raw_statuses: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in raw_statuses:
        node_id, separator, status = raw.partition("=")
        if not separator or not NODE_ID_RE.fullmatch(node_id):
            raise ContractError(f"invalid status assignment: {raw!r}")
        if status not in STATUSES:
            raise ContractError(f"unsupported status in {raw!r}")
        if node_id in result:
            raise ContractError(f"duplicate status for node {node_id!r}")
        result[node_id] = status
    return result


def assess_result(result: dict[str, Any], raw_statuses: list[str]) -> tuple[dict[str, Any], int]:
    statuses = parse_statuses(raw_statuses)
    affected_ids = {entry["id"] for entry in result["affected"]}
    unknown = sorted(set(statuses) - affected_ids)
    if unknown:
        raise ContractError(
            "statuses supplied for unaffected nodes: " + ", ".join(unknown),
        )
    missing = sorted(affected_ids - set(statuses))
    for entry in result["affected"]:
        entry["status"] = statuses.get(entry["id"], "unresolved")
    blocking = sorted(
        node_id
        for node_id, status in statuses.items()
        if status not in PASSING_STATUSES
    )
    result["missing_statuses"] = missing
    result["blocking_statuses"] = blocking
    result["complete"] = not missing and not blocking
    return result, 0 if result["complete"] else 3


def repository_paths(repo: Path) -> list[str]:
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                "core.quotepath=false",
                "-C",
                str(repo),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            check=True,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return sorted(
            path.relative_to(repo).as_posix()
            for path in repo.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(repo).parts
        )
    return sorted(
        os.fsdecode(path)
        for path in completed.stdout.split(b"\0")
        if path
    )


def coverage_result(graph: dict[str, Any], repo: Path) -> tuple[dict[str, Any], int]:
    if not repo.is_dir():
        raise ContractError(f"repository directory not found: {repo}")
    unmapped: list[str] = []
    mapped = 0
    ignored_count = 0
    for path in repository_paths(repo):
        if ignored(graph, path):
            ignored_count += 1
        elif nodes_for_path(graph, path):
            mapped += 1
        else:
            unmapped.append(path)
    result = {
        "graph": graph["graph"]["name"],
        "repo": str(repo.resolve()),
        "mapped_paths": mapped,
        "ignored_paths": ignored_count,
        "unmapped_paths": unmapped,
        "complete": not unmapped,
    }
    if unmapped and graph["graph"]["unmapped_paths"] == "block":
        return result, 3
    return result, 0


def validation_result(graph: dict[str, Any]) -> tuple[dict[str, Any], int]:
    result = {
        "graph": graph["graph"]["name"],
        "nodes": len(graph["nodes"]),
        "edges": len(graph["edges"]),
        "schema_version": graph["schema_version"],
    }
    if graph["schema_version"] != SEMANTIC_GRAPH_SCHEMA_VERSION:
        result.update(
            {
                "status": "incomplete",
                "semantic_model": False,
                "problems": [
                    {
                        "code": "semantic-model-unavailable",
                        "message": (
                            "schema_version 1 has no semantic types, authorities, "
                            "or representation roles"
                        ),
                    },
                ],
            },
        )
        return result, 3
    result.update({"status": "ok", "semantic_model": True, "problems": []})
    return result, 0


def add_trace_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--graph", required=True, type=Path)
    parser.add_argument("--changed-node", action="append", default=[])
    parser.add_argument("--changed-path", action="append", default=[])
    parser.add_argument("--facet", choices=sorted(FACETS))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and traverse a project impact graph.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--graph", required=True, type=Path)

    trace_parser = subparsers.add_parser("trace")
    add_trace_arguments(trace_parser)

    assess_parser = subparsers.add_parser("assess")
    add_trace_arguments(assess_parser)
    assess_parser.add_argument("--status", action="append", default=[])

    coverage_parser = subparsers.add_parser("coverage")
    coverage_parser.add_argument("--graph", required=True, type=Path)
    coverage_parser.add_argument("--repo", required=True, type=Path)
    return parser


def emit(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    args = build_parser().parse_args()
    try:
        graph = load_graph(args.graph)
        if args.command == "validate":
            result, exit_code = validation_result(graph)
            emit(result)
            return exit_code
        if args.command == "coverage":
            result, exit_code = coverage_result(graph, args.repo)
            emit(result)
            return exit_code
        result = trace_result(
            graph,
            args.changed_node,
            args.changed_path,
            args.facet,
        )
        if args.command == "assess":
            result, exit_code = assess_result(result, args.status)
            emit(result)
            return exit_code
        emit(result)
        return 0
    except ContractError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
