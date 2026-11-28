"""Bounded interventions for declared, pure answer transformations.

This is a contract, not a security sandbox. A rule must not call a model, use
randomness, mutate external state or perform I/O. Repeated replay catches some
violations; it cannot prove purity. Runtime gates that request another turn are
outside this protocol and need paired complete rollouts.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import itertools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Rule:
    rule_id: str
    transform: Callable[[str, dict], str]
    depends_on: tuple[str, ...] = ()

    def manifest(self):
        path = inspect.getsourcefile(self.transform)
        try:
            line = inspect.getsourcelines(self.transform)[1]
            source_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except (OSError, TypeError):
            raise ValueError(f"Rule {self.rule_id} needs a saved source file") from None
        return {
            "rule_id": self.rule_id,
            "path": path,
            "line": line,
            "source_sha256": source_hash,
            "depends_on": list(self.depends_on),
        }


def _validate(rules):
    ids = [rule.rule_id for rule in rules]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Rules need nonempty unique IDs")
    previous = set()
    for rule in rules:
        if not rule.rule_id or set(rule.depends_on) - previous:
            raise ValueError("Dependencies must reference earlier rules")
        previous.add(rule.rule_id)
    return ids


def run_rules(answer, evidence, rules, *, enabled=None, emit=None, provenance=None):
    """Use this same function in before_final and offline replay.

    Each emitted event includes full boundary input/output and the rule source
    hash. Caller provenance should include task_id and harness snapshot_id.
    An exception propagates: a partial pipeline is never a valid intervention.
    """
    ids = _validate(rules)
    enabled = set(ids if enabled is None else enabled)
    if enabled - set(ids):
        raise ValueError("Intervention references an unknown rule")
    for rule in rules:
        if rule.rule_id in enabled and set(rule.depends_on) - enabled:
            raise ValueError("Intervention omits a required dependency")
    if not isinstance(answer, str) or not isinstance(evidence, dict):
        raise ValueError("Expected a string answer and JSON evidence object")
    # JSON round trip freezes an independently owned, serializable input.
    frozen = json.loads(json.dumps(evidence, ensure_ascii=False))
    events = []
    for rule in rules:
        before = answer
        active = rule.rule_id in enabled
        if active:
            answer = rule.transform(answer, copy.deepcopy(frozen))
            if not isinstance(answer, str):
                raise ValueError(f"Rule {rule.rule_id} returned a non-string")
        event = {
            "rule": rule.manifest(),
            "enabled": active,
            "action": "disabled"
            if not active
            else "replace"
            if before != answer
            else "no_op",
            "before": before,
            "after": answer,
            "evidence": frozen,
            "provenance": dict(provenance or {}),
            "input_sha256": digest({"answer": before, "evidence": frozen}),
        }
        events.append(event)
        if emit:
            emit("rule_transition", **copy.deepcopy(event))
    return {"output": answer, "events": events}


def audit_rules(rules, cases, *, hypotheses=(), provenance=None):
    """Exhaust all subsets (at most four rules), replay each twice.

    Cases have id, answer, evidence, expected (list of accepted exact strings).
    Labels are evaluator inputs, never passed into a rule. Invalid/unstable
    variants have no grade and cannot support a semantic attribution.
    """
    ids = _validate(rules)
    if len(ids) > 4:
        raise ValueError("Bounded audit supports at most four rules")
    case_ids = [case["id"] for case in cases]
    if not case_ids or len(case_ids) != len(set(case_ids)):
        raise ValueError("Cases need nonempty unique IDs")
    for case in cases:
        expected = case.get("expected")
        if (
            not isinstance(expected, list)
            or not expected
            or any(not isinstance(x, str) for x in expected)
        ):
            raise ValueError("Case expected must be a nonempty string list")
    variants = []
    for size in range(len(ids) + 1):
        for subset in itertools.combinations(ids, size):
            rows = []
            for case in cases:
                try:
                    args = (case["answer"], case.get("evidence", {}), rules)
                    opts = {
                        "enabled": subset,
                        "provenance": {**(provenance or {}), "case_id": case["id"]},
                    }
                    first = run_rules(*args, **opts)
                    second = run_rules(*args, **opts)
                    if first != second:
                        raise ValueError(
                            "Repeated replay differed; purity is unverified"
                        )
                    rows.append(
                        {
                            "id": case["id"],
                            "valid": True,
                            **first,
                            "correct": first["output"] in case["expected"],
                        }
                    )
                except Exception as error:
                    rows.append(
                        {
                            "id": case["id"],
                            "valid": False,
                            "problem": f"{type(error).__name__}: {error}",
                        }
                    )
            valid = all(row["valid"] for row in rows)
            variant = {
                "enabled": list(subset),
                "valid": valid,
                "cases": rows,
                "score": sum(row["correct"] for row in rows) / len(rows)
                if valid
                else None,
            }
            variants.append(variant)
    return {
        "protocol": "pure-answer-rule-interventions-v1",
        "manifest": [r.manifest() for r in rules],
        "inputs_sha256": digest(cases),
        "provenance": dict(provenance or {}),
        "scope": "fixed_input_output_effect_not_general_reasoning_causality",
        "variants": variants,
        **interpret_variants(variants, ids, hypotheses),
    }


def interpret_variants(variants, ids, hypotheses=()):
    """Interpret already-executed, already-graded binary task outcomes.

    The outer evaluator may regrade outputs with its own benchmark rather than
    exact string comparison, without re-executing any candidate code.
    """
    by_subset = {frozenset(v["enabled"]): v for v in variants}
    case_ids = [row["id"] for row in variants[0]["cases"]]
    full = by_subset[frozenset(ids)]
    identity = by_subset[frozenset()]
    effects = []
    for rule_id in ids:
        removed = by_subset[frozenset(set(ids) - {rule_id})]
        alone = by_subset[frozenset({rule_id})]
        valid = full["valid"] and removed["valid"]
        effects.append(
            {
                "rule_id": rule_id,
                "status": "valid" if valid else "insufficient_evidence",
                "removal_gain": removed["score"] - full["score"] if valid else None,
                "alone_gain": alone["score"] - identity["score"]
                if alone["valid"] and identity["valid"]
                else None,
                "rescued_ids": [
                    a["id"]
                    for a, b in zip(removed["cases"], full["cases"])
                    if a.get("valid")
                    and b.get("valid")
                    and a["correct"]
                    and not b["correct"]
                ],
                "regressed_ids": [
                    a["id"]
                    for a, b in zip(removed["cases"], full["cases"])
                    if a.get("valid")
                    and b.get("valid")
                    and not a["correct"]
                    and b["correct"]
                ],
            }
        )
    interactions = []
    for a, b in itertools.combinations(ids, 2):
        others = set(ids) - {a, b}
        cells = [
            by_subset[frozenset(others | enabled)]
            for enabled in (set(), {a}, {b}, {a, b})
        ]
        interactions.append(
            {
                "rules": [a, b],
                "others_enabled": sorted(others),
                "difference_in_differences": cells[3]["score"]
                - cells[1]["score"]
                - cells[2]["score"]
                + cells[0]["score"]
                if all(c["valid"] for c in cells)
                else None,
            }
        )
    checks = []
    for hypothesis in hypotheses:
        # Check the executable prediction separately from its effect on quality.
        # A correct prediction may demonstrate that a rule is USEFUL, not faulty.
        request = hypothesis if isinstance(hypothesis, dict) else {}
        cid = request.get("case_id")
        disabled = request.get("disable")
        prediction = request.get("predicted_output")
        valid_request = (
            cid in case_ids
            and isinstance(disabled, list)
            and bool(disabled)
            and all(isinstance(x, str) and x in ids for x in disabled)
            and isinstance(prediction, str)
            and isinstance(request.get("claim"), str)
        )
        check = {"request": hypothesis, "status": "insufficient_evidence"}
        if valid_request:
            changed = by_subset[frozenset(set(ids) - set(disabled))]
            row = changed["cases"][case_ids.index(cid)]
            old = full["cases"][case_ids.index(cid)]
            if row["valid"] and old["valid"]:
                matches = row["output"] == prediction
                gain = int(row["correct"]) - int(old["correct"])
                check.update(
                    status="prediction_supported_on_frozen_input"
                    if matches
                    else "prediction_refuted_on_frozen_input",
                    observed_output=row["output"],
                    original_output=old["output"],
                    removal_quality_effect=gain,
                    error_attribution_supported=matches and gain > 0,
                    interpretation="disabling improved quality"
                    if gain > 0
                    else "disabling hurt quality"
                    if gain < 0
                    else "no measured quality change",
                )
        else:
            check["problem"] = "Malformed request or unknown case/rule ID"
        checks.append(check)
    return {"effects": effects, "interactions": interactions, "hypotheses": checks}
