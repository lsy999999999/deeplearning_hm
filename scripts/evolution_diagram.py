"""Draw the recorded proposal and selection history as a portable SVG."""

from __future__ import annotations

from html import escape


def svg_diagram(run):
    """One row per round; the right-hand path tracks retained versions."""
    rounds = run["rounds"]
    height = 220 + 132 * len(rounds)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="{height}" style="max-width:100%;height:auto" viewBox="0 0 1000 {height}" role="img" aria-labelledby="diagram-title diagram-desc">',
        f'<title id="diagram-title">{escape(run["name"])} 逐轮进化选择</title>',
        '<desc id="diagram-desc">左侧为每轮提出的候选；右侧主线为该轮选择后实际保留的版本。候选标注其真实父版本，最终测试分数单独展示。</desc>',
        '<defs><marker id="evo-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M1 1L7 4L1 7" fill="none" stroke="#687481" stroke-width="1.5"/></marker></defs>',
        f'<rect width="1000" height="{height}" fill="#fff"/>',
        '<style>text{font-family:system-ui,"Noto Sans CJK SC",sans-serif;fill:#1e293b;font-size:17px}.small{font-size:15px;fill:#475569}.heading{font-size:23px;font-weight:600}.node{fill:#f8fafc;stroke:#ccd4dd;stroke-width:1.2}.chosen{fill:#e7f5ef;stroke:#8eb8a4;stroke-width:1.2}.link{fill:none;stroke:#687481;stroke-width:1.5;marker-end:url(#evo-arrow)}</style>',
    ]

    def text(x, y, value, cls=""):
        parts.append(f'<text x="{x}" y="{y}" class="{cls}">{escape(str(value))}</text>')

    def box(x, y, width, lines, selected=False):
        cls = "chosen" if selected else "node"
        parts.append(
            f'<rect x="{x}" y="{y}" width="{width}" height="90" rx="7" class="{cls}"/>'
        )
        for i, line in enumerate(lines):
            text(x + 16, y + 27 + i * 25, line, "" if i == 0 else "small")

    def score(version):
        if not version or version.get("score") is None:
            return "未评估"
        value = f"{version['score']:.2%}"
        if run["metric"] == "exact_match" and version["count"]:
            value += f"（{version['score'] * version['count']:g}/{version['count']}）"
        return value

    text(24, 34, f"{run['name']} · {len(rounds)} 轮进化", "heading")
    text(
        24,
        62,
        f"{run['model']} · seed {run['seed']} · "
        + (
            "每轮使用不同的新开发题；右侧为保留版本"
            if run.get("method") == "ca_rsi"
            else "同一开发集；右侧主线为保留版本"
        ),
        "small",
    )
    initial = (run.get("initial_id") or "未记录")[:10]
    initial_eval = (
        next(
            (v for v in rounds[0]["variants"] if v["id"] == run.get("initial_id")), None
        )
        if rounds
        else None
    )
    initial_label = "首轮新题基线" if run.get("method") == "ca_rsi" else "开发集基线"
    box(
        520,
        85,
        450,
        ["初始版本", f"{initial_label}：{score(initial_eval)}", f"Snapshot {initial}"],
    )
    current = run.get("initial_id")
    previous_bottom = 175
    for i, round_ in enumerate(rounds):
        y = 217 + i * 132
        variants = round_["variants"]
        candidate = next(
            (v for v in variants if v["id"] == round_["candidate_id"]), None
        )
        selected_id = round_["selected_id"] or current
        chosen = next((v for v in variants if v["id"] == selected_id), None)
        if round_["status"] == "skipped_edit_budget":
            proposal_score = "修改文件数超限，未审计候选"
        elif round_["status"] == "declined":
            proposal_score = "未形成有效候选"
        else:
            proposal_score = f"候选开发集：{score(candidate)}"
        box(
            24,
            y,
            390,
            [
                f"第 {round_['step'] + 1} 轮 · 未形成有效候选"
                if round_["status"] == "declined"
                else f"第 {round_['step'] + 1} 轮候选 · {len(round_['changes'])} 个文件修改",
                proposal_score,
                f"候选 {(round_['candidate_id'] or '无')[:8]} · 父版本 {(round_['baseline_id'] or current or '未记录')[:8]}",
            ],
        )
        accepted = bool(round_["selected_paths"])
        if not accepted:
            choice = "保留原有版本"
        elif selected_id == round_["candidate_id"]:
            choice = "接受完整候选"
        else:
            choice = f"接受文件子集（{len(round_['selected_paths'])} 项）"
        change = ""
        if chosen:
            change = f" · 改正 {chosen['improved']} / 退步 {chosen['regressions']}"
        retained_score = chosen or {
            "score": round_.get("retained_score"),
            "count": round_.get("retained_count", 0),
        }
        box(
            520,
            y,
            450,
            [
                f"第 {round_['step'] + 1} 轮选择：{choice}",
                f"保留版本开发集：{score(retained_score)}{change}",
                f"Snapshot {(selected_id or '未记录')[:10]}",
            ],
            selected=accepted,
        )
        parts.append(f'<path d="M745 {previous_bottom}V{y - 5}" class="link"/>')
        parts.append(f'<path d="M424 {y + 45}H510" class="link"/>')
        current, previous_bottom = selected_id, y + 90
    footer = height - 23
    final = run.get("final_id") or "未记录"
    test = f"{run['test_score']:.2%}" if run["test_score"] is not None else "未记录"
    text(
        24,
        footer,
        f"最终版本 {final[:10]} · 最终测试 {test} · {run.get('test_tasks') or 0} 题（不参与选择）",
        "small",
    )
    parts.append("</svg>")
    return "\n".join(parts)
