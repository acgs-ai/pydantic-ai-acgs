# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""The README only shows code that runs, and the quickstart output it shows is the real output."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.fakes import in_own_thread

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
QUICKSTART = ROOT / "examples" / "quickstart.py"


def _blocks(language: str) -> list[str]:
    return [textwrap.dedent(block) for block in re.findall(rf"```{language}\n(.*?)```", README, re.DOTALL)]


def test_quickstart_output_matches_readme() -> None:
    env = {**os.environ, "OTEL_SDK_DISABLED": "true", "PYDANTIC_AI_NO_BANNER": "1"}
    completed = subprocess.run(  # noqa: S603
        [sys.executable, str(QUICKSTART)], capture_output=True, text=True, check=True, cwd=ROOT, env=env, timeout=300
    )
    [expected] = [block for block in _blocks("text") if block.startswith("send_email (failed):")]
    assert completed.stdout == expected


def test_readme_embeds_the_quickstart_verbatim() -> None:
    assert QUICKSTART.read_text(encoding="utf-8") in _blocks("python")


def test_every_readme_python_block_runs(capsys: pytest.CaptureFixture[str]) -> None:
    blocks = _blocks("python")
    assert len(blocks) == 6

    def run_blocks() -> None:
        for block in blocks:
            exec(compile(block, "<README>", "exec", dont_inherit=True), {"__name__": "readme"})  # noqa: S102

    in_own_thread(run_blocks)
    output = capsys.readouterr().out
    assert "normalized: update config | dry run" in output
    assert "paused: issue_refund: Human review required by ACGS rule REFUND-REVIEW" in output
