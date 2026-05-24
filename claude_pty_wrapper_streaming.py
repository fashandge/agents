"""Realtime streaming helpers for Claude through claude-pty-wrapper.

This module intentionally does not invoke ``claude -p``. It uses
``claude-pty-wrapper -p --output-format stream-json`` and converts the wrapper's
JSONL output into readable progress events while preserving a clean final
assistant output string.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from agents import coding_agents_cli
from agents import env as agents_env


DEFAULT_MODEL = "sonnet"
DEFAULT_TIMEOUT_SECONDS = 1200
DEFAULT_PTY_WRAPPER_NO_TIMEOUT_SECONDS = 7 * 24 * 60 * 60
TOOL_RENDER_LIMIT = 200


@dataclass(frozen=True)
class ClaudePtyStreamEvent:
    """One event from a streaming Claude PTY wrapper run."""

    kind: str
    payload: dict[str, object]


@dataclass(frozen=True)
class ClaudePtyStreamResult:
    """Final result from a streaming Claude PTY wrapper run."""

    output: str
    returncode: int
    stderr: str
    session_id: str | None = None


@dataclass(frozen=True)
class _FormattedStreamLine:
    display_text: str
    assistant_text: str
    result_text: str | None = None
    session_id: str | None = None


def build_claude_pty_wrapper_command(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    auto_approve: bool = True,
    timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
    cwd: Path | str | None = None,
) -> list[str]:
    """Build a claude-pty-wrapper command for stream-json output.

    The prompt is accepted for API compatibility but is sent through stdin by
    ``_stream_command`` so multiline and long prompts do not become argv.
    """
    wrapper_bin = coding_agents_cli.find_claude_pty_wrapper_bin()
    claude_bin = coding_agents_cli.find_claude_bin()
    wrapper_timeout = timeout or DEFAULT_PTY_WRAPPER_NO_TIMEOUT_SECONDS

    command = [
        wrapper_bin,
        "-p",
        "--output-format",
        "stream-json",
        "--claude-bin",
        claude_bin,
        "--model",
        model,
        "--timeout",
        str(wrapper_timeout),
    ]
    if cwd is not None:
        command.extend(["--cwd", str(cwd)])
    if auto_approve:
        command.extend(["--permission-mode", "bypassPermissions"])
    return command


def stream_claude_pty(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    auto_approve: bool = True,
    timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
    cwd: Path | str | None = None,
) -> Iterator[ClaudePtyStreamEvent]:
    """Stream readable Claude output from claude-pty-wrapper.

    Yields ``status``, ``log``, ``error``, and ``done`` events. The ``done``
    payload contains a ``ClaudePtyStreamResult``.
    """
    try:
        command = build_claude_pty_wrapper_command(
            prompt,
            model=model,
            auto_approve=auto_approve,
            timeout=timeout,
            cwd=cwd,
        )
    except Exception as exc:
        yield ClaudePtyStreamEvent(
            kind="error",
            payload={"message": f"claude-pty-wrapper unavailable: {exc}"},
        )
        return

    yield ClaudePtyStreamEvent(
        kind="status",
        payload={"message": "Starting Claude via claude-pty-wrapper."},
    )
    yield from _stream_command(command, prompt=prompt, timeout=timeout, cwd=cwd)


def run_claude_pty_streaming(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    auto_approve: bool = True,
    timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
    cwd: Path | str | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> ClaudePtyStreamResult:
    """Run Claude through claude-pty-wrapper and print readable output live."""
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    final_result: ClaudePtyStreamResult | None = None

    for event in stream_claude_pty(
        prompt,
        model=model,
        auto_approve=auto_approve,
        timeout=timeout,
        cwd=cwd,
    ):
        if event.kind == "log":
            text = str(event.payload.get("text", ""))
            stream_name = str(event.payload.get("stream", "claude"))
            target = stderr if stream_name == "stderr" else stdout
            target.write(text)
            target.flush()
        elif event.kind == "error":
            message = str(event.payload.get("message", "Claude PTY wrapper stream failed"))
            raise RuntimeError(message)
        elif event.kind == "done":
            result = event.payload.get("result")
            if isinstance(result, ClaudePtyStreamResult):
                final_result = result

    if final_result is None:
        raise RuntimeError("Claude PTY wrapper stream ended without a result")
    return final_result


def _stream_command(
    command: list[str],
    *,
    prompt: str,
    timeout: float | None,
    cwd: Path | str | None,
) -> Iterator[ClaudePtyStreamEvent]:
    process: subprocess.Popen[str] | None = None
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    result_text: str | None = None
    session_id: str | None = None
    assistant_displayed = False
    started_at = time.monotonic()

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(cwd) if cwd is not None else None,
            env=agents_env.build_env(),
        )
        output_queue: queue.Queue[tuple[str, str] | None] = queue.Queue()
        threads = []
        if process.stdin is not None:
            thread = threading.Thread(
                target=_write_prompt_to_stdin,
                args=(process.stdin, prompt),
                daemon=True,
            )
            thread.start()
        for stream, stream_name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            if stream is None:
                continue
            thread = threading.Thread(
                target=_enqueue_stream,
                args=(stream, stream_name, output_queue),
                daemon=True,
            )
            thread.start()
            threads.append(thread)

        finished_streams = 0
        while finished_streams < len(threads):
            if timeout is not None and time.monotonic() - started_at > timeout + 10:
                _terminate_process(process)
                yield ClaudePtyStreamEvent(
                    kind="error",
                    payload={"message": f"Claude timed out after {timeout} seconds"},
                )
                return

            try:
                item = output_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if item is None:
                finished_streams += 1
                continue

            stream_name, text = item
            if stream_name == "stderr":
                stderr_chunks.append(text)
                yield ClaudePtyStreamEvent(
                    kind="log",
                    payload={"stream": "stderr", "text": text},
                )
                continue

            formatted = _format_stream_json_line(text)
            if formatted.session_id:
                session_id = formatted.session_id
            if formatted.assistant_text:
                stdout_chunks.append(formatted.assistant_text)
            if formatted.result_text is not None:
                result_text = formatted.result_text
            if formatted.display_text:
                if formatted.assistant_text:
                    assistant_displayed = True
                elif formatted.result_text is not None and assistant_displayed:
                    continue
                yield ClaudePtyStreamEvent(
                    kind="log",
                    payload={"stream": "claude", "text": formatted.display_text},
                )

        returncode = process.wait()
        if returncode != 0:
            yield ClaudePtyStreamEvent(
                kind="error",
                payload={
                    "message": f"claude-pty-wrapper failed with exit code {returncode}",
                    "stderr": "".join(stderr_chunks),
                },
            )
            return

        output = result_text if result_text is not None else "".join(stdout_chunks)
        yield ClaudePtyStreamEvent(
            kind="done",
            payload={
                "result": ClaudePtyStreamResult(
                    output=output,
                    returncode=returncode,
                    stderr="".join(stderr_chunks),
                    session_id=session_id,
                )
            },
        )
    except GeneratorExit:
        if process is not None:
            _terminate_process(process)
        raise
    except Exception as exc:
        if process is not None:
            _terminate_process(process)
        yield ClaudePtyStreamEvent(kind="error", payload={"message": str(exc)})
    finally:
        if process is not None and process.poll() is None:
            _terminate_process(process)


def _enqueue_stream(
    stream: TextIO,
    stream_name: str,
    output_queue: queue.Queue[tuple[str, str] | None],
) -> None:
    try:
        for line in iter(stream.readline, ""):
            if not line:
                break
            output_queue.put((stream_name, line))
    finally:
        output_queue.put(None)


def _write_prompt_to_stdin(stdin: TextIO, prompt: str) -> None:
    try:
        stdin.write(prompt)
        stdin.flush()
    except BrokenPipeError:
        pass
    finally:
        stdin.close()


def _format_stream_json_line(line: str) -> _FormattedStreamLine:
    stripped = line.strip()
    if not stripped:
        return _FormattedStreamLine("", "")
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        return _FormattedStreamLine(line, "")
    if not isinstance(event, dict):
        return _FormattedStreamLine(line, "")

    event_type = event.get("type")
    session_id = _string_value(event.get("session_id"))

    if event_type == "system":
        return _FormattedStreamLine("", "", session_id=session_id)

    if event_type == "assistant":
        display_parts: list[str] = []
        assistant_parts: list[str] = []
        message = event.get("message")
        if isinstance(message, dict):
            for block in _content_blocks(message.get("content")):
                block_type = block.get("type")
                if block_type == "text":
                    text = _string_value(block.get("text"))
                    if text:
                        display_parts.append(text)
                        assistant_parts.append(text)
                elif block_type == "tool_use":
                    name = _string_value(block.get("name")) or "tool"
                    display_parts.append(f"[tool: {name} {_render_tool_input(block.get('input'))}]\n")
        return _FormattedStreamLine(
            display_text="".join(display_parts),
            assistant_text="".join(assistant_parts),
            session_id=session_id,
        )

    if event_type == "user":
        message = event.get("message")
        if isinstance(message, dict):
            for block in _content_blocks(message.get("content")):
                if block.get("type") == "tool_result":
                    summary = _tool_result_summary(block.get("content"))
                    if summary:
                        return _FormattedStreamLine(
                            display_text=f"[tool result: {summary}]\n",
                            assistant_text="",
                            session_id=session_id,
                        )
        return _FormattedStreamLine("", "", session_id=session_id)

    if event_type == "result":
        result = _string_value(event.get("result")) or ""
        display = f"{result}\n" if result else ""
        return _FormattedStreamLine(
            display_text=display,
            assistant_text="",
            result_text=result,
            session_id=session_id,
        )

    return _FormattedStreamLine(line, "", session_id=session_id)


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _render_tool_input(value: Any) -> str:
    try:
        rendered = json.dumps(value if value is not None else {}, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(value)
    return _truncate(rendered)


def _tool_result_summary(content: Any) -> str:
    if isinstance(content, str):
        return _truncate(" ".join(content.split()))
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = _string_value(item.get("text"))
                if text:
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return _truncate(" ".join(" ".join(parts).split()))
    return ""


def _truncate(text: str, limit: int = TOOL_RENDER_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _terminate_process(process: subprocess.Popen[str], *, timeout: float = 2.0) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)
