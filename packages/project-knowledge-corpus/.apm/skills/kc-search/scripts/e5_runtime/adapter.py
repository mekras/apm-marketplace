"""Read one embedding request and run the pinned local ONNX model on CPU."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.dont_write_bytecode = True
from e5_runtime import MODEL, REVISION, VERSIONS, verify


def validate_request(request):
    if not isinstance(request, dict) or request.get("embedding_protocol_version") != 1:
        raise ValueError("Expected embedding protocol version 1")
    if request.get("model") != MODEL or request.get("revision") != REVISION:
        raise ValueError("E5 model or revision mismatch")
    rows = request.get("inputs")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 256:
        raise ValueError("Expected 1..256 inputs")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise ValueError("Expected string input id")
        if row["id"] in seen:
            raise ValueError("Duplicate input id")
        seen.add(row["id"])
        text = row.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 4224:
            raise ValueError("Invalid E5 input text")
        if not text.startswith(("query: ", "passage: ")):
            raise ValueError("E5 requires query: or passage: prefix")
    return rows


def encode(rows, directory):
    for package, expected in VERSIONS.items():
        if importlib.metadata.version(package) != expected:
            raise ValueError("Unsupported E5 runtime version: " + package)
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
    tokenizer.no_truncation()
    tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
    encoded = tokenizer.encode_batch([row["text"] for row in rows])
    if any(len(row.ids) > 512 for row in encoded):
        raise ValueError("E5 input exceeds 512 tokens; reduce max_input_chars")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(directory / "model_quantized.onnx"),
                                   sess_options=options, providers=["CPUExecutionProvider"])
    feed = {"input_ids": np.asarray([e.ids for e in encoded], dtype=np.int64),
            "attention_mask": np.asarray([e.attention_mask for e in encoded], dtype=np.int64),
            "token_type_ids": np.asarray([e.type_ids for e in encoded], dtype=np.int64)}
    hidden = session.run(None, feed)[0]
    mask = feed["attention_mask"][..., None]
    vectors = (hidden * mask).sum(axis=1) / mask.sum(axis=1)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if vectors.shape != (len(rows), 384) or not np.isfinite(vectors).all() or (norms == 0).any():
        raise ValueError("Invalid E5 output vectors")
    return (vectors / norms).tolist()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if os.environ.get("KC_EMBEDDING_NETWORK") != "prohibited":
            raise ValueError("Use this adapter through the configured search command")
        rows = validate_request(json.load(sys.stdin))
        verify(args.model_dir)
        vectors = encode(rows, args.model_dir)
        json.dump({"embedding_protocol_version": 1, "model": MODEL, "revision": REVISION,
                   "embeddings": [{"id": row["id"], "values": vector}
                                  for row, vector in zip(rows, vectors)]},
                  sys.stdout, ensure_ascii=False, allow_nan=False)
    except Exception:
        # Do not echo input texts, third-party exceptions or filesystem contents.
        print("E5 adapter failed: check runtime versions, model hashes and input limits", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
