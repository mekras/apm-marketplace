"""Pinned optional CPU profile. Importing this module needs only Python."""
import hashlib
from pathlib import Path

MODEL = "Xenova/multilingual-e5-small"
REVISION = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"
VERSIONS = {"numpy": "2.2.2", "onnxruntime": "1.26.0", "tokenizers": "0.22.2"}
FILES = {
    "model_quantized.onnx": ("onnx/model_quantized.onnx",
        "f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193"),
    "tokenizer.json": ("tokenizer.json",
        "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39"),
}


def digest(path):
    with Path(path).open("rb") as stream:
        checksum = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
        return checksum.hexdigest()


def verify(directory):
    for name, (_, expected) in FILES.items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise ValueError("Missing or changed E5 model file: " + name)
