#!/usr/bin/env python3
"""Уведомления о давности исполнения сценариев без вызова моделей."""
from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8", errors="replace")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def inventory(root, skill_dirs=None, registry=None):
    """Редакция каждого задания, включая общие поля и входы фикстуры."""
    revisions = {}

    def add(kind, case, inputs):
        key = f"{kind}:{case['id']}"
        if key in revisions:
            raise ValueError(f"Повтор сценария: {key}")
        revisions[key] = digest(inputs)

    dirs = skill_dirs if skill_dirs is not None else sorted((root / '.apm/skills').glob('*'))
    for skill in dirs:
        for kind, name in (("trigger", "triggers.json"), ("result", "result-scenarios.json")):
            path = skill / 'evals' / name
            if not path.is_file():
                continue
            data = read_json(path)
            common = {key: value for key, value in data.items() if key != 'cases'}
            for case in data['cases']:
                add(kind, case, [common, case])
    path = registry if registry is not None else root / 'evals/fixtures/registry.json'
    if not path.is_absolute():
        path = root / path
    if path.is_file():
        for case in read_json(path)['cases']:
            fixture = path.parent / case['fixture']
            if not fixture.is_dir():
                raise ValueError(f"Нет входной фикстуры: {fixture}")
            files = {file.relative_to(fixture).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
                     for file in sorted(fixture.rglob('*')) if file.is_file() and '.git' not in file.relative_to(fixture).parts}
            add('fixture', case, [case, read_json(path.parent / case['oracle']), files])
    return revisions


def executed_keys(report):
    if report.get('schema_version') in (None, 1, 2):
        for kind, field in (('fixture', 'runs'), ('result', 'result_runs')):
            for record in report.get(field, []):
                if record.get('case_id') and not record.get('candidate_error'):
                    yield f"{kind}:{record['case_id']}"
        return
    for call in report.get('calls', []):
        if call.get('status') != 'completed':
            continue
        context = call.get('context', {})
        if context.get('role') != 'candidate':
            continue
        phase = context.get('phase')
        if phase == 'trigger':
            for case_id in context.get('case_ids', []):
                yield f'trigger:{case_id}'
        elif phase in ('result', 'application') and context.get('case_id'):
            kind = 'result' if phase == 'result' else 'fixture'
            yield f"{kind}:{context['case_id']}"


def timestamp(value):
    result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('У даты отсутствует часовой пояс')
    return result


def assess(revisions, reports, now, days):
    history = {}
    for report in reports:
        if report.get('suite') == 'comparison':
            continue
        if report.get('schema_version') not in (None, 1, 2, 3):
            raise ValueError('Неизвестная версия отчёта модельного прогона')
        if report.get('schema_version') == 3 and not isinstance(report.get('calls'), list):
            raise ValueError('Отчёт версии 3 должен содержать calls')
        date = timestamp(report['generated_at'])
        if date > now:
            raise ValueError('Дата отчёта находится в будущем')
        snapshot = report.get('scenario_revisions', {}) if report.get('schema_version') == 3 else {}
        if not isinstance(snapshot, dict):
            raise ValueError('scenario_revisions должен быть объектом')
        for key in executed_keys(report):
            history.setdefault(key, []).append((date, snapshot.get(key)))
    rows = []
    for key, revision in sorted(revisions.items()):
        entries = history.get(key, [])
        matching = [date for date, old in entries if old == revision]
        latest = max(matching) if matching else None
        if latest:
            status = 'stale' if now - latest > dt.timedelta(days=days) else 'fresh'
        elif any(old is not None for _, old in entries):
            status = 'changed'
        elif entries:
            status = 'unknown_revision'
        else:
            status = 'unrecorded'
        rows.append({'scenario': key, 'status': status,
                     'last_matching_run': latest.isoformat() if latest else None})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('skills_path', nargs='?', type=Path, default=Path('.apm/skills'))
    parser.add_argument('--fixture-registry', type=Path, default=Path('evals/fixtures/registry.json'))
    parser.add_argument('--reports', type=Path, default=Path('eval-results'))
    parser.add_argument('--max-age-days', type=int, default=30)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    if args.max_age_days < 0:
        parser.error('Порог давности должен быть неотрицательным')
    try:
        if not args.skills_path.is_dir():
            raise ValueError(f'Нет каталога навыков: {args.skills_path}')
        dirs = [args.skills_path] if (args.skills_path / 'SKILL.md').is_file() else sorted(args.skills_path.glob('*'))
        revisions = inventory(Path.cwd(), dirs, args.fixture_registry)
        paths = [args.reports] if args.reports.is_file() else sorted(args.reports.glob('*.json'))
        reports = []
        for path in paths:
            try:
                data = read_json(path)
                if not isinstance(data, dict):
                    raise ValueError('Ожидается объект отчёта')
                if 'schema_version' not in data and not all(key in data for key in ('generated_at', 'provenance', 'runs')):
                    if args.reports.is_file():
                        raise ValueError('Файл не является отчётом модельного прогона')
                    print(f'Пропущен документ другого вида: {path}', file=sys.stderr)
                    continue
                reports.append(data)
            except (OSError, ValueError) as error:
                raise ValueError(f'{path}: {error}') from error
        rows = assess(revisions, reports, dt.datetime.now(dt.timezone.utc), args.max_age_days)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(f'Проверка давности не завершена: {error}', file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        labels = {'unrecorded': 'нет зарегистрированного исполнения', 'stale': 'давнее исполнение',
                  'fresh': 'свежее исполнение', 'changed': 'задание изменилось',
                  'unknown_revision': 'редакция прежнего задания неизвестна'}
        for row in rows:
            if row['status'] != 'fresh':
                print(f"{row['scenario']}: {labels[row['status']]}")
        counts = Counter(row['status'] for row in rows)
        print('Давность сценариев: ' + ', '.join(f'{label}: {counts[key]}' for key, label in labels.items()))
        print('Учтены завершённые вызовы кандидата. Свежесть не подтверждает успех или пользу навыка.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
