#!/usr/bin/env python3
"""Search an explicitly configured local corpus without a persistent cache."""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time

from semantic_engine import EmbeddingAdapter, fuse

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Expected a nonempty portable relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or ":" in value:
        raise ValueError("Path must stay inside the corpus")
    return path


def load_scope(path, root):
    raw = path.read_bytes()
    scope = json.loads(raw.decode("utf-8"))
    if not isinstance(scope, dict) or scope.get("scope_version") != 1:
        raise ValueError("Expected scope_version: 1")
    sources = scope.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("Scope must declare sources")
    roots, ids = [], set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("Source must be an object")
        for key in ("id", "title", "root"):
            if not isinstance(source.get(key), str) or not source[key].strip():
                raise ValueError("Source requires " + key)
        if source["id"] in ids:
            raise ValueError("Duplicate source id")
        ids.add(source["id"])
        if source.get("authorized_for_local_search") is not True:
            raise ValueError("Source requires authorized_for_local_search: true")
        for key in ("exclude", "limitations"):
            if not isinstance(source.get(key, []), list) or any(
                not isinstance(value, str) for value in source.get(key, [])
            ):
                raise ValueError(key + " must be a list of strings")
        if not isinstance(source.get("constraints", {}), dict):
            raise ValueError("constraints must be an object")
        for key in ("description", "locator"):
            if key in source and not isinstance(source[key], str):
                raise ValueError(key + " must be a string")
        if not isinstance(source.get("allow_local", False), bool):
            raise ValueError("allow_local must be boolean")
        folder = root / relative_path(source["root"])
        if not folder.resolve().is_relative_to(root):
            raise ValueError("Source root leaves the corpus")
        resolved = folder.resolve()
        if any(resolved.is_relative_to(old) or old.is_relative_to(resolved) for old in roots):
            raise ValueError("Source roots must not overlap")
        roots.append(resolved)
    return sources, hashlib.sha256(raw).hexdigest()


def normalized(text):
    return text.casefold().replace("ё", "е")


def scan(root, sources, max_bytes):
    documents, skipped, digests = [], [], {}
    for source in sources:
        folder = root / source["root"]
        title = source["title"] + " " + source.get("description", "")
        documents.append({"path": source["root"], "line_start": None,
                          "line_end": None, "text": title, "kind": "metadata",
                          "source": source, "sha256": None})
        parts = Path(source["root"]).parts
        if not source.get("allow_local", False) and any(
            ".local." in part or part.endswith(".local") for part in parts
        ):
            skipped.append({"path": source["root"], "reason": "local_not_enabled"})
            continue
        if any(part == ".git" or ".tmp." in part for part in parts):
            skipped.append({"path": source["root"], "reason": "temporary_or_vcs"})
            continue
        if not folder.is_dir() or folder.is_symlink():
            skipped.append({"path": source["root"], "reason": "unavailable_source"})
            continue

        def walk_error(error):
            skipped.append({"path": Path(error.filename).relative_to(root).as_posix(),
                            "reason": "unreadable_directory"})

        for directory, dirs, files in os.walk(folder, followlinks=False, onerror=walk_error):
            for name in sorted(dirs + files):
                path = Path(directory) / name
                rel = path.relative_to(folder).as_posix()
                location = path.relative_to(root).as_posix()
                reason = None
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    reason = "symlink"
                elif name == ".git" or ".tmp." in name:
                    reason = "temporary_or_vcs"
                elif not source.get("allow_local", False) and (
                    ".local." in name or name.endswith(".local")
                ):
                    reason = "local_not_enabled"
                elif any(fnmatch.fnmatchcase(rel, mask) for mask in source.get("exclude", [])):
                    reason = "excluded_by_scope"
                if reason:
                    skipped.append({"path": location, "reason": reason})
                    if name in dirs:
                        dirs.remove(name)
                    continue
                if name in dirs:
                    continue
                try:
                    info = path.stat()
                    if not stat.S_ISREG(info.st_mode):
                        skipped.append({"path": location, "reason": "not_regular_file"})
                        continue
                    if info.st_size > max_bytes:
                        skipped.append({"path": location, "reason": "size_limit"})
                        continue
                    with path.open("rb") as handle:
                        raw = handle.read(max_bytes + 1)
                    if len(raw) > max_bytes:
                        skipped.append({"path": location, "reason": "size_limit"})
                        continue
                    if b"\x00" in raw:
                        raise UnicodeError("binary")
                    text = raw.decode("utf-8-sig")
                except (OSError, UnicodeError) as error:
                    skipped.append({"path": location, "reason": (
                        "unreadable" if isinstance(error, OSError) else "not_utf8_text")})
                    continue
                digest = hashlib.sha256(raw).hexdigest()
                digests[location] = digest
                lines = text.splitlines()
                for start in range(0, len(lines), 18):
                    documents.append({"path": location, "line_start": start + 1,
                                      "line_end": min(start + 24, len(lines)),
                                      "text": "\n".join(lines[start:start + 24]),
                                      "kind": "text", "source": source, "sha256": digest})
            dirs.sort()
    return documents, {"scope": "configured_sources_only", "files": digests,
                       "indexed_files": len(digests), "skipped": skipped,
                       "sources": [s["id"] for s in sources],
                       "absence_is_not_proven": True}


def make_index(documents):
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE passages USING fts5(title, body)")
        connection.executemany("INSERT INTO passages(rowid,title,body) VALUES(?,?,?)", [
            (number + 1, normalized(doc["source"]["title"]), normalized(doc["text"]))
            for number, doc in enumerate(documents)
        ])
    except sqlite3.Error:
        connection.close()
        raise
    return connection


def candidate(doc, rank, score):
    source = doc["source"]
    identity = json.dumps([source["id"], doc["path"], doc["line_start"], doc["sha256"]])
    return {k: v for k, v in doc.items() if k != "source"} | {
        "passage_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "rank": rank, "score": score,
        "source_id": source["id"], "source_title": source["title"],
        "source_locator": source.get("locator"),
        "usage": {"status": "unassessed", "constraints": source.get("constraints", {}),
                  "limitations": source.get("limitations", [])},
    }


def ranked_candidates(documents, rows, top_k):
    results, seen = [], {}
    for position, score in rows:
        doc = documents[position]
        start = doc["line_start"]
        if start is not None and any(abs(start - old) < 24 for old in seen.get(doc["path"], [])):
            continue
        if start is not None:
            seen.setdefault(doc["path"], []).append(start)
        results.append(candidate(doc, len(results) + 1, score))
        if len(results) >= top_k:
            break
    return results


def search(connection, documents, query, top_k):
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    terms = list(dict.fromkeys(re.findall(r"\w+", normalized(query), flags=re.UNICODE)))
    if not terms:
        raise ValueError("Query must contain words or identifiers")
    expression = " OR ".join('"' + word + '"' for word in terms)
    rows = connection.execute(
        "SELECT rowid,bm25(passages,2.0,1.0) FROM passages "
        "WHERE passages MATCH ? ORDER BY 2,rowid", (expression,))
    return ranked_candidates(documents, ((rowid - 1, -score) for rowid, score in rows), top_k)


class Retriever:
    def __init__(self, connection, documents, adapter=None):
        self.connection = connection
        self.documents = documents
        self.adapter = adapter
        self.document_vectors = None
        self.positions = [i for i, doc in enumerate(documents) if doc["text"].strip()]
        self.query_vectors = {}

    def prepare(self, queries):
        if self.document_vectors is None:
            self.document_vectors = self.adapter.encode(
                [self.documents[i]["text"] for i in self.positions], "passage")
        missing = list(dict.fromkeys(query for query in queries if query not in self.query_vectors))
        if missing:
            self.query_vectors.update(zip(missing, self.adapter.encode(missing, "query")))

    def semantic(self, query, top_k):
        self.prepare([query])
        query_vector = self.query_vectors[query]
        scores = [(position, max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(vector, query_vector)))))
                  for position, vector in zip(self.positions, self.document_vectors)]
        scores.sort(key=lambda row: (-row[1], row[0]))
        return ranked_candidates(self.documents, scores, top_k)

    def __call__(self, query, top_k, mode):
        if not isinstance(query, str) or not re.search(r"\w", query):
            raise ValueError("Query must contain words or identifiers")
        if mode == "lexical":
            return search(self.connection, self.documents, query, top_k)
        if self.adapter is None:
            raise ValueError("Semantic search requires an embedding config")
        if mode == "semantic":
            return self.semantic(query, top_k)
        window = self.adapter.config["rank_window"]
        return fuse(search(self.connection, self.documents, query, window),
                    self.semantic(query, window), top_k, self.adapter.config["rrf_constant"])


def validated_cases(documents, suite):
    if not isinstance(suite, dict) or suite.get("suite_version") != 1:
        raise ValueError("Expected suite_version: 1")
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Suite must contain cases")
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or case["id"] in ids:
            raise ValueError("Cases require unique string ids")
        ids.add(case["id"])
        if not isinstance(case.get("query"), str) or not re.search(r"\w", case["query"]):
            raise ValueError("Query must contain words or identifiers")
        expected = case.get("expected")
        if not isinstance(expected, list) or any(
            not isinstance(target, dict) or not isinstance(target.get("path"), str)
            or not isinstance(target.get("contains"), str) or not target["contains"]
            for target in expected
        ):
            raise ValueError("expected must list path/contains targets")
        if len({(t["path"], t["contains"]) for t in expected}) != len(expected):
            raise ValueError("Duplicate expected target")
        for target in expected:
            if not any(doc["path"] == target["path"] and target["contains"] in doc["text"]
                       for doc in documents):
                raise ValueError("Expected evidence is missing from configured scope")
    return cases


def evaluate(connection, documents, suite, top_k, retrieve=None):
    cases = validated_cases(documents, suite)
    output, recalls, reciprocal_ranks = [], [], []
    for case in cases:
        expected = case["expected"]
        results = (retrieve(case.get("query"), top_k) if retrieve else
                   search(connection, documents, case.get("query"), top_k))
        ranks = [next((r["rank"] for r in results if r["path"] == target["path"]
                       and target["contains"] in r["text"]), None) for target in expected]
        if expected:
            recalls.append(sum(rank is not None for rank in ranks) / len(expected))
            reciprocal_ranks.append(1 / min(rank for rank in ranks if rank is not None)
                                    if any(rank is not None for rank in ranks) else 0)
        output.append({"id": case["id"], "question": case.get("question"),
                       "query": case.get("query"), "expected_ranks": ranks,
                       "results": results, "negative_control": not expected})
    return {"top_k": top_k, "positive_cases": len(recalls),
            "recall_at_k": sum(recalls) / len(recalls) if recalls else None,
            "mrr_at_k": sum(reciprocal_ranks) / len(reciprocal_ranks) if recalls else None,
            "cases": output, "answer_quality_measured": False}


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Expected a positive integer")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("search", "evaluate"))
    parser.add_argument("--corpus-root", type=Path, required=True)
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--query")
    parser.add_argument("--suite", type=Path)
    parser.add_argument("--mode", choices=("lexical", "semantic", "hybrid", "compare"), default="lexical")
    parser.add_argument("--embedding-config", type=Path)
    parser.add_argument("--top-k", type=positive_int, default=10)
    parser.add_argument("--max-file-bytes", type=positive_int, default=4 * 1024 * 1024)
    args = parser.parse_args(argv)
    started = time.monotonic()
    try:
        root = args.corpus_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Corpus root must be a directory")
        if args.operation == "search" and (args.query is None or args.suite is not None):
            raise ValueError("search requires --query and does not accept --suite")
        if args.operation == "evaluate" and (args.suite is None or args.query is not None):
            raise ValueError("evaluate requires --suite and does not accept --query")
        if args.mode == "compare" and args.operation != "evaluate":
            raise ValueError("compare is available only for evaluate")
        if (args.mode == "lexical") != (args.embedding_config is None):
            raise ValueError("Embedding config is required only for semantic, hybrid or compare")
        sources, scope_digest = load_scope(args.scope, root)
        adapter = EmbeddingAdapter(args.embedding_config, scope_digest) if args.embedding_config else None
        if adapter and args.top_k > adapter.config["rank_window"]:
            raise ValueError("top-k must not exceed embedding rank_window")
        documents, coverage = scan(root, sources, args.max_file_bytes)
        connection = make_index(documents)
        try:
            retriever = Retriever(connection, documents, adapter)
            if args.operation == "search":
                result = {"query": args.query, "results": retriever(args.query, args.top_k, args.mode)}
            else:
                suite_raw = args.suite.read_bytes()
                suite = json.loads(suite_raw.decode("utf-8"))
                cases = validated_cases(documents, suite)
                if adapter:
                    retriever.prepare([case["query"] for case in cases])
                modes = ("lexical", "semantic", "hybrid") if args.mode == "compare" else (args.mode,)
                variants = {mode: evaluate(connection, documents, suite, args.top_k,
                            lambda query, k, selected=mode: retriever(query, k, selected)) for mode in modes}
                result = {"variants": variants} if args.mode == "compare" else variants[args.mode]
                result["suite_sha256"] = hashlib.sha256(suite_raw).hexdigest()
        finally:
            connection.close()
        engines = {"lexical": "sqlite_fts5_unicode61_or_bm25", "semantic": "cosine",
                   "hybrid": "reciprocal_rank_fusion", "compare": "retrieval_comparison"}
        result.update({"result_version": 1, "engine": engines[args.mode],
                       "mode": args.mode, "elapsed_seconds": round(time.monotonic() - started, 3),
                       "scope_sha256": scope_digest,
                       "coverage": coverage, "persistent_cache": False})
        if adapter:
            result["embedding"] = adapter.summary()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, sqlite3.Error, RecursionError, OverflowError) as error:
        print("Search failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
