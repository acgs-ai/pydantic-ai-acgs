# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
import pydantic_ai_acgs


def test_public_api() -> None:
    assert sorted(pydantic_ai_acgs.__all__) == sorted(
        ["METADATA_KEY", "ACGSCapability", "GovernanceDecision", "ToolCallFormatter", "Verdict", "format_tool_call"]
    )
    for name in pydantic_ai_acgs.__all__:
        assert getattr(pydantic_ai_acgs, name) is not None
