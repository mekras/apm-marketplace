"""Versioned per-user E5 storage and explicitly requested runtime preparation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig

from . import MODEL, REVISION, VERSIONS, digest

REQUIREMENTS = Path(__file__).resolve().parents[2] / "assets/e5-requirements.txt"


def shared_locations(root=None):
    if root is None:
        cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        if not cache.is_absolute():
            cache = Path.home() / ".cache"
        root = cache / "project-knowledge-corpus"
    root = root.expanduser().absolute()
    key = f"{sys.implementation.cache_tag}-{sysconfig.get_platform()}-{digest(REQUIREMENTS)[:16]}"
    runtime = root / "runtimes" / key
    return {"model_dir": root / "models" / MODEL.replace("/", "--") / REVISION,
            "runtime_dir": runtime,
            "runtime_python": runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")}


def run(command, cache):
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8", timeout=900,
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                            "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_CACHE_DIR": str(cache)})
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ValueError("E5 runtime preparation or validation failed") from error


def check_runtime(python, cache):
    code = ("import importlib.metadata as m; "
            f"expected={VERSIONS!r}; "
            "assert all(m.version(name)==version for name,version in expected.items())")
    run([str(python), "-I", "-B", "-c", code], cache)


def reuse_runtime(locations):
    directory = locations["runtime_dir"]
    marker = directory / "e5-runtime.json"
    expected = {"requirements_sha256": digest(REQUIREMENTS),
                "python_tag": sys.implementation.cache_tag, "platform": sysconfig.get_platform()}
    if not directory.exists() and not directory.is_symlink():
        raise ValueError("Shared E5 runtime is missing; use --prepare-runtime")
    if (directory.is_symlink() or marker.is_symlink() or not marker.is_file()
            or json.loads(marker.read_text(encoding="utf-8")) != expected
            or not locations["runtime_python"].is_file()):
        raise ValueError("Existing shared E5 runtime is incomplete or incompatible; no overwrite")
    check_runtime(locations["runtime_python"], directory.parent / "package-downloads")


def prepare_runtime(locations):
    if sys.version_info[:2] != (3, 13):
        raise ValueError("Prepare the E5 runtime with CPython 3.13")
    directory = locations["runtime_dir"]
    python = locations["runtime_python"]
    marker = directory / "e5-runtime.json"
    expected = {"requirements_sha256": digest(REQUIREMENTS),
                "python_tag": sys.implementation.cache_tag, "platform": sysconfig.get_platform()}
    cache = directory.parent / "package-downloads"
    directory.parent.mkdir(parents=True, exist_ok=True)
    lock = directory.with_name(directory.name + ".lock")
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise ValueError("Shared E5 runtime is being prepared; retry after setup finishes") from error
    created = False
    try:
        if directory.exists() or directory.is_symlink():
            reuse_runtime(locations)
            return "reused"
        directory.mkdir()
        created = True
        run([sys.executable, "-B", "-m", "venv", "--without-pip", str(directory)], cache)
        run([sys.executable, "-B", "-m", "pip", "--python", str(python), "install",
             "--no-compile", "--only-binary=:all:", "--require-hashes", "-r", str(REQUIREMENTS)], cache)
        run([sys.executable, "-B", "-m", "pip", "--python", str(python), "check"], cache)
        check_runtime(python, cache)
        marker.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
        return "created"
    except BaseException:
        if created:
            shutil.rmtree(directory)
        raise
    finally:
        lock.rmdir()
