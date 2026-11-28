"""Historical exposure includes nested task records and both content identities."""

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from run_fixed_meta_case import identities


def test_nested_exposure_keeps_content_for_changed_task_ids():
    ids, hashes = set(), set()
    identities(
        {
            "trajectories": [
                {
                    "task": {
                        "task_id": "old",
                        "question": "same text",
                        "metadata": {"question_checksum": "normalized-question-hash"},
                    }
                }
            ]
        },
        ids,
        hashes,
    )
    assert ids == {"old"}
    assert hashlib.sha256(b"same text").hexdigest() in hashes
    assert "normalized-question-hash" in hashes


def test_public_evidence_omits_context_but_keeps_rule_results():
    from public_evidence import public_evidence

    raw = {
        "question": "private context",
        "output": "answer",
        "events": [{"action": "replace", "before": "old", "after": "new"}],
        "evaluations": [{"messages": ["private trace"]}],
    }
    exported = public_evidence(raw)
    assert "question" not in exported and "evaluations" not in exported
    assert exported["events"] == raw["events"]
    assert exported["output"] == "answer"
    assert public_evidence(exported) == exported
