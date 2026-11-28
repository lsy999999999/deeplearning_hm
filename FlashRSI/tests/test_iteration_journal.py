"""Actual edits must not be confused with explanations or later adoption."""

import asyncio
import json

from test_recursive_ca_rsi import make, records

from flashrsi.algorithms.meta_harness._journal import changes, fence


def test_missing_or_duplicate_reasons_are_not_invented(tmp_path):
    algorithm = make(tmp_path)
    before = algorithm.target.snapshot
    with before.check_out() as directory:
        (directory / "skills/policy.md").write_text("NEW")
        after = before.commit(directory)
    assert (
        changes(before, after, "whole proposal rationale")["changes"][0]["reason"]
        is None
    )
    block = (
        "<CHANGE_REASONS>"
        + json.dumps(
            {
                "files": [
                    {"path": "skills/policy.md", "reason": "first"},
                    {"path": "skills/policy.md", "reason": "second"},
                ]
            }
        )
        + "</CHANGE_REASONS>"
    )
    result = changes(before, after, block)
    assert result["reason_metadata_problem"]
    assert result["reason_coverage"]["mapped"] == 0
    assert fence("```\ncontent").startswith("````text")


def test_task_meta_and_shadow_changes_have_distinct_version_links(tmp_path):
    algorithm = make(tmp_path)
    asyncio.run(algorithm.evolve())
    changes_ = records(algorithm, "code_change")
    assert {row["subject"] for row in changes_} == {"Task", "Meta", "ShadowTask"}
    meta = next(row for row in changes_ if row["subject"] == "Meta")
    decision = records(algorithm, "meta_decision")[0]
    assert meta["parent_id"] == decision["worker_before_id"]
    assert meta["candidate_id"] == decision["worker_candidate_id"]
    next_task = next(
        row for row in changes_ if row["subject"] == "Task" and row["step"] == 1
    )
    assert decision["accepted"]
    assert next_task["worker_snapshot_id"] == decision["worker_candidate_id"]
    text = (algorithm._run_dir / "RSI_ITERATIONS.md").read_text()
    for row in changes_:
        assert row["candidate_id"] in text
        assert row["reason_coverage"]["mapped"] == 0
    assert "未单独记录" in text and "Meta 自身换版决定" in text
