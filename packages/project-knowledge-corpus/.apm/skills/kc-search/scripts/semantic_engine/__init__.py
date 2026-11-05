"""Validated local embedding adapter and deterministic rank fusion."""
from __future__ import annotations

import json
import hashlib
import math
import os
import subprocess
import sys
import time


def unit_vector(values):
    if not isinstance(values, list) or not values or len(values) > 65536:
        raise ValueError("Embedding must be a nonempty numeric vector")
    if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v)
           for v in values):
        raise ValueError("Embedding contains a nonfinite or nonnumeric value")
    norm = math.hypot(*values)
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("Embedding has zero or nonfinite norm")
    return [v / norm for v in values]


def read_config(path, scope_digest):
    raw = path.read_bytes()
    config = json.loads(raw.decode("utf-8"))
    if not isinstance(config, dict) or config.get("embedding_config_version") != 1:
        raise ValueError("Expected embedding_config_version: 1")
    if config.get("approved_scope_sha256") != scope_digest:
        raise ValueError("Embedding access review does not match this scope")
    if config.get("execution") != "local":
        raise ValueError("This adapter contract requires execution: local")
    command = config.get("command")
    if not isinstance(command, list) or not command or any(
        not isinstance(arg, str) or not arg for arg in command
    ):
        raise ValueError("Embedding command must be a nonempty argument array")
    for key in ("model", "revision"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError("Embedding config requires " + key)
    for key, default, lower, upper in (("batch_size", 32, 1, 256),
                                       ("max_input_chars", 1000, 64, 4096),
                                       ("timeout_seconds", 120, 1, 600),
                                       ("rank_window", 50, 1, 10000),
                                       ("rrf_constant", 60, 1, 10000)):
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise ValueError("Invalid embedding option: " + key)
        config[key] = value
    for key in ("query_prefix", "passage_prefix"):
        if not isinstance(config.get(key, ""), str) or len(config.get(key, "")) > 128:
            raise ValueError("Invalid embedding prefix")
        config.setdefault(key, "")
    config["command"] = [arg.replace("{config_dir}", str(path.resolve().parent))
                         .replace("{python}", sys.executable) for arg in command]
    config["_sha256"] = hashlib.sha256(raw).hexdigest()
    return config


class EmbeddingAdapter:
    def __init__(self, path, scope_digest):
        self.path = path.resolve()
        self.config = read_config(path, scope_digest)
        self.dimension = None
        self.requests = 0
        self.inputs = 0
        self.elapsed_seconds = 0.0

    def batch(self, texts):
        request = {"embedding_protocol_version": 1, "model": self.config["model"],
                   "revision": self.config["revision"],
                   "inputs": [{"id": str(i), "text": text} for i, text in enumerate(texts)]}
        started = time.monotonic()
        try:
            result = subprocess.run(
                self.config["command"], input=json.dumps(request, ensure_ascii=False),
                capture_output=True, text=True, encoding="utf-8", check=False,
                cwd=self.path.parent, timeout=self.config["timeout_seconds"],
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                     "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                     "KC_EMBEDDING_READ_ONLY": "1", "KC_EMBEDDING_NETWORK": "prohibited"},
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError("Embedding adapter failed or timed out") from error
        finally:
            self.elapsed_seconds += time.monotonic() - started
        if result.returncode != 0:
            # Adapter stderr may contain private corpus content or credentials.
            raise ValueError("Embedding adapter returned a nonzero exit status")
        try:
            response = json.loads(result.stdout)
        except ValueError as error:
            raise ValueError("Embedding adapter returned invalid JSON") from error
        if not isinstance(response, dict) or response.get("embedding_protocol_version") != 1:
            raise ValueError("Unsupported embedding response")
        if any(response.get(key) != self.config[key] for key in ("model", "revision")):
            raise ValueError("Embedding model or revision mismatch")
        rows = response.get("embeddings")
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise ValueError("Embedding count mismatch")
        vectors = {}
        expected_ids = {str(i) for i in range(len(texts))}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                raise ValueError("Embedding response requires string ids")
            ident = row["id"]
            if ident not in expected_ids or ident in vectors:
                raise ValueError("Missing, duplicate or unknown embedding id")
            vector = unit_vector(row.get("values"))
            if self.dimension is None:
                self.dimension = len(vector)
            if len(vector) != self.dimension:
                raise ValueError("Embedding dimension mismatch")
            vectors[ident] = vector
        self.requests += 1
        self.inputs += len(texts)
        return [vectors[str(i)] for i in range(len(texts))]

    def encode(self, texts, kind):
        prefix = self.config[kind + "_prefix"]
        limit = self.config["max_input_chars"]
        stride = limit - min(128, limit // 8)
        pieces, owners = [], []
        for index, text in enumerate(texts):
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Cannot embed empty text")
            for start in range(0, len(text), stride):
                pieces.append(prefix + text[start:start + limit])
                owners.append(index)
                if start + limit >= len(text):
                    break
        totals = [None] * len(texts)
        batch_size = self.config["batch_size"]
        for start in range(0, len(pieces), batch_size):
            vectors = self.batch(pieces[start:start + batch_size])
            for owner, vector in zip(owners[start:start + batch_size], vectors):
                if totals[owner] is None:
                    totals[owner] = vector
                else:
                    totals[owner] = [a + b for a, b in zip(totals[owner], vector)]
        return [unit_vector(vector) for vector in totals]

    def summary(self):
        return {key: self.config[key] for key in (
            "model", "revision", "execution", "max_input_chars", "query_prefix",
            "passage_prefix", "batch_size", "rank_window", "rrf_constant"
        )} | {"dimension": self.dimension, "requests": self.requests, "inputs": self.inputs,
             "adapter_seconds": round(self.elapsed_seconds, 3),
             "identity_check": "adapter_self_report", "config_sha256": self.config["_sha256"]}


def fuse(lexical, semantic, limit, constant):
    scores, candidates, channels = {}, {}, {}
    for name, results in (("lexical", lexical), ("semantic", semantic)):
        for row in results:
            ident = row["passage_id"]
            candidates[ident] = row
            scores[ident] = scores.get(ident, 0.0) + 1 / (constant + row["rank"])
            channels.setdefault(ident, {})[name] = {"rank": row["rank"], "score": row["score"]}
    ranked = sorted(scores, key=lambda ident: (-scores[ident], ident))[:limit]
    return [candidates[ident] | {"rank": rank, "score": scores[ident], "channels": channels[ident]}
            for rank, ident in enumerate(ranked, 1)]
