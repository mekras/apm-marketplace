#!/usr/bin/env python3
import json
import os
mode = os.environ["KC_EVALUATION_ACCESS"]
if mode == "statements":
    result = {"representation": "present", "statement_ids": ["RELEASE-001"], "paths": ["data/release/statements.yml"], "rationale": "fixture"}
elif mode == "judge":
    result = {"correctness": "pass", "groundedness": "pass", "target_expressed": True, "forbidden_claim_detected": False}
elif mode == "closed_book":
    result = {"answer": "unknown", "abstained": True, "evidence": []}
else:
    result = {"answer": "AGENTS.md applies when CLAUDE.md is absent.", "abstained": False, "evidence": [{"path": "data/release/statements.yml", "statement_id": "RELEASE-001", "locator": "RELEASE-001"}]}
print(json.dumps(result))
