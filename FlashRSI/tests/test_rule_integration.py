"""No-API end-to-end controller test with real frozen boundary subprocesses."""

import asyncio
import importlib.util
import json
from dataclasses import replace

from flashrsi.agents.miniagent.runtime.rule_audit import run_rules
from flashrsi.algorithms.recursive_ca_rsi import RecursiveCARSI, _mechanism
from flashrsi.benchmarks._official_qa._hotpot import exact_match_score
from flashrsi.core import Agent, Benchmark, Task, Trajectory

MODULE = "middleware/gate.py"


def source(mode):
    body = {
        "SEED": 'return "unknown"',
        "FAULT": 'return answer.partition(",")[0]',
        "REPAIRED": 'head, sep, tail = answer.partition(",")\n    return head if tail.strip()[:1].islower() else answer',
    }[mode]
    return (
        "from flashrsi.agents.miniagent.runtime.rule_audit import Rule\n"
        f'MODE = "{mode}"\n'
        "def transform(answer, evidence):\n    " + body + "\n"
        'AUDIT_RULES = [Rule("comma",transform)]\n'
    )


class Tasks(Benchmark):
    name = "frozen-boundary-test"
    description = "Controlled pipeline; official Hotpot normalization for grading."
    primary_metric = "exact_match"
    metadata = {}

    def load(self):
        self._splits = {
            "train": (
                Task("train-county", "Graham County, Kansas", "Graham County, Kansas"),
                Task("train-person", "Ada Lovelace, a mathematician", "Ada Lovelace"),
            ),
            "dev": (
                Task("audit-city", "Portland, Oregon", "Portland, Oregon"),
                Task("audit-person", "Mary Shelley, the novelist", "Mary Shelley"),
            ),
            "test": (Task("sealed", "SEALED_TEST_NEVER_IN_REPAIR", "hidden"),),
        }
        return self._splits

    def grade(self, rows):
        return tuple(
            replace(
                row,
                metrics={
                    "exact_match": float(
                        exact_match_score(row.output, row.task.ground_truth)
                    )
                },
            )
            for row in rows
        )


class Target(Agent):
    name = "frozen-boundary-target"
    description = "Execute actual versioned pure-rule code."

    async def _run(self, prompt, working_dir=None):
        path = self.files_dir / MODULE
        spec = importlib.util.spec_from_file_location("test_pinned_rule", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        evidence = {"context": prompt}
        result = run_rules(prompt, evidence, module.AUDIT_RULES)
        return Trajectory(
            output=result["output"],
            usage={
                "rule_replays": [
                    {
                        "snapshot_id": self.snapshot.id,
                        "module_path": MODULE,
                        "answer": prompt,
                        "evidence": evidence,
                        **result,
                    }
                ]
            },
        )


class Meta(Agent):
    name = "frozen-boundary-meta"
    description = "Controlled proposals; not an LLM efficacy experiment."

    def __init__(self, config):
        super().__init__(config)
        self.repair_prompt = ""

    async def _run(self, prompt, working_dir=None):
        if "[Recursive-CA-RSI: diagnose]" in prompt:
            evidence = json.loads(prompt.rsplit("\n", 1)[-1])
            case = next(
                c for c in evidence["rule_cases"] if c["task_id"] == "train-county"
            )
            return Trajectory(
                output=json.dumps(
                    {
                        "issues": [
                            {
                                "path": MODULE,
                                "kind": "overbroad_comma",
                                "claim": "Drops the state from a county name",
                                "evidence_task_ids": ["train-county"],
                                "repair": "Keep meaningful comma qualifiers",
                                "verification": "Disable comma on frozen county input",
                            }
                        ],
                        "interventions": [
                            {
                                "case_id": case["id"],
                                "disable": ["comma"],
                                "predicted_output": case["answer"],
                                "claim": "Comma truncates county name",
                            }
                        ],
                    }
                )
            )
        if "[Recursive-CA-RSI: repair]" in prompt:
            self.repair_prompt = prompt
            mode = "REPAIRED"
        else:
            mode = "FAULT"
        (working_dir / MODULE).write_text(source(mode))
        reason = {
            "files": [
                {
                    "path": MODULE,
                    "reason": "Preserve meaningful comma qualifiers"
                    if mode == "REPAIRED"
                    else "Try removing answer glosses",
                    "evidence_task_ids": ["train-county"],
                }
            ]
        }
        return Trajectory(
            output="Proposed rule change.\n<CHANGE_REASONS>"
            + json.dumps(reason)
            + "</CHANGE_REASONS>"
        )


def make(tmp_path):
    root = tmp_path / "source"
    (root / "middleware").mkdir(parents=True)
    (root / "components.yaml").write_text("code: ['middleware/**']\n")
    (root / MODULE).write_text(source("SEED"))
    config = {"source_dir": str(root), "llm": {"model_name": "offline"}}
    algorithm = RecursiveCARSI(
        {
            "max_steps": 1,
            "repair_attempts": 1,
            "meta_self_update": False,
            "rule_verification": True,
        },
        target=Target(config),
        worker=Meta(config),
        benchmark=Tasks(),
    )
    algorithm._run_dir = tmp_path / "run"
    for name in ("snapshots", "trajectories", "working"):
        (algorithm._run_dir / name).mkdir(parents=True)
    return algorithm


def records(algorithm, label):
    return [
        json.loads(p.read_text())
        for p in sorted(
            (algorithm._run_dir / "trajectories").glob("*-" + label + ".json")
        )
    ]


def test_interventions_reach_repair_and_keep_version_code_reason_links(tmp_path):
    algorithm = make(tmp_path)
    final = asyncio.run(algorithm.evolve())
    assert 'MODE = "REPAIRED"' in final.files[MODULE].content.decode()
    audits = records(algorithm, "rule_intervention")
    assert len(audits) == 2
    prediction = audits[0]["groups"][0]["report"]["hypotheses"][0]
    assert prediction["status"] == "prediction_supported_on_frozen_input"
    assert prediction["error_attribution_supported"]
    assert audits[1]["comparison"] == {
        "valid": True,
        "n": 2,
        "before_score": 0.5,
        "after_score": 1,
        "regressions": [],
    }
    assert (
        "registered_benchmark_binary_primary_metric" in algorithm.worker.repair_prompt
    )
    assert "SEALED_TEST_NEVER_IN_REPAIR" not in algorithm.worker.repair_prompt
    assert records(algorithm, "repair")[0]["accepted_on_train"]
    journal = (algorithm._run_dir / "RSI_ITERATIONS.md").read_text()
    for change in records(algorithm, "code_change"):
        assert change["reason_coverage"] == {"mapped": 1, "changed_files": 1}
        assert change["parent_id"] in journal and change["candidate_id"] in journal
        assert change["changes"][0]["reason"] in journal
    assert "规则干预执行证据" in journal
    # Rejected intermediate FAULT remains available even after a repair wins.
    faulty = records(algorithm, "code_change")[0]["candidate_id"]
    assert (algorithm._run_dir / "snapshots" / f"{faulty}.json").exists()


def test_source_mismatch_and_missing_module_are_not_mechanism_evidence(tmp_path):
    algorithm = make(tmp_path)
    learning = asyncio.run(
        algorithm._evaluate(
            algorithm.target.snapshot, algorithm.benchmark.train, label="train"
        )
    )
    first = learning[0]
    corrupted = dict(first.usage["rule_replays"][0])
    corrupted["snapshot_id"] = "other"
    bad = replace(first, usage={"rule_replays": [corrupted]})
    cases, skipped = _mechanism.collect_cases(algorithm.target.snapshot, [bad], 8)
    assert not cases and "snapshot" in skipped[0]["reason"]


def test_intervention_schema_cannot_select_hidden_or_unknown_cases():
    evidence = {
        "changed_paths": [],
        "before": [],
        "rule_cases": [{"id": "known", "rule_ids": ["comma"]}],
    }
    for request in (
        {"case_id": "sealed"},
        {"case_id": []},
        {
            "case_id": "known",
            "disable": ["unknown"],
            "predicted_output": "x",
            "claim": "x",
        },
    ):
        try:
            RecursiveCARSI._diagnosis(
                json.dumps({"issues": [], "interventions": [request]}), evidence
            )
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid intervention should be rejected")


def test_nonreproduced_boundary_cannot_support_an_attribution(tmp_path):
    algorithm = make(tmp_path)
    learning = asyncio.run(
        algorithm._evaluate(
            algorithm.target.snapshot, algorithm.benchmark.train, label="train"
        )
    )
    cases, _ = _mechanism.collect_cases(algorithm.target.snapshot, learning, 8)
    cases[0]["observed_output"] = "forged output"
    result = asyncio.run(
        _mechanism.audit(
            algorithm, algorithm.target.snapshot, cases, [], require_reproduction=True
        )
    )
    assert result["groups"][0]["status"] == "unavailable"
    assert "reproduce" in result["groups"][0]["problem"]


def test_binary_benchmark_normalization_replaces_child_placeholder_grades():
    task = Task("normalization", "question", "Paris")
    cases = [
        {
            "id": "normalization::boundary",
            "rule_ids": ["comma"],
            "trajectory": Trajectory(task=task),
        }
    ]
    variants = [
        {
            "enabled": enabled,
            "valid": True,
            "score": 0,
            "cases": [
                {
                    "id": cases[0]["id"],
                    "valid": True,
                    "output": "The Paris.",
                    "correct": False,
                }
            ],
        }
        for enabled in ([], ["comma"])
    ]
    report = _mechanism.regrade({"variants": variants}, cases, Tasks(), [])
    assert all(v["score"] == 1 for v in report["variants"])
    assert report["grading"] == "registered_benchmark_binary_primary_metric"
