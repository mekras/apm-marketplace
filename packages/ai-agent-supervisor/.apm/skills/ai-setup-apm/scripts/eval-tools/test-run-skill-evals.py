#!/usr/bin/env python3
"""Проверки разбора длинного ответа модельного прогона."""

from __future__ import annotations

import runpy
import sys
import tempfile
from pathlib import Path


RUNNER = Path(__file__).with_name("run-skill-evals.py")
runner = runpy.run_path(str(RUNNER))
extract_answer_text = runner["extract_answer_text"]
PROMPT = 'Сценарий: {"id": "example-result-case"}'


def main() -> int:
    assert runner["russian_count"](1, "сценарий", "сценария", "сценариев") == "1 сценарий"
    assert runner["russian_count"](2, "сценарий", "сценария", "сценариев") == "2 сценария"
    assert runner["russian_count"](5, "сценарий", "сценария", "сценариев") == "5 сценариев"
    assert runner["russian_count"](11, "сценарий", "сценария", "сценариев") == "11 сценариев"
    assert runner["russian_count"](21, "сценарий", "сценария", "сценариев") == "21 сценарий"

    parsed_config = runner["parse_evals_yaml"](
        """# Комментарии не требуют отдельного YAML-пакета.
adapters:
  adapter: "tools/adapter --flag # не комментарий"
models:
  - adapter:model
workspace_models: []
judge: adapter:judge-model
timeout: 900
repetitions: 3
judge_repetitions: 3
results_dir: eval-results # комментарий
pricing:
  adapter:model:
    input_per_million: 1.5
    output_per_million: 2
"""
    )
    assert parsed_config == {
        "adapters": {"adapter": "tools/adapter --flag # не комментарий"},
        "models": ["adapter:model"],
        "workspace_models": [],
        "judge": "adapter:judge-model",
        "timeout": 900,
        "repetitions": 3,
        "judge_repetitions": 3,
        "results_dir": "eval-results",
        "pricing": {
            "adapter:model": {
                "input_per_million": 1.5,
                "output_per_million": 2,
            }
        },
    }
    try:
        runner["parse_evals_yaml"]("models:\n    - adapter:model\n")
    except ValueError as error:
        assert "поддерживаемую схему" in str(error)
    else:
        raise AssertionError("Неподдерживаемый отступ YAML должен отклоняться")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        sample = root / "evals.sample.yml"
        sample.write_text("models: []\n", encoding="utf-8")
        config = root / "evals.local.yml"
        runner["bootstrap_config"](root, config)
        assert config.read_text(encoding="utf-8") == "models: []\n"

    answer = extract_answer_text(
        """<<ANSWER>>
Текст с \"кавычками\", списком и {\"фрагментом\": \"JSON\"}.
</ANSWER>""",
        PROMPT,
    )
    assert answer == {
        "answers": [{
            "id": "example-result-case",
            "answer": 'Текст с "кавычками", списком и {"фрагментом": "JSON"}.',
        }],
    }

    unterminated_answer = extract_answer_text(
        """<<ANSWER>>
Ответ модели без закрывающего маркера.

```md
# AGENTS.md
```""",
        PROMPT,
    )
    assert unterminated_answer["answers"][0]["answer"] == (
        "Ответ модели без закрывающего маркера.\n\n```md\n# AGENTS.md\n```"
    )

    plain_answer = extract_answer_text(
        "Обычный текст без служебной оболочки.",
        PROMPT,
    )
    assert plain_answer["answers"][0]["answer"] == (
        "Обычный текст без служебной оболочки."
    )

    json_answer = extract_answer_text(
        '{"answers":[{"id":"example-result-case","answer":"Прежний JSON."}]}',
        PROMPT,
    )
    assert json_answer["answers"][0]["answer"] == "Прежний JSON."

    legacy_answer = extract_answer_text(
        """<<ANSWER>>
Ответ в прежней оболочке.
<</ANSWER>>""",
        PROMPT,
    )
    assert legacy_answer["answers"][0]["answer"] == "Ответ в прежней оболочке."

    adapter_output = """<<ANSWER>>
Ответ через адаптер без закрывающего маркера.

```md
# AGENTS.md
```
"""
    adapter_code = (
        "import sys; "
        "prompt = sys.stdin.read(); "
        "assert 'Верни только обычный текст ответа.' in prompt; "
        f"print({adapter_output!r})"
    )
    call = runner["make_model_call"](
        [sys.executable, "-c", adapter_code],
        "test-model",
        10,
    )
    adapter_answer = call(PROMPT, runner["ANSWER_SCHEMA"])
    assert adapter_answer["answers"][0]["answer"] == (
        "Ответ через адаптер без закрывающего маркера.\n\n"
        "```md\n# AGENTS.md\n```"
    )

    judge_prompt = runner["fixture_judge_prompt"](
        {
            "id": "fixture-case",
            "target_skill": "example",
            "oracle_data": {
                "success_criteria": ["результат подтверждён"],
                "failure_indicators": ["результат не подтверждён"],
                "fixture_checks": [{"command": ["python3", "check.py"], "exit_code": 1}],
            },
        },
        {"answer": "готово"},
        "skill",
    )
    assert "fixture_checks" not in judge_prompt
    assert "результат подтверждён" in judge_prompt

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fixture = root / "fixture"
        fixture.mkdir()
        (fixture / "AGENTS.md").write_text("Проверь проект.\n", encoding="utf-8")
        skill_dirs = []
        for name, description, body in (
            ("audit", "Аудит проекта", "SECRET AUDIT BODY"),
            ("writing", "Документация", "SECRET WRITING BODY"),
        ):
            skill_dir = root / name
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
                encoding="utf-8",
            )
            skill_dirs.append(skill_dir)
        fixture_case = {"id": "fixture", "prompt": "Проверь проект", "fixture_dir": fixture, "target_skill": "audit"}
        selection_prompt = runner["fixture_catalog_selection_prompt"](fixture_case, skill_dirs)
        assert "Аудит проекта" in selection_prompt
        assert "SECRET AUDIT BODY" not in selection_prompt
        application_prompt = runner["fixture_candidate_prompt"](
            fixture_case,
            "catalog",
            skill_dirs,
            False,
            "audit",
        )
        assert "SECRET AUDIT BODY" in application_prompt
        assert "SECRET WRITING BODY" not in application_prompt

        invalid_adapter = [sys.executable, "-c", "print('not-json')"]
        fixture_case.update(
            {
                "oracle_data": {
                    "success_criteria": ["успех"],
                    "failure_indicators": ["провал"],
                }
            }
        )
        errors, records = runner["run_fixture_evals"](
            repo_root=root,
            cases=[fixture_case],
            skill_dirs=skill_dirs,
            run={"adapter": invalid_adapter, "model": "candidate", "label": "invalid", "workspace": False},
            judge={"adapter": invalid_adapter, "model": "judge", "label": "invalid-judge"},
            timeout=10,
            repetitions=1,
            judge_repetitions=1,
            pricing={},
        )
        assert len(records) == 3
        assert all(record["candidate_error"] for record in records)
        assert len(errors) == 2

    print("Проверки разбора длинных ответов модельного прогона пройдены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
