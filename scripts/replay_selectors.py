"""Compare selectors on the SAME recorded fresh-dev counterfactual matrix.

This does not emulate new proposal histories, held-out testing, or retention.
No model calls are made. Consulted variants are not API-call or token costs.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from statistics import mean


def compare(audit, max_regressions=0):
    if max_regressions < 0:
        raise ValueError("max_regressions must be nonnegative")
    paths = audit["changed_paths"]
    task_ids = audit["task_ids"]
    if (
        not paths
        or len(paths) != len(set(paths))
        or not task_ids
        or len(task_ids) != len(set(task_ids))
    ):
        raise ValueError("Expected nonempty unique edit paths and task IDs")
    variants = {}
    for row in audit["variants"]:
        selected = row["paths"]
        if len(selected) != len(set(selected)) or not set(selected) <= set(paths):
            raise ValueError("Invalid variant paths")
        mask = sum(1 << paths.index(path) for path in selected)
        if mask in variants:
            raise ValueError("Duplicate variant mask")
        if set(row["per_task_delta"]) != set(task_ids):
            raise ValueError("Unpaired task IDs in audit matrix")
        if not all(
            math.isfinite(x) for x in [row["score"], *row["per_task_delta"].values()]
        ):
            raise ValueError("Nonfinite audit score")
        if row["regressions"] != sum(x < 0 for x in row["per_task_delta"].values()):
            raise ValueError("Regression count disagrees with task deltas")
        variants[mask] = row
    full = (1 << len(paths)) - 1
    if 0 not in variants:
        raise ValueError("Baseline must be evaluated")
    full_invalid = any(
        set(row["paths"]) == set(paths) for row in audit.get("invalid_variants", [])
    )
    if full not in variants and not full_invalid:
        raise ValueError("Full candidate must be evaluated or explicitly invalid")
    base_score = variants[0]["score"]
    for row in variants.values():
        if not math.isclose(
            mean(row["per_task_delta"].values()),
            row["score"] - base_score,
            abs_tol=1e-9,
        ):
            raise ValueError("Aggregate score disagrees with paired deltas")
    if any(variants[0]["per_task_delta"].values()):
        raise ValueError("Baseline must have zero task deltas")

    def best(masks):
        return max(
            masks, key=lambda mask: (variants[mask]["score"], -mask.bit_count(), -mask)
        )

    greedy, consulted = 0, {0}
    while True:
        neighbors = [
            greedy | (1 << index)
            for index in range(len(paths))
            if not greedy & (1 << index) and greedy | (1 << index) in variants
        ]
        consulted.update(neighbors)
        choice = best([greedy, *neighbors])
        if variants[choice]["score"] <= variants[greedy]["score"]:
            break
        greedy = choice

    eligible = [
        mask
        for mask, row in variants.items()
        if row["regressions"] <= max_regressions and row["score"] > base_score
    ]
    selections = {
        "seed": (0, {0}),
        "full_if_improved": (
            full if full in variants and variants[full]["score"] > base_score else 0,
            {0, full} & set(variants),
        ),
        "greedy": (greedy, consulted),
        "ca_fresh_dev": (best(eligible) if eligible else 0, set(variants)),
    }
    if full in variants:
        selections = {
            "seed": selections.pop("seed"),
            "accept_full": (full, {0, full}),
            **selections,
        }
    rows = []
    for name, (mask, used) in selections.items():
        row = variants[mask]
        conditional = [
            row["score"] - variants[mask ^ (1 << i)]["score"]
            for i in range(len(paths))
            if mask & (1 << i) and mask ^ (1 << i) in variants
        ]
        rows.append(
            {
                "selector": name,
                "snapshot_id": row["snapshot_id"],
                "selected_paths": row["paths"],
                "dev_score": row["score"],
                "dev_gain": row["score"] - base_score,
                "regressed_tasks": row["regressions"],
                "tasks": len(task_ids),
                "regression_rate": row["regressions"] / len(task_ids),
                "conditional_harmful_units": sum(x < 0 for x in conditional),
                "conditional_comparable_units": len(conditional),
                "consulted_variants": len(used),
            }
        )
    return {
        "step": audit["step"],
        "baseline_id": audit["baseline_id"],
        "candidate_id": audit["candidate_id"],
        "matrix_variants": len(variants),
        "invalid_variants": len(audit.get("invalid_variants", [])),
        "scope": "paired_fresh_dev_only_no_retention_no_test_no_new_proposals",
        "selections": rows,
        "unavailable_selectors": []
        if full in variants
        else [
            {
                "selector": "accept_full",
                "reason": "full candidate invalid; no valid score",
            }
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--max-regressions", type=int, default=0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    audits = []
    skipped = []
    for path in sorted((args.run_dir / "trajectories").glob("*-audit.json")):
        audit = json.loads(path.read_text())
        if "variants" not in audit:
            skipped.append({"file": path.name, "status": audit.get("status")})
        else:
            audits.append(compare(audit, args.max_regressions))
    result = {"run_dir": str(args.run_dir), "audits": audits, "skipped": skipped}
    output = args.output or args.run_dir / "selector-comparison.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(output)


if __name__ == "__main__":
    main()
