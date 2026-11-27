"""Counterfactual audit of file-level harness edits before promotion."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

from flashrsi.algorithms.meta_harness import MetaHarness
from flashrsi.core import Snapshot


class CARSI(MetaHarness):
    """Propose from the incumbent, then compare all bounded edit subsets.

    A changed Snapshot path is one edit unit. Different changes within one file
    cannot be attributed separately by this algorithm.
    """

    name = "ca_rsi"
    description = "Audit file-level edits with paired counterfactual evaluations."
    _defaults = {"max_steps": 1, "max_edit_units": 3, "max_task_regressions": 0}
    _ranges = {
        "max_steps": (int, 1),
        "max_edit_units": (int, 1),
        "max_task_regressions": (int, 0),
    }

    def _check(self) -> None:
        super()._check()
        if not self.benchmark.train:
            raise ValueError(
                "CA-RSI requires a non-empty train split for proposer evidence"
            )
        if len(self.benchmark.dev) < self.config.max_steps:
            raise ValueError("CA-RSI requires at least one fresh dev task per step")
        if self.config.max_edit_units > 4:
            raise ValueError("CA-RSI max_edit_units cannot exceed 4 (16 variants)")
        train_ids = {task.task_id for task in self.benchmark.train}
        dev_ids = {task.task_id for task in self.benchmark.dev}
        if len(dev_ids) != len(self.benchmark.dev):
            raise ValueError("CA-RSI dev task IDs must be unique")
        if train_ids & dev_ids:
            raise ValueError("CA-RSI train and dev task IDs must be disjoint")

    async def evolve(self) -> Snapshot:
        run_dir = self._run_dir
        with ExitStack() as resources:
            if run_dir is None:
                self._run_dir = Path(resources.enter_context(TemporaryDirectory()))
                for name in ("snapshots", "trajectories", "working"):
                    (self._run_dir / name).mkdir()
            try:
                seed = incumbent = self.target.snapshot
                versions = [seed]
                await self._evaluate(seed, self.benchmark.train, label="train")
                dev = self.benchmark.dev
                retention_tasks = ()
                for step in range(self.config.max_steps):
                    tasks = dev[step :: self.config.max_steps]
                    baseline = await self._evaluate(
                        incumbent, tasks, label="audit_baseline"
                    )
                    candidate = await self._propose(
                        incumbent,
                        versions,
                        step,
                        history_labels=frozenset({"train", "worker"}),
                        records_description=(
                            "training evaluations and Worker traces only. Audit tasks, "
                            "scores and selection records are withheld from the Worker. "
                            f"Edit at most {self.config.max_edit_units} distinct harness "
                            "files in this proposal: a new, changed or deleted file "
                            "each counts as one edit unit. Larger proposals are skipped."
                        ),
                    )
                    if candidate is None:
                        retention_tasks += tasks
                        continue
                    versions.append(candidate)
                    paths = incumbent.changed_paths(candidate)
                    if len(paths) > self.config.max_edit_units:
                        self._record(
                            "audit",
                            {
                                "step": step,
                                "candidate_id": candidate.id,
                                "changed_paths": paths,
                                "status": "skipped_edit_budget",
                            },
                        )
                        retention_tasks += tasks
                        continue

                    full_mask = (1 << len(paths)) - 1
                    evaluated = {0: (incumbent, baseline)}
                    evaluated[full_mask] = (
                        candidate,
                        await self._evaluate(candidate, tasks, label="audit_candidate"),
                    )
                    invalid = []
                    for mask in range(1, full_mask):
                        selected_paths = tuple(
                            path
                            for index, path in enumerate(paths)
                            if mask & (1 << index)
                        )
                        try:
                            subset = self._subset(incumbent, candidate, selected_paths)
                        except (OSError, ValueError) as error:
                            invalid.append(
                                {"paths": selected_paths, "problem": str(error)}
                            )
                            continue
                        evaluated[mask] = (
                            subset,
                            await self._evaluate(subset, tasks, label="audit_subset"),
                        )

                    rows = []
                    eligible = []
                    for mask, (snapshot, result) in sorted(evaluated.items()):
                        regressions = sum(
                            result.per_task[task_id] < score
                            for task_id, score in baseline.per_task.items()
                        )
                        row = {
                            "paths": tuple(
                                path
                                for index, path in enumerate(paths)
                                if mask & (1 << index)
                            ),
                            "snapshot_id": snapshot.id,
                            "score": result.score,
                            "per_task_delta": {
                                task_id: result.per_task[task_id] - score
                                for task_id, score in baseline.per_task.items()
                            },
                            "regressions": regressions,
                        }
                        rows.append(row)
                        if (
                            mask
                            and result.score > baseline.score
                            and regressions <= self.config.max_task_regressions
                        ):
                            eligible.append(mask)

                    ranked = sorted(
                        eligible,
                        key=lambda mask: (
                            evaluated[mask][1].score,
                            -mask.bit_count(),
                            -mask,
                        ),
                        reverse=True,
                    )
                    selected_mask = 0
                    retention_checks = []
                    if ranked and retention_tasks:
                        retention_baseline = await self._evaluate(
                            incumbent, retention_tasks, label="retention_baseline"
                        )
                    for mask in ranked:
                        if retention_tasks:
                            retention = await self._evaluate(
                                evaluated[mask][0],
                                retention_tasks,
                                label="retention_candidate",
                            )
                            regressions = sum(
                                retention.per_task[task_id] < score
                                for task_id, score in (
                                    retention_baseline.per_task.items()
                                )
                            )
                            retention_checks.append(
                                {
                                    "snapshot_id": evaluated[mask][0].id,
                                    "regressions": regressions,
                                }
                            )
                            if regressions > self.config.max_task_regressions:
                                continue
                        selected_mask = mask
                        break
                    selected = evaluated[selected_mask][0]
                    full_score = evaluated[full_mask][1].score
                    self._record(
                        "audit",
                        {
                            "step": step,
                            "baseline_id": incumbent.id,
                            "candidate_id": candidate.id,
                            "changed_paths": paths,
                            "task_ids": [task.task_id for task in tasks],
                            "variants": rows,
                            "invalid_variants": invalid,
                            "retention_task_ids": [
                                task.task_id for task in retention_tasks
                            ],
                            "retention_checks": retention_checks,
                            "leave_one_out": {
                                path: (
                                    full_score
                                    - evaluated[full_mask ^ (1 << index)][1].score
                                    if full_mask ^ (1 << index) in evaluated
                                    else None
                                )
                                for index, path in enumerate(paths)
                            },
                            "selected_id": selected.id,
                            "selected_paths": tuple(
                                path
                                for index, path in enumerate(paths)
                                if selected_mask & (1 << index)
                            ),
                        },
                    )
                    if selected_mask:
                        incumbent = selected
                        if selected.id != candidate.id:
                            versions.append(selected)
                        if step + 1 < self.config.max_steps:
                            await self._evaluate(
                                incumbent, self.benchmark.train, label="train"
                            )
                    retention_tasks += tasks

                return self._promote(
                    seed, incumbent, audit_rounds=self.config.max_steps
                )
            finally:
                self._run_dir = run_dir

    def _subset(
        self, baseline: Snapshot, candidate: Snapshot, paths: tuple[str, ...]
    ) -> Snapshot:
        with baseline.check_out(working_dir=self._run_dir / "working") as directory:
            for path in paths:
                destination = directory / path
                entry = candidate.files.get(path)
                if entry is None:
                    destination.unlink()
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(entry.content)
                    destination.chmod(0o755 if entry.executable else 0o644)
            return self._commit(baseline, directory)


__all__ = ["CARSI"]
