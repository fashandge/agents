"""Realtime streaming for Claude through direct ``claude -p``.

The default Claude streaming path. Runs ``claude -p --output-format stream-json
--verbose`` (``-p`` refuses stream-json without ``--verbose``) and reuses
``claude_pty_wrapper_streaming``'s JSONL formatter, since both emit the same
Claude stream-json events. Unlike the wrapper, ``claude -p`` reaps its own MCP
servers on exit, so a finished run cannot hang on orphaned MCP children.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from agents import claude_pty_wrapper_streaming
from agents import coding_agents_cli


DEFAULT_MODEL = claude_pty_wrapper_streaming.DEFAULT_MODEL
DEFAULT_TIMEOUT_SECONDS = claude_pty_wrapper_streaming.DEFAULT_TIMEOUT_SECONDS
ClaudePtyStreamEvent = claude_pty_wrapper_streaming.ClaudePtyStreamEvent


def build_claude_print_command(
    *,
    model: str = DEFAULT_MODEL,
    auto_approve: bool = True,
    effort: str | None = None,
    disable_mcp: bool = False,
) -> list[str]:
    """Build a ``claude -p`` stream-json command. The prompt goes via stdin."""
    command = [
        coding_agents_cli.find_claude_bin(),
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
    ]
    command.extend(
        coding_agents_cli.build_claude_flag_args(
            effort=effort, auto_approve=auto_approve, disable_mcp=disable_mcp
        )
    )
    return command


def stream_claude_print(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    auto_approve: bool = True,
    timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
    cwd: Path | str | None = None,
    effort: str | None = None,
    disable_mcp: bool = False,
) -> Iterator[ClaudePtyStreamEvent]:
    """Stream readable Claude output from ``claude -p``.

    Yields the same ``status``/``log``/``error``/``done`` events as
    ``claude_pty_wrapper_streaming.stream_claude_pty``.
    """
    try:
        command = build_claude_print_command(
            model=model,
            auto_approve=auto_approve,
            effort=effort,
            disable_mcp=disable_mcp,
        )
    except Exception as exc:
        yield ClaudePtyStreamEvent(kind="error", payload={"message": f"claude unavailable: {exc}"})
        return

    yield ClaudePtyStreamEvent(kind="status", payload={"message": "Starting Claude via claude -p."})
    yield from claude_pty_wrapper_streaming._stream_command(
        command, prompt=prompt, timeout=timeout, cwd=cwd, process_label="claude -p"
    )
