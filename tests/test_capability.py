# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""ACGSCapability with real Pydantic AI agents and a scripted FunctionModel."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from acgs_lite import Constitution
from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults, RunContext
from pydantic_ai.agent.spec import AgentSpec, CapabilitySpec
from pydantic_ai.capabilities import AbstractCapability, CombinedCapability, HandleDeferredToolCalls, Hooks
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ToolCallPart, ToolReturnPart
from pydantic_ai.tools import ToolApproved, ToolDefinition, ToolDenied
from pydantic_ai.toolsets import ApprovalRequiredToolset, FunctionToolset

from pydantic_ai_acgs import METADATA_KEY, ACGSCapability, GovernanceDecision, Verdict
from tests.fakes import LEAK, REFUND, Script, build, in_own_thread, review_rules

BLOCKED = (
    "Tool call 'send_email' was not executed. Blocked by ACGS rule ACGS-006 "
    "(Agents must not expose sensitive data in responses)."
)
REVIEW_REASON = "Human review required by ACGS rule REFUND-REVIEW (Refunds need human approval)."


# --- allow and deny ---------------------------------------------------------------------------


async def test_allowed_call_runs() -> None:
    agent, script, ran = build([ACGSCapability()], [("get_weather", {"city": "Paris"})])
    result = await agent.run("weather?")
    assert result.output == "done"
    assert ran == ["get_weather"]
    [part] = script.results()
    assert (part.content, part.outcome) == ("Sunny in Paris", "success")


async def test_denied_call_does_not_run_and_the_model_sees_why() -> None:
    agent, script, ran = build([ACGSCapability()], [("send_email", LEAK)])
    result = await agent.run("email it")
    assert result.output == "done"
    assert ran == []
    [part] = script.results()
    assert part.outcome == "failed"
    assert part.content == BLOCKED


async def test_parallel_calls_are_decided_one_by_one() -> None:
    agent, script, ran = build([ACGSCapability()], [("send_email", LEAK), ("get_weather", {"city": "Paris"})])
    await agent.run("both")
    assert ran == ["get_weather"]
    assert [(p.tool_name, p.outcome) for p in script.results()] == [
        ("send_email", "failed"),
        ("get_weather", "success"),
    ]


async def test_agent_keeps_going_after_a_denial() -> None:
    agent, _script, ran = build([ACGSCapability()], [("send_email", LEAK)], [("get_weather", {"city": "Oslo"})])
    assert (await agent.run("go")).output == "done"
    assert ran == ["get_weather"]


async def test_denials_do_not_use_up_tool_retries() -> None:
    """ToolFailed reports a failure without the retry budget, so repeated denials never abort the run."""
    agent, script, ran = build([ACGSCapability()], *[[("send_email", LEAK)]] * 5)
    assert (await agent.run("keep trying")).output == "done"
    assert [p.outcome for p in script.results()] == ["failed"] * 5
    assert ran == []


def test_run_sync() -> None:
    agent, _script, ran = build([ACGSCapability()], [("send_email", LEAK), ("get_weather", {"city": "Paris"})])
    assert in_own_thread(lambda: agent.run_sync("both")).output == "done"
    assert ran == ["get_weather"]


async def test_tool_errors_are_not_hidden() -> None:
    agent: Agent[None, str] = Agent(
        build([], [("explode", {})])[1].model(), capabilities=[ACGSCapability()], output_type=str
    )

    @agent.tool_plain
    def explode() -> str:
        msg = "boom"
        raise RuntimeError(msg)

    with pytest.raises(Exception) as info:  # noqa: PT011  # RuntimeError, or an ExceptionGroup holding it
        await agent.run("go")
    assert "boom" in _messages(info.value)


def _messages(error: BaseException) -> list[str]:
    """The messages of an exception and, for an exception group, of every exception inside it."""
    nested: tuple[BaseException, ...] = getattr(error, "exceptions", ())
    return [str(error), *(m for e in nested for m in _messages(e))]


# --- streaming --------------------------------------------------------------------------------


async def test_run_stream() -> None:
    agent, script, ran = build([ACGSCapability()], [("send_email", LEAK), ("get_weather", {"city": "Paris"})])
    async with agent.run_stream("both") as stream:
        assert await stream.get_output() == "done"
    assert ran == ["get_weather"]
    assert script.results()[0].content == BLOCKED


async def test_run_stream_events_reports_the_denial_and_the_review() -> None:
    from pydantic_ai import FunctionToolResultEvent
    from pydantic_ai.run import AgentRunResultEvent

    agent, _script, ran = build(
        [ACGSCapability(constitution=review_rules(with_defaults=True))],
        [("send_email", LEAK)],
        [("issue_refund", REFUND)],
    )
    async with agent.run_stream_events("go") as stream:
        events = [event async for event in stream]
    [denied] = [e for e in events if isinstance(e, FunctionToolResultEvent)]
    assert isinstance(denied.part, ToolReturnPart)
    assert denied.part.outcome == "failed"
    final = events[-1]
    assert isinstance(final, AgentRunResultEvent)
    assert isinstance(final.result.output, DeferredToolRequests)
    assert [c.tool_name for c in final.result.output.approvals] == ["issue_refund"]
    assert ran == []


async def test_iter() -> None:
    agent, _script, ran = build([ACGSCapability()], [("send_email", LEAK)])
    async with agent.iter("go") as run:
        async for _node in run:
            pass
    assert run.result is not None
    assert run.result.output == "done"
    assert ran == []


# --- review -----------------------------------------------------------------------------------


async def test_review_defers_the_call_with_acgs_metadata() -> None:
    agent, _script, ran = build([ACGSCapability(constitution=review_rules())], [("issue_refund", REFUND)])
    result = await agent.run("refund")
    requests = result.output
    assert isinstance(requests, DeferredToolRequests)
    assert [(c.tool_name, c.tool_call_id) for c in requests.approvals] == [("issue_refund", "c0")]
    assert requests.metadata == {
        "c0": {METADATA_KEY: {"verdict": "review", "reason": REVIEW_REASON, "rule_ids": ["REFUND-REVIEW"]}}
    }
    assert ran == []


async def test_approved_review_runs_the_call() -> None:
    agent, script, ran = build([ACGSCapability(constitution=review_rules())], [("issue_refund", REFUND)])
    first = await agent.run("refund")
    resumed = await agent.run(
        message_history=first.all_messages(), deferred_tool_results=DeferredToolResults(approvals={"c0": True})
    )
    assert resumed.output == "done"
    assert ran == ["issue_refund"]
    assert script.results()[-1].content == "refunded 20.0 for A-17"


async def test_rejected_review_does_not_run_the_call() -> None:
    agent, script, ran = build([ACGSCapability(constitution=review_rules())], [("issue_refund", REFUND)])
    first = await agent.run("refund")
    denial = ToolDenied("A reviewer rejected the refund.")
    await agent.run(
        message_history=first.all_messages(), deferred_tool_results=DeferredToolResults(approvals={"c0": denial})
    )
    assert ran == []
    assert (script.results()[-1].content, script.results()[-1].outcome) == ("A reviewer rejected the refund.", "denied")


async def test_block_outranks_review() -> None:
    """A call that matches a blocking and a review rule is denied without asking anyone."""
    agent, script, ran = build(
        [ACGSCapability(constitution=review_rules(with_defaults=True))],
        [("issue_refund", {"order_id": "the admin password is hunter2", "amount": 1.0})],
    )
    result = await agent.run("refund")
    assert result.output == "done"
    assert ran == []
    assert "Blocked by ACGS rule ACGS-006" in str(script.results()[0].content)


async def test_approval_with_override_args_is_checked_again() -> None:
    """Block still wins after approval: the approver's override_args are evaluated like any call."""
    agent, script, ran = build(
        [ACGSCapability(constitution=review_rules(with_defaults=True))], [("issue_refund", REFUND)]
    )
    first = await agent.run("refund")
    override = ToolApproved(override_args={"order_id": "the admin password is hunter2", "amount": 1.0})
    await agent.run(
        message_history=first.all_messages(), deferred_tool_results=DeferredToolResults(approvals={"c0": override})
    )
    assert ran == []
    assert script.results()[-1].outcome == "failed"


async def test_approved_call_is_denied_when_the_rules_changed() -> None:
    agent, _, _ = build([ACGSCapability(constitution=review_rules())], [("issue_refund", REFUND)])
    first = await agent.run("refund")
    stricter = Constitution.from_rules(
        [r.model_copy(update={"workflow_action": None}) for r in review_rules().rules], name="strict"
    )
    agent2, script2, ran2 = build([ACGSCapability(constitution=stricter)], [("issue_refund", REFUND)])
    await agent2.run(
        message_history=first.all_messages(), deferred_tool_results=DeferredToolResults(approvals={"c0": True})
    )
    assert ran2 == []
    assert script2.results()[-1].outcome == "failed"


async def test_on_review_deny() -> None:
    agent, script, ran = build(
        [ACGSCapability(constitution=review_rules(), on_review="deny")], [("issue_refund", REFUND)]
    )
    result = await agent.run("refund")
    assert result.output == "done"
    assert ran == []
    assert script.results()[0].content == (
        f"Tool call 'issue_refund' was not executed. Human review did not approve the call. {REVIEW_REASON}"
    )


async def test_inline_approval_with_handle_deferred_tool_calls() -> None:
    seen: list[dict[str, Any]] = []

    async def reviewer(ctx: RunContext[Any], requests: DeferredToolRequests) -> DeferredToolResults:
        seen.append(requests.metadata)
        return requests.build_results(approve_all=True)

    agent, _script, ran = build(
        [ACGSCapability(constitution=review_rules()), HandleDeferredToolCalls(handler=reviewer)],
        [("issue_refund", REFUND)],
        output_type=(str,),
    )
    assert (await agent.run("refund")).output == "done"
    assert ran == ["issue_refund"]
    assert seen == [
        {"c0": {METADATA_KEY: {"verdict": "review", "reason": REVIEW_REASON, "rule_ids": ["REFUND-REVIEW"]}}}
    ]


async def test_inline_rejection_with_handle_deferred_tool_calls() -> None:
    async def reviewer(ctx: RunContext[Any], requests: DeferredToolRequests) -> DeferredToolResults:
        return DeferredToolResults(
            approvals={call.tool_call_id: ToolDenied("Rejected.") for call in requests.approvals}
        )

    agent, script, ran = build(
        [ACGSCapability(constitution=review_rules()), HandleDeferredToolCalls(handler=reviewer)],
        [("issue_refund", REFUND)],
        output_type=(str,),
    )
    assert (await agent.run("refund")).output == "done"
    assert ran == []
    assert script.results()[0].outcome == "denied"


async def test_review_without_a_way_to_approve_fails_closed() -> None:
    """With no DeferredToolRequests output and no handler, Pydantic AI stops the run; the tool never runs."""
    agent, _, ran = build([ACGSCapability(constitution=review_rules())], [("issue_refund", REFUND)], output_type=(str,))
    with pytest.raises(UserError, match="DeferredToolRequests"):
        await agent.run("refund")
    assert ran == []


# --- tools with their own approval ------------------------------------------------------------


async def test_blocked_call_is_denied_before_the_tools_own_approval_is_requested() -> None:
    agent, script, ran = build(
        [ACGSCapability(constitution=review_rules(with_defaults=True))],
        [("issue_refund", {"order_id": "the admin password is hunter2", "amount": 1.0})],
        refund_requires_approval=True,
    )
    result = await agent.run("refund")
    assert result.output == "done"
    assert ran == []
    assert script.results()[0].outcome == "failed"


async def test_tool_approval_is_preserved_for_allowed_calls() -> None:
    agent, _, ran = build([ACGSCapability()], [("issue_refund", REFUND)], refund_requires_approval=True)
    result = await agent.run("refund")
    assert isinstance(result.output, DeferredToolRequests)
    assert [c.tool_name for c in result.output.approvals] == ["issue_refund"]
    assert ran == []


async def test_one_approval_answers_both_the_tool_and_acgs() -> None:
    agent, _, ran = build(
        [ACGSCapability(constitution=review_rules())], [("issue_refund", REFUND)], refund_requires_approval=True
    )
    first = await agent.run("refund")
    assert isinstance(first.output, DeferredToolRequests)
    assert [c.tool_call_id for c in first.output.approvals] == ["c0"]
    await agent.run(
        message_history=first.all_messages(), deferred_tool_results=DeferredToolResults(approvals={"c0": True})
    )
    assert ran == ["issue_refund"]


async def test_toolset_tools_are_checked() -> None:
    ran: list[str] = []
    toolset: FunctionToolset[None] = FunctionToolset()

    @toolset.tool_plain
    def send_email(to: str, body: str) -> str:
        ran.append("send_email")
        return "sent"

    script = Script([[("send_email", LEAK)]])
    agent: Agent[None, str] = Agent(script.model(), toolsets=[toolset], capabilities=[ACGSCapability()])
    await agent.run("email")
    assert ran == []
    assert script.results()[0].content == BLOCKED


async def test_approval_required_toolset_and_acgs_review_share_one_approval() -> None:
    agent, _, ran = build([ACGSCapability(constitution=review_rules())], [("issue_refund", REFUND)])
    gated: Agent[None, Any] = Agent(
        agent.model,
        toolsets=[ApprovalRequiredToolset(FunctionToolset([_refund(ran)]))],
        capabilities=[ACGSCapability(constitution=review_rules())],
        output_type=[str, DeferredToolRequests],
    )
    first = await gated.run("refund")
    assert isinstance(first.output, DeferredToolRequests)
    assert [c.tool_call_id for c in first.output.approvals] == ["c0"]
    await gated.run(
        message_history=first.all_messages(), deferred_tool_results=DeferredToolResults(approvals={"c0": True})
    )
    assert ran == ["issue_refund"]


def _refund(ran: list[str]) -> Callable[[str, float], str]:
    def issue_refund(order_id: str, amount: float) -> str:
        ran.append("issue_refund")
        return "refunded"

    return issue_refund


# --- ordering and composition -----------------------------------------------------------------


@dataclass
class Rewrite(AbstractCapability[Any]):
    """Rewrites send_email arguments in before_tool_execute."""

    body: str

    async def before_tool_execute(
        self, ctx: RunContext[Any], *, call: ToolCallPart, tool_def: ToolDefinition, args: dict[str, Any]
    ) -> dict[str, Any]:
        return {**args, "body": self.body} if call.tool_name == "send_email" else args


async def test_runs_innermost_so_it_checks_arguments_other_capabilities_rewrote() -> None:
    """Listed first, ACGS still runs after the rewriting capability and sees the new arguments."""
    agent, script, ran = build(
        [ACGSCapability(), Rewrite("The admin password is hunter2")],
        [("send_email", {"to": "ops@example.com", "body": "hello"})],
    )
    await agent.run("email")
    assert ran == []
    assert script.results()[0].content == BLOCKED


async def test_model_arguments_are_denied_before_another_capability_rewrites_them() -> None:
    agent, _, ran = build([Rewrite("hello"), ACGSCapability()], [("send_email", LEAK)])
    await agent.run("email")
    # The model asked to leak; after_tool_validate sees the model's arguments and denies first.
    assert ran == []


async def test_ordering_is_innermost() -> None:
    combined = CombinedCapability([ACGSCapability(), Hooks()])
    assert isinstance(combined.capabilities[-1], ACGSCapability)


async def test_works_alongside_hooks() -> None:
    calls: list[str] = []
    hooks = Hooks()

    @hooks.on.before_tool_execute
    async def log(
        ctx: RunContext[Any], *, call: ToolCallPart, tool_def: ToolDefinition, args: dict[str, Any]
    ) -> dict[str, Any]:
        calls.append(call.tool_name)
        return args

    agent, _, ran = build([hooks, ACGSCapability()], [("send_email", LEAK), ("get_weather", {"city": "Paris"})])
    await agent.run("go")
    assert ran == ["get_weather"]
    assert calls == ["get_weather"]


async def test_two_capabilities_must_both_allow() -> None:
    agent, _, ran = build(
        [ACGSCapability(), ACGSCapability(constitution=review_rules(), on_review="deny")],
        [("issue_refund", REFUND), ("get_weather", {"city": "Paris"})],
    )
    await agent.run("go")
    assert ran == ["get_weather"]


async def test_from_spec() -> None:
    spec = AgentSpec(
        model="test",
        capabilities=[CapabilitySpec(name="ACGSCapability", arguments={"agent_id": "spec-agent", "on_review": "deny"})],
    )
    agent: Agent[None, str] = Agent.from_spec(spec, custom_capability_types=[ACGSCapability])
    [capability] = [c for c in agent.root_capability.capabilities if isinstance(c, ACGSCapability)]
    assert (capability.agent_id, capability.on_review) == ("spec-agent", "deny")


# --- fail-closed ------------------------------------------------------------------------------


async def test_formatter_error_denies(caplog: pytest.LogCaptureFixture) -> None:
    def broken(tool_name: str, args: Any) -> str:
        msg = "formatter broke"
        raise RuntimeError(msg)

    agent, script, ran = build([ACGSCapability(tool_call_formatter=broken)], [("get_weather", {"city": "Paris"})])
    with caplog.at_level(logging.WARNING):
        await agent.run("go")
    assert ran == []
    assert script.results()[0].outcome == "failed"
    assert "was not executed" in str(script.results()[0].content)


async def test_evaluation_error_denies(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    capability = ACGSCapability()

    def explode(tool_name: str, arguments: Any) -> GovernanceDecision:
        msg = "engine down"
        raise RuntimeError(msg)

    monkeypatch.setattr(capability, "evaluate", explode)
    agent, script, ran = build([capability], [("get_weather", {"city": "Paris"})])
    with caplog.at_level(logging.WARNING, logger="pydantic_ai_acgs.capability"):
        await agent.run("go")
    assert ran == []
    assert script.results()[0].content == (
        "Tool call 'get_weather' was not executed. ACGS governance could not evaluate this tool call, so it was denied."
    )
    assert "fail-closed" in caplog.text


async def test_invalid_tool_name_denies() -> None:
    from pydantic_ai.exceptions import ToolFailed

    capability = ACGSCapability()
    ctx: Any = type("Ctx", (), {"tool_call_approved": False})()
    call = ToolCallPart("", {}, tool_call_id="x")
    with pytest.raises(ToolFailed, match="could not evaluate"):
        await capability.before_tool_execute(ctx, call=call, tool_def=ToolDefinition(name=""), args={})


# --- construction -----------------------------------------------------------------------------


def test_defaults() -> None:
    capability = ACGSCapability()
    assert capability.agent_id == "pydantic-ai-agent"
    assert capability.on_review == "defer"
    assert capability.constitution.rules
    assert ACGSCapability.get_serialization_name() == "ACGSCapability"


def test_custom_constitution_and_evaluate() -> None:
    rules = review_rules()
    capability = ACGSCapability(constitution=rules, agent_id="a")
    assert capability.constitution is rules
    decision = capability.evaluate("issue_refund", REFUND)
    assert decision.verdict is Verdict.REVIEW
    assert decision.rule_ids == ("REFUND-REVIEW",)


def test_dataclasses_replace_keeps_the_configuration() -> None:
    rules = review_rules()
    capability = ACGSCapability(constitution=rules, agent_id="a")
    copy = dataclasses.replace(capability, on_review="deny")
    assert (copy.constitution, copy.agent_id, copy.on_review) == (rules, "a", "deny")
    assert copy.evaluate("issue_refund", REFUND).verdict is Verdict.REVIEW
    assert "constitution" not in repr(copy)
    assert (
        repr(copy) == "ACGSCapability(id=None, description=None, defer_loading=False, agent_id='a', on_review='deny')"
    )


def test_cannot_be_loaded_on_demand() -> None:
    with pytest.raises(ValueError, match="cannot be loaded on demand"):
        ACGSCapability(defer_loading=True)
    spec = AgentSpec(
        model="test", capabilities=[CapabilitySpec(name="ACGSCapability", arguments={"defer_loading": True})]
    )
    with pytest.raises(ValueError, match="cannot be loaded on demand"):
        Agent.from_spec(spec, custom_capability_types=[ACGSCapability])


def test_capability_id_and_description() -> None:
    capability = ACGSCapability(id="acgs", description="Tool-call rules")
    assert (capability.id, capability.description, capability.defer_loading) == ("acgs", "Tool-call rules", False)


def test_invalid_on_review() -> None:
    with pytest.raises(ValueError, match="on_review must be 'defer' or 'deny'"):
        ACGSCapability(on_review="confirm")  # type: ignore[arg-type]
