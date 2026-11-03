import runpy
from pathlib import Path

service = runpy.run_path(str(Path(__file__).resolve().parents[1] / "src/service.py"))
assert service["greeting"]("Ada") == {"greeting": "Hello, Ada"}