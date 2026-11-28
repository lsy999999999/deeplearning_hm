"""Append native and frozen historical RSI edits to one Markdown journal.

No model calls. Existing entries are immutable: repeated imports are idempotent;
a changed source under the same identity raises instead of rewriting history.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import os
import sys
from pathlib import Path

from filelock import FileLock
from flashrsi.algorithms.meta_harness._journal import render_event

sys.path.insert(0, str(Path(__file__).resolve().parent))
from public_evidence import public_evidence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "results/RSI_ITERATIONS.md"


def read(path):
    return json.loads(path.read_text())


def sha(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def link(path, output):
    return f"[{path.name}]({Path(os.path.relpath(path, output.parent)).as_posix()})"


def append_entries(entries, output=DEFAULT_OUTPUT):
    output = output.resolve()
    index_path = output.with_suffix(".index.json")
    # A single lock covers Markdown and its index. Recoverable event hashes are
    # also embedded in Markdown, so an interrupted index write cannot duplicate.
    lock = ROOT / "results/private/rsi-iteration-journal.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    added = 0
    with FileLock(str(lock)):
        existing = output.read_text() if output.exists() else ""
        index = read(index_path) if index_path.exists() else {}
        for event_id, label, payload, source in entries:
            payload_hash = sha({"label": label, "payload": payload, "source": source})
            marker = f"<!-- rsi-source:{event_id}:{payload_hash} -->"
            if event_id in index:
                if index[event_id] != payload_hash:
                    raise ValueError(
                        f"Journal source changed for {event_id}; retain old event and add a correction under a new identity"
                    )
                if marker not in existing:
                    raise ValueError(
                        "Journal/index mismatch; preserve both and inspect before continuing"
                    )
                continue
            if f"<!-- rsi-source:{event_id}:" in existing:
                if marker not in existing:
                    raise ValueError(f"Changed source for recovered event {event_id}")
                index[event_id] = payload_hash
                continue
            text = render_event(event_id, label, payload, source)
            if not text:
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("a") as stream:
                if stream.tell() == 0:
                    stream.write(
                        "# RSI 机器代码迭代持续记录\n\n此文档按导入顺序追加，既保留采用的修改，也保留拒绝的尝试。每个文件对应实际 diff 和机器理由；缺少逐文件理由时明确留空。Task、Meta 自身、影子 Task 与独立规则试验分别标识。机器理由为待验证声明，采用决定和实验事实另列。框架开发者的代码维护不计作机器自改进成果。\n\n原始示例保护清单：[originals-1ad8a37.json](../docs/demo_archive/originals-1ad8a37.json)。旧图片、配置、输入、基线与修复均保留原路径。\n"
                    )
                stream.write("\n" + marker + "\n" + text)
            existing += "\n" + marker + "\n" + text
            index[event_id] = payload_hash
            added += 1
        index_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = index_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(index_path)
    return added


def native_entries(directory, output):
    labels = {
        "code_change",
        "audit",
        "repair",
        "diagnosis",
        "meta_decision",
        "rule_intervention",
        "decision",
    }
    for record in sorted((directory / "trajectories").glob("*.json")):
        label = record.stem.split("-", 1)[-1]
        if label in labels:
            yield (
                f"public-v1/{directory.name}/{record.stem}",
                label,
                public_evidence(read(record)),
                link(record, output),
            )


def append_run(directory, output=DEFAULT_OUTPUT):
    return append_entries(native_entries(directory.resolve(), output.resolve()), output)


def historical_entries(view, output):
    for run in read(view)["runs"]:
        for round_ in run["rounds"]:
            prefix = f"historical/{run['run']}/{round_['step']}"
            candidate = round_.get("candidate_id")
            if candidate:
                payload = {
                    "step": round_["step"],
                    "phase": "historical_task_proposal",
                    "actor": "Meta",
                    "subject": "Task (historical; not Meta self-edit)",
                    "before_id": round_.get("baseline_id"),
                    "parent_id": None,
                    "candidate_id": candidate,
                    "explanation": round_.get("worker")
                    or "机器原始说明未包含在此导出中。",
                    "changes": [
                        {
                            **change,
                            "reason": None,
                            "reason_status": "not_recorded_per_file",
                        }
                        for change in round_.get("changes", [])
                    ],
                }
                yield (
                    prefix + "/proposal",
                    "code_change",
                    payload,
                    link(view, output) + f" · {run['run']} · round {round_['step']}",
                )
            decision = {
                k: round_.get(k)
                for k in (
                    "step",
                    "baseline_id",
                    "candidate_id",
                    "selected_id",
                    "selected_paths",
                    "status",
                    "problem",
                    "variants",
                    "invalid",
                    "retention",
                )
            }
            # The raw exported view remains the detailed task evidence. Avoid
            # duplicating every question and answer in the Markdown decision.
            decision["variants"] = [
                {k: v for k, v in variant.items() if k != "tasks"}
                for variant in round_.get("variants", [])
            ]
            decision["historical_scope"] = (
                "historical Task modification; no new run or Meta self-improvement inferred"
            )
            yield prefix + "/decision", "audit", decision, link(view, output)


def pilot_entries(directory, output):
    proposal = read(directory / "proposal.json")
    baseline = ROOT / "research/rule_audit_pilot/baseline.py"
    repair = directory / "repair.py"
    old = baseline.read_text()
    tree = ast.parse(old)
    old_rule = (
        ast.get_source_segment(
            old,
            next(
                n
                for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "comma_gloss"
            ),
        )
        + "\n"
    )
    new = repair.read_text()
    diff = "".join(
        difflib.unified_diff(
            old_rule.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile="baseline.py::comma_gloss (calls preserved _drop_trailing_gloss)",
            tofile="repair.py::comma_gloss",
        )
    )
    payload = {
        "step": 0,
        "phase": "frozen_rule_pilot",
        "actor": "fixed Meta",
        "subject": "IndependentRuleTrial (not a promoted Task or Meta version)",
        "before_id": "file-sha256:" + hashlib.sha256(baseline.read_bytes()).hexdigest(),
        "parent_id": None,
        "candidate_id": "file-sha256:"
        + hashlib.sha256(repair.read_bytes()).hexdigest(),
        "explanation": json.dumps(proposal, ensure_ascii=False, indent=2),
        "changes": [
            {
                "path": "comma_gloss binding: baseline.py → repair.py",
                "operation": "replace callable binding; baseline file preserved",
                "component": "pure answer rule",
                "diff": diff,
                "reason": "\n".join(h["claim"] for h in proposal.get("hypotheses", [])),
                "reason_status": "model_stated_unverified",
                "evidence_task_ids": [
                    h["case_id"] for h in proposal.get("hypotheses", [])
                ],
            }
        ],
    }
    prefix = f"pilot/{directory.name}"
    yield (
        prefix + "/repair-proposal",
        "code_change",
        payload,
        link(directory / "proposal.json", output)
        + " · "
        + link(baseline, output)
        + " · "
        + link(repair, output),
    )
    evidence = read(directory / "evidence.json")
    summary = {
        split: evidence[split]["summary"]
        for split in ("train", "heldout", "historical")
    }
    summary.update(
        decision=evidence["decision"],
        hypotheses=evidence["train"]["before"]["hypotheses"],
        scope="controlled pilot and historical boundary proxies; not end-to-end RSI efficacy",
    )
    yield (
        prefix + "/verification",
        "rule_intervention",
        summary,
        link(directory / "evidence.json", output)
        + " · "
        + link(directory / "REPORT_CN.md", output),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, action="append", default=[])
    parser.add_argument("--historical-view", type=Path)
    parser.add_argument("--pilot", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    entries = []
    for directory in args.run:
        entries.extend(native_entries(directory.resolve(), args.output.resolve()))
    if args.historical_view:
        entries.extend(
            historical_entries(args.historical_view.resolve(), args.output.resolve())
        )
    if args.pilot:
        entries.extend(pilot_entries(args.pilot.resolve(), args.output.resolve()))
    if not entries:
        parser.error("Supply --run, --historical-view or --pilot with existing records")
    print(
        f"Appended {append_entries(entries, args.output)} journal entries to {args.output}"
    )


if __name__ == "__main__":
    main()
