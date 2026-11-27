"""Reuse unchanged target evaluations after a failed one-round CA-RSI run."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

import yaml


@contextmanager
def reuse_evaluations(config, source):
    from flashrsi._records import _redact_config
    from flashrsi.algorithms.ca_rsi import CARSI
    from flashrsi.core import Trajectory
    from flashrsi.core.trajectory import Results

    if source is None:
        yield
        return
    source = Path(source).resolve()
    if config.algorithm.name != "ca_rsi" or config.algorithm.max_steps != 1:
        raise ValueError("Recovery currently requires a single-round CA-RSI run")
    if not (source / "error.json").is_file() or (source / "result.json").exists():
        raise ValueError("Reuse is limited to failed runs with no final test result")
    old = yaml.safe_load((source / "config.yaml").read_text())
    current = _redact_config(config)
    if current["agent"] != old["agent"] or current["benchmark"] != old["benchmark"]:
        raise ValueError(
            "Cannot reuse evaluations after target settings or data changed"
        )
    cached = {}
    for path in sorted((source / "trajectories").glob("*.json")):
        label = path.stem.split("-", 1)[-1]
        if label not in {"train", "audit_baseline"}:
            continue
        record = json.loads(path.read_text())
        rows = record.get("trajectories", [])
        if not rows or any(row["status"] != "finished" for row in rows):
            continue
        key = (
            record["snapshot_id"],
            label,
            tuple(row["task"]["task_id"] for row in rows),
        )
        cached[key] = (path, record)
    original = CARSI._evaluate

    async def evaluate(algorithm, snapshot, tasks, *, label="evaluation"):
        tasks = tuple(tasks)
        key = (snapshot.id, label, tuple(task.task_id for task in tasks))
        if key not in cached:
            return await original(algorithm, snapshot, tasks, label=label)
        path, record = cached[key]
        rows = record["trajectories"]
        for task, row in zip(tasks, rows, strict=True):
            saved = row["task"]
            if (
                task.question != saved["question"]
                or task.ground_truth != saved["ground_truth"]
            ):
                raise ValueError("Cached task ID matches but task contents changed")
        results = Results(
            (
                Trajectory(task=task, **{k: v for k, v in row.items() if k != "task"})
                for task, row in zip(tasks, rows, strict=True)
            ),
            metric=algorithm.benchmark.primary_metric,
        )
        algorithm._save_snapshot(snapshot)
        # Keep the provenance opaque in proposer-visible training records.
        # The run-level recovery.json retains the actual failed-run location.
        provenance = hashlib.sha256(str(path).encode()).hexdigest()
        algorithm._record(label, {**record, "reused_from": provenance})
        return results

    CARSI._evaluate = evaluate
    try:
        yield
    finally:
        CARSI._evaluate = original
