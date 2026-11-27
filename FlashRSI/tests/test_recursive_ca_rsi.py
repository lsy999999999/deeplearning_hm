"""Offline evidence for recursive version activation and hidden-data boundaries."""

import asyncio
import json
from dataclasses import replace

import pytest

from flashrsi.algorithms.ca_rsi import CARSI
from flashrsi.algorithms.recursive_ca_rsi import RecursiveCARSI
from flashrsi.core import Agent, Benchmark, Task, Trajectory


class Tasks(Benchmark):
    name = "recursive-toy"
    description = "Task and Meta validation require distinct behaviors."
    primary_metric = "accuracy"
    metadata = {}

    def load(self):
        if self._splits is None:
            self._splits = {
                "train": (
                    Task("proposal-0", "proposal"),
                    Task("probe-0", "probe-training"),
                    Task("probe-1", "probe-training"),
                ),
                "dev": tuple(
                    Task(f"audit-{i}", "task" if i % 2 == 0 else "meta-validation")
                    for i in range(4)
                ),
                "test": (Task("sealed-test", "sealed-test-content"),),
            }
        return self._splits

    def grade(self, rows):
        return tuple(
            replace(row, metrics={"accuracy": float(row.output or 0)}) for row in rows
        )


class Target(Agent):
    name = "recursive-target"
    description = "A deterministic Task agent with a versioned policy."

    async def _run(self, prompt, working_dir=None):
        policy = (self.files_dir / "skills/policy.md").read_text()
        passed = policy == "PROBE" or (policy == "MAIN" and prompt != "meta-validation")
        return Trajectory(output=str(int(passed)))


class Meta(Agent):
    name = "recursive-meta"
    description = "A deterministic Meta agent; its own strategy controls its edits."

    def __init__(self, config, *, self_strategy="BETTER", repair=False):
        super().__init__(config)
        self.self_strategy = self_strategy
        self.repair = repair
        self.calls = []
        self.self_feedback = []
        self.probe_evidence = []

    async def _run(self, prompt, working_dir=None):
        strategy = (self.files_dir / "skills/strategy.md").read_text()
        self.calls.append((prompt.splitlines()[0], strategy))
        if "[Recursive-CA-RSI: diagnose]" in prompt:
            diagnosis = {
                "issues": [
                    {
                        "path": "skills/policy.md",
                        "kind": "overbroad_rule",
                        "claim": "Bad policy fails the supplied training case",
                        "evidence_task_ids": ["proposal-0"],
                        "repair": "Use MAIN",
                        "verification": "Execute the supplied training case again",
                    }
                ]
            }
            return Trajectory(output=json.dumps(diagnosis))
        if "[Recursive-CA-RSI: improve-meta]" in prompt:
            directory = next(working_dir.glob("meta-feedback-*"))
            self.self_feedback.append(
                "\n".join(p.read_text() for p in directory.rglob("*.json"))
            )
            if self.self_strategy == "RAISE":
                raise RuntimeError("self edit failed")
            (working_dir / "skills/strategy.md").write_text(self.self_strategy)
        elif "[Recursive-CA-RSI: meta-probe]" in prompt:
            evidence = next(working_dir.glob("probe-evidence-*/train.json"))
            self.probe_evidence.append(evidence.read_text())
            policy = "PROBE" if strategy == "BETTER" else "MAIN"
            (working_dir / "skills/policy.md").write_text(policy)
        elif "[Recursive-CA-RSI: repair]" in prompt:
            (working_dir / "skills/policy.md").write_text("MAIN")
        else:
            policy = (
                "BAD" if self.repair else "PROBE" if strategy == "BETTER" else "MAIN"
            )
            (working_dir / "skills/policy.md").write_text(policy)
        return Trajectory(output="edited reusable policy")


def source(root, name, file, text):
    path = root / name
    (path / "skills").mkdir(parents=True)
    (path / "components.yaml").write_text(
        "skill: ['skills/**']\nconfig: ['agent.yaml']\ncode: ['middleware/**']\n"
    )
    (path / file).write_text(text)
    return {"source_dir": str(path), "llm": {"model_name": "offline"}, "type": "agent"}


def make(tmp_path, **options):
    target = Target(source(tmp_path, "task", "skills/policy.md", "SEED"))
    worker = Meta(
        source(tmp_path, "meta", "skills/strategy.md", "BASIC"),
        self_strategy=options.pop("self_strategy", "BETTER"),
        repair=options.pop("repair", False),
    )
    algorithm = RecursiveCARSI(
        {
            "max_steps": 2,
            "repair_attempts": 0,
            "meta_repeats": 2,
            "meta_train_tasks_per_step": 1,
            **options,
        },
        target=target,
        worker=worker,
        benchmark=Tasks(),
    )
    algorithm._run_dir = tmp_path / "run"
    for name in ("snapshots", "trajectories", "working"):
        (algorithm._run_dir / name).mkdir(parents=True)
    return algorithm


def records(algorithm, label):
    return [
        json.loads(p.read_text())
        for p in sorted((algorithm._run_dir / "trajectories").glob(f"*-{label}.json"))
    ]


def test_meta_promotes_on_downstream_gain_and_next_round_uses_new_version(tmp_path):
    algorithm = make(tmp_path)
    original = algorithm.worker.snapshot.id
    asyncio.run(algorithm.evolve())
    decisions = records(algorithm, "meta_decision")
    assert decisions[0]["accepted"] is True
    assert decisions[0]["mean_paired_gain"] == 1
    assert len(decisions[0]["trials"]) == 2
    assert (
        decisions[0]["trials"][0]["original"]["snapshot_id"]
        == decisions[0]["trials"][1]["original"]["snapshot_id"]
    )
    calls = [
        strategy
        for prompt, strategy in algorithm.worker.calls
        if "Meta-Harness: propose" in prompt
    ]
    assert calls == ["BASIC", "BETTER"]
    assert algorithm.worker.snapshot.id != original
    saved = json.loads((algorithm._run_dir / "snapshots/worker-final.json").read_text())
    assert saved["id"] == algorithm.worker.snapshot.id


@pytest.mark.parametrize("self_strategy", ["WORSE", "RAISE"])
def test_failed_or_unhelpful_meta_revision_does_not_change_worker(
    tmp_path, self_strategy
):
    algorithm = make(tmp_path, self_strategy=self_strategy)
    original = algorithm.worker.snapshot.id
    asyncio.run(algorithm.evolve())
    assert algorithm.worker.snapshot.id == original
    assert all(not r["accepted"] for r in records(algorithm, "meta_decision"))
    assert all(
        strategy == "BASIC"
        for prompt, strategy in algorithm.worker.calls
        if "Meta-Harness: propose" in prompt
    )


def test_diagnosis_and_repair_are_generated_by_meta_and_verified_on_train(tmp_path):
    algorithm = make(tmp_path, repair=True, repair_attempts=1, self_strategy="WORSE")
    final = asyncio.run(algorithm.evolve())
    assert final.files["skills/policy.md"].content == b"MAIN"
    diagnosis = records(algorithm, "diagnosis")[0]
    assert diagnosis["status"] == "hypothesis_not_verified"
    repair = records(algorithm, "repair")[0]
    assert repair["accepted_on_train"] is True
    assert repair["before_score"] == 0 and repair["after_score"] == 1
    assert records(algorithm, "audit")[0]["selected_paths"] == ["skills/policy.md"]


def test_probe_and_final_test_evidence_stays_out_of_self_edit_and_diagnosis(tmp_path):
    algorithm = make(tmp_path, repair_attempts=1)
    asyncio.run(algorithm.evolve())
    for text in algorithm.worker.self_feedback:
        assert "sealed-test-content" not in text
        assert '"task_id": "probe-' not in text
        assert "meta-validation" not in text
        assert '"task_id": "audit-1"' not in text
        assert '"task_id": "audit-3"' not in text
    assert '"task_id": "audit-0"' in algorithm.worker.self_feedback[0]
    assert '"task_id": "audit-2"' not in algorithm.worker.self_feedback[0]
    assert len(algorithm.worker.probe_evidence) == 4
    for index in range(0, 4, 2):
        assert (
            algorithm.worker.probe_evidence[index]
            == algorithm.worker.probe_evidence[index + 1]
        )
        assert "meta-validation" not in algorithm.worker.probe_evidence[index]
        assert "sealed-test" not in algorithm.worker.probe_evidence[index]
    protocol = records(algorithm, "recursive_protocol")[0]
    groups = [
        set(protocol[k])
        for k in (
            "task_train_ids",
            "task_audit_ids",
            "meta_probe_train_ids",
            "meta_probe_dev_ids",
        )
    ]
    assert all(not a & b for i, a in enumerate(groups) for b in groups[i + 1 :])


def test_meta_probe_does_not_promote_its_shadow_task_candidate(tmp_path):
    algorithm = make(tmp_path)
    asyncio.run(algorithm.evolve())
    # Revised Meta's PROBE policy wins its challenge, but does not replace the
    # MAIN policy already selected on actual task audits (where they tie).
    assert algorithm.target.snapshot.files["skills/policy.md"].content == b"MAIN"


def test_diagnosis_rejects_unknown_evidence_and_outside_paths():
    evidence = {"changed_paths": ["a.md"], "before": [{"task": {"task_id": "train"}}]}
    issue = {
        "path": "a.md",
        "kind": "bug",
        "claim": "cause",
        "repair": "fix",
        "verification": "check",
        "evidence_task_ids": ["sealed"],
    }
    with pytest.raises(ValueError, match="training evidence"):
        RecursiveCARSI._diagnosis(json.dumps({"issues": [issue]}), evidence)
    issue["evidence_task_ids"] = ["train"]
    issue["path"] = "grader.py"
    with pytest.raises(ValueError, match="outside"):
        RecursiveCARSI._diagnosis(json.dumps({"issues": [issue]}), evidence)


@pytest.mark.parametrize("mutation", ["overlap", "few_train", "few_dev"])
def test_recursive_data_requirements(tmp_path, mutation):
    benchmark = Tasks()
    splits = benchmark.load()
    if mutation == "overlap":
        splits["test"] = splits["train"][:1]
    elif mutation == "few_train":
        splits["train"] = splits["train"][:2]
    else:
        splits["dev"] = splits["dev"][:3]
    target = Target(source(tmp_path, "task", "skills/policy.md", "SEED"))
    worker = Meta(source(tmp_path, "meta", "skills/strategy.md", "BASIC"))
    with pytest.raises(ValueError):
        RecursiveCARSI(
            {"max_steps": 2}, target=target, worker=worker, benchmark=benchmark
        )


def test_preflight_excludes_broken_dependency_from_attribution(tmp_path):
    config = source(tmp_path, "task", "skills/policy.md", "SEED")
    target = Target(config)
    snapshot = target.snapshot
    with snapshot.check_out() as directory:
        (directory / "agent.yaml").write_text("middlewares: [missing]\n")
        broken = snapshot.commit(directory)
    assert "missing registered middleware" in CARSI._variant_problem(broken)


def test_worker_version_is_restored_after_probe_exception(tmp_path):
    algorithm = make(tmp_path)
    original = algorithm.worker.snapshot
    with original.check_out() as directory:
        (directory / "skills/strategy.md").write_text("CHANGED")
        revised = original.commit(directory)
    with pytest.raises(RuntimeError), algorithm._worker_version(revised):
        assert algorithm.worker.snapshot.id == revised.id
        raise RuntimeError("probe failed")
    assert algorithm.worker.snapshot.id == original.id


class DependencyMeta(Meta):
    async def _run(self, prompt, working_dir=None):
        (working_dir / "agent.yaml").write_text("middlewares: [gate]\n")
        (working_dir / "middleware").mkdir(exist_ok=True)
        (working_dir / "middleware/gate.py").write_text(
            "def before_final(answer, ctx): return None\n"
        )
        (working_dir / "skills/policy.md").write_text("MAIN")
        return Trajectory(output="registered gate")


def test_dependency_invalid_subsets_have_no_score_or_leave_one_out_claim(tmp_path):
    target = Target(source(tmp_path, "task", "skills/policy.md", "SEED"))
    worker = DependencyMeta(source(tmp_path, "meta", "skills/strategy.md", "BASIC"))
    algorithm = CARSI({"max_steps": 1}, target=target, worker=worker, benchmark=Tasks())
    algorithm._run_dir = tmp_path / "run"
    for name in ("snapshots", "trajectories", "working"):
        (algorithm._run_dir / name).mkdir(parents=True)
    asyncio.run(algorithm.evolve())
    audit = records(algorithm, "audit")[0]
    assert any(row["paths"] == ["agent.yaml"] for row in audit["invalid_variants"])
    assert not any(row["paths"] == ["agent.yaml"] for row in audit["variants"])
    assert audit["leave_one_out"]["middleware/gate.py"] is None


class FailingTarget(Target):
    async def _run(self, prompt, working_dir=None):
        if (self.files_dir / "skills/policy.md").read_text() == "MAIN":
            return Trajectory(status="error", error_message="runtime fault")
        return await super()._run(prompt, working_dir)


def test_execution_failure_is_invalid_instead_of_semantically_negative(tmp_path):
    target = FailingTarget(source(tmp_path, "task", "skills/policy.md", "SEED"))
    worker = Meta(source(tmp_path, "meta", "skills/strategy.md", "BASIC"))
    algorithm = CARSI({"max_steps": 1}, target=target, worker=worker, benchmark=Tasks())
    algorithm._run_dir = tmp_path / "run"
    for name in ("snapshots", "trajectories", "working"):
        (algorithm._run_dir / name).mkdir(parents=True)
    final = asyncio.run(algorithm.evolve())
    assert final.files["skills/policy.md"].content == b"SEED"
    audit = records(algorithm, "audit")[0]
    assert len(audit["variants"]) == 1
    assert "execution failed" in audit["invalid_variants"][0]["problem"]
    assert audit["leave_one_out"]["skills/policy.md"] is None


def test_self_edit_cannot_take_effect_without_downstream_improvement(tmp_path):
    algorithm = make(tmp_path, self_strategy="WORSE")
    original = algorithm.worker.snapshot
    asyncio.run(algorithm.evolve())
    assert records(algorithm, "meta_decision")[0]["mean_paired_gain"] == 0
    assert algorithm.worker.snapshot.id == original.id


def test_meta_specific_data_size_is_checked_before_model_calls(tmp_path):
    with pytest.raises(ValueError, match="meta_train_tasks_per_step"):
        make(tmp_path, meta_train_tasks_per_step=2)
