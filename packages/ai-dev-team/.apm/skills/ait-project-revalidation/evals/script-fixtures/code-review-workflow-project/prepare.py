"""Подготовить фикстуру к проверке кода через публичный CLI."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


script = sys.argv[1]
repo = Path(sys.argv[2])


def call(*arguments: str) -> None:
    subprocess.run(
        [sys.executable, script, *arguments, "--repo", str(repo)],
        check=True,
        cwd=repo,
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
    "--concept", "docs/concept.md", "--evidence", "Указатель найден.",
)
call(
    "start-application", "--id", "concept", "--stage", "requirements",
    "--capability", "skill:ait-docs-concept", "--method", "review",
    "--surface", "docs/concept.md", "--action", "Проверить концепцию",
    "--priority-rationale", "Концепция обязательна.", "--subject",
    "docs/concept.md",
)
for criterion in (
    "problem-goal-method-result", "essential-frames", "meaning-and-modality",
):
    call(
        "record-observation", "--application", "concept", "--artifact",
        "docs/concept.md", "--start-line", "1", "--end-line", "3",
        "--criterion-id", criterion, "--result", "supports", "--note",
        "Критерий подтверждён.",
    )
call(
    "finish-application", "--application", "concept", "--outcome", "passed",
    "--decision", "accept", "--evidence", "concept", "--artifact",
    "docs/concept.md", "--coverage", "Концепция проверена.", "--claim",
    "Концепция согласована.", "--claim-support", "observation-001",
    "--challenge", "Есть ли противоречие", "--challenge-outcome", "refuted",
    "--challenge-support", "observation-001",
)
call("record-knowledge", "--result", "absent", "--evidence", "Корпус отсутствует.")
