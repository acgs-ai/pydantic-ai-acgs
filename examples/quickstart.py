"""Gate Pydantic AI tool calls with ACGS. Runs offline: a FunctionModel stands in for an LLM."""

from acgs_lite import Constitution, Rule, Severity, ViolationAction
from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pydantic_ai_acgs import ACGSCapability


def scripted(*calls: ToolCallPart) -> FunctionModel:
    """Make one tool call per turn, then answer "Done." Prints each tool result the model receives."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        if isinstance(last, ModelRequest):
            for part in last.parts:
                if isinstance(part, ToolReturnPart):
                    print(f"{part.tool_name} ({part.outcome}): {part.content}")
        turn = sum(isinstance(m, ModelResponse) for m in messages)
        return ModelResponse(parts=[calls[turn]] if turn < len(calls) else [TextPart("Done.")])

    return FunctionModel(respond)


def send_email(to: str, body: str) -> str:
    """Send an email."""
    return f"sent to {to}"


def get_weather(city: str) -> str:
    """Get the weather for a city."""
    return f"Sunny in {city}"


def issue_refund(order_id: str, amount: float) -> str:
    """Refund an order."""
    return f"refunded {amount} for {order_id}"


# 1. The default constitution blocks the email that leaks a password; the weather lookup runs.
model = scripted(
    ToolCallPart("send_email", {"to": "ops@example.com", "body": "The admin password is hunter2"}),
    ToolCallPart("get_weather", {"city": "Paris"}),
)
agent = Agent(model, tools=[send_email, get_weather], capabilities=[ACGSCapability()])
agent.run_sync("Hi")

# 2. A review rule: refunds become deferred tool calls and run once approved.
refunds = Constitution.from_rules(
    [
        Rule(
            id="REFUND-REVIEW",
            text="Refunds need human approval",
            severity=Severity.HIGH,
            keywords=["refund"],
            workflow_action=ViolationAction.REQUIRE_HUMAN_REVIEW,
        )
    ],
    name="refunds",
)
model = scripted(ToolCallPart("issue_refund", {"order_id": "A-17", "amount": 20.0}, tool_call_id="refund-1"))
agent = Agent(
    model,
    tools=[issue_refund],
    capabilities=[ACGSCapability(constitution=refunds)],
    output_type=[str, DeferredToolRequests],
)
result = agent.run_sync("Refund order A-17")
if isinstance(result.output, DeferredToolRequests):
    for call in result.output.approvals:
        print(f"paused: {call.tool_name}: {result.output.metadata[call.tool_call_id]['acgs']['reason']}")

# A person approves the call; resume the run with their decision.
approvals = DeferredToolResults(approvals={"refund-1": True})
agent.run_sync(message_history=result.all_messages(), deferred_tool_results=approvals)
