"""Run one fixed-Meta case after checking prior task IDs and content hashes."""

import asyncio
import hashlib
import json
import subprocess
from unittest.mock import patch

from run_experiment import (
    ROOT,
    apply_overrides,
    build_parser,
    configured,
    execute,
    settings,
)


def identities(value, ids, hashes):
    if isinstance(value, dict):
        if isinstance(value.get("task_id"), str):
            ids.add(value["task_id"])
            if isinstance(value.get("question"), str):
                hashes.add(hashlib.sha256(value["question"].encode()).hexdigest())
            checksum = value.get("metadata", {}).get("question_checksum")
            if checksum:
                hashes.add(checksum)
        for child in value.values():
            identities(child, ids, hashes)
    elif isinstance(value, list):
        for child in value:
            identities(child, ids, hashes)


async def run(args, values):
    from flashrsi import runner

    if args.check_api or args.reuse_evaluations:
        raise ValueError("This entry does not support API probes or evaluation reuse")
    if args.dry_run or args.prepare:
        return await execute(args, values)

    config = apply_overrides(configured(args.config, values), args)
    if config.algorithm.name != "recursive_ca_rsi" or config.algorithm.meta_self_update:
        raise ValueError("This experiment requires fixed Meta")
    if not config.algorithm.rule_verification:
        raise ValueError("Rule verification must be enabled")
    algorithm = await runner._prepare(config)
    old_ids, old_hashes = set(), set()
    paths = list((ROOT / "runs").glob("**/trajectories/*.json"))
    paths += list((ROOT / "results").glob("*.json"))
    for path in paths:
        identities(json.loads(path.read_text()), old_ids, old_hashes)
    splits = {}
    for role in ("train", "dev", "test"):
        rows = []
        for task in getattr(algorithm.benchmark, role):
            digest = hashlib.sha256(task.question.encode()).hexdigest()
            checksum = task.metadata.get("question_checksum")
            if (
                task.task_id in old_ids
                or digest in old_hashes
                or checksum in old_hashes
            ):
                raise ValueError(f"Previously exposed task in {role}: {task.task_id}")
            rows.append(
                {
                    "task_id": task.task_id,
                    "content_sha256": digest,
                    "question_checksum": checksum,
                }
            )
        splits[role] = rows
    plan = {
        "status": "frozen_before_model_calls",
        "source_commit": subprocess.check_output(  # noqa: ASYNC221 - short local provenance command
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "config": str(args.config),
        "splits": splits,
        "historical_records_checked": len(paths),
        "historical_task_ids": len(old_ids),
        "overlap": 0,
        "meta_self_update": False,
        "scope": "one consecutive natural candidate; unsupported diagnosis remains a result",
        "test_feedback_to_repair": False,
    }
    destination = ROOT / "results/fixed-meta-diagnosis-20261128-plan.json"
    if destination.exists():
        raise ValueError(
            "Plan already frozen; use a distinct experiment before rerunning"
        )
    destination.write_text(json.dumps(plan, indent=2) + "\n")
    print(
        "Frozen disjoint task IDs and content hashes; starting native experiment",
        flush=True,
    )

    async def prepared(_config):
        return algorithm

    with patch.object(runner, "_prepare", prepared):
        await execute(args, values)


if __name__ == "__main__":
    args = build_parser().parse_args()
    values = settings(args.env)
    try:
        asyncio.run(run(args, values))
    except Exception as error:  # noqa: BLE001 - redact CLI transport errors
        from flashrsi._records import _redact

        secrets = [
            v
            for k, v in values.items()
            if v and any(s in k.lower() for s in ("key", "token", "secret", "password"))
        ]
        raise SystemExit(_redact(str(error), secrets))
