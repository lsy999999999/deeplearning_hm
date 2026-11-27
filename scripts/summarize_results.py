"""Collect actual native results and canonical usage, including failed runs."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "FlashRSI"))


def summarize(directory):
    import yaml
    from flashrsi._run_summary import _collect_usage

    config = yaml.safe_load((directory / "config.yaml").read_text())
    result_file = directory / "result.json"
    error_file = directory / "error.json"
    result = json.loads(result_file.read_text()) if result_file.exists() else {}
    error = json.loads(error_file.read_text()) if error_file.exists() else {}
    usage = _collect_usage(directory)["total"]
    timing_file = directory / "execution.json"
    timing = json.loads(timing_file.read_text()) if timing_file.exists() else {}
    deltas = []
    accepted = 0
    for path in (directory / "trajectories").glob("*-audit.json"):
        audit = json.loads(path.read_text())
        if "variants" not in audit:
            continue
        chosen = next(
            v for v in audit["variants"] if v["snapshot_id"] == audit["selected_id"]
        )
        deltas.extend(chosen["per_task_delta"].values())
        accepted += bool(audit["selected_paths"])
    return {
        "run": directory.name,
        "algorithm": config["algorithm"]["name"],
        "benchmark": config["benchmark"]["name"],
        "target_model": config["agent"]["llm"]["model_name"],
        "reasoning_effort": config["agent"]["llm"].get("reasoning_effort"),
        "thinking_mode": config["agent"]["llm"]
        .get("extra_body", {})
        .get("thinking", {})
        .get("type"),
        "unlimited_output": config["agent"]["llm"].get("unlimited_output", False),
        "worker_model": config["algorithm"]
        .get("worker", {})
        .get("llm", {})
        .get("model_name"),
        "seed": config["benchmark"].get("seed"),
        "status": result.get("status", error.get("status", "incomplete")),
        "primary_metric": result.get("primary_metric"),
        "snapshot_id": result.get("snapshot_id"),
        "test_score": result.get("test_score"),
        "test_tasks": result.get("test_tasks"),
        "elapsed_seconds": timing.get("elapsed_seconds"),
        "accepted_audit_rounds": accepted,
        "audited_task_occurrences": len(deltas),
        "regressed_task_occurrences": sum(d < 0 for d in deltas),
        "regression_rate": sum(d < 0 for d in deltas) / len(deltas) if deltas else None,
        **{
            k: usage[k]
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "usage_entries",
                "usage_entries_missing_tokens",
                "failed_records",
                "sdk_cost_usd",
            )
        },
        "error": error.get("error"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "results/run-summary.csv")
    args = parser.parse_args()
    directories = sorted(
        {
            p.parent
            for p in args.root.rglob("config.yaml")
            if (p.parent / "trajectories").is_dir()
        }
    )
    rows = [summarize(directory) for directory in directories]
    if not rows:
        raise SystemExit("No native run directories found")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(args.output)


if __name__ == "__main__":
    main()
