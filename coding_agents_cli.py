"""Reusable CLI utilities for running AI agent tools like Codex, Claude Code, and Gemini.

Environment note (cmux + Claude Code): when one of these agent runs is spawned as a
background child of a Claude Code session hosted inside cmux, an agent-process
reaper can SIGKILL the whole process group roughly 6 minutes after the hosting
session goes idle (observed 2026-07-15: two ~7-minute codex runs killed at idle+6min
while plain shells survived; a detached run of the same command completed). For runs
expected to exceed ~6 minutes in that environment, use ``run_detached`` (CLI:
``--detach OUTPUT_BASE``) so the agent runs in its own session, invisible to the
reaper's process-tree walk. Short runs don't need it.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from agents import env

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 1200  # 20 minutes
DEFAULT_HEARTBEAT_INTERVAL = 15.0  # seconds
DEFAULT_PTY_WRAPPER_NO_TIMEOUT_SECONDS = 7 * 24 * 60 * 60  # 1 week

# Ordered directories to search for agent CLI binaries (node, claude, codex, agy,
# claude-pty-wrapper). The Hermes agent ships its own bundled Node runtime under
# ~/.hermes/node and shims node/npm/claude into ~/.local/bin, so those come first;
# Homebrew / /usr/local are legacy fallbacks. Codex in particular only lives under
# ~/.hermes/node/bin, so omitting it leaves codex undiscoverable.
_AGENT_BIN_DIRS: tuple[Path, ...] = (
    Path.home() / ".local/bin",
    Path.home() / ".hermes/node/bin",
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
)


# =============================================================================
# Unified Interface
# =============================================================================


class AgentType(str, Enum):
    """Supported coding agent CLI types."""
    CODEX = "codex"
    CLAUDE = "claude"
    GEMINI = "gemini"


# Default models per agent
DEFAULT_MODELS: dict[AgentType, str] = {
    AgentType.CODEX: "gpt-5.6-luna",
    AgentType.CLAUDE: "sonnet",
    AgentType.GEMINI: "gemini-3.6-flash",
}


@dataclass
class AgentConfig:
    """Unified configuration for all coding agent CLIs.

    All fields are resolved to concrete values on construction — ``model`` and
    ``codex_reasoning_effort`` are filled with their defaults automatically, so
    callers can always read them directly without calling any getter method.

    Common options:
        agent: Which agent to use (codex, claude, gemini). Accepts a string;
            normalised to AgentType on construction.
        model: Model name. Defaults to the agent-specific default.
        auto_approve: Auto-approve all actions for unattended runs.

    Claude-specific options (prefixed with claude_):
        claude_use_pty_wrapper: Use claude-pty-wrapper instead of ``claude -p``.
            Defaults to False (``claude -p``). The PTY wrapper drives the
            interactive Claude Code session, which starts plugin MCP servers
            (Playwright / chrome-devtools via ``npm exec``) that don't shut down
            on teardown and keep the wrapper's PTY slave open, so an
            otherwise-finished headless run hangs until its hard timeout (and
            leaks those servers). Direct ``claude -p`` exits cleanly, so it is the
            default for unattended runs; set this True to opt into the
            interactive-session wrapper.
        claude_disable_mcp: Run Claude with no MCP servers (``--strict-mcp-config``
            + empty ``--mcp-config``). Use for unattended/headless runs that need
            no MCP tools: it avoids the teardown hang where orphaned stdio MCP
            servers keep the PTY open so the wrapper never sees EOF, and it stops
            those servers from leaking as background processes.
        claude_effort: Reasoning effort forwarded via Claude's ``--effort``
            flag (low, medium, high, xhigh, max). When None (default), no
            ``--effort`` flag is emitted and any ambient ``CLAUDE_EFFORT``
            environment variable is left in effect via the inherited
            subprocess env. Setting it explicitly overrides the env var for
            this run. Accepted by both the direct ``claude -p`` path and the
            ``claude-pty-wrapper`` path (which forwards ``--effort``).
        claude_review_command: Invoke an unnamespaced native Claude Code review
            command before the supplied prompt: ``code-review`` for the current
            working diff or ``review`` for a GitHub pull request.
        claude_review_target: Optional PR number or URL passed to ``/review``.

    Codex-specific options (prefixed with codex_):
        codex_reasoning_effort: Reasoning effort level
            (low, medium, high, xhigh, max). Defaults to ``"high"`` for the
            default Codex model (gpt-5.6-luna) and ``"medium"`` for others.
            xhigh/max are honoured by extended-reasoning models (e.g. gpt-5.x
            reasoning variants).
        codex_working_dir: Working directory for Codex.
        codex_add_dirs: Additional directories to add to context.
        codex_output_schema: JSON schema for structured output.
        codex_skip_git_check: Skip git repository validation.
        codex_review: Invoke the native headless ``codex exec review`` workflow
            with the supplied prompt as custom review instructions.
    """
    agent: AgentType | Literal["codex", "claude", "gemini"] = AgentType.CLAUDE
    model: str | None = None
    auto_approve: bool = True

    # Claude-specific
    claude_use_pty_wrapper: bool = False
    claude_disable_mcp: bool = False
    # Reasoning effort for Claude ("low", "medium", "high", "xhigh", "max").
    # When None, no --effort flag is passed; an ambient CLAUDE_EFFORT env var
    # (if set) continues to take effect via the inherited subprocess env.
    claude_effort: str | None = None
    claude_review_command: Literal["code-review", "review"] | None = None
    claude_review_target: str | None = None

    # Codex-specific
    codex_reasoning_effort: str | None = None
    codex_working_dir: Path | None = None
    codex_add_dirs: list[Path] | None = None
    codex_output_schema: dict | None = None
    codex_skip_git_check: bool = True
    codex_review: bool = False

    def __post_init__(self) -> None:
        # Normalise agent string → AgentType
        if isinstance(self.agent, str):
            self.agent = AgentType(self.agent)
        # Resolve model default
        if self.model is None:
            self.model = DEFAULT_MODELS[self.agent]
        # Resolve codex reasoning effort default
        if self.codex_reasoning_effort is None:
            self.codex_reasoning_effort = (
                "high" if self.model == DEFAULT_MODELS[AgentType.CODEX] else "medium"
            )
        if self.claude_review_command not in {None, "code-review", "review"}:
            raise ValueError(f"Unknown Claude review command: {self.claude_review_command}")
        if self.claude_review_target is not None:
            if self.claude_review_command != "review":
                raise ValueError("claude_review_target requires claude_review_command='review'")
            if not self.claude_review_target.strip() or "\n" in self.claude_review_target:
                raise ValueError("claude_review_target must be a non-empty single line")


@dataclass(frozen=True)
class AgentResult:
    """Unified result from running any coding agent CLI."""
    output: str
    returncode: int
    stderr: str
    agent: AgentType


# Default fallback order used by run_with_config_and_fallback when no configs
# are explicitly provided: Codex first, then Claude, then Gemini.
DEFAULT_AGENT_FALLBACK_ORDER: tuple[AgentConfig, ...] = (
    AgentConfig(agent=AgentType.CODEX),
    AgentConfig(agent=AgentType.CLAUDE),
    AgentConfig(agent=AgentType.GEMINI),
)


def _run_heartbeat(stop_event: threading.Event, interval: float, agent: AgentType) -> None:
    """Log heartbeat messages until stop_event is set."""
    while not stop_event.wait(interval):
        logger.info("Agent %s still running...", agent.value)


def run_with_config(
    prompt: str,
    config: AgentConfig,
    *,
    heartbeat_interval: float | None = DEFAULT_HEARTBEAT_INTERVAL,
    timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
) -> AgentResult:
    """Run a coding agent using an AgentConfig.

    This is the primary implementation. All fields on ``config`` are already
    resolved to concrete values by ``AgentConfig.__post_init__``.

    Args:
        prompt: The prompt to send to the agent.
        config: Agent configuration.
        heartbeat_interval: If set, log a heartbeat message every N seconds
            while the agent is running. Useful for long-running jobs.
        timeout: Maximum time in seconds to wait for the agent to complete.
            Defaults to 1200 (20 minutes). Set to None for no timeout.

    Returns:
        AgentResult with output, returncode, stderr, and agent type.

    Raises:
        FileNotFoundError: If agent binary is not found.
        ValueError: If unknown agent type.
        TimeoutError: If the agent exceeds the timeout.
    """
    agent = config.agent
    effective_model = config.model

    logger.info("Running agent with config: %s", config)

    # Start heartbeat thread if interval is set
    stop_heartbeat: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None
    if heartbeat_interval is not None:
        stop_heartbeat = threading.Event()
        heartbeat_thread = threading.Thread(
            target=_run_heartbeat,
            args=(stop_heartbeat, heartbeat_interval, agent),
            daemon=True,
        )
        heartbeat_thread.start()

    try:
        if agent == AgentType.CODEX:
            result = _run_codex_internal(
                prompt,
                model=effective_model,
                reasoning_effort=config.codex_reasoning_effort,
                auto_approve=config.auto_approve,
                skip_git_check=config.codex_skip_git_check,
                working_dir=config.codex_working_dir,
                add_dirs=config.codex_add_dirs,
                output_schema=config.codex_output_schema,
                timeout=timeout,
                review=config.codex_review,
            )
        elif agent == AgentType.CLAUDE:
            result = _run_claude_internal(
                prompt,
                model=effective_model,
                auto_approve=config.auto_approve,
                use_pty_wrapper=config.claude_use_pty_wrapper,
                disable_mcp=config.claude_disable_mcp,
                effort=config.claude_effort,
                timeout=timeout,
                review_command=config.claude_review_command,
                review_target=config.claude_review_target,
            )
        elif agent == AgentType.GEMINI:
            result = _run_gemini_internal(
                prompt,
                model=effective_model,
                auto_approve=config.auto_approve,
                timeout=timeout,
            )
        else:
            raise ValueError(f"Unknown agent type: {agent}")

        return AgentResult(
            output=result.output,
            returncode=result.returncode,
            stderr=result.stderr,
            agent=agent,
        )
    finally:
        if stop_heartbeat is not None:
            stop_heartbeat.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1.0)


def run_with_config_or_raise(
    prompt: str,
    config: AgentConfig,
    *,
    heartbeat_interval: float | None = DEFAULT_HEARTBEAT_INTERVAL,
    timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Run a coding agent using AgentConfig, raising on failure."""
    result = run_with_config(prompt, config, heartbeat_interval=heartbeat_interval, timeout=timeout)
    if result.returncode != 0:
        error_msg = result.stderr.strip() or result.output.strip()
        raise RuntimeError(f"{result.agent.value} exec failed: {error_msg}")
    return result.output


def run_with_config_and_fallback(
    prompt: str | Sequence[str],
    configs: Sequence[AgentConfig] | None = None,
    *,
    heartbeat_interval: float | None = DEFAULT_HEARTBEAT_INTERVAL,
    timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
) -> AgentResult:
    """Try each AgentConfig in order, returning the first successful result.

    An agent is considered to have failed if it returns a non-zero exit code or
    raises an exception (e.g. binary not found, timeout). The next config in
    the list is then attempted. If every agent fails, a RuntimeError is raised
    summarising all errors.

    Args:
        prompt: The prompt(s) to send to the agents. If a single string, all
            agents receive the same prompt. If a sequence of strings, each
            agent receives the corresponding prompt (must match len(configs)).
        configs: Ordered sequence of AgentConfigs to try. Defaults to
            ``DEFAULT_AGENT_FALLBACK_ORDER`` (Codex → Claude → Gemini).
        heartbeat_interval: If set, log a heartbeat message every N seconds
            while each agent is running.
        timeout: Maximum time in seconds to wait for each agent to complete.
            Defaults to 1200 (20 minutes). Set to None for no timeout.

    Returns:
        The first successful AgentResult.

    Raises:
        ValueError: If configs is empty, or if prompt is a sequence whose
            length does not match the number of configs.
        RuntimeError: If all agents fail.
    """
    if configs is None:
        configs = DEFAULT_AGENT_FALLBACK_ORDER
    if not configs:
        raise ValueError("configs must not be empty")

    if isinstance(prompt, str):
        prompts: Sequence[str] = [prompt] * len(configs)
    else:
        prompts = prompt
        if len(prompts) != len(configs):
            raise ValueError(
                f"prompt sequence length ({len(prompts)}) must match "
                f"configs length ({len(configs)})"
            )

    errors: list[str] = []
    for config, agent_prompt in zip(configs, prompts):
        try:
            result = run_with_config(
                agent_prompt, config, heartbeat_interval=heartbeat_interval, timeout=timeout
            )
            if result.returncode == 0:
                return result
            error_msg = result.stderr.strip() or result.output.strip()
            errors.append(f"{config.agent.value}: {error_msg}")
            logger.warning(
                "Agent %s failed (returncode=%d): %s. Trying next fallback.",
                config.agent.value,
                result.returncode,
                error_msg or "(no error message)",
            )
        except Exception as exc:
            errors.append(f"{config.agent.value}: {exc}")
            logger.warning(
                "Agent %s raised an exception (%s), trying next fallback.",
                config.agent.value,
                exc,
            )

    raise RuntimeError(
        "All agents failed:\n" + "\n".join(f"  - {e}" for e in errors)
    )


def run_detached(
    runner: Callable[[], AgentResult],
    output_base: str | Path,
) -> dict:
    """Run an agent call as a fully detached daemon and return immediately.

    Use this only when the run is expected to be LONG (more than ~6 minutes) and
    the caller lives inside an environment whose idle reaper kills agent child
    processes — see the module docstring. Short runs gain nothing from it and
    lose the natural wait-for-exit-code contract.

    The agent runs in its own session (double fork + ``os.setsid``), reparented
    to launchd/init, so it survives the caller's exit and is invisible to
    process-tree walks of the calling session. Results are file-based:

    - ``<output_base>.out``      — the agent's final output (``AgentResult.output``)
    - ``<output_base>.err``      — stderr/log stream while running (line-buffered)
    - ``<output_base>.pid``      — pid of the detached process
    - ``<output_base>.exitcode`` — written LAST; its existence means the run is done

    Callers poll for ``.exitcode`` to detect completion.

    Args:
        runner: Zero-arg callable executing the agent call (e.g. a closure over
            ``run_with_config(...)``). It runs inside the detached process; any
            configured timeout still applies there. An exception in the runner
            is written to ``.err`` and yields exit code 1.
        output_base: Path prefix for the result files; parent dirs are created.

    Returns:
        Dict with ``pid`` and the four file paths, from the calling process.
    """
    base = Path(output_base)
    base.parent.mkdir(parents=True, exist_ok=True)
    paths = {name: f"{base}.{name}" for name in ("out", "err", "pid", "exitcode")}

    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid > 0:  # original process
        os.close(write_fd)
        with os.fdopen(read_fd) as fh:
            daemon_pid = int(fh.read().strip() or "-1")
        os.waitpid(pid, 0)  # reap the intermediate child
        return {
            "pid": daemon_pid,
            "out": paths["out"],
            "err": paths["err"],
            "pid_file": paths["pid"],
            "exitcode": paths["exitcode"],
        }

    # Intermediate child: new session, then exit so the grandchild is orphaned
    # (reparented to launchd) and can never reacquire a controlling terminal.
    os.close(read_fd)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)

    # Grandchild (the detached daemon).
    rc = 1
    try:
        with os.fdopen(write_fd, "w") as fh:
            fh.write(str(os.getpid()))
        Path(paths["pid"]).write_text(str(os.getpid()))
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        err_fd = os.open(paths["err"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        os.dup2(err_fd, 1)  # stray stdout prints belong in the log stream;
        os.dup2(err_fd, 2)  # the real answer is written to .out explicitly.
        # Rebind the Python-level streams too: a wrapper (pytest capture, an
        # outer redirector) may have replaced sys.stdout/sys.stderr with objects
        # that bypass fds 1/2 entirely.
        import sys

        sys.stdout = os.fdopen(1, "w", buffering=1)
        sys.stderr = os.fdopen(2, "w", buffering=1)
        try:
            result = runner()
            Path(paths["out"]).write_text(result.output)
            rc = result.returncode
        except BaseException:
            import traceback

            traceback.print_exc()
    finally:
        try:
            Path(paths["exitcode"]).write_text(str(rc))
        finally:
            os._exit(rc if 0 <= rc < 256 else 1)


def run_agent(
    prompt: str,
    agent: AgentType | Literal["codex", "claude", "gemini"] = AgentType.CODEX,
    *,
    model: str | None = None,
    auto_approve: bool = True,
    claude_use_pty_wrapper: bool = False,
    claude_effort: str | None = None,
    codex_reasoning_effort: str | None = None,
    codex_working_dir: Path | None = None,
    codex_add_dirs: list[Path] | None = None,
    codex_output_schema: dict | None = None,
    heartbeat_interval: float | None = DEFAULT_HEARTBEAT_INTERVAL,
    timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
) -> AgentResult:
    """Run a coding agent CLI with unified interface.

    Convenience wrapper around :func:`run_with_config`. Builds an
    :class:`AgentConfig` from keyword arguments and delegates.

    Args:
        prompt: The prompt to send to the agent.
        agent: Which agent to use (codex, claude, gemini).
        model: Model name. Defaults to agent-specific default.
        auto_approve: Auto-approve all actions for unattended runs.
claude_use_pty_wrapper: Use claude-pty-wrapper for Claude runs instead
            of ``claude -p``. Defaults to True.
        claude_effort: Reasoning effort for Claude runs forwarded via Claude's
            ``--effort`` flag (low, medium, high, xhigh, max). When None (the
            default), no ``--effort`` flag is emitted and any ambient
            ``CLAUDE_EFFORT`` environment variable stays in effect.
        codex_reasoning_effort: Reasoning effort (Codex only: low, medium, high,
            xhigh, max). If None, defaults to ``"high"`` for the default Codex
            model (gpt-5.6-luna) and ``"medium"`` for others. xhigh/max require
            a model that honours extended reasoning.
        codex_working_dir: Working directory (Codex only).
        codex_add_dirs: Additional directories to add (Codex only).
        codex_output_schema: JSON schema for structured output (Codex only).
        heartbeat_interval: If set, log a heartbeat message every N seconds.
        timeout: Maximum time in seconds to wait. Defaults to 1200 (20 minutes).

    Returns:
        An AgentResult with output, returncode, stderr, and agent type.

    Raises:
        FileNotFoundError: If agent binary is not found.
        ValueError: If unknown agent type.
        TimeoutError: If the agent exceeds the timeout.
    """
    return run_with_config(
        prompt,
        AgentConfig(
            agent=agent,
            model=model,
            auto_approve=auto_approve,
            claude_use_pty_wrapper=claude_use_pty_wrapper,
            claude_effort=claude_effort,
            codex_reasoning_effort=codex_reasoning_effort,
            codex_working_dir=codex_working_dir,
            codex_add_dirs=codex_add_dirs,
            codex_output_schema=codex_output_schema,
        ),
        heartbeat_interval=heartbeat_interval,
        timeout=timeout,
    )


def run_agent_or_raise(
    prompt: str,
    agent: AgentType | Literal["codex", "claude", "gemini"] = AgentType.CODEX,
    *,
    model: str | None = None,
    auto_approve: bool = True,
    claude_use_pty_wrapper: bool = False,
    claude_effort: str | None = None,
    codex_reasoning_effort: str | None = None,
    codex_working_dir: Path | None = None,
    codex_add_dirs: list[Path] | None = None,
    codex_output_schema: dict | None = None,
    heartbeat_interval: float | None = DEFAULT_HEARTBEAT_INTERVAL,
    timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Run a coding agent CLI and return output, raising on failure.

    Convenience wrapper around :func:`run_with_config_or_raise`.

    Args:
        claude_use_pty_wrapper: Use claude-pty-wrapper for Claude runs instead
            of ``claude -p``. Defaults to True.
        claude_effort: Reasoning effort for Claude runs (see :func:`run_agent`).

    Returns:
        The output string from the agent.

    Raises:
        FileNotFoundError: If agent binary is not found.
        ValueError: If unknown agent type.
        RuntimeError: If agent returns non-zero exit code.
        TimeoutError: If the agent exceeds the timeout.
    """
    return run_with_config_or_raise(
        prompt,
        AgentConfig(
            agent=agent,
            model=model,
            auto_approve=auto_approve,
            claude_use_pty_wrapper=claude_use_pty_wrapper,
            claude_effort=claude_effort,
            codex_reasoning_effort=codex_reasoning_effort,
            codex_working_dir=codex_working_dir,
            codex_add_dirs=codex_add_dirs,
            codex_output_schema=codex_output_schema,
        ),
        heartbeat_interval=heartbeat_interval,
        timeout=timeout,
    )


# =============================================================================
# Codex CLI
# =============================================================================


@dataclass(frozen=True)
class CodexBinaries:
    """Paths to node and codex binaries."""
    node: str
    codex: str


def _find_binary(name: str) -> str | None:
    """Locate a single CLI binary, preferring ~/.local/bin, then the other
    standard agent dirs, then the newest NVM node version, then `which`."""
    # Priority 1: Standard paths (~/.local/bin first).
    for base in _AGENT_BIN_DIRS:
        candidate = base / name
        if candidate.exists():
            return str(candidate)

    # Priority 2: Newest NVM node version that has the binary.
    nvm_dir = Path.home() / ".nvm/versions/node"
    if nvm_dir.exists():
        for version_dir in sorted(nvm_dir.iterdir(), reverse=True):
            candidate = version_dir / "bin" / name
            if version_dir.is_dir() and candidate.exists():
                return str(candidate)

    # Priority 3: Fallback to `which`.
    try:
        found = subprocess.run(["which", name], capture_output=True, text=True).stdout.strip()
        if found:
            return found
    except Exception:
        pass

    return None


def find_codex_binaries() -> CodexBinaries:
    """Find node and codex binaries for the Codex CLI.

    Each binary is resolved independently, preferring ~/.local/bin (where the
    Hermes-bundled node/codex are shimmed) so codex is discovered there first
    even if node resolves from a different directory.
    """
    codex_path = _find_binary("codex")
    node_path = _find_binary("node")
    if codex_path and node_path:
        return CodexBinaries(node=node_path, codex=codex_path)

    raise FileNotFoundError("Could not find node and codex binaries in standard paths or NVM.")


@dataclass(frozen=True)
class _RunResult:
    """Internal result shared by all agent runners."""
    output: str
    returncode: int
    stderr: str


def build_codex_command(
    config: AgentConfig,
    *,
    output_schema_path: Path | None = None,
    output_path: Path | None = None,
) -> list[str]:
    """Build a Codex CLI command from an AgentConfig."""
    binaries = find_codex_binaries()
    native_review = config.codex_review
    command = [binaries.node, binaries.codex, "exec"]

    # `--cd` and `--add-dir` are `codex exec` options, so they must precede the
    # `review` subcommand. Keep the legacy generic command ordering unchanged.
    if native_review:
        if config.codex_working_dir:
            command.extend(["--cd", str(config.codex_working_dir)])
        if config.codex_add_dirs:
            for add_dir in config.codex_add_dirs:
                command.extend(["--add-dir", str(add_dir)])
        command.append("review")

    command.extend(
        [
            "--model",
            str(config.model),
            "-c",
            f'model_reasoning_effort="{config.codex_reasoning_effort}"',
        ]
    )

    if config.auto_approve:
        command.append("--dangerously-bypass-approvals-and-sandbox")

    if config.codex_skip_git_check:
        command.append("--skip-git-repo-check")

    if config.codex_working_dir and not native_review:
        command.extend(["--cd", str(config.codex_working_dir)])

    if config.codex_add_dirs and not native_review:
        for add_dir in config.codex_add_dirs:
            command.extend(["--add-dir", str(add_dir)])

    if output_schema_path is not None:
        command.extend(["--output-schema", str(output_schema_path)])

    if output_path is not None:
        command.extend(["--output-last-message", str(output_path)])

    command.append("-")
    return command


def _run_codex_internal(
    prompt: str,
    *,
    model: str,
    reasoning_effort: str,
    auto_approve: bool,
    skip_git_check: bool,
    working_dir: Path | None,
    add_dirs: list[Path] | None,
    output_schema: dict | None,
    timeout: float | None,
    review: bool = False,
) -> _RunResult:
    """Internal Codex runner."""
    schema_path: Path | None = None
    output_path: Path | None = None

    try:
        if output_schema:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8"
            ) as schema_file:
                json.dump(output_schema, schema_file)
                schema_path = Path(schema_file.name)

            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8"
            ) as output_file:
                output_path = Path(output_file.name)

        command = build_codex_command(
            AgentConfig(
                agent=AgentType.CODEX,
                model=model,
                auto_approve=auto_approve,
                codex_reasoning_effort=reasoning_effort,
                codex_working_dir=working_dir,
                codex_add_dirs=add_dirs,
                codex_output_schema=output_schema,
                codex_skip_git_check=skip_git_check,
                codex_review=review,
            ),
            output_schema_path=schema_path,
            output_path=output_path,
        )

        logger.debug("Running codex command with stdin: %s", " ".join(command[:5]) + " ...")
        try:
            proc_env = env.build_env()
            completed = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
                env=proc_env,
            )
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(f"Codex timed out after {timeout} seconds") from e

        if output_path and output_path.exists():
            output = output_path.read_text(encoding="utf-8")
        else:
            output = completed.stdout

        return _RunResult(
            output=output,
            returncode=completed.returncode,
            stderr=completed.stderr,
        )
    finally:
        if schema_path:
            schema_path.unlink(missing_ok=True)
        if output_path:
            output_path.unlink(missing_ok=True)


# =============================================================================
# Claude Code CLI
# =============================================================================


def build_claude_prompt(
    prompt: str,
    *,
    review_command: Literal["code-review", "review"] | None = None,
    review_target: str | None = None,
) -> str:
    """Prefix a prompt with an unnamespaced native Claude review command."""
    if review_command is None:
        return prompt

    command = f"/{review_command}"
    if review_target is not None:
        command = f"{command} {review_target}"
    if not prompt:
        return command
    return f"{command}\n\n{prompt}"


def find_claude_bin() -> str:
    """Find the Claude Code CLI binary."""
    # Priority 1: Check standard paths
    for base in _AGENT_BIN_DIRS:
        claude_path = base / "claude"
        if claude_path.exists():
            return str(claude_path)

    # Priority 2: Check NVM
    nvm_dir = Path.home() / ".nvm/versions/node"
    if nvm_dir.exists():
        for version_dir in sorted(nvm_dir.iterdir(), reverse=True):
            if not version_dir.is_dir():
                continue
            claude_path = version_dir / "bin/claude"
            if claude_path.exists():
                return str(claude_path)

    # Priority 3: Fallback to which
    try:
        path = subprocess.run(["which", "claude"], capture_output=True, text=True).stdout.strip()
        if path:
            return path
    except Exception:
        pass

    raise FileNotFoundError("claude not found in standard paths, NVM, or PATH")


def find_claude_pty_wrapper_bin() -> str:
    """Find the claude-pty-wrapper CLI binary."""
    # Priority 1: Check standard paths
    for base in _AGENT_BIN_DIRS:
        wrapper_path = base / "claude-pty-wrapper"
        if wrapper_path.exists():
            return str(wrapper_path)

    # Priority 2: Check NVM
    nvm_dir = Path.home() / ".nvm/versions/node"
    if nvm_dir.exists():
        for version_dir in sorted(nvm_dir.iterdir(), reverse=True):
            if not version_dir.is_dir():
                continue
            wrapper_path = version_dir / "bin/claude-pty-wrapper"
            if wrapper_path.exists():
                return str(wrapper_path)

    # Priority 3: Fallback to which
    try:
        path = subprocess.run(
            ["which", "claude-pty-wrapper"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if path:
            return path
    except Exception:
        pass

    raise FileNotFoundError(
        "claude-pty-wrapper not found in standard paths, NVM, or PATH"
    )



def _run_claude_internal(
    prompt: str,
    *,
    model: str,
    auto_approve: bool,
    use_pty_wrapper: bool,
    timeout: float | None,
    disable_mcp: bool = False,
    effort: str | None = None,
    review_command: Literal["code-review", "review"] | None = None,
    review_target: str | None = None,
) -> _RunResult:
    """Internal Claude runner."""
    claude_bin = find_claude_bin()

    if use_pty_wrapper:
        command = [
            find_claude_pty_wrapper_bin(),
            "-p",
            "--claude-bin",
            claude_bin,
            "--model",
            model,
        ]
        wrapper_timeout = timeout or DEFAULT_PTY_WRAPPER_NO_TIMEOUT_SECONDS
        command.extend(["--timeout", str(wrapper_timeout)])
    else:
        command = [
            claude_bin,
            "-p",
            "--model",
            model,
        ]

    # Forward reasoning effort when requested. Both `claude -p` and
    # claude-pty-wrapper accept --effort and forward it to Claude. When omitted,
    # an ambient CLAUDE_EFFORT env var (if any) still applies via the inherited
    # subprocess env, so we only inject the flag when explicitly set.
    if effort is not None:
        command.extend(["--effort", effort])

    if auto_approve:
        command.extend(["--permission-mode", "bypassPermissions"])

    if disable_mcp:
        # No MCP servers: --strict-mcp-config makes Claude ignore all ambient MCP
        # config (including plugin-provided servers), and an empty --mcp-config
        # leaves nothing enabled. Both the pty-wrapper and `claude -p` accept and
        # forward these flags. This prevents the headless-teardown hang where
        # orphaned npm-exec stdio MCP servers (Playwright / chrome-devtools) hold
        # the wrapper's PTY slave open so its master read never sees EOF, and it
        # stops those servers from leaking as long-lived background processes.
        command.extend(["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'])

    logger.debug("Running claude command: %s", " ".join(command))
    proc_env = env.build_env()
    subprocess_timeout = timeout
    if use_pty_wrapper and timeout is not None:
        subprocess_timeout = timeout + 10
    try:
        effective_prompt = build_claude_prompt(
            prompt,
            review_command=review_command,
            review_target=review_target,
        )
        completed = subprocess.run(
            command,
            input=effective_prompt,
            capture_output=True,
            text=True,
            check=False,
            timeout=subprocess_timeout,
            env=proc_env,
        )
    except subprocess.TimeoutExpired as e:
        raise TimeoutError(f"Claude timed out after {timeout} seconds") from e

    return _RunResult(
        output=completed.stdout,
        returncode=completed.returncode,
        stderr=completed.stderr,
    )

# =============================================================================
# Gemini CLI
# =============================================================================


def find_gemini_bin() -> str:
    """Find the agy CLI binary (named gemini for compatibility)."""
    # Priority 1: Check standard paths and user local bin
    for base in _AGENT_BIN_DIRS:
        agy_path = base / "agy"
        if agy_path.exists():
            return str(agy_path)

    # Priority 2: Check NVM
    nvm_dir = Path.home() / ".nvm/versions/node"
    if nvm_dir.exists():
        for version_dir in sorted(nvm_dir.iterdir(), reverse=True):
            if not version_dir.is_dir():
                continue
            agy_path = version_dir / "bin/agy"
            if agy_path.exists():
                return str(agy_path)

    # Priority 3: Fallback to which
    try:
        path = subprocess.run(["which", "agy"], capture_output=True, text=True).stdout.strip()
        if path:
            return path
    except Exception:
        pass

    raise FileNotFoundError("agy not found in standard paths, NVM, or PATH")


def build_gemini_env(model: str) -> dict[str, str]:
    """Build the env dict used to invoke agy with the configured model."""
    proc_env = env.build_env()
    proc_env["ANTIGRAVITY_MODEL"] = model
    return proc_env


def build_gemini_command(*, auto_approve: bool, prompt: str | None = None) -> list[str]:
    """Build an agy command that runs a single prompt non-interactively.

    agy no longer reads the prompt from stdin: passing ``--print -`` makes it
    treat ``-`` as the literal prompt and reply with a generic greeting. The
    prompt must be passed as the ``--print`` argument instead. When ``prompt``
    is None the legacy ``--print -`` form is emitted (streaming path, which
    supplies the prompt separately via a pty)."""
    command = [find_gemini_bin()]
    if auto_approve:
        command.append("--dangerously-skip-permissions")
    if prompt is None:
        command.extend(["--print", "-"])
    else:
        command.extend(["--print", prompt])
    return command


def _run_gemini_internal(
    prompt: str,
    *,
    model: str,
    auto_approve: bool = False,
    timeout: float | None = None,
) -> _RunResult:
    """Internal Gemini runner (using agy CLI under the hood).

    The prompt is written to a temporary file and passed to agy via shell
    command substitution to avoid ARG_MAX limits on very long prompts.
    """
    import shlex

    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    ) as prompt_file:
        prompt_file.write(prompt)
        prompt_file_path = prompt_file.name

    try:
        gemini_bin = shlex.quote(find_gemini_bin())
        skip_perms = "--dangerously-skip-permissions " if auto_approve else ""
        cat_path = shlex.quote(prompt_file_path)
        shell_cmd = f'{gemini_bin} {skip_perms}--print "$(cat {cat_path})"'
        command = ["sh", "-c", shell_cmd]

        logger.debug("Running agy command via temp file: %s", prompt_file_path)
        proc_env = build_gemini_env(model)

        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=proc_env,
        )
    except subprocess.TimeoutExpired as e:
        raise TimeoutError(f"Gemini timed out after {timeout} seconds") from e
    finally:
        os.unlink(prompt_file_path)

    return _RunResult(
        output=completed.stdout,
        returncode=completed.returncode,
        stderr=completed.stderr,
    )
