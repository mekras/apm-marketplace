#!/usr/bin/env python3
"""Регрессии уведомлений об исполнении сценариев."""
import datetime as dt
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import os

import eval_freshness as freshness

NOW = dt.datetime(2026, 11, 11, tzinfo=dt.timezone.utc)


def report(revision='a', age=0, phase='result', status='completed'):
    return {'schema_version': 3, 'generated_at': (NOW-dt.timedelta(days=age)).isoformat(),
            'scenario_revisions': {'result:one': revision},
            'calls': [{'status': status, 'context': {'role': 'candidate', 'phase': phase, 'case_id': 'one'}}]}


class FreshnessTests(unittest.TestCase):
    def status(self, reports):
        return freshness.assess({'result:one': 'a'}, reports, NOW, 30)[0]['status']

    def test_required_states(self):
        self.assertEqual(self.status([]), 'unrecorded')
        self.assertEqual(self.status([report(age=31)]), 'stale')
        self.assertEqual(self.status([report(age=30)]), 'fresh')
        self.assertEqual(self.status([report()]), 'fresh')
        self.assertEqual(self.status([report(revision='b')]), 'changed')

    def test_legacy_report_has_unknown_revision(self):
        data = {'generated_at': NOW.isoformat(), 'provenance': {},
                'runs': [{'case_id':'one','candidate_error':''}]}
        row = freshness.assess({'fixture:one':'a'}, [data], NOW, 30)[0]
        self.assertEqual(row['status'], 'unknown_revision')

    def test_unknown_revision(self):
        old = report()
        old.pop('scenario_revisions')
        self.assertEqual(self.status([old]), 'unknown_revision')

    def test_partial_and_failed_calls(self):
        for phase, state in [('selection','completed'), ('result','started'), ('result','failed')]:
            self.assertEqual(self.status([report(phase=phase,status=state)]), 'unrecorded')
        unused = report()
        unused['calls'] = []
        self.assertEqual(self.status([unused]), 'unrecorded')

    def test_history_uses_latest_matching_revision(self):
        self.assertEqual(self.status([report(age=40),report(age=2,revision='b')]), 'stale')
        self.assertEqual(self.status([report(age=40),report(age=2)]), 'fresh')

    def test_wrong_dates_and_schema(self):
        for data in [report(age=-1), {'schema_version': 4}, {**report(), 'generated_at':'bad'}]:
            with self.assertRaises((ValueError,KeyError)):
                self.status([data])

    def test_kinds_do_not_collide(self):
        data = report(phase='application')
        data['scenario_revisions']['fixture:one'] = 'b'
        rows = freshness.assess({'result:one':'a','fixture:one':'b'}, [data], NOW,30)
        self.assertEqual([row['status'] for row in rows], ['fresh','unrecorded'])
        trigger = report(phase='trigger')
        trigger['calls'][0]['context']['case_ids'] = ['one']
        trigger['scenario_revisions'] = {'trigger:one':'a'}
        self.assertEqual(freshness.assess({'trigger:one':'a'},[trigger],NOW,30)[0]['status'],'fresh')

    def test_inventory_changes_only_affected_case(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = root/'.apm/skills/test'
            (skill/'evals').mkdir(parents=True)
            path = skill/'evals/result-scenarios.json'
            data = {'skill_name':'test','cases':[{'id':'one','prompt':'old'},{'id':'two','prompt':'same'}]}
            path.write_text(json.dumps(data), encoding="utf-8")
            before = freshness.inventory(root)
            data['cases'][0]['prompt'] = 'new'
            path.write_text(json.dumps(data), encoding="utf-8")
            after = freshness.inventory(root)
            self.assertNotEqual(before['result:one'],after['result:one'])
            self.assertEqual(before['result:two'],after['result:two'])

    def test_fixture_prompt_oracle_and_input_revision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root/'evals/fixtures'
            (base/'case').mkdir(parents=True)
            (base/'case/input').write_text('before', encoding="utf-8")
            (base/'oracle.json').write_text('{"success": true}', encoding="utf-8")
            registry = base/'registry.json'
            data = {'cases':[{'id':'one','prompt':'task','fixture':'case','oracle':'oracle.json'}]}
            registry.write_text(json.dumps(data), encoding="utf-8")
            before = freshness.inventory(root)
            for path, value in [(base/'case/input','after'), (base/'oracle.json','{}')]:
                path.write_text(value, encoding="utf-8")
                after = freshness.inventory(root)
                self.assertNotEqual(before,after)
                before = after
            data['cases'][0]['prompt']='changed'
            registry.write_text(json.dumps(data), encoding="utf-8")
            self.assertNotEqual(before,freshness.inventory(root))

    def test_cli_no_reports_and_corrupt_report(self):
        script = Path(freshness.__file__).resolve()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'.apm/skills').mkdir(parents=True)
            result = subprocess.run([sys.executable,str(script)],cwd=root,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            (root/'eval-results').mkdir()
            (root/'eval-results/bad.json').write_text('{', encoding="utf-8")
            result = subprocess.run([sys.executable,str(script)],cwd=root,capture_output=True)
            self.assertEqual(result.returncode,2)

    def test_comparison_does_not_count_as_regression(self):
        data = report()
        data['suite'] = 'comparison'
        self.assertEqual(self.status([data]), 'unrecorded')

    def test_cli_real_scenario_with_custom_history(self):
        script = Path(freshness.__file__).resolve()
        with tempfile.TemporaryDirectory(prefix='Проверка с пробелом ') as temp:
            root = Path(temp)
            skill = root/'.apm/skills/example'
            (skill/'evals').mkdir(parents=True)
            (skill/'SKILL.md').write_text('example', encoding='utf-8')
            scenario = skill/'evals/result-scenarios.json'
            scenario.write_text(json.dumps({'cases':[{'id':'one','prompt':'task'}]}), encoding='utf-8')
            data = report()
            data['generated_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
            data['scenario_revisions'] = freshness.inventory(root)
            history = root/'history.json'
            history.write_text(json.dumps(data), encoding='utf-8')
            result = subprocess.run([sys.executable,str(script),'--reports',str(history),'--json'],
                                    cwd=root,capture_output=True,encoding='utf-8')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)[0]['status'],'fresh')

    def test_main_captures_revision_before_execution(self):
        spec = importlib.util.spec_from_file_location('runner',Path(__file__).with_name('run-skill-evals.py'))
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = root/'.apm/skills/example'
            (skill/'evals').mkdir(parents=True)
            (skill/'SKILL.md').write_text('---\nname: example\ndescription: example\n---\n',encoding='utf-8')
            scenario = skill/'evals/result-scenarios.json'
            data = {'skill_name':'example','cases':[{'id':'one','prompt':'before'}]}
            scenario.write_text(json.dumps(data),encoding='utf-8')
            before = freshness.inventory(root)
            args = mock.Mock(paths=[],case_id=[],limit=0,config=root/'config',output=root/'report.json',
                             fixture_registry=Path('evals/fixtures/registry.json'),comparison_plan=None,
                             repetitions=None,yes=True,result_rule_delivery='available')
            config = {'runs':[{'label':'fake','workspace':True}], 'judge':{},'timeout':1,
                      'pricing':{},'repetitions':1,'judge_repetitions':1,'results_dir':'history'}
            def execute(**kwargs):
                data['cases'][0]['prompt']='after'
                scenario.write_text(json.dumps(data),encoding='utf-8')
                call = report()['calls'][0]
                call.update(id=1, metrics={'cost':None,'currency':None,'estimated_cost':None,
                                          'estimated_currency':None,'elapsed_seconds':0})
                kwargs['call_records'].append(call)
                return []
            previous = Path.cwd()
            try:
                os.chdir(root)
                with mock.patch.object(runner,'parse_args',return_value=args), \
                     mock.patch.object(runner,'load_config',return_value=config), \
                     mock.patch.object(runner,'verify_evaluation_storage'), \
                     mock.patch.object(runner,'confirm_model_run',return_value=True), \
                     mock.patch.object(runner,'run_for_target',side_effect=execute):
                    self.assertEqual(runner.main(),0)
                saved = json.loads(args.output.read_text(encoding='utf-8'))
                self.assertEqual(saved['scenario_revisions'],before)
                self.assertNotEqual(saved['scenario_revisions'],freshness.inventory(root))
            finally:
                os.chdir(previous)

    def test_runner_serializes_supplied_snapshot(self):
        spec = importlib.util.spec_from_file_location('runner',Path(__file__).with_name('run-skill-evals.py'))
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            path=runner.write_fixture_report(root,root/'report.json',[],[],[],1,1,
                                            scenario_revisions={'result:one':'before'})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))['scenario_revisions'],{'result:one':'before'})


if __name__ == '__main__':
    unittest.main()
