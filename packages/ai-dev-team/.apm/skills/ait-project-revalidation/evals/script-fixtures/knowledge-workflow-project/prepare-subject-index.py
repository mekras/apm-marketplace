#!/usr/bin/env python3
"""Проверяет полный состав документов через публичные команды индекса."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


script = sys.argv[1]
repo = Path(sys.argv[2])
documents = {
    "docs/requirements.md": (
        "# Требования\n\n"
        "- [Концепция](concept.md)\n"
        "- [Второе](requirements/second.md)\n"
        "- [Повтор концепции](concept.md#details)\n"
        "- [Внешний документ](https://example.invalid/external.md)\n"
    ),
    "docs/requirements/second.md": "# Второе требование\n",
    "docs/requirements/unlinked.md": "# Документ вне оглавления\n",
}
for reference, content in documents.items():
    path = repo / reference
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

subprocess.run(
    [sys.executable, "prepare.py", script, str(repo), "semantic"],
    check=True,
    cwd=repo,
)
