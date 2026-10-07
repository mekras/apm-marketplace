#!/usr/bin/env python3
"""Регрессионные проверки узкого обхода ложного APM drift."""

from __future__ import annotations

import json
import os
import runpy
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PureWindowsPath

# Русские сообщения не должны падать на консоли с однобайтовой кодировкой.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8", errors="replace")


AUDIT = Path(__file__).with_name("apm-audit-ci")


def write_project(
    root: Path,
    *,
    active_owner: str = ".",
    adapter_owner: str = ".",
    extra_failure: bool = False,
    local_hash_drift: bool = False,
    phantom_bytecode: bool = False,
    present_bytecode: bool = False,
    lock_bytecode: bool = False,
    echoed_ref_mismatch: bool = False,
    actual_ref_mismatch: bool = False,
    local_addition: bool = False,
    changed_local_addition: bool = False,
    local_removal: bool = False,
    unowned_removal: bool = False,
    rewritten_local_source: bool = False,
    local_source_presence: bool = False,
    package_id: str = "example/local-package",
    dependency: dict[str, object] | None = None,
    duplicate_dependency: bool = False,
    manifest_version: str = "1.0.0",
) -> Path:
    (root / "apm.yml").write_text(
        f"name: local-package\nversion: {manifest_version}\n",
        encoding="utf-8",
    )
    source = root / ".apm" / "skills" / "example" / "SKILL.md"
    deployed = root / ".agents" / "skills" / "example" / "SKILL.md"
    source.parent.mkdir(parents=True)
    deployed.parent.mkdir(parents=True)
    source.write_text(
        "[skill](../other/README.md)\n" if rewritten_local_source else "local source\n",
        encoding="utf-8",
    )
    deployed.write_text(
        "[skill](../../../.apm/skills/other/README.md)\n"
        if rewritten_local_source
        else "local source\n",
        encoding="utf-8",
    )
    adapter_source = root / ".apm" / "skills" / "example" / "scripts" / "adapter.py"
    adapter_deployed = root / ".agents" / "skills" / "example" / "scripts" / "adapter.py"
    adapter_source.parent.mkdir(parents=True)
    adapter_deployed.parent.mkdir(parents=True)
    adapter_source.write_text("#!/bin/sh\n", encoding="utf-8")
    adapter_deployed.write_text("#!/bin/sh\n", encoding="utf-8")
    dependency = dependency if dependency is not None else {
        "repo_url": "example/local-package", "name": "local-package", "version": "1.0.0",
    }
    dependencies = [dependency, dependency] if duplicate_dependency else [dependency]
    lockfile = f"""dependencies: {json.dumps(dependencies)}
deployments:
- value: .agents/skills/example/SKILL.md
  owners: [{package_id}, .]
  active_owner: {active_owner}
- value: .agents/skills/example/scripts/adapter.py
  owners: [{package_id}, {adapter_owner}]
  active_owner: {adapter_owner}
"""
    if local_removal or unowned_removal:
        removal_path = ".agents/skills/example/references/removed.md"
        removal_owners = (
            "[example/local-package]" if local_removal else "[example/other-package]"
        )
        lockfile += f"""- value: {removal_path}
  owners: {removal_owners}
  active_owner: example/local-package
"""
    (root / "apm.lock.yaml").write_text(lockfile, encoding="utf-8")
    if lock_bytecode:
        with (root / "apm.lock.yaml").open("a", encoding="utf-8") as stream:
            stream.write(
                "- value: .agents/skills/example/scripts/__pycache__/"
                "adapter.cpython-313.pyc\n"
                "  owners: [example/local-package]\n"
                "  active_owner: example/local-package\n"
            )
    checks = [{"name": "lockfile-exists", "passed": True}]
    if echoed_ref_mismatch or actual_ref_mismatch:
        manifest_ref = "^0.22.0"
        lockfile_ref = manifest_ref if echoed_ref_mismatch else "0.22.0"
        checks.append(
            {
                "name": "ref-consistency",
                "passed": False,
                "details": [
                    f"example/local-package: manifest ref '{manifest_ref}' != lockfile ref '{lockfile_ref}'"
                ],
            }
        )
        drift = []
    else:
        checks.append({"name": "drift", "passed": False})
        drift = [
            {
                "path": ".agents/skills/example/SKILL.md",
                "kind": "modified",
                "package": package_id,
            }
        ]
    if extra_failure:
        checks.append({"name": "content-integrity", "passed": False})
    if local_hash_drift:
        checks.append(
            {
                "name": "content-integrity",
                "passed": False,
                "details": [
                    "hash-drift: .agents/skills/example/SKILL.md "
                    "(dep=<self>, expected=old, actual=new)"
                ],
            }
        )
    if phantom_bytecode:
        bytecode = root / ".agents" / "skills" / "example" / "scripts" / "__pycache__" / "adapter.cpython-313.pyc"
        if present_bytecode:
            bytecode.parent.mkdir(parents=True)
            bytecode.write_bytes(b"not phantom")
        drift.append(
            {
            "path": ".agents/skills/example/scripts/__pycache__/adapter.cpython-313.pyc",
                "kind": "unintegrated",
                "package": "",
            }
        )
    if local_source_presence:
        checks.append(
            {
                "name": "deployed-files-present",
                "passed": False,
                "details": [".agents/skills/example/SKILL.md"],
            }
        )
    if local_addition or changed_local_addition:
        added_source = root / ".apm" / "skills" / "example" / "references" / "new.md"
        added_deployed = root / ".agents" / "skills" / "example" / "references" / "new.md"
        added_source.parent.mkdir(parents=True, exist_ok=True)
        added_deployed.parent.mkdir(parents=True, exist_ok=True)
        added_source.write_text("new local source\n", encoding="utf-8")
        added_deployed.write_text(
            "changed\n" if changed_local_addition else "new local source\n",
            encoding="utf-8",
        )
        drift.append(
            {
                "path": ".agents/skills/example/references/new.md",
                "kind": "orphaned",
                "package": ".",
            }
        )
    if local_removal or unowned_removal:
        checks.append(
            {
                "name": "deployed-files-present",
                "passed": False,
                "details": [removal_path],
            }
        )
        drift.append(
            {
                "path": removal_path,
                "kind": "unintegrated",
                "package": "example/local-package",
            }
        )
    report = {
        "passed": False,
        "checks": checks,
        "drift": {
            "drift": drift
        },
    }
    fake_apm = root / "fake-apm"
    fake_apm.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "assert os.environ.get('PYTHONDONTWRITEBYTECODE') == '1'\n"
        f"print({json.dumps(json.dumps(report))})\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    fake_apm.chmod(fake_apm.stat().st_mode | stat.S_IXUSR)
    if os.name == "nt":
        wrapper = root / "fake-apm.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{fake_apm}" %*\n', encoding="utf-8")
        return wrapper
    return fake_apm


def run(
    root: Path,
    fake_apm: Path,
    *,
    allow_unpublished_version: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-S",
            str(AUDIT),
            "--apm",
            str(fake_apm),
            "--project-root",
            str(root),
            *(
                ["--allow-unpublished-local-version"]
                if allow_unpublished_version
                else []
            ),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def main() -> int:
    test_package_identity()
    test_apm_fields()
    test_windows_ledger_path()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        accepted = run(root, write_project(root))
        assert accepted.returncode == 0, accepted.stdout + accepted.stderr
        assert "подтверждено файлов — 1" in accepted.stdout

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fake_apm = write_project(root, manifest_version="1.1.0")
        accepted = run(root, fake_apm)
        assert accepted.returncode == 0, accepted.stdout + accepted.stderr
        assert "не внесённой в файл блокировки" in accepted.stdout
        accepted = run(root, fake_apm, allow_unpublished_version=True)
        assert accepted.returncode == 0, accepted.stdout + accepted.stderr
        assert "неопубликованной локальной версией" in accepted.stdout

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rejected = run(
            root,
            write_project(root, manifest_version="0.9.0"),
            allow_unpublished_version=True,
        )
        assert rejected.returncode == 1, rejected.stdout + rejected.stderr

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        accepted = run(root, write_project(root, phantom_bytecode=True))
        assert accepted.returncode == 0, accepted.stdout + accepted.stderr
        assert "подтверждено файлов — 2" in accepted.stdout

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rejected = run(
            root,
            write_project(root, phantom_bytecode=True, lock_bytecode=True),
        )
        assert rejected.returncode == 1, rejected.stdout + rejected.stderr
        assert "apm.lock.yaml" in rejected.stderr

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        accepted = run(
            root,
            write_project(
                root,
                phantom_bytecode=True,
                adapter_owner="example/other-package",
            ),
        )
        assert accepted.returncode == 0, accepted.stdout + accepted.stderr
        assert "подтверждено файлов — 2" in accepted.stdout

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        accepted = run(root, write_project(root, local_hash_drift=True))
        assert accepted.returncode == 0, accepted.stdout + accepted.stderr
        assert "подтверждено файлов — 1" in accepted.stdout

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        accepted = run(
            root,
            write_project(
                root,
                local_hash_drift=True,
                rewritten_local_source=True,
                local_source_presence=True,
            ),
        )
        assert accepted.returncode == 0, accepted.stdout + accepted.stderr
        assert "подтверждено файлов — 1" in accepted.stdout

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        accepted = run(root, write_project(root, local_addition=True))
        assert accepted.returncode == 0, accepted.stdout + accepted.stderr
        assert "подтверждено файлов — 2" in accepted.stdout

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rejected = run(root, write_project(root, changed_local_addition=True))
        assert rejected.returncode == 1, rejected.stdout + rejected.stderr
        assert '"orphaned"' in rejected.stdout

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        accepted = run(root, write_project(root, local_removal=True))
        assert accepted.returncode == 0, accepted.stdout + accepted.stderr
        assert "подтверждено файлов — 2" in accepted.stdout

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rejected = run(root, write_project(root, unowned_removal=True))
        assert rejected.returncode == 1, rejected.stdout + rejected.stderr
        assert '"deployed-files-present"' in rejected.stdout

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rejected = run(
            root,
            write_project(root, phantom_bytecode=True, present_bytecode=True),
        )
        assert rejected.returncode == 1, rejected.stdout + rejected.stderr
        assert '"unintegrated"' in rejected.stdout

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        accepted = run(root, write_project(root, active_owner="example/local-package"))
        assert accepted.returncode == 0, accepted.stdout + accepted.stderr

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rejected = run(root, write_project(root, active_owner="example/other-package"))
        assert rejected.returncode == 1, rejected.stdout + rejected.stderr
        assert '"passed": false' in rejected.stdout

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rejected = run(root, write_project(root, extra_failure=True))
        assert rejected.returncode == 1, rejected.stdout + rejected.stderr
        assert '"content-integrity"' in rejected.stdout

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        accepted = run(root, write_project(root, echoed_ref_mismatch=True))
        assert accepted.returncode == 0, accepted.stdout + accepted.stderr
        assert "самопротиворечивым сообщением" in accepted.stdout

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rejected = run(root, write_project(root, actual_ref_mismatch=True))
        assert rejected.returncode == 1, rejected.stdout + rejected.stderr
        assert '"ref-consistency"' in rejected.stdout

    print("Узкий обход ложного APM drift проверен.")
    return 0


def test_windows_ledger_path() -> None:
    """Ключ ledger сохраняет разделитель APM при разборе пути Windows."""
    namespace = runpy.run_path(str(AUDIT))
    check = namespace["is_phantom_bytecode"]
    check.__globals__["Path"] = PureWindowsPath
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        deployed = root / ".agents/skills/example/scripts/adapter.py"
        deployed.parent.mkdir(parents=True)
        deployed.write_text("pass\n", encoding="utf-8")
        finding = {
            "kind": "unintegrated", "package": "",
            "path": ".agents/skills/example/scripts/__pycache__/adapter.cpython-313.pyc",
        }
        owners = {".agents/skills/example/scripts/adapter.py": {
            "active_owner": "example/package", "owners": ["example/package"],
        }}
        assert check(finding, owners, "example/package", root)
        assert not check(finding, {}, "example/package", root)


def test_apm_fields() -> None:
    """Генерируемый APM YAML читается без PyYAML, неоднозначность отклоняется."""
    load = runpy.run_path(str(AUDIT))["load_yaml"]
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fake = write_project(root)
        lock = root / "apm.lock.yaml"
        lock.write_text(
            "dependencies:\n"
            "- repo_url: example/local-package\n"
            "  name: local-package\n"
            "  version: 1.0.0\n"
            "  deployed_files:\n"
            "  - .agents/skills/example/SKILL.md\n"
            "  deployed_file_hashes:\n"
            "    .agents/skills/example/SKILL.md: ignored\n"
            "deployments:\n"
            "- value: .agents/skills/example/SKILL.md\n"
            "  owners:\n"
            "  - example/local-package\n"
            "  - '.'\n"
            "  active_owner: '.'\n",
            encoding="utf-8",
        )
        accepted = run(root, fake)
        assert accepted.returncode == 0, accepted.stdout + accepted.stderr
        original = lock.read_text(encoding="utf-8")
        indented = "\n".join(
            line if line in {"dependencies:", "deployments:"} else "  " + line
            for line in original.splitlines()
        ) + "\n"
        lock.write_text(indented, encoding="utf-8")
        accepted = run(root, fake)
        assert accepted.returncode == 0, accepted.stdout + accepted.stderr
        for invalid in (
            original.replace("  name: local-package", "  name: local-package\n  name: other"),
            original.replace("  active_owner: '.'", "  active_owner: *owner"),
            original.replace("  version: 1.0.0", "  version: |\n    1.0.0"),
            original + "dependencies: []\n",
            original + "- value: .agents/skills/example/SKILL.md\n  owners: [.]\n  active_owner: .\n",
            original.replace("- repo_url: example/local-package\n  name: local-package\n  version: 1.0.0\n  deployed_files:\n  - .agents/skills/example/SKILL.md\n  deployed_file_hashes:\n    .agents/skills/example/SKILL.md: ignored", "- repo_url: example/local-package\n  name: local-package\n  version: 1.0.0\n  version: 2.0.0"),
        ):
            lock.write_text(invalid, encoding="utf-8")
            rejected = run(root, fake)
            assert rejected.returncode == 1, rejected.stdout + rejected.stderr
            assert "не подтверждено" in rejected.stderr
        lock.write_text(original, encoding="utf-8")
        assert load(lock)["deployments"][0]["owners"] == ["example/local-package", "."]
        manifest = root / "apm.yml"
        manifest.write_text("name: local-package # комментарий\nversion: 1.0.0\ndependencies:\n  apm: []\n", encoding="utf-8")
        assert load(manifest, manifest=True) == {"name": "local-package", "version": "1.0.0"}


def test_package_identity() -> None:
    package_id = "example/marketplace/packages/local-package"
    virtual = {
        "name": "local-package", "version": "1.0.0", "repo_url": "example/marketplace",
        "is_virtual": True, "virtual_path": "packages/local-package",
    }
    for active_owner in (".", package_id):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accepted = run(root, write_project(root, dependency=virtual,
                package_id=package_id, active_owner=active_owner))
            assert accepted.returncode == 0, accepted.stdout + accepted.stderr
            assert "подтверждено файлов — 1" in accepted.stdout

    for options in (
        {"dependency": {key: value for key, value in virtual.items() if key != "virtual_path"}},
        {"dependency": virtual, "duplicate_dependency": True},
        {"dependency": {**virtual, "version": "2.0.0"}},
        {"dependency": virtual, "active_owner": "example/other-package"},
        {"dependency": virtual, "extra_failure": True},
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rejected = run(root, write_project(root, package_id=package_id, **options))
            assert rejected.returncode == 1, rejected.stdout + rejected.stderr
            assert '"passed": false' in rejected.stdout

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fake_apm = write_project(root, dependency=virtual, package_id=package_id)
        (root / ".agents/skills/example/SKILL.md").write_text("unrelated edit\n", encoding="utf-8")
        rejected = run(root, fake_apm)
        assert rejected.returncode == 1, rejected.stdout + rejected.stderr

    resolve = runpy.run_path(str(AUDIT))["local_package_id"]
    matches = runpy.run_path(str(AUDIT))["matches_local_source"]
    manifest = {"name": "local-package", "version": "1.0.0"}
    ordinary = {key: value for key, value in virtual.items() if key not in {"is_virtual", "virtual_path"}}
    assert resolve(manifest, {"dependencies": [ordinary]}) == "example/marketplace"
    assert resolve(manifest, {"dependencies": [{**ordinary, "is_virtual": False}]}) == "example/marketplace"
    for invalid in (
        {**virtual, "is_virtual": "true"},
        {**virtual, "is_virtual": False},
        {**virtual, "virtual_path": ""},
        {**virtual, "virtual_path": 42},
        {**virtual, "virtual_path": "../local-package"},
        {**virtual, "virtual_path": "packages/./local-package"},
        {**virtual, "virtual_path": r"packages\local-package"},
        {**virtual, "virtual_path": "/packages/local-package"},
        {**virtual, "repo_url": ""},
    ):
        assert resolve(manifest, {"dependencies": [invalid]}) is None, invalid
    assert resolve(manifest, {"dependencies": [ordinary, {**ordinary, "repo_url": None}]}) is None

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / ".apm/skills/example/references/SKILL.md"
        deployed = root / ".agents/skills/example/references/SKILL.md"
        source.parent.mkdir(parents=True)
        deployed.parent.mkdir(parents=True)
        source.write_text(
            "[history](../../other/references/history.md)\n",
            encoding="utf-8",
        )
        deployed.write_text(
            "[history](../../../../.apm/skills/other/references/history.md)\n",
            encoding="utf-8",
        )
        assert matches(source, deployed, root)
        deployed.write_text(
            "[history](../../../../.apm/skills/wrong/references/history.md)\n",
            encoding="utf-8",
        )
        assert not matches(source, deployed, root)
        deployed.write_text(
            "[history](../../../../.apm/skills/other/references/history.md)\n"
            "unrelated change\n",
            encoding="utf-8",
        )
        assert not matches(source, deployed, root)


if __name__ == "__main__":
    raise SystemExit(main())
