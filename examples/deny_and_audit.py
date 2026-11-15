"""Run one allowed and one denied Pydantic AI tool call offline, then print the decision trail."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Any

# Disable telemetry and the framework banner before importing Pydantic AI.
os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["PYDANTIC_AI_NO_BANNER"] = "1"

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pydantic_ai_acgs import ACGSCapability, GovernanceDecision


class AuditedCapability(ACGSCapability):
    """Collect decisions through the public API without retaining argument values."""

    def __init__(self) -> None:
        super().__init__()
        self.decisions: list[GovernanceDecision] = []

    def evaluate(self, tool_name: str, arguments: Any) -> GovernanceDecision:
        decision = super().evaluate(tool_name, arguments)
        self.decisions.append(decision)
        return decision


def record_note(body: str) -> str:
    """Record a demo note by printing it locally."""
    print(f"Executed record_note: {body}")
    return "Note recorded."


def main() -> None:
    calls = [
        ToolCallPart("record_note", {"body": body}, tool_call_id=f"note-{index}")
        for index, body in enumerate(["The meeting starts at noon.", "The admin password is demo-secret."])
    ]

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        turn = sum(isinstance(message, ModelResponse) for message in messages)
        return ModelResponse(parts=[calls[turn]] if turn < len(calls) else [TextPart("Done.")])

    gate = AuditedCapability()
    agent = Agent(FunctionModel(respond), tools=[record_note], capabilities=[gate])
    agent.run_sync("Record the two notes.")
    print("Audit trail (application decisions):")
    for decision in gate.decisions:
        print(json.dumps(asdict(decision), sort_keys=True))


if __name__ == "__main__":
    main()
