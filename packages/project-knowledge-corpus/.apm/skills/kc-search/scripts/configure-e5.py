#!/usr/bin/env python3
"""Create a local E5 configuration, optionally downloading pinned model files."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.request

sys.dont_write_bytecode = True
from e5_runtime import MODEL, REVISION, FILES, digest
from e5_runtime.storage import shared_locations, prepare_runtime, reuse_runtime


def download_model(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for name, (remote, expected) in FILES.items():
        destination = directory / name
        if destination.is_symlink():
            raise ValueError("Model destination is a symlink: " + name)
        if destination.exists():
            if not destination.is_file() or digest(destination) != expected:
                raise ValueError("Existing model file differs; no overwrite: " + name)
            continue
        url = f"https://huggingface.co/{MODEL}/resolve/{REVISION}/{remote}"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as target:
                temporary = Path(target.name)
                with urllib.request.urlopen(url, timeout=120) as response:
                    total = 0
                    while block := response.read(1024 * 1024):
                        total += len(block)
                        if total > 200 * 1024 * 1024:
                            raise ValueError("Model download exceeds limit")
                        target.write(block)
            if digest(temporary) != expected:
                raise ValueError("Downloaded model hash mismatch: " + name)
            # Exclusive creation prevents replacing an existing file in a concurrent setup.
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if destination.is_symlink() or not destination.is_file() or digest(destination) != expected:
                    raise ValueError("Concurrent model file differs; no overwrite: " + name)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def configure(scope, model_dir, runtime_python, output, approved_digest, download=False,
              prepare=False, shared_dir=None):
    raw = scope.read_bytes()
    scope_digest = hashlib.sha256(raw).hexdigest()
    if approved_digest != scope_digest:
        raise ValueError("Reviewed scope SHA-256 does not match")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or data.get("scope_version") != 1:
        raise ValueError("Expected scope_version: 1")
    if output.exists() or output.is_symlink():
        raise ValueError("Configuration already exists; no overwrite")
    if prepare and runtime_python is not None:
        raise ValueError("--prepare-runtime cannot be combined with --runtime-python")
    locations = shared_locations(shared_dir)
    model_dir = (model_dir or locations["model_dir"]).expanduser()
    preparation = prepare_runtime(locations) if prepare else "not_requested"
    if not prepare and runtime_python is None:
        reuse_runtime(locations)
        preparation = "reused"
    runtime_python = (runtime_python or locations["runtime_python"]).expanduser()
    if not runtime_python.is_file():
        raise ValueError("Runtime Python is missing; use --prepare-runtime or --runtime-python")
    if download:
        download_model(model_dir)
    config = {"embedding_config_version": 1, "model": MODEL, "revision": REVISION,
              "execution": "local", "approved_scope_sha256": scope_digest,
              "command": [str(runtime_python.absolute()), "-B",
                          str(Path(__file__).resolve().parent / "e5_runtime/adapter.py"),
                          "--model-dir", str(model_dir.resolve())],
              "query_prefix": "query: ", "passage_prefix": "passage: ",
              "batch_size": 32, "max_input_chars": 1000, "timeout_seconds": 120,
              "rank_window": 50, "rrf_constant": 60}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return {"configuration": str(output), "model": MODEL, "revision": REVISION,
            "model_download_requested": download, "runtime_validated": False,
            "runtime_preparation": preparation, "model_dir": str(model_dir.resolve()),
            "runtime_python": str(runtime_python.absolute()),
            "next_step": "Run a semantic search to validate the runtime and model"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--approved-scope-sha256", required=True)
    parser.add_argument("--model-dir", type=Path, help="Override the shared versioned model directory")
    parser.add_argument("--runtime-python", type=Path, help="Use an explicitly prepared Python runtime")
    parser.add_argument("--shared-dir", type=Path, help="Override the common per-user storage root")
    parser.add_argument("--prepare-runtime", action="store_true",
                        help="Explicitly allow creating the shared runtime and installing pinned packages")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--download-model", action="store_true",
                        help="Explicitly allow HTTPS download of the pinned model and tokenizer")
    args = parser.parse_args()
    try:
        result = configure(args.scope, args.model_dir, args.runtime_python, args.output,
                           args.approved_scope_sha256, args.download_model, args.prepare_runtime, args.shared_dir)
        print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError) as error:
        print("E5 setup failed: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
