"""Static provider: one source is observed, the other is unavailable in this environment."""

import json
import sys

operation, source_id = sys.argv[1:]
status = "ready" if operation == "probe" else "unchanged"
message = "Static provider observed."
if source_id == "OFFLINE" and operation == "probe":
    status = "technical-unavailable"
    message = "Provider host is unreachable from this network."
result = {
    "contract_version": 2, "operation": operation, "source_id": source_id,
    "adapter": "fixture.provider", "status": status, "message": message, "artifacts": [],
}
if operation == "index":
    result["observation"] = {
        "scope": "card-navigation-v1", "complete": True,
        "evidence": ["Static provider navigation fixture"],
        "units": {"CARD-X": {"locator": "fixture://provider/cards/x", "fingerprint": "a" * 64}},
    }
print(json.dumps(result))
