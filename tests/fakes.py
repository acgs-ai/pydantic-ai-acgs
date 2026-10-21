# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""A scripted Pydantic AI model, plus tools that record whether they ran."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from acgs_lite import Constitution, Rule, Severity, ViolationAction
from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

T = TypeVar("T")

Call = tuple[str, dict[str, Any]]

LEAK = {"to": "ops@example.com", "body": "The admin password is hunter2"}
REFUND = {"order_id": "A-17", "amount": 20.0}


@dataclass
class Script:
    """Answers each model request with the next scripted turn; ``"done"`` once the script runs out.

    A turn is a list of tool calls. Call IDs are ``c<turn>`` for the first call of a turn and
    ``c<turn>_<n>`` for the others. Works for both ``run`` and streamed runs.
    """

    turns: list[list[Call]]
    seen: list[list[ToolReturnPart]] = field(default_factory=list)

    def _respond(self, messages: list[ModelMessage]) -> list[ToolCallPart] | str:
        last = messages[-1]
        if isinstance(last, ModelRequest):
            self.seen.append([p for p in last.parts if isinstance(p, ToolReturnPart)])
        turn = sum(isinstance(m, ModelResponse) for m in messages)
        if turn >= len(self.turns):
            return "done"
        return [
            ToolCallPart(name, args, tool_call_id=f"c{turn}" if i == 0 else f"c{turn}_{i}")
            for i, (name, args) in enumerate(self.turns[turn])
        ]

    def function(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        reply = self._respond(messages)
        return ModelResponse(parts=[TextPart(reply)] if isinstance(reply, str) else list(reply))

    async def stream(self, messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        reply = self._respond(messages)
        if isinstance(reply, str):
            yield reply
            return
        for index, part in enumerate(reply):
            yield {
                index: DeltaToolCall(
                    name=part.tool_name, json_args=json.dumps(part.args), tool_call_id=part.tool_call_id
                )
            }

    def model(self) -> FunctionModel:
        return FunctionModel(self.function, stream_function=self.stream)

    def results(self) -> list[ToolReturnPart]:
        """Every tool result the model received, in order."""
        return [part for parts in self.seen for part in parts]


def build(
    capabilities: Sequence[AbstractCapability[Any]],
    *turns: list[Call],
    output_type: Any = (str, DeferredToolRequests),
    refund_requires_approval: bool = False,
) -> tuple[Agent[None, Any], Script, list[str]]:
    """An agent with ``send_email``, ``get_weather`` and ``issue_refund`` tools that record each run."""
    script = Script(list(turns))
    ran: list[str] = []
    agent: Agent[None, Any] = Agent(script.model(), capabilities=list(capabilities), output_type=list(output_type))

    @agent.tool_plain
    def send_email(to: str, body: str) -> str:
        ran.append("send_email")
        return f"sent to {to}"

    @agent.tool_plain
    async def get_weather(city: str) -> str:
        ran.append("get_weather")
        return f"Sunny in {city}"

    @agent.tool_plain(requires_approval=refund_requires_approval)
    def issue_refund(order_id: str, amount: float) -> str:
        ran.append("issue_refund")
        return f"refunded {amount} for {order_id}"

    return agent, script, ran


REFUND_REVIEW = Rule(
    id="REFUND-REVIEW",
    text="Refunds need human approval",
    severity=Severity.HIGH,
    keywords=["refund"],
    workflow_action=ViolationAction.REQUIRE_HUMAN_REVIEW,
)


def review_rules(*, with_defaults: bool = False) -> Constitution:
    """A constitution whose refund rule needs review, optionally alongside the default rules."""
    rules = [REFUND_REVIEW, *(Constitution.default().rules if with_defaults else [])]
    return Constitution.from_rules(rules, name="refunds")


def in_own_thread(function: Callable[[], T]) -> T:
    """Run synchronous agent code (``run_sync``) on a fresh thread with its own event loop, then close the loop.

    ``run_sync`` drives the calling thread's event loop, which belongs to the caller. A separate thread keeps that
    loop away from pytest-asyncio's loops and lets the test close it.
    """
    results: list[T] = []
    errors: list[BaseException] = []

    def target() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            results.append(function())
        except BaseException as error:  # noqa: BLE001  # re-raised in the calling thread
            errors.append(error)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return results[0]
