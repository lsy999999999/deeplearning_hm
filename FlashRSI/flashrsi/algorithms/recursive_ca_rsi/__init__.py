"""Separate Task/Meta evolution, with blind downstream validation of Meta edits."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from flashrsi.algorithms.ca_rsi import CARSI
from flashrsi.algorithms.meta_harness._journal import REASON_INSTRUCTION
from flashrsi.algorithms.recursive_ca_rsi import _mechanism
from flashrsi.core import Benchmark
from flashrsi.core.snapshot import discard_runtime_files


class _TaskView(Benchmark):
    """Keep source grading/test unchanged while reserving independent probes."""

    def __init__(self, source, train, dev):
        super().__init__()
        self.source = source
        self.name = source.name
        self.description = source.description
        self.primary_metric = source.primary_metric
        self.metadata = dict(source.metadata)
        self._splits = {"train": train, "dev": dev, "test": source.test}

    def load(self):
        return self._splits

    def grade(self, results):
        return self.source.grade(results)


class RecursiveCARSI(CARSI):
    """Evolve task harnesses and validate revisions of the proposer itself.

    Proposal/repair uses only public training evidence. A Meta revision must
    generate better Task patches on fresh, paired challenges before it becomes
    the Worker for the next round. The evaluator and selection rule stay fixed.
    """

    name = "recursive_ca_rsi"
    description = "Diagnose/repair Task edits and recursively validate Meta edits."
    _defaults = {
        **CARSI._defaults,
        "max_steps": 2,
        "repair_attempts": 1,
        "meta_self_update": True,
        "rule_verification": False,
        "max_rule_cases": 8,
        "max_meta_edit_units": 3,
        "meta_repeats": 2,
        "meta_train_tasks_per_step": 2,
    }
    _ranges = {
        **CARSI._ranges,
        "repair_attempts": (int, 0),
        "meta_self_update": bool,
        "rule_verification": bool,
        "max_rule_cases": (int, 1),
        "max_meta_edit_units": (int, 1),
        "meta_repeats": (int, 1),
        "meta_train_tasks_per_step": (int, 1),
    }

    def _check(self):
        super()._check()
        if self.config.max_rule_cases > 16:
            raise ValueError("max_rule_cases cannot exceed 16")
        if self.config.repair_attempts > 2:
            raise ValueError("repair_attempts cannot exceed 2")
        if self.config.max_meta_edit_units > 4:
            raise ValueError("max_meta_edit_units cannot exceed 4")
        if not self.worker.component_rules:
            raise ValueError("recursive_ca_rsi needs a versioned agent Worker")
        source = self.benchmark
        all_ids = [task.task_id for tasks in source.load().values() for task in tasks]
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("All train/dev/test task IDs must be unique and disjoint")
        steps = self.config.max_steps
        reserved = (
            steps * self.config.meta_train_tasks_per_step
            if self.config.meta_self_update
            else 0
        )
        if len(source.train) <= reserved:
            raise ValueError(
                "recursive_ca_rsi needs train-size > rounds * meta_train_tasks_per_step"
            )
        if len(source.dev) < (2 if self.config.meta_self_update else 1) * steps:
            raise ValueError(
                "recursive_ca_rsi needs enough dev tasks for each enabled audit lane"
            )
        # This split is fixed before any rollout; no task is selected by score.
        self._probe_train = tuple(
            source.train[-reserved:][step::steps] for step in range(steps)
        )
        self._probe_dev = source.dev[1::2] if self.config.meta_self_update else ()
        if self.config.meta_self_update:
            self.benchmark = _TaskView(
                source, source.train[:-reserved], source.dev[::2]
            )
        else:
            self._probe_train = ()
            self.benchmark = _TaskView(source, source.train, source.dev)
        self.worker.set_scope({name: "rw" for name in self.worker.component_rules})
        self._meta_feedback = []
        self._task_feedback = []
        self._consumed_tasks = ()

    async def evolve(self):
        self._record(
            "recursive_protocol",
            {
                "task_train_ids": [t.task_id for t in self.benchmark.train],
                "task_audit_ids": [t.task_id for t in self.benchmark.dev],
                "meta_probe_train_ids": [
                    t.task_id for group in self._probe_train for t in group
                ],
                "meta_probe_dev_ids": [t.task_id for t in self._probe_dev],
                "meta_repeats": self.config.meta_repeats,
                "meta_self_update": self.config.meta_self_update,
                "test_used_for_selection": False,
                "scope": "adaptive_development_validation_not_final_efficacy",
            },
        )
        final = await super().evolve()
        if self._run_dir is not None:
            self.worker.snapshot.save(self._run_dir / "snapshots" / "worker-final.json")
        return final

    async def _propose(self, seed, versions, step, **options):
        options["history_labels"] = frozenset(options.get("history_labels", ())) | {
            "task_feedback",
            "rule_intervention",
        }
        options["records_description"] = (
            "training/Worker traces and consumed task_feedback only. Consumed audits "
            "are now adaptive training feedback; future audits, Meta probes and final "
            "tests are withheld. "
            f"Edit at most {self.config.max_edit_units} distinct harness files."
        )
        if self.config.rule_verification:
            options["records_description"] += (
                " For deterministic answer transformations, define an ordered AUDIT_RULES "
                "list in the same middleware module (from runtime.rule_audit import Rule) "
                "and execute it with ctx.apply_answer_rules(answer, evidence, AUDIT_RULES). "
                "Each Rule(id, pure_function) takes (answer, evidence) and returns a string; "
                "no model calls or I/O. Keep the unmodified original examples. "
            )
        candidate = await super()._propose(seed, versions, step, **options)
        if candidate is None or not self.config.repair_attempts:
            return candidate
        if len(seed.changed_paths(candidate)) > self.config.max_edit_units:
            return candidate
        learning = self.benchmark.train + self._consumed_tasks
        baseline = await self._evaluate(seed, learning, label="repair_baseline")
        current = await self._evaluate(candidate, learning, label="repair_candidate")
        if not self._usable(baseline):
            return candidate
        for attempt in range(self.config.repair_attempts):
            rule_cases, rule_skipped = (
                _mechanism.collect_cases(candidate, current, self.config.max_rule_cases)
                if self.config.rule_verification
                else ([], [])
            )
            evidence = {
                "rule_cases": _mechanism.visible_cases(rule_cases),
                "rule_case_skips": rule_skipped,
                "changed_paths": seed.changed_paths(candidate),
                "candidate_files": {
                    path: text
                    for path, text in self._read_text(candidate).items()
                    if path in seed.changed_paths(candidate)
                },
                "baseline_files": {
                    path: text
                    for path, text in self._read_text(seed).items()
                    if path in seed.changed_paths(candidate)
                },
                "before": [self._trajectory_evidence(t) for t in baseline],
                "after": [self._trajectory_evidence(t) for t in current],
                "preflight_problem": self._variant_problem(candidate),
                "previous_audit_feedback": [
                    {key: value for key, value in item.items() if key != "evaluations"}
                    for item in self._task_feedback
                ],
            }
            prompt = (
                "[Recursive-CA-RSI: diagnose]\nDiagnose this Task harness proposal "
                "using ONLY the supplied training and consumed audit evidence. Separate runtime faults, "
                "answer-format mismatches, reasoning errors and uncertain hypotheses. "
                'Return ONLY JSON: {"issues": [{"path": "changed file", '
                '"kind": "error type", "claim": "specific suspected cause", '
                '"evidence_task_ids": ["training ID"], "repair": "minimal fix", '
                '"verification": "a falsifiable check"}]}. An empty issues list '
                "is valid. You may additionally return top-level interventions: "
                "[{case_id: supplied rule_case ID, disable: [known rule ID], "
                "predicted_output: exact output, claim: mechanism hypothesis}]. "
                "These structured requests will be executed AFTER your prediction. "
                "Rules may be useful on some inputs and harmful on others. "
                "A score difference alone does not establish causation.\n"
                + json.dumps(evidence, ensure_ascii=False)
            )
            try:
                diagnosis = self._diagnosis(await self.worker.ask(prompt), evidence)
            except (RuntimeError, ValueError) as error:
                self._record(
                    "diagnosis",
                    {
                        "step": step,
                        "attempt": attempt,
                        "status": "unavailable",
                        "problem": str(error),
                    },
                )
                break
            self._record(
                "diagnosis",
                {
                    "step": step,
                    "attempt": attempt,
                    "candidate_id": candidate.id,
                    **diagnosis,
                },
            )
            rule_evidence = None
            if self.config.rule_verification:
                rule_evidence = await _mechanism.audit(
                    self,
                    candidate,
                    rule_cases,
                    diagnosis.get("interventions", []),
                    require_reproduction=True,
                )
                rule_evidence["skipped"] = rule_skipped
                self._record(
                    "rule_intervention",
                    {
                        "step": step,
                        "attempt": attempt,
                        "stage": "candidate_before_repair",
                        **rule_evidence,
                    },
                )
                diagnosis["executed_rule_interventions"] = rule_evidence
            if not diagnosis["issues"]:
                break
            self._save_snapshot(candidate)
            try:
                with candidate.check_out(
                    working_dir=self._run_dir / "working"
                ) as directory:
                    explanation = await self.worker.edit(
                        directory,
                        "[Recursive-CA-RSI: repair]\nImplement a minimal Task harness "
                        "repair for the supplied diagnosis. Treat it as a hypothesis, "
                        "preserve useful changes, do not hard-code answers or task IDs. "
                        f"Change at most {self.config.max_edit_units} files relative to "
                        "the original parent. Do not access audit/test data or graders.\n"
                        + json.dumps(diagnosis, ensure_ascii=False)
                        + REASON_INSTRUCTION,
                    )
                    discard_runtime_files(directory)
                    repaired = self._commit(seed, directory)
                self._record_change(
                    candidate,
                    repaired,
                    explanation,
                    step=step,
                    phase="task_repair",
                    subject="Task",
                    attempt=attempt,
                )
                problem = self._variant_problem(repaired)
                if len(seed.changed_paths(repaired)) > self.config.max_edit_units:
                    problem = "repair exceeded edit budget"
                result = (
                    None
                    if problem
                    else await self._evaluate(repaired, learning, label="repair_check")
                )
                boundary_check = None
                if rule_cases:
                    repair_evidence = await _mechanism.audit(
                        self,
                        repaired,
                        rule_cases,
                        [],
                        require_reproduction=False,
                    )
                    boundary_check = _mechanism.comparison(
                        rule_evidence, repair_evidence
                    )
                    self._record(
                        "rule_intervention",
                        {
                            "step": step,
                            "attempt": attempt,
                            "stage": "repair_on_same_frozen_inputs",
                            **repair_evidence,
                            "comparison": boundary_check,
                        },
                    )
                accepted = (
                    (
                        boundary_check is None
                        or (
                            boundary_check["valid"]
                            and not boundary_check["regressions"]
                        )
                    )
                    and result is not None
                    and self._usable(result)
                    and (
                        not self._usable(current)
                        or (
                            result.score > current.score
                            and self._regressions(result, current)
                            <= self.config.max_task_regressions
                        )
                    )
                )
                self._record(
                    "repair",
                    {
                        "step": step,
                        "attempt": attempt,
                        "before_id": candidate.id,
                        "after_id": repaired.id,
                        "accepted_on_train": accepted,
                        "frozen_boundary_check": boundary_check,
                        "problem": problem,
                        "before_score": current.score,
                        "after_score": result.score if result is not None else None,
                    },
                )
                if accepted:
                    candidate, current = repaired, result
                else:
                    break
            except (RuntimeError, ValueError, OSError) as error:
                self._record(
                    "repair",
                    {
                        "step": step,
                        "attempt": attempt,
                        "accepted_on_train": False,
                        "problem": str(error),
                    },
                )
                break
        return candidate

    @staticmethod
    def _diagnosis(text, evidence):
        text = text.strip()
        if text.startswith("```") and text.endswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        try:
            result = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError("Diagnosis is not JSON") from error
        if not isinstance(result, dict) or not isinstance(result.get("issues"), list):
            raise ValueError("Diagnosis needs an issues list")
        task_ids = {row["task"]["task_id"] for row in evidence["before"]}
        for issue in result["issues"]:
            if (
                not isinstance(issue, dict)
                or issue.get("path") not in evidence["changed_paths"]
            ):
                raise ValueError("Diagnosis references a file outside the proposal")
            for field in ("kind", "claim", "repair", "verification"):
                if not isinstance(issue.get(field), str) or not issue[field].strip():
                    raise ValueError(f"Diagnosis missing {field}")
            ids = issue.get("evidence_task_ids")
            if (
                not isinstance(ids, list)
                or not ids
                or any(not isinstance(i, str) or i not in task_ids for i in ids)
            ):
                raise ValueError("Diagnosis needs supplied training evidence IDs")
        interventions = result.get("interventions", [])
        known_cases = {case["id"]: case for case in evidence.get("rule_cases", [])}
        if not isinstance(interventions, list):
            raise ValueError("interventions must be a list")
        for request in interventions:
            if (
                not isinstance(request, dict)
                or not isinstance(request.get("case_id"), str)
                or request["case_id"] not in known_cases
            ):
                raise ValueError("Intervention must reference a supplied rule_case")
            rule_ids = known_cases[request["case_id"]]["rule_ids"]
            disabled = request.get("disable")
            if (
                not isinstance(disabled, list)
                or not disabled
                or any(
                    not isinstance(rule, str) or rule not in rule_ids
                    for rule in disabled
                )
                or not isinstance(request.get("predicted_output"), str)
                or not isinstance(request.get("claim"), str)
            ):
                raise ValueError("Invalid structured rule intervention")
        # The model's explanation remains a hypothesis. Controller results are
        # attached later; merely accepting this JSON never verifies its claim.
        return {
            "issues": result["issues"],
            "interventions": interventions,
            "status": "hypothesis_not_verified",
        }

    @staticmethod
    def _usable(result):
        return all(t.status == "finished" and not t.error_message for t in result)

    @staticmethod
    def _regressions(result, baseline):
        return sum(result.per_task[i] < value for i, value in baseline.per_task.items())

    @contextmanager
    def _worker_version(self, snapshot):
        previous = self.worker.snapshot
        self.worker.update(snapshot)
        try:
            yield
        finally:
            self.worker.update(previous)

    async def _on_round_end(self, step, incumbent):
        """Generate self-edit first; reveal this round's challenge only afterwards."""
        self._publish_task_feedback(step)
        if not self.config.meta_self_update:
            self._record(
                "meta_decision",
                {
                    "step": step,
                    "accepted": False,
                    "reason": "meta_self_update_disabled",
                },
            )
            return
        original = self.worker.snapshot
        self._save_snapshot(original)
        try:
            with original.check_out(working_dir=self._run_dir / "working") as directory:
                # Training plus completed Task audits and consumed Meta summaries.
                # No future audit, probe contents, or final test records are copied.
                with TemporaryDirectory(
                    prefix="meta-feedback-", dir=directory
                ) as temporary:
                    feedback = Path(temporary)
                    visible = feedback / "records"
                    visible.mkdir()
                    for record in (self._run_dir / "trajectories").glob("*.json"):
                        label = record.stem.split("-", 1)[-1]
                        if label in {
                            "train",
                            "diagnosis",
                            "repair",
                            "repair_baseline",
                            "repair_candidate",
                            "repair_check",
                            "rule_intervention",
                            "task_feedback",
                        }:
                            (visible / record.name).write_bytes(record.read_bytes())
                    (feedback / "consumed-meta-feedback.json").write_text(
                        json.dumps(self._meta_feedback, ensure_ascii=False)
                    )
                    explanation = await self.worker.edit(
                        directory,
                        "[Recursive-CA-RSI: improve-meta]\nYou are editing YOUR OWN "
                        "Meta Agent harness, which proposes and diagnoses Task edits. "
                        "Improve reusable diagnosis, repair, planning or tool-use "
                        "strategies using the supplied feedback. It is not a Task "
                        "answering harness. Preserve editing capabilities and interfaces. "
                        f"Change at most {self.config.max_meta_edit_units} files. "
                        f"Feedback: {feedback.name}/. Treat it as read-only. "
                        "No benchmark calls; do not access other experiment records, "
                        "future challenges, unused audit/test data or graders. Your revision "
                        "will be compared with your original version on fresh downstream "
                        "Task-improvement challenges. Return the design rationale."
                        + REASON_INSTRUCTION,
                    )
                discard_runtime_files(directory)
                revised = self._commit(original, directory)
            self._record_change(
                original,
                revised,
                explanation,
                step=step,
                phase="meta_self_edit",
                subject="Meta",
            )
            paths = original.changed_paths(revised)
            problem = self._variant_problem(revised)
            if not paths or len(paths) > self.config.max_meta_edit_units or problem:
                self._record(
                    "meta_decision",
                    {
                        "step": step,
                        "accepted": False,
                        "worker_before_id": original.id,
                        "worker_candidate_id": revised.id,
                        "problem": problem or "unchanged or over-budget Meta proposal",
                    },
                )
                return
            train = self._probe_train[step]
            dev = self._probe_dev[step :: self.config.max_steps]
            # Both versions start from exactly the same incumbent, evidence,
            # editing prompt and configured budget. Trials never promote Task edits.
            training = await self._evaluate(incumbent, train, label="meta_probe_train")
            baseline = await self._evaluate(incumbent, dev, label="meta_probe_baseline")
            if not self._usable(training) or not self._usable(baseline):
                self._record(
                    "meta_decision",
                    {
                        "step": step,
                        "accepted": False,
                        "worker_before_id": original.id,
                        "worker_candidate_id": revised.id,
                        "problem": "probe training/baseline execution failed",
                    },
                )
                return
            trials = []
            for repeat in range(self.config.meta_repeats):
                pair = {}
                order = [("original", original), ("revised", revised)]
                if repeat % 2:
                    order.reverse()
                for role, worker_snapshot in order:
                    with self._worker_version(worker_snapshot):
                        pair[role] = await self._probe_patch(
                            incumbent,
                            training,
                            dev,
                            journal_context={
                                "step": step,
                                "repeat": repeat,
                                "worker_role": role,
                            },
                        )
                trials.append({"repeat": repeat, **pair})
            usable = all(
                pair[role]["usable"]
                for pair in trials
                for role in ("original", "revised")
            )
            gain = (
                sum(
                    pair["revised"]["score"] - pair["original"]["score"]
                    for pair in trials
                )
                / len(trials)
                if usable
                else None
            )
            regressions = (
                sum(
                    pair["revised"]["per_task"][i] < score
                    for pair in trials
                    for i, score in pair["original"]["per_task"].items()
                )
                if usable
                else None
            )
            # Also demand a positive improvement over doing nothing. Merely
            # producing a less harmful patch is not sufficient to self-promote.
            improves_task = (
                usable
                and sum(pair["revised"]["score"] for pair in trials) / len(trials)
                > baseline.score
            )
            task_regressions = (
                sum(
                    pair["revised"]["per_task"][i] < score
                    for pair in trials
                    for i, score in baseline.per_task.items()
                )
                if usable
                else None
            )
            accepted = bool(
                usable
                and gain > 0
                and improves_task
                and task_regressions <= self.config.max_task_regressions
                and regressions <= self.config.max_task_regressions
            )
            decision = {
                "step": step,
                "worker_before_id": original.id,
                "worker_candidate_id": revised.id,
                "changed_paths": paths,
                "accepted": accepted,
                "mean_paired_gain": gain,
                "regressions": regressions,
                "task_regressions": task_regressions,
                "task_baseline_score": baseline.score,
                "trials": trials,
                "scope": "fresh_meta_dev_repeated_paired_estimate_not_causal_proof",
            }
            self._record("meta_decision", decision)
            # Scores of consumed challenges may inform a later self-edit; task
            # contents/answers, final tests and future challenges remain withheld.
            self._meta_feedback.append(
                {key: value for key, value in decision.items() if key != "trials"}
            )
            if accepted:
                self.worker.update(revised)
        except (RuntimeError, ValueError, OSError) as error:
            self.worker.update(original)
            self._record(
                "meta_decision",
                {
                    "step": step,
                    "accepted": False,
                    "worker_before_id": original.id,
                    "problem": str(error),
                },
            )

    def _publish_task_feedback(self, step):
        """A completed task audit becomes learning evidence, never a future probe."""
        evaluations, audit = [], None
        for record in sorted((self._run_dir / "trajectories").glob("*.json")):
            label = record.stem.split("-", 1)[-1]
            if label == "audit_baseline":
                evaluations, audit = [], None
            if label in {"audit_baseline", "audit_candidate", "audit_subset"}:
                evaluations.append(json.loads(record.read_text()))
            elif label == "audit":
                audit = json.loads(record.read_text())
        feedback = {
            "step": step,
            "scope": "consumed_audit_adaptive_training_not_independent_validation",
            "audit": audit,
            "evaluations": evaluations,
        }
        self._task_feedback.append(feedback)
        self._consumed_tasks += self.benchmark.dev[step :: self.config.max_steps]
        self._record("task_feedback", feedback)

    async def _probe_patch(self, incumbent, training, dev, *, journal_context=None):
        try:
            with incumbent.check_out(
                working_dir=self._run_dir / "working"
            ) as directory:
                with TemporaryDirectory(
                    prefix="probe-evidence-", dir=directory
                ) as temporary:
                    evidence = Path(temporary) / "train.json"
                    evidence.write_text(
                        json.dumps(
                            [self._trajectory_evidence(t) for t in training],
                            ensure_ascii=False,
                        )
                    )
                    explanation = await self.worker.edit(
                        directory,
                        "[Recursive-CA-RSI: meta-probe]\nImprove the Task harness "
                        "using only the supplied training trajectory in "
                        f"{evidence.parent.name}/train.json. Edit at most "
                        f"{self.config.max_edit_units} files. Preserve the interface "
                        "and useful behavior. Do not hard-code answers/IDs or access "
                        "other records, evaluation data, graders or model connections. "
                        "Do not edit the supplied evidence. Return a brief rationale."
                        + REASON_INSTRUCTION,
                    )
                discard_runtime_files(directory)
                proposal = self._commit(incumbent, directory)
            self._record_change(
                incumbent,
                proposal,
                explanation,
                phase="shadow_task_probe",
                subject="ShadowTask",
                **({"step": None} if journal_context is None else journal_context),
            )
            problem = self._variant_problem(proposal)
            if len(incumbent.changed_paths(proposal)) > self.config.max_edit_units:
                problem = "probe proposal exceeded edit budget"
            result = (
                None
                if problem
                else await self._evaluate(proposal, dev, label="meta_probe_result")
            )
            return {
                "snapshot_id": proposal.id,
                "usable": result is not None and self._usable(result),
                "problem": problem,
                "score": result.score if result is not None else None,
                "per_task": dict(result.per_task) if result is not None else {},
            }
        except (RuntimeError, ValueError, OSError) as error:
            return {
                "usable": False,
                "problem": str(error),
                "score": None,
                "per_task": {},
            }


__all__ = ["RecursiveCARSI"]
