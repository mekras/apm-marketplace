"""Deterministic vectors for protocol tests, not a semantic model."""
import json
import sys
import time

request = json.load(sys.stdin)
fault = sys.argv[1] if len(sys.argv) > 1 else ''
if fault == 'timeout':
    time.sleep(3)
if fault == 'failure':
    print('private-adapter-error-content', file=sys.stderr)
    raise SystemExit(1)
if fault == 'badjson':
    print('not json')
    raise SystemExit(0)
rows = []
for entry in request['inputs']:
    text = entry['text'].casefold()
    vector = [1.0, 0.0, 0.0] if any(word in text for word in ('наставника', 'помощник')) else [0.0, 1.0, 0.0]
    if fault == 'zero': vector = [0.0, 0.0, 0.0]
    if fault == 'nan': vector = [float('nan'), 1.0, 0.0]
    if fault == 'dimension' and entry['id'] != '0': vector = [1.0, 0.0]
    rows.append({'id': '0' if fault == 'duplicate' else entry['id'], 'values': vector})
if fault == 'missing': rows = []
response = {'embedding_protocol_version': 1, 'model': request['model'],
            'revision': 'wrong' if fault == 'revision' else request['revision'],
            'embeddings': list(reversed(rows))}
json.dump(response, sys.stdout)
