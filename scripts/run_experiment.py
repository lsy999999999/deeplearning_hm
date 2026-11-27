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


def apply_overrides(config, args):
    """Apply explicit CLI settings after YAML and environment model defaults."""
    if args.seed is not None:
        config.benchmark.seed = args.seed
    if args.output_dir is not None:
        config.run.output_dir = str(args.output_dir.expanduser().resolve())
    if args.container_image is not None:
        config.agent.container_image = args.container_image
    evaluation = config.run.get("mode") == "evaluate"
    search_options = (
        args.algorithm,
        args.rounds,
        args.max_edit_units,
        args.max_task_regressions,
        args.worker_model,
        args.worker_max_turns,
        args.worker_timeout,
    )
    if evaluation and any(value is not None for value in search_options):
        raise ValueError("Frozen evaluation cannot override search/Worker settings")
    if args.algorithm is not None:
        config.algorithm.name = args.algorithm
    if args.rounds is not None:
        config.algorithm.max_steps = args.rounds
    roles = [
        (
            config.agent,
            args.target_model if args.target_model is not None else args.model,
            args.target_max_turns,
            args.target_timeout,
        )
    ]
    if not evaluation:
        roles.append(
            (
                config.algorithm.worker,
                args.worker_model if args.worker_model is not None else args.model,
                args.worker_max_turns,
                args.worker_timeout,
            )
        )
    for role, model, turns, timeout in roles:
        if model is not None:
            if not model.strip():
                raise ValueError("Model name must not be empty")
            role.llm.model_name = model
        if turns is not None:
            role.max_turns = turns
        if timeout is not None:
            role.timeout = timeout
        if args.temperature is not None:
            role.llm.setdefault("extra_body", {})["temperature"] = args.temperature
    for split in ("train", "dev", "test"):
        count = getattr(args, split + "_size")
        if count is not None:
            config.benchmark.setdefault("split_counts", {})[split] = count
    if not evaluation and config.algorithm.name == "ca_rsi":
        for key in ("max_edit_units", "max_task_regressions"):
            value = getattr(args, key)
            if value is not None:
                config.algorithm[key] = value
        units = config.algorithm.get("max_edit_units", 3)
        if not 1 <= units <= 4:
            raise ValueError("CA-RSI max_edit_units must be between 1 and 4")
        if config.algorithm.get("max_task_regressions", 0) < 0:
            raise ValueError("max_task_regressions must be nonnegative")
        count = config.benchmark.get("split_counts", {}).get("dev")
        if count is not None and count < config.algorithm.get("max_steps", 1):
            raise ValueError("CA-RSI dev-size must be at least the number of rounds")
    if not evaluation and config.algorithm.name == "meta_harness":
        for key in ("max_edit_units", "max_task_regressions"):
            config.algorithm.pop(key, None)
    return config


async def execute(args, values):
    from flashrsi._records import _redact_config
    from flashrsi.runner import _evaluate, _prepare, _prepare_evaluation, _run

    config = apply_overrides(
        configured(args.config, values, require_key=not args.dry_run), args
    )
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
    if config.algorithm.name in {"meta_harness", "ca_rsi"}:
        from export_evolution import refresh

        try:
            refresh(directory, args.visualization_output)
        except Exception as error:
            raise RuntimeError(
                f"Experiment finished and saved in {directory}; visualization update failed: {error}"
            ) from error
        print("Visualization updated:", args.visualization_output)


def positive_int(text):
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def temperature(text):
    import math

    value = float(text)
    if not math.isfinite(value) or not 0 <= value <= 2:
        raise argparse.ArgumentTypeError("temperature must be between 0 and 2")
    return value


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument(
        "--config", type=Path, default=ROOT / "configs/hotpotqa_smoke.yaml"
    )
    parser.add_argument("--seed", type=int)
    parser.add_argument("--model", help="Set both Target and Worker models")
    parser.add_argument("--target-model", help="Override --model for the Target")
    parser.add_argument("--worker-model", help="Override --model for the Worker")
    parser.add_argument("--rounds", "--max-steps", type=positive_int)
    parser.add_argument("--max-edit-units", type=positive_int)
    parser.add_argument("--max-task-regressions", type=int)
    for split in ("train", "dev", "test"):
        parser.add_argument(f"--{split}-size", type=positive_int)
    for role in ("target", "worker"):
        parser.add_argument(f"--{role}-max-turns", type=positive_int)
        parser.add_argument(f"--{role}-timeout", type=positive_int)
    parser.add_argument("--temperature", type=temperature)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--visualization-output",
        type=Path,
        default=ROOT / "results/evolution-view.html",
    )
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
    return parser


def main():
    args = build_parser().parse_args()
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
