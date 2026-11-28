#!/usr/bin/env python3
"""Bounded fixed-Meta pilot: proposal, interventions, repair check, reflection.

MATPOOL_API_KEY comes only from environment; max two requests, no retries.
--offline replays saved repair without API calls. Never overwrites a run.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from flashrsi.agents.miniagent.runtime.rule_audit import Rule, audit_rules

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "research/rule_audit_pilot"


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def load(path):
    spec = importlib.util.spec_from_file_location("pilot_" + path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    mod.re = re
    spec.loader.exec_module(mod)
    return mod


def decode(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


def validate_source(source):
    tree = ast.parse(source)
    if (
        len(tree.body) != 1
        or not isinstance(tree.body[0], ast.FunctionDef)
        or tree.body[0].name != "comma_gloss"
    ):
        raise ValueError("Repair must contain only def comma_gloss(answer, evidence)")
    fn = tree.body[0]
    if (
        fn.decorator_list
        or fn.returns
        or fn.args.defaults
        or fn.args.kw_defaults
        or any(a.annotation for a in fn.args.args)
    ):
        raise ValueError("No decorators, annotations or executable defaults")
    if (
        [a.arg for a in fn.args.args] != ["answer", "evidence"]
        or fn.args.vararg
        or fn.args.kwarg
        or fn.args.posonlyargs
        or fn.args.kwonlyargs
    ):
        raise ValueError("Repair must use the declared two-argument signature")
    for node in ast.walk(tree):
        if isinstance(
            node,
            (
                ast.Import,
                ast.ImportFrom,
                ast.Global,
                ast.Nonlocal,
                ast.ClassDef,
                ast.AsyncFunctionDef,
            ),
        ):
            raise TypeError("Repair must be self-contained and synchronous")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError("No reflection")
        if isinstance(node, ast.Name) and node.id in {
            "open",
            "exec",
            "eval",
            "compile",
            "__import__",
            "input",
            "globals",
            "locals",
            "getattr",
            "setattr",
            "delattr",
        }:
            raise ValueError("No dynamic execution or I/O")
    # Contract plus a subprocess deadline; not an adversarial security sandbox.


def call_model(output, name, messages):
    from openai import OpenAI

    request = {
        "model": "DeepSeek-V4.1-Flash",
        "messages": messages,
        "temperature": 0,
        "max_tokens": 2600,
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    write(output / (name + "-request.json"), request)
    start = time.monotonic()
    client = OpenAI(
        api_key=os.environ["MATPOOL_API_KEY"],
        base_url="https://token.matpool.com/v1",
        timeout=90,
        max_retries=0,
    )
    try:
        response = client.chat.completions.create(**request)
    except Exception as error:  # noqa: BLE001 - redact transport exceptions
        write(
            output / (name + "-error.json"),
            {"type": type(error).__name__, "seconds": time.monotonic() - start},
        )
        raise RuntimeError("Model request failed; see redacted error record") from None
    write(output / (name + "-response.json"), response.model_dump(mode="json"))
    write(
        output / (name + "-usage.json"),
        {
            "seconds": time.monotonic() - start,
            "usage": response.usage.model_dump(mode="json") if response.usage else None,
            "model_returned": response.model,
            "finish_reason": response.choices[0].finish_reason,
        },
    )
    return response.choices[0].message.content or ""


def evaluate(output, compare_existing=False):
    cases, proposal = read(DATA / "cases.json"), read(output / "proposal.json")
    baseline, repaired = load(DATA / "baseline.py"), load(output / "repair.py")
    before = [
        Rule("surface", baseline.trim_surface),
        Rule("comma_gloss", baseline.comma_gloss),
    ]
    after = [
        Rule("surface", baseline.trim_surface),
        Rule("comma_gloss", repaired.comma_gloss),
    ]
    report = {}
    for split, rows in cases.items():
        original = audit_rules(
            before,
            rows,
            hypotheses=proposal.get("hypotheses", ()) if split == "train" else (),
            provenance={"split": split},
        )
        repair = audit_rules(
            after, rows, provenance={"split": split, "source": "model-repair"}
        )
        full, final = original["variants"][-1], repair["variants"][-1]
        removed = next(v for v in original["variants"] if v["enabled"] == ["surface"])
        valid = full["valid"] and final["valid"]
        regressions = [
            a["id"]
            for a, b in zip(full["cases"], final["cases"])
            if a.get("correct") and b.get("valid") and not b["correct"]
        ]
        report[split] = {
            "before": original,
            "repair": repair,
            "summary": {
                "n": len(rows),
                "full_score": full["score"],
                "removal_score": removed["score"],
                "repair_score": final["score"],
                "regressions": regressions,
                "passes_no_regression_gain_check": valid
                and final["score"] > full["score"]
                and not regressions,
            },
        }
    report["decision"] = {
        "accepted_controlled_pilot": report["train"]["summary"][
            "passes_no_regression_gain_check"
        ]
        and report["heldout"]["summary"]["passes_no_regression_gain_check"],
        "promoted_to_live_task_harness": False,
        "scope": "controlled mechanism pilot; historical answers are boundary proxies",
    }
    if compare_existing:
        if read(output / "evidence.json") != report:
            raise RuntimeError(
                "Replay differs from frozen evidence; prior report preserved"
            )
    else:
        write(output / "evidence.json", report)
    print(
        json.dumps(
            {k: v["summary"] for k, v in report.items() if k != "decision"},
            ensure_ascii=False,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument(
        "--recover-empty-proposal",
        action="store_true",
        help="Use remaining second call after a length-limited empty response; no reflection",
    )
    parser.add_argument("--evaluate-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--check-existing", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    output = args.output.resolve()
    if args.evaluate_child:
        evaluate(output, compare_existing=args.check_existing)
        return
    if args.offline:
        subprocess.run(
            [
                sys.executable,
                __file__,
                "--output",
                str(output),
                "--evaluate-child",
                "--check-existing",
            ],
            check=True,
            timeout=25,
        )
        return
    if args.recover_empty_proposal:
        previous = read(output / "proposal-response.json")
        if (
            previous["choices"][0]["message"].get("content")
            or previous["choices"][0]["finish_reason"] != "length"
            or (output / "proposal-recovery-request.json").exists()
        ):
            raise ValueError(
                "Recovery only permits one call after an empty length-limited response"
            )
    else:
        output.mkdir(parents=True, exist_ok=False)
    frozen = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in DATA.iterdir()
        if p.is_file()
    }
    if args.recover_empty_proposal:
        if read(output / "input-hashes.json") != frozen:
            raise RuntimeError("Frozen inputs changed before recovery")
    else:
        write(output / "input-hashes.json", frozen)
    cases, baseline = read(DATA / "cases.json"), load(DATA / "baseline.py")
    rules = [
        Rule("surface", baseline.trim_surface),
        Rule("comma_gloss", baseline.comma_gloss),
    ]
    full = audit_rules(rules, cases["train"])["variants"][-1]
    prompt = {
        "task": 'Diagnose and repair comma_gloss. Rules execute surface then comma_gloss. Preserve useful appositive removal; protect meaningful commas in entity names. Do not hardcode task IDs or named entities. If uncertain preserve input. Before seeing disable-rule executions, predict their concrete outputs. Return ONLY JSON: {"hypotheses":[{"case_id":"training ID","disable":["rule_id"],"predicted_output":"answer","claim":"specific mechanism"}],"repair_source":"def comma_gloss(answer, evidence): plus body"}. Repair may use builtins/string methods and preprovided re; no imports, decorators, annotations, global state, I/O, model calls or top-level code.',
        "source": (DATA / "baseline.py").read_text(),
        "training_cases": cases["train"],
        "full_pipeline": full,
    }
    proposal = decode(
        call_model(
            output,
            "proposal-recovery" if args.recover_empty_proposal else "proposal",
            [
                {
                    "role": "system",
                    "content": "You are a fixed Meta Agent diagnosing pure answer rules. JSON only.",
                },
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
        )
    )
    if not isinstance(proposal, dict) or not isinstance(
        proposal.get("hypotheses"), list
    ):
        raise TypeError("Invalid proposal; no retry")
    validate_source(proposal["repair_source"])
    write(output / "proposal.json", proposal)
    (output / "repair.py").write_text(proposal["repair_source"] + "\n")
    subprocess.run(
        [sys.executable, __file__, "--output", str(output), "--evaluate-child"],
        check=True,
        timeout=25,
    )
    if args.recover_empty_proposal:
        write(
            output / "run-status.json",
            {
                "calls": 2,
                "reflection": "omitted_to_preserve_two_call_budget",
                "first_call": "empty_length_limited_response",
                "recovery": "thinking_disabled",
            },
        )
        return
    report = read(output / "evidence.json")

    def compact(audit):
        return [
            {
                "enabled": v["enabled"],
                "cases": [
                    {k: value for k, value in row.items() if k != "events"}
                    for row in v["cases"]
                ],
            }
            for v in audit["variants"]
        ]

    reflection_input = {
        "proposal": proposal,
        "hypothesis_checks": report["train"]["before"]["hypotheses"],
        "training_interventions": compact(report["train"]["before"]),
        "training_repair": compact(report["train"]["repair"]),
        "instruction": "Reassess each hypothesis from controller evidence. State supported/refuted/insufficient; explain narrow input-output mechanism and limitations. Concise Chinese JSON only. No new patch or general RSI efficacy claim.",
    }
    reflection = call_model(
        output,
        "reflection",
        [
            {
                "role": "system",
                "content": "Review the executable evidence as the same fixed Meta Agent.",
            },
            {
                "role": "user",
                "content": json.dumps(reflection_input, ensure_ascii=False),
            },
        ],
    )
    (output / "reflection.txt").write_text(reflection + "\n")
    if frozen != {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in DATA.iterdir()
        if p.is_file()
    }:
        raise RuntimeError("Frozen inputs changed")


if __name__ == "__main__":
    main()
