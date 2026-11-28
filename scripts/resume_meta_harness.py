"""Resume an interrupted native Meta-Harness run without repeating scored steps."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml
from filelock import FileLock
from run_experiment import ROOT, configured, settings

sys.path.insert(0, str(ROOT / "FlashRSI"))


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def completed_state(directory):
    records = [
        (p, read(p)) for p in sorted((directory / "trajectories").glob("*.json"))
    ]
    baseline = next(d for p, d in records if p.stem.endswith("-baseline"))
    decisions = [
        d
        for p, d in records
        if p.stem.endswith("-decision") and d.get("kind") in {"admit", "decline"}
    ]
    steps = [d["step"] for d in decisions]
    if steps != list(range(len(steps))):
        raise ValueError("Completed Meta steps must be a unique contiguous prefix")
    if any(d.get("kind") == "promote" for _, d in records):
        raise ValueError("This recovery entry expects a failure before final promotion")
    scores = next(d for p, d in records if p.stem.endswith("-scores"))
    if scores["snapshot_id"] != baseline["snapshot_id"]:
        raise ValueError("Baseline identity differs from recorded score")
    return records, baseline, decisions, scores["score"]


async def resume(directory, values):
    from flashrsi._records import _redact_config, _write_record
    from flashrsi.core import Snapshot
    from flashrsi.runner import _finish_summary, _prepare

    if (directory / "result.json").exists() or not (directory / "error.json").exists():
        raise ValueError("Resume only a failed run without a final test result")
    saved = yaml.safe_load((directory / "config.yaml").read_text())
    config = configured(directory / "config.yaml", values)
    clean = _redact_config(config)
    if config.algorithm.name != "meta_harness":
        raise ValueError("This entry only resumes original Meta-Harness")
    for section in ("agent", "benchmark", "algorithm"):
        if clean[section] != saved[section]:
            raise ValueError(f"Refusing recovery after {section} settings changed")
    records, baseline, decisions, baseline_score = completed_state(directory)
    if len(decisions) >= config.algorithm.max_steps:
        raise ValueError("No uncompleted search step remains")
    algorithm = await _prepare(config)
    seed = algorithm.target.snapshot
    if seed.id != baseline["snapshot_id"]:
        raise ValueError(
            "Current initial agent source differs from the interrupted run"
        )
    expected = [row["task"] for row in baseline["trajectories"]]
    current = [
        {
            "task_id": t.task_id,
            "question": t.question,
            "ground_truth": t.ground_truth,
            "env": t.env,
            "metadata": dict(t.metadata),
        }
        for t in algorithm.benchmark.dev
    ]
    # Task metadata freezes lists into tuples; records store JSON arrays.
    if json.loads(json.dumps(current, default=dict)) != expected:
        raise ValueError("Development task contents changed")
    algorithm._run_dir = directory
    algorithm._sequence = max(int(p.name.split("-", 1)[0]) for p, _ in records)
    best, best_score, versions = seed, baseline_score, [seed]
    for decision in decisions:
        if decision["kind"] == "admit":
            candidate = Snapshot.load(
                directory / "snapshots" / f"{decision['candidate_id']}.json"
            )
            versions.append(candidate)
            improved = decision["score"] > best_score
            if improved != decision["improved_best"]:
                raise ValueError(
                    "Recorded selection disagrees with strict historical-best policy"
                )
            if improved:
                best, best_score = candidate, decision["score"]
    stamp = datetime.now(UTC).isoformat()
    recovery = {
        "started_at": stamp,
        "completed_steps_reused": len(decisions),
        "restart_step_zero_based": len(decisions),
        "original_error": read(directory / "error.json"),
        "policy": "native fixed-seed proposals and strict historical-best selection",
        "history": "all original trajectories retained, including failed Worker trace",
        "usage": "same run directory; every original and resumed call counted once",
    }
    algorithm._record("recovery", recovery)
    start = time.monotonic()
    try:
        for step in range(len(decisions), config.algorithm.max_steps):
            candidate = await algorithm._propose(seed, versions, step)
            if candidate is None:
                continue
            results = await algorithm._evaluate(
                candidate, algorithm.benchmark.dev, label="candidate"
            )
            versions.append(candidate)
            improved = results.score > best_score
            algorithm._record(
                "decision",
                {
                    "kind": "admit",
                    "step": step,
                    "parent_id": candidate.parent_id,
                    "candidate_id": candidate.id,
                    "score": results.score,
                    "improved_best": improved,
                },
            )
            if improved:
                best, best_score = candidate, results.score
        final = algorithm._promote(
            seed,
            best,
            score=best_score,
            baseline_score=baseline_score,
            candidate_ids=[v.id for v in versions],
        )
        results = await algorithm._evaluate(
            final, algorithm.benchmark.test, label="test"
        )
        summary = {
            "status": "finished",
            "snapshot_id": final.id,
            "primary_metric": algorithm.benchmark.primary_metric,
            "test_score": results.score,
            "test_tasks": len(results),
            "test_metrics": {
                k: sum(t.metrics[k] for t in results) / len(results)
                for k in results[0].metrics
            },
            "benchmark": dict(algorithm.benchmark.metadata),
            "resumed_after_error": True,
        }
        _write_record(directory / "result.json", summary, config)
        _write_record(
            directory / "execution.json",
            {
                "elapsed_seconds": None,
                "recovery_elapsed_seconds": time.monotonic() - start,
                "wall_elapsed_seconds": time.time()
                - (directory / "config.yaml").stat().st_mtime,
                "scope": "original monotonic duration unavailable; wall time includes interruption/sleep",
                "python": sys.version,
            },
            config,
        )
    except BaseException as error:
        _write_record(
            directory / f"recovery-error-{time.time_ns()}.json",
            {
                "status": "error",
                "error": str(error),
                "started_at": stamp,
            },
            config,
        )
        raise
    finally:
        _finish_summary(directory)
    from export_evolution import refresh

    refresh(directory)
    print(
        json.dumps({"run_dir": str(directory), "result": summary}, ensure_ascii=False)
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    values = settings(args.env)
    try:
        with FileLock(str(directory / ".resume.lock"), timeout=0):
            asyncio.run(resume(directory, values))
    except Exception as error:  # noqa: BLE001 - redact transport errors before CLI output
        from flashrsi._records import _redact

        secrets = [
            v
            for k, v in values.items()
            if v and any(s in k.lower() for s in ("key", "token", "secret", "password"))
        ]
        print(_redact(str(error), secrets), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
