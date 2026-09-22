#!/usr/bin/env python3
"""A bounded Atom discovery adapter with durable, atomic JSON state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ATOM = "{http://www.w3.org/2005/Atom}"


class AtomError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atom_entries(data: bytes) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise AtomError(f"invalid Atom XML: {exc}") from exc
    if root.tag != ATOM + "feed":
        raise AtomError("document is not an Atom feed")
    entries: list[dict[str, str]] = []
    for entry in root.findall(ATOM + "entry"):
        value = lambda name: (entry.findtext(ATOM + name) or "").strip()
        links = [link.get("href", "") for link in entry.findall(ATOM + "link") if link.get("rel", "alternate") == "alternate"]
        item_id, title, updated = value("id"), value("title"), value("updated")
        if not item_id or not title or not updated or not links:
            raise AtomError("Atom entry lacks id, title, updated or canonical link")
        content = value("content") or value("summary")
        entries.append({"id": item_id, "title": title, "updated": updated, "canonical_link": links[0], "content": content})
    return entries


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(data, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def fetch(url: str, previous: dict[str, Any], timeout: float, maximum: int) -> tuple[int, bytes | None, dict[str, str]]:
    headers = {key: previous[key] for key in ("etag", "last_modified") if isinstance(previous.get(key), str)}
    request = urllib.request.Request(url, headers={"If-None-Match" if key == "etag" else "If-Modified-Since": value for key, value in headers.items()})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(maximum + 1)
            if len(data) > maximum:
                raise AtomError(f"response exceeds {maximum} bytes")
            return response.status, data, {"etag": response.headers.get("ETag", ""), "last_modified": response.headers.get("Last-Modified", "")}
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, None, {}
        raise AtomError(f"network unavailable: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AtomError(f"network unavailable: {exc}") from exc


def update(url: str, state_path: Path, *, fixture: Path | None, timeout: float, maximum: int) -> dict[str, Any]:
    previous = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {"entries": {}}
    try:
        if fixture is not None:
            data = fixture.read_bytes()
            if len(data) > maximum:
                raise AtomError(f"response exceeds {maximum} bytes")
            status, headers = 200, {}
        else:
            status, data, headers = fetch(url, previous, timeout, maximum)
        if status == 304:
            return {"status": "unchanged", "http_status": 304, "entries": [], "snapshot_sha256": previous.get("snapshot_sha256")}
        assert data is not None
        entries = atom_entries(data)
    except AtomError:
        # Do not overwrite the last known good index on an unavailable or bad feed.
        raise
    before = previous.get("entries", {})
    after = {entry["id"]: entry for entry in entries}
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(key for key in set(after) & set(before) if after[key] != before[key])
    state = {"url": url, "entries": after, "etag": headers.get("etag") or previous.get("etag"), "last_modified": headers.get("last_modified") or previous.get("last_modified"), "snapshot_sha256": sha256(data)}
    atomic_json(state_path, state)
    return {"status": "changed" if added or changed or removed else "unchanged", "http_status": 200, "added": added, "changed": changed, "removed": removed, "entries": entries, "snapshot_sha256": state["snapshot_sha256"]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--max-bytes", type=int, default=2_000_000)
    args = parser.parse_args()
    try:
        print(json.dumps(update(args.url, args.state, fixture=args.fixture, timeout=args.timeout, maximum=args.max_bytes), ensure_ascii=False, sort_keys=True))
    except AtomError as exc:
        print(json.dumps({"status": "fetch-error", "message": str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
