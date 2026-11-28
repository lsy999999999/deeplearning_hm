"""Append-only, source-linked Markdown for machine proposals and decisions."""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

REASON_INSTRUCTION = """\nFor the audit journal, append a <CHANGE_REASONS> JSON block to your explanation:
{"files":[{"path":"exact changed relative path","reason":"why this file needed this change","evidence_task_ids":["only IDs you actually used"]}]}.
Include every added, modified or deleted file exactly once. Keep a file's reason
specific to its actual edit. Explanations are hypotheses, not verified causes.
Do not create this metadata as a target harness file.
"""


def changes(before, after, explanation):
    """Bind a model-stated per-file reason to an exact versioned diff."""
    matches = re.findall(
        r"<CHANGE_REASONS>\s*(.*?)\s*</CHANGE_REASONS>", explanation, re.S
    )
    reasons, problem = {}, None
    try:
        if len(matches) != 1:
            raise ValueError("Expected exactly one CHANGE_REASONS block")
        rows = json.loads(matches[0])["files"]
        if not isinstance(rows, list):
            raise ValueError("files is not a list")
        for row in rows:
            path = row["path"]
            if (
                path in reasons
                or not isinstance(row.get("reason"), str)
                or not row["reason"].strip()
            ):
                raise ValueError("Duplicate file or missing reason")
            reasons[path] = row
        if set(reasons) - set(before.changed_paths(after)):
            raise ValueError("Reason references an unchanged file")
    except (ValueError, TypeError, KeyError) as error:
        reasons, problem = {}, str(error)
    result = []
    for path in before.changed_paths(after):
        old, new = before.files.get(path), after.files.get(path)
        left, right = old.content if old else b"", new.content if new else b""
        try:
            delta = difflib.unified_diff(
                left.decode().splitlines(keepends=True),
                right.decode().splitlines(keepends=True),
                fromfile="before/" + path,
                tofile="after/" + path,
            )
            diff = "".join(
                line
                if line.endswith("\n")
                else line + "\n\\ No newline at end of file\n"
                for line in delta
            )
            if not diff:
                diff = "Content unchanged; executable/component metadata changed."
        except UnicodeDecodeError:
            diff = f"Binary file: {len(left)} -> {len(right)} bytes"
        row = reasons.get(path)
        result.append(
            {
                "path": path,
                "operation": "add"
                if old is None
                else "delete"
                if new is None
                else "modify",
                "component": (new or old).component,
                "diff": diff,
                "reason": row["reason"] if row else None,
                "reason_status": "model_stated_unverified"
                if row
                else "not_recorded_per_file",
                "evidence_task_ids": row.get("evidence_task_ids", []) if row else [],
            }
        )
    return {
        "changes": result,
        "explanation": explanation,
        "reason_metadata_problem": problem,
        "reason_coverage": {
            "mapped": sum(r["reason"] is not None for r in result),
            "changed_files": len(result),
        },
    }


def fence(text, language="text"):
    text = str(text)
    marker = "`" * max(3, max((len(x) + 1 for x in re.findall(r"`+", text)), default=3))
    return f"{marker}{language}\n{text}\n{marker}\n"


def render_event(event_id, label, payload, source):
    """A decision is a NEW entry; never rewrite the earlier proposal."""
    titles = {
        "code_change": "机器代码修改",
        "audit": "Task 组合审计决定",
        "repair": "Task 修复检查",
        "diagnosis": "Meta 诊断假设",
        "meta_decision": "Meta 自身换版决定",
        "rule_intervention": "规则干预执行证据",
        "decision": "Task 选择或无修改决定",
    }
    if label not in titles:
        return ""
    lines = [
        f"\n<!-- rsi-event:{event_id} -->\n",
        f"## {event_id} · {titles[label]}\n",
        f"原始记录：{source}\n",
    ]
    if label == "code_change":
        lines += [
            f"轮次（从 0 开始）：{payload.get('step')}；阶段：{payload.get('phase')}；修改者：{payload.get('actor', 'Meta')}；被修改对象：{payload.get('subject')}。\n",
            f"差异比较起点：`{payload.get('before_id', payload.get('parent_id'))}` → 候选版本：`{payload.get('candidate_id')}`。实际 Snapshot 父版本：`{payload.get('parent_id')}`（None 表示原始记录未提供或根版本）。\n",
            "此处仅记录提出的修改；是否采用见后续对应版本的决定条目。\n",
        ]
        lines += [
            f"执行修改的 Worker：`{payload.get('worker_agent', '历史未记录')}`；模型：`{payload.get('worker_model', '历史未记录')}`；Worker 版本：`{payload.get('worker_snapshot_id', '历史未记录')}`。\n"
        ]
        if not payload.get("changes"):
            lines += ["本次未产生文件改动。\n"]
        for index, row in enumerate(payload.get("changes", []), 1):
            lines += [
                f"### {event_id}.{index} · `{row['path']}`\n",
                f"操作：{row.get('operation')}；组件：{row.get('component')}。\n",
            ]
            if row.get("reason"):
                lines += ["机器为此文件给出的理由（待验证）：\n", fence(row["reason"])]
            else:
                lines += ["逐文件理由：**未单独记录**。不将整段说明臆配给此文件。\n"]
            if row.get("evidence_task_ids"):
                lines += [
                    "机器引用的证据 ID（引用声明，不等于证据已核验）：\n",
                    fence(json.dumps(row["evidence_task_ids"], ensure_ascii=False)),
                ]
            lines += ["实际代码差异：\n", fence(row.get("diff", ""), "diff")]
        lines += ["机器的完整原始说明：\n", fence(payload.get("explanation", "未记录"))]
    else:
        # Native event contains exact task/candidate IDs and the decision rule.
        lines += [fence(json.dumps(payload, ensure_ascii=False, indent=2))]
    return "\n".join(lines)


def append_native(run_dir: Path, record: Path, label: str):
    """Use the already-redacted native record as the single source."""
    payload = json.loads(record.read_text())
    text = render_event(
        record.stem, label, payload, f"[{record.name}](trajectories/{record.name})"
    )
    if not text:
        return
    journal = run_dir / "RSI_ITERATIONS.md"
    with journal.open("a", encoding="utf-8") as stream:
        if stream.tell() == 0:
            stream.write(
                "# RSI 机器修改与验证记录\n\n按发生顺序追加。Task 修改、Meta 自身修改和仅供验证的影子 Task 修改分别标识；理由是机器陈述，实验结论另记。未改动的原始代码由 snapshots/ 中的父版本保存。\n"
            )
        stream.write(text)
