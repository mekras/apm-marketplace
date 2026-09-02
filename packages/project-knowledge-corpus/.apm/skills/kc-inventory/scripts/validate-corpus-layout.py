#!/usr/bin/env python3
"""Validate a project knowledge corpus layout.

This script validates the optional portable layout described in
kc-inventory/references/corpus-layout-contract.md. It is intentionally
generic: project-specific adapters and semantic checks should remain in the
project that owns the corpus.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from datetime import date
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - depends on target project environment.
    yaml = None


SOURCE_REQUIRED = {
    "id",
    "slug",
    "title",
    "access",
    "status",
    "carrier_type",
    "source_kind",
    "adapter",
    "reliability",
    "refresh_policy",
}

ITEM_REQUIRED = {
    "id",
    "title",
    "access",
    "status",
    "workflow_stage",
}

STATEMENT_REQUIRED = {
    "id",
    "source_id",
    "item_id",
    "status",
    "text",
    "excerpt",
    "artifact",
    "checked_at",
    "scope",
    "open_questions",
}

STATEMENT_V2_REQUIRED = {
    "id",
    "source_id",
    "item_id",
    "kind",
    "text",
    "excerpt",
    "artifact",
    "checked_at",
    "scope",
    "open_questions",
    "processing_status",
    "source_role",
    "evidence_strength",
    "confidence",
    "temporal_status",
    "corroboration",
    "limitations",
}

DERIVED_STATEMENT_REQUIRED = {
    "id",
    "kind",
    "status",
    "text",
    "derived_from",
    "derivation",
    "checked_at",
    "checked_by",
    "scope",
    "limitations",
    "open_questions",
}

CONCEPT_RECORD_REQUIRED = {
    "id",
    "primary",
    "definition",
    "boundaries",
    "authority",
    "defined_by",
}

DEFAULT_ALLOWED_STAGES = {
    "indexed",
    "needs_fetch",
    "fetched",
    "needs_transcript",
    "raw_transcribed",
    "normalized",
    "statements_extracted",
    "source_checked",
    "verification_assessed",
    "blocked",
    "rejected",
}

NORMALIZED_OR_LATER_STAGES = {
    "normalized",
    "statements_extracted",
    "source_checked",
    "verification_assessed",
}

STATEMENTS_OR_LATER_STAGES = {
    "statements_extracted",
    "source_checked",
    "verification_assessed",
}

ITEM_CONTRACT_VERSIONS = {1, 2}
VERIFICATION_ACQUISITION_METHODS = {
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
VERIFICATION_CONTENT_SCOPES = {"full_text", "fragment", "none"}
VERIFICATION_METADATA_FIELDS = {"locator", "author", "publication_date"}
VERIFICATION_MATCH_RESULTS = {"verified", "partially_verified", "unverified", "mismatch"}
VERIFICATION_METADATA_RESULTS = {"verified", "unverified", "mismatch"}
VERIFICATION_OVERALL_RESULTS = {"verified", "partially_verified", "unverified"}
VERIFICATION_COMPLETENESS_RESULTS = {"complete", "partial", "not_assessed"}
VERIFICATION_FORBIDDEN_FIELDS = {
    "availability",
    "access_status",
    "profile",
    "profile_name",
    "secret",
    "token",
    "password",
    "cookie",
    "session",
    "evidence_strength",
    "corroboration",
    "legal_conclusion",
}
USE_POLICY_STATUSES = {"permitted", "restricted", "unknown", "prohibited"}

DEFAULT_STATEMENT_KINDS = {
    "fact",
    "observation",
    "inference",
    "limitation",
}

DERIVED_STATEMENT_KINDS = {
    "observation",
    "inference",
    "limitation",
}

DERIVED_STATEMENT_STATUSES = {
    "candidate",
    "confirmed",
    "blocked",
    "rejected",
}

STATEMENT_PROCESSING_VALUES = {
    "extraction": {"complete"},
    "traceability": {"pending", "passed", "failed", "blocked"},
    "semantic_review": {"pending", "passed", "failed", "blocked"},
    "strong_review": {"not_required", "pending", "passed", "blocked"},
    "corroboration_check": {"pending", "complete", "blocked"},
}

STATEMENT_SOURCE_ROLES = {"primary", "secondary", "user_generated", "unknown"}
STATEMENT_EVIDENCE_STRENGTHS = {"strong", "moderate", "weak", "unknown"}
STATEMENT_CONFIDENCE_VALUES = {"high", "medium", "low"}
STATEMENT_TEMPORAL_STATUSES = {"current", "aging", "historical", "unknown"}
STATEMENT_CORROBORATION_VALUES = {
    "single_source",
    "independently_confirmed",
    "conflict",
    "not_applicable",
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

PROJECT_PROFILES = {"public", "restricted_internal"}
ACTION_POLICY_FIELDS = {
    "acquire",
    "process",
    "retain_uncertain",
    "retain_sensitive",
    "tracked_storage",
    "external_disclosure",
    "delete",
    "irreversible_transform",
    "secrets_in_tracked_storage",
}

OPERATIONAL_FINDING_KINDS = {
    "access-secret",
    "credentialed-url",
    "personal-data",
    "public-contact",
}

OPERATIONAL_BLOCKER_CODES = {
    "access-secret": "credential_exposure",
    "credentialed-url": "credential_exposure",
    "personal-data": "publication_not_permitted",
    "public-contact": "publication_not_permitted",
}

DERIVATION_TYPES = {
    "aggregation",
    "logical",
    "interpretive",
    "mixed",
}

ANALYSIS_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{1,11}$")
CONCEPT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")

PEER_EXTERNAL_CORPUS_USE_AS = {"peer"}

LEGACY_ROOT_DIRS = {
    "inventory",
    "normalized",
    "primary",
    "reports",
    "statements",
}

SOURCE_MAP_REQUIRED_PASSPORT_FIELDS = {
    "format",
    "file_size_bytes",
    "metadata_source",
    "extraction_tool",
    "extraction_status",
}

SOURCE_MAP_POSTPONED_STATUSES = {"postponed", "отложено"}

RESTRICTED_SOURCE_MAP_COPY_POLICIES = {
    "local_only",
    "metadata_only",
    "fragments_only",
}

FULL_TEXT_SOURCE_MAP_KEYS = {
    "body",
    "complete_text",
    "content",
    "extracted_text",
    "full_markdown",
    "full_text",
    "html_dump",
    "markdown_dump",
    "ocr_text",
    "raw_text",
    "summary",
    "text_dump",
}


def load_classifier_values(filename: str) -> set[str]:
    if yaml is None:
        return set()
    path = Path(__file__).resolve().parents[1] / "assets" / filename
    if not path.exists():
        return set()
    data = load_yaml(path)
    values = data.get("values") if isinstance(data, dict) else None
    if not isinstance(values, dict):
        return set()
    return {key for key in values if isinstance(key, str)}

STATEMENT_TEXT_FORBIDDEN_PREFIXES = (
    "в посте сказано",
    "в посте говорится",
    "в посте заявлено",
    "в посте указано",
    "в посте описано",
    "в посте сообщается",
    "автор считает",
    "автор сообщает",
    "автор утверждает",
    "автор пишет",
    "автор указывает",
    "автор заявляет",
    "по оценке автора",
    "по сообщению автора",
    "по словам автора",
    "по тексту поста",
    "по тексту источника",
    "по источнику",
)

REDACTION_PLACEHOLDER_PATTERNS = (
    "[обезличено]",
    "[номер горячей линии]",
    "[номер телефона]",
    "[номер email]",
    "[номер e-mail]",
    "[редактировано:",
)

SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|auth(?:orization)?|bearer|password|passwd|secret|cookie|session(?:[_-]?id)?)\b\s*[:=]\s*\S+"
)
CREDENTIALLED_URL_PATTERN = re.compile(
    r"(?i)\b(?:https?|ssh)://[^\s/@:]+:[^\s/@]+@|[?&](?:access_token|api[_-]?key|token|signature|x-amz-signature)=[^\s&#]+"
)
EMAIL_PATTERN = re.compile(r"(?i)\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b")
# Зарезервированный RFC домен примеров служит синтетическими данными для
# документации и тестов, а не персональными контактами. Проверка отделена от
# EMAIL_PATTERN, чтобы строка с тестовым и обычным адресами всё равно попала в отчёт.
EXAMPLE_EMAIL_DOMAIN = "example.com"
# Ролевые и технические ящики идентифицируют функцию, а не человека. Список
# намеренно ограничен устойчивыми именами, чтобы не скрывать адреса с именем.
ROLE_EMAIL_LOCAL_PARTS = frozenset(
    {
        "abuse",
        "admin",
        "contact",
        "doc.writer",
        "etl",
        "guide",
        "info",
        "legal",
        "noreply",
        "no-reply",
        "osi",
        "press",
        "privacy",
        "project",
        "security",
        "ssdf",
        "support",
        "team",
        "webmaster",
    }
)
# A bare sequence of digits is ambiguous: public HTML commonly contains app,
# author and document identifiers of the same length as a phone number.  Treat
# only a number with an international prefix or explicit formatting as a phone.
PHONE_PATTERN = re.compile(
    r"(?:\+\d[\d\s().-]{7,}\d|\b\d{1,4}(?:[\s().-]+\d{1,4}){2,}\b)"
)
METADATA_CONTACT_FILES = {"catalog.yml", "source.yml", "items.yml", "item.yml"}


@dataclass(frozen=True)
class OperationalFinding:
    """A redacted operational finding; it must never retain matched content."""

    kind: str
    path: str
    line: int
    classification: str
    reason: str
    blocker_code: str | None = None


def load_yaml(path: Path) -> Any:
    if yaml is None:
        raise RuntimeError("PyYAML is required: pip install PyYAML")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def walk_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for child in value.values():
            result.extend(walk_values(child))
        return result
    if isinstance(value, list):
        result: list[str] = []
        for child in value:
            result.extend(walk_values(child))
        return result
    return []


def nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def nonempty_date_or_text(value: Any) -> bool:
    return isinstance(value, date) or nonempty_string(value)


def is_bad_absolute_path(value: str) -> bool:
    return value.startswith("/") and "://" not in value


def contains_redaction_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(pattern in lowered for pattern in REDACTION_PLACEHOLDER_PATTERNS)


def normalize_text(value: str) -> str:
    return " ".join(value.split()).casefold()


ELLIPSIS_SPLIT_RE = re.compile(r"\s*(?:\.{3}|…)\s*")


def excerpt_fragments(excerpt: str) -> list[str]:
    """Split a multi-span excerpt joined by an ellipsis into its verbatim fragments.

    The corpus convention quotes several non-contiguous verbatim spans of a
    source joined by "…" (or "..."), rather than one contiguous quote.
    """
    return [fragment for fragment in ELLIPSIS_SPLIT_RE.split(excerpt) if fragment.strip()]


def excerpt_found_in_artifact(excerpt: str, artifact_text: str) -> bool:
    normalized_artifact = normalize_text(artifact_text)
    fragments = excerpt_fragments(excerpt)
    if not fragments:
        return True
    search_from = 0
    for fragment in fragments:
        position = normalized_artifact.find(normalize_text(fragment), search_from)
        if position == -1:
            return False
        search_from = position + len(normalize_text(fragment))
    return True


def has_any_stage(stages: set[str], allowed: set[str]) -> bool:
    return not stages.isdisjoint(allowed)


class Validator:
    def __init__(
        self,
        root: Path,
        *,
        strict_statements: bool = False,
        strict_concepts: bool = False,
        strict_verification: bool = False,
        operational: bool = False,
        operational_policy: Path | None = None,
    ) -> None:
        self.root = root.resolve()
        self.strict_statements = strict_statements
        self.strict_concepts = strict_concepts
        self.strict_verification = strict_verification
        self.operational = operational
        self.operational_policy = operational_policy
        self.contract_path = self.root / "corpus.yml"
        self.catalog_path = self.root / "catalog.yml"
        self.errors: list[str] = []
        self.contract_warnings: list[str] = []
        self.warnings: list[OperationalFinding] = []
        self.blockers: list[OperationalFinding] = []
        self.suppressed: list[OperationalFinding] = []
        self.allowed_stages = set(DEFAULT_ALLOWED_STAGES)
        self.allowed_carrier_types = load_classifier_values("source-carrier-types.yml")
        self.allowed_source_kinds = load_classifier_values("source-kinds.yml")
        self.allowed_statement_kinds = (
            load_classifier_values("statement-kinds.yml") or set(DEFAULT_STATEMENT_KINDS)
        )
        self.source_ids: set[str] = set()
        self.item_ids: set[str] = set()
        self.statement_ids: set[str] = set()
        self.analysis_ids: set[str] = set()
        self.derived_statement_count = 0

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def add_value_errors(self, path_label: str, value: Any) -> None:
        for item in walk_values(value):
            if is_bad_absolute_path(item):
                self.errors.append(f"{path_label}: absolute local path is not allowed: {item}")

    def validate(self, *, output: str = "text") -> int:
        try:
            self.validate_contract()
            self.validate_no_legacy_roots()
            source_dirs = self.source_dirs()
            for source_dir in source_dirs:
                self.validate_source(source_dir)
            self.validate_catalog(source_dirs)
            self.validate_global_items_index(source_dirs)
            self.validate_concepts()
            self.validate_derived_statements()
            if self.operational:
                self.validate_operational_safety()
        except RuntimeError as exc:
            self.errors.append(str(exc))

        exit_code = 1 if self.errors or self.blockers else 0
        if output == "json":
            print(
                json.dumps(
                    {
                        "contract_errors": self.errors,
                        "contract_warnings": self.contract_warnings,
                        "blockers": [finding.__dict__ for finding in self.blockers],
                        "quality_warnings": [finding.__dict__ for finding in self.warnings],
                        "suppressed": [finding.__dict__ for finding in self.suppressed],
                        "counts": {
                            "contract_errors": len(self.errors),
                            "contract_warnings": len(self.contract_warnings),
                            "blockers": len(self.blockers),
                            "quality_warnings": len(self.warnings),
                            "suppressed": len(self.suppressed),
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return exit_code

        if self.errors:
            print("Corpus validation failed:")
            for error in self.errors:
                print(f"- {error}")
        if self.blockers:
            print("Operational blockers:")
            for finding in self.blockers:
                print(f"- {finding.path}:{finding.line}: {finding.kind}")
        if self.contract_warnings:
            print("Contract warnings:")
            for warning in self.contract_warnings:
                print(f"- {warning}")
        if self.warnings:
            print("Quality warnings:")
            for finding in self.warnings:
                print(f"- {finding.path}:{finding.line}: {finding.kind}")
        if self.suppressed:
            print(f"Operational findings suppressed by policy or metadata: {len(self.suppressed)}.")
        if exit_code:
            return exit_code

        print(
            "Corpus validation passed: "
            f"{len(self.source_ids)} source(s), "
            f"{self.derived_statement_count} derived statement(s) checked."
        )
        return 0

    def validate_operational_safety(self) -> None:
        """Inspect publishable Git files and retain no matched values."""
        policy = self.load_operational_policy()
        for relative_path in self.publishable_corpus_paths():
            if self.is_local_path(relative_path):
                continue
            path = self.root / relative_path
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(lines, start=1):
                kind = self.operational_kind(relative_path, line)
                if kind is None:
                    continue
                classification, reason = self.classify_operational_finding(
                    policy, kind, relative_path
                )
                blocker_code = OPERATIONAL_BLOCKER_CODES.get(kind) if classification == "blocker" else None
                finding = OperationalFinding(
                    kind,
                    relative_path,
                    line_number,
                    classification,
                    reason,
                    blocker_code,
                )
                if classification == "blocker":
                    self.blockers.append(finding)
                elif classification == "warning":
                    self.warnings.append(finding)
                else:
                    self.suppressed.append(finding)

    def publishable_corpus_paths(self) -> list[str]:
        prefix_result = subprocess.run(
            ["git", "rev-parse", "--show-prefix"],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        result = subprocess.run(
            [
                "git",
                "ls-files",
                "-z",
                "--full-name",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            cwd=self.root,
            capture_output=True,
            text=False,
        )
        if prefix_result.returncode != 0 or result.returncode != 0:
            raise RuntimeError(
                "operational check requires a Git worktree and scans files publishable by Git"
            )
        corpus_prefix = prefix_result.stdout.strip()
        if corpus_prefix:
            corpus_prefix = f"{corpus_prefix.rstrip('/')}/"
        paths: list[str] = []
        for raw_path in result.stdout.split(b"\0"):
            if not raw_path:
                continue
            repository_path = raw_path.decode("utf-8", errors="strict")
            if corpus_prefix:
                if not repository_path.startswith(corpus_prefix):
                    continue
                relative_path = repository_path[len(corpus_prefix) :]
            else:
                relative_path = repository_path
            candidate = (self.root / relative_path).resolve()
            try:
                candidate.relative_to(self.root)
            except ValueError:
                self.errors.append(f"operational check: tracked path leaves corpus: {relative_path}")
                continue
            paths.append(relative_path)
        return sorted(paths)

    @staticmethod
    def is_local_path(relative_path: str) -> bool:
        parts = PurePosixPath(relative_path).parts
        return any(
            part == ".local"
            or part == "local"
            or part.endswith(".local")
            or ".local." in part
            or ".tmp." in part
            for part in parts
        )

    @staticmethod
    def operational_kind(relative_path: str, line: str) -> str | None:
        if SECRET_ASSIGNMENT_PATTERN.search(line):
            return "access-secret"
        if CREDENTIALLED_URL_PATTERN.search(line):
            return "credentialed-url"
        has_phone = any(
            sum(character.isdigit() for character in match.group()) >= 10
            for match in PHONE_PATTERN.finditer(line)
        )
        has_personal_email = any(
            not Validator.is_non_personal_email_address(match.group())
            for match in EMAIL_PATTERN.finditer(line)
        )
        if has_personal_email or has_phone:
            if PurePosixPath(relative_path).name in METADATA_CONTACT_FILES:
                return "public-contact"
            return "personal-data"
        return None

    @staticmethod
    def is_non_personal_email_address(address: str) -> bool:
        """Проверяет, относится ли адрес к тестовому домену или ролевому ящику."""
        local_part, domain = address.rsplit("@", maxsplit=1)
        normalized_domain = domain.casefold()
        return (
            normalized_domain == EXAMPLE_EMAIL_DOMAIN
            or normalized_domain.endswith(f".{EXAMPLE_EMAIL_DOMAIN}")
            or local_part.casefold() in ROLE_EMAIL_LOCAL_PARTS
        )

    def load_operational_policy(self) -> list[dict[str, str]]:
        if self.operational_policy is None:
            return []
        path = self.operational_policy
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise RuntimeError("operational policy must be inside the corpus") from exc
        data = load_yaml(path)
        rules = data.get("rules") if isinstance(data, dict) else None
        if not isinstance(rules, list):
            raise RuntimeError("operational policy must contain a rules list")
        parsed: list[dict[str, str]] = []
        for position, rule in enumerate(rules, start=1):
            if not isinstance(rule, dict):
                raise RuntimeError(f"operational policy rule #{position} must be a mapping")
            kind, path_pattern, action, reason = (
                rule.get("kind"),
                rule.get("path"),
                rule.get("action"),
                rule.get("reason"),
            )
            if (
                not nonempty_string(kind)
                or kind not in OPERATIONAL_FINDING_KINDS
                or not nonempty_string(path_pattern)
                or action not in {"suppress", "warning", "blocker"}
                or not nonempty_string(reason)
            ):
                raise RuntimeError(
                    f"operational policy rule #{position} requires kind, path, action and reason"
                )
            parsed.append(
                {"kind": kind, "path": path_pattern, "action": action, "reason": reason}
            )
        return parsed

    @staticmethod
    def classify_operational_finding(
        policy: list[dict[str, str]], kind: str, relative_path: str
    ) -> tuple[str, str]:
        for rule in policy:
            if rule["kind"] == kind and fnmatch.fnmatchcase(relative_path, rule["path"]):
                classification = "suppressed" if rule["action"] == "suppress" else rule["action"]
                return classification, rule["reason"]
        if kind in {"access-secret", "credentialed-url"}:
            return "blocker", "похоже на секрет доступа или закрытый локатор"
        if kind == "personal-data":
            return "warning", "похоже на содержательные персональные сведения; нужна проверка публикации"
        return "suppressed", "открытый контакт в метаданных источника не является блокером"

    def validate_contract(self) -> None:
        if not self.contract_path.exists():
            self.errors.append("missing corpus.yml")
            return

        contract = load_yaml(self.contract_path)
        if not isinstance(contract, dict):
            self.errors.append("corpus.yml must be a mapping")
            return

        for key in ("contract_version", "tracked_data", "local_data", "source_units"):
            if key not in contract:
                self.errors.append(f"corpus.yml missing required key: {key}")

        stages = contract.get("workflow_stages")
        if isinstance(stages, list) and all(isinstance(item, str) for item in stages):
            self.allowed_stages = set(stages)

        self.add_value_errors("corpus.yml", contract)
        self.validate_action_policy(contract)
        self.validate_contract_legacy_layers(contract)

    def validate_action_policy(self, contract: dict[str, Any]) -> None:
        profile = contract.get("project_profile")
        policy = contract.get("action_policy")
        if profile is None and policy is None:
            return
        if profile not in PROJECT_PROFILES:
            allowed = ", ".join(sorted(PROJECT_PROFILES))
            self.errors.append(f"corpus.yml: project_profile must be one of: {allowed}")
        if not isinstance(policy, dict):
            self.errors.append("corpus.yml: action_policy must be a mapping")
            return
        missing = sorted(ACTION_POLICY_FIELDS - policy.keys())
        if missing:
            self.errors.append(
                "corpus.yml: action_policy missing required keys: " + ", ".join(missing)
            )
        if policy.get("secrets_in_tracked_storage") != "prohibit":
            self.errors.append(
                "corpus.yml: action_policy.secrets_in_tracked_storage must be prohibit"
            )

    def validate_contract_legacy_layers(self, contract: dict[str, Any]) -> None:
        structural_sections = ("tracked_data", "local_data", "source_units", "indexes", "reports")
        for section in structural_sections:
            for label, value in self.contract_paths(contract.get(section), section):
                label_parts = label.replace("[", ".").split(".")
                if not any(part.startswith("legacy_") for part in label_parts) and not any(
                    part in LEGACY_ROOT_DIRS for part in value.parts
                ):
                    continue
                self.errors.append(
                    "corpus.yml: legacy layer remains active outside portable layout: "
                    f"{label}={value.as_posix()}"
                )

    def contract_paths(self, contract: Any, prefix: str = "") -> list[tuple[str, PurePosixPath]]:
        if isinstance(contract, str):
            return [(prefix, PurePosixPath(contract))]
        if isinstance(contract, dict):
            result: list[tuple[str, PurePosixPath]] = []
            for key, value in contract.items():
                if not isinstance(key, str):
                    continue
                child_prefix = key if not prefix else f"{prefix}.{key}"
                result.extend(self.contract_paths(value, child_prefix))
            return result
        if isinstance(contract, list):
            result: list[tuple[str, PurePosixPath]] = []
            for index, value in enumerate(contract, start=1):
                result.extend(self.contract_paths(value, f"{prefix}[{index}]"))
            return result
        return []

    def validate_no_legacy_roots(self) -> None:
        for name in sorted(LEGACY_ROOT_DIRS):
            path = self.root / name
            if path.exists():
                self.errors.append(
                    f"{name}/: legacy corpus layer remains outside data/; "
                    "migrate it into data/ (untracked material as a *.local.* file "
                    "beside its source or unit) before declaring portable layout complete"
                )

    def source_dirs(self) -> list[Path]:
        data_root = self.root / "data"
        if not data_root.exists():
            return []
        return sorted(path.parent for path in data_root.glob("*/source.yml"))

    def validate_catalog(self, source_dirs: list[Path]) -> None:
        if not self.catalog_path.exists():
            self.errors.append("missing catalog.yml")
            return

        catalog = load_yaml(self.catalog_path)
        if not isinstance(catalog, dict) or not isinstance(catalog.get("sources"), list):
            self.errors.append("catalog.yml must contain a sources list")
            return

        seen: set[str] = set()
        for index, source in enumerate(catalog["sources"], start=1):
            prefix = f"catalog.yml: source #{index}"
            if not isinstance(source, dict):
                self.errors.append(f"{prefix}: source must be a mapping")
                continue
            source_id = source.get("id")
            path = source.get("path")
            if isinstance(source_id, str):
                seen.add(source_id)
            else:
                self.errors.append(f"{prefix}: id must be a string")
            if not isinstance(path, str) or not (self.root / path / "source.yml").exists():
                self.errors.append(f"{prefix}: source path is invalid: {path}")

        expected_ids = set()
        for source_dir in source_dirs:
            source = load_yaml(source_dir / "source.yml")
            source_id = source.get("id") if isinstance(source, dict) else None
            if isinstance(source_id, str):
                expected_ids.add(source_id)

        missing = expected_ids - seen
        extra = seen - expected_ids
        if missing:
            self.errors.append(f"catalog.yml missing sources: {', '.join(sorted(missing))}")
        if extra:
            self.errors.append(f"catalog.yml references unknown sources: {', '.join(sorted(extra))}")

    def validate_global_items_index(self, source_dirs: list[Path]) -> None:
        path = self.root / "index" / "items.yml"
        if not path.exists():
            return

        rel = self.rel(path)
        data = load_yaml(path)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            self.errors.append(f"{rel}: items must be a list")
            return

        global_by_id: dict[str, Any] = {}
        for index, item in enumerate(items, start=1):
            prefix = f"{rel}: item #{index}"
            if not isinstance(item, dict):
                self.errors.append(f"{prefix}: item must be a mapping")
                continue
            item_id = item.get("id")
            if not isinstance(item_id, str):
                self.errors.append(f"{prefix}: id must be a string")
                continue
            if item_id in global_by_id:
                self.errors.append(f"{prefix}: duplicate item id: {item_id}")
            global_by_id[item_id] = item

        expected_ids: set[str] = set()
        for source_dir in source_dirs:
            source = load_yaml(source_dir / "source.yml")
            source_id = source.get("id") if isinstance(source, dict) else None
            source_items_path = source_dir / "items.yml"
            if not source_items_path.exists():
                continue
            source_items = load_yaml(source_items_path)
            source_list = source_items.get("items") if isinstance(source_items, dict) else None
            if not isinstance(source_id, str) or not isinstance(source_list, list):
                continue
            for source_item in source_list:
                if not isinstance(source_item, dict):
                    continue
                item_id = source_item.get("id")
                if not isinstance(item_id, str):
                    continue
                expected_ids.add(item_id)
                global_item = global_by_id.get(item_id)
                if global_item is None:
                    self.errors.append(f"{rel}: missing source item: {item_id}")
                    continue

                item_path = source_item.get("path")
                expected_path = None
                if isinstance(item_path, str):
                    expected_path = (source_dir / item_path).relative_to(self.root).as_posix()
                expected_fields = {
                    "source_id": source_id,
                    "path": expected_path,
                    "title": source_item.get("title"),
                    "workflow_stage": source_item.get("workflow_stage"),
                    "access": source_item.get("access"),
                }
                for field, expected in expected_fields.items():
                    if global_item.get(field) != expected:
                        self.errors.append(f"{rel}: {item_id} field {field} is out of sync")

        extra_ids = sorted(set(global_by_id) - expected_ids)
        if extra_ids:
            self.errors.append(f"{rel}: references unknown items: {', '.join(extra_ids)}")

    def validate_concepts(self) -> None:
        path = self.root / "concepts.yml"
        if not path.exists():
            if self.strict_concepts:
                self.errors.append("missing concepts.yml required by strict concept validation")
            return

        rel = self.rel(path)
        data = load_yaml(path)
        if not isinstance(data, dict):
            self.errors.append(f"{rel}: must be a mapping")
            return
        if data.get("concept_contract_version") != 1:
            self.errors.append(f"{rel}: concept_contract_version must be 1")
        concepts = data.get("concepts")
        if not isinstance(concepts, list) or not concepts:
            self.errors.append(f"{rel}: concepts must be a non-empty list")
            return

        concept_ids: set[str] = set()
        relationships: list[tuple[str, str]] = []
        for index, concept in enumerate(concepts, start=1):
            prefix = f"{rel}: concept #{index}"
            if not isinstance(concept, dict):
                self.errors.append(f"{prefix}: must be a mapping")
                continue
            missing = sorted(CONCEPT_RECORD_REQUIRED - concept.keys())
            if missing:
                self.errors.append(f"{prefix}: missing concept fields: {', '.join(missing)}")

            concept_id = concept.get("id")
            if not isinstance(concept_id, str) or not CONCEPT_ID_PATTERN.fullmatch(concept_id):
                self.errors.append(f"{prefix}: id must be lowercase kebab-case")
            elif concept_id in concept_ids:
                self.errors.append(f"{prefix}: duplicate concept id: {concept_id}")
            else:
                concept_ids.add(concept_id)

            primary = concept.get("primary")
            primary_forms = [primary] if isinstance(primary, str) else primary
            if (
                not isinstance(primary_forms, list)
                or not primary_forms
                or not all(nonempty_string(item) for item in primary_forms)
            ):
                self.errors.append(f"{prefix}: primary must be non-empty text or a list of non-empty texts")
            if not nonempty_string(concept.get("definition")):
                self.errors.append(f"{prefix}: definition must be non-empty text")

            boundaries = concept.get("boundaries")
            if not isinstance(boundaries, dict):
                self.errors.append(f"{prefix}: boundaries must be a mapping")
            else:
                for field in ("includes", "excludes"):
                    values = boundaries.get(field)
                    if (
                        not isinstance(values, list)
                        or not values
                        or not all(nonempty_string(item) for item in values)
                    ):
                        self.errors.append(
                            f"{prefix}: boundaries.{field} must be a non-empty list of texts"
                        )

            authority = concept.get("authority")
            if not isinstance(authority, dict) or not all(
                nonempty_string(authority.get(field)) for field in ("type", "ref")
            ):
                self.errors.append(f"{prefix}: authority must contain non-empty type and ref")

            defined_by = concept.get("defined_by")
            if (
                not isinstance(defined_by, list)
                or not defined_by
                or not all(nonempty_string(item) for item in defined_by)
            ):
                self.errors.append(f"{prefix}: defined_by must be a non-empty list of statement ids")
            elif invalid_ids := sorted(set(defined_by) - self.statement_ids):
                self.errors.append(
                    f"{prefix}: defined_by references unknown statements: {', '.join(invalid_ids)}"
                )

            relation_entries = concept.get("relationships", [])
            if not isinstance(relation_entries, list):
                self.errors.append(f"{prefix}: relationships must be a list")
            else:
                for relation_index, relation in enumerate(relation_entries, start=1):
                    relation_prefix = f"{prefix}: relationship #{relation_index}"
                    if not isinstance(relation, dict) or not all(
                        nonempty_string(relation.get(field)) for field in ("type", "target")
                    ):
                        self.errors.append(
                            f"{relation_prefix}: must contain non-empty type and target"
                        )
                    else:
                        relationships.append((relation_prefix, relation["target"]))

        for prefix, target in relationships:
            if target not in concept_ids:
                self.errors.append(f"{prefix}: target references unknown concept: {target}")

    def validate_source(self, source_dir: Path) -> None:
        path = source_dir / "source.yml"
        rel = self.rel(path)
        source = load_yaml(path)
        if not isinstance(source, dict):
            self.errors.append(f"{rel}: source card must be a mapping")
            return

        missing = sorted(SOURCE_REQUIRED - source.keys())
        if missing:
            self.errors.append(f"{rel}: missing source fields: {', '.join(missing)}")

        source_id = source.get("id")
        if isinstance(source_id, str):
            if source_id in self.source_ids:
                self.errors.append(f"{rel}: duplicate source id: {source_id}")
            self.source_ids.add(source_id)
        else:
            self.errors.append(f"{rel}: id must be a string")

        access = source.get("access")
        if not isinstance(access, dict):
            self.errors.append(f"{rel}: access must be a mapping")
        elif not nonempty_string(access.get("default")):
            self.errors.append(f"{rel}: access.default must be non-empty text")

        carrier_type = source.get("carrier_type")
        if self.allowed_carrier_types and carrier_type not in self.allowed_carrier_types:
            self.errors.append(f"{rel}: unknown carrier_type: {carrier_type}")

        source_kind = source.get("source_kind")
        if self.allowed_source_kinds and source_kind not in self.allowed_source_kinds:
            self.errors.append(f"{rel}: unknown source_kind: {source_kind}")

        long_source = source.get("long_source")
        if long_source is not None and not isinstance(long_source, bool):
            self.errors.append(f"{rel}: long_source must be boolean when present")

        self.validate_access_requirements(source.get("access_requirements"), rel)
        self.validate_use_policy(source.get("use_policy"), rel)
        self.validate_external_corpus_source(source, rel)
        self.add_value_errors(rel, source)
        self.validate_items(source_dir, source_id, source)
        self.validate_unit_dirs(source_dir, source_id)
        self.validate_long_source(source_dir, source_id, source)

    def validate_access_requirements(self, value: Any, prefix: str) -> None:
        if value is None:
            return
        if not isinstance(value, dict):
            self.errors.append(f"{prefix}: access_requirements must be a mapping")
            return
        for field in ("authorization_kind", "profile_name"):
            if field in value and not nonempty_string(value.get(field)):
                self.errors.append(f"{prefix}: access_requirements.{field} must be non-empty text")
        capabilities = value.get("required_capabilities")
        if capabilities is not None and (
            not isinstance(capabilities, list)
            or not all(nonempty_string(item) for item in capabilities)
        ):
            self.errors.append(
                f"{prefix}: access_requirements.required_capabilities must be a list of texts"
            )
        interactive = value.get("interactive_setup")
        if interactive is not None and interactive not in {"allowed", "prohibited"}:
            self.errors.append(
                f"{prefix}: access_requirements.interactive_setup must be allowed or prohibited"
            )

    def validate_use_policy(self, value: Any, prefix: str) -> None:
        if value is None:
            return
        if not isinstance(value, dict):
            self.errors.append(f"{prefix}: use_policy must be a mapping")
            return
        for field in ("action", "basis", "scope"):
            if not nonempty_string(value.get(field)):
                self.errors.append(f"{prefix}: use_policy.{field} must be non-empty text")
        if not nonempty_date_or_text(value.get("checked_at")):
            self.errors.append(f"{prefix}: use_policy.checked_at must be a date or non-empty text")
        status = value.get("status")
        if status not in USE_POLICY_STATUSES:
            allowed = ", ".join(sorted(USE_POLICY_STATUSES))
            self.errors.append(f"{prefix}: use_policy.status must be one of: {allowed}")
        restrictions = value.get("restrictions")
        if not isinstance(restrictions, list) or not all(
            nonempty_string(item) for item in restrictions
        ):
            self.errors.append(f"{prefix}: use_policy.restrictions must be a list of texts")

    def validate_external_corpus_source(self, source: dict[str, Any], rel: str) -> None:
        source_kind = source.get("source_kind")
        external_corpus = source.get("external_corpus")

        if external_corpus is None:
            if source_kind == "knowledge_corpus":
                self.errors.append(f"{rel}: knowledge_corpus source requires external_corpus block")
            return

        if source_kind != "knowledge_corpus":
            self.errors.append(
                f"{rel}: external_corpus block is allowed only for source_kind knowledge_corpus"
            )
            return

        if not isinstance(external_corpus, dict):
            self.errors.append(f"{rel}: external_corpus must be a mapping")
            return

        if not nonempty_string(source.get("locator")):
            self.errors.append(f"{rel}: knowledge_corpus source requires non-empty locator")

        contract = external_corpus.get("contract")
        if not nonempty_string(contract):
            self.errors.append(f"{rel}: external_corpus.contract must be non-empty text")

        use_as = external_corpus.get("use_as")
        if use_as not in PEER_EXTERNAL_CORPUS_USE_AS:
            allowed = ", ".join(sorted(PEER_EXTERNAL_CORPUS_USE_AS))
            self.errors.append(f"{rel}: external_corpus.use_as must be one of: {allowed}")

    def validate_items(self, source_dir: Path, source_id: Any, source: dict[str, Any]) -> None:
        path = source_dir / "items.yml"
        rel = self.rel(path)
        if not path.exists():
            if self.is_peer_external_corpus(source):
                return
            self.errors.append(f"{rel}: missing items.yml")
            return

        data = load_yaml(path)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            self.errors.append(f"{rel}: items must be a list")
            return

        for index, item in enumerate(items, start=1):
            self.validate_item(source_dir, item, source_id, f"{rel}: item #{index}")

    def is_peer_external_corpus(self, source: dict[str, Any]) -> bool:
        if source.get("source_kind") != "knowledge_corpus":
            return False
        external_corpus = source.get("external_corpus")
        return isinstance(external_corpus, dict) and external_corpus.get("use_as") in (
            PEER_EXTERNAL_CORPUS_USE_AS
        )

    def validate_item(self, source_dir: Path, item: Any, source_id: Any, prefix: str) -> None:
        if not isinstance(item, dict):
            self.errors.append(f"{prefix}: item must be a mapping")
            return

        missing = sorted(ITEM_REQUIRED - item.keys())
        if missing:
            self.errors.append(f"{prefix}: missing fields: {', '.join(missing)}")

        item_id = item.get("id")
        if isinstance(item_id, str):
            if item_id in self.item_ids:
                self.errors.append(f"{prefix}: duplicate item id: {item_id}")
            self.item_ids.add(item_id)
        else:
            self.errors.append(f"{prefix}: id must be a string")

        if source_id and item.get("source_id") not in (None, source_id):
            self.errors.append(f"{prefix}: source_id does not match {source_id}")
        if not nonempty_string(item.get("access")):
            self.errors.append(f"{prefix}: access must be non-empty text")

        stage = item.get("workflow_stage")
        if stage and stage not in self.allowed_stages:
            self.errors.append(f"{prefix}: unknown workflow_stage: {stage}")
        self.validate_blocker_code(item, prefix, stage == "blocked")
        self.validate_item_contract(item, prefix)
        self.validate_access_requirements(item.get("access_requirements"), prefix)

        long_source = item.get("long_source")
        if long_source is not None and not isinstance(long_source, bool):
            self.errors.append(f"{prefix}: long_source must be boolean when present")

        item_path = item.get("path")
        if isinstance(item_path, str):
            resolved = source_dir / item_path
            if not resolved.exists():
                self.errors.append(f"{prefix}: item path does not exist: {item_path}")
            elif resolved.is_file():
                self.errors.append(
                    f"{prefix}: item path must point to the unit folder, not to item.yml "
                    f"itself: {item_path}"
                )
            elif not (resolved / "item.yml").is_file():
                self.errors.append(f"{prefix}: item path folder has no item.yml: {item_path}")

        self.add_value_errors(prefix, item)

    def validate_unit_dirs(self, source_dir: Path, source_id: Any) -> None:
        for units_dir in sorted(path for path in source_dir.iterdir() if path.is_dir()):
            for unit_dir in sorted(path for path in units_dir.iterdir() if path.is_dir()):
                item_path = unit_dir / "item.yml"
                if item_path.exists():
                    item = load_yaml(item_path)
                    self.validate_unit_item(item, source_id, item_path)
                elif (unit_dir / "verification.yml").exists():
                    self.errors.append(
                        f"{self.rel(unit_dir / 'verification.yml')}: verification requires item.yml"
                    )
                self.validate_unit_artifacts(unit_dir)
                statements_path = unit_dir / "statements.yml"
                if statements_path.exists():
                    item_id = item.get("id") if item_path.exists() and isinstance(item, dict) else None
                    self.validate_statements(statements_path, source_id, item_id)

    def validate_blocker_code(
        self,
        value: dict[str, Any],
        prefix: str,
        required: bool,
    ) -> None:
        blocker_code = value.get("blocker_code")
        if required and blocker_code not in BLOCKER_CODES:
            allowed = ", ".join(sorted(BLOCKER_CODES))
            self.errors.append(f"{prefix}: blocker_code must be one of: {allowed}")
        elif blocker_code is not None and blocker_code not in BLOCKER_CODES:
            allowed = ", ".join(sorted(BLOCKER_CODES))
            self.errors.append(f"{prefix}: blocker_code must be one of: {allowed}")

    def validate_unit_item(self, item: Any, source_id: Any, path: Path) -> None:
        rel = self.rel(path)
        if not isinstance(item, dict):
            self.errors.append(f"{rel}: item must be a mapping")
            return

        missing = sorted(ITEM_REQUIRED - item.keys())
        if missing:
            self.errors.append(f"{rel}: missing fields: {', '.join(missing)}")

        item_id = item.get("id")
        if isinstance(item_id, str) and item_id not in self.item_ids:
            self.errors.append(f"{rel}: item is not present in source items.yml: {item_id}")

        if source_id and item.get("source_id") not in (None, source_id):
            self.errors.append(f"{rel}: source_id does not match {source_id}")
        if not nonempty_string(item.get("access")):
            self.errors.append(f"{rel}: access must be non-empty text")

        stage = item.get("workflow_stage")
        if stage and stage not in self.allowed_stages:
            self.errors.append(f"{rel}: unknown workflow_stage: {stage}")
        self.validate_blocker_code(item, rel, stage == "blocked")
        self.validate_item_contract(item, rel)
        self.validate_access_requirements(item.get("access_requirements"), rel)

        long_source = item.get("long_source")
        if long_source is not None and not isinstance(long_source, bool):
            self.errors.append(f"{rel}: long_source must be boolean when present")

        self.add_value_errors(rel, item)

        files = item.get("files", {})
        if files and not isinstance(files, dict):
            self.errors.append(f"{rel}: files must be a mapping")
            return

        for artifact in files.get("tracked", []):
            if not (path.parent / artifact).exists():
                self.errors.append(f"{rel}: tracked file does not exist: {artifact}")
        for artifact in files.get("local", []):
            if ".local." not in artifact and ".tmp." not in artifact:
                self.errors.append(
                    f"{rel}: local file must use *.local.* or *.tmp.* name: {artifact}"
                )

        verification_path = path.parent / "verification.yml"
        if verification_path.exists():
            self.validate_verification(verification_path)
        elif item.get("item_contract_version", 1) == 2:
            message = f"{rel}: item contract version 2 has no verification.yml"
            if stage == "verification_assessed":
                self.errors.append(message)
            elif self.strict_verification:
                self.contract_warnings.append(message)

    def validate_item_contract(self, item: dict[str, Any], prefix: str) -> None:
        version = item.get("item_contract_version", 1)
        if version not in ITEM_CONTRACT_VERSIONS:
            self.errors.append(f"{prefix}: unsupported item_contract_version: {version}")
            return
        stage = item.get("workflow_stage")
        if version == 1 and stage == "verification_assessed":
            self.errors.append(
                f"{prefix}: verification_assessed requires item_contract_version: 2"
            )
        if version == 2 and stage == "source_checked":
            self.errors.append(
                f"{prefix}: item contract version 2 uses verification_assessed instead of source_checked"
            )

    def validate_verification(self, path: Path) -> None:
        rel = self.rel(path)
        data = load_yaml(path)
        if not isinstance(data, dict):
            self.errors.append(f"{rel}: verification must be a mapping")
            return
        if data.get("verification_contract_version") != 1:
            self.errors.append(
                f"{rel}: unsupported verification_contract_version: "
                f"{data.get('verification_contract_version')}"
            )
            return

        self.add_value_errors(rel, data)
        self.validate_verification_forbidden_fields(data, rel)
        artifact = data.get("artifact")
        artifact_path: Path | None = None
        if not nonempty_string(artifact):
            self.errors.append(f"{rel}: artifact must be non-empty text")
        elif is_bad_absolute_path(artifact) or ".." in PurePosixPath(artifact).parts:
            self.errors.append(f"{rel}: artifact must be relative to the unit")
        else:
            artifact_path = path.parent / artifact
            if not artifact_path.is_file():
                self.errors.append(f"{rel}: verified artifact does not exist: {artifact}")
                artifact_path = None

        hash_data = data.get("hash")
        if not isinstance(hash_data, dict):
            self.errors.append(f"{rel}: hash must be a mapping")
        else:
            if hash_data.get("algorithm") != "sha256":
                self.errors.append(f"{rel}: hash.algorithm must be sha256")
            hash_value = hash_data.get("value")
            if not isinstance(hash_value, str) or not re.fullmatch(r"[0-9a-f]{64}", hash_value):
                self.errors.append(f"{rel}: hash.value must be a lowercase sha256 digest")
            elif artifact_path is not None:
                actual = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
                if actual != hash_value:
                    self.errors.append(f"{rel}: artifact hash does not match verification.yml")

        acquisition = data.get("acquisition")
        acquisition_method: Any = None
        if not isinstance(acquisition, dict):
            self.errors.append(f"{rel}: acquisition must be a mapping")
        else:
            acquisition_method = acquisition.get("method")
            if acquisition_method not in VERIFICATION_ACQUISITION_METHODS:
                allowed = ", ".join(sorted(VERIFICATION_ACQUISITION_METHODS))
                self.errors.append(f"{rel}: acquisition.method must be one of: {allowed}")
            if not nonempty_date_or_text(acquisition.get("recorded_at")):
                self.errors.append(f"{rel}: acquisition.recorded_at must be non-empty text")

        verification = data.get("verification")
        if not isinstance(verification, dict):
            self.errors.append(f"{rel}: verification must be a mapping")
            return
        method = verification.get("method")
        if method not in VERIFICATION_METHODS:
            allowed = ", ".join(sorted(VERIFICATION_METHODS))
            self.errors.append(f"{rel}: verification.method must be one of: {allowed}")
        if not nonempty_date_or_text(verification.get("checked_at")):
            self.errors.append(f"{rel}: verification.checked_at must be non-empty text")
        checked_by = verification.get("checked_by")
        if not isinstance(checked_by, dict) or not nonempty_string(checked_by.get("role")):
            self.errors.append(f"{rel}: verification.checked_by.role must be non-empty text")

        scope = verification.get("scope")
        content_scope: Any = None
        metadata_scope: set[str] = set()
        if not isinstance(scope, dict):
            self.errors.append(f"{rel}: verification.scope must be a mapping")
        else:
            content_scope = scope.get("content")
            if content_scope not in VERIFICATION_CONTENT_SCOPES:
                allowed = ", ".join(sorted(VERIFICATION_CONTENT_SCOPES))
                self.errors.append(f"{rel}: verification.scope.content must be one of: {allowed}")
            if content_scope == "fragment" and not nonempty_string(scope.get("fragment")):
                self.errors.append(f"{rel}: fragment content scope requires scope.fragment")
            metadata = scope.get("metadata")
            if not isinstance(metadata, list) or not all(
                item in VERIFICATION_METADATA_FIELDS for item in metadata
            ):
                allowed = ", ".join(sorted(VERIFICATION_METADATA_FIELDS))
                self.errors.append(f"{rel}: verification.scope.metadata may contain: {allowed}")
            else:
                metadata_scope = set(metadata)

        result = verification.get("result")
        if not isinstance(result, dict):
            self.errors.append(f"{rel}: verification.result must be a mapping")
            return
        overall = result.get("overall")
        content_match = result.get("content_match")
        completeness = result.get("scope_completeness")
        if overall not in VERIFICATION_OVERALL_RESULTS:
            self.errors.append(f"{rel}: verification.result.overall has an unsupported value")
        if content_match not in VERIFICATION_MATCH_RESULTS:
            self.errors.append(f"{rel}: verification.result.content_match has an unsupported value")
        if completeness not in VERIFICATION_COMPLETENESS_RESULTS:
            self.errors.append(
                f"{rel}: verification.result.scope_completeness has an unsupported value"
            )
        metadata_results = result.get("metadata")
        if not isinstance(metadata_results, dict) or set(metadata_results) != VERIFICATION_METADATA_FIELDS:
            self.errors.append(
                f"{rel}: verification.result.metadata must contain locator, author and publication_date"
            )
        else:
            for field, value in metadata_results.items():
                if value not in VERIFICATION_METADATA_RESULTS:
                    self.errors.append(f"{rel}: unsupported metadata result for {field}: {value}")
                if value != "unverified" and field not in metadata_scope:
                    self.errors.append(
                        f"{rel}: metadata result for {field} exceeds the declared verification scope"
                    )

        if method in {"local_integrity_only", "no_source_comparison"} and (
            overall != "unverified" or content_match != "unverified"
        ):
            self.errors.append(
                f"{rel}: {method} cannot confirm correspondence with the external source"
            )
        if acquisition_method == "user_provided" and method == "no_source_comparison" and (
            overall != "unverified" or content_match != "unverified"
        ):
            self.errors.append(
                f"{rel}: user-provided material is not verified without source comparison"
            )
        if content_scope == "none" and content_match != "unverified":
            self.errors.append(f"{rel}: content result exceeds the declared none scope")

        limitations = verification.get("limitations")
        if not isinstance(limitations, list) or not all(
            nonempty_string(item) for item in limitations
        ):
            self.errors.append(f"{rel}: verification.limitations must be a list of texts")

    def validate_verification_forbidden_fields(self, value: Any, prefix: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = key.lower().replace("-", "_") if isinstance(key, str) else ""
                if normalized in VERIFICATION_FORBIDDEN_FIELDS:
                    self.errors.append(f"{prefix}: forbidden verification field: {key}")
                self.validate_verification_forbidden_fields(child, prefix)
        elif isinstance(value, list):
            for child in value:
                self.validate_verification_forbidden_fields(child, prefix)

    def validate_unit_artifacts(self, unit_dir: Path) -> None:
        for artifact_path in sorted(unit_dir.iterdir()):
            if not artifact_path.is_file() or artifact_path.suffix not in {".md", ".txt"}:
                continue
            text = artifact_path.read_text(encoding="utf-8")
            if contains_redaction_placeholder(text):
                rel = self.rel(artifact_path)
                self.errors.append(
                    f"{rel}: inline redaction placeholder found; describe restrictions in metadata"
                )

    def validate_long_source(
        self, source_dir: Path, source_id: Any, source: dict[str, Any]
    ) -> None:
        source_map_path = source_dir / "source-map.yml"
        long_source = source.get("long_source") is True
        source_stages = self.collect_source_stages(source_dir)
        long_unit_stages = self.collect_long_source_unit_stages(source_dir)
        extraction_status = source.get("extraction_status")
        reached_normalization = (
            long_source
            and (
                has_any_stage(source_stages, NORMALIZED_OR_LATER_STAGES)
                or extraction_status == "normalized_fragments_ready"
            )
        ) or has_any_stage(long_unit_stages, NORMALIZED_OR_LATER_STAGES)

        source_map_stages = source_stages | long_unit_stages

        if long_unit_stages and not isinstance(source.get("long_source"), bool):
            self.errors.append(
                f"{self.rel(source_dir / 'source.yml')}: source has long source units; "
                "set long_source explicitly to true or false"
            )

        if reached_normalization and not source_map_path.exists():
            self.errors.append(
                f"{self.rel(source_dir / 'source.yml')}: long source reached normalization "
                "or statements without source-map.yml"
            )
            return

        if source_map_path.exists():
            self.validate_source_map(source_map_path, source_dir, source_id, source, source_map_stages)

    def collect_source_stages(self, source_dir: Path) -> set[str]:
        stages: set[str] = set()
        items_path = source_dir / "items.yml"
        if items_path.exists():
            items_data = load_yaml(items_path)
            items = items_data.get("items") if isinstance(items_data, dict) else None
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and isinstance(item.get("workflow_stage"), str):
                        stages.add(item["workflow_stage"])

        for units_dir in sorted(path for path in source_dir.iterdir() if path.is_dir()):
            for unit_dir in sorted(path for path in units_dir.iterdir() if path.is_dir()):
                item_path = unit_dir / "item.yml"
                if item_path.exists():
                    item = load_yaml(item_path)
                    if isinstance(item, dict) and isinstance(item.get("workflow_stage"), str):
                        stages.add(item["workflow_stage"])
                if (unit_dir / "statements.yml").exists():
                    stages.add("statements_extracted")

        return stages

    def collect_long_source_unit_stages(self, source_dir: Path) -> set[str]:
        stages: set[str] = set()
        items_path = source_dir / "items.yml"
        if items_path.exists():
            items_data = load_yaml(items_path)
            items = items_data.get("items") if isinstance(items_data, dict) else None
            if isinstance(items, list):
                for item in items:
                    if (
                        isinstance(item, dict)
                        and item.get("long_source") is True
                        and isinstance(item.get("workflow_stage"), str)
                    ):
                        stages.add(item["workflow_stage"])

        for units_dir in sorted(path for path in source_dir.iterdir() if path.is_dir()):
            for unit_dir in sorted(path for path in units_dir.iterdir() if path.is_dir()):
                item_path = unit_dir / "item.yml"
                if not item_path.exists():
                    continue
                item = load_yaml(item_path)
                if (
                    isinstance(item, dict)
                    and item.get("long_source") is True
                    and isinstance(item.get("workflow_stage"), str)
                ):
                    stages.add(item["workflow_stage"])

        return stages

    def validate_source_map(
        self,
        path: Path,
        source_dir: Path,
        source_id: Any,
        source: dict[str, Any],
        source_stages: set[str],
    ) -> None:
        rel = self.rel(path)
        data = load_yaml(path)
        if not isinstance(data, dict):
            self.errors.append(f"{rel}: source-map.yml must be a mapping")
            return

        self.add_value_errors(rel, data)

        map_source_id = data.get("source_id")
        if source_id and map_source_id not in (None, source_id):
            self.errors.append(f"{rel}: source_id does not match {source_id}")

        map_long_source = data.get("long_source")
        if map_long_source is not None and map_long_source is not True:
            self.errors.append(f"{rel}: long_source must be true when present")

        passport = data.get("extraction_passport")
        if not isinstance(passport, dict):
            self.errors.append(f"{rel}: extraction_passport must be a mapping")
        else:
            self.validate_source_map_passport(rel, passport)

        structure = data.get("structure")
        structure_ids: set[str] = set()
        if not isinstance(structure, dict):
            self.errors.append(f"{rel}: structure must be a mapping")
        else:
            units = structure.get("units")
            if not isinstance(units, list) or not units:
                self.errors.append(f"{rel}: structure.units must be a non-empty list")
            else:
                structure_ids = self.validate_source_map_structure(rel, units)

        coverage_required = has_any_stage(source_stages, STATEMENTS_OR_LATER_STAGES)
        coverage = data.get("coverage")
        coverage_absence_reason = data.get("coverage_absence_reason")
        if coverage is None:
            if coverage_required or not nonempty_string(coverage_absence_reason):
                self.errors.append(
                    f"{rel}: coverage.units is required or coverage_absence_reason must explain "
                    "its absence on early normalization stage"
                )
        elif not isinstance(coverage, dict):
            self.errors.append(f"{rel}: coverage must be a mapping")
        else:
            self.validate_source_map_coverage(rel, source_dir, coverage, structure_ids)

        if (
            source.get("copy_policy") in RESTRICTED_SOURCE_MAP_COPY_POLICIES
            and self.source_map_contains_full_text_dump(data)
        ):
            policies = ", ".join(sorted(RESTRICTED_SOURCE_MAP_COPY_POLICIES))
            self.errors.append(
                f"{rel}: source-map.yml contains full-text-like fields for restricted copy_policy "
                f"({policies})"
            )

        local_inputs = data.get("local_inputs")
        if local_inputs is not None:
            if not isinstance(local_inputs, list):
                self.errors.append(f"{rel}: local_inputs must be a list")
            else:
                for index, entry in enumerate(local_inputs, start=1):
                    if not isinstance(entry, str) or not entry.strip():
                        self.errors.append(f"{rel}: local_inputs #{index} must be non-empty text")
                        continue
                    if is_bad_absolute_path(entry):
                        self.errors.append(f"{rel}: local_inputs #{index} must be relative: {entry}")
                    if ".local." not in entry and ".tmp." not in entry:
                        self.errors.append(
                            f"{rel}: local_inputs #{index} must use *.local.* or *.tmp.* marker: "
                            f"{entry}"
                        )

    def validate_source_map_passport(self, rel: str, passport: dict[str, Any]) -> None:
        missing = sorted(SOURCE_MAP_REQUIRED_PASSPORT_FIELDS - passport.keys())
        if missing:
            self.errors.append(
                f"{rel}: extraction_passport missing fields: {', '.join(missing)}"
            )

        hash_value = passport.get("content_hash")
        hash_absence_reason = passport.get("content_hash_absence_reason")
        if not nonempty_string(hash_value) and not nonempty_string(hash_absence_reason):
            self.errors.append(
                f"{rel}: extraction_passport requires content_hash or "
                "content_hash_absence_reason"
            )

        file_size_bytes = passport.get("file_size_bytes")
        if not isinstance(file_size_bytes, int) or file_size_bytes <= 0:
            self.errors.append(f"{rel}: extraction_passport.file_size_bytes must be positive integer")

        for field in (
            "format",
            "metadata_source",
            "extraction_tool",
            "extraction_status",
        ):
            if field in passport and not nonempty_string(passport.get(field)):
                self.errors.append(f"{rel}: extraction_passport.{field} must be non-empty text")

    def validate_source_map_structure(self, rel: str, units: list[Any]) -> set[str]:
        unit_ids: set[str] = set()
        for index, unit in enumerate(units, start=1):
            prefix = f"{rel}: structure.units #{index}"
            if not isinstance(unit, dict):
                self.errors.append(f"{prefix}: unit must be a mapping")
                continue
            unit_id = unit.get("id")
            if not nonempty_string(unit_id):
                self.errors.append(f"{prefix}: id must be non-empty text")
                continue
            if unit_id in unit_ids:
                self.errors.append(f"{prefix}: duplicate structure unit id: {unit_id}")
            unit_ids.add(unit_id)
            if not (nonempty_string(unit.get("title")) or nonempty_string(unit.get("heading"))):
                self.errors.append(f"{prefix}: title or heading is required")
            has_order = isinstance(unit.get("order"), int)
            locator = unit.get("locator")
            has_locator = isinstance(locator, (str, dict)) and (
                nonempty_string(locator) if isinstance(locator, str) else bool(locator)
            )
            if not has_order and not has_locator:
                self.errors.append(f"{prefix}: order or locator is required")
        return unit_ids

    def validate_source_map_coverage(
        self,
        rel: str,
        source_dir: Path,
        coverage: dict[str, Any],
        structure_ids: set[str],
    ) -> None:
        units = coverage.get("units")
        if not isinstance(units, list):
            self.errors.append(f"{rel}: coverage.units must be a list")
            return

        seen_unit_ids: set[str] = set()
        for index, unit in enumerate(units, start=1):
            prefix = f"{rel}: coverage.units #{index}"
            if not isinstance(unit, dict):
                self.errors.append(f"{prefix}: unit must be a mapping")
                continue
            unit_id = unit.get("unit_id")
            if not nonempty_string(unit_id):
                self.errors.append(f"{prefix}: unit_id must be non-empty text")
                continue
            seen_unit_ids.add(unit_id)
            if structure_ids and unit_id not in structure_ids:
                self.errors.append(f"{prefix}: unit_id is not declared in structure.units: {unit_id}")
            status = unit.get("status")
            if not nonempty_string(status):
                self.errors.append(f"{prefix}: status must be non-empty text")
            elif status in SOURCE_MAP_POSTPONED_STATUSES and not nonempty_string(unit.get("reason")):
                self.errors.append(f"{prefix}: postponed status requires reason")

            self.validate_source_map_artifact_links(prefix, source_dir, unit.get("artifacts"))
            self.validate_source_map_statement_links(prefix, source_dir, unit.get("statements"))

        missing_ids = sorted(structure_ids - seen_unit_ids)
        if missing_ids:
            self.errors.append(
                f"{rel}: coverage.units missing statuses for structure units: "
                f"{', '.join(missing_ids)}"
            )

    def validate_source_map_artifact_links(
        self, prefix: str, source_dir: Path, artifacts: Any
    ) -> None:
        if artifacts is None:
            return
        if not isinstance(artifacts, list):
            self.errors.append(f"{prefix}: artifacts must be a list")
            return
        for index, artifact in enumerate(artifacts, start=1):
            if not isinstance(artifact, str) or not artifact.strip():
                self.errors.append(f"{prefix}: artifacts #{index} must be non-empty text")
                continue
            if is_bad_absolute_path(artifact):
                self.errors.append(f"{prefix}: artifacts #{index} must be relative: {artifact}")
                continue
            if not (source_dir / artifact).exists():
                self.errors.append(f"{prefix}: artifact link does not exist: {artifact}")

    def validate_source_map_statement_links(
        self, prefix: str, source_dir: Path, statements: Any
    ) -> None:
        if statements is None:
            return
        if not isinstance(statements, list):
            self.errors.append(f"{prefix}: statements must be a list")
            return
        for index, entry in enumerate(statements, start=1):
            if not isinstance(entry, str) or not entry.strip():
                self.errors.append(f"{prefix}: statements #{index} must be non-empty text")
                continue
            if is_bad_absolute_path(entry):
                self.errors.append(f"{prefix}: statements #{index} must be relative: {entry}")
                continue
            statement_path_text, _, statement_id = entry.partition("#")
            statement_path = source_dir / statement_path_text
            if not statement_path.exists():
                self.errors.append(
                    f"{prefix}: statement link file does not exist: {statement_path_text}"
                )
                continue
            if statement_id:
                data = load_yaml(statement_path)
                statement_list = data.get("statements") if isinstance(data, dict) else None
                if not isinstance(statement_list, list) or not any(
                    isinstance(item, dict) and item.get("id") == statement_id
                    for item in statement_list
                ):
                    self.errors.append(
                        f"{prefix}: statement link target is missing in file: {entry}"
                    )

    def source_map_contains_full_text_dump(self, value: Any) -> bool:
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(key, str) and key.casefold() in FULL_TEXT_SOURCE_MAP_KEYS:
                    return True
                if self.source_map_contains_full_text_dump(child):
                    return True
            return False
        if isinstance(value, list):
            return any(self.source_map_contains_full_text_dump(child) for child in value)
        return False

    def validate_statements(self, path: Path, source_id: Any, item_id: Any) -> None:
        rel = self.rel(path)
        data = load_yaml(path)
        statements = data.get("statements") if isinstance(data, dict) else None
        if not isinstance(statements, list):
            self.errors.append(f"{rel}: statements must be a list")
            return

        contract_version = data.get("statement_contract_version", 1) if isinstance(data, dict) else 1
        if contract_version not in {1, 2}:
            self.errors.append(f"{rel}: unsupported statement_contract_version: {contract_version}")
            return

        seen: set[str] = set()
        for index, statement in enumerate(statements, start=1):
            prefix = f"{rel}: statement #{index}"
            if not isinstance(statement, dict):
                self.errors.append(f"{prefix}: statement must be a mapping")
                continue

            required = STATEMENT_V2_REQUIRED if contract_version == 2 else STATEMENT_REQUIRED
            missing = sorted(required - statement.keys())
            if missing:
                self.errors.append(f"{prefix}: missing fields: {', '.join(missing)}")

            statement_id = statement.get("id")
            if isinstance(statement_id, str):
                if statement_id in seen:
                    self.errors.append(f"{prefix}: duplicate statement id: {statement_id}")
                seen.add(statement_id)
                if statement_id in self.statement_ids:
                    self.errors.append(f"{prefix}: duplicate corpus statement id: {statement_id}")
                self.statement_ids.add(statement_id)
            else:
                self.errors.append(f"{prefix}: id must be a string")

            if source_id and statement.get("source_id") != source_id:
                self.errors.append(f"{prefix}: source_id does not match {source_id}")
            if item_id and statement.get("item_id") != item_id:
                self.errors.append(f"{prefix}: item_id does not match {item_id}")

            status = statement.get("status")
            if contract_version == 2 and "status" in statement:
                self.errors.append(
                    f"{prefix}: status is not allowed in statement contract v2. "
                    "use processing_status and evidence assessment fields"
                )
            if status in self.allowed_statement_kinds:
                self.errors.append(
                    f"{prefix}: status contains statement kind {status}; use kind instead"
                )

            kind = statement.get("kind")
            if kind is None:
                if self.strict_statements:
                    self.errors.append(f"{prefix}: missing kind in strict statement validation")
            else:
                if not isinstance(kind, str):
                    self.errors.append(f"{prefix}: kind must be a string")
                elif kind not in self.allowed_statement_kinds:
                    allowed = ", ".join(sorted(self.allowed_statement_kinds))
                    self.errors.append(f"{prefix}: kind must be one of: {allowed}")

            if contract_version == 2:
                self.validate_statement_v2_assessment(statement, prefix)
                processing = statement.get("processing_status")
                processing_blocked = isinstance(processing, dict) and any(
                    value == "blocked" for value in processing.values()
                )
                self.validate_blocker_code(statement, prefix, processing_blocked)
            else:
                self.validate_blocker_code(statement, prefix, status == "blocked")

            text = statement.get("text")
            if isinstance(text, str):
                normalized_text = text.strip().lower()
                if normalized_text.startswith(STATEMENT_TEXT_FORBIDDEN_PREFIXES):
                    self.errors.append(f"{prefix}: text must not be agent retelling of the source")
                if contains_redaction_placeholder(text):
                    self.errors.append(f"{prefix}: text contains inline redaction placeholder")
            elif "text" in statement:
                self.errors.append(f"{prefix}: text must be a string")

            excerpt = statement.get("excerpt")
            if isinstance(excerpt, str):
                if contains_redaction_placeholder(excerpt):
                    self.errors.append(f"{prefix}: excerpt contains inline redaction placeholder")
                if self.strict_statements and not excerpt.strip():
                    self.errors.append(f"{prefix}: excerpt must be non-empty text")
                if (
                    self.strict_statements
                    and isinstance(text, str)
                    and len(normalize_text(text)) > 20
                    and normalize_text(excerpt) == normalize_text(text)
                ):
                    self.errors.append(
                        f"{prefix}: excerpt duplicates statement text; use a source fragment"
                    )
            elif "excerpt" in statement:
                self.errors.append(f"{prefix}: excerpt must be a string")

            artifact = statement.get("artifact")
            artifact_path: Path | None = None
            if isinstance(artifact, str) and artifact.strip():
                artifact_path = path.parent / artifact
                if not artifact_path.exists():
                    self.errors.append(f"{prefix}: artifact does not exist: {artifact}")
            elif "artifact" in statement:
                self.errors.append(f"{prefix}: artifact must be non-empty text")

            if (
                self.strict_statements
                and artifact_path is not None
                and artifact_path.exists()
                and artifact_path.suffix in {".md", ".txt"}
                and isinstance(excerpt, str)
                and excerpt.strip()
            ):
                artifact_text = artifact_path.read_text(encoding="utf-8")
                if not excerpt_found_in_artifact(excerpt, artifact_text):
                    self.errors.append(
                        f"{prefix}: excerpt is not found in referenced text artifact"
                    )

            scope = statement.get("scope")
            if scope is not None and not isinstance(scope, dict):
                self.errors.append(f"{prefix}: scope must be a mapping")
            if (
                self.strict_statements
                and isinstance(scope, dict)
                and "section_title" in scope
                and not nonempty_string(scope.get("section_title"))
            ):
                self.errors.append(f"{prefix}: scope.section_title must be non-empty")

            open_questions = statement.get("open_questions")
            if open_questions is not None and not isinstance(open_questions, list):
                self.errors.append(f"{prefix}: open_questions must be a list")

            self.add_value_errors(prefix, statement)

    def validate_statement_v2_assessment(self, statement: dict[str, Any], prefix: str) -> None:
        processing = statement.get("processing_status")
        if not isinstance(processing, dict):
            self.errors.append(f"{prefix}: processing_status must be a mapping")
        else:
            for field, allowed_values in STATEMENT_PROCESSING_VALUES.items():
                value = processing.get(field)
                if value not in allowed_values:
                    allowed = ", ".join(sorted(allowed_values))
                    self.errors.append(
                        f"{prefix}: processing_status.{field} must be one of: {allowed}"
                    )

        classifications = (
            ("source_role", STATEMENT_SOURCE_ROLES),
            ("evidence_strength", STATEMENT_EVIDENCE_STRENGTHS),
            ("confidence", STATEMENT_CONFIDENCE_VALUES),
            ("temporal_status", STATEMENT_TEMPORAL_STATUSES),
            ("corroboration", STATEMENT_CORROBORATION_VALUES),
        )
        for field, allowed_values in classifications:
            value = statement.get(field)
            if value not in allowed_values:
                allowed = ", ".join(sorted(allowed_values))
                self.errors.append(f"{prefix}: {field} must be one of: {allowed}")

        limitations = statement.get("limitations")
        if not isinstance(limitations, list) or not all(
            isinstance(value, str) and value.strip() for value in limitations
        ):
            self.errors.append(f"{prefix}: limitations must be a list of non-empty strings")

        if isinstance(processing, dict):
            strong_review = processing.get("strong_review")
            if (
                statement.get("confidence") == "low"
                or statement.get("corroboration") == "conflict"
                or processing.get("semantic_review") == "failed"
            ) and strong_review == "not_required":
                self.errors.append(
                    f"{prefix}: low confidence, conflict or failed semantic review "
                    "requires strong_review"
                )

    def validate_derived_statements(self) -> None:
        analysis_root = self.root / "analysis"
        if not analysis_root.exists():
            return

        for path in sorted(analysis_root.glob("*/derived-statements.yml")):
            self.validate_derived_statements_file(path)

    def validate_derived_statements_file(self, path: Path) -> None:
        rel = self.rel(path)
        data = load_yaml(path)
        if not isinstance(data, dict):
            self.errors.append(f"{rel}: derived statement file must be a mapping")
            return

        analysis_id = data.get("analysis_id")
        if not isinstance(analysis_id, str) or not ANALYSIS_ID_PATTERN.fullmatch(analysis_id):
            self.errors.append(
                f"{rel}: analysis_id must contain 2-12 uppercase Latin letters or digits "
                "and start with a letter"
            )
            analysis_id = None
        elif analysis_id in self.analysis_ids:
            self.errors.append(f"{rel}: duplicate analysis_id: {analysis_id}")
        else:
            self.analysis_ids.add(analysis_id)

        if not nonempty_string(data.get("title")):
            self.errors.append(f"{rel}: title must be non-empty text")

        statements = data.get("derived_statements")
        if not isinstance(statements, list) or not statements:
            self.errors.append(f"{rel}: derived_statements must be a non-empty list")
            return

        for index, statement in enumerate(statements, start=1):
            self.validate_derived_statement(
                statement,
                f"{rel}: derived statement #{index}",
                analysis_id,
            )

    def validate_derived_statement(
        self,
        statement: Any,
        prefix: str,
        analysis_id: str | None,
    ) -> None:
        if not isinstance(statement, dict):
            self.errors.append(f"{prefix}: derived statement must be a mapping")
            return

        missing = sorted(DERIVED_STATEMENT_REQUIRED - statement.keys())
        if missing:
            self.errors.append(f"{prefix}: missing fields: {', '.join(missing)}")

        statement_id = statement.get("id")
        expected_pattern = None
        if analysis_id is not None:
            expected_pattern = re.compile(rf"^DRV-{re.escape(analysis_id)}-\d{{3}}$")
        if not isinstance(statement_id, str):
            self.errors.append(f"{prefix}: id must be a string")
        else:
            if expected_pattern is not None and not expected_pattern.fullmatch(statement_id):
                self.errors.append(
                    f"{prefix}: id must match DRV-{analysis_id}-NNN"
                )
            if statement_id in self.statement_ids:
                self.errors.append(f"{prefix}: duplicate corpus statement id: {statement_id}")
            self.statement_ids.add(statement_id)

        kind = statement.get("kind")
        if kind not in DERIVED_STATEMENT_KINDS:
            allowed = ", ".join(sorted(DERIVED_STATEMENT_KINDS))
            self.errors.append(f"{prefix}: kind must be one of: {allowed}; fact is direct only")

        status = statement.get("status")
        if status not in DERIVED_STATEMENT_STATUSES:
            allowed = ", ".join(sorted(DERIVED_STATEMENT_STATUSES))
            self.errors.append(f"{prefix}: status must be one of: {allowed}")

        if not nonempty_string(statement.get("text")):
            self.errors.append(f"{prefix}: text must be non-empty text")
        checked_at = statement.get("checked_at")
        if not isinstance(checked_at, (str, date)) or (
            isinstance(checked_at, str) and not checked_at.strip()
        ):
            self.errors.append(f"{prefix}: checked_at must be a date or non-empty text")
        checked_by = statement.get("checked_by")
        if checked_by not in (None, "") and not nonempty_string(checked_by):
            self.errors.append(f"{prefix}: checked_by must be text when present")
        if status == "confirmed" and not nonempty_string(checked_by):
            self.errors.append(f"{prefix}: confirmed statement requires checked_by")

        scope = statement.get("scope")
        if not isinstance(scope, dict):
            self.errors.append(f"{prefix}: scope must be a mapping")

        for field in ("limitations", "open_questions"):
            if not isinstance(statement.get(field), list):
                self.errors.append(f"{prefix}: {field} must be a list")
        if "result" in statement and not isinstance(statement.get("result"), dict):
            self.errors.append(f"{prefix}: result must be a mapping when present")

        self.validate_derived_from(statement.get("derived_from"), prefix, status)
        self.validate_derivation(statement.get("derivation"), prefix)
        self.add_value_errors(prefix, statement)
        self.derived_statement_count += 1

    def validate_derived_from(self, value: Any, prefix: str, status: Any) -> None:
        if not isinstance(value, dict):
            self.errors.append(f"{prefix}: derived_from must be a mapping")
            return

        statement_ids = value.get("statement_ids", [])
        item_ids = value.get("item_ids", [])
        artifacts = value.get("artifacts", [])
        external_references = value.get("external_references", [])

        collections = {
            "statement_ids": statement_ids,
            "item_ids": item_ids,
            "artifacts": artifacts,
            "external_references": external_references,
        }
        for field, entries in collections.items():
            if not isinstance(entries, list):
                self.errors.append(f"{prefix}: derived_from.{field} must be a list")

        if not any(isinstance(entries, list) and entries for entries in collections.values()):
            self.errors.append(f"{prefix}: derived_from must contain at least one input")

        if isinstance(statement_ids, list):
            for statement_id in statement_ids:
                if not nonempty_string(statement_id):
                    self.errors.append(
                        f"{prefix}: derived_from.statement_ids entries must be non-empty text"
                    )
                elif statement_id.startswith("DRV-"):
                    self.errors.append(
                        f"{prefix}: derived statements must use direct inputs, not {statement_id}"
                    )
                elif statement_id not in self.statement_ids:
                    self.errors.append(
                        f"{prefix}: derived statement input is missing in corpus: {statement_id}"
                    )

        if isinstance(item_ids, list):
            for item_id in item_ids:
                if not nonempty_string(item_id):
                    self.errors.append(
                        f"{prefix}: derived_from.item_ids entries must be non-empty text"
                    )
                elif item_id not in self.item_ids:
                    self.errors.append(
                        f"{prefix}: derived item input is missing in corpus: {item_id}"
                    )

        if isinstance(external_references, list):
            for index, reference in enumerate(external_references, start=1):
                reference_prefix = f"{prefix}: derived_from.external_references #{index}"
                if not isinstance(reference, dict):
                    self.errors.append(
                        f"{reference_prefix}: external reference must be a mapping"
                    )
                    continue
                corpus_source_id = reference.get("corpus_source_id")
                statement_id = reference.get("statement_id")
                revision = reference.get("revision")
                revision_absence_reason = reference.get("revision_absence_reason")
                if not nonempty_string(corpus_source_id):
                    self.errors.append(
                        f"{reference_prefix}: corpus_source_id must be non-empty text"
                    )
                elif corpus_source_id not in self.source_ids:
                    self.errors.append(
                        f"{reference_prefix}: corpus source is missing: {corpus_source_id}"
                    )
                if not nonempty_string(statement_id):
                    self.errors.append(
                        f"{reference_prefix}: statement_id must be non-empty text"
                    )
                if not nonempty_string(revision) and not nonempty_string(
                    revision_absence_reason
                ):
                    self.errors.append(
                        f"{reference_prefix}: revision or revision_absence_reason is required"
                    )
                if status == "confirmed" and not nonempty_string(revision):
                    self.errors.append(
                        f"{reference_prefix}: confirmed external input requires revision"
                    )

        if isinstance(artifacts, list):
            for index, artifact in enumerate(artifacts, start=1):
                artifact_prefix = f"{prefix}: derived_from.artifacts #{index}"
                if not isinstance(artifact, dict):
                    self.errors.append(f"{artifact_prefix}: artifact input must be a mapping")
                    continue
                artifact_path = artifact.get("path")
                if not nonempty_string(artifact_path):
                    self.errors.append(f"{artifact_prefix}: path must be non-empty text")
                elif is_bad_absolute_path(artifact_path) or ".." in PurePosixPath(
                    artifact_path
                ).parts:
                    self.errors.append(f"{artifact_prefix}: path must be repository-relative")
                elif ".local." not in artifact_path and not (self.root / artifact_path).exists():
                    self.errors.append(
                        f"{artifact_prefix}: input artifact does not exist: {artifact_path}"
                    )

                content_hash = artifact.get("content_hash")
                hash_absence_reason = artifact.get("content_hash_absence_reason")
                if not nonempty_string(content_hash) and not nonempty_string(hash_absence_reason):
                    self.errors.append(
                        f"{artifact_prefix}: content_hash or "
                        "content_hash_absence_reason is required"
                    )
                if status == "confirmed" and not nonempty_string(content_hash):
                    self.errors.append(
                        f"{artifact_prefix}: confirmed statement input requires content_hash"
                    )

    def validate_derivation(self, value: Any, prefix: str) -> None:
        if not isinstance(value, dict):
            self.errors.append(f"{prefix}: derivation must be a mapping")
            return

        derivation_type = value.get("type")
        if derivation_type not in DERIVATION_TYPES:
            allowed = ", ".join(sorted(DERIVATION_TYPES))
            self.errors.append(f"{prefix}: derivation.type must be one of: {allowed}")
        if not nonempty_string(value.get("method")):
            self.errors.append(f"{prefix}: derivation.method must be non-empty text")
        parameters = value.get("parameters")
        if parameters is not None and not isinstance(parameters, dict):
            self.errors.append(f"{prefix}: derivation.parameters must be a mapping")

        artifact = value.get("artifact")
        if derivation_type == "aggregation" and not nonempty_string(artifact):
            self.errors.append(f"{prefix}: aggregation derivation requires artifact")
        if artifact is not None:
            if not nonempty_string(artifact):
                self.errors.append(f"{prefix}: derivation.artifact must be non-empty text")
            elif is_bad_absolute_path(artifact) or ".." in PurePosixPath(artifact).parts:
                self.errors.append(f"{prefix}: derivation.artifact must be repository-relative")
            elif not (self.root / artifact).exists():
                self.errors.append(f"{prefix}: derivation artifact does not exist: {artifact}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a project knowledge corpus layout.")
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="Project root containing corpus.yml, catalog.yml and data/.",
    )
    parser.add_argument(
        "--strict-statements",
        action="store_true",
        help=(
            "Treat statement quality gaps as validation errors: missing kind, "
            "duplicated text/excerpt, broken excerpt traceability and empty section titles."
        ),
    )
    parser.add_argument(
        "--strict-concepts",
        action="store_true",
        help=(
            "Require concepts.yml and validate canonical concept definitions, boundaries, "
            "statement provenance and concept relationships."
        ),
    )
    parser.add_argument(
        "--strict-verification",
        action="store_true",
        help=(
            "Warn when an item contract version 2 has no verification.yml and require "
            "a valid hash-bound verification for verification_assessed items."
        ),
    )
    parser.add_argument(
        "--operational",
        action="store_true",
        help=(
            "Scan Git-tracked corpus files for access secrets, credentialed URLs and "
            "possible personal data without printing matched values."
        ),
    )
    parser.add_argument(
        "--operational-policy",
        type=Path,
        help="Optional corpus-relative YAML rules for documented operational suppressions.",
    )
    parser.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
        help="Report format; JSON contains only paths, lines and finding types.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.operational_policy and not args.operational:
        raise RuntimeError("--operational-policy requires --operational")
    return Validator(
        Path(args.root),
        strict_statements=args.strict_statements,
        strict_concepts=args.strict_concepts,
        strict_verification=args.strict_verification,
        operational=args.operational,
        operational_policy=args.operational_policy,
    ).validate(output=args.output)


if __name__ == "__main__":
    sys.exit(main())
