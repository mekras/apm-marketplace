#!/usr/bin/env python3
"""Record a hash-bound verification for one corpus unit snapshot."""

from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - depends on the target project environment.
    yaml = None


ACQUISITION_METHODS = {
    "adapter_fetch",
    "provider_export",
    "local_file",
    "user_provided",
    "manual_copy",
    "unknown_legacy",
}
VERIFICATION_METHODS = {
    "direct_reopen",
    "export_comparison",
    "manual_confirmation",
    "local_integrity_only",
    "no_source_comparison",
}
CONTENT_SCOPES = {"full_text", "fragment", "none"}
CONTENT_RESULTS = {"verified", "partially_verified", "unverified", "mismatch"}
OVERALL_RESULTS = {"verified", "partially_verified", "unverified"}
COMPLETENESS_RESULTS = {"complete", "partial", "not_assessed"}
METADATA_FIELDS = {"locator", "author", "publication_date"}
METADATA_RESULTS = {"verified", "unverified", "mismatch"}


class VerificationError(RuntimeError):
    """The requested verification would violate the portable contract."""


def require_yaml() -> None:
    if yaml is None:
        raise VerificationError("Для записи verification.yml нужен модуль PyYAML.")


def load_mapping(path: Path) -> dict[str, Any]:
    require_yaml()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise VerificationError(f"Не удалось прочитать {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise VerificationError(f"{path} должен содержать словарь YAML.")
    return data


def write_yaml_atomically(path: Path, data: dict[str, Any]) -> None:
    require_yaml()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            yaml.safe_dump(data, stream, allow_unicode=True, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def parse_metadata_results(values: list[str], selected: set[str]) -> dict[str, str]:
    results = {field: "unverified" for field in sorted(METADATA_FIELDS)}
    for value in values:
        field, separator, result = value.partition("=")
        if not separator or field not in METADATA_FIELDS or result not in METADATA_RESULTS:
            raise VerificationError(
                "--metadata-result принимает locator|author|publication_date="
                "verified|unverified|mismatch."
            )
        if field not in selected and result != "unverified":
            raise VerificationError(
                f"Результат {field} выходит за явно выбранную область метаданных."
            )
        results[field] = result
    return results


def validate_semantics(args: argparse.Namespace, metadata_results: dict[str, str]) -> None:
    if args.content_scope == "fragment" and not args.fragment:
        raise VerificationError("Для области fragment нужен --fragment.")
    if args.content_scope == "none" and args.content_match != "unverified":
        raise VerificationError("Область none не может подтверждать совпадение текста.")
    if args.verification_method in {"local_integrity_only", "no_source_comparison"}:
        if args.content_match != "unverified" or args.overall_result != "unverified":
            raise VerificationError(
                f"{args.verification_method} не подтверждает совпадение с первоисточником."
            )
    if (
        args.acquisition_method == "user_provided"
        and args.verification_method == "no_source_comparison"
        and (args.content_match != "unverified" or args.overall_result != "unverified")
    ):
        raise VerificationError(
            "Предоставление материала пользователем не заменяет сверку с первоисточником."
        )
    if any(result != "unverified" for result in metadata_results.values()) and not args.metadata:
        raise VerificationError("Проверенные метаданные должны входить в --metadata.")


def resolve_unit_and_artifact(unit_raw: Path, artifact_raw: Path) -> tuple[Path, Path, str]:
    unit = unit_raw.resolve()
    if not unit.is_dir() or not (unit / "item.yml").is_file():
        raise VerificationError("Путь единицы должен быть каталогом с item.yml.")
    artifact = artifact_raw.resolve()
    try:
        relative = artifact.relative_to(unit)
    except ValueError as exc:
        raise VerificationError("Проверяемый артефакт должен принадлежать выбранной единице.") from exc
    if not artifact.is_file():
        raise VerificationError("Проверяемый артефакт не существует или не является файлом.")
    if relative.name in {"item.yml", "verification.yml", "statements.yml"}:
        raise VerificationError("Служебный YAML единицы нельзя использовать как снимок материала.")
    return unit, artifact, relative.as_posix()


def update_corpus_stage_contract(unit: Path) -> None:
    for parent in (unit, *unit.parents):
        contract_path = parent / "corpus.yml"
        if not contract_path.is_file():
            continue
        contract = load_mapping(contract_path)
        stages = contract.get("workflow_stages")
        if stages is None:
            return
        if not isinstance(stages, list) or not all(isinstance(stage, str) for stage in stages):
            raise VerificationError("corpus.yml workflow_stages должен быть списком строк.")
        if "verification_assessed" not in stages:
            stages.append("verification_assessed")
            write_yaml_atomically(contract_path, contract)
        return
    raise VerificationError("Для миграции единицы не найден corpus.yml в родительских каталогах.")


def build_verification(args: argparse.Namespace, artifact: Path, artifact_relative: str) -> dict[str, Any]:
    selected_metadata = set(args.metadata)
    metadata_results = parse_metadata_results(args.metadata_result, selected_metadata)
    validate_semantics(args, metadata_results)
    checked_at = args.checked_at or datetime.now(UTC).date().isoformat()
    scope: dict[str, Any] = {
        "content": args.content_scope,
        "metadata": sorted(selected_metadata),
    }
    if args.content_scope == "fragment":
        scope["fragment"] = args.fragment
    verification: dict[str, Any] = {
        "verification_contract_version": 1,
        "artifact": artifact_relative,
        "hash": {
            "algorithm": "sha256",
            "value": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        },
        "acquisition": {
            "method": args.acquisition_method,
            "recorded_at": args.acquired_at or checked_at,
        },
        "verification": {
            "method": args.verification_method,
            "checked_at": checked_at,
            "checked_by": {"role": args.checked_by_role},
            "scope": scope,
            "result": {
                "overall": args.overall_result,
                "content_match": args.content_match,
                "scope_completeness": args.scope_completeness,
                "metadata": metadata_results,
            },
            "limitations": args.limitation,
        },
    }
    if args.next_check_at:
        verification["next_check_at"] = args.next_check_at
    return verification


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Записать проверку текущего снимка одной единицы корпуса."
    )
    parser.add_argument("unit", type=Path, help="Каталог единицы с item.yml.")
    parser.add_argument("artifact", type=Path, help="Артефакт внутри выбранной единицы.")
    parser.add_argument(
        "--acquisition-method", required=True, choices=sorted(ACQUISITION_METHODS)
    )
    parser.add_argument(
        "--verification-method", required=True, choices=sorted(VERIFICATION_METHODS)
    )
    parser.add_argument("--content-scope", required=True, choices=sorted(CONTENT_SCOPES))
    parser.add_argument("--fragment", help="Точный подтверждённый фрагмент для области fragment.")
    parser.add_argument("--content-match", required=True, choices=sorted(CONTENT_RESULTS))
    parser.add_argument("--scope-completeness", required=True, choices=sorted(COMPLETENESS_RESULTS))
    parser.add_argument("--overall-result", required=True, choices=sorted(OVERALL_RESULTS))
    parser.add_argument(
        "--metadata", action="append", default=[], choices=sorted(METADATA_FIELDS)
    )
    parser.add_argument(
        "--metadata-result",
        action="append",
        default=[],
        metavar="FIELD=RESULT",
    )
    parser.add_argument("--checked-by-role", required=True)
    parser.add_argument("--checked-at")
    parser.add_argument("--acquired-at")
    parser.add_argument("--next-check-at")
    parser.add_argument("--limitation", action="append", default=[])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    unit, artifact, artifact_relative = resolve_unit_and_artifact(args.unit, args.artifact)
    verification = build_verification(args, artifact, artifact_relative)
    item_path = unit / "item.yml"
    item = load_mapping(item_path)
    item["item_contract_version"] = 2
    item["workflow_stage"] = "verification_assessed"
    update_corpus_stage_contract(unit)
    write_yaml_atomically(unit / "verification.yml", verification)
    write_yaml_atomically(item_path, item)
    print(f"Проверка снимка записана: {unit / 'verification.yml'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VerificationError as exc:
        print(f"Ошибка проверки снимка: {exc}", file=os.sys.stderr)
        raise SystemExit(2)
