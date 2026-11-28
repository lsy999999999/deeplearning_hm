"""Summarize the frozen shared-candidate study without further model calls."""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "FlashRSI"))
from export_evolution import changed_files
from flashrsi._run_summary import _add_usage, _collect_usage, _empty
from public_evidence import public_evidence


def read(p):
    return json.loads(p.read_text())


def canonical_usage(root):
    # This study's cache is evaluation reuse, not the native cache/target layout.
    # Count original trajectory records, not reuse copies and not an empty cache.
    with TemporaryDirectory(prefix="continuous-usage-") as temporary:
        view = Path(temporary)
        (view / "trajectories").symlink_to(
            root.resolve() / "trajectories", target_is_directory=True
        )
        return _collect_usage(view)


def regressed(arm, baseline):
    return sum(arm["per_task"][k] < v for k, v in baseline["per_task"].items())


def totals(cases):
    output = {}
    for policy in ("base", "gate_only", "enhanced"):
        aggregate = {
            "accepted": 0,
            "observed_bad_passes": 0,
            "observed_good_B_blocked": 0,
            "observed_bad_B_count": 0,
            "observed_good_B_count": 0,
            "test_correct": 0,
            "test_f1_sum": 0,
            "test_n": 0,
            "selected_regressions": 0,
        }
        for case in cases:
            test = case["test"]
            a = test["A"]
            b = test.get("B")
            pick = case["selection"][policy]
            selected = test[pick]
            aggregate["accepted"] += pick != "A"
            aggregate["observed_bad_passes"] += (
                pick != "A" and regressed(selected, a) > 0
            )
            aggregate["selected_regressions"] += regressed(selected, a)
            aggregate["test_correct"] += sum(selected["per_task"].values())
            aggregate["test_f1_sum"] += selected["f1"] * selected["n"]
            aggregate["test_n"] += selected["n"]
            if b:
                bad = regressed(b, a) > 0
                good = b["usable"] and b["em"] > a["em"] and not bad
                aggregate["observed_bad_B_count"] += bad
                aggregate["observed_good_B_count"] += good
                aggregate["observed_good_B_blocked"] += good and pick == "A"
        aggregate["test_em"] = (
            aggregate["test_correct"] / aggregate["test_n"]
            if aggregate["test_n"]
            else None
        )
        aggregate["test_f1"] = (
            aggregate["test_f1_sum"] / aggregate["test_n"]
            if aggregate["test_n"]
            else None
        )
        output[policy] = aggregate
    return output


def phase_usage(root):
    phases = {}
    seen = set()
    for p in sorted((root / "trajectories").glob("*.json")):
        record = read(p)
        if "trajectory" in record:
            prompt = record.get("prompt", "")
            phase = (
                "proposal"
                if prompt.startswith("Propose ONE")
                else "diagnosis"
                if prompt.startswith("Diagnose ONLY")
                else "reflection_and_repair"
            )
            rows = [record["trajectory"]]
            role = "worker"
        elif "trajectories" in record:
            phase = (
                "final_test"
                if "-study_test_" in p.name
                else "validation"
                if "-study_dev_" in p.name
                else "learning"
            )
            rows = record["trajectories"]
            role = "target"
        else:
            continue
        total = phases.setdefault(phase, _empty())
        for row in rows:
            _add_usage(total, row.get("usage", {}), seen, role)
            total["records"] += 1
            total["failed_records"] += row.get("status") != "finished"
    return phases


def verify_protocol(root, cases, protocol):
    tasks = read(root / "tasks.json")
    ids = {key: {row["task_id"] for row in rows} for key, rows in tasks.items()}
    workers = []
    tests = []
    for p in sorted((root / "trajectories").glob("*.json")):
        record = read(p)
        if "trajectory" in record:
            workers.append((p, record))
        if "-study_test_" in p.name:
            tests.append(p)
    freeze = read(root / "selection-freeze.json")
    checks = {
        "ten_actual_candidates": len(cases) == 10,
        "split_sizes": {key: len(value) for key, value in ids.items()},
        "disjoint_task_ids": sum(map(len, ids.values()))
        == len(set.union(*ids.values())),
        "frozen_selections_match": all(
            frozen["selection"] == case["selection"]
            for frozen, case in zip(freeze["cases"], cases, strict=True)
        ),
        "no_dev_or_test_ids_in_worker_prompts": not any(
            task_id in record["prompt"]
            for _, record in workers
            for task_id in ids["dev"] | ids["test"]
        ),
        "worker_calls_per_request": sorted(
            {len(record["trajectory"]["usage"]["model_calls"]) for _, record in workers}
        ),
        "no_worker_records_after_test_begins": bool(tests)
        and max(int(p.name.split("-")[0]) for p, _ in workers)
        < min(int(p.name.split("-")[0]) for p in tests),
        "selection_freeze_precedes_test_records": bool(tests)
        and (root / "selection-freeze.json").stat().st_mtime_ns
        <= min(p.stat().st_mtime_ns for p in tests),
        "driver_hash_matches": hashlib.sha256(
            (ROOT / "scripts/run_continuous_diagnosis.py").read_bytes()
        ).hexdigest()
        == protocol["driver_sha256"],
        "config_hash_matches": hashlib.sha256(
            (ROOT / "configs/continuous_diagnosis_20261128.yaml").read_bytes()
        ).hexdigest()
        == protocol["config_sha256"],
    }
    if any(
        value is False
        for key, value in checks.items()
        if key != "ten_actual_candidates"
    ):
        raise ValueError(f"Protocol evidence check failed: {checks}")
    return checks


def diagram(cases):
    height = 115 + 58 * len(cases)
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="{height}" viewBox="0 0 1100 {height}">',
        '<rect width="1100" height="100%" fill="white"/>',
        "<style>text{font-family:system-ui,sans-serif;font-size:17px;fill:#243247}.h{font-size:24px;font-weight:600}</style>",
        '<text x="22" y="34" class="h">连续自然候选：同候选上的三种选择</text>',
        '<text x="22" y="65">每行是独立候选；A 原版 · B 候选 · D 验证后的修复。不是累积收益曲线。</text>',
        '<text x="22" y="96">候选</text><text x="180" y="96">原选择流程</text><text x="440" y="96">仅零退步门槛</text><text x="730" y="96">诊断验证与修复</text>',
    ]
    for i, c in enumerate(cases):
        y = 125 + 58 * i
        lines += [f'<text x="22" y="{y}">#{c["index"] + 1} · {c["B"][:8]}</text>']
        for x, policy in [(180, "base"), (440, "gate_only"), (730, "enhanced")]:
            k = c["selection"][policy]
            s = c["test"][k]
            lines += [
                f'<rect x="{x - 8}" y="{y - 22}" width="240" height="43" rx="5" fill="{"#e7f5ef" if k != "A" else "#f1f5f9"}"/>',
                f'<text x="{x}" y="{y}">{k} · 测试 {sum(s["per_task"].values()):.0f}/{s["n"]}</text>',
            ]
    lines.append(
        f'<text x="22" y="{height - 8}" style="font-size:14px">绿色表示采用修改，不代表测试获益；测试不参与选择。</text>'
    )
    lines.append("</svg>")
    return "\n".join(lines)


def journal(evidence, root, out):
    from record_iterations import DEFAULT_OUTPUT, append_entries, link

    source = link(out / "evidence.json", DEFAULT_OUTPUT)
    entries = []
    for case in evidence["cases"]:
        prefix = f"continuous-v1/{root.name}/{case['index']}"
        for change in evidence["code_changes"]:
            if change["case"] != case["index"]:
                continue
            snapshot = read(root / "snapshots" / f"{change['after']}.json")
            payload = {
                "step": case["index"],
                "phase": "shared_candidate_" + change["version"],
                "actor": "Meta" if change["version"] != "C" else "controller removal",
                "subject": "Task; fixed Meta, not self-update",
                "before_id": change["before"],
                "candidate_id": change["after"],
                "parent_id": snapshot.get("parent_id"),
                "worker_agent": "miniagent",
                "worker_model": "DeepSeek-V4-Flash",
                "worker_snapshot_id": evidence["protocol"]["worker_snapshot"],
                "changes": change["changes"],
                "explanation": case["history"].get("reason")
                if change["version"] == "B"
                else "See the recorded intervention and repair evidence.",
            }
            entries.append(
                (
                    prefix + "/" + change["version"],
                    "code_change",
                    public_evidence(payload),
                    source,
                )
            )
        entries.append(
            (
                prefix + "/selection",
                "decision",
                public_evidence(
                    {
                        "step": case["index"],
                        "candidate_id": case["B"],
                        "selection": case["selection"],
                        "dev": case["dev"],
                        "final_test_after_all_selections_frozen": case["test"],
                        "valid_requests": case.get("valid_request_count", 0),
                        "supported_harmful_requests": case.get(
                            "supported_harmful_requests", []
                        ),
                    }
                ),
                source,
            )
        )
    return append_entries(entries)


def report(root, out):
    result = read(root / "result.json")
    cases = result["cases"]
    summary = totals(cases)
    initial_n = sum(c["test"]["A"]["n"] for c in cases)
    initial = {
        "test_n": initial_n,
        "test_em": sum(sum(c["test"]["A"]["per_task"].values()) for c in cases)
        / initial_n,
        "test_f1": sum(c["test"]["A"]["f1"] * c["test"]["A"]["n"] for c in cases)
        / initial_n,
    }
    usage = canonical_usage(root)
    phases = phase_usage(root)
    assert (
        sum(p["input_tokens"] for p in phases.values())
        == usage["total"]["input_tokens"]
    )
    assert (
        sum(p["output_tokens"] for p in phases.values())
        == usage["total"]["output_tokens"]
    )
    proposals = sorted(root.glob("attempt-*-proposal.json"))
    protocol = read(root / "plan.json")
    evidence = {
        "protocol": protocol,
        "verification": verify_protocol(root, cases, protocol),
        "exposure": read(root / "exposure.json"),
        "candidate_count": len(cases),
        "proposal_attempts": len(proposals),
        "policies": summary,
        "initial_miniagent": initial,
        "cases": cases,
        "usage": usage,
        "phase_usage": phases,
        "diagnostic_evidence": [
            {"file": p.name, "data": read(p)}
            for p in sorted(root.glob("attempt-*-interventions.json"))
        ],
        "diagnoses": [
            {"file": p.name, "data": read(p)}
            for p in sorted(root.glob("attempt-*-diagnosis.json"))
        ],
        "reference_status": "controller evidence and heldout observations; independent human review pending",
        "elapsed_active_session_seconds": result["elapsed_active_session_seconds"],
    }
    evidence["code_changes"] = [
        {
            "case": c["index"],
            "version": key,
            "before": c["A"] if key == "B" else c["B"],
            "after": c[key],
            "changes": changed_files(
                read(root / "snapshots" / f"{c['A'] if key == 'B' else c['B']}.json")[
                    "files"
                ],
                read(root / "snapshots" / f"{c[key]}.json")["files"],
            ),
        }
        for c in cases
        for key in ("B", "C", "D")
        if c.get(key)
    ]
    evidence["proposal_outcomes"] = [
        {"file": p.name, "status": read(p)["status"], "problem": read(p).get("problem")}
        for p in proposals
    ]
    localization = {
        "candidates_with_boundaries": sum(c["boundary_count"] > 0 for c in cases),
        "valid_meta_requests": sum(c.get("valid_request_count", 0) for c in cases),
        "invalid_meta_requests": sum(len(c.get("invalid_requests", [])) for c in cases),
        "supported_harmful_requests": sum(
            len(c.get("supported_harmful_requests", [])) for c in cases
        ),
        "admissible_repairs": sum(bool(c.get("D")) for c in cases),
        "replayed_boundary_count": sum(c["boundary_count"] for c in cases),
        "boundaries_changed_by_full_rules": 0,
        "candidates_with_changed_boundaries": 0,
        "controller_single_rule_rescued_case_pairs": sum(
            len(effect.get("rescued_ids", []))
            for item in evidence["diagnostic_evidence"]
            for group in item["data"]["groups"]
            for effect in group.get("report", {}).get("effects", [])
        ),
    }
    for item in evidence["diagnostic_evidence"]:
        changed = 0
        for group in item["data"]["groups"]:
            variants = group.get("report", {}).get("variants", [])
            if not variants:
                continue
            full = max(variants, key=lambda v: len(v["enabled"]))
            empty = min(variants, key=lambda v: len(v["enabled"]))
            before = {row["id"]: row["output"] for row in empty["cases"]}
            changed += sum(row["output"] != before[row["id"]] for row in full["cases"])
        localization["boundaries_changed_by_full_rules"] += changed
        localization["candidates_with_changed_boundaries"] += changed > 0
    evidence["localization_counts"] = localization
    evidence["usage_provenance"] = {
        "source": "original trajectories; evaluation reuse cache excluded",
        "native_result_usage": result["usage"],
        "correction": "The native collector assumes cache/target layout when cache exists. This study uses a generic evaluation reuse cache; recompute from original trajectories.",
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "evidence.json").write_text(
        json.dumps(public_evidence(evidence), ensure_ascii=False, indent=2) + "\n"
    )
    (out / "selection.svg").write_text(diagram(cases))
    lines = [
        "# 连续自然修改：固定 Meta 的同候选对照",
        "",
        f"共完成 {len(cases)} 个实际候选，来自 {len(proposals)} 次按顺序保存的模型提议。无效提议和未成功修复不删除。",
        "本次为限定答案处理规则的可行性实验。MiniAgent 直接提交 JSON 文件补丁，控制器应用；每阶段一次模型调用，不使用编辑工具循环。",
        "两种流程共享候选及评估；另加入“只加零退步门槛”的对照，区分严格门槛和诊断修复的作用。不是两套策略各自演化的等预算实验。",
        "",
        "## 数据与选择边界",
        "",
        f"执行框架起点为 `{protocol['source_commit']}`；独立实验脚本及配置的冻结 SHA-256 见 plan.json，代码差异见 evidence.json。本结果只对应这次冻结执行。",
        "固定初始 MiniAgent 与固定 Meta；HotpotQA、DeepSeek-V4-Flash、温度 0。8 道训练题共享，各候选分配 4 道不同验证题和 4 道不同测试题，总划分 8/40/40。",
        "从 128 题候选池中按预定种子排序并排除历史曝光，冻结 ID 与内容；测试在全部候选和选择冻结后统一评估，不反馈给 Meta。",
        "每个候选从相同初始版本提出，Meta 可见之前提议及训练反馈，不可见验证或测试结果。重复内容仍记录，不能把模型随机性造成的分数差解释为代码效果。",
        "",
        "## 流程对照",
        "",
        "| 流程 | 接纳候选数 | 观测坏修改放行 | 观测好 B 被保留原版阻挡 | 最终测试 EM | 最终测试 F1 |",
        "|---|---:|---:|---:|---:|---:|",
        f"| 初始 MiniAgent（不选择修改） | 0/{len(cases)} | — | — | {initial['test_em']:.2%} | {initial['test_f1']:.2%} |",
    ]
    labels = {
        "base": "原选择规则",
        "gate_only": "仅零退步门槛",
        "enhanced": "诊断验证与修复 + 零退步门槛",
    }
    for k, v in summary.items():
        lines.append(
            f"| {labels[k]} | {v['accepted']}/{len(cases)} | {v['observed_bad_passes']} | {v['observed_good_B_blocked']}/{v['observed_good_B_count']} | {v['test_em']:.2%} | {v['test_f1']:.2%} |"
        )
    lines += [
        "",
        "“观测坏修改”指选中版本在该候选的 4 道测试题上至少使一道原本答对的题退步；“观测好 B”指 B 在这 4 题上严格提高 EM 且无退步；阻挡指该流程选择 A。分母为 0 时比例未定义，只展示计数。",
        "这些是小样本、独立模型调用下的结果，不是已经确立的因果真值或通用误报率。独立人工参考复核尚未完成。",
        "坏修改与门槛均按预先固定的 EM 定义；EM 不变但 F1 下降不计入上述坏修改数，仍在逐版本结果中保留。因此零个 EM 退步不等于所有答案质量都未下降。",
        "",
        "## 连续候选清单",
        "",
        "| 候选 | 候选训练 EM | 可重放边界 | 合法预测 | 有害原因得到局部支持 | 产生合格 D | 验证选择：原/仅门槛/增强 |",
        "|---|---|---:|---:|---:|---|---|",
    ]
    for c in cases:
        train = c.get("train")
        train_text = f"{train['em']:.2%}" if train else "不可评估"
        lines.append(
            f"| {c['index'] + 1} | B {train_text} | {c['boundary_count']} | {c.get('valid_request_count', 0)} | {len(c.get('supported_harmful_requests', []))} | {'是' if c.get('D') else '否'} | {c['selection']['base']} / {c['selection']['gate_only']} / {c['selection']['enhanced']} |"
        )
    lines += [
        "",
        "![逐候选选择](selection.svg)",
        "",
        f"有规则边界的候选 {localization['candidates_with_boundaries']}/{len(cases)}；合法 Meta 请求 {localization['valid_meta_requests']}，引用不合格请求 {localization['invalid_meta_requests']}，有害原因得到局部支持的请求 {localization['supported_harmful_requests']}，合格修复 {localization['admissible_repairs']}。",
        f"完整规则相对全部关闭实际改变答案的边界为 {localization['boundaries_changed_by_full_rules']}/{localization['replayed_boundary_count']}，分布在 {localization['candidates_with_changed_boundaries']} 个候选中。保存了边界不等于规则触发，也不等于包含有害规则；未触发案例不能证明定位能力。",
        f"控制器在学习边界上发现的单规则关闭后恢复正确的案例对为 {localization['controller_single_rule_rescued_case_pairs']}。没有正例时定位召回率无法估计；没有请求时预测准确率无法估计，不填成 100%。",
        "C 只撤回 Meta 首个获得控制器支持的有害规则请求，保留候选其余修改；D 必须改善同一批冻结边界且不新增退步，才进入新题选择。没有支持的原因时 C/D 缺席，不能补造四个版本。",
        "全部可用训练边界均进入规则组合审计，包含未改变答案和有益规则；无法重放、边界缺失、输入输出引用不符均保留。每个规则组合重复两次。",
        "",
        "## 调用成本",
        "",
        "| 阶段 | 输入 token | 输出 token | 用量条目 |",
        "|---|---:|---:|---:|",
    ]
    for name, v in phases.items():
        lines.append(
            f"| {name} | {v['input_tokens']:,} | {v['output_tokens']:,} | {v['usage_entries']} |"
        )
    u = usage["total"]
    extra_tokens = sum(
        phases.get(name, {}).get("input_tokens", 0)
        + phases.get(name, {}).get("output_tokens", 0)
        for name in ("diagnosis", "reflection_and_repair")
    )
    common_tokens = sum(
        phases.get(name, {}).get("input_tokens", 0)
        + phases.get(name, {}).get("output_tokens", 0)
        for name in ("proposal", "learning", "validation")
    )
    extra_entries = sum(
        phases.get(name, {}).get("usage_entries", 0)
        for name in ("diagnosis", "reflection_and_repair")
    )
    lines += [
        "",
        f"合计输入加输出 {u['input_tokens'] + u['output_tokens']:,} tokens。缓存读取已包含在输入中，不重复相加。",
        f"本次正式运行耗时 {result['elapsed_active_session_seconds'] / 60:.2f} 分钟；包含模型等待与控制器执行，不含事前数据准备。局部重放未单独计时。",
        "用量从原始 trajectories 重算：本次通用评估缓存不符合旧汇总器的 cache/target 目录约定，旧 result 的用量字段漏计。公开报告以原始调用记录为准，分阶段输入和输出均已与总数核对。",
        "候选和共同评估成本只计一次；诊断与反思/修复属于增强流程额外调用。最终测试所有冻结版本用于离线比较，其费用属于研究测量成本。",
        f"增强流程的诊断与反思/修复额外记录 {extra_entries} 个用量条目、{extra_tokens:,} tokens；共用的提议、学习评估与验证为 {common_tokens:,} tokens。控制器纯规则重放不调用模型。",
        "用量条目不等于供应商账单请求数；未返回实际费用。失败已知用量保留，未知用量不视为零。",
        "",
        "## 当前可汇报范围",
        "",
        "这批连续案例用于检查系统能否发现未知错误、执行干预和避免无依据修复。需要结合逐个诊断、控制器证据和新题结果解释，不能只按总分推断因果。",
        "即使局部干预支持原因，也不保证修复在新题泛化；即使更严格流程放行更少坏修改，也可能误拦有用改进。比较增强流程与仅门槛流程，才能检查诊断修复是否增加了价值。",
        "检索、重试和规划尚未纳入同样完整的干预；10 个候选及每候选 4 道测试题不足以证明稳定有效。",
        "",
        f"原始本地记录：`{root.relative_to(ROOT)}`。公开证据省略数据集上下文与完整轨迹，以哈希关联原始记录。",
    ]
    (out / "REPORT_CN.md").write_text("\n".join(lines) + "\n")
    print(f"Journal entries appended: {journal(evidence, root, out)}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    a = parser.parse_args()
    report(a.run.resolve(), a.output.resolve())
