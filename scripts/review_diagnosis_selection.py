"""Review frozen learning records under the new selection/gates; zero model calls.

This is a new retrospective analysis, never a rewrite or rerun of the old RSI
experiment. It does not execute candidate code or inspect final-test records.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "FlashRSI"))

from flashrsi.algorithms.recursive_ca_rsi import RecursiveCARSI, _mechanism
from flashrsi.core import Snapshot, Task, Trajectory


def learning_record(path):
    if not path.stem.endswith(("-repair_baseline", "-repair_candidate", "-train")):
        raise ValueError(
            "Only native learning records are supported; do not open final tests"
        )
    value = json.loads(path.read_text())
    rows = [
        Trajectory(**{**row, "task": Task(**row["task"])})
        for row in value["trajectories"]
    ]
    return value, rows


def review(
    snapshot_path,
    baseline_path,
    candidate_path,
    *,
    limit=4,
    metric="exact_match",
    diagnosis_path=None,
):
    if not 1 <= limit <= 16:
        raise ValueError("limit must be between 1 and 16")
    snapshot = Snapshot.load(snapshot_path)
    before, baseline = learning_record(baseline_path)
    after, candidate = learning_record(candidate_path)
    if tuple(row.task for row in baseline) != tuple(row.task for row in candidate):
        raise ValueError(
            "Baseline and candidate must contain the same ordered learning tasks"
        )
    if after["snapshot_id"] != snapshot.id:
        raise ValueError("Candidate record does not match the pinned snapshot")
    cases, skipped = _mechanism.collect_cases(
        snapshot, candidate, limit, baseline=baseline, primary_metric=metric
    )
    paths = [snapshot_path, baseline_path, candidate_path]
    diagnosis_check = None
    if diagnosis_path is not None:
        if not diagnosis_path.stem.endswith("-diagnosis"):
            raise ValueError("Expected a native diagnosis record")
        paths.append(diagnosis_path)
        baseline_snapshot_path = snapshot_path.with_name(
            before["snapshot_id"] + ".json"
        )
        baseline_snapshot = Snapshot.load(baseline_snapshot_path)
        paths.append(baseline_snapshot_path)
        diagnosis = json.loads(diagnosis_path.read_text())
        try:
            if diagnosis.get("candidate_id") != snapshot.id:
                raise ValueError(
                    "Diagnosis candidate does not match the pinned snapshot"
                )
            RecursiveCARSI._diagnosis(
                json.dumps(diagnosis),
                {
                    "strict_rule_diagnosis": True,
                    "candidate_snapshot_id": snapshot.id,
                    "changed_paths": baseline_snapshot.changed_paths(snapshot),
                    "before": [
                        {"task": {"task_id": row.task.task_id}} for row in baseline
                    ],
                    "after": [
                        {"task": {"task_id": row.task.task_id}, "output": row.output}
                        for row in candidate
                    ],
                    "rule_cases": _mechanism.visible_cases(cases),
                },
            )
            diagnosis_check = {
                "status": "schema_valid_only",
                "repair_permitted": False,
                "reason": "New controller interventions are still required",
            }
        except ValueError as error:
            diagnosis_check = {
                "status": "rejected_by_new_gate",
                "repair_permitted": False,
                "reason": str(error),
            }
    return {
        "scope": "retrospective_learning_selection_only_not_new_model_experiment",
        "model_calls": 0,
        "candidate_code_executed": False,
        "candidate_snapshot_id": snapshot.id,
        "baseline_snapshot_id": before["snapshot_id"],
        "policy": _mechanism.CASE_SELECTION,
        "limit": limit,
        "metric": metric,
        "selected": [
            {
                "task_id": c["task_id"],
                "input_answer": c["answer"],
                "observed_output": c["observed_output"],
                **c["selection"],
            }
            for c in cases
        ],
        "skipped": skipped,
        "diagnosis_check": diagnosis_check,
        "sources": [
            {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in paths
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--diagnosis", type=Path)
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Output already exists; preserve it and choose a new path")
    result = review(
        args.snapshot,
        args.baseline,
        args.candidate,
        limit=args.limit,
        diagnosis_path=args.diagnosis,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"Saved retrospective selection review; zero model calls: {args.output}")


if __name__ == "__main__":
    main()
