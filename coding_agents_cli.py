"""Reusable CLI utilities for running AI agent tools like Codex, Claude Code, and Gemini."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from agents import env

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 1200  # 20 minutes
DEFAULT_HEARTBEAT_INTERVAL = 15.0  # seconds
DEFAULT_PTY_WRAPPER_NO_TIMEOUT_SECONDS = 7 * 24 * 60 * 60  # 1 week


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
    AgentType.CODEX: "gpt-5.4-mini",
    AgentType.CLAUDE: "sonnet",
    AgentType.GEMINI: "gemini-3.5-flash",
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
            Defaults to True because the PTY wrapper can use the interactive
            Claude Code session while still returning print-like output.

    Codex-specific options (prefixed with codex_):
        codex_reasoning_effort: Reasoning effort level (low, medium, high).
            Defaults to ``"high"`` for gpt-5.4-mini and ``"medium"`` for others.
        codex_working_dir: Working directory for Codex.
        codex_add_dirs: Additional directories to add to context.
        codex_output_schema: JSON schema for structured output.
        codex_skip_git_check: Skip git repository validation.
    """
    agent: AgentType | Literal["codex", "claude", "gemini"] = AgentType.CLAUDE
    model: str | None = None
    auto_approve: bool = True

    # Claude-specific
    claude_use_pty_wrapper: bool = True

    # Codex-specific
    codex_reasoning_effort: str | None = None
    codex_working_dir: Path | None = None
    codex_add_dirs: list[Path] | None = None
    codex_output_schema: dict | None = None
    codex_skip_git_check: bool = True

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
                "high" if self.model == "gpt-5.4-mini" else "medium"
            )


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
            )
        elif agent == AgentType.CLAUDE:
            result = _run_claude_internal(
                prompt,
                model=effective_model,
                auto_approve=config.auto_approve,
                use_pty_wrapper=config.claude_use_pty_wrapper,
                timeout=timeout,
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



def run_agent(
    prompt: str,
    agent: AgentType | Literal["codex", "claude", "gemini"] = AgentType.CODEX,
    *,
    model: str | None = None,
    auto_approve: bool = True,
    claude_use_pty_wrapper: bool = True,
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
        codex_reasoning_effort: Reasoning effort (Codex only: low, medium, high).
            If None, defaults to ``"high"`` for gpt-5.4-mini and ``"medium"``
            for other models.
        codex_working_dir: Working directory (Codex only).
        codex_add_dirs: Additional directories to add (Codex only).
        codex_output_schema: JSON schema for structured output (Codex only).
        heartbeat_interval: If set, log a heartbeat message every N seconds.
        timeout: Maximum time in seconds to wait. Defaults to 1200 (20 minutes).

    Returns:
        AgentResult with output, returncode, stderr, and agent type.

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
    claude_use_pty_wrapper: bool = True,
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


def find_codex_binaries() -> CodexBinaries:
    """Find node and codex binaries for the Codex CLI."""
    # Priority 1: Check standard paths
    for base in [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]:
        node_path = base / "node"
        codex_path = base / "codex"
        if node_path.exists() and codex_path.exists():
            return CodexBinaries(node=str(node_path), codex=str(codex_path))

    # Priority 2: Check NVM
    nvm_dir = Path.home() / ".nvm/versions/node"
    if nvm_dir.exists():
        for version_dir in sorted(nvm_dir.iterdir(), reverse=True):
            if not version_dir.is_dir():
                continue
            node_path = version_dir / "bin/node"
            codex_path = version_dir / "bin/codex"
            if node_path.exists() and codex_path.exists():
                return CodexBinaries(node=str(node_path), codex=str(codex_path))

    # Priority 3: Fallback to system node and which codex
    try:
        node_path = subprocess.run(["which", "node"], capture_output=True, text=True).stdout.strip()
        codex_path = subprocess.run(["which", "codex"], capture_output=True, text=True).stdout.strip()
        if node_path and codex_path:
            return CodexBinaries(node=node_path, codex=codex_path)
    except Exception:
        pass

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
    command = [
        binaries.node,
        binaries.codex,
        "exec",
        "--model",
        str(config.model),
        "-c",
        f'model_reasoning_effort="{config.codex_reasoning_effort}"',
    ]

    if config.auto_approve:
        command.append("--dangerously-bypass-approvals-and-sandbox")

    if config.codex_skip_git_check:
        command.append("--skip-git-repo-check")

    if config.codex_working_dir:
        command.extend(["--cd", str(config.codex_working_dir)])

    if config.codex_add_dirs:
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
            command.extend(["--output-schema", str(schema_path)])

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
            ),
            output_schema_path=schema_path,
            output_path=output_path,
        )

        logger.debug("Running codex command with stdin: %s", " ".join(command[:5]) + " ...")
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
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


def find_claude_bin() -> str:
    """Find the Claude Code CLI binary."""
    # Priority 1: Check standard paths
    for base in [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]:
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
    for base in [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]:
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

    if auto_approve:
        command.extend(["--permission-mode", "bypassPermissions"])

    logger.debug("Running claude command: %s", " ".join(command))
    proc_env = env.build_env()
    subprocess_timeout = timeout
    if use_pty_wrapper and timeout is not None:
        subprocess_timeout = timeout + 10
    try:
        completed = subprocess.run(
            command,
            input=prompt,
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
    for base in [
        Path.home() / ".local/bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    ]:
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


def build_gemini_command(*, auto_approve: bool) -> list[str]:
    """Build an agy command that reads the prompt from stdin via ``--print -``."""
    command = [find_gemini_bin()]
    if auto_approve:
        command.append("--dangerously-skip-permissions")
    command.extend(["--print", "-"])
    return command


def _run_gemini_internal(
    prompt: str,
    *,
    model: str,
    auto_approve: bool = False,
    timeout: float | None = None,
) -> _RunResult:
    """Internal Gemini runner (using agy CLI under the hood)."""
    command = build_gemini_command(auto_approve=auto_approve)

    logger.debug("Running agy command: %s", command)
    proc_env = build_gemini_env(model)

    try:
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
        raise TimeoutError(f"Gemini timed out after {timeout} seconds") from e

    return _RunResult(
        output=completed.stdout,
        returncode=completed.returncode,
        stderr=completed.stderr,
    )
