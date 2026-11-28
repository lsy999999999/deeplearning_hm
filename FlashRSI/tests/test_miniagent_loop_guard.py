"""Exercise runtime stopping with mocked model responses and real files."""

import asyncio
import json
from types import SimpleNamespace

from flashrsi.agents.miniagent.runtime import agent, llm
from flashrsi.agents.miniagent.runtime.loop_guard import ToolLoopGuard


def response(content="", *, tools=()):
    return SimpleNamespace(
        id="offline",
        usage=None,
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=list(tools)),
                finish_reason="tool_calls" if tools else "stop",
            )
        ],
    )


def call(number):
    return SimpleNamespace(
        id=f"call-{number}",
        type="function",
        function=SimpleNamespace(
            name="shell", arguments=json.dumps({"command": "check"})
        ),
    )


def test_repeated_checks_stop_before_30_turns_and_finalize_once(monkeypatch, tmp_path):
    requests, executions = [], []

    async def chat(messages, **kwargs):
        requests.append(kwargs)
        if kwargs["tool_choice"] == "none":
            return response("No files changed; repeated check stopped.")
        return response(tools=[call(len(requests))])

    async def dispatch(name, args, ctx):
        executions.append((name, args))
        return "same result"

    monkeypatch.setattr(llm, "chat", chat)
    monkeypatch.setattr(agent, "_dispatch", dispatch)
    (tmp_path / "work").mkdir()
    result = asyncio.run(
        agent.run(
            "Make a focused edit",
            model="offline",
            max_turns=30,
            runs_root=tmp_path / "traces",
            working_dir=tmp_path / "work",
            tool_repeat_limit=3,
        )
    )
    assert len(executions) == 3 and len(requests) == 4
    assert requests[-1]["tool_choice"] == "none"
    assert result["termination_reason"] == "tool_loop_guard"
    assert result["stop_reason"] == "completed"
    assert result["tool_loop_guard"]["repetitions"] == 3
    assert result["turns"] == 3


def test_file_edit_resets_repeat_count_but_new_tool_ids_do_not(tmp_path):
    path = tmp_path / "policy.md"
    path.write_text("before")
    guard = ToolLoopGuard(3, tmp_path)
    assert not guard.observe("check", {"x": 1}, "pass")
    assert not guard.observe("check", {"x": 1}, "pass")
    path.write_text("after with different length")
    assert not guard.observe("check", {"x": 1}, "pass")
    assert not guard.observe("check", {"x": 1}, "pass")
    assert guard.observe("check", {"x": 1}, "pass")


def test_tool_free_diagnosis_rejects_provider_tool_call(monkeypatch, tmp_path):
    requests = []

    async def chat(messages, **kwargs):
        requests.append(kwargs)
        return response(tools=[call(1)])

    async def dispatch(*args):
        raise AssertionError("Diagnosis must never execute a tool")

    monkeypatch.setattr(llm, "chat", chat)
    monkeypatch.setattr(agent, "_dispatch", dispatch)
    result = asyncio.run(
        agent.run(
            "Return diagnosis JSON",
            model="offline",
            allow_tools=False,
            max_turns=1,
            runs_root=tmp_path,
        )
    )
    assert len(requests) == 1
    assert requests[0]["tools"] is None
    assert result["stop_reason"].startswith("llm_error: tools are disabled")


def test_oversized_input_stops_before_any_model_call(monkeypatch, tmp_path):
    async def chat(*args, **kwargs):
        raise AssertionError("Over-budget prompt must not reach the API")

    monkeypatch.setattr(llm, "chat", chat)
    result = asyncio.run(
        agent.run(
            "Very long evidence " * 100,
            model="offline",
            input_char_limit=100,
            runs_root=tmp_path,
        )
    )
    assert result["stop_reason"] == "llm_error: input character budget exceeded"
    assert not result["usage"]
