#!/usr/bin/env python3
"""Общие операции с наборами проверки доступности знаний."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - зависит от целевого окружения
    raise SystemExit(
        "Для средств проверки доступности знаний нужен пакет PyYAML."
    ) from exc


SUITE_VERSION = 1
CASE_STATUSES = {"candidate", "approved", "rejected"}
QUESTION_TYPES = {"direct", "paraphrase", "application"}
IMPORTANCE_CLASSES = {
    "definition",
    "rule",
    "constraint",
    "procedure",
    "criterion",
    "fact",
    "limitation",
    "example",
    "conflict",
}
REPRESENTATIONS = {"present", "partial", "absent", "unknown"}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as source:
        return yaml.safe_load(source)


def dump_yaml(data: Any) -> str:
    return yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        width=100,
    )


def nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def safe_relative_path(value: Any) -> bool:
    if not nonempty_string(value):
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def extract_source_text(
    corpus_root: Path,
    source: dict[str, Any],
) -> tuple[str | None, str | None]:
    artifact = source.get("artifact")
    line_start = source.get("line_start")
    line_end = source.get("line_end")
    if not safe_relative_path(artifact):
        return None, "source.artifact must be a safe relative path"
    if not isinstance(line_start, int) or line_start < 1:
        return None, "source.line_start must be a positive integer"
    if not isinstance(line_end, int) or line_end < line_start:
        return None, "source.line_end must be greater than or equal to line_start"

    root = corpus_root.resolve()
    path = (root / artifact).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None, "source.artifact resolves outside corpus root"
    if not path.is_file():
        return None, f"source artifact does not exist: {artifact}"

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return None, f"source artifact is not UTF-8 text: {artifact}"
    if line_end > len(lines):
        return None, (
            f"source line range {line_start}-{line_end} exceeds "
            f"artifact length {len(lines)}"
        )
    return "\n".join(lines[line_start - 1 : line_end]), None


def source_text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_suite(
    data: Any,
    *,
    corpus_root: Path | None = None,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(data, dict):
        return ["suite root must be a mapping"], warnings

    if data.get("suite_version") != SUITE_VERSION:
        errors.append(f"suite_version must be {SUITE_VERSION}")
    suite_id = data.get("id")
    if not nonempty_string(suite_id):
        errors.append("id must be non-empty text")
    elif not ID_PATTERN.fullmatch(suite_id):
        errors.append("id must contain only lowercase letters, digits, dots, dashes or underscores")
    if not nonempty_string(data.get("description")):
        errors.append("description must be non-empty text")

    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append("cases must be a non-empty list")
        return errors, warnings

    seen_case_ids: set[str] = set()
    for index, case in enumerate(cases, start=1):
        prefix = f"case #{index}"
        if not isinstance(case, dict):
            errors.append(f"{prefix}: case must be a mapping")
            continue
        case_id = case.get("id")
        if not nonempty_string(case_id):
            errors.append(f"{prefix}: id must be non-empty text")
        elif not ID_PATTERN.fullmatch(case_id):
            errors.append(f"{prefix}: id has unsupported characters")
        elif case_id in seen_case_ids:
            errors.append(f"{prefix}: duplicate id {case_id}")
        else:
            seen_case_ids.add(case_id)

        status = case.get("status")
        if status not in CASE_STATUSES:
            errors.append(f"{prefix}: status must be one of {sorted(CASE_STATUSES)}")
            continue
        source = case.get("source")
        validate_source(source, prefix, errors)
        if corpus_root is not None and isinstance(source, dict):
            validate_source_content(source, corpus_root, prefix, errors)

        if status == "candidate":
            continue
        if status == "rejected":
            if not nonempty_string(case.get("rejection_reason")):
                errors.append(f"{prefix}: rejected case requires rejection_reason")
            continue
        validate_approved_case(case, prefix, errors, warnings)
        if corpus_root is not None:
            validate_corpus_expectation(case, corpus_root, prefix, errors)

    return errors, warnings


def validate_source(source: Any, prefix: str, errors: list[str]) -> None:
    if not isinstance(source, dict):
        errors.append(f"{prefix}: source must be a mapping")
        return
    if not safe_relative_path(source.get("artifact")):
        errors.append(f"{prefix}: source.artifact must be a safe relative path")
    line_start = source.get("line_start")
    line_end = source.get("line_end")
    if not isinstance(line_start, int) or line_start < 1:
        errors.append(f"{prefix}: source.line_start must be a positive integer")
    if not isinstance(line_end, int) or not isinstance(line_start, int) or line_end < line_start:
        errors.append(f"{prefix}: source.line_end must not precede line_start")
    digest = source.get("sha256")
    if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
        errors.append(f"{prefix}: source.sha256 must be a lowercase SHA-256 digest")


def validate_source_content(
    source: dict[str, Any],
    corpus_root: Path,
    prefix: str,
    errors: list[str],
) -> None:
    text, error = extract_source_text(corpus_root, source)
    if error:
        errors.append(f"{prefix}: {error}")
        return
    expected = source.get("sha256")
    actual = source_text_sha256(text or "")
    if expected != actual:
        errors.append(f"{prefix}: source.sha256 does not match selected source lines")


def validate_approved_case(
    case: dict[str, Any],
    prefix: str,
    errors: list[str],
    warnings: list[str],
) -> None:
    target = case.get("target")
    if not isinstance(target, dict):
        errors.append(f"{prefix}: approved case requires target")
    else:
        if not nonempty_string(target.get("text")):
            errors.append(f"{prefix}: target.text must be non-empty text")
        importance_class = target.get("importance_class")
        if importance_class not in IMPORTANCE_CLASSES:
            errors.append(
                f"{prefix}: target.importance_class must be one of "
                f"{sorted(IMPORTANCE_CLASSES)}"
            )
        if not nonempty_string(target.get("importance_rationale")):
            errors.append(f"{prefix}: target.importance_rationale must be non-empty text")
        limitations = target.get("limitations")
        if not isinstance(limitations, list):
            errors.append(f"{prefix}: target.limitations must be a list")

    validate_string_list(case.get("acceptable_answers"), f"{prefix}: acceptable_answers", errors)
    validate_string_list(
        case.get("must_not_claim"),
        f"{prefix}: must_not_claim",
        errors,
        allow_empty=True,
    )

    expectation = case.get("corpus_expectation")
    if not isinstance(expectation, dict):
        errors.append(f"{prefix}: corpus_expectation must be a mapping")
    else:
        if expectation.get("representation") not in REPRESENTATIONS:
            errors.append(
                f"{prefix}: corpus_expectation.representation must be one of "
                f"{sorted(REPRESENTATIONS)}"
            )
        statement_ids = expectation.get("statement_ids")
        if not isinstance(statement_ids, list) or not all(
            nonempty_string(item) for item in statement_ids
        ):
            errors.append(f"{prefix}: corpus_expectation.statement_ids must be a string list")
        elif expectation.get("representation") in {"present", "partial"} and not statement_ids:
            errors.append(
                f"{prefix}: present or partial corpus expectation requires statement_ids"
            )
        elif expectation.get("representation") in {"absent", "unknown"} and statement_ids:
            errors.append(
                f"{prefix}: absent or unknown corpus expectation must not contain statement_ids"
            )

    questions = case.get("questions")
    question_types: set[str] = set()
    question_ids: set[str] = set()
    if not isinstance(questions, list) or len(questions) < 2:
        errors.append(f"{prefix}: approved case requires at least two questions")
    else:
        for question_index, question in enumerate(questions, start=1):
            question_prefix = f"{prefix}: question #{question_index}"
            if not isinstance(question, dict):
                errors.append(f"{question_prefix}: question must be a mapping")
                continue
            question_id = question.get("id")
            if not nonempty_string(question_id):
                errors.append(f"{question_prefix}: id must be non-empty text")
            elif question_id in question_ids:
                errors.append(f"{question_prefix}: duplicate id {question_id}")
            else:
                question_ids.add(question_id)
            question_type = question.get("type")
            if question_type not in QUESTION_TYPES:
                errors.append(
                    f"{question_prefix}: type must be one of {sorted(QUESTION_TYPES)}"
                )
            else:
                question_types.add(question_type)
            if not nonempty_string(question.get("text")):
                errors.append(f"{question_prefix}: text must be non-empty")
        for required_type in ("direct", "paraphrase"):
            if required_type not in question_types:
                errors.append(f"{prefix}: approved case requires a {required_type} question")

    provenance = case.get("provenance")
    review = case.get("review")
    target_author = provenance.get("target_author") if isinstance(provenance, dict) else None
    if not nonempty_string(target_author):
        errors.append(f"{prefix}: provenance.target_author must be non-empty text")
    if not isinstance(review, dict):
        errors.append(f"{prefix}: review must be a mapping")
        return
    if review.get("target_entailment") != "confirmed":
        errors.append(f"{prefix}: review.target_entailment must be confirmed")
    if review.get("question_answerability") != "confirmed":
        errors.append(f"{prefix}: review.question_answerability must be confirmed")
    target_reviewer = review.get("target_reviewer")
    question_reviewer = review.get("question_reviewer")
    if not nonempty_string(target_reviewer):
        errors.append(f"{prefix}: review.target_reviewer must be non-empty text")
    if not nonempty_string(question_reviewer):
        errors.append(f"{prefix}: review.question_reviewer must be non-empty text")
    if nonempty_string(target_author) and target_author == target_reviewer:
        errors.append(f"{prefix}: target author and target reviewer must differ")
    if target_reviewer == question_reviewer:
        warnings.append(
            f"{prefix}: one reviewer checked both source entailment and question answerability"
        )

    access_review = case.get("access_review")
    if not isinstance(access_review, dict):
        errors.append(f"{prefix}: access_review must be a mapping")
    else:
        if access_review.get("source_evidence_to_judge") != "approved":
            errors.append(
                f"{prefix}: access_review.source_evidence_to_judge must be approved"
            )
        if not nonempty_string(access_review.get("reviewed_by")):
            errors.append(f"{prefix}: access_review.reviewed_by must be non-empty text")
        if not nonempty_string(access_review.get("rationale")):
            errors.append(f"{prefix}: access_review.rationale must be non-empty text")


def validate_corpus_expectation(
    case: dict[str, Any],
    corpus_root: Path,
    prefix: str,
    errors: list[str],
) -> None:
    expectation = case.get("corpus_expectation")
    if not isinstance(expectation, dict):
        return
    expected_ids = expectation.get("statement_ids")
    if not isinstance(expected_ids, list) or not expected_ids:
        return

    found_ids: set[str] = set()
    for path in corpus_root.rglob("statements.yml"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            data = load_yaml(path)
        except Exception:
            continue
        statements = data.get("statements") if isinstance(data, dict) else None
        if not isinstance(statements, list):
            continue
        found_ids.update(
            statement.get("id")
            for statement in statements
            if isinstance(statement, dict) and nonempty_string(statement.get("id"))
        )
    missing = sorted(set(expected_ids) - found_ids)
    if missing:
        errors.append(
            f"{prefix}: corpus expectation references missing statement ids: "
            f"{', '.join(missing)}"
        )


def validate_string_list(
    value: Any,
    label: str,
    errors: list[str],
    *,
    allow_empty: bool = False,
) -> None:
    if not isinstance(value, list):
        errors.append(f"{label} must be a list")
        return
    if not value and not allow_empty:
        errors.append(f"{label} must not be empty")
        return
    if not all(nonempty_string(item) for item in value):
        errors.append(f"{label} must contain only non-empty strings")
