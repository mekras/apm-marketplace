#!/usr/bin/env python3
"""Evaluate a consumer-owned source-quality policy using only JSON and stdlib."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any


class QualityError(RuntimeError):
    pass


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QualityError(f"Cannot read JSON {path}: {exc}") from exc


def checked_age(value: Any, today: date) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return (today - datetime.fromisoformat(value.replace("Z", "+00:00")).date()).days
    except ValueError:
        return None


def event_key(event: dict[str, Any], identity: list[str]) -> tuple[str, ...]:
    return tuple(str(event.get(field, "")).strip().lower() for field in identity)


def evaluate(policy: dict[str, Any], sources: list[dict[str, Any]], events: list[dict[str, Any]], today: date) -> dict[str, Any]:
    if policy.get("contract_version") != 1 or not isinstance(policy.get("areas"), list):
        raise QualityError("source quality policy must have contract_version 1 and areas")
    exceptions = {item.get("area_id"): item for item in policy.get("owner_exceptions", []) if isinstance(item, dict) and isinstance(item.get("area_id"), str) and item.get("reason")}
    coverage: list[dict[str, Any]] = []
    recommendations: list[dict[str, str]] = []
    for area in policy["areas"]:
        if not isinstance(area, dict) or not isinstance(area.get("id"), str):
            raise QualityError("each area needs a string id")
        matches = [source for source in sources if area["id"] in source.get("areas", [])]
        roles = set(role for source in matches for role in source.get("roles", []) if isinstance(role, str))
        primary = [source for source in matches if source.get("primary") is True and "evidence" in source.get("roles", [])]
        runnable = [source for source in matches if source.get("adapter_status") == "ready" and "discovery" in source.get("roles", [])]
        max_age = area.get("max_successful_check_age_days")
        stale = [source.get("id", "<unknown>") for source in matches if isinstance(max_age, int) and (checked_age(source.get("last_successful_check"), today) is None or checked_age(source.get("last_successful_check"), today) > max_age)]
        problems: list[str] = []
        if "discovery" in area.get("required_roles", []) and not runnable:
            problems.append("missing_discovery")
        if "evidence" in area.get("required_roles", []) and not primary:
            problems.append("secondary_only_evidence")
        if len(primary) < int(area.get("minimum_primary_sources", 0)):
            problems.append("insufficient_primary_evidence")
        if stale:
            problems.append("stale_source")
        exception = exceptions.get(area["id"])
        state = "covered" if not problems else ("degraded" if exception else "missing")
        coverage.append({"area_id": area["id"], "state": state, "problems": problems, "stale_sources": stale, "owner_exception": exception})
        if problems:
            recommendations.append({"area_id": area["id"], "action": "keep" if exception else "add", "reason": ",".join(problems)})
        else:
            recommendations.append({"area_id": area["id"], "action": "keep", "reason": "healthy_coverage"})
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    areas_by_id = {area.get("id"): area for area in policy["areas"] if isinstance(area, dict)}
    default_identity = ["provider", "version", "canonical_url"]
    for event in events:
        if not isinstance(event, dict):
            continue
        identity = event.get("identity", default_identity)
        if not isinstance(identity, list) or not all(isinstance(key, str) for key in identity):
            raise QualityError("event identity must be a string list")
        grouped.setdefault(event_key(event, identity), []).append(event)
    merged: list[dict[str, Any]] = []
    for key, group in sorted(grouped.items()):
        area = areas_by_id.get(group[0].get("area_id"), {})
        preferred_id = area.get("preferred_canonical_source") if isinstance(area, dict) else None
        preferred = next((item for item in group if item.get("source_id") == preferred_id and item.get("primary") is True), None)
        preferred = preferred or next((item for item in group if item.get("canonical") is True and item.get("primary") is True), None)
        preferred = preferred or next((item for item in group if item.get("primary") is True), group[0])
        corroborating = [item.get("url") for item in group if item is not preferred and isinstance(item.get("url"), str)]
        conflicts = sorted({str(item.get("content_hash", "")) for item in group if item.get("content_hash")})
        merged.append({"event_key": list(key), "canonical_evidence": preferred.get("url"), "corroborating_evidence": corroborating, "conflict": len(conflicts) > 1})
    return {"contract_version": 1, "coverage": coverage, "recommendations": recommendations, "events": merged, "healthy": all(row["state"] == "covered" for row in coverage)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--sources", required=True, type=Path)
    parser.add_argument("--events", required=True, type=Path)
    parser.add_argument("--today", default=date.today().isoformat())
    args = parser.parse_args()
    try:
        result = evaluate(load_json(args.policy), load_json(args.sources), load_json(args.events), date.fromisoformat(args.today))
    except (QualityError, ValueError) as exc:
        print(json.dumps({"status": "invalid", "message": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
