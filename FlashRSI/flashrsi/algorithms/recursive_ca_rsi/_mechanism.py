"""Frozen final-boundary interventions; learning tasks only, no model calls."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import signal
import sys
from dataclasses import replace
from pathlib import Path

from flashrsi.agents.miniagent.runtime.rule_audit import interpret_variants
from flashrsi.core.snapshot import _validate_path

ENTRY = Path(__file__).with_name("_rule_entry.py")


CASE_SELECTION = (
    "learning-strata-v1: regression, triggered, failure, unchanged; round-robin"
)


def collect_cases(snapshot, trajectories, limit, *, baseline=(), primary_metric=None):
    """Select within learning data using a fixed, recorded coverage policy.

    A changed answer is not necessarily harmful. Include no-op controls and
    failures as well, without consulting future audit or test outcomes.
    """
    cases, skipped = [], []
    previous = {row.task.task_id: row for row in baseline}
    for trajectory in trajectories:
        task_id = trajectory.task.task_id
        receipts = trajectory.usage.get("rule_replays", ())
        if not receipts:
            skipped.append(
                {"task_id": task_id, "reason": "no declared pure-rule boundary"}
            )
            continue
        receipt = receipts[-1]  # Earlier retries are not a replayable final answer.
        module = receipt.get("module_path")
        reason = None
        try:
            _validate_path(module)
            events = receipt["events"]
            if module not in snapshot.files or not module.endswith(".py"):
                raise ValueError("module absent from snapshot")
            if receipt.get("snapshot_id") != snapshot.id:
                raise ValueError("receipt snapshot does not match candidate")
            source_hash = hashlib.sha256(snapshot.files[module].content).hexdigest()
            if not events or any(
                e["rule"]["source_sha256"] != source_hash for e in events
            ):
                raise ValueError("rule source hash does not match pinned module")
            if (
                trajectory.status != "finished"
                or trajectory.error_message
                or receipt["output"] != trajectory.output
            ):
                raise ValueError("boundary is not the final successful output")
            if len(events) > 4:
                raise ValueError("more than four declared rules")
        except (ValueError, TypeError, KeyError) as error:
            reason = str(error)
        if reason:
            skipped.append({"task_id": task_id, "reason": reason})
            continue
        triggered = [e["rule"]["rule_id"] for e in events if e["before"] != e["after"]]
        old = previous.get(task_id)
        regressed = bool(
            old is not None
            and any(
                trajectory.metrics.get(metric, value) < value
                for metric, value in old.metrics.items()
                if primary_metric is None or metric == primary_metric
            )
        )
        stratum = (
            "regression"
            if regressed
            else "triggered"
            if triggered
            else "failure"
            if any(
                v == 0
                for k, v in trajectory.metrics.items()
                if primary_metric is None or k == primary_metric
            )
            else "unchanged"
        )
        cases.append(
            {
                "id": f"{task_id}::final-boundary",
                "task_id": task_id,
                "module_path": module,
                "rule_ids": [e["rule"]["rule_id"] for e in events],
                "answer": receipt["answer"],
                "evidence": copy.deepcopy(receipt["evidence"]),
                "observed_output": receipt["output"],
                "selection": {
                    "policy": CASE_SELECTION,
                    "stratum": stratum,
                    "triggered_rules": triggered,
                    "observed_regression": regressed,
                },
                "trajectory": trajectory,
            }
        )
    buckets = {
        name: [case for case in cases if case["selection"]["stratum"] == name]
        for name in ("regression", "triggered", "failure", "unchanged")
    }
    selected = []
    while len(selected) < limit and any(buckets.values()):
        for bucket in buckets.values():
            if bucket and len(selected) < limit:
                selected.append(bucket.pop(0))
    chosen = {case["id"] for case in selected}
    skipped.extend(
        {
            "task_id": case["task_id"],
            "reason": "fixed case budget reached",
            "selection": case["selection"],
        }
        for case in cases
        if case["id"] not in chosen
    )
    return selected, skipped


def visible_cases(cases):
    return [
        {key: value for key, value in case.items() if key != "trajectory"}
        for case in cases
    ]


async def _execute(snapshot, cases, hypotheses, working_dir):
    with snapshot.check_out(working_dir=working_dir) as root:
        request = {
            "root": str(root),
            "module_path": cases[0]["module_path"],
            "rule_ids": cases[0]["rule_ids"],
            # Reference labels stay outside candidate execution. Child
            # grades are placeholders and must be replaced by Benchmark.
            "cases": [
                {
                    "id": c["id"],
                    "answer": c["answer"],
                    "evidence": c["evidence"],
                    "expected": ["__controller_placeholder__"],
                }
                for c in cases
            ],
            "hypotheses": hypotheses,
            "provenance": {"snapshot_id": snapshot.id},
        }
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().endswith(("API_KEY", "TOKEN", "SECRET", "PASSWORD"))
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-B",
            str(ENTRY),
            cwd=root,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(json.dumps(request).encode()), timeout=15
            )
        finally:
            # Reap subprocess descendants as well, including on cancellation.
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.returncode is None:
                process.kill()
            await process.wait()
        if process.returncode:
            # Do not persist arbitrary subprocess stderr or environment details.
            raise ValueError(
                f"Rule replay process failed with status {process.returncode}"
            )
        return json.loads(stdout)


def regrade(report, cases, benchmark, hypotheses):
    """Use the registered task grader, never the child's placeholder grades."""
    for variant in report["variants"]:
        valid_pairs = [
            (case, row) for case, row in zip(cases, variant["cases"]) if row["valid"]
        ]
        if not valid_pairs:
            variant["score"] = None
            continue
        proposed = [
            replace(case["trajectory"], output=row["output"])
            for case, row in valid_pairs
        ]
        graded = tuple(benchmark.grade(proposed))
        if tuple(row.task for row in graded) != tuple(row.task for row in proposed):
            raise ValueError("Boundary grading must preserve task order")
        scores = [row.metrics[benchmark.primary_metric] for row in graded]
        if any(score not in (0, 1) for score in scores):
            raise ValueError(
                "Rule verification currently requires binary primary task grades"
            )
        for (case, row), score in zip(valid_pairs, scores):
            row["correct"] = bool(score)
        variant["score"] = sum(scores) / len(scores) if variant["valid"] else None
    report.update(
        interpret_variants(report["variants"], cases[0]["rule_ids"], hypotheses)
    )
    report["grading"] = "registered_benchmark_binary_primary_metric"
    report["primary_metric"] = benchmark.primary_metric
    return report


async def audit(algorithm, snapshot, cases, hypotheses, *, require_reproduction):
    groups = {}
    for case in cases:
        groups.setdefault((case["module_path"], tuple(case["rule_ids"])), []).append(
            case
        )
    reports = []
    for (module, rule_ids), group in groups.items():
        requests = [
            h for h in hypotheses if h.get("case_id") in {c["id"] for c in group}
        ]
        try:
            report = await _execute(
                snapshot, group, requests, algorithm._run_dir / "working"
            )
            if require_reproduction and any(
                not row["valid"] or row["output"] != case["observed_output"]
                for case, row in zip(group, report["variants"][-1]["cases"])
            ):
                raise ValueError(
                    "Full pipeline failed to reproduce the recorded boundary"
                )
            report = regrade(report, group, algorithm.benchmark, requests)
            reports.append(
                {"module_path": module, "status": "executed", "report": report}
            )
        except (ValueError, RuntimeError, OSError, TimeoutError) as error:
            reports.append(
                {"module_path": module, "status": "unavailable", "problem": str(error)}
            )
    return {
        "snapshot_id": snapshot.id,
        "status": "executed" if reports else "unsupported",
        "scope": "learning_only_fixed_final_boundary_not_end_to_end_causality",
        "groups": reports,
    }


def comparison(before, after):
    original = {}
    repaired = {}
    for evidence, destination in ((before, original), (after, repaired)):
        for group in evidence["groups"]:
            if group["status"] != "executed":
                return {
                    "valid": False,
                    "problem": "one or more rule modules could not be replayed",
                }
            for row in group["report"]["variants"][-1]["cases"]:
                if not row["valid"]:
                    return {"valid": False, "problem": "full rule pipeline invalid"}
                destination[row["id"]] = row["correct"]
    if not original or set(original) != set(repaired):
        return {"valid": False, "problem": "no comparable frozen boundary cases"}
    return {
        "valid": True,
        "n": len(original),
        "before_score": sum(original.values()) / len(original),
        "after_score": sum(repaired.values()) / len(repaired),
        "regressions": [cid for cid in original if original[cid] and not repaired[cid]],
    }
