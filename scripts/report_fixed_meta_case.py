"""Export factual fixed-Meta results; missing interventions stay missing."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "FlashRSI"))
from flashrsi._run_summary import _collect_usage
from public_evidence import public_evidence


def report(directory, output):
    read = lambda p: json.loads(p.read_text())
    result = read(directory / "result.json")
    records = [
        (p.name, read(p)) for p in sorted((directory / "trajectories").glob("*.json"))
    ]
    selected = lambda label: [
        {"record": n, **d} for n, d in records if n.endswith("-" + label + ".json")
    ]
    evaluations = []
    for name, data in records:
        if "trajectories" not in data:
            continue
        rows = data["trajectories"]
        evaluations.append(
            {
                "record": name,
                "snapshot_id": data["snapshot_id"],
                "n": len(rows),
                "em": sum(t["metrics"]["exact_match"] for t in rows) / len(rows),
                "f1": sum(t["metrics"]["f1"] for t in rows) / len(rows),
                "rule_receipts": sum(
                    len(t["usage"].get("rule_replays", [])) for t in rows
                ),
                "failed": sum(
                    t["status"] != "finished" or bool(t.get("error_message"))
                    for t in rows
                ),
            }
        )
    payload = {
        "run": directory.name,
        "result": result,
        "evaluations": evaluations,
        "diagnoses": selected("diagnosis"),
        "interventions": selected("rule_intervention"),
        "changes": selected("code_change"),
        "repairs": selected("repair"),
        "audits": selected("audit"),
        "meta_decisions": selected("meta_decision"),
        "usage": _collect_usage(directory),
        "execution": read(directory / "execution.json"),
        "scope": "single natural candidate; no general precision/recall or repair efficacy claim",
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "evidence.json").write_text(
        json.dumps(public_evidence(payload), ensure_ascii=False, indent=2) + "\n"
    )
    lines = [
        "# 固定 Meta：首个自然候选诊断实验（2026-11-28）",
        "",
        "本次为真实 MiniAgent + DeepSeek-V4-Flash 实验。Meta 自更新关闭，规则验证开启。",
        "训练/验证/测试为 8/8/16，seed 20261129，temperature 0，单次最大输出 8000 token。",
        "任务 ID 和题目内容在调用前与本地历史记录去重。清单见 [冻结计划](../fixed-meta-diagnosis-20261128-plan.json)。",
        "",
        "## 结果",
        "",
        "| 评估记录 | EM | F1 | 题数 | 规则回执 | 失败题 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for e in evaluations:
        lines.append(
            f"| {e['record']} | {e['em']:.2%} | {e['f1']:.2%} | {e['n']} | {e['rule_receipts']} | {e['failed']} |"
        )
    lines += [
        "",
        "## 诊断证据",
        "",
        "| Meta 声明（待验证） | 执行验证文字 | 结构化请求数 |",
        "|---|---|---:|",
    ]
    for d in payload["diagnoses"]:
        for issue in d.get("issues", []):
            cell = lambda s: str(s).replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {cell(issue['claim'])} | {cell(issue['verification'])} | {len(d.get('interventions', []))} |"
            )
    lines += [
        "",
        "以上是 Meta 的原始声明，不是报告作者认可的结论。控制器的原始组合结果与预测核验保存在 evidence.json。",
        "",
        "## 调用成本",
        "",
    ]
    usage = payload["usage"]["total"]
    lines += [
        f"记录输入 {usage['input_tokens']:,}、输出 {usage['output_tokens']:,}，合计 {usage['input_tokens'] + usage['output_tokens']:,} tokens；缓存读取已包含在输入中。",
        f"用量条目 {usage['usage_entries']}，失败记录 {usage['failed_records']}。费用未返回，未落盘消耗不视为零。",
        "",
        "## 证据边界",
        "",
        "每个文件的模型理由、诊断、干预和修复决定保留在 evidence.json。公开导出以哈希代替数据集上下文和完整评估轨迹，原始证据留在本地 runs。所有测试结果在版本选择之后产生；未反馈给修复。",
        "一个候选不足以估计定位 precision/recall；尚未完成 12 个受控案例和至少 8 个自然案例。",
        "",
        f"原始运行：`{directory.relative_to(ROOT)}`。",
    ]
    (output / "REPORT_CN.md").write_text("\n".join(lines) + "\n")
    print(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report(args.run.resolve(), args.output.resolve())
