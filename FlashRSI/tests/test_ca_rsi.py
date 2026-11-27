"""Offline counterfactual audit tests."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from flashrsi.algorithms.ca_rsi import CARSI
from flashrsi.core import Agent, Benchmark, Task, Trajectory


class ToyTasks(Benchmark):
    name = "ca-rsi-toy"
    description = "Offline tasks with a harmful edit hidden by a useful edit."
    primary_metric = "accuracy"
    metadata = {}

    def load(self):
        if self._splits is None:
            self._splits = {
                "train": (Task("train-0", "train-0"),),
                "dev": tuple(Task(f"dev-{i}", f"dev-{i}") for i in range(4)),
                "test": (Task("test-0", "test-0"),),
            }
        return self._splits

    def grade(self, results):
        return tuple(
            replace(item, metrics={"accuracy": float(item.output)})
            for item in results
        )


class ToyTarget(Agent):
    name = "ca-rsi-target"
    description = "Reads two versioned prompt files."

    async def _run(self, prompt, working_dir=None):
        useful = (self.files_dir / "skills/a.md").read_text() == "GOOD"
        harmful = (self.files_dir / "skills/b.md").read_text() == "BAD"
        passed = useful and (not harmful or prompt != "dev-3")
        return Trajectory(output=str(float(passed)))


class InteractionTarget(ToyTarget):
    name = "ca-rsi-interaction-target"

    async def _run(self, prompt, working_dir=None):
        useful = (self.files_dir / "skills/a.md").read_text() == "GOOD"
        partner = (self.files_dir / "skills/b.md").read_text() == "BAD"
        return Trajectory(output=str(float(useful and partner)))


class RetentionTarget(ToyTarget):
    name = "ca-rsi-retention-target"

    async def _run(self, prompt, working_dir=None):
        useful = (self.files_dir / "skills/a.md").read_text() == "GOOD"
        harmful = (self.files_dir / "skills/b.md").read_text() == "BAD"
        passed = useful and (
            prompt in {"dev-1", "dev-3"} if harmful else prompt == "dev-0"
        )
        return Trajectory(output=str(float(passed)))


class ToyWorker(Agent):
    name = "ca-rsi-worker"
    description = "Creates a useful and a harmful edit."

    def __init__(self):
        super().__init__({"type": "agent", "llm": {"model_name": "offline"}})
        self.seen_records = []
        self.prompts = []
        self.turn = 0

    async def _run(self, prompt, working_dir=None):
        self.prompts.append(prompt)
        history = next(working_dir.glob("history-*"))
        self.seen_records.append(
            sorted(path.name for path in (history / "trajectories").glob("*.json"))
        )
        if self.turn == 0:
            (working_dir / "skills/a.md").write_text("GOOD")
        (working_dir / "skills/b.md").write_text("BAD")
        self.turn += 1
        return Trajectory(output="edited")


class CARSIStudy(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        (source / "skills").mkdir(parents=True)
        (source / "components.yaml").write_text("skill: ['skills/**']\n")
        (source / "skills/a.md").write_text("SEED")
        (source / "skills/b.md").write_text("SEED")
        self.target = ToyTarget(
            {"source_dir": str(source), "llm": {"model_name": "offline"}}
        )
        self.worker = ToyWorker()

    def make_algorithm(self, **options):
        algorithm = CARSI(
            {"max_steps": 1, **options},
            target=self.target,
            worker=self.worker,
            benchmark=ToyTasks(),
        )
        algorithm._run_dir = self.root / "run"
        for name in ("snapshots", "trajectories", "working"):
            (algorithm._run_dir / name).mkdir(parents=True)
        return algorithm

    def records(self, label):
        return [
            json.loads(path.read_text())
            for path in sorted((self.root / "run/trajectories").glob(f"*-{label}.json"))
        ]

    def test_harmful_file_is_removed_and_marginal_effect_is_recorded(self):
        algorithm = self.make_algorithm()
        final = asyncio.run(algorithm.evolve())

        self.assertEqual(final.files["skills/a.md"].content, b"GOOD")
        self.assertEqual(final.files["skills/b.md"].content, b"SEED")
        audit = self.records("audit")[0]
        self.assertEqual(len(audit["variants"]), 4)
        self.assertEqual(audit["selected_paths"], ["skills/a.md"])
        self.assertEqual(audit["leave_one_out"]["skills/b.md"], -0.25)
        self.assertEqual(self.records("decision")[-1]["candidate_id"], final.id)
        self.assertTrue(
            all("audit" not in name for name in self.worker.seen_records[0])
        )
        self.assertIn("training evaluations", self.worker.prompts[0])
        self.assertIn("Edit at most 3 distinct harness files", self.worker.prompts[0])
        self.assertNotIn("Score/decision records", self.worker.prompts[0])

    def test_fresh_dev_tasks_and_audit_records_remain_hidden(self):
        algorithm = self.make_algorithm(max_steps=2)
        final = asyncio.run(algorithm.evolve())

        self.assertEqual(final.files["skills/a.md"].content, b"GOOD")
        self.assertEqual(final.files["skills/b.md"].content, b"SEED")
        audits = self.records("audit")
        self.assertEqual(
            [row["task_ids"] for row in audits],
            [["dev-0", "dev-2"], ["dev-1", "dev-3"]],
        )
        self.assertTrue(all(
            "audit" not in name
            for records in self.worker.seen_records
            for name in records
        ))

    def test_interacting_edits_are_kept_together(self):
        self.target = InteractionTarget(dict(self.target.config))
        algorithm = self.make_algorithm()
        final = asyncio.run(algorithm.evolve())

        self.assertEqual(final.files["skills/a.md"].content, b"GOOD")
        self.assertEqual(final.files["skills/b.md"].content, b"BAD")
        audit = self.records("audit")[0]
        self.assertEqual(audit["selected_paths"], ["skills/a.md", "skills/b.md"])
        self.assertEqual(audit["leave_one_out"]["skills/a.md"], 1.0)
        self.assertEqual(audit["leave_one_out"]["skills/b.md"], 1.0)

    def test_later_gain_cannot_erase_an_earlier_correct_answer(self):
        self.target = RetentionTarget(dict(self.target.config))
        algorithm = self.make_algorithm(max_steps=2)
        final = asyncio.run(algorithm.evolve())

        self.assertEqual(final.files["skills/a.md"].content, b"GOOD")
        self.assertEqual(final.files["skills/b.md"].content, b"SEED")
        second = self.records("audit")[1]
        self.assertEqual(second["selected_paths"], [])
        self.assertEqual(second["retention_task_ids"], ["dev-0", "dev-2"])
        self.assertEqual(second["retention_checks"][0]["regressions"], 1)

    def test_missing_train_split_is_rejected(self):
        class NoTrain(ToyTasks):
            def load(self):
                splits = super().load()
                splits["train"] = ()
                return splits

        with self.assertRaisesRegex(ValueError, "train split"):
            CARSI({}, target=self.target, worker=self.worker, benchmark=NoTrain())

    def test_variant_budget_is_bounded(self):
        with self.assertRaisesRegex(ValueError, "max_edit_units"):
            self.make_algorithm(max_edit_units=5)


if __name__ == "__main__":
    unittest.main()
