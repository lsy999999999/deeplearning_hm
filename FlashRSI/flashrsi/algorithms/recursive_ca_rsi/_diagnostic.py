"""Controller checks for bound observations and locally supported repairs."""

from __future__ import annotations

STRICT_INSTRUCTION = """
Strict rule diagnosis: an issue must include observations, a nonempty list of
{task_id, snapshot_id, output}. Copy the CURRENT candidate snapshot ID and exact
output from after; never describe a baseline failure as a current observation.
Each issue must also include intervention_indices: zero-based indices into the
top-level interventions list. Each referenced request must use a supplied case
for the same changed module and observed task, disable known rules, and predict
an exact output before execution. Include a nonempty falsified_if string in
each request. A prose verification is not an executable experiment.
Return issues: [] and interventions: [] when no supported current fault is
suspected. A latent risk or successful current output is not a demonstrated
failure. The controller will permit a rule repair only when a linked prediction
matches AND disabling improves that frozen case's benchmark grade. Findings
outside the supported pure-rule boundary remain unverified and are not repaired
through this protocol. No tool calls are needed for this diagnosis.
"""


def validate_observations(issues, evidence, interventions):
    """Check exact facts, not the truth of the model's causal explanation."""
    after = {row["task"]["task_id"]: row for row in evidence["after"]}
    cases = {case["id"]: case for case in evidence.get("rule_cases", [])}
    for issue in issues:
        observations = issue.get("observations")
        if not isinstance(observations, list) or not observations:
            raise ValueError("Strict diagnosis needs current candidate observations")
        observed_ids = set()
        for observation in observations:
            if not isinstance(observation, dict):
                raise ValueError("Invalid candidate observation")
            task_id = observation.get("task_id")
            if not isinstance(task_id, str):
                raise ValueError("Observation needs a supplied task ID")
            row = after.get(task_id)
            if (
                row is None
                or observation.get("snapshot_id") != evidence["candidate_snapshot_id"]
                or observation.get("output") != row["output"]
            ):
                raise ValueError(
                    "Observation differs from current candidate snapshot/output"
                )
            observed_ids.add(task_id)
        if observed_ids != set(issue["evidence_task_ids"]):
            raise ValueError(
                "Observations must cover exactly the issue's evidence tasks"
            )
        indices = issue.get("intervention_indices")
        if (
            not isinstance(indices, list)
            or not indices
            or any(
                type(i) is not int or not 0 <= i < len(interventions) for i in indices
            )
        ):
            raise ValueError("Rule issue needs linked executable intervention indices")
        for index in indices:
            request = interventions[index]
            case = cases[request["case_id"]]
            if (
                case["module_path"] != issue["path"]
                or case["task_id"] not in observed_ids
            ):
                raise ValueError(
                    "Intervention must match the observed task and changed module"
                )
            if (
                not isinstance(request.get("falsified_if"), str)
                or not request["falsified_if"].strip()
            ):
                raise ValueError(
                    "Intervention needs an explicit falsification condition"
                )


def repair_gate(diagnosis, evidence, executed):
    """Allow only issues with matching predictions and measured local harm.

    This is a local evidence gate, not a general semantic causality proof.
    Unverified issues stay in the original diagnosis record.
    """
    if not evidence.get("strict_rule_diagnosis"):
        return diagnosis["issues"], []
    checks = [
        (group["module_path"], check)
        for group in (executed or {}).get("groups", [])
        if group["status"] == "executed"
        for check in group["report"]["hypotheses"]
    ]
    supported, rejected = [], []
    for index, issue in enumerate(diagnosis["issues"]):
        requests = [
            diagnosis["interventions"][i] for i in issue["intervention_indices"]
        ]
        found = any(
            module == issue["path"]
            and check["request"] in requests
            and check.get("error_attribution_supported") is True
            for module, check in checks
        )
        if found:
            supported.append(issue)
        else:
            rejected.append(
                {
                    "issue_index": index,
                    "reason": "no linked prediction demonstrated local harm",
                }
            )
    return supported, rejected


def repair_summary(executed):
    """Keep full replay artifacts on disk; avoid repeating every transition."""
    if executed is None:
        return None
    return {
        **{key: value for key, value in executed.items() if key != "groups"},
        "groups": [
            {
                **{key: value for key, value in group.items() if key != "report"},
                **(
                    {
                        "report": {
                            key: value
                            for key, value in group["report"].items()
                            if key
                            in {
                                "effects",
                                "interactions",
                                "hypotheses",
                                "grading",
                                "primary_metric",
                                "scope",
                            }
                        }
                    }
                    if "report" in group
                    else {}
                ),
            }
            for group in executed["groups"]
        ],
    }
