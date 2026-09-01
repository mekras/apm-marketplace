#!/usr/bin/env python3
"""Воспроизвести скрытые предусловия проектных fixture-evals."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверить воспроизводимость проектных фикстур.")
    parser.add_argument("registry", nargs="?", type=Path, default=Path("evals/fixtures/registry.json"))
    args = parser.parse_args()
    registry_path = args.registry.resolve()
    registry = load_json(registry_path)
    errors: list[str] = []
    completed_checks = 0

    for case in registry.get("cases", []):
        fixture = (registry_path.parent / case["fixture"]).resolve()
        oracle_path = (registry_path.parent / case["oracle"]).resolve()
        oracle = load_json(oracle_path)
        for index, check in enumerate(oracle.get("fixture_checks", [])):
            label = f"{case['id']}.fixture_checks[{index}]"
            with tempfile.TemporaryDirectory(prefix="fixture-check-") as temporary:
                workspace = Path(temporary) / "workspace"
                shutil.copytree(fixture, workspace)
                result = subprocess.run(
                    check["command"],
                    cwd=workspace,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                if result.returncode != check["exit_code"]:
                    errors.append(
                        f"{label}: ожидался код {check['exit_code']}, получен {result.returncode}"
                    )
                if check.get("stdout_contains") and check["stdout_contains"] not in result.stdout:
                    errors.append(f"{label}: stdout не содержит {check['stdout_contains']!r}")
                if check.get("stderr_contains") and check["stderr_contains"] not in result.stderr:
                    errors.append(f"{label}: stderr не содержит {check['stderr_contains']!r}")
                if "json_file" in check:
                    json_path = (workspace / check["json_file"]).resolve()
                    if workspace not in json_path.parents or not json_path.is_file():
                        errors.append(f"{label}: не создан JSON {check['json_file']}")
                    else:
                        try:
                            actual = load_json(json_path)
                        except (OSError, UnicodeError, json.JSONDecodeError) as error:
                            errors.append(f"{label}: JSON не разобран: {error}")
                        else:
                            if "json_equals" in check and actual != check["json_equals"]:
                                errors.append(
                                    f"{label}: JSON отличается: ожидалось {check['json_equals']!r}, получено {actual!r}"
                                )
                completed_checks += 1

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"Воспроизведено скрытых предусловий: {completed_checks}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
