"""Keep execution evidence public without copying dataset contexts or raw traces."""

import hashlib
import json


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def public_evidence(value):
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if key in {"messages", "trajectories", "evaluations"}:
                result[key + "_omitted_sha256"] = digest(child)
            elif key == "question":
                result["question_omitted_sha256"] = digest(child)
            else:
                result[key] = public_evidence(child)
        return result
    if isinstance(value, list):
        return [public_evidence(child) for child in value]
    if isinstance(value, str) and "Context:\n" in value:
        return "[dataset context omitted; sha256=" + digest(value) + "]"
    return value
