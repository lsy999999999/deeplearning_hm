"""Run the pinned FlashRSI checkout with local, uncommitted Matpool credentials."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "FlashRSI"))


def read_env(path: Path) -> dict[str, str]:
    values = {}
    if path.is_file():
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            line = line.removeprefix("export ")
            if "=" not in line:
                raise ValueError(f"Invalid .env assignment at line {number}")
            key, value = line.split("=", 1)
            parts = shlex.split(value, comments=True)
            values[key.strip()] = " ".join(parts)
    return values


def settings(path: Path) -> dict[str, str]:
    return {**read_env(path), **os.environ}


def credential(values: dict[str, str]) -> str:
    key = (
        values.get("MATPOOL_API_KEY")
        or values.get("OPENAI_API_KEY")
        or values.get("apikey")
        or values.get("API_KEY")
    )
    if not key:
        raise ValueError("Set MATPOOL_API_KEY (or apikey) in the local .env file")
    return key


def configured(path: Path, values: dict[str, str], *, require_key=True):
    from flashrsi.config import load_config

    config = load_config(path)
    roles = [("TARGET", config.agent)]
    if config.run.get("mode") != "evaluate":
        roles.append(("WORKER", config.algorithm.worker))
    for role, section in roles:
        section.llm.base_url = values.get(
            "MATPOOL_BASE_URL", "https://token.matpool.com/v1"
        )
        section.llm.model_name = values.get(f"{role}_MODEL") or section.llm.model_name
        section.llm.api_key = credential(values) if require_key else "<local-env>"
    return config


async def execute(args, values):
    from flashrsi._records import _redact_config
    from flashrsi.runner import _evaluate, _prepare, _prepare_evaluation, _run

    config = configured(args.config, values, require_key=not args.dry_run)
    if args.seed is not None:
        config.benchmark.seed = args.seed
    if args.container_image is not None:
        config.agent.container_image = args.container_image
    if args.algorithm is not None:
        if config.run.get("mode") == "evaluate":
            raise ValueError("An evaluation cannot override its algorithm")
        config.algorithm.name = args.algorithm
        if args.algorithm == "meta_harness":
            for key in ("max_edit_units", "max_task_regressions"):
                config.algorithm.pop(key, None)
    if args.dry_run:
        print(json.dumps(_redact_config(config), ensure_ascii=False, indent=2))
        return
    if args.prepare:
        if config.run.get("mode") == "evaluate":
            target, benchmark = await _prepare_evaluation(config)
        else:
            algorithm = await _prepare(config)
            target, benchmark = algorithm.target, algorithm.benchmark
        manifest = {
            "benchmark": dict(benchmark.metadata),
            "splits": {
                role: [task.task_id for task in getattr(benchmark, role)]
                for role in ("train", "dev", "test")
            },
            "target_snapshot": target.snapshot.id,
            "config": _redact_config(config),
        }
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        print("Prepared without model calls:", args.manifest)
        return
    import time

    from reuse_evaluations import reuse_evaluations

    start = time.monotonic()
    with reuse_evaluations(config, args.reuse_evaluations):
        directory, result = await (
            _evaluate(config) if config.run.get("mode") == "evaluate" else _run(config)
        )
    if args.reuse_evaluations:
        (directory / "recovery.json").write_text(
            json.dumps(
                {
                    "source_failed_run": str(args.reuse_evaluations.resolve()),
                    "reuse_scope": "unchanged_target_train_and_dev_baseline_only",
                    "usage_owner": "original_run_for_reused_evaluations",
                },
                indent=2,
            )
        )
    (directory / "execution.json").write_text(
        json.dumps(
            {
                "elapsed_seconds": time.monotonic() - start,
                "python": sys.version,
                "scope": "whole_launcher_including_prepare_and_test",
            },
            indent=2,
        )
    )
    print(json.dumps({"run_dir": str(directory), "result": result}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument(
        "--config", type=Path, default=ROOT / "configs/hotpotqa_smoke.yaml"
    )
    parser.add_argument("--seed", type=int)
    parser.add_argument("--container-image")
    parser.add_argument("--reuse-evaluations", type=Path)
    parser.add_argument("--algorithm", choices=("ca_rsi", "meta_harness"))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--prepare", action="store_true")
    modes.add_argument("--check-api", action="store_true")
    parser.add_argument(
        "--manifest", type=Path, default=ROOT / "results/private/split_manifest.json"
    )
    args = parser.parse_args()
    values = settings(args.env)
    try:
        if args.check_api:
            from openai import OpenAI

            client = OpenAI(
                api_key=credential(values),
                base_url=values.get("MATPOOL_BASE_URL", "https://token.matpool.com/v1"),
                max_retries=0,
                timeout=60,
            )
            response = client.chat.completions.create(
                model=values.get("TARGET_MODEL", "DeepSeek-V4-Flash"),
                messages=[{"role": "user", "content": "Reply only OK."}],
                reasoning_effort="none",
                extra_body={"thinking": {"type": "disabled"}},
            )
            print(
                json.dumps(
                    {
                        "model": response.model,
                        "finish_reason": response.choices[0].finish_reason,
                        "reply": response.choices[0].message.content,
                        "usage": response.usage.model_dump()
                        if response.usage
                        else None,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            asyncio.run(execute(args, values))
    except Exception as error:  # noqa: BLE001 - redact all CLI errors
        # Provider errors can echo request values. Redact credentials before printing.
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
