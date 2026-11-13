"""Unit tests for Antigravity harness telemetry, formatting, and chunk streaming."""

from __future__ import annotations

from typing import Any

import pytest
from google.antigravity import types

from app.adapters.outbound.antigravity_harness import (
    extract_intent_summary,
    format_tool_args,
    stream_chat_response,
)
from app.application.pipeline import _summarize_gate_failure


class _FakeResponse:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    @property
    async def chunks(self):
        for item in self._items:
            yield item


def test_format_tool_args_handles_files_commands_searches_and_custom_tools() -> None:
    wt = "/tmp/wt/task-1"
    assert (
        format_tool_args(
            "view_file",
            {"AbsolutePath": f"{wt}/packages/zlib/package.manifest", "StartLine": 1, "EndLine": 40},
            worktree_path=wt,
        )
        == "packages/zlib/package.manifest:1-40"
    )
    assert (
        format_tool_args(
            "run_command",
            {"CommandLine": "git   status\n  --short", "SafeToAutoRun": True},
        )
        == "git status --short"
    )
    assert (
        format_tool_args(
            "search_directory",
            {"Query": "PKG_VERSION", "SearchPath": f"{wt}/packages"},
            worktree_path=wt,
        )
        == '"PKG_VERSION" in packages'
    )
    assert (
        format_tool_args(
            "just_pkg_audit",
            {"package": "zlib", "strict": True},
        )
        == "package=zlib, strict=True"
    )
    assert format_tool_args("just_pkg_list", {}) == ""


def test_extract_intent_summary_prefers_clean_heading_or_first_sentence() -> None:
    assert (
        extract_intent_summary("**Inspecting zlib package manifest**\n\nLet me check lines 1-20.")
        == "Inspecting zlib package manifest"
    )
    assert (
        extract_intent_summary(
            "Checking which package versions lag upstream. Next I will check patches and build logs."
        )
        == "Checking which package versions lag upstream."
    )
    assert extract_intent_summary("   \n\n  ") == ""


@pytest.mark.asyncio
async def test_stream_chat_response_emits_intent_tool_args_and_failures() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    fake_resp = _FakeResponse(
        [
            types.Thought(step_index=0, text="**Checking zlib "),
            types.Thought(step_index=0, text="manifest version**\nExtra verbose chain of thought..."),
            types.ToolCall(
                id="call-1",
                name="view_file",
                args={"path": "/repo/.wintermute/wt/packages/zlib/package.manifest"},
            ),
            types.ToolResult(id="call-1", name="view_file", result="ok", error=None),
            types.Text(step_index=1, text="Running package audit now to check updates."),
            types.ToolCall(
                id="call-2",
                name="run_command",
                args={"CommandLine": "just pkg::audit zlib"},
            ),
            types.ToolResult(
                id="call-2",
                name="run_command",
                result=None,
                error="Command exited with code 2: recipe not found",
            ),
            types.Text(step_index=2, text="Done! "),
            types.Text(step_index=2, text="zlib can be updated to 1.3.1."),
        ]
    )

    full_text = await stream_chat_response(
        fake_resp,
        event_callback=lambda kind, payload: events.append((kind, payload)),
        role="coder",
        worktree_path="/repo/.wintermute/wt",
    )

    assert full_text == "Running package audit now to check updates.Done! zlib can be updated to 1.3.1."
    assert [e[0] for e in events] == [
        "AGENT_INTENT",
        "TOOL_CALL_STARTED",
        "AGENT_INTENT",
        "TOOL_CALL_STARTED",
        "TOOL_CALL_FAILED",
    ]
    assert events[0][1]["kind"] == "thought"
    assert events[0][1]["summary"] == "Checking zlib manifest version"
    assert events[1][1]["tool"] == "view_file"
    assert events[1][1]["args"] == "packages/zlib/package.manifest"
    assert events[2][1]["kind"] == "plan"
    assert events[2][1]["summary"] == "Running package audit now to check updates."
    assert events[3][1]["tool"] == "run_command"
    assert events[3][1]["args"] == "just pkg::audit zlib"
    assert events[4][1]["tool"] == "run_command"
    assert events[4][1]["args"] == "just pkg::audit zlib"
    assert "recipe not found" in events[4][1]["error"]


def test_summarize_gate_failure_extracts_meaningful_error_lines() -> None:
    report = (
        "$ just pkg::verify\n"
        "Checking repository tree...\n"
        "Validating package manifests...\n"
        "error: packages/zlib/package.manifest: missing field 'source'\n"
        "error: pkg::verify exited with status 1"
    )
    summary = _summarize_gate_failure(report)
    assert "$ just pkg::verify" not in summary
    assert "missing field 'source'" in summary
    assert "status 1" in summary

