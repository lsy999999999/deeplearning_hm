"""Reject invented evidence and distinguish aggregate gain from new regressions."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from run_continuous_diagnosis import choose, requests_checked, validate_files


def arm(values):
    return {
        "usable": True,
        "em": sum(values) / len(values),
        "per_task": dict(enumerate(values)),
    }


def test_same_candidate_policies_use_different_predeclared_gates():
    arms = {"A": arm([1, 0, 0, 0]), "B": arm([0, 1, 1, 0])}
    assert choose(arms, "seed") == {
        "base": "B",
        "gate_only": "A",
        "enhanced": "A",
        "seed_id": "seed",
    }
    arms["D"] = arm([1, 1, 0, 0])
    assert choose(arms, "seed")["enhanced"] == "D"


def test_unusable_and_tied_candidate_is_not_promoted():
    a = arm([1, 0])
    b = arm([1, 1])
    b["usable"] = False
    assert choose({"A": a, "B": b}, "seed")["base"] == "A"
    assert choose({"A": a, "B": a}, "seed")["enhanced"] == "A"


def test_interventions_require_exact_observed_boundary_and_falsifier():
    cases = [
        {
            "id": "c",
            "answer": "County, State",
            "observed_output": "County",
            "rule_ids": ["comma"],
        }
    ]
    good = {
        "case_id": "c",
        "observed_input": "County, State",
        "observed_output": "County",
        "disable": ["comma"],
        "predicted_output": "County, State",
        "claim": "truncation",
        "falsifier": "output remains County",
    }
    assert requests_checked({"interventions": [good]}, cases) == ([good], [])
    bad = {**good, "observed_output": "invented"}
    assert requests_checked({"interventions": [bad]}, cases) == ([], [bad])
    assert requests_checked({"interventions": None}, cases) == ([], [None])
    with pytest.raises(ValueError):
        validate_files({"runtime/agent.py": "change controller"})


def test_resume_reuses_identical_metadata_and_protects_paid_evidence(tmp_path):
    from types import SimpleNamespace

    from run_continuous_diagnosis import Study

    (tmp_path / "trajectories").mkdir()
    study = Study(SimpleNamespace(), {}, tmp_path)
    study.save("plan.json", {"version": 1})
    study.save("plan.json", {"version": 1})
    study.save("plan.json", {"version": 2})
    assert len(list(tmp_path.glob("plan-precall-*.json"))) == 1
    (tmp_path / "trajectories/00001-worker.json").write_text("{}")
    with pytest.raises(ValueError):
        study.save("plan.json", {"version": 3})


def test_study_cost_uses_original_records_not_generic_reuse_cache(tmp_path):
    import json

    from report_continuous_diagnosis import canonical_usage, phase_usage

    (tmp_path / "cache").mkdir()
    (tmp_path / "trajectories").mkdir()
    row = {"status": "finished", "usage": {"prompt_tokens": 17, "completion_tokens": 3}}
    (tmp_path / "trajectories/00001-worker.json").write_text(
        json.dumps({"prompt": "Propose ONE", "trajectory": row})
    )
    assert canonical_usage(tmp_path)["total"]["input_tokens"] == 17
    assert phase_usage(tmp_path)["proposal"]["output_tokens"] == 3


def test_report_separates_observed_bad_passes_from_good_candidate_blocking():
    from report_continuous_diagnosis import totals

    def scored(values):
        value = arm(values)
        return {**value, "n": len(values), "f1": value["em"]}

    cases = [
        {
            "selection": {"base": "B", "gate_only": "A", "enhanced": "D"},
            "test": {
                "A": scored([1, 0, 1, 0]),
                "B": scored([0, 1, 1, 1]),
                "D": scored([1, 1, 1, 0]),
            },
        },
        {
            "selection": {"base": "A", "gate_only": "A", "enhanced": "A"},
            "test": {"A": scored([1, 0]), "B": scored([1, 1])},
        },
    ]
    measured = totals(cases)
    assert measured["base"]["observed_bad_passes"] == 1
    assert measured["enhanced"]["observed_bad_passes"] == 0
    assert measured["enhanced"]["observed_good_B_blocked"] == 1
    assert measured["enhanced"]["observed_good_B_count"] == 1
    assert measured["base"]["test_n"] == 6
