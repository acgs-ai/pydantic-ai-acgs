# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""ACGS governance for Pydantic AI tool calls, as a capability."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from acgs_lite import Constitution
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering
from pydantic_ai.exceptions import ApprovalRequired, ToolFailed
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition

from pydantic_ai_acgs._governance import GovernanceDecision, ToolCallFormatter, ToolCallGovernor, Verdict

logger = logging.getLogger(__name__)

METADATA_KEY = "acgs"
"""The key of the ACGS entry in ``DeferredToolRequests.metadata[tool_call_id]`` for a review."""

_EVALUATION_FAILED = "ACGS governance could not evaluate this tool call, so it was denied."


@dataclass(init=False)
class ACGSCapability(AbstractCapability[Any]):
    """Check every Pydantic AI function tool call against an ACGS constitution before the tool runs.

    Add it to an agent with ``Agent(..., capabilities=[ACGSCapability()])``. Each call is checked
    twice: once its arguments are validated (``after_tool_validate``), and again just before the
    tool runs (``before_tool_execute``), with the arguments the tool receives.

    - Allowed calls run unchanged.
    - Denied calls raise ``ToolFailed``: the tool does not run, and the model gets a failed tool
      result with the reason (``outcome='failed'``) without using up the tool's retries.
    - Calls that only match review rules raise ``ApprovalRequired`` by default (``on_review="defer"``),
      so they become deferred tool calls: the run ends with ``DeferredToolRequests`` or a
      ``HandleDeferredToolCalls`` handler decides them, and the tool runs only once approved.
      ``on_review="deny"`` denies them instead.

    A blocking rule always wins: a call that matches one is denied even after approval, and before
    a tool's own ``requires_approval`` asks anyone. Any error while evaluating a call denies it
    instead of failing the run.

    Example:
        >>> from pydantic_ai_acgs import ACGSCapability
        >>> capability = ACGSCapability()
        >>> capability.evaluate("send_email", {"to": "a@example.com", "body": "The password is hunter2"}).verdict
        <Verdict.DENY: 'deny'>
    """

    constitution: Constitution = field(repr=False)
    """The constitution this capability enforces."""
    agent_id: str
    on_review: Literal["defer", "deny"]
    tool_call_formatter: ToolCallFormatter | None = field(repr=False)
    _governor: ToolCallGovernor = field(init=False, repr=False, compare=False)

    def __init__(  # noqa: PLR0913  # keyword-only options, mirrored by the dataclass fields
        self,
        *,
        constitution: Constitution | None = None,
        agent_id: str = "pydantic-ai-agent",
        on_review: Literal["defer", "deny"] = "defer",
        tool_call_formatter: ToolCallFormatter | None = None,
        id: str | None = None,  # noqa: A002  # the AbstractCapability field name
        description: str | None = None,
        defer_loading: bool = False,
    ) -> None:
        """Create the capability.

        Args:
            constitution: The ACGS constitution to enforce. Defaults to
                ``acgs_lite.Constitution.default()``.
            agent_id: Agent identifier recorded by ACGS for each validation.
            on_review: What to do when only review rules match. ``"defer"`` raises
                ``ApprovalRequired`` and runs the call only once it is approved. ``"deny"`` denies it.
            tool_call_formatter: Optional function ``(tool_name, args) -> str`` that replaces
                ``format_tool_call``.
            id: Optional capability ID (an ``AbstractCapability`` field).
            description: Optional capability description (an ``AbstractCapability`` field).
            defer_loading: Must be ``False``. A capability loaded on demand only runs its hooks once the model
                loads it, so ACGS checks would be skipped until then.

        Raises:
            ValueError: If ``on_review`` is invalid or ``defer_loading`` is true.
        """
        if on_review not in ("defer", "deny"):
            msg = f"on_review must be 'defer' or 'deny', got {on_review!r}"
            raise ValueError(msg)
        if defer_loading:
            msg = "ACGSCapability cannot be loaded on demand: its checks must run for every tool call."
            raise ValueError(msg)
        self.id = id
        self.description = description
        self.defer_loading = False
        self._governor = ToolCallGovernor(constitution, agent_id=agent_id, tool_call_formatter=tool_call_formatter)
        # The fields mirror the constructor arguments, so `dataclasses.replace` builds an equivalent capability.
        self.constitution = self._governor.constitution
        self.agent_id = agent_id
        self.on_review = on_review
        self.tool_call_formatter = tool_call_formatter

    def evaluate(self, tool_name: str, arguments: Any) -> GovernanceDecision:
        """Evaluate a tool call without a running agent. Never raises.

        Args:
            tool_name: Name of the tool.
            arguments: The tool arguments.

        Returns:
            The governance decision.
        """
        return self._governor.evaluate(tool_name, arguments)

    def get_ordering(self) -> CapabilityOrdering:
        """Run innermost, so the check sees the arguments other capabilities hand to the tool."""
        return CapabilityOrdering(position="innermost")

    async def after_tool_validate(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Deny a blocked call as soon as its arguments are validated, before any approval is requested."""
        self._check(ctx, call, args, review=False)
        return args

    async def before_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Check the call with the arguments the tool will receive, and defer it if it needs review."""
        self._check(ctx, call, args, review=True)
        return args

    def _check(self, ctx: RunContext[Any], call: ToolCallPart, args: dict[str, Any], *, review: bool) -> None:
        tool_name = "unknown"
        try:
            tool_name = call.tool_name
            if not isinstance(tool_name, str) or not tool_name:
                msg = f"Tool name must be a non-empty string, got {tool_name!r}."
                raise TypeError(msg)
            decision = self.evaluate(tool_name, args)
            approved = ctx.tool_call_approved
        except Exception:  # fail-closed: Pydantic AI would otherwise abort the whole run
            logger.warning("ACGS check failed for tool %r; denying the call (fail-closed).", tool_name, exc_info=True)
            raise _denied(str(tool_name), _EVALUATION_FAILED) from None
        if decision.verdict is Verdict.ALLOW:
            return
        if decision.verdict is Verdict.DENY:
            raise _denied(tool_name, decision.reason)
        if not review:
            return
        if self.on_review == "deny":
            raise _denied(tool_name, f"Human review did not approve the call. {decision.reason}")
        if approved:
            logger.info("ACGS review of tool '%s' was approved.", tool_name)
            return
        raise ApprovalRequired(metadata={METADATA_KEY: _metadata(decision)})


def _denied(tool_name: str, reason: str) -> ToolFailed:
    return ToolFailed(f"Tool call '{tool_name}' was not executed. {reason}")


def _metadata(decision: GovernanceDecision) -> dict[str, Any]:
    return {"verdict": decision.verdict.value, "reason": decision.reason, "rule_ids": list(decision.rule_ids)}
