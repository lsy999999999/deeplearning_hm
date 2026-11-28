"""Executable attribution must distinguish harmful rules, interactions and faults."""

import pytest

from flashrsi.agents.miniagent.runtime.rule_audit import Rule, audit_rules, run_rules


def trim(answer, evidence):
    return answer.strip()


def comma(answer, evidence):
    return answer.partition(",")[0]


def identity(answer, evidence):
    return answer


def boom(answer, evidence):
    raise RuntimeError("failed rule")


def suffix(answer, evidence):
    return answer + "x" if answer == "A" else answer


def consume(answer, evidence):
    return "wrong" if answer == "Ax" else answer


def case(cid="county", answer="Graham County, Kansas", expected=None):
    return {
        "id": cid,
        "answer": answer,
        "evidence": {},
        "expected": [answer] if expected is None else [expected],
    }


def test_harmful_rule_and_benign_rule_are_distinguished():
    rows = [case(), case("quote", " A ", "A")]
    report = audit_rules(
        [Rule("trim", trim), Rule("comma", comma), Rule("noop", identity)],
        rows,
        hypotheses=[
            {
                "case_id": "county",
                "disable": ["comma"],
                "predicted_output": "Graham County, Kansas",
                "claim": "truncates name",
            },
            {
                "case_id": "county",
                "disable": ["noop"],
                "predicted_output": "Graham County, Kansas",
                "claim": "no-op is harmful",
            },
            {
                "case_id": "unknown",
                "disable": ["comma"],
                "predicted_output": "x",
                "claim": "invalid",
            },
        ],
    )
    effects = {row["rule_id"]: row for row in report["effects"]}
    assert effects["comma"]["removal_gain"] == 0.5
    assert effects["trim"]["removal_gain"] == -0.5
    assert effects["noop"]["removal_gain"] == 0
    assert [x["status"] for x in report["hypotheses"]] == [
        "prediction_supported_on_frozen_input",
        "prediction_refuted_on_frozen_input",
        "insufficient_evidence",
    ]
    assert report["variants"][-1]["cases"][0]["events"][-1]["action"] == "no_op"


def test_pair_interaction_can_be_harmful_without_harmful_singletons():
    report = audit_rules(
        [Rule("produce", suffix), Rule("consume", consume)],
        [case(answer="A", expected="Ax"), case("other", "B")],
    )
    # Produce is helpful alone, but enables consume to corrupt that output.
    assert report["effects"][0]["alone_gain"] == 0.5
    assert report["interactions"][0]["difference_in_differences"] == -0.5


def test_invalid_subsets_are_not_scored_or_semantically_attributed():
    report = audit_rules(
        [Rule("bad", boom), Rule("dependent", identity, ("bad",))], [case()]
    )
    assert report["variants"][-1]["score"] is None
    assert all(x["removal_gain"] is None for x in report["effects"])
    assert (
        next(v for v in report["variants"] if v["enabled"] == ["dependent"])["valid"]
        is False
    )


def test_fixed_evidence_and_provenance_are_preserved():
    evidence = {"context": ["original"]}

    def mutate(answer, supplied):
        supplied["context"].append("modified")
        return answer

    events = []
    result = run_rules(
        "A",
        evidence,
        [Rule("mutate", mutate)],
        emit=lambda kind, **kw: events.append((kind, kw)),
        provenance={"snapshot_id": "frozen", "task_id": "t1"},
    )
    assert evidence == {"context": ["original"]}
    assert result["events"][0]["evidence"] == evidence
    assert events[0][1]["provenance"]["snapshot_id"] == "frozen"
    assert len(events[0][1]["rule"]["source_sha256"]) == 64


def test_nondeterminism_is_invalid():
    values = iter(["a", "b"])

    def unstable(answer, evidence):
        return next(values)

    report = audit_rules([Rule("unstable", unstable)], [case()])
    assert report["variants"][-1]["score"] is None
    assert "Repeated replay differed" in report["variants"][-1]["cases"][0]["problem"]


def test_unknown_ids_and_excessive_budget_are_rejected():
    with pytest.raises(ValueError):
        run_rules("a", {}, [Rule("x", identity)], enabled=["missing"])
    with pytest.raises(ValueError):
        audit_rules([Rule(str(i), identity) for i in range(5)], [case()])
    with pytest.raises(ValueError):
        audit_rules([Rule("x", identity)], [case(), case()])


def test_correct_prediction_of_helpful_rule_is_not_reported_as_an_error():
    report = audit_rules(
        [Rule("trim", trim)],
        [case("space", " A ", "A")],
        hypotheses=[
            {
                "case_id": "space",
                "disable": ["trim"],
                "predicted_output": " A ",
                "claim": "trimming is useful",
            },
            None,
        ],
    )
    check = report["hypotheses"][0]
    assert check["status"] == "prediction_supported_on_frozen_input"
    assert check["removal_quality_effect"] == -1
    assert not check["error_attribution_supported"]
    assert report["hypotheses"][1]["status"] == "insufficient_evidence"


def test_native_runtime_preserves_rule_boundary_for_future_audit(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace

    import yaml

    from flashrsi.agents import MiniAgent
    from flashrsi.agents.miniagent.runtime import agent, llm

    root = tmp_path / "workspace"
    seed = MiniAgent({"llm": {"model_name": "unused"}}).snapshot
    for name, entry in seed.files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(entry.content)
    monkeypatch.syspath_prepend(str(root))
    config_path = root / "agent.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["middlewares"] = ["audited"]
    config_path.write_text(yaml.safe_dump(config))
    (root / "middleware").mkdir(exist_ok=True)
    (root / "middleware/audited.py").write_text(
        "from runtime.rule_audit import Rule\n"
        "def unquote(answer, evidence):\n    return answer.strip(chr(34))\n"
        'AUDIT_RULES = [Rule("unquote", unquote)]\n'
        "async def before_final(answer, ctx):\n"
        '    final = ctx.apply_answer_rules(answer, {"context":ctx.question}, AUDIT_RULES)\n'
        '    return {"action":"replace", "answer":final}\n'
    )

    async def chat(messages, **kwargs):
        return SimpleNamespace(
            id="call",
            usage=None,
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='"A"', tool_calls=None),
                    finish_reason="stop",
                )
            ],
        )

    monkeypatch.setattr(llm, "chat", chat)
    result = asyncio.run(
        agent.run(
            "Frozen context",
            workspace_dir=root,
            runs_root=tmp_path / "runs",
            max_turns=1,
        )
    )
    assert result["answer"] == "A"
    receipt = result["rule_replays"][0]
    assert receipt["answer"] == '"A"'
    assert receipt["evidence"] == {"context": "Frozen context"}
    assert receipt["events"][0]["after"] == "A"
    assert receipt["events"][0]["provenance"]["run_id"]
