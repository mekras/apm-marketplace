#!/usr/bin/env python3
"""Проверки разбора длинного ответа модельного прогона."""

from __future__ import annotations

import runpy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


RUNNER = Path(__file__).with_name("run-skill-evals.py")
CLAUDE_ADAPTER = Path(__file__).parent / "adapters" / "claude"
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

    timeout_call = runner["make_model_call"](
        [sys.executable, "-c", "import time; time.sleep(1)"],
        "test-model",
        0.01,
    )
    try:
        timeout_call(PROMPT, runner["ANSWER_SCHEMA"])
    except RuntimeError as error:
        assert "превысил тайм-аут" in str(error)
    else:
        raise AssertionError("Тайм-аут адаптера должен стать ошибкой модельного вызова")

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
            True,
            ["audit"],
        )
        assert ".agents/skills/audit/SKILL.md" in application_prompt
        assert "SECRET AUDIT BODY" not in application_prompt
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
            run={"adapter": invalid_adapter, "model": "candidate", "label": "invalid", "workspace": True},
            judge={"adapter": invalid_adapter, "model": "judge", "label": "invalid-judge"},
            timeout=10,
            repetitions=1,
            judge_repetitions=1,
            pricing={},
        )
        assert len(records) == 3
        assert all(record["candidate_error"] for record in records)
        assert len(errors) == 2

        binary_before = root / "binary-before"
        binary_after = root / "binary-after"
        binary_before.mkdir()
        binary_after.mkdir()
        (binary_before / "output.bin").write_bytes(b"\xff\x00")
        (binary_after / "output.bin").write_bytes(b"\xff\x01")
        binary_diff = runner["directory_diff"](binary_before, binary_after)
        assert "Binary files a/output.bin and b/output.bin differ" in binary_diff

        valid_adapter = [
            sys.executable,
            "-c",
            "import json,sys; prompt=sys.stdin.read(); "
            "print(json.dumps({'selected_skills':['audit']} if '\"required\": [\"selected_skills\"]' in prompt else {'answer':'готово'}))",
        ]
        timed_out_judge = [sys.executable, "-c", "import time; time.sleep(1)"]
        errors, records = runner["run_fixture_evals"](
            repo_root=root,
            cases=[fixture_case],
            skill_dirs=skill_dirs,
            run={"adapter": valid_adapter, "model": "candidate", "label": "candidate", "workspace": True},
            judge={"adapter": timed_out_judge, "model": "judge", "label": "judge"},
            timeout=0.2,
            repetitions=1,
            judge_repetitions=1,
            pricing={},
        )
        assert len(records) == 3
        assert all(record["judge_error"] for record in records)
        assert all(record["judge_execution"] for record in records)
        assert len(errors) == 2

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        workspace = root / "рабочая папка"
        workspace.mkdir()
        fake_bin = root / "bin"
        fake_bin.mkdir()
        fake_claude = fake_bin / "claude"
        fake_claude.write_text("#!/usr/bin/env sh\npwd\ntouch changed-by-claude\n", encoding="utf-8")
        fake_claude.chmod(0o755)
        result = subprocess.run(
            ["bash", str(CLAUDE_ADAPTER), "fixture-model"],
            input="проверка",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "APM_EVAL_WORKSPACE": str(workspace),
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            },
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(workspace)
        assert (workspace / "changed-by-claude").is_file()

    test_workspace_evidence()
    test_cost_accounting()
    test_result_workspace()
    test_adapter_traces()
    test_command_line()
    test_comparison()
    print("Проверки ответов, рабочих копий и доказательств модельного прогона пройдены.")
    return 0


def sample_skills(root: Path) -> list[Path]:
    skills = []
    for name in ("audit", "writing"):
        skill = root / "packages" / name
        (skill / "references").mkdir(parents=True)
        (skill / "assets").mkdir()
        (skill / "scripts").mkdir()
        (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: example\n---\nRead references/rules.md\n")
        (skill / "references/rules.md").write_text("complete-reference")
        (skill / "assets/data.bin").write_bytes(b"\xff\x00")
        script = skill / "scripts/check"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        skills.append(skill)
    return skills


def test_workspace_evidence() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fixture = root / "fixture"
        fixture.mkdir()
        (fixture / "README.md").write_text("original\n")
        skills = sample_skills(root)
        case = {"id": "task", "prompt": "Измени README.md", "fixture_dir": fixture,
                "target_skill": "audit", "catalog_skill": "audit", "oracle": "oracle.json",
                "oracle_data": {"success_criteria": ["README updated"],
                                "required_diff": {"paths": ["README.md"], "must_include": ["updated"]}}}
        (root / "oracle.json").write_text(json.dumps(case["oracle_data"]))
        judge = {"adapter": [sys.executable, "-c", "import json; print(json.dumps({'output':json.dumps({'results':[{'id':'task','passed':True}]}),'usage':{'cost':2,'currency':'USD'}}))"],
                 "model": "judge", "label": "judge"}
        for selected in ([], ["audit", "writing"]):
            code = f'''
import json, os, sys
from pathlib import Path
def emit(value):
    print(json.dumps({{"output":json.dumps(value), "usage":{{"cost":1,"currency":"USD"}}}}))
prompt = sys.stdin.read()
workspace = Path(os.environ["APM_EVAL_WORKSPACE"])
assert Path.cwd() == workspace
assert not (workspace / "oracle.json").exists()
packages = workspace / ".agents/skills"
if packages.exists():
    for name in ("audit", "writing"):
        for mount in (".agents/skills", ".claude/skills"):
            skill = workspace / mount / name
            assert (skill / "references/rules.md").read_text() == "complete-reference"
            assert (skill / "assets/data.bin").read_bytes() == b"\\xff\\x00"
            assert os.access(skill / "scripts/check", os.X_OK)
if '"required": ["selected_skills"]' in prompt:
    emit({{"selected_skills": {selected!r}}})
else:
    assert (workspace / "README.md").read_text() == "original\\n"
    (workspace / "README.md").write_text("updated\\n")
    Path(os.environ["APM_EVAL_TRACE"]).write_text(json.dumps({{"tool": "write", "path": "README.md", "exit_code": 0, "cwd": str(workspace)}}))
    emit({{"answer": "готово", "selected_skills": {selected!r} if packages.exists() else []}})
'''
            run = {"adapter": [sys.executable, "-c", code], "model": "candidate", "label": "candidate", "workspace": True}
            ledger = []
            errors, records = runner["run_fixture_evals"](
                repo_root=root, cases=[case], skill_dirs=skills, run=run, judge=judge,
                timeout=10, repetitions=2, judge_repetitions=3, pricing={}, call_records=ledger)
            assert not errors, errors
            assert len(records) == 6 and all(item["passed"] for item in records)
            assert len(ledger) == 26
            assert [item["id"] for item in ledger] == list(range(1, 27))
            assert runner["summarize_calls"](ledger)["total_cost"] == 44
            assert [call_id for record in records for call_id in record["call_ids"]] == list(range(1, 27))
            paths = []
            for record in records:
                evidence = record["evidence"]
                assert evidence["trace_available"] and not evidence["package_changes"]
                assert evidence["final_files"][0]["content"] == "updated\n"
                assert "+++ b/README.md" in evidence["workspace_diff"]
                assert ".git/" not in evidence["workspace_diff"]
                trace = json.loads(evidence["execution"][-1]["trace"])
                paths.append(trace["cwd"])
                assert not Path(trace["cwd"]).exists()
                if record["mode"] == "catalog":
                    assert record["routing"]["initial_selected_skills"] == selected
                    assert len(record["call_ids"]) == 5
                    phases = [ledger[call_id - 1]["context"]["phase"] for call_id in record["call_ids"]]
                    assert phases == ["selection", "application", "fixture", "fixture", "fixture"]
            assert len(set(paths)) == 6
            assert (fixture / "README.md").read_text() == "original\n"
            report = runner["write_fixture_report"](root, Path("report.json"), records, skills, [case], 2, 3, call_records=ledger)
            report_data = json.loads(report.read_text())
            assert report_data["schema_version"] == 3
            assert report_data["accounting"]["total_cost"] == 44
            assert len(report_data["calls"]) == 26
            assert "references/rules.md" in report_data["provenance"]["skills"]["packages/audit"]

        # Даже согласный судья не превращает рассказ о правке в изменение файла.
        fake = "import sys; p=sys.stdin.read(); print('{\"selected_skills\":[]}' if '\"required\": [\"selected_skills\"]' in p else '{\"answer\":\"README.md updated\"}')"
        run["adapter"] = [sys.executable, "-c", fake]
        _, records = runner["run_fixture_evals"](repo_root=root, cases=[case], skill_dirs=skills,
            run=run, judge=judge, timeout=10, repetitions=1, judge_repetitions=1, pricing={})
        assert all(not item["passed"] and item["diff_errors"] for item in records)
        assert all(not item["evidence"]["trace_available"] for item in records)
        assert runner["check_required_diff"]({"required_diff": {"paths": ["README.md"]}},
            "--- a/other\n+++ b/other\n++++ b/README.md\n", ["other"])
        for invalid in ({"selected_skills": "audit"}, {"selected_skills": ["missing"]}, {"selected_skills": ["audit", "audit"]}, {"selected_skill": "audit"}):
            try:
                runner["validate_selected_skills"](invalid, skills)
            except RuntimeError:
                pass
            else:
                raise AssertionError(invalid)
        for unsafe in ("../escape", "/absolute"):
            try:
                runner["checked_relative_path"](unsafe)
            except RuntimeError:
                pass
            else:
                raise AssertionError(unsafe)
        (fixture / "link").symlink_to(root / "oracle.json")
        try:
            runner["prepare_trial"](root / "unsafe", skills, fixture)
        except RuntimeError as error:
            assert "символическую ссылку" in str(error)
        else:
            raise AssertionError("Ссылка не должна копировать оракул в рабочую среду")


def test_cost_accounting() -> None:
    estimate = runner["estimate_metrics"]
    price = {"model": {"input_per_million": 1, "output_per_million": 2, "currency": "USD"}}
    unknown = estimate("abcd", "abcd", 1, {}, "model")
    assert unknown["cost"] is None and unknown["estimated_cost"] is None
    assert unknown["input_tokens"] is None and unknown["estimated_input_tokens"] == 1
    assert unknown["elapsed_seconds"] == 1 and unknown["reported_elapsed_seconds"] is None
    reported = estimate("", "", 1, price, "model", {"input_tokens": 100, "output_tokens": 200})
    assert reported["estimated_cost"] == 0.0005 and reported["cost"] is None
    assert reported["estimate_scope"] == "reported_tokens"
    visible = estimate("abcd", "abcdefgh", 1, price, "model")
    assert visible["estimated_cost"] == 0.000005 and visible["estimate_scope"] == "visible_text_only"
    assert estimate("abcd", "", 1, price, "model", completed=False)["estimated_cost"] is None
    assert estimate("", "", 1, price, "model", {"cost": 0, "currency": "USD"})["cost"] == 0
    assert estimate("", "", 1, price, "model", {"cost": 3})["cost"] == 3
    for missing in ("input_per_million", "output_per_million", "currency"):
        partial = {"model": {key: value for key, value in price["model"].items() if key != missing}}
        assert estimate("a", "b", 1, partial, "model")["estimated_cost"] is None
    free = {"model": {"input_per_million": 0, "output_per_million": 0, "currency": "USD"}}
    assert estimate("a", "b", 1, free, "model")["estimated_cost"] == 0
    for value in (True, False, -1, float("nan"), float("inf"), "1"):
        for field in ("input_tokens", "output_tokens", "cost", "elapsed_seconds"):
            metrics = estimate("a", "b", 1, {}, "model", {field: value})
            assert field in metrics["invalid_fields"]
            assert metrics["reported_elapsed_seconds" if field == "elapsed_seconds" else field] is None
            json.dumps(metrics, allow_nan=False)
        for field in ("input_per_million", "output_per_million"):
            metrics = estimate("a", "b", 1, {"model": {**price["model"], field: value}}, "model")
            assert "pricing." + field in metrics["invalid_fields"] and metrics["estimated_cost"] is None
    assert "input_tokens" in estimate("", "", 1, {}, "model", {"input_tokens": 1.5})["invalid_fields"]

    ledger = []
    def invoke(output, usage=None, exit_code=0, delay=0):
        payload = json.dumps({"output": output, "usage": usage}) if usage is not None else output
        script = f"import sys,time; sys.stdin.read(); print({payload!r},flush=True); print('diagnostic',file=sys.stderr,flush=True); time.sleep({delay}); sys.exit({exit_code})"
        call = runner["make_model_call"]([sys.executable, "-c", script], "model", 0.2 if delay else 10,
            call_records=ledger, pricing=price, context={"phase": "test"})
        try:
            call("abcd", runner["FIXTURE_ANSWER_SCHEMA"])
        except RuntimeError:
            assert ledger[-1]["status"] == "failed"
        assert call.last_call_id == ledger[-1]["id"]
        assert ledger[-1]["context"]["phase"] == "test"
        assert "diagnostic" in ledger[-1]["stderr"]
        return ledger[-1]
    parsed_failure = invoke("not-json", {"cost": 3, "currency": "USD"})
    assert parsed_failure["status"] == "failed" and parsed_failure["metrics"]["cost"] == 3
    transport_failure = invoke('{"answer":"done"}', {"cost": 4, "currency": "USD"}, exit_code=7)
    assert transport_failure["returncode"] == 7 and transport_failure["metrics"]["cost"] == 4
    timeout = invoke('{"answer":"done"}', delay=2)
    assert timeout["status"] == "failed" and timeout["metrics"]["estimated_cost"] is None
    timed_usage = invoke('{"answer":"done"}', {"cost": 5, "currency": "USD"}, delay=2)
    assert timed_usage["metrics"]["cost"] == 5 and timed_usage["status"] == "failed"
    no_usage = invoke("not-json")
    assert no_usage["metrics"]["cost"] is None and no_usage["metrics"]["estimated_cost"] is None
    actual = invoke('{"answer":"done"}', {"cost": 0, "currency": "USD"})
    no_currency = invoke('{"answer":"done"}', {"cost": 2})
    other_currency = invoke('{"answer":"done"}', {"cost": 2, "currency": "EUR"})
    estimated = invoke('{"answer":"done"}')
    assert estimated["metrics"]["estimated_input_tokens"] > 1  # Учтено дополнение со схемой.
    summary = runner["summarize_calls"](ledger)
    assert summary["calls"] == 9 and summary["failed_calls"] == 5
    assert summary["unknown_cost_calls"] == 2 and summary["unknown_currency_calls"] == 1
    assert summary["by_currency"]["USD"]["cost"] == 12 and summary["by_currency"]["EUR"]["cost"] == 2
    assert summary["total_cost"] is None
    assert runner["summarize_calls"]([actual])["total_cost"] == 0
    assert runner["summarize_calls"]([no_currency])["total_cost"] is None
    assert runner["summarize_calls"]([estimated])["total_cost"] is None
    assert runner["summarize_calls"]([])["total_cost"] is None


def test_result_workspace() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        skills = sample_skills(root)
        case = {"id": "result-case", "prompt": "Обнови файл", "input_files": [
            {"path": "input.txt", "content": "original"}, {"path": "unknown.txt", "purpose": "неизвестный вход"}],
            "oracle": {"required_diff": {"paths": ["input.txt"]}}}
        data = {"skill_name": "audit", "cases": [case]}
        def factory(workspace: Path, read_only: bool = False):
            assert read_only is False
            def call(prompt, schema):
                assert "как будто применение навыка уже выполнено" not in prompt
                assert (workspace / "input.txt").read_text() == "original"
                assert not (workspace / "unknown.txt").exists()
                assert (workspace / ".agents/skills/writing/references/rules.md").is_file()
                (workspace / "input.txt").write_text("changed")
                return {"answers": [{"id": "result-case", "answer": "готово"}]}
            return call
        def judge(prompt, schema):
            assert '"workspace_diff"' in prompt and "changed" in prompt
            assert '"unspecified_inputs"' in prompt
            return {"results": [{"id": "result-case", "passed": True}]}
        records = []
        errors = runner["run_result_evals"](repo_root=root, groups=[(skills[0], data, [case])],
            call_factory=factory, judge_call=judge, skill_dirs=skills, records=records, model_label="candidate")
        assert not errors and records[0]["passed"]
        assert records[0]["evidence"]["unspecified_inputs"] == ["unknown.txt"]
        # Проверяем общий журнал через реальные процессы, а не только функции-имитаторы.
        candidate_code = "import json; from pathlib import Path; Path('input.txt').write_text('changed'); print(json.dumps({'output':'готово','usage':{'cost':1,'currency':'USD'}}))"
        judge_code = "import json; print(json.dumps({'output':json.dumps({'results':[{'id':'result-case','passed':True}]}),'usage':{'cost':2,'currency':'USD'}}))"
        calls, records = [], []
        errors = runner["run_for_target"](repo_root=root,
            run={"adapter": [sys.executable, "-c", candidate_code], "model": "candidate", "label": "candidate"},
            judge={"adapter": [sys.executable, "-c", judge_code], "model": "judge", "label": "judge"},
            timeout=10, trigger_cases=[], result_groups=[(skills[0], data, [case])], skill_dirs=skills,
            result_records=records, call_records=calls)
        assert not errors and records[0]["call_ids"] == [1, 2]
        assert [call["context"]["role"] for call in calls] == ["candidate", "judge"]
        assert all(call["context"]["case_id"] == "result-case" for call in calls)
        assert runner["summarize_calls"](calls)["total_cost"] == 3


def test_adapter_traces() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        fake_bin = root / "bin"
        fake_bin.mkdir()
        for name in ("codex", "claude"):
            cli = fake_bin / name
            cli.write_text(f'''#!{sys.executable}
import json, sys
from pathlib import Path
sys.stdin.read()
if "--output-last-message" in sys.argv:
    Path(sys.argv[sys.argv.index("--output-last-message") + 1]).write_text('{{"answer":"done"}}')
    print(json.dumps({{"type":"item.completed","item":{{"type":"command_execution","exit_code":0}}}}))
else:
    assert "stream-json" in sys.argv
    print(json.dumps({{"type":"assistant","message":{{"content":[{{"type":"tool_use","name":"Read"}}]}}}}))
    print(json.dumps({{"type":"result","result":'{{"answer":"done"}}',"is_error":False}}))
''')
            cli.chmod(0o755)
        old_path = os.environ["PATH"]
        os.environ["PATH"] = str(fake_bin) + os.pathsep + old_path
        try:
            for name in ("codex", "claude"):
                call = runner["make_model_call"](["bash", str(RUNNER.parent / "adapters" / name)], "fixture-model", 10, workspace)
                assert call("task", runner["FIXTURE_ANSWER_SCHEMA"])["answer"] == "done"
                assert call.last_execution["trace"]
                assert call.last_execution["returncode"] == 0
        finally:
            os.environ["PATH"] = old_path
        missing_trace = runner["make_model_call"]([sys.executable, "-c",
            "import os; os.unlink(os.environ['APM_EVAL_TRACE']); print('{\"answer\":\"done\"}')"],
            "fixture-model", 10, workspace)
        assert missing_trace("task", runner["FIXTURE_ANSWER_SCHEMA"])["answer"] == "done"
        assert missing_trace.last_execution["trace_error"]
        assert not missing_trace.last_execution["trace"]
        if hasattr(os, "mkfifo"):
            fifo_trace = runner["make_model_call"]([sys.executable, "-c",
                "import os; p=os.environ['APM_EVAL_TRACE']; os.unlink(p); os.mkfifo(p); print('{\"answer\":\"done\"}')"],
                "fixture-model", 10, workspace)
            assert fifo_trace("task", runner["FIXTURE_ANSWER_SCHEMA"])["answer"] == "done"
            assert fifo_trace.last_execution["trace_error"]


def test_command_line() -> None:
    """Поставленная команда: выбор сценария не скрывает соседние навыки."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        sample_skills(root)
        (root / ".apm").mkdir()
        (root / "packages").rename(root / ".apm/skills")
        fixture = root / "evals/fixtures/basic"
        fixture.mkdir(parents=True)
        (fixture / "README.md").write_text("original")
        (root / "evals/oracle.json").write_text(json.dumps({"success_criteria": ["updated"],
            "required_diff": {"paths": ["README.md"]}}))
        (root / "evals/fixtures/registry.json").write_text(json.dumps({"cases": [
            {"id": "task", "prompt": "Обнови README", "target_skill": "audit", "fixture": "basic", "oracle": "../oracle.json"},
            {"id": "other-task", "prompt": "Не выполнять", "target_skill": "writing", "fixture": "basic", "oracle": "../oracle.json"}]}))
        tools_dir = root / "tools"
        tools_dir.mkdir()
        adapter = tools_dir / "adapter.py"
        adapter.write_text('''import json, os, sys
from pathlib import Path
prompt = sys.stdin.read()
if sys.argv[-1] == "judge":
    assert "APM_EVAL_WORKSPACE" not in os.environ
    print(json.dumps({"results":[{"id":"task","passed":True}]}))
elif '"required": ["selected_skills"]' in prompt:
    print(json.dumps({"selected_skills":[]}))
else:
    root = Path(os.environ["APM_EVAL_WORKSPACE"])
    assert Path.cwd() == root
    if (root / ".agents").exists():
        assert (root / ".agents/skills/writing/references/rules.md").is_file()
    (root / "README.md").write_text("updated")
    print(json.dumps({"answer":"done"}))
''')
        config = root / "evals.local.yml"
        template = f'''adapters:
  local: "{sys.executable} tools/adapter.py"
models:
  - local:candidate
workspace_models: []
judge: local:judge
repetitions: 1
judge_repetitions: 1
results_dir: eval-results
'''
        config.write_text(template)
        env = {key: value for key, value in os.environ.items() if not key.startswith("APM_EVAL_")}
        env["APM_EVAL_PATH"] = ".apm/skills/audit"
        command = [sys.executable, str(RUNNER), "--yes", "--output", "report.json"]
        stopped = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True)
        assert stopped.returncode == 1 and "workspace_models" in stopped.stderr
        assert not (root / "report.json").exists()
        config.write_text(template.replace("workspace_models: []", "workspace_models:\n  - local:candidate"))
        done = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True)
        assert done.returncode == 0, done.stdout + done.stderr
        report = json.loads((root / "report.json").read_text())
        assert len(report["runs"]) == 3
        assert all(item["case_id"] == "task" and item["passed"] for item in report["runs"])
        assert set(report["provenance"]["skills"]) == {".apm/skills/audit", ".apm/skills/writing"}
        assert (fixture / "README.md").read_text() == "original"
        assert report["accounting"]["calls"] == 7
        assert report["accounting"]["unknown_cost_calls"] == 7
        assert report["accounting"]["total_cost"] is None

        trigger_dir = root / ".apm/skills/audit/evals"
        trigger_dir.mkdir()
        (trigger_dir / "triggers.json").write_text(json.dumps({"skill_name": "audit", "cases": [
            {"id": name, "prompt": "Проверь", "should_trigger": True, "rationale": "пример"}
            for name in ("trigger-one", "trigger-two")]}))
        adapter.write_text('''import json, sys
p = sys.stdin.read()
case = "trigger-one" if '"id": "trigger-one"' in p else "trigger-two"
print(json.dumps({"output":json.dumps({"results":[{"id":case,"should_trigger":True}]}),"usage":{"cost":1,"currency":"USD"}}))
''')
        command.extend(["--case-id", "trigger-one", "--case-id", "trigger-two"])
        done = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True)
        assert done.returncode == 0, done.stdout + done.stderr
        report = json.loads((root / "report.json").read_text())
        assert not report["runs"] and not report["result_runs"]
        assert report["accounting"]["calls"] == 2 and report["accounting"]["total_cost"] == 2
        assert [call["context"]["attempt"] for call in report["calls"]] == [1, 2]
        assert report["calls"][1]["context"]["case_ids"] == ["trigger-two"]
        # Отчёт сохраняется и при ошибке ответа, и при неожиданной ошибке обработки результата.
        for output in ("not-json", '{"results":null}'):
            adapter.write_text(f"import json; print(json.dumps({{'output':{output!r},'usage':{{'cost':3,'currency':'USD'}}}}))")
            done = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True)
            assert done.returncode != 0
            report = json.loads((root / "report.json").read_text())
            assert report["accounting"]["calls"] == 1 and report["accounting"]["total_cost"] == 3
            if output == "not-json":
                assert report["calls"][0]["status"] == "failed"


def test_comparison() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        skills = sample_skills(root)
        folder = root / "evals/comparison"
        folder.mkdir(parents=True)
        fixture = folder / "fixture"
        fixture.mkdir()
        (fixture / "README.md").write_text("original")
        (fixture / "AGENTS.md").write_text("COMMONRULE")
        (folder / "minimal.md").write_text("MINIMALONLY")
        (folder / "oracle.json").write_text(json.dumps({"success_criteria": ["README updated"],
            "required_diff": {"paths": ["README.md"]}}))
        registry = {"cases": [{"id": "task", "prompt": "Обнови README", "fixture": "fixture", "oracle": "oracle.json"},
                               {"id": "unused", "prompt": "Не выполнять", "fixture": "fixture", "oracle": "oracle.json"}]}
        (folder / "registry.json").write_text(json.dumps(registry))
        plan = {"schema_version": 1, "id": "example-comparison", "question": "Как меняется результат?",
                "common_background": "COMMONRULE и одинаковая оснастка", "collection": "../../packages",
                "fixture_registry": "registry.json", "minimal_instructions": "minimal.md", "case_ids": ["task"], "seed": 37}
        plan_path = folder / "plan.json"
        plan_path.write_text(json.dumps(plan))
        marker = root / "calls.log"
        adapter = root / "adapter.py"
        adapter.write_text(f'''import json, os, sys
from pathlib import Path
p = sys.stdin.read()
marker = Path({str(marker)!r})
with marker.open("a") as log:
    log.write(sys.argv[-1] + "\\n")
if sys.argv[-1] == "judge":
    assert "APM_EVAL_WORKSPACE" not in os.environ
    data, _ = json.JSONDecoder().raw_decode(p.split("Данные:\\n", 1)[1])
    assert "mode" not in data and "condition" not in data
    assert data["oracle"]["success_criteria"] == ["README updated"]
    result = {{"results":[{{"id":"task","passed":data["answer"] == "good"}}]}}
    price = 2
else:
    workspace = Path(os.environ["APM_EVAL_WORKSPACE"])
    assert Path.cwd() == workspace
    assert (workspace / "README.md").read_text() == "original"
    assert (workspace / "AGENTS.md").read_text() == "COMMONRULE"
    assert not (workspace / "oracle.json").exists() and not (workspace / "plan.json").exists()
    assert '"required": ["selected_skills"]' not in p and "Начальный выбор:" not in p
    minimal = "MINIMALONLY" in p
    collection = (workspace / ".agents/skills").exists()
    assert collection == (workspace / ".claude/skills").exists()
    if collection:
        assert minimal
        for name in ("audit", "writing"):
            assert (workspace / f".agents/skills/{{name}}/references/rules.md").read_text() == "complete-reference"
            assert (workspace / f".claude/skills/{{name}}/assets/data.bin").read_bytes() == b"\\xff\\x00"
            assert os.access(workspace / f".agents/skills/{{name}}/scripts/check", os.X_OK)
    condition = "collection" if collection else "minimal" if minimal else "ordinary"
    (workspace / "README.md").write_text("updated")
    Path(os.environ["APM_EVAL_TRACE"]).write_text(json.dumps({{"condition":condition,"prompt":p,"workspace":str(workspace)}}))
    result = {{"answer":"good" if not collection and (minimal or sys.argv[-1] == "candidate-a") else "bad", "selected_skills":[]}}
    # Изменение оригиналов после старта не должно менять следующие условия.
    Path({str(folder / 'minimal.md')!r}).write_text("CHANGED AFTER START")
    Path({str(fixture / 'README.md')!r}).write_text("CHANGED AFTER START")
    price = 1
print(json.dumps({{"output":json.dumps(result),"usage":{{"cost":price,"currency":"USD"}}}}))
''')
        config_path = root / "evals.local.yml"
        config_path.write_text(f'''adapters:
  local: "{sys.executable} adapter.py"
models:
  - local:candidate-a
  - local:candidate-b
workspace_models:
  - local:candidate-a
  - local:candidate-b
judge: local:judge
repetitions: 2
judge_repetitions: 1
timeout: 10
results_dir: eval-results
''')
        env = {key: value for key, value in os.environ.items() if not key.startswith("APM_EVAL_")}
        command = [sys.executable, str(RUNNER), "--comparison-plan", "evals/comparison/plan.json", "--output", "report.json", "--yes"]
        done = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True)
        assert done.returncode == 0, done.stdout + done.stderr
        report = json.loads((root / "report.json").read_text())
        assert report["schema_version"] == 3 and report["suite"] == "comparison" and not report["summary"]
        assert len(report["runs"]) == 12 and len(report["calls"]) == 24 and not report["result_runs"]
        assert report["accounting"]["total_cost"] == 36
        assert report["comparison"]["execution_status"] == "completed"
        assert report["comparison"]["minimal_instructions"]["text"] == "MINIMALONLY"
        assert report["comparison"]["plan"] == plan
        assert report["provenance"]["fixtures"]["task"]["prompt"] == "Обнови README"
        assert "assets/data.bin" in report["provenance"]["skills"]["packages/audit"]
        assert report["provenance"]["modes"] == ["ordinary", "minimal", "collection"]
        conditions_a = report["comparison_summary"]["local:candidate-a"]["conditions"]
        conditions_b = report["comparison_summary"]["local:candidate-b"]["conditions"]
        assert conditions_a["ordinary"]["pass_rate"] == 1 and conditions_b["ordinary"]["pass_rate"] == 0
        assert conditions_a["collection"]["delta_to_ordinary"] == -1
        assert conditions_b["collection"]["delta_to_ordinary"] == 0 and conditions_b["collection"]["delta_to_minimal"] == -1
        prompts, paths = {}, []
        for trial in report["runs"]:
            trace = json.loads(trial["evidence"]["execution"][0]["trace"])
            assert trace["condition"] == trial["condition"] and trial["case_id"] == "task"
            prompts.setdefault(trial["condition"], set()).add(trace["prompt"])
            paths.append(trace["workspace"])
            assert len(trial["call_ids"]) == 2
            assert trial["routing"]["expected_skill"] is None and not trial["routing"]["affects_task_pass"]
        assert len(set(paths)) == 12 and all(not Path(path).exists() for path in paths)
        assert prompts["minimal"] == prompts["collection"] and len(prompts["ordinary"]) == 1
        order = [{"case_id": trial["case_id"], "condition": trial["condition"], "repetition": trial["repetition"]} for trial in report["runs"]]
        assert order[:6] == order[6:] == report["comparison"]["order"]
        assert len({condition for block in runner["trial_blocks"]("task", 12, {"plan": plan}) for condition, _ in block[:1]}) > 1

        # Ошибки подготовки не вызывают модели и не перезаписывают готовый отчёт.
        initial_marker, initial_report = marker.read_bytes(), (root / "report.json").read_bytes()
        original_config = config_path.read_text()
        for contents in (None, "models: []\n", "models:\n    - unsupported-indent\n"):
            if contents is None:
                config_path.unlink()
            else:
                config_path.write_text(contents)
            failure = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True)
            assert failure.returncode == 1 and marker.read_bytes() == initial_marker
            assert (root / "report.json").read_bytes() == initial_report
        config_path.write_text(original_config)
        for delta in ({"case_ids": ["unknown"]}, {"case_ids": ["task", "task"]}, {"seed": True},
                      {"common_background": ""}, {"schema_version": True}, {"collection": "../../packages/audit"},
                      {"minimal_instructions": "../../../../outside.md"}):
            plan_path.write_text(json.dumps({**plan, **delta}))
            failure = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True)
            assert failure.returncode == 1, delta
            assert marker.read_bytes() == initial_marker and (root / "report.json").read_bytes() == initial_report
        plan_path.write_text(json.dumps(plan))
        for extra in (["--case-id", "task"], ["--repetitions", "1"], ["packages"], ["--limit", "1"]):
            failure = subprocess.run([*command, *extra], cwd=root, env=env, text=True, capture_output=True)
            assert failure.returncode == 1 and marker.read_bytes() == initial_marker
        (folder / "minimal.md").write_text("")
        failure = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True)
        assert failure.returncode == 1 and marker.read_bytes() == initial_marker
        (folder / "minimal.md").write_text("MINIMALONLY")
        (fixture / "README.md").write_text("original")
        config = runner["load_config"](root, config_path)
        with tempfile.TemporaryDirectory() as frozen:
            metadata, cases, frozen_skills = runner["freeze_comparison"](root, plan_path, config, Path(frozen))
            runner["check_comparison_snapshot"](metadata, cases, frozen_skills)
            # Прерывание следующего условия сохраняет уже завершённую задачу.
            scope = runner["run_fixture_evals"].__globals__
            original_check = scope["check_comparison_snapshot"]
            checked = []
            def interrupt_after_first(*args):
                checked.append(True)
                if len(checked) > 2:
                    raise RuntimeError("Остановлено тестом")
                return original_check(*args)
            preserved, calls = [], []
            scope["check_comparison_snapshot"] = interrupt_after_first
            try:
                runner["run_fixture_evals"](repo_root=root, cases=cases, skill_dirs=frozen_skills,
                    run=config["runs"][0], judge=config["judge"], timeout=10, repetitions=2,
                    judge_repetitions=1, pricing={}, comparison=metadata, call_records=calls, record_sink=preserved)
            except RuntimeError as error:
                assert "Остановлено тестом" in str(error)
            else:
                raise AssertionError("Прерывание должно выйти из прогона")
            finally:
                scope["check_comparison_snapshot"] = original_check
            assert len(preserved) == 1 and len(calls) == 2
            summary = runner["comparison_summary"](preserved, calls, metadata)
            assert all(bucket["pass_rate"] is None for result in summary.values() for bucket in result["conditions"].values())
            (cases[0]["fixture_dir"] / "README.md").write_text("tampered")
            try:
                runner["check_comparison_snapshot"](metadata, cases, frozen_skills)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Изменение зафиксированных входов должно остановить сравнение")
        adapter.write_text("print('not-json')")
        failure = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True)
        assert failure.returncode == 1
        failed_report = json.loads((root / "report.json").read_text())
        assert failed_report["comparison"]["execution_status"] == "completed_with_errors"
        assert len(failed_report["runs"]) == 12 and len(failed_report["calls"]) == 12
        assert all(call["status"] == "failed" for call in failed_report["calls"])


if __name__ == "__main__":
    raise SystemExit(main())
