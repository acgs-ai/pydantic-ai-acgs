# pydantic-ai-acgs

[![CI](https://github.com/acgs-ai/pydantic-ai-acgs/actions/workflows/ci.yml/badge.svg)](https://github.com/acgs-ai/pydantic-ai-acgs/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pydantic-ai-acgs.svg)](https://pypi.org/project/pydantic-ai-acgs/)
[![Python](https://img.shields.io/pypi/pyversions/pydantic-ai-acgs.svg)](https://pypi.org/project/pydantic-ai-acgs/)

`pydantic-ai-acgs` checks every [Pydantic AI](https://ai.pydantic.dev) function tool call against
[ACGS](https://pypi.org/project/acgs-lite/) constitution rules before the tool runs. It is a Pydantic AI
[capability](https://pydantic.dev/docs/ai/capabilities/overview/): you add `ACGSCapability()` to an agent's
`capabilities`, and its [tool hooks](https://pydantic.dev/docs/ai/hooks/#tool-execution-hooks) see every call.
Agents and tools stay unchanged.

- Calls that match no blocking rule run unchanged.
- Calls that match a blocking rule do not run. The capability raises `ToolFailed`, so the model gets a failed tool
  result with the reason and can continue. Denials do not use up the tool's retries.
- Calls that only match review rules become [deferred tool calls](https://pydantic.dev/docs/ai/deferred-tools/#human-in-the-loop-tool-approval)
  (`ApprovalRequired`) and run only once approved, through Pydantic AI's own human-in-the-loop flow. They can also
  be denied outright.
- Any error while evaluating a call denies it (fail-closed).

The package is a thin adapter over the public `acgs-lite` API.

## Installation

```bash
pip install pydantic-ai-acgs
```

Requires Python 3.10 or later, `pydantic-ai-slim>=2.16.0,<3` and `acgs-lite>=3.0.1,<5`. (Pydantic AI 2.55 and later
need Python 3.11.) Install the model provider you use as usual, for example `pip install "pydantic-ai-slim[openai]"`.

acgs-lite 4 is a major release. Its production profile of `GovernedCallable` requires an Ed25519
receipt signer, governed payloads are JSON-only, and `GovernedAgent` no longer passes per-call keyword arguments to
rule conditions (see the [acgs-lite changelog](https://github.com/acgs-ai/acgs-lite/blob/main/CHANGELOG.md)). This
adapter works with both majors, but a fresh environment installs the newest version allowed. To stay on 3.x, also
require `"acgs-lite<4"`, for example `pip install pydantic-ai-acgs "acgs-lite<4"`.

## Usage

```python
from pydantic_ai import Agent, DeferredToolRequests

from pydantic_ai_acgs import ACGSCapability

agent = Agent(
    "test",  # your model, for example "openai:gpt-5.2" or "anthropic:claude-sonnet-4-5"
    capabilities=[ACGSCapability()],
    output_type=[str, DeferredToolRequests],  # lets calls that need review pause the run
)
```

The default constitution has review rules too (`ACGS-002`, `ACGS-005`). If a call needs review and the agent has
neither `DeferredToolRequests` in its `output_type` nor a `HandleDeferredToolCalls` handler, Pydantic AI stops the
run with a `UserError` (the tool does not run). Use `on_review="deny"` for agents that cannot pause. Agent
[specs](https://pydantic.dev/docs/ai/agent-spec/) work too:

```python
from pydantic_ai import Agent
from pydantic_ai.agent.spec import AgentSpec, CapabilitySpec

from pydantic_ai_acgs import ACGSCapability

spec = AgentSpec(model="test", capabilities=[CapabilitySpec(name="ACGSCapability", arguments={"on_review": "deny"})])
agent = Agent.from_spec(spec, custom_capability_types=[ACGSCapability])
```

In YAML that is `- ACGSCapability: {on_review: deny}` under `capabilities:`, loaded with
`Agent.from_file("agent.yaml", custom_capability_types=[ACGSCapability])`. Specs use the default constitution;
pass `constitution=` in Python to use your own rules.

## Quickstart

`examples/quickstart.py` runs offline. A `FunctionModel` stands in for an LLM and prints each tool result it
receives:

```python
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
```

```text
send_email (failed): Tool call 'send_email' was not executed. Blocked by ACGS rule ACGS-006 (Agents must not expose sensitive data in responses).
get_weather (success): Sunny in Paris
paused: issue_refund: Human review required by ACGS rule REFUND-REVIEW (Refunds need human approval).
issue_refund (success): refunded 20.0 for A-17
```

## How a tool call is evaluated

1. The tool name and the validated arguments are rendered as text by `format_tool_call`:

   ```python
   from pydantic_ai_acgs import format_tool_call

   print(format_tool_call("update_config", {"service": "billing", "change": "disable\nlogging", "dryRun": False}))
   ```

   ```text
   tool: update_config
   arguments:
   change: disable logging
   dryRun: false
   service: billing
   normalized: update config | dry run
   ```

   Each argument leaf is one `path: value` line (nested values use `a.b[0].c` paths), so both argument names and
   values are visible to rules. Whitespace inside values is collapsed so phrases split across lines still match.
   Identifiers written in `snake_case`, `kebab-case` or `camelCase` (the tool name, argument names, and
   single-token string values) are repeated as plain words on the `normalized:` line, so the keyword
   `skip audit` also matches a tool called `skip_audit`. Nothing is truncated.
   Pass `tool_call_formatter=` to use a different representation.

2. The text is validated with `acgs_lite.GovernanceEngine.validate` against the constitution
   (`Constitution.default()` unless you pass one).

3. The result decides what happens to the call:

   | ACGS outcome | Result |
   |---|---|
   | No blocking rule matched (warn-only rules may match) | The tool runs |
   | Only `require_human_review` / `escalate_to_senior` rules matched | Review (see below) |
   | Any `block`, `block_and_notify` or `halt_and_alert` rule matched | Denied |
   | A rule ID not found in the constitution, or an invalid result without a rule | Denied |
   | Any exception while reading or evaluating the tool call | Denied (fail-closed) |

   A matched blocking rule is never downgraded to review because a review rule matched as well.
   The denial reason then lists only the blocking rules after "Blocked by" and names the matched review
   rules separately.

A denied call raises `ToolFailed("Tool call '<name>' was not executed. <reason>")`. Pydantic AI does not run the
tool and sends the message to the model as a tool result with `outcome='failed'`, without counting it against the
tool's `max_retries`. The reason names the matched rules but never repeats argument values. Exceptions raised by the
tool itself are handled by Pydantic AI as usual.

`ACGSCapability.evaluate(tool_name, args)` returns the underlying `GovernanceDecision`
(`verdict`, `reason`, `rule_ids`, `warning_rule_ids`, `error`) without running anything.

### When the check runs

Each call is checked at two documented hook points:

- `after_tool_validate`, as soon as the model's arguments are validated. A call that matches a blocking rule is
  denied here, before anyone is asked to approve it, including for tools registered with `requires_approval=True`.
- `before_tool_execute`, just before the tool runs, with the arguments the tool will receive. This check denies
  blocked calls again and defers calls that need review.

The capability declares `CapabilityOrdering(position="innermost")`, so its `before_tool_execute` runs after the
`before_tool_execute` hooks and inside the `wrap_tool_execute` hooks of the other capabilities, and sees arguments
they changed.

## Review

| `on_review` | Behaviour |
|---|---|
| `"defer"` (default) | Raises `ApprovalRequired`. The call becomes a deferred tool call: the run ends with a `DeferredToolRequests` output (add it to `output_type`), or a [`HandleDeferredToolCalls`](https://pydantic.dev/docs/ai/deferred-tools/#resolving-deferred-calls-with-a-handler) capability decides it inline. The tool runs only once the call is approved. |
| `"deny"` | The call is denied with the review reason. Use it for unattended runs. |

The request carries the ACGS decision in `DeferredToolRequests.metadata[tool_call_id]["acgs"]`
(`verdict`, `reason`, `rule_ids`), so an approval UI can show why the call was paused. Approve or deny with
`DeferredToolResults(approvals={tool_call_id: True | False | ToolApproved(...) | ToolDenied(...)})` on the next
`agent.run(message_history=..., deferred_tool_results=...)`, or answer inline:

```python
from typing import Any

from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults, RunContext
from pydantic_ai.capabilities import HandleDeferredToolCalls

from pydantic_ai_acgs import ACGSCapability


async def reviewer(ctx: RunContext[Any], requests: DeferredToolRequests) -> DeferredToolResults:
    for call in requests.approvals:
        print(f"review {call.tool_name}: {requests.metadata.get(call.tool_call_id, {}).get('acgs')}")
    return requests.build_results(approve_all=True)  # or ask a person


agent = Agent("test", capabilities=[ACGSCapability(), HandleDeferredToolCalls(handler=reviewer)])
unattended = Agent("test", capabilities=[ACGSCapability(on_review="deny")])
```

A resumed call is checked again. A blocking rule still wins: if the approver supplies `ToolApproved(override_args=...)`
the new arguments are evaluated like any call, and if the rules changed while the call was waiting and now block
it, the call is denied even though it was approved. An approved call that only matches review rules runs.

An approval answers one tool call ID. If the tool also requires approval on its own (`requires_approval=True`,
`ApprovalRequiredToolset`), one approval answers both. When `requires_approval=True` is what paused the call,
Pydantic AI collects it before the execute hooks run, so the request has no `acgs` metadata; the ACGS check then
runs when the approved call resumes.

## Custom constitutions

```python
from acgs_lite import Constitution, Rule, Severity, ViolationAction

from pydantic_ai_acgs import ACGSCapability

constitution = Constitution.from_rules(
    [
        Rule(
            id="NO-DESTRUCTIVE-SQL",
            text="No destructive SQL",
            severity=Severity.CRITICAL,
            keywords=["drop table", "truncate table"],
        ),
        Rule(
            id="REFUND-REVIEW",
            text="Refunds need approval",
            severity=Severity.HIGH,
            keywords=["refund"],
            workflow_action=ViolationAction.REQUIRE_HUMAN_REVIEW,
        ),
    ],
    name="payments",
)
capability = ACGSCapability(constitution=constitution, agent_id="payments-app")
```

`Constitution.from_yaml("rules.yaml")` works as well. To enforce two constitutions, add two capabilities; a call
runs only if both allow it.

## Options

| Argument | Default | Meaning |
|---|---|---|
| `constitution` | `Constitution.default()` | Rules to enforce |
| `agent_id` | `"pydantic-ai-agent"` | Agent identifier recorded by ACGS |
| `on_review` | `"defer"` | `"defer"` or `"deny"` |
| `tool_call_formatter` | `format_tool_call` | `(tool_name, args) -> str` used as the evaluated text |

## Limitations

- Only function tool calls are checked, including tools from toolsets (tested with `FunctionToolset`). Tools of
  MCP servers that the agent connects to take the same path but are not tested here. Pydantic AI does not run tool
  hooks for output tools (structured output) or for native tools that the model provider executes itself
  (`WebSearchTool`, `CodeExecutionTool`, `MCPServerTool` and similar), so those are not checked. User messages,
  model requests and responses, and tool results are not checked either. External tools (`kind='external'`, or calls
  a tool defers with `CallDeferred`) are executed by your application, not by the agent, so check those where you
  run them.
- Another capability that also declares `position="innermost"` and is listed after `ACGSCapability` can still
  change arguments after the check. `after_tool_validate` hooks of other capabilities run after this
  capability's validation check, but the execute-stage check sees their changes.
- Tested with `run`, `run_sync`, `run_stream`, `run_stream_events` and `iter`, parallel tool calls in one model
  turn, deferred approval and resume, `HandleDeferredToolCalls`, `requires_approval` tools, `FunctionToolset` and
  `ApprovalRequiredToolset`, other capabilities and `Hooks`, and agent specs, against pydantic-ai-slim 2.16.0 and
  the latest release. Durable execution (Temporal, DBOS, Prefect) and realtime sessions are not tested.
- `Constitution.default()` focuses on governance integrity (self-approval, skipping audits, privilege escalation)
  and on secrets and personal data (`password`, `api_key`, `sk-...` and `ghp_...` tokens, US SSN
  patterns). It does **not** cover destructive operations such as `rm -rf` or `DROP TABLE`. Write rules for the
  tools you expose. `tests/test_default_constitution.py` lists what the default rules do and do not catch.
- Keyword rules in acgs-lite 3.0.1 match literal, case-insensitive substrings. They do not understand negation
  ("never share the password" matches `password`) and do not treat `-`, `_` and spaces as equivalent
  (`api-key` does not match the keyword `api_key`).
- Because argument names are part of the evaluated text, a tool with a parameter named `password` is denied under
  the default constitution whenever it is called.

## Development

```bash
uv sync --group dev        # or use hatch: hatch run prepare
hatch run format           # ruff format + ruff check --fix
hatch run lint             # ruff check + ruff format --check
hatch run typecheck        # mypy --strict
hatch run cov              # pytest with a 100% branch-coverage gate, network disabled
hatch run min:cov          # the same tests against pydantic-ai-slim 2.16.0 and acgs-lite 3.0.1
pre-commit install         # format, lint, type check and tests on commit; commitizen on commit messages
```

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/); `cz bump` updates the version
and the changelog.

## License

Apache-2.0. See [LICENSE](LICENSE).
