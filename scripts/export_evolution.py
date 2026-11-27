"""Export Meta-Harness / CA-RSI native records without calling any model."""

from __future__ import annotations

import argparse
import base64
import difflib
import json
import tempfile
from pathlib import Path
from statistics import mean

import yaml
from evolution_diagram import svg_diagram
from filelock import FileLock

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = Path(__file__).with_name("evolution_view.html")
SHELL = Path(__file__).with_name("evolution_shell.html")
DEFAULT_OUTPUT = ROOT / "results/evolution-view.html"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def changed_files(before, after):
    """Compare snapshot entries, including deletion and executable changes."""
    rows = []
    for name in sorted(before.keys() | after.keys()):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        left = base64.b64decode(old["content"]) if old else b""
        right = base64.b64decode(new["content"]) if new else b""
        try:
            a, b = left.decode("utf-8"), right.decode("utf-8")
            diff = "".join(
                difflib.unified_diff(
                    a.splitlines(keepends=True),
                    b.splitlines(keepends=True),
                    fromfile="原版/" + name,
                    tofile="候选/" + name,
                )
            )
            if not diff:
                diff = "文件内容未变；元数据（例如可执行权限）发生变化。"
        except UnicodeDecodeError:
            diff = f"二进制文件：{len(left)} → {len(right)} 字节"
        rows.append(
            {
                "path": name,
                "operation": "新增"
                if old is None
                else "删除"
                if new is None
                else "修改",
                "component": (new or old).get("component", "unknown"),
                "diff": diff,
            }
        )
    return rows


def task_rows(record, metric):
    rows = {}
    for item in record.get("trajectories", []):
        task = item["task"]
        rows[task["task_id"]] = {
            "id": task["task_id"],
            "question": task["question"].rsplit("Question:", 1)[-1].strip(),
            "gold": task.get("ground_truth"),
            "output": item.get("output"),
            "score": item["metrics"][metric],
            "error": item.get("error_message"),
            "status": item.get("status"),
        }
    return rows


def variant(snapshot_id, paths, evaluations, baseline, source, score=None, deltas=None):
    items = evaluations.get(snapshot_id, {})
    tasks = [
        {
            **item,
            "before": baseline[task_id]["output"],
            "before_score": baseline[task_id]["score"],
            "delta": item["score"] - baseline[task_id]["score"],
        }
        for task_id, item in items.items()
        if task_id in baseline
    ]
    if deltas is None:
        deltas = {row["id"]: row["delta"] for row in tasks}
    return {
        "id": snapshot_id,
        "paths": list(paths),
        "source": source,
        "score": score
        if score is not None
        else mean(x["score"] for x in items.values())
        if items
        else None,
        "improved": sum(x > 0 for x in deltas.values()),
        "regressions": sum(x < 0 for x in deltas.values()),
        "errors": sum(
            x["status"] != "finished" or bool(x["error"]) for x in items.values()
        ),
        "tasks": tasks,
        "count": len(items) or len(deltas),
    }


def load_run(directory):
    config = yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))
    method = config["algorithm"]["name"]
    if method not in {"meta_harness", "ca_rsi"}:
        raise ValueError(f"Unsupported algorithm: {method}")
    records = [
        (p.name, read(p)) for p in sorted((directory / "trajectories").glob("*.json"))
    ]
    snapshots = {
        p.stem: read(p)
        for p in (directory / "snapshots").glob("*.json")
        if p.stem != "worker"
    }
    result_path = directory / "result.json"
    result = read(result_path) if result_path.exists() else {}
    metric = result.get("primary_metric", "exact_match")
    rounds, evaluations = [], {}
    baseline_record, initial_id, best_id, best_score = {}, None, None, None
    previous_worker = ""
    for name, record in records:
        label = name.split("-", 1)[-1].removesuffix(".json")
        # Training and retention evaluations can reuse the same Snapshot IDs.
        # They must never overwrite the fresh-dev evidence of this audit round.
        if label == "audit_baseline":
            evaluations = {}
        if "trajectories" in record and label in {
            "baseline",
            "candidate",
            "audit_baseline",
            "audit_candidate",
            "audit_subset",
        }:
            evaluations[record["snapshot_id"]] = task_rows(record, metric)
            if label in {"baseline", "audit_baseline"}:
                baseline_record = record
                if initial_id is None:
                    initial_id = record["snapshot_id"]
                    best_id = initial_id
                    best_score = mean(
                        x["score"] for x in evaluations[initial_id].values()
                    )
        if label == "worker":
            previous_worker = record.get("trajectory", {}).get("output", "")
        if label == "audit":
            base_id = record.get("baseline_id", baseline_record.get("snapshot_id"))
            candidate_id = record["candidate_id"]
            base = evaluations.get(base_id, {})
            variants = [
                variant(
                    row["snapshot_id"],
                    row["paths"],
                    evaluations,
                    base,
                    name,
                    row["score"],
                    row["per_task_delta"],
                )
                for row in record.get("variants", [])
            ]
            rounds.append(
                {
                    "step": record.get("step", len(rounds)),
                    "source": name,
                    "baseline_id": base_id,
                    "candidate_id": candidate_id,
                    "selected_id": record.get("selected_id", base_id),
                    "selected_paths": record.get("selected_paths", []),
                    "changes": changed_files(
                        snapshots[base_id]["files"], snapshots[candidate_id]["files"]
                    ),
                    "variants": variants,
                    "worker": previous_worker,
                    "status": record.get("status", "audited"),
                    "invalid": record.get("invalid_variants", []),
                    "retention": record.get("retention_checks", []),
                    "retention_count": len(record.get("retention_task_ids", [])),
                    "retained_score": mean(x["score"] for x in base.values())
                    if base
                    else None,
                    "retained_count": len(base),
                }
            )
        if label == "decision" and record.get("kind") == "admit":
            candidate_id, base_id = record["candidate_id"], record["parent_id"]
            changes = changed_files(
                snapshots[base_id]["files"], snapshots[candidate_id]["files"]
            )
            base = evaluations.get(base_id, {})
            rounds.append(
                {
                    "step": record["step"],
                    "source": name,
                    "baseline_id": base_id,
                    "candidate_id": candidate_id,
                    "selected_id": candidate_id if record["improved_best"] else best_id,
                    "selected_paths": [x["path"] for x in changes]
                    if record["improved_best"]
                    else [],
                    "previous_best_score": best_score,
                    "changes": changes,
                    "variants": [
                        variant(base_id, [], evaluations, base, name),
                        variant(
                            candidate_id,
                            [x["path"] for x in changes],
                            evaluations,
                            base,
                            name,
                            record["score"],
                        ),
                    ],
                    "worker": previous_worker,
                    "status": "admitted",
                    "improved_best": record["improved_best"],
                    "invalid": [],
                    "retention": [],
                }
            )
            if not record["improved_best"] and best_id not in {base_id, candidate_id}:
                historical = variant(
                    best_id,
                    [
                        row["path"]
                        for row in changed_files(
                            snapshots[base_id]["files"], snapshots[best_id]["files"]
                        )
                    ],
                    evaluations,
                    base,
                    name,
                    best_score,
                )
                historical["label"] = "历史最好版本"
                rounds[-1]["variants"].append(historical)
            if record["improved_best"]:
                best_id, best_score = candidate_id, record["score"]
        if label == "decision" and record.get("kind") == "decline":
            base_id = (
                initial_id
                if method == "meta_harness"
                else baseline_record.get("snapshot_id")
            )
            selected_id = best_id if method == "meta_harness" else base_id
            retained = evaluations.get(selected_id, {})
            rounds.append(
                {
                    "step": record["step"],
                    "source": name,
                    "status": "declined",
                    "problem": record.get("problem"),
                    "baseline_id": base_id,
                    "candidate_id": None,
                    "selected_id": selected_id,
                    "retained_score": best_score
                    if method == "meta_harness"
                    else mean(x["score"] for x in retained.values())
                    if retained
                    else None,
                    "retained_count": len(retained),
                    "selected_paths": [],
                    "changes": [],
                    "variants": [],
                    "worker": previous_worker,
                    "invalid": [],
                    "retention": [],
                }
            )
    promote = next((d for _, d in reversed(records) if d.get("kind") == "promote"), {})
    final_id = result.get("snapshot_id", promote.get("candidate_id"))
    return {
        "name": "Meta-Harness" if method == "meta_harness" else "CA-RSI",
        "method": method,
        "run": directory.name,
        "source": str(directory.relative_to(ROOT))
        if directory.is_relative_to(ROOT)
        else str(directory),
        "seed": config["benchmark"].get("seed"),
        "model": config.get("agent", {}).get("llm", {}).get("model_name", "未记录"),
        "worker_model": config["algorithm"]
        .get("worker", {})
        .get("llm", {})
        .get("model_name", "未记录"),
        "planned_rounds": config["algorithm"].get("max_steps"),
        "metric": metric,
        "max_regressions": config["algorithm"].get("max_task_regressions", 0),
        "rounds": rounds,
        "initial_id": initial_id,
        "final_id": final_id,
        "final_changes": changed_files(
            snapshots[initial_id]["files"], snapshots[final_id]["files"]
        )
        if final_id in snapshots and initial_id in snapshots
        else [],
        "test_score": result.get("test_score"),
        "test_tasks": result.get("test_tasks"),
        "status": result.get("status", "incomplete"),
    }


def registry_path(output):
    return output.with_name(output.stem + "-runs.json")


def default_runs(output=DEFAULT_OUTPUT):
    registry = registry_path(output)
    if registry.exists():
        candidates = [ROOT / name for name in read(registry)["runs"]]
    else:
        # Bootstrap from the published main studies, excluding debugging attempts.
        candidates = [
            ROOT / read(ROOT / "results" / name)["run_dir"]
            for name in ("meta-harness-baseline.json", "deepseek-unlimited-study.json")
            if (ROOT / "results" / name).exists()
        ]
    return list(
        dict.fromkeys(
            path.resolve() for path in candidates if (path / "result.json").exists()
        )
    )


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(text)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def export(directories, output):
    if not directories:
        raise ValueError("No runs found; pass --run with a native experiment directory")
    data = {"runs": [load_run(path.resolve()) for path in directories]}
    notes_path = ROOT / "results/evolution-annotations.json"
    notes = read(notes_path) if notes_path.exists() else {}
    for run in data["runs"]:
        for round_ in run["rounds"]:
            for change in round_["changes"]:
                change["summary"] = notes.get(round_["candidate_id"], {}).get(
                    change["path"], ""
                )
        run["diagram"] = svg_diagram(run)
        diagram_path = output.with_name(output.stem + "-" + run["run"] + ".svg")
        atomic_write(diagram_path, run["diagram"])
    payload = json.dumps(
        data, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).replace("<", "\\u003c")
    template = TEMPLATE.read_text(encoding="utf-8")
    fragment = template.replace("__EVOLUTION_DATA__", payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(output.with_name(output.stem + "-inline.html"), fragment)
    atomic_write(
        output,
        SHELL.read_text(encoding="utf-8").replace("__EVOLUTION_FRAGMENT__", fragment),
    )
    atomic_write(
        output.with_suffix(".json"), json.dumps(data, ensure_ascii=False, indent=2)
    )
    return data


def refresh(directory, output=DEFAULT_OUTPUT):
    """Register one completed experiment and rebuild its page plus the history."""
    directory, output = directory.resolve(), output.resolve()
    if read(directory / "result.json").get("status") != "finished":
        raise ValueError("Only a finished experiment can update the automatic history")
    lock_path = ROOT / "results/private/evolution-export.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock_path)):
        directories = [p for p in default_runs(output) if p != directory] + [directory]
        export([directory], directory / "evolution-view.html")
        export(directories, output)
        atomic_write(
            registry_path(output),
            json.dumps(
                {
                    "runs": [
                        str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)
                        for p in directories
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ),
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        type=Path,
        action="append",
        help="Native run directory; repeat for comparison",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--register",
        action="store_true",
        help="Register completed --run directories and update the full history",
    )
    args = parser.parse_args()
    if args.register:
        if not args.run:
            parser.error("--register requires --run")
        for directory in args.run:
            refresh(directory, args.output)
    else:
        export(args.run or default_runs(args.output), args.output)
    print(args.output)


if __name__ == "__main__":
    main()
