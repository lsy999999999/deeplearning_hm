"""Check that the viewer preserves native decisions and failed evaluations."""

import base64
import json
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import export_evolution
from evolution_diagram import svg_diagram
from export_evolution import changed_files, export, load_run, refresh, task_rows


def entry(text, executable=False):
    return {
        "content": base64.b64encode(text.encode()).decode(),
        "executable": executable,
    }


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def setup_run(tmp_path, method):
    (tmp_path / "config.yaml").write_text(
        f"algorithm:\n  name: {method}\nbenchmark:\n  seed: 43\n"
    )
    for identifier, text in [("seed", "initial"), ("a", "first"), ("b", "second")]:
        write(
            tmp_path / "snapshots" / f"{identifier}.json",
            {"files": {"x.md": entry(text)}},
        )


def evaluation(identifier, scores, failed=False):
    return {
        "snapshot_id": identifier,
        "trajectories": [
            {
                "task": {
                    "task_id": str(i),
                    "question": f"Question: Q{i}",
                    "ground_truth": "gold",
                },
                "status": "failed" if failed else "finished",
                "output": f"{identifier}{i}",
                "metrics": {"exact_match": score},
                "error_message": "missing dependency" if failed else None,
            }
            for i, score in enumerate(scores)
        ],
    }


def test_meta_harness_keeps_best_history_instead_of_last_candidate(tmp_path):
    setup_run(tmp_path, "meta_harness")
    records = [
        ("baseline", evaluation("seed", [0, 0])),
        ("candidate", evaluation("a", [1, 1])),
        (
            "decision",
            {
                "kind": "admit",
                "step": 0,
                "parent_id": "seed",
                "candidate_id": "a",
                "score": 1,
                "improved_best": True,
            },
        ),
        ("candidate", evaluation("b", [1, 0])),
        (
            "decision",
            {
                "kind": "admit",
                "step": 1,
                "parent_id": "seed",
                "candidate_id": "b",
                "score": 0.5,
                "improved_best": False,
            },
        ),
        ("decision", {"kind": "decline", "step": 2, "problem": "invalid scratch path"}),
        ("decision", {"kind": "promote", "candidate_id": "a"}),
    ]
    for i, (label, record) in enumerate(records):
        write(tmp_path / "trajectories" / f"{i:05}-{label}.json", record)
    result = load_run(tmp_path)
    assert result["final_id"] == "a"
    assert result["rounds"][1]["selected_id"] == "a"
    assert result["rounds"][1]["previous_best_score"] == 1
    assert result["rounds"][1]["variants"][1]["improved"] == 1
    assert result["rounds"][1]["variants"][2]["label"] == "历史最好版本"
    assert result["rounds"][1]["variants"][2]["id"] == "a"
    assert result["final_changes"][0]["diff"].endswith("+first")
    svg = svg_diagram(result)
    ElementTree.fromstring(svg)
    assert "第 2 轮选择：保留原有版本" in svg
    assert svg.count("Snapshot a") == 3
    assert result["rounds"][2]["baseline_id"] == "seed"
    assert result["rounds"][2]["selected_id"] == "a"
    assert result["rounds"][2]["retained_score"] == 1


def test_ca_rsi_error_count_does_not_equal_regression_count(tmp_path):
    setup_run(tmp_path, "ca_rsi")
    records = [
        ("audit_baseline", evaluation("seed", [1, 0])),
        ("audit_candidate", evaluation("a", [0, 0], failed=True)),
        (
            "audit",
            {
                "step": 0,
                "baseline_id": "seed",
                "candidate_id": "a",
                "selected_id": "seed",
                "selected_paths": [],
                "variants": [
                    {
                        "snapshot_id": "seed",
                        "paths": [],
                        "score": 0.5,
                        "per_task_delta": {"0": 0, "1": 0},
                    },
                    {
                        "snapshot_id": "a",
                        "paths": ["x.md"],
                        "score": 0,
                        "per_task_delta": {"0": -1, "1": 0},
                    },
                ],
            },
        ),
    ]
    for i, (label, record) in enumerate(records):
        write(tmp_path / "trajectories" / f"{i:05}-{label}.json", record)
    result = load_run(tmp_path)
    candidate = result["rounds"][0]["variants"][1]
    assert candidate["errors"] == 2
    assert candidate["regressions"] == 1
    assert result["rounds"][0]["selected_id"] == "seed"
    assert result["test_score"] is None


def test_missing_metric_is_not_silently_reported_as_zero():
    data = evaluation("seed", [1])
    data["trajectories"][0]["metrics"] = {}
    with pytest.raises(KeyError):
        task_rows(data, "exact_match")


def test_file_deletion_and_mode_change_are_visible():
    rows = changed_files(
        {"a": entry("deleted"), "b": entry("same")}, {"b": entry("same", True)}
    )
    assert rows[0]["operation"] == "删除"
    assert "元数据" in rows[1]["diff"]


def test_export_script_termination_is_escaped(tmp_path):
    setup_run(tmp_path, "meta_harness")
    write(tmp_path / "trajectories/00001-baseline.json", evaluation("seed", [1]))
    write(
        tmp_path / "trajectories/00002-worker.json",
        {"trajectory": {"output": "</script><script>alert(1)</script>"}},
    )
    write(
        tmp_path / "trajectories/00003-decision.json",
        {"kind": "decline", "step": 0, "problem": "unchanged"},
    )
    output = tmp_path / "view.html"
    export([tmp_path], output)
    assert "</script><script>alert(1)" not in output.read_text()
    assert output.with_name("view-inline.html").exists()
    diagrams = list(output.parent.glob("view-*.svg"))
    assert len(diagrams) == 1
    ElementTree.fromstring(diagrams[0].read_text())


def test_fresh_audit_tasks_are_not_overwritten_by_retention_or_training(tmp_path):
    setup_run(tmp_path, "ca_rsi")

    def ev(identifier, scores, offset=0):
        data = evaluation(identifier, scores)
        for i, row in enumerate(data["trajectories"]):
            row["task"]["task_id"] = str(i + offset)
        return data

    def audit(step, base, candidate, scores, offset):
        return {
            "step": step,
            "baseline_id": base,
            "candidate_id": candidate,
            "selected_id": candidate,
            "selected_paths": ["x.md"],
            "variants": [
                {
                    "snapshot_id": base,
                    "paths": [],
                    "score": 0,
                    "per_task_delta": {str(offset + i): 0 for i in range(2)},
                },
                {
                    "snapshot_id": candidate,
                    "paths": ["x.md"],
                    "score": 1,
                    "per_task_delta": {
                        str(offset + i): s for i, s in enumerate(scores)
                    },
                },
            ],
        }

    records = [
        ("audit_baseline", ev("seed", [0, 0])),
        ("audit_candidate", ev("a", [1, 1])),
        ("audit", audit(0, "seed", "a", [1, 1], 0)),
        ("train", ev("a", [0, 0], 100)),
        ("audit_baseline", ev("a", [0, 0], 2)),
        ("audit_candidate", ev("b", [1, 1], 2)),
        ("retention_baseline", ev("a", [1, 1])),
        ("retention_candidate", ev("b", [1, 1])),
        ("audit", audit(1, "a", "b", [1, 1], 2)),
    ]
    for i, (label, record) in enumerate(records):
        write(tmp_path / "trajectories" / f"{i:05}-{label}.json", record)
    result = load_run(tmp_path)
    candidate = result["rounds"][1]["variants"][1]
    assert {t["id"] for t in candidate["tasks"]} == {"2", "3"}
    assert candidate["count"] == 2
    assert [t["delta"] for t in candidate["tasks"]] == [1, 1]


def test_refresh_preserves_history_deduplicates_and_ignores_failed_runs(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(export_evolution, "ROOT", tmp_path)
    output = tmp_path / "results/view.html"
    runs = []
    for name in ("one", "two"):
        run = tmp_path / name
        run.mkdir()
        setup_run(run, "meta_harness")
        write(run / "trajectories/00001-baseline.json", evaluation("seed", [1]))
        write(run / "result.json", {"status": "finished", "snapshot_id": "seed"})
        runs.append(run)
        refresh(run, output)
    refresh(runs[0], output)
    assert export_evolution.default_runs(output) == [runs[1], runs[0]]
    assert (runs[0] / "evolution-view.html").exists()
    assert len(json.loads(output.with_suffix(".json").read_text())["runs"]) == 2
    original = output.read_bytes()
    write(runs[1] / "result.json", {"status": "error"})
    with pytest.raises(ValueError, match="finished"):
        refresh(runs[1], output)
    assert output.read_bytes() == original
