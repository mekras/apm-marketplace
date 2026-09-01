#!/usr/bin/env python3
"""Регрессии запускателя скрытых предусловий fixture-evals."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "tools/run-fixture-checks.py"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    (root / "fixtures/sample").mkdir(parents=True)
    (root / "fixtures/sample/input.txt").write_text("fixture", encoding="utf-8")
    write_json(
        root / "fixtures/registry.json",
        {
            "version": 1,
            "cases": [
                {
                    "id": "sample",
                    "fixture": "sample",
                    "oracle": "../oracles/sample.json",
                }
            ],
        },
    )
    write_json(
        root / "oracles/sample.json",
        {
            "fixture_checks": [
                {
                    "command": [
                        sys.executable,
                        "-c",
                        "import json; from pathlib import Path; Path('state.json').write_text(json.dumps({'ok': True})); print('done')",
                    ],
                    "exit_code": 0,
                    "stdout_contains": "done",
                    "json_file": "state.json",
                    "json_equals": {"ok": True},
                }
            ]
        },
    )
    completed = subprocess.run(
        [sys.executable, str(RUNNER), str(root / "fixtures/registry.json")],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Воспроизведено скрытых предусловий: 1" in completed.stdout

print("Регрессии запуска скрытых предусловий пройдены.")
