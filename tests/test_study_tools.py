"""Scientific-boundary checks for shared-candidate comparisons and credentials."""

import asyncio
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


replay = module("replay_selectors")
runner = module("run_experiment")
freeze = module("freeze_test_plan")
recovery = module("reuse_evaluations")


def audit(outputs, baseline=(1, 0, 0, 0)):
    paths = ["a.md", "b.md"]
    rows = []
    for mask, scores in enumerate([baseline, *outputs]):
        deltas = {
            f"t{i}": x - y
            for i, (x, y) in enumerate(zip(scores, baseline, strict=True))
        }
        rows.append(
            {
                "paths": [path for i, path in enumerate(paths) if mask & (1 << i)],
                "snapshot_id": str(mask),
                "score": sum(scores) / len(scores),
                "per_task_delta": deltas,
                "regressions": sum(x < 0 for x in deltas.values()),
            }
        )
    return {
        "step": 0,
        "baseline_id": "0",
        "candidate_id": "3",
        "task_ids": list(rows[0]["per_task_delta"]),
        "changed_paths": paths,
        "variants": rows,
    }


def test_overall_gain_can_hide_harmful_edit():
    data = audit([(1, 1, 1, 1), (0, 0, 0, 0), (0, 1, 1, 1)])
    rows = {r["selector"]: r for r in replay.compare(data)["selections"]}
    assert rows["full_if_improved"]["dev_gain"] == 0.5
    assert rows["full_if_improved"]["regressed_tasks"] == 1
    assert rows["full_if_improved"]["conditional_harmful_units"] == 1
    assert rows["ca_fresh_dev"]["selected_paths"] == ["a.md"]
    assert rows["ca_fresh_dev"]["regressed_tasks"] == 0


def test_complementary_pair_defeats_single_addition_greedy():
    data = audit([(1, 0, 0, 0), (1, 0, 0, 0), (1, 1, 1, 1)])
    rows = {r["selector"]: r for r in replay.compare(data)["selections"]}
    assert rows["greedy"]["selected_paths"] == []
    assert rows["ca_fresh_dev"]["selected_paths"] == ["a.md", "b.md"]
    assert rows["greedy"]["consulted_variants"] == 3
    assert rows["ca_fresh_dev"]["consulted_variants"] == 4


def test_ties_retain_seed_and_incomparable_tasks_are_rejected():
    data = audit([(1, 0, 0, 0)] * 3)
    rows = {r["selector"]: r for r in replay.compare(data)["selections"]}
    assert rows["full_if_improved"]["snapshot_id"] == "0"
    assert rows["ca_fresh_dev"]["snapshot_id"] == "0"
    bad = copy.deepcopy(data)
    bad["variants"][1]["per_task_delta"]["new-task"] = 0
    with pytest.raises(ValueError, match="Unpaired"):
        replay.compare(bad)


def test_malformed_matrix_cannot_silently_make_a_result():
    data = audit([(1, 0, 0, 0)] * 3)
    data["variants"][1]["score"] = 1
    with pytest.raises(ValueError, match="Aggregate"):
        replay.compare(data)


def test_env_compatibility_and_redacted_dry_run_config(tmp_path):
    env = tmp_path / ".env"
    env.write_text('apikey="local-test-secret" # comment\nTARGET_MODEL=DeepSeek-V4-Flash\n')
    values = runner.read_env(env)
    assert runner.credential(values) == "local-test-secret"
    config = runner.configured(
        ROOT / "configs/hotpotqa_smoke.yaml", values, require_key=False
    )
    assert config.agent.llm.api_key == "<local-env>"
    assert config.algorithm.worker.llm.base_url == "https://token.matpool.com/v1"


def test_test_scores_do_not_change_frozen_dev_choices(tmp_path):
    import yaml

    source = tmp_path / "source"
    (source / "snapshots").mkdir(parents=True)
    (source / "trajectories").mkdir()
    data = audit([(1, 1, 1, 1), (0, 0, 0, 0), (0, 1, 1, 1)])
    (source / "trajectories/00001-audit.json").write_text(json.dumps(data))
    for row in data["variants"]:
        (source / "snapshots" / f"{row['snapshot_id']}.json").write_text("{}")
    config = yaml.safe_load((ROOT / "configs/hotpotqa_smoke.yaml").read_text())
    (source / "config.yaml").write_text(yaml.safe_dump(config))
    result = source / "result.json"
    result.write_text(
        json.dumps({"status": "finished", "snapshot_id": "1", "test_score": 0.0})
    )
    plan_file = freeze.freeze(source, tmp_path / "frozen")
    first = plan_file.read_text()
    result.write_text(
        json.dumps({"status": "finished", "snapshot_id": "1", "test_score": 1.0})
    )
    assert freeze.freeze(source, tmp_path / "frozen").read_text() == first
    plan = json.loads(first)
    assert len(plan["evaluations"]) == 3
    assert sum("reuse_native_result" in e for e in plan["evaluations"]) == 1


def test_freezer_refuses_multiround_history(tmp_path):
    source = tmp_path / "source/trajectories"
    source.mkdir(parents=True)
    for number in (1, 2):
        (source / f"{number:05}-audit.json").write_text("{}")
    with pytest.raises(ValueError, match="exactly one"):
        freeze.freeze(source.parent, tmp_path / "frozen")


def test_recovery_reuses_evidence_without_duplicate_cost_and_rejects_changed_inputs(
    tmp_path,
):
    from types import SimpleNamespace

    import yaml
    from flashrsi._records import _redact_config
    from flashrsi._run_summary import _collect_usage
    from flashrsi.algorithms.ca_rsi import CARSI
    from flashrsi.core import Task

    config = runner.configured(
        ROOT / "configs/hotpotqa_pilot.yaml", {}, require_key=False
    )
    source = tmp_path / "failed"
    destination = tmp_path / "recovered"
    (source / "trajectories").mkdir(parents=True)
    (destination / "trajectories").mkdir(parents=True)
    (source / "config.yaml").write_text(yaml.safe_dump(_redact_config(config)))
    (source / "error.json").write_text('{"status":"error"}')
    task = Task("train-0", "Question?", "Gold")
    record = {
        "snapshot_id": "seed",
        "trajectories": [
            {
                "task": {
                    "task_id": task.task_id,
                    "question": task.question,
                    "ground_truth": task.ground_truth,
                },
                "status": "finished",
                "output": "Gold",
                "metrics": {"exact_match": 1},
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            }
        ],
    }
    (source / "trajectories/00001-train.json").write_text(json.dumps(record))
    algorithm = SimpleNamespace(
        benchmark=SimpleNamespace(primary_metric="exact_match"),
        _save_snapshot=lambda snapshot: None,
        _record=lambda label, data: (
            destination / f"trajectories/00001-{label}.json"
        ).write_text(json.dumps(data)),
    )
    original = CARSI._evaluate
    with recovery.reuse_evaluations(config, source):
        result = asyncio.run(
            CARSI._evaluate(
                algorithm, SimpleNamespace(id="seed"), [task], label="train"
            )
        )
        assert result.score == 1
        with pytest.raises(ValueError, match="contents changed"):
            asyncio.run(
                CARSI._evaluate(
                    algorithm,
                    SimpleNamespace(id="seed"),
                    [Task("train-0", "Different?", "Gold")],
                    label="train",
                )
            )
    assert CARSI._evaluate is original
    assert _collect_usage(source)["total"]["input_tokens"] == 10
    assert _collect_usage(destination)["total"]["input_tokens"] == 0
    assert _collect_usage(destination)["total"]["usage_entries_missing_tokens"] == 0
    config.agent.llm.max_tokens = 123
    with pytest.raises(ValueError, match="target settings"):
        with recovery.reuse_evaluations(config, source):
            pass


def test_unlimited_output_omits_all_generation_caps_and_disables_thinking(monkeypatch):
    from types import SimpleNamespace

    from flashrsi.agents.miniagent.runtime import llm

    names = (
        "API_KEY",
        "DEFAULT_MODEL",
        "SIDE_MODEL",
        "BASE_URL",
        "SIDE_BASE_URL",
        "MAX_TOKENS",
        "UNLIMITED_OUTPUT",
        "LLM_MAX_RETRIES",
        "REASONING_EFFORT",
        "EXTRA_BODY",
    )
    for name in names:
        monkeypatch.setattr(llm, name, getattr(llm, name))
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace()

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        responses=SimpleNamespace(create=create),
    )
    monkeypatch.setattr(llm, "_get_client", lambda url: client)
    llm.configure(
        {
            "model_name": "DeepSeek-V4-Flash",
            "unlimited_output": True,
            "max_tokens": 4096,
            "reasoning_effort": "none",
            "extra_body": {"thinking": {"type": "disabled"}, "max_tokens": 123},
        }
    )
    asyncio.run(llm.chat([{"role": "user", "content": "Question"}], max_tokens=500))
    asyncio.run(llm.responses("Question", model="gpt-example", max_output_tokens=500))
    for request in calls:
        assert not (
            {"max_tokens", "max_completion_tokens", "max_output_tokens"}
            & request.keys()
        )
        assert not (
            {"max_tokens", "max_completion_tokens", "max_output_tokens"}
            & request["extra_body"].keys()
        )
        assert request["extra_body"]["thinking"]["type"] == "disabled"
    assert calls[0]["reasoning_effort"] == "none"
    llm.configure({})
    assert llm.MAX_TOKENS is None
