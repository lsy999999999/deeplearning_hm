"""Prospective, fixed-Meta, shared-candidate study; never feeds test back to Meta."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

from filelock import FileLock
from flashrsi._records import _redact, _secrets, _write_record
from flashrsi._run_summary import _collect_usage, _finish_summary
from flashrsi.algorithms.recursive_ca_rsi import RecursiveCARSI, _mechanism
from flashrsi.core import Snapshot, Trajectory
from flashrsi.core.trajectory import Results
from run_experiment import ROOT, configured, settings
from run_fixed_meta_case import identities

from flashrsi import runner

ALLOWED = {"agent.yaml", "skills/main.md"}


def read(path):
    return json.loads(path.read_text())


def decode(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    value = json.loads(text)
    if not isinstance(value, dict):
        raise TypeError("Expected a JSON object")
    return value


def validate_files(files):
    if not isinstance(files, dict) or not 1 <= len(files) <= 3:
        raise ValueError("One to three complete changed files required")
    for name, content in files.items():
        if name not in ALLOWED and not (
            name.startswith("middleware/")
            and name.endswith(".py")
            and name.count("/") == 1
        ):
            raise ValueError("Only config, main skill and answer middleware may change")
        if not isinstance(content, str):
            raise TypeError("File content must be text")


def requests_checked(value, cases):
    known = {c["id"]: c for c in cases}
    requests, invalid = [], []
    raw = value.get("interventions", [])
    if not isinstance(raw, list):
        return [], [raw]
    for request in raw:
        case = known.get(request.get("case_id")) if isinstance(request, dict) else None
        valid = bool(case) and all(
            isinstance(request.get(k), str) and request[k].strip()
            for k in ("claim", "predicted_output", "falsifier")
        )
        if valid:
            valid = (
                request.get("observed_input") == case["answer"]
                and request.get("observed_output") == case["observed_output"]
                and isinstance(request.get("disable"), list)
                and bool(request["disable"])
                and all(r in case["rule_ids"] for r in request["disable"])
            )
        (requests if valid else invalid).append(request)
    return requests, invalid


def scores(results):
    return {
        "em": results.score,
        "f1": sum(t.metrics["f1"] for t in results) / len(results),
        "n": len(results),
        "per_task": dict(results.per_task),
        "usable": all(t.status == "finished" and not t.error_message for t in results),
    }


def regressions(after, before):
    return sum(after["per_task"][k] < v for k, v in before["per_task"].items())


def compact_interventions(evidence):
    """Keep all measured outputs; omit repeated context/event copies from Meta input."""
    groups = []
    for group in evidence["groups"]:
        report = group.get("report", {})
        groups.append(
            {
                "module_path": group["module_path"],
                "status": group["status"],
                "problem": group.get("problem"),
                "hypotheses": report.get("hypotheses", []),
                "effects": report.get("effects", []),
                "interactions": report.get("interactions", []),
                "variants": [
                    {
                        "enabled": v["enabled"],
                        "valid": v["valid"],
                        "score": v["score"],
                        "cases": [
                            {
                                k: row.get(k)
                                for k in ("id", "output", "correct", "valid", "error")
                            }
                            for row in v["cases"]
                        ],
                    }
                    for v in report.get("variants", [])
                ],
            }
        )
    return {"status": evidence["status"], "scope": evidence["scope"], "groups": groups}


def choose(arms, seed_id):
    a, b = arms["A"], arms.get("B")
    base = "B" if b and b["usable"] and a["usable"] and b["em"] > a["em"] else "A"
    eligible = [
        k
        for k in ("B", "D")
        if k in arms
        and arms[k]["usable"]
        and a["usable"]
        and arms[k]["em"] > a["em"]
        and regressions(arms[k], a) == 0
    ]
    enhanced = (
        max(eligible, key=lambda k: (arms[k]["em"], k == "B")) if eligible else "A"
    )
    gate_only = "B" if base == "B" and regressions(b, a) == 0 else "A"
    return {
        "base": base,
        "gate_only": gate_only,
        "enhanced": enhanced,
        "seed_id": seed_id,
    }


class Study:
    def __init__(self, algorithm, config, directory):
        self.a, self.config, self.root = algorithm, config, directory
        algorithm._run_dir = directory
        algorithm._sequence = max(
            [
                int(p.name.split("-")[0])
                for p in (directory / "trajectories").glob("*.json")
            ]
            or [0]
        )

    def save(self, name, value):
        path = self.root / name
        if path.exists():
            clean = json.loads(json.dumps(_redact(value, _secrets(self.config))))
            if read(path) == clean:
                return
            if name == "plan.json" and not list(
                (self.root / "trajectories").glob("*.json")
            ):
                # Preserve preparation revisions before the first paid call.
                digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
                path.rename(self.root / f"plan-precall-{digest}.json")
            else:
                raise ValueError(
                    f"Refusing to overwrite different saved evidence: {name}"
                )
        _write_record(path, value, self.config)

    async def ask(self, name, prompt):
        path = self.root / (name + ".json")
        if path.exists():
            return read(path)
        start = time.monotonic()
        try:
            text = await self.a.worker.ask(prompt)
            result = {"status": "finished", "text": text}
            try:
                result["value"] = decode(text)
            except (ValueError, TypeError) as error:
                result.update(status="invalid_json", problem=str(error))
        except (RuntimeError, ValueError) as error:
            result = {"status": "failed", "problem": str(error)}
        result["elapsed_seconds"] = time.monotonic() - start
        self.save(name + ".json", result)
        return result

    async def evaluate(self, snapshot, tasks, phase):
        # Same version on the same tasks is reused once, including failed outcomes.
        key = hashlib.sha256(
            (snapshot.id + ":" + ",".join(t.task_id for t in tasks)).encode()
        ).hexdigest()
        path = self.root / "cache" / f"{key}.json"
        if path.exists():
            rows = read(path)["trajectories"]
            return Results(
                (
                    Trajectory(task=t, **{k: v for k, v in row.items() if k != "task"})
                    for t, row in zip(tasks, rows, strict=True)
                ),
                metric="exact_match",
            )
        result = await self.a._evaluate(snapshot, tasks, label=phase)
        from flashrsi._records import _trajectory_record

        self.save(
            "cache/" + key + ".json",
            {
                "snapshot_id": snapshot.id,
                "trajectories": [_trajectory_record(t) for t in result],
            },
        )
        return result


async def prepare(config, root):
    if (root / "tasks.json").exists():
        from flashrsi.benchmarks import HotpotQA
        from flashrsi.core import Task

        frozen = read(root / "tasks.json")
        splits = {role: tuple(Task(**t) for t in rows) for role, rows in frozen.items()}

        def load_frozen(benchmark):
            benchmark._splits = splits
            benchmark._tasks_by_id = {
                t.task_id: t for rows in splits.values() for t in rows
            }
            benchmark.metadata = {
                **dict(benchmark.metadata),
                **dict(config.benchmark.dataset),
                "mode": benchmark.mode,
                "scoring": benchmark.scoring,
            }
            return splits

        with patch.object(HotpotQA, "load", load_frozen):
            algorithm = await runner._prepare(config)
        source = algorithm.benchmark.source
    else:
        algorithm = await runner._prepare(config)
        source = algorithm.benchmark.source
        ids, hashes = set(), set()
        for path in list((ROOT / "runs").glob("**/trajectories/*.json")) + list(
            (ROOT / "results").glob("*.json")
        ):
            if path.is_relative_to(root):
                continue
            identities(read(path), ids, hashes)
        pool = sorted(
            (t for group in source.load().values() for t in group),
            key=lambda t: hashlib.sha256(
                f"{config.benchmark.seed}:{t.task_id}".encode()
            ).digest(),
        )
        eligible = [
            t
            for t in pool
            if t.task_id not in ids
            and hashlib.sha256(t.question.encode()).hexdigest() not in hashes
            and t.metadata.get("question_checksum") not in hashes
        ]
        if len(eligible) < 88:
            raise ValueError("Fewer than 88 unexposed tasks in pool")
        splits = {
            "train": tuple(eligible[:8]),
            "dev": tuple(eligible[8:48]),
            "test": tuple(eligible[48:88]),
        }
        frozen = {
            role: [
                {
                    "task_id": t.task_id,
                    "question": t.question,
                    "ground_truth": t.ground_truth,
                    "env": t.env,
                    "metadata": dict(t.metadata),
                }
                for t in rows
            ]
            for role, rows in splits.items()
        }
        _write_record(root / "tasks.json", frozen, config)
        _write_record(
            root / "exposure.json",
            {
                "historical_ids": len(ids),
                "excluded": len(pool) - len(eligible),
                "overlap": 0,
            },
            config,
        )
    source._splits = splits
    source._tasks_by_id = {t.task_id: t for rows in splits.values() for t in rows}
    algorithm = RecursiveCARSI(
        config.algorithm,
        target=algorithm.target,
        worker=algorithm.worker,
        benchmark=source,
    )
    return algorithm


async def run(args, values):
    config = configured(args.config, values)
    if config.algorithm.meta_self_update or not config.algorithm.rule_verification:
        raise ValueError("Fixed Meta and rule verification required")
    root = args.output.resolve()
    for name in ("trajectories", "snapshots", "working", "cache"):
        (root / name).mkdir(parents=True, exist_ok=True)
    if (root / "result.json").exists():
        raise ValueError("Study already finished")
    algorithm = await prepare(config, root)
    study = Study(algorithm, config, root)
    seed = algorithm.target.snapshot
    algorithm._save_snapshot(seed)
    plan = {
        "protocol": "shared-natural-candidates-v1",
        "target_candidates": 10,
        "max_proposal_attempts": 20,
        "meta_self_update": False,
        "candidate_origin": "MiniAgent model-generated JSON file patches; controller only applies and validates",
        "source_commit": subprocess.check_output(  # noqa: ASYNC221 - local provenance
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "initial_snapshot": seed.id,
        "worker_snapshot": algorithm.worker.snapshot.id,
        "split_counts": {"train": 8, "dev": 40, "test": 40},
        "dev_per_candidate": 4,
        "test_per_candidate": 4,
        "primary_metric": "exact_match",
        "test_scope": "evaluate all frozen A/B/C/D versions after all selections; never feed test to Meta",
        "base_policy": "accept complete B iff usable and dev EM strictly exceeds A",
        "gate_only_policy": "accept B iff base accepts and dev has zero paired regressions; no repair",
        "enhanced_policy": "select B or verified repair D iff dev EM strictly exceeds A with zero paired regressions; prefer B on ties",
        "removal_policy": "C removes first controller-supported harmful rule request; diagnostic comparator only",
        "scope": "bounded pure answer rules; shared fixed-seed proposals, not two independent adaptive evolution trajectories",
        "cost": "shared evidence counted once; added diagnosis/intervention/repair costs separately reported",
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
    }
    if (root / "plan.json").exists():
        if read(root / "plan.json")["initial_snapshot"] != seed.id:
            raise ValueError("Initial code changed; cannot resume")
        if list((root / "trajectories").glob("*.json")):
            old = read(root / "plan.json")
            if any(old.get(k) != plan[k] for k in ("driver_sha256", "config_sha256")):
                raise ValueError("Frozen driver/config changed after model calls")
        else:
            study.save("plan.json", plan)
    else:
        study.save("plan.json", plan)
    from flashrsi._records import _redact_config

    study.save("config.json", _redact_config(config))
    if args.prepare_only:
        print("Prepared and frozen without model calls", flush=True)
        return
    start = time.monotonic()
    learning = algorithm.benchmark.train
    initial = await study.evaluate(seed, learning, "study_train_baseline")
    training = [
        {
            "task_id": t.task.task_id,
            "question": t.task.question,
            "expected": t.task.ground_truth,
            "output": t.output,
            "metrics": dict(t.metrics),
        }
        for t in initial
    ]
    editable = {p: e.content.decode() for p, e in seed.files.items() if p in ALLOWED}
    history = []
    cases = []
    api_contract = (
        "Use MiniAgent's existing API: from runtime.rule_audit import Rule. "
        "Declare at most four ordered AUDIT_RULES=[Rule('stable_id', pure_fn)] in a middleware module. "
        "Each pure_fn(answer,evidence) returns a string, no I/O/model/randomness. "
        "async def before_final(answer,ctx): result=ctx.apply_answer_rules(answer, {'context':ctx.question}, AUDIT_RULES); "
        "return {'action':'replace','answer':result} if result!=answer else {'action':'accept'}. "
        "Register the module name in agent.yaml middlewares. Keep other config fields. "
        "Allowed changes: agent.yaml, one middleware/*.py, skills/main.md, at most three files. "
        "Preserve useful answer content. Do not hard-code training answers/entities/task IDs or create grader lookups. "
        "No tools: submit full source files in JSON, the controller applies them. "
    )
    for attempt in range(20):
        if len(cases) >= 10:
            break
        name = f"attempt-{attempt:02d}"
        answer = await study.ask(
            name + "-proposal",
            "Propose ONE reusable improvement to this MiniAgent from the training evidence. "
            "Focus on answer processing; do not intentionally inject errors. Each proposal starts from the initial agent. "
            + api_contract
            + 'Return ONLY JSON {"reason":str,"files":{path:complete_text}}.\n'
            + json.dumps(
                {
                    "initial_files": editable,
                    "training": training,
                    "prior_proposals": history,
                },
                ensure_ascii=False,
            ),
        )
        if answer["status"] != "finished":
            history.append({"attempt": attempt, "status": answer["status"]})
            continue
        try:
            value = answer["value"]
            validate_files(value.get("files"))
            candidate = algorithm._candidate(seed, value["files"])
            if candidate.id == seed.id:
                raise ValueError("Proposal did not change files")
        except (ValueError, TypeError) as error:
            study.save(name + "-invalid.json", {"problem": str(error)})
            history.append(
                {"attempt": attempt, "status": "invalid", "problem": str(error)}
            )
            continue
        index = len(cases)
        state_path = root / f"case-{index:02d}.json"
        if state_path.exists():
            case = read(state_path)
            cases.append(case)
            history.append(case["history"])
            continue
        case = {
            "index": index,
            "attempt": attempt,
            "A": seed.id,
            "B": candidate.id,
            "C": None,
            "D": None,
            "proposal_reason": value.get("reason"),
            "changed_paths": seed.changed_paths(candidate),
        }
        print(f"Case {index + 1}/10: candidate {candidate.id[:10]}", flush=True)
        invalid = algorithm._variant_problem(candidate)
        if invalid:
            case["candidate_problem"] = invalid
            current = None
            boundary = []
            skipped = []
        else:
            current = await study.evaluate(candidate, learning, "study_train_candidate")
            boundary, skipped = _mechanism.collect_cases(candidate, current, 16)
            case["train"] = scores(current)
        case["boundary_count"] = len(boundary)
        case["boundary_skips"] = skipped
        diagnosis = None
        rule_evidence = None
        supported = []
        if current is not None:
            evidence = {
                "before": training,
                "after": [
                    {
                        "task_id": t.task.task_id,
                        "output": t.output,
                        "metrics": dict(t.metrics),
                        "status": t.status,
                    }
                    for t in current
                ],
                "changed_files": value["files"],
                "rule_cases": _mechanism.visible_cases(boundary),
            }
            diagnosis = await study.ask(
                name + "-diagnosis",
                "Diagnose ONLY the candidate after outputs. Do not mistake baseline failures for candidate failures. "
                "Normal, useful and inactive rules are valid outcomes; do not invent faults. "
                "For a suspected harmful rule, supply an executable prediction BEFORE seeing intervention results. "
                "Copy observed_input and observed_output exactly from the referenced rule case. "
                'Return ONLY JSON {"assessment":str,"interventions":[{"case_id":str,"disable":[rule_id],'
                '"observed_input":str,"observed_output":str,"predicted_output":str,"claim":str,"falsifier":str}]}. '
                "An empty list is valid; unsupported retrieval/retry/planning claims must say insufficient evidence. No tools.\n"
                + json.dumps(evidence, ensure_ascii=False),
            )
            requests, bad = requests_checked(diagnosis.get("value", {}), boundary)
            case["invalid_requests"] = bad
            case["valid_request_count"] = len(requests)
            # All available learning boundaries are audited, including unchanged and useful cases.
            rule_evidence = await _mechanism.audit(
                algorithm, candidate, boundary, requests, require_reproduction=True
            )
            study.save(name + "-interventions.json", rule_evidence)
            for group in rule_evidence["groups"]:
                if group["status"] == "executed":
                    supported += [
                        h["request"]
                        for h in group["report"]["hypotheses"]
                        if h.get("error_attribution_supported")
                    ]
            case["supported_harmful_requests"] = supported
            reflection = await study.ask(
                name + "-reflection",
                "Update your diagnosis using the controller experiments. A prediction matching output is not itself evidence of harm. "
                'Return ONLY JSON {"conclusion":"supported|refuted|insufficient_evidence|no_harm_found",'
                '"reason":str,"files":{path:complete_text}}. Only propose a minimal repair when controller-supported harmful requests exist; '
                "otherwise files MUST be empty. Preserve all useful changes. No tools. "
                + api_contract
                + "\n"
                + json.dumps(
                    {
                        "candidate_files": value["files"],
                        "diagnosis": diagnosis.get("value"),
                        "controller": compact_interventions(rule_evidence),
                        "supported": supported,
                    },
                    ensure_ascii=False,
                ),
            )
            case["reflection_status"] = reflection["status"]
            case["reflection"] = reflection.get("value", {})
            if supported:
                first = supported[0]
                module = next(
                    c["module_path"] for c in boundary if c["id"] == first["case_id"]
                )
                # Delete only the nominated rule(s); all other candidate files are preserved.
                source = candidate.files[module].content.decode()
                removed = algorithm._candidate(
                    candidate,
                    {
                        module: source
                        + "\nAUDIT_RULES = [r for r in AUDIT_RULES if r.rule_id not in "
                        + repr(first["disable"])
                        + "]\n"
                    },
                )
                case["C"] = removed.id
                files = reflection.get("value", {}).get("files", {})
                if files:
                    try:
                        validate_files(files)
                        repaired = algorithm._candidate(candidate, files)
                        if not candidate.changed_paths(repaired):
                            raise ValueError("Unchanged repair")
                        if algorithm._variant_problem(repaired):
                            raise ValueError("Invalid repair runtime configuration")
                        if len(seed.changed_paths(repaired)) > 3:
                            raise ValueError("Repair exceeds total file budget")
                        check = await _mechanism.audit(
                            algorithm,
                            repaired,
                            boundary,
                            [],
                            require_reproduction=False,
                        )
                        comparison = _mechanism.comparison(rule_evidence, check)
                        study.save(
                            name + "-repair-replay.json",
                            {"evidence": check, "comparison": comparison},
                        )
                        if (
                            comparison["valid"]
                            and not comparison["regressions"]
                            and comparison["after_score"] > comparison["before_score"]
                        ):
                            case["D"] = repaired.id
                        else:
                            case["repair_problem"] = (
                                "Repair did not improve frozen boundaries without regression"
                            )
                    except (ValueError, TypeError) as error:
                        case["repair_problem"] = str(error)
        dev = algorithm.benchmark.dev[index * 4 : index * 4 + 4]
        arms = {}
        for key in ("A", "B", "C", "D"):
            if case.get(key) and not (key == "B" and invalid):
                snapshot = Snapshot.load(root / "snapshots" / f"{case[key]}.json")
                arms[key] = scores(
                    await study.evaluate(snapshot, dev, "study_dev_" + key)
                )
        case["dev"] = arms
        case["selection"] = choose(arms, seed.id)
        case["history"] = {
            "attempt": attempt,
            "candidate": candidate.id,
            "reason": value.get("reason"),
            "train": case.get("train"),
            "diagnosis": diagnosis.get("value", {}).get("assessment")
            if diagnosis
            else "unsupported",
        }
        study.save(f"case-{index:02d}.json", case)
        cases.append(case)
        history.append(case["history"])
        print(f"Case {index + 1}: selections {case['selection']}", flush=True)
    study.save(
        "selection-freeze.json",
        {
            "cases": [
                {
                    "index": c["index"],
                    "versions": {k: c.get(k) for k in ("A", "B", "C", "D")},
                    "selection": c["selection"],
                }
                for c in cases
            ],
            "test_started_after_all_selections": True,
        },
    )
    # No more model proposals, diagnoses or repairs after this point.
    for case in cases:
        index = case["index"]
        tasks = algorithm.benchmark.test[index * 4 : index * 4 + 4]
        case["test"] = {}
        for key in ("A", "B", "C", "D"):
            if case.get(key) and not (key == "B" and case.get("candidate_problem")):
                snapshot = Snapshot.load(root / "snapshots" / f"{case[key]}.json")
                case["test"][key] = scores(
                    await study.evaluate(snapshot, tasks, "study_test_" + key)
                )
        study.save(f"case-{index:02d}-test.json", case)
        print(f"Final test case {index + 1}/{len(cases)} finished", flush=True)
    study.save(
        "result.json",
        {
            "status": "finished",
            "cases": cases,
            "actual_candidates": len(cases),
            "goal_met_candidate_count": len(cases) >= 10,
            "elapsed_active_session_seconds": time.monotonic() - start,
            "usage": _collect_usage(root),
            "scope": plan["scope"],
        },
    )
    _finish_summary(root)
    print("Finished " + str(root), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    values = settings(ROOT / ".env")
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(args.output / ".lock"), timeout=0):
            asyncio.run(run(args, values))
    except Exception as error:  # noqa: BLE001 - redact CLI failures
        from flashrsi._records import _redact

        secrets = [
            v
            for k, v in values.items()
            if v and any(x in k.lower() for x in ("key", "token", "secret", "password"))
        ]
        raise SystemExit(_redact(str(error), secrets))


if __name__ == "__main__":
    main()
