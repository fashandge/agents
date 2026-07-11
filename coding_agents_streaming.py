"""Reusable streaming runners for Codex, Claude, and Gemini agent CLIs."""

from __future__ import annotations

import errno
import os
import pty
import queue
import select
import shlex
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from agents import claude_pty_wrapper_streaming
from agents import coding_agents_cli


DEFAULT_TIMEOUT_SECONDS = coding_agents_cli.DEFAULT_TIMEOUT_SECONDS
DEFAULT_GEMINI_HEARTBEAT_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class AgentStreamEvent:
    """One event from a streaming agent run."""

    kind: str
    payload: dict[str, object]


@dataclass(frozen=True)
class AgentStreamResult:
    """Final result from a successful streaming agent run."""

    output: str
    returncode: int
    stderr: str
    agent: coding_agents_cli.AgentType


def build_codex_command(config: coding_agents_cli.AgentConfig) -> list[str]:
    """Build the Codex CLI command for streaming."""
    if config.codex_output_schema is not None:
        raise ValueError("streaming Codex does not support codex_output_schema")
    return coding_agents_cli.build_codex_command(config)


def build_gemini_command(config: coding_agents_cli.AgentConfig) -> list[str]:
    """Build the agy (Gemini) command for streaming (prompt via stdin)."""
    return coding_agents_cli.build_gemini_command(
        auto_approve=config.auto_approve,
    )


def build_gemini_env(config: coding_agents_cli.AgentConfig) -> dict[str, str]:
    """Build the environment used for agy streaming."""
    return coding_agents_cli.build_gemini_env(str(config.model))


def stream_with_config(
    prompt: str,
    config: coding_agents_cli.AgentConfig,
    *,
    timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
) -> Iterator[AgentStreamEvent]:
    """Stream one configured agent run."""
    if config.agent == coding_agents_cli.AgentType.CODEX:
        yield from _stream_codex(prompt, config, timeout=timeout)
    elif config.agent == coding_agents_cli.AgentType.CLAUDE:
        yield from _stream_claude(prompt, config, timeout=timeout)
    elif config.agent == coding_agents_cli.AgentType.GEMINI:
        yield from _stream_gemini(prompt, config, timeout=timeout)
    else:
        yield AgentStreamEvent(
            kind="error",
            payload={"message": f"unknown agent type: {config.agent}"},
        )


def stream_with_config_and_fallback(
    prompt: str | Sequence[str],
    configs: Sequence[coding_agents_cli.AgentConfig] | None = None,
    *,
    timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
) -> Iterator[AgentStreamEvent]:
    """Stream configured agents in order, falling back on failure."""
    if configs is None:
        configs = coding_agents_cli.DEFAULT_AGENT_FALLBACK_ORDER
    if not configs:
        yield AgentStreamEvent(kind="error", payload={"message": "configs must not be empty"})
        return

    if isinstance(prompt, str):
        prompts: Sequence[str] = [prompt] * len(configs)
    else:
        prompts = prompt
        if len(prompts) != len(configs):
            yield AgentStreamEvent(
                kind="error",
                payload={
                    "message": (
                        f"prompt sequence length ({len(prompts)}) must match "
                        f"configs length ({len(configs)})"
                    )
                },
            )
            return

    errors: list[str] = []
    for index, (config, agent_prompt) in enumerate(zip(configs, prompts)):
        failed = False
        error_message = ""
        for event in stream_with_config(agent_prompt, config, timeout=timeout):
            if event.kind == "error":
                failed = True
                error_message = str(event.payload.get("message", ""))
                stderr = str(event.payload.get("stderr", ""))
                errors.append(
                    f"{config.agent.value}: {error_message or stderr or 'unknown error'}"
                )
                break
            yield event
            if event.kind == "done":
                return

        if not failed:
            errors.append(f"{config.agent.value}: stream ended without a result")
            error_message = "stream ended without a result"

        next_config = configs[index + 1] if index + 1 < len(configs) else None
        if next_config is not None:
            yield AgentStreamEvent(
                kind="fallback",
                payload={
                    "from_agent": config.agent.value,
                    "to_agent": next_config.agent.value,
                    "message": (
                        f"{config.agent.value} failed ({error_message}); "
                        f"falling back to {next_config.agent.value}."
                    )
                    if error_message
                    else (
                        f"{config.agent.value} failed; "
                        f"falling back to {next_config.agent.value}."
                    ),
                },
            )
            continue

        yield AgentStreamEvent(
            kind="error",
            payload={"message": "All agents failed:\n" + "\n".join(f"  - {e}" for e in errors)},
        )
        return


def _stream_codex(
    prompt: str,
    config: coding_agents_cli.AgentConfig,
    *,
    timeout: float | None,
) -> Iterator[AgentStreamEvent]:
    try:
        command = build_codex_command(config)
    except Exception as exc:
        yield AgentStreamEvent(kind="error", payload={"message": f"codex unavailable: {exc}"})
        return

    output_chunks: list[str] = []
    stderr_chunks: list[str] = []
    try:
        yield AgentStreamEvent(kind="status", payload={"message": "Streaming Codex output via pty."})
        for event in _stream_subprocess_with_pty(
            command,
            prompt,
            label="codex",
            cwd=_string_path(config.codex_working_dir),
            timeout=timeout,
        ):
            if event.kind == "log":
                _append_stream_text(event, output_chunks, stderr_chunks)
            if event.kind == "error":
                yield event
                return
            yield event
    except OSError:
        output_chunks.clear()
        stderr_chunks.clear()
        yield AgentStreamEvent(
            kind="status",
            payload={"message": "PTY unavailable; streaming Codex output via pipes."},
        )
        for event in _stream_subprocess_with_pipes(
            command,
            prompt,
            label="codex",
            cwd=_string_path(config.codex_working_dir),
            timeout=timeout,
        ):
            if event.kind == "log":
                _append_stream_text(event, output_chunks, stderr_chunks)
            if event.kind == "error":
                yield event
                return
            yield event
    except Exception as exc:
        yield AgentStreamEvent(kind="error", payload={"message": str(exc)})
        return

    yield _done_event(
        output="".join(output_chunks),
        stderr="".join(stderr_chunks),
        agent=config.agent,
    )


def _stream_claude(
    prompt: str,
    config: coding_agents_cli.AgentConfig,
    *,
    timeout: float | None,
) -> Iterator[AgentStreamEvent]:
    yield AgentStreamEvent(
        kind="status",
        payload={"message": "Streaming Claude output via claude-pty-wrapper."},
    )
    try:
        for event in claude_pty_wrapper_streaming.stream_claude_pty(
            prompt,
            model=str(config.model),
            auto_approve=config.auto_approve,
            timeout=timeout,
            cwd=config.codex_working_dir,
        ):
            if event.kind == "done":
                result = event.payload.get("result")
                output = getattr(result, "output", "")
                stderr = getattr(result, "stderr", "")
                returncode = getattr(result, "returncode", 0)
                yield AgentStreamEvent(
                    kind="done",
                    payload={
                        "result": AgentStreamResult(
                            output=output if isinstance(output, str) else "",
                            returncode=returncode if isinstance(returncode, int) else 0,
                            stderr=stderr if isinstance(stderr, str) else "",
                            agent=config.agent,
                        )
                    },
                )
                return
            yield AgentStreamEvent(kind=event.kind, payload=event.payload)
            if event.kind == "error":
                return
    except Exception as exc:
        yield AgentStreamEvent(kind="error", payload={"message": str(exc)})


def _stream_gemini(
    prompt: str,
    config: coding_agents_cli.AgentConfig,
    *,
    timeout: float | None,
) -> Iterator[AgentStreamEvent]:
    """Stream Gemini output. Uses a temp file for the prompt (agy 1.1.1+ no
    longer reads stdin) and does not write to the subprocess stdin."""
    try:
        gemini_bin = coding_agents_cli.find_gemini_bin()
    except Exception as exc:
        yield AgentStreamEvent(kind="error", payload={"message": f"gemini unavailable: {exc}"})
        return

    # Write prompt to a temp file to avoid ARG_MAX limits and agy's lack of
    # stdin support.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    ) as prompt_file:
        prompt_file.write(prompt)
        prompt_file_path = prompt_file.name

    try:
        skip_perms = "--dangerously-skip-permissions " if config.auto_approve else ""
        shell_cmd = (
            f"{shlex.quote(gemini_bin)} "
            f"{skip_perms}"
            f'--print "$(cat {shlex.quote(prompt_file_path)})"'
        )
        command = ["sh", "-c", shell_cmd]
        env = build_gemini_env(config)
        cwd = _string_path(config.codex_working_dir)

        output_chunks: list[str] = []
        stderr_chunks: list[str] = []
        try:
            yield AgentStreamEvent(kind="status", payload={"message": "Streaming Gemini output via pty."})
            for event in _stream_subprocess_with_pty(
                command,
                prompt="",  # not used; write_stdin=False
                label="gemini",
                cwd=cwd,
                env=env,
                write_stdin=False,
                heartbeat_interval=DEFAULT_GEMINI_HEARTBEAT_INTERVAL_SECONDS,
                timeout=timeout,
            ):
                if event.kind == "log":
                    _append_stream_text(event, output_chunks, stderr_chunks)
                if event.kind == "error":
                    yield event
                    return
                yield event
        except OSError:
            output_chunks.clear()
            stderr_chunks.clear()
            yield AgentStreamEvent(
                kind="status",
                payload={"message": "PTY unavailable; streaming Gemini output via pipes."},
            )
            for event in _stream_subprocess_with_pipes(
                command,
                prompt="",
                label="gemini",
                cwd=cwd,
                env=env,
                write_stdin=False,
                timeout=timeout,
            ):
                if event.kind == "log":
                    _append_stream_text(event, output_chunks, stderr_chunks)
                if event.kind == "error":
                    yield event
                    return
                yield event
        except Exception as exc:
            yield AgentStreamEvent(kind="error", payload={"message": str(exc)})
            return

        yield _done_event(
            output="".join(output_chunks),
            stderr="".join(stderr_chunks),
            agent=config.agent,
        )
    finally:
        os.unlink(prompt_file_path)


def _stream_subprocess_with_pty(
    command: list[str],
    prompt: str,
    *,
    label: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    write_stdin: bool = True,
    heartbeat_interval: float | None = None,
    timeout: float | None = None,
) -> Iterator[AgentStreamEvent]:
    master_fd: int | None = None
    slave_fd: int | None = None
    process: subprocess.Popen[str] | None = None
    started_at = time.monotonic()
    try:
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if write_stdin else subprocess.DEVNULL,
            stdout=slave_fd,
            stderr=slave_fd,
            text=True,
            bufsize=0,
            cwd=cwd,
            env=env,
        )
        os.close(slave_fd)
        slave_fd = None
        if write_stdin:
            assert process.stdin is not None
            process.stdin.write(prompt)
            process.stdin.close()

        last_output_at = started_at
        while True:
            if timeout is not None and time.monotonic() - started_at > timeout:
                _terminate_process(process)
                yield AgentStreamEvent(
                    kind="error",
                    payload={"message": f"{label} timed out after {timeout} seconds"},
                )
                return

            select_timeout = heartbeat_interval if heartbeat_interval and heartbeat_interval > 0 else 0.1
            ready, _, _ = select.select([master_fd], [], [], select_timeout)
            if not ready:
                if process.poll() is not None:
                    break
                if heartbeat_interval is not None and heartbeat_interval > 0:
                    elapsed = int(time.monotonic() - started_at)
                    idle = int(time.monotonic() - last_output_at)
                    yield AgentStreamEvent(
                        kind="status",
                        payload={"message": f"{label} working... {elapsed}s elapsed, idle {idle}s"},
                    )
                continue

            try:
                chunk = os.read(master_fd, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO and process.poll() is not None:
                    break
                raise
            if not chunk:
                break
            last_output_at = time.monotonic()
            yield AgentStreamEvent(
                kind="log",
                payload={"stream": "pty", "text": chunk.decode("utf-8", errors="replace")},
            )
        returncode = process.wait()
        if returncode != 0:
            yield AgentStreamEvent(
                kind="error",
                payload={"message": f"{label} exec failed with exit code {returncode}"},
            )
    except GeneratorExit:
        if process is not None:
            _terminate_process(process)
        raise
    finally:
        if slave_fd is not None:
            os.close(slave_fd)
        if master_fd is not None:
            os.close(master_fd)
        if process is not None and process.poll() is None:
            _terminate_process(process)


def _stream_subprocess_with_pipes(
    command: list[str],
    prompt: str,
    *,
    label: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    write_stdin: bool = True,
    timeout: float | None = None,
) -> Iterator[AgentStreamEvent]:
    process: subprocess.Popen[str] | None = None
    started_at = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if write_stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=cwd,
            env=env,
        )
        if write_stdin:
            assert process.stdin is not None
            process.stdin.write(prompt)
            process.stdin.close()

        output_queue: queue.Queue[tuple[str, str] | None] = queue.Queue()
        threads = []
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
            if timeout is not None and time.monotonic() - started_at > timeout:
                _terminate_process(process)
                yield AgentStreamEvent(
                    kind="error",
                    payload={"message": f"{label} timed out after {timeout} seconds"},
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
            yield AgentStreamEvent(kind="log", payload={"stream": stream_name, "text": text})

        returncode = process.wait()
        if returncode != 0:
            yield AgentStreamEvent(
                kind="error",
                payload={"message": f"{label} exec failed with exit code {returncode}"},
            )
    except GeneratorExit:
        if process is not None:
            _terminate_process(process)
        raise
    finally:
        if process is not None and process.poll() is None:
            _terminate_process(process)


def _enqueue_stream(stream, stream_name: str, output_queue: queue.Queue[tuple[str, str] | None]) -> None:
    try:
        for line in iter(stream.readline, ""):
            if not line:
                break
            output_queue.put((stream_name, line))
    finally:
        output_queue.put(None)


def _append_stream_text(
    event: AgentStreamEvent,
    output_chunks: list[str],
    stderr_chunks: list[str],
) -> None:
    text = str(event.payload.get("text", ""))
    stream_name = str(event.payload.get("stream", ""))
    if stream_name == "stderr":
        stderr_chunks.append(text)
    else:
        output_chunks.append(text)


def _done_event(
    *,
    output: str,
    stderr: str,
    agent: coding_agents_cli.AgentType,
) -> AgentStreamEvent:
    return AgentStreamEvent(
        kind="done",
        payload={
            "result": AgentStreamResult(
                output=output,
                returncode=0,
                stderr=stderr,
                agent=agent,
            )
        },
    )


def _string_path(path: Path | None) -> str | None:
    return str(path) if path is not None else None


def _terminate_process(process: subprocess.Popen[str], *, timeout: float = 2.0) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)
