"""Static provider used by the public CLI contract fixture."""

import json
import sys

operation = sys.argv[1]
result = {
    "contract_version": 2, "operation": operation, "source_id": "FIXTURE",
    "adapter": "fixture.provider", "status": "ready" if operation == "probe" else "changed",
    "message": "Static provider observed.", "artifacts": [],
}
if operation == "index":
    result["observation"] = {
        "scope": "card-navigation-v1", "complete": True,
        "evidence": ["Static provider navigation fixture"],
        "units": {"CARD-X": {"locator": "fixture://provider/cards/x", "fingerprint": "a" * 64}},
    }
print(json.dumps(result))
