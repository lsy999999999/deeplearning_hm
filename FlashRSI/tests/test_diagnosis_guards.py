"""Regression checks for the first natural diagnostic failure; no API calls."""

import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_rule_integration import MODULE, Meta, make, records, source

from flashrsi.algorithms.recursive_ca_rsi import RecursiveCARSI, _diagnostic, _mechanism
from flashrsi.core import Task, Trajectory


def schema():
    case = {
        "id": "train::final-boundary",
        "task_id": "train",
        "module_path": MODULE,
        "rule_ids": ["comma"],
        "observed_output": "Graham County",
    }
    evidence = {
        "strict_rule_diagnosis": True,
        "candidate_snapshot_id": "candidate",
        "changed_paths": [MODULE],
        "before": [{"task": {"task_id": "train"}}],
        "after": [{"task": {"task_id": "train"}, "output": "Graham County"}],
        "rule_cases": [case],
    }
    diagnosis = {
        "issues": [
            {
                "path": MODULE,
                "kind": "truncation",
                "claim": "Drops the state",
                "repair": "Keep the state",
                "verification": "Disable comma",
                "evidence_task_ids": ["train"],
                "intervention_indices": [0],
                "observations": [
                    {
                        "task_id": "train",
                        "snapshot_id": "candidate",
                        "output": "Graham County",
                    }
                ],
            }
        ],
        "interventions": [
            {
                "case_id": case["id"],
                "disable": ["comma"],
                "predicted_output": "Graham County, Kansas",
                "claim": "Truncates state",
                "falsified_if": "State is not restored",
            }
        ],
    }
    return evidence, diagnosis


@pytest.mark.parametrize(
    "mutation",
    [
        "old_snapshot",
        "old_output",
        "no_request",
        "no_observation",
        "no_falsification",
        "wrong_index",
    ],
)
def test_stale_or_non_executable_diagnosis_cannot_enter_repair(mutation):
    evidence, diagnosis = schema()
    issue = diagnosis["issues"][0]
    if mutation == "old_snapshot":
        issue["observations"][0]["snapshot_id"] = "baseline"
    elif mutation == "old_output":
        issue["observations"][0]["output"] = "a previous answer"
    elif mutation == "no_request":
        diagnosis["interventions"] = []
    elif mutation == "no_observation":
        issue.pop("observations")
    elif mutation == "no_falsification":
        diagnosis["interventions"][0].pop("falsified_if")
    else:
        issue["intervention_indices"] = [True]
    with pytest.raises(ValueError):
        RecursiveCARSI._diagnosis(json.dumps(diagnosis), evidence)


@pytest.mark.parametrize("harm", [False, True])
def test_correct_prediction_only_permits_repair_if_disabling_improves_grade(harm):
    evidence, diagnosis = schema()
    diagnosis = RecursiveCARSI._diagnosis(json.dumps(diagnosis), evidence)
    executed = {
        "groups": [
            {
                "module_path": MODULE,
                "status": "executed",
                "report": {
                    "hypotheses": [
                        {
                            "request": diagnosis["interventions"][0],
                            "error_attribution_supported": harm,
                        }
                    ]
                },
            }
        ]
    }
    supported, rejected = _diagnostic.repair_gate(diagnosis, evidence, executed)
    assert bool(supported) is harm
    assert bool(rejected) is not harm


def test_sixth_triggered_case_is_selected_with_unchanged_controls(tmp_path):
    algorithm = make(tmp_path)
    with algorithm.target.snapshot.check_out() as directory:
        (directory / MODULE).write_text(source("FAULT"))
        candidate = algorithm.target.snapshot.commit(directory)
    tasks = tuple(
        Task(
            f"case-{i}",
            "Graham County, Kansas" if i == 5 else "Ada",
            "Graham County, Kansas" if i == 5 else "Ada",
        )
        for i in range(8)
    )
    rows = algorithm.benchmark.grade(
        asyncio.run(algorithm.target.rollout(tasks, snapshot=candidate))
    )
    before = tuple(replace(row, metrics={"exact_match": 1}) for row in rows)
    cases, skipped = _mechanism.collect_cases(
        candidate, rows, 4, baseline=before, primary_metric="exact_match"
    )
    assert cases[0]["task_id"] == "case-5"
    assert cases[0]["selection"]["observed_regression"]
    assert cases[0]["selection"]["triggered_rules"] == ["comma"]
    assert any(c["selection"]["stratum"] == "unchanged" for c in cases)
    assert len(cases) == 4 and len(skipped) == 4
    assert [c["id"] for c in cases] == [
        c["id"]
        for c in _mechanism.collect_cases(
            candidate, rows, 4, baseline=before, primary_metric="exact_match"
        )[0]
    ]


def test_noop_repair_skips_frozen_replay_and_model_reevaluation(tmp_path, monkeypatch):
    algorithm = make(tmp_path)
    original = Meta._run

    async def run(self, prompt, working_dir=None):
        if "[Recursive-CA-RSI: repair]" in prompt:
            return Trajectory(output="No justified edit; no files changed.")
        return await original(self, prompt, working_dir)

    monkeypatch.setattr(Meta, "_run", run)
    asyncio.run(algorithm.evolve())
    repair = records(algorithm, "repair")[0]
    assert repair["status"] == "no_code_change"
    assert repair["before_id"] == repair["after_id"]
    assert repair["evaluation_skipped"] and repair["after_score"] is None
    assert not records(algorithm, "repair_check")
    assert len(records(algorithm, "rule_intervention")) == 1
    assert records(algorithm, "repair_gate")[0]["permitted"]
    journal = (algorithm._run_dir / "RSI_ITERATIONS.md").read_text()
    assert "no_code_change" in journal


@pytest.mark.parametrize("mode", ["no_issues", "useful_rule", "bad_observation"])
def test_unnecessary_or_unsupported_repair_is_not_called(tmp_path, monkeypatch, mode):
    algorithm = make(tmp_path)
    original = Meta._run

    async def run(self, prompt, working_dir=None):
        if "[Recursive-CA-RSI: repair]" in prompt:
            raise AssertionError("Unverified repair must not be called")
        result = await original(self, prompt, working_dir)
        if "[Recursive-CA-RSI: diagnose]" not in prompt:
            return result
        payload = json.loads(result.output)
        if mode == "no_issues":
            payload = {"issues": [], "interventions": []}
        elif mode == "bad_observation":
            payload["issues"][0]["observations"][0]["output"] = "old output"
        else:
            # Disabling the useful comma rule on the person makes it wrong.
            evidence = json.loads(prompt.rsplit("\n", 1)[-1])
            case = next(
                c for c in evidence["rule_cases"] if c["task_id"] == "train-person"
            )
            payload["issues"][0]["evidence_task_ids"] = ["train-person"]
            payload["issues"][0]["observations"][0].update(
                task_id="train-person", output=case["observed_output"]
            )
            payload["interventions"][0].update(
                case_id=case["id"], predicted_output=case["answer"]
            )
        return replace(result, output=json.dumps(payload))

    monkeypatch.setattr(Meta, "_run", run)
    asyncio.run(algorithm.evolve())
    assert not records(algorithm, "repair_check")
    repair = records(algorithm, "repair")[0]
    assert repair["status"] == (
        "not_needed" if mode == "no_issues" else "not_attempted"
    )


def test_worker_limits_restore_config_after_error(tmp_path):
    algorithm = make(tmp_path)
    algorithm.worker.config["max_turns"] = 30
    before = dict(algorithm.worker.config)
    with pytest.raises(RuntimeError), algorithm._worker_limits(diagnosing=True):
        assert algorithm.worker.config["max_turns"] == 1
        assert algorithm.worker.config["_diagnosis_tool_free"]
        raise RuntimeError("failure")
    assert dict(algorithm.worker.config) == before
    with algorithm._worker_limits():
        assert algorithm.worker.config["max_turns"] == 12
    assert dict(algorithm.worker.config) == before


def test_first_natural_prose_diagnosis_remains_unverified():
    path = (
        Path(__file__).resolve().parents[2]
        / "results/fixed-meta-diagnosis-20261128/evidence.json"
    )
    archived = json.loads(path.read_text())
    diagnosis = archived["diagnoses"][0]
    assert diagnosis["interventions"] == []
    evidence = {
        "strict_rule_diagnosis": True,
        "candidate_snapshot_id": diagnosis["candidate_id"],
        "changed_paths": [MODULE, "middleware/answer_shape.py"],
        "before": [
            {"task": {"task_id": i}}
            for issue in diagnosis["issues"]
            for i in issue["evidence_task_ids"]
        ],
        "after": [],
        "rule_cases": [],
    }
    with pytest.raises(ValueError, match="observations"):
        RecursiveCARSI._diagnosis(json.dumps(diagnosis), evidence)
    # No historical artifact or prediction is rewritten to make it pass.


def test_repair_summary_keeps_outcomes_without_repeated_event_payload():
    report = {
        "groups": [
            {
                "module_path": MODULE,
                "status": "executed",
                "report": {
                    "variants": [{"events": ["very long raw evidence"]}],
                    "hypotheses": [{"status": "insufficient_evidence"}],
                    "effects": [],
                },
            }
        ]
    }
    original = copy.deepcopy(report)
    compact = _diagnostic.repair_summary(report)
    assert "variants" not in compact["groups"][0]["report"]
    assert compact["groups"][0]["report"]["hypotheses"]
    assert report == original


def test_retrospective_review_only_reads_learning_records(tmp_path, monkeypatch):
    algorithm = make(tmp_path)
    asyncio.run(algorithm.evolve())
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    from review_diagnosis_selection import review

    folder = algorithm._run_dir / "trajectories"
    baseline = next(folder.glob("*-repair_baseline.json"))
    candidate = next(folder.glob("*-repair_candidate.json"))
    diagnosis = next(folder.glob("*-diagnosis.json"))
    snapshot = (
        algorithm._run_dir
        / "snapshots"
        / (json.loads(candidate.read_text())["snapshot_id"] + ".json")
    )
    originals = {path: path.read_bytes() for path in folder.glob("*.json")}
    report = review(snapshot, baseline, candidate, diagnosis_path=diagnosis)
    assert report["model_calls"] == 0 and not report["candidate_code_executed"]
    assert report["selected"]
    assert report["diagnosis_check"]["status"] == "schema_valid_only"
    assert not report["diagnosis_check"]["repair_permitted"]
    assert all(path.read_bytes() == original for path, original in originals.items())
    with pytest.raises(ValueError, match="learning records"):
        review(snapshot, baseline, folder / "00025-test.json")


def test_oversized_diagnosis_records_no_attempt_without_worker_retry(tmp_path):
    algorithm = make(tmp_path)
    algorithm.config["diagnosis_max_chars"] = 1
    asyncio.run(algorithm.evolve())
    assert records(algorithm, "diagnosis")[0]["status"] == "unavailable"
    assert records(algorithm, "repair")[0]["status"] == "not_attempted"
    assert not records(algorithm, "repair_check")
