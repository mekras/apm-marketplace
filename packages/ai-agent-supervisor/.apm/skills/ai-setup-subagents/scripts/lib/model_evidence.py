"""Проверка ссылки адаптера на свидетельство модели в журнале запуска."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def verify_model(header: object, journal: Path) -> dict:
    result = {
        "actual_model": None,
        "model_status": "unconfirmed",
        "model_evidence": None,
        "model_evidence_reason": "missing_evidence",
    }
    if not isinstance(header, dict):
        return result
    model = header.get("model")
    evidence = header.get("model_evidence")
    if not isinstance(model, str) or not model or not isinstance(evidence, dict):
        return result
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
        result["model_evidence_reason"] = "invalid_reference"
        return result
    try:
        raw = journal.read_bytes()
        value = json.loads(raw.splitlines()[line - 1])
        for key in field:
            if not isinstance(value, dict):
                raise ValueError("поле свидетельства не является объектом")
            value = value[key]
        if value != model:
            raise ValueError("модель не совпадает со свидетельством")
    except (OSError, IndexError, KeyError, ValueError, UnicodeError):
        result["model_evidence_reason"] = "unverifiable_reference"
        return result
    result.update(
        actual_model=model,
        model_status="confirmed",
        model_evidence={
            "source": "journal",
            "journal": str(journal),
            "line": line,
            "field": field,
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
        model_evidence_reason=None,
    )
    return result
