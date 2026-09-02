#!/usr/bin/env python3
"""Готовит состояние через публичный CLI для сценариев проверки корпуса."""

from __future__ import annotations

import shutil
import subprocess
import sys
import json
from pathlib import Path


script = sys.argv[1]
repo = Path(sys.argv[2])


def call(*arguments: str) -> None:
    subprocess.run(
        [sys.executable, script, *arguments, "--repo", str(repo)],
        check=True,
        cwd=repo,
    )


shutil.move(
    repo / ".agents/skills/ait-docs-concept/SKILL.md.fixture",
    repo / ".agents/skills/ait-docs-concept/SKILL.md",
)
subprocess.run(["git", "init", str(repo)], check=True)
subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
subprocess.run(
    [
        "git", "-C", str(repo), "-c", "user.email=contract@example.invalid",
        "-c", "user.name=Contract", "commit", "-m", "init",
    ],
    check=True,
)
call("init", "--mode", "manual")
call(
    "record-concept", "--result", "found", "--instructions", "AGENTS.md",
    "--concept", "docs/concept.md", "--evidence", "AGENTS.md указывает концепцию",
)
call(
    "classify", "--id", "rule:AGENTS", "--participation", "not_applicable",
    "--applicable", "no", "--reason", "Правило не является отдельной проверкой",
)
call(
    "start-application", "--id", "concept-check", "--stage", "requirements",
    "--capability", "skill:ait-docs-concept", "--method", "review",
    "--surface", "docs/concept.md", "--action", "Проверить концепцию",
    "--priority-rationale", "Концепция задаёт основание проверки",
    "--subject", "docs/concept.md",
)
for criterion, note in (
    ("problem-goal-method-result", "Четыре элемента согласованы"),
    ("essential-frames", "Рамки не противоречат замыслу"),
    ("meaning-and-modality", "Смысловых противоречий нет"),
):
    call(
        "record-observation", "--application", "concept-check",
        "--artifact", "docs/concept.md", "--start-line", "1", "--end-line", "3",
        "--criterion-id", criterion, "--result", "supports", "--note", note,
    )
call(
    "finish-application", "--application", "concept-check", "--outcome", "passed",
    "--decision", "accept", "--evidence", "observation-001", "--evidence",
    "observation-002", "--evidence", "observation-003", "--artifact",
    "docs/concept.md", "--coverage", "Критерии проверены по концепции",
    "--claim", "Концепция содержит основание проекта", "--claim-support",
    "observation-001", "--challenge", "Есть ли скрытое противоречие",
    "--challenge-outcome", "refuted", "--challenge-support", "observation-001",
)
mode = sys.argv[3] if len(sys.argv) > 3 else None
if mode in {
    "knowledge", "technical", "semantic", "semantic-broad", "complete", "blocking",
    "requirements-validation",
}:
    call(
        "record-knowledge", "--result", "found", "--root", "knowledge",
        "--evidence", "Корпус находится в knowledge",
    )
    state = json.loads(
        (repo / ".git/ai-dev-team/project-review-state.json").read_text(
            encoding="utf-8",
        ),
    )
    subjects = state["knowledge_review"]["subjects"]
    if any(".local." in item["reference"] for item in subjects):
        raise RuntimeError("локальные файлы попали в технический состав корпуса")
if mode in {
    "technical", "semantic", "semantic-broad", "complete", "blocking",
    "requirements-validation",
}:
    call(
        "start-application", "--id", "knowledge-technical", "--stage",
        "repository", "--capability", "skill:kc-validation", "--method",
        "validation", "--surface", "knowledge/index.md", "--action",
        "Проверить структуру корпуса", "--priority-rationale",
        "Корпус обязателен после концепции", "--knowledge-phase", "technical",
        "--subject-pattern", "knowledge/**",
    )
    state = json.loads(
        (repo / ".git/ai-dev-team/project-review-state.json").read_text(
            encoding="utf-8",
        ),
    )
    technical_scope = state["applications"]["knowledge-technical"][
        "subject_scope"
    ]
    technical_subjects = [item["reference"] for item in technical_scope]
    registered_subjects = [
        item["reference"] for item in state["knowledge_review"]["subjects"]
    ]
    if technical_subjects != registered_subjects:
        raise RuntimeError(
            "техническая область не совпадает с зарегистрированным составом "
            "корпуса",
        )
    if any(".local." in reference for reference in technical_subjects):
        raise RuntimeError(
            "локальный файл попал в техническую область применения",
        )
    call(
        "finish-application", "--application", "knowledge-technical", "--outcome",
        "passed", "--decision", "accept", "--evidence", "technical-evidence",
        "--artifact", "knowledge/catalog.yml", "--artifact", "knowledge/corpus.yml",
        "--artifact", "knowledge/data/test/response-headers.txt", "--artifact",
        "knowledge/data/test/source.yml", "--artifact",
        "knowledge/data/test/statements.yml",
        "--coverage", "Корпус проверен", "--claim", "Корпус структурно пригоден",
        "--challenge",
        "Есть ли недоступный материал корпуса", "--challenge-outcome", "refuted",
        "--command", "true",
    )
if mode in {"semantic", "complete", "blocking", "requirements-validation"}:
    call(
        "start-application", "--id", "knowledge-semantic", "--stage",
        "repository", "--capability", "skill:kc-validation", "--method", "review",
        "--surface", "knowledge/index.md", "--action", "Проверить смысл корпуса",
        "--priority-rationale", "Смысл корпуса определяет основания решений",
        "--knowledge-phase", "semantic", "--subject-pattern", "knowledge/corpus.yml",
        "--subject-pattern", "knowledge/catalog.yml", "--subject-pattern",
        "knowledge/**/source.yml", "--subject-pattern", "knowledge/**/statements.yml",
    )
    for artifact, end_line, criterion, note in (
        ("knowledge/data/test/source.yml", "2", "source-concept-fit", "Источник раскрывает состав корпуса."),
        ("knowledge/data/test/statements.yml", "3", "statement-consistency", "Утверждение относится к корпусу."),
        ("knowledge/catalog.yml", "4", "coverage-gaps", "Ограничения охвата названы."),
        ("knowledge/corpus.yml", "1", "decision-value", "Утверждение пригодно для решения."),
    ):
        call(
            "record-observation", "--application", "knowledge-semantic",
            "--artifact", artifact, "--start-line", "1", "--end-line", end_line,
            "--criterion-id", criterion, "--result", "supports",
            "--note", note,
        )
    call(
        "finish-application", "--application", "knowledge-semantic", "--outcome",
        "passed", "--decision", "accept", "--evidence", "semantic-evidence",
        "--artifact", "knowledge/catalog.yml", "--artifact", "knowledge/corpus.yml",
        "--artifact", "knowledge/data/test/source.yml", "--artifact",
        "knowledge/data/test/statements.yml",
        "--coverage", "Смысл корпуса проверен", "--claim",
        "Корпус содержит проверяемое утверждение", "--claim-support",
        "observation-001", "--challenge", "Есть ли разрыв между индексом и утверждением",
        "--challenge-outcome", "refuted", "--challenge-support", "observation-001",
    )
if mode == "semantic-broad":
    call(
        "start-application", "--id", "knowledge-semantic", "--stage",
        "repository", "--capability", "skill:kc-validation", "--method", "review",
        "--surface", "knowledge/**", "--action", "Проверить смысл корпуса",
        "--priority-rationale", "Смысл корпуса определяет основания решений",
        "--knowledge-phase", "semantic", "--subject-pattern", "knowledge/**",
    )
if mode == "requirements-validation":
    call(
        "start-application", "--id", "requirements-validation", "--stage",
        "requirements", "--capability", "skill:ait-docs-concept", "--method",
        "validation", "--surface", "docs/concept.md", "--action",
        "Проверить структурный договор концепции", "--priority-rationale",
        "Структурная проверка устраняет риск недостоверного договора", "--subject",
        "docs/concept.md",
    )
if mode in {"complete", "blocking"}:
    call(
        "start-application", "--id", "requirements-check", "--stage",
        "requirements", "--capability", "skill:ait-req-revalidation", "--method",
        "review", "--surface", "docs/concept.md", "--action",
        "Проверить требования", "--priority-rationale",
        "Требования определяют последующую работу", "--subject-pattern",
        "docs/concept.md",
    )
    for criterion, note, result in (
        (
            "level-and-solution-boundary",
            "Требование смешано с решением.",
            "problem" if mode == "blocking" else "supports",
        ),
        ("source-and-necessity", "Основание связано с концепцией.", "supports"),
        ("clarity-and-verifiability", "Формулировка проверяема.", "supports"),
        ("set-consistency-and-traceability", "Связь с концепцией сохранена.", "supports"),
    ):
        call(
            "record-observation", "--application", "requirements-check",
            "--artifact", "docs/concept.md", "--start-line", "1", "--end-line", "3",
            "--criterion-id", criterion, "--result", result, "--note", note,
        )
    if mode == "blocking":
        call(
            "record-finding", "--id", "requirements-blocker", "--stage",
            "requirements", "--summary", "Требование смешано с решением.",
            "--blocking", "--evidence", "Наблюдение requirements-check:001",
            "--observation", "observation-001", "--allowed-path",
            "docs/concept.md", "--verification", "Повторно проверить требования.",
        )
    call(
        "finish-application", "--application", "requirements-check", "--outcome",
        "failed" if mode == "blocking" else "passed", "--decision",
        "reject" if mode == "blocking" else "accept", "--evidence", "requirements-evidence",
        "--artifact", "docs/concept.md", "--coverage", "Требования проверены",
        "--claim", "Требования требуют исправления" if mode == "blocking" else "Требования согласованы с концепцией", "--claim-support",
        "observation-001", "--challenge", "Есть ли разрыв с концепцией",
        "--challenge-outcome", "confirmed" if mode == "blocking" else "refuted", "--challenge-support", "observation-001",
    )
    if mode == "complete":
        call(
            "start-application", "--id", "code-review", "--stage", "code",
            "--capability", "skill:ait-code-review", "--method", "review",
            "--surface", "src/module.py", "--action", "Проверить реализацию",
            "--priority-rationale", "Исходный код требует содержательного обзора.",
            "--subject-pattern", "src/*.py",
        )
        for criterion, note in (
            ("implementation-quality", "Реализация читаема и не имеет внешних границ."),
            ("implementation-traceability", "Реализация относится к проверяемой фикстуре."),
        ):
            call(
                "record-observation", "--application", "code-review", "--artifact",
                "src/module.py", "--start-line", "1", "--end-line", "2",
                "--criterion-id", criterion, "--result", "supports", "--note", note,
            )
        call(
            "finish-application", "--application", "code-review", "--outcome",
            "passed", "--decision", "accept", "--evidence", "code-review-evidence",
            "--artifact", "src/module.py", "--coverage", "Реализация проверена.",
            "--claim", "Реализация соответствует фикстуре.", "--claim-support",
            "observation-001", "--challenge", "Есть ли скрытая ошибка",
            "--challenge-outcome", "refuted", "--challenge-support",
            "observation-001",
        )
    for stage in ("repository", "requirements", "design", "code", "tests", "assurance", "impact") if mode == "complete" else ():
        call("stage", "--name", stage, "--status", "running")
        call("stage", "--name", stage, "--status", "complete")
