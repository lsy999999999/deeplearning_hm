"""Recovery must append only unfinished work and retain original decisions."""

import asyncio
import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import export_evolution
import resume_meta_harness as recovery
from run_experiment import configured


def test_resume_does_not_repeat_completed_proposals_or_dev_calls(tmp_path, monkeypatch):
    import flashrsi.runner as native

    path = ROOT / "FlashRSI/tests/test_meta_harness.py"
    spec = importlib.util.spec_from_file_location("meta_recovery_toy", path)
    toy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(toy)
    edits = []

    def before_failure(prompt, directory):
        edits.append("original")
        if len(edits) == 2:
            raise RuntimeError("Connection error")
        (directory / "skills/main.md").write_text("GOOD")

    algorithm, calls = toy.make_setup(
        monkeypatch, tmp_path, before_failure, max_steps=3
    )
    algorithm.benchmark._splits["dev"] = tuple(
        replace(task, metadata={"answers": ["one", "two"]})
        for task in algorithm.benchmark.dev
    )
    directory = toy.attach_run(algorithm, tmp_path)
    with pytest.raises(RuntimeError, match="Connection error"):
        asyncio.run(algorithm.evolve())
    assert len(calls) == 4
    (directory / "error.json").write_text(
        json.dumps({"status": "error", "error": "connection"})
    )
    config = configured(
        ROOT / "configs/meta_harness_hotpotqa_pilot.yaml", {}, require_key=False
    )
    from flashrsi._records import _redact_config

    config.algorithm.max_steps = 3
    config.agent.llm.api_key = None
    config.algorithm.worker.llm.api_key = None
    (directory / "config.yaml").write_text(yaml.safe_dump(_redact_config(config)))

    async def prepare(config):
        return algorithm

    remaining = iter(["WORSE", "GREAT"])

    def after_failure(prompt, folder):
        edits.append("resumed")
        (folder / "skills/main.md").write_text(next(remaining))

    algorithm.worker.apply_edit = after_failure
    monkeypatch.setattr(native, "_prepare", prepare)
    monkeypatch.setattr(export_evolution, "refresh", lambda directory: None)
    asyncio.run(recovery.resume(directory, {"apikey": "test-only-secret"}))
    assert edits == ["original", "original", "resumed", "resumed"]
    assert len(calls) == 9  # Only two pending candidates and final test were added.
    assert (directory / "error.json").exists()
    assert json.loads((directory / "result.json").read_text())["resumed_after_error"]
    records = [
        json.loads(p.read_text())
        for p in (directory / "trajectories").glob("*-decision.json")
    ]
    assert [
        r["step"]
        for r in sorted(records, key=lambda r: r.get("step", 99))
        if r["kind"] == "admit"
    ] == [0, 1, 2]
    assert (
        json.loads((directory / "execution.json").read_text())["elapsed_seconds"]
        is None
    )


def test_recovery_rejects_gaps_in_completed_history(tmp_path):
    directory = tmp_path / "run"
    (directory / "trajectories").mkdir(parents=True)
    for n, label, data in [
        (1, "baseline", {"snapshot_id": "seed"}),
        (2, "scores", {"snapshot_id": "seed", "score": 0}),
        (3, "decision", {"kind": "decline", "step": 1}),
    ]:
        (directory / "trajectories" / f"{n:05}-{label}.json").write_text(
            json.dumps(data)
        )
    with pytest.raises(ValueError, match="contiguous"):
        recovery.completed_state(directory)
