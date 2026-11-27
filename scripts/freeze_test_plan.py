"""Freeze a one-round shared-candidate choice before evaluating held-out tasks."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import yaml
from replay_selectors import compare


def freeze(directory, output, container_image=None):
    audits = sorted((directory / "trajectories").glob("*-audit.json"))
    if len(audits) != 1:
        raise ValueError("Shared-candidate test plans require exactly one audit round")
    source = audits[0].read_bytes()
    comparison = compare(json.loads(source))
    config = yaml.safe_load((directory / "config.yaml").read_text())
    selections = comparison["selections"]
    # Deduplicate identical snapshots across selectors; each unique snapshot gets
    # one test evaluation. The native CA-RSI result, if already present, is reused.
    native_file = directory / "result.json"
    native = json.loads(native_file.read_text()) if native_file.exists() else {}
    entries = []
    pending = []
    output.mkdir(parents=True, exist_ok=True)
    for identity in sorted({row["snapshot_id"] for row in selections}):
        snapshot = directory / "snapshots" / f"{identity}.json"
        if not snapshot.is_file():
            raise ValueError(f"Missing frozen snapshot: {identity}")
        entry = {
            "snapshot_id": identity,
            "selectors": [
                row["selector"] for row in selections if row["snapshot_id"] == identity
            ],
        }
        if native.get("status") == "finished" and native.get("snapshot_id") == identity:
            entry["reuse_native_result"] = str(native_file.resolve())
        else:
            evaluation = {
                "run": {
                    "mode": "evaluate",
                    "snapshot": str(snapshot.resolve()),
                    "output_dir": str(
                        (directory.parent / "shared-candidate-tests").resolve()
                    ),
                },
                "benchmark": config["benchmark"],
                "agent": config["agent"],
            }
            evaluation["agent"]["llm"]["api_key"] = None
            if container_image:
                evaluation["agent"]["container_image"] = container_image
            path = output / f"test-{identity}.yaml"
            pending.append(
                (path, yaml.safe_dump(evaluation, allow_unicode=True, sort_keys=False))
            )
            entry["config"] = str(path.resolve())
        entries.append(entry)
    plan = {
        "protocol": "one_round_shared_candidate_fresh_dev_choices_then_frozen_test",
        "audit_sha256": hashlib.sha256(source).hexdigest(),
        "selection_uses_test_metrics": False,
        "evaluation_container_image_override": container_image,
        "selections": selections,
        "evaluations": entries,
    }
    plan_file = output / "test-plan.json"
    # Refuse to overwrite choices after someone has begun test evaluation.
    if plan_file.exists() and json.loads(plan_file.read_text()) != plan:
        raise ValueError("Refusing to change an existing frozen test plan")
    for path, content in pending:
        path.write_text(content)
    plan_file.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    return plan_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--container-image")
    args = parser.parse_args()
    print(freeze(args.run_dir, args.output, args.container_image))


if __name__ == "__main__":
    main()
