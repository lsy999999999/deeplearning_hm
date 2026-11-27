"""Validate experiment controls and automatic export without model calls."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import export_evolution
import run_experiment as runner


def args(*flags):
    return runner.build_parser().parse_args(
        ["--config", str(ROOT / "configs/hotpotqa_pilot.yaml"), *flags]
    )


def test_explicit_model_and_search_controls_override_yaml_and_env():
    parsed = args(
        "--model",
        "both",
        "--worker-model",
        "worker",
        "--rounds",
        "4",
        "--max-edit-units",
        "2",
        "--max-task-regressions",
        "1",
        "--train-size",
        "9",
        "--dev-size",
        "32",
        "--test-size",
        "48",
        "--target-max-turns",
        "7",
        "--worker-max-turns",
        "40",
        "--temperature",
        "0.7",
    )
    config = runner.configured(
        parsed.config, {"TARGET_MODEL": "environment"}, require_key=False
    )
    runner.apply_overrides(config, parsed)
    assert config.agent.llm.model_name == "both"
    assert config.algorithm.worker.llm.model_name == "worker"
    assert config.algorithm.max_steps == 4
    assert config.algorithm.max_edit_units == 2
    assert config.algorithm.max_task_regressions == 1
    assert dict(config.benchmark.split_counts) == {"train": 9, "dev": 32, "test": 48}
    assert config.agent.max_turns == 7
    assert config.algorithm.worker.max_turns == 40
    assert config.agent.llm.extra_body.temperature == 0.7
    assert config.agent.llm.unlimited_output
    assert config.agent.llm.extra_body.thinking.type == "disabled"


def test_baseline_does_not_inherit_ca_rsi_constraints():
    parsed = args(
        "--algorithm", "meta_harness", "--rounds", "4", "--max-edit-units", "1"
    )
    config = runner.apply_overrides(
        runner.configured(parsed.config, {}, require_key=False), parsed
    )
    assert "max_edit_units" not in config.algorithm
    assert "max_task_regressions" not in config.algorithm
    assert config.algorithm.max_steps == 4


@pytest.mark.parametrize(
    "flags, message",
    [
        (("--rounds", "5", "--dev-size", "4"), "dev-size"),
        (("--max-edit-units", "5"), "between 1 and 4"),
        (("--max-task-regressions", "-1"), "nonnegative"),
    ],
)
def test_invalid_search_controls_rejected_before_execution(flags, message):
    parsed = args(*flags)
    with pytest.raises(ValueError, match=message):
        runner.apply_overrides(
            runner.configured(parsed.config, {}, require_key=False), parsed
        )


def test_successful_launcher_refreshes_view_and_saves_time_without_api(
    tmp_path, monkeypatch, capsys
):
    import flashrsi.runner as native
    from test_evolution_export import evaluation, setup_run, write

    directory = tmp_path / "native"
    directory.mkdir()
    setup_run(directory, "ca_rsi")
    write(directory / "trajectories/00001-audit_baseline.json", evaluation("seed", [1]))
    result = {"status": "finished", "snapshot_id": "seed"}
    write(directory / "result.json", result)
    monkeypatch.setattr(export_evolution, "ROOT", tmp_path)

    async def fake_run(config):
        return directory, result

    monkeypatch.setattr(native, "_run", fake_run)
    output = tmp_path / "results/view.html"
    asyncio.run(
        runner.execute(
            args("--visualization-output", str(output)), {"apikey": "test-only-secret"}
        )
    )
    assert output.exists()
    assert (
        json.loads(output.with_suffix(".json").read_text())["runs"][0]["run"]
        == "native"
    )
    assert (directory / "execution.json").exists()
    assert "test-only-secret" not in capsys.readouterr().out


def test_failed_launcher_does_not_register_a_success(tmp_path, monkeypatch):
    import flashrsi.runner as native

    async def fail(config):
        raise RuntimeError("failed model")

    def no_export(*args):
        pytest.fail("a failed run must not enter the successful history")

    monkeypatch.setattr(native, "_run", fail)
    monkeypatch.setattr(export_evolution, "refresh", no_export)
    with pytest.raises(RuntimeError, match="failed model"):
        asyncio.run(runner.execute(args(), {"apikey": "test-only-secret"}))


def test_batch_dry_run_uses_requested_models_rounds_and_seeds(tmp_path):
    env = {
        **os.environ,
        "MODEL": "test-common",
        "TARGET_MODEL": "test-target",
        "WORKER_MODEL": "test-worker",
        "ROUNDS": "3",
        "SEEDS": "43 44",
        "ALGORITHMS": "meta_harness ca_rsi",
        "DEV_SIZE": "12",
        "OUTPUT_DIR": str(tmp_path / "runs"),
    }
    completed = subprocess.run(
        ["bash", str(ROOT / "scripts/run_study.sh"), "--dry-run"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    decoder, text, configs = json.JSONDecoder(), completed.stdout, []
    while text.strip():
        config, end = decoder.raw_decode(text.lstrip())
        configs.append(config)
        text = text.lstrip()[end:]
    assert len(configs) == 4
    assert {(c["algorithm"]["name"], c["benchmark"]["seed"]) for c in configs} == {
        (a, s) for a in ("meta_harness", "ca_rsi") for s in (43, 44)
    }
    assert all(c["agent"]["llm"]["model_name"] == "test-target" for c in configs)
    assert all(
        c["algorithm"]["worker"]["llm"]["model_name"] == "test-worker" for c in configs
    )
    assert all(c["algorithm"]["max_steps"] == 3 for c in configs)
    assert all(c["agent"]["llm"]["api_key"] is None for c in configs)
    assert not (tmp_path / "runs").exists()


def test_batch_validates_all_before_calls_and_continues_after_failure(tmp_path):
    fake = tmp_path / "fake-python"
    log = tmp_path / "calls.jsonl"
    fake.write_text(
        f"#!{sys.executable}\n"
        + """import json, os, sys
with open(os.environ["STUDY_TEST_LOG"], "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
if "--dry-run" not in sys.argv and sys.argv[sys.argv.index("--algorithm")+1] == "meta_harness":
    sys.exit(1)
"""
    )
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PYTHON_BIN": str(fake),
        "STUDY_TEST_LOG": str(log),
        "ALGORITHMS": "meta_harness ca_rsi",
        "SEEDS": "43",
    }
    completed = subprocess.run(
        ["bash", str(ROOT / "scripts/run_study.sh")],
        check=False,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert completed.returncode == 1
    assert len(calls) == 4
    assert ["--dry-run" in call for call in calls] == [True, True, False, False]
    assert calls[-1][calls[-1].index("--algorithm") + 1] == "ca_rsi"
