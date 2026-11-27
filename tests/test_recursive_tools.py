"""Ensure recursive traces and structurally invalid candidates remain usable."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from replay_selectors import compare
from run_experiment import apply_overrides, build_parser, configured


def test_invalid_full_candidate_can_be_replayed_without_inventing_a_score():
    data = {
        "step": 0,
        "baseline_id": "seed",
        "candidate_id": "broken",
        "changed_paths": ["a.py"],
        "task_ids": ["t"],
        "variants": [
            {
                "paths": [],
                "snapshot_id": "seed",
                "score": 1.0,
                "per_task_delta": {"t": 0.0},
                "regressions": 0,
            }
        ],
        "invalid_variants": [{"paths": ["a.py"], "problem": "syntax error"}],
    }
    result = compare(data)
    assert all(row["snapshot_id"] == "seed" for row in result["selections"])
    assert result["unavailable_selectors"][0]["selector"] == "accept_full"
    data["invalid_variants"] = []
    with pytest.raises(ValueError, match="explicitly invalid"):
        compare(data)


def test_recursive_launcher_validates_reserved_meta_training_budget():
    args = build_parser().parse_args(
        [
            "--config",
            str(ROOT / "configs/recursive_ca_rsi_hotpotqa.yaml"),
            "--rounds",
            "4",
            "--train-size",
            "8",
            "--dry-run",
        ]
    )
    config = configured(args.config, {}, require_key=False)
    with pytest.raises(ValueError, match="meta_train_tasks_per_step"):
        apply_overrides(config, args)


def test_export_keeps_task_and_meta_versions_separate(tmp_path):
    test_path = ROOT / "FlashRSI/tests/test_recursive_ca_rsi.py"
    spec = importlib.util.spec_from_file_location("recursive_toy_fixture", test_path)
    toy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(toy)
    import asyncio

    import yaml
    from export_evolution import export, load_run

    algorithm = toy.make(tmp_path, repair=True, repair_attempts=1)
    final = asyncio.run(algorithm.evolve())
    directory = algorithm._run_dir
    (directory / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "agent": {"llm": {"model_name": "offline"}},
                "algorithm": {"name": "recursive_ca_rsi", "max_steps": 2},
                "benchmark": {"seed": 43},
            }
        )
    )
    (directory / "result.json").write_text(
        json.dumps(
            {
                "status": "finished",
                "primary_metric": "accuracy",
                "snapshot_id": final.id,
                "worker_snapshot_id": algorithm.worker.snapshot.id,
            }
        )
    )
    result = load_run(directory)
    assert result["final_id"] == final.id
    assert result["final_worker_id"] == algorithm.worker.snapshot.id
    assert result["final_id"] != result["final_worker_id"]
    assert len(result["meta_updates"]) == 2
    assert result["meta_updates"][0]["accepted"] is True
    assert result["diagnoses"] and result["repairs"]
    # Also exercise the real offline HTML exporter, including Meta UI fields.
    output = tmp_path / "viewer.html"
    export([directory], output)
    assert "Meta 的诊断、修复与自身更新" in output.read_text()
