"""Reusable CLI utilities for running AI agent tools like Codex, Claude Code, and Gemini."""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


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
    AgentType.GEMINI: "gemini-3-flash-preview",
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

    Codex-specific options (prefixed with codex_):
        codex_reasoning_effort: Reasoning effort level (low, medium, high).
            Defaults to ``"high"`` for gpt-5.4-mini and ``"medium"`` for others.
        codex_working_dir: Working directory for Codex.
        codex_add_dirs: Additional directories to add to context.
        codex_output_schema: JSON schema for structured output.
        codex_skip_git_check: Skip git repository validation.
    """
    agent: AgentType | Literal["codex", "claude", "gemini"] = AgentType.CODEX
    model: str | None = None
    auto_approve: bool = True

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
# are explicitly provided: Gemini first, then Codex, then Claude.
DEFAULT_AGENT_FALLBACK_ORDER: tuple[AgentConfig, ...] = (
    AgentConfig(agent=AgentType.GEMINI),
    AgentConfig(agent=AgentType.CODEX),
    AgentConfig(agent=AgentType.CLAUDE),
)


def _run_heartbeat(stop_event: threading.Event, interval: float, agent: AgentType) -> None:
    """Log heartbeat messages until stop_event is set."""
    while not stop_event.wait(interval):
        logger.info("Agent %s still running...", agent.value)


def run_with_config(
    prompt: str,
    config: AgentConfig,
    *,
    heartbeat_interval: float | None = None,
) -> AgentResult:
    """Run a coding agent using an AgentConfig.

    This is the primary implementation. All fields on ``config`` are already
    resolved to concrete values by ``AgentConfig.__post_init__``.

    Args:
        prompt: The prompt to send to the agent.
        config: Agent configuration.
        heartbeat_interval: If set, log a heartbeat message every N seconds
            while the agent is running. Useful for long-running jobs.

    Returns:
        AgentResult with output, returncode, stderr, and agent type.

    Raises:
        FileNotFoundError: If agent binary is not found.
        ValueError: If unknown agent type.
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
            )
        elif agent == AgentType.CLAUDE:
            result = _run_claude_internal(
                prompt,
                model=effective_model,
                auto_approve=config.auto_approve,
            )
        elif agent == AgentType.GEMINI:
            result = _run_gemini_internal(
                prompt,
                model=effective_model,
                auto_approve=config.auto_approve,
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
    heartbeat_interval: float | None = None,
) -> str:
    """Run a coding agent using AgentConfig, raising on failure."""
    result = run_with_config(prompt, config, heartbeat_interval=heartbeat_interval)
    if result.returncode != 0:
        error_msg = result.stderr.strip() or result.output.strip()
        raise RuntimeError(f"{result.agent.value} exec failed: {error_msg}")
    return result.output


def run_with_config_and_fallback(
    prompt: str | Sequence[str],
    configs: Sequence[AgentConfig] | None = None,
    *,
    heartbeat_interval: float | None = None,
) -> AgentResult:
    """Try each AgentConfig in order, returning the first successful result.

    An agent is considered to have failed if it returns a non-zero exit code or
    raises an exception (e.g. binary not found). The next config in the list is
    then attempted. If every agent fails, a RuntimeError is raised summarising
    all errors.

    Args:
        prompt: The prompt(s) to send to the agents. If a single string, all
            agents receive the same prompt. If a sequence of strings, each
            agent receives the corresponding prompt (must match len(configs)).
        configs: Ordered sequence of AgentConfigs to try. Defaults to
            ``DEFAULT_AGENT_FALLBACK_ORDER`` (Gemini → Codex → Claude).
        heartbeat_interval: If set, log a heartbeat message every N seconds
            while each agent is running.

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
                agent_prompt, config, heartbeat_interval=heartbeat_interval
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
    codex_reasoning_effort: str | None = None,
    codex_working_dir: Path | None = None,
    codex_add_dirs: list[Path] | None = None,
    codex_output_schema: dict | None = None,
    heartbeat_interval: float | None = None,
) -> AgentResult:
    """Run a coding agent CLI with unified interface.

    Convenience wrapper around :func:`run_with_config`. Builds an
    :class:`AgentConfig` from keyword arguments and delegates.

    Args:
        prompt: The prompt to send to the agent.
        agent: Which agent to use (codex, claude, gemini).
        model: Model name. Defaults to agent-specific default.
        auto_approve: Auto-approve all actions for unattended runs.
        codex_reasoning_effort: Reasoning effort (Codex only: low, medium, high).
            If None, defaults to ``"high"`` for gpt-5.4-mini and ``"medium"``
            for other models.
        codex_working_dir: Working directory (Codex only).
        codex_add_dirs: Additional directories to add (Codex only).
        codex_output_schema: JSON schema for structured output (Codex only).
        heartbeat_interval: If set, log a heartbeat message every N seconds.

    Returns:
        AgentResult with output, returncode, stderr, and agent type.

    Raises:
        FileNotFoundError: If agent binary is not found.
        ValueError: If unknown agent type.
    """
    return run_with_config(
        prompt,
        AgentConfig(
            agent=agent,
            model=model,
            auto_approve=auto_approve,
            codex_reasoning_effort=codex_reasoning_effort,
            codex_working_dir=codex_working_dir,
            codex_add_dirs=codex_add_dirs,
            codex_output_schema=codex_output_schema,
        ),
        heartbeat_interval=heartbeat_interval,
    )


def run_agent_or_raise(
    prompt: str,
    agent: AgentType | Literal["codex", "claude", "gemini"] = AgentType.CODEX,
    *,
    model: str | None = None,
    auto_approve: bool = True,
    codex_reasoning_effort: str | None = None,
    codex_working_dir: Path | None = None,
    codex_add_dirs: list[Path] | None = None,
    codex_output_schema: dict | None = None,
    heartbeat_interval: float | None = None,
) -> str:
    """Run a coding agent CLI and return output, raising on failure.

    Convenience wrapper around :func:`run_with_config_or_raise`.

    Returns:
        The output string from the agent.

    Raises:
        FileNotFoundError: If agent binary is not found.
        ValueError: If unknown agent type.
        RuntimeError: If agent returns non-zero exit code.
    """
    return run_with_config_or_raise(
        prompt,
        AgentConfig(
            agent=agent,
            model=model,
            auto_approve=auto_approve,
            codex_reasoning_effort=codex_reasoning_effort,
            codex_working_dir=codex_working_dir,
            codex_add_dirs=codex_add_dirs,
            codex_output_schema=codex_output_schema,
        ),
        heartbeat_interval=heartbeat_interval,
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
) -> _RunResult:
    """Internal Codex runner."""
    binaries = find_codex_binaries()

    command = [
        binaries.node,
        binaries.codex,
        "exec",
        "--model",
        model,
        "-c",
        f'model_reasoning_effort="{reasoning_effort}"',
    ]

    if auto_approve:
        command.append("--dangerously-bypass-approvals-and-sandbox")

    if skip_git_check:
        command.append("--skip-git-repo-check")

    if working_dir:
        command.extend(["--cd", str(working_dir)])

    if add_dirs:
        for add_dir in add_dirs:
            command.extend(["--add-dir", str(add_dir)])

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
            command.extend(["--output-last-message", str(output_path)])

        command.append("-")

        logger.debug("Running codex command with stdin: %s", " ".join(command[:5]) + " ...")
        completed = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            check=False,
        )

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



def _run_claude_internal(
    prompt: str,
    *,
    model: str,
    auto_approve: bool,
) -> _RunResult:
    """Internal Claude runner."""
    claude_bin = find_claude_bin()

    command = [
        claude_bin,
        "-p",
        "--model",
        model,
    ]

    if auto_approve:
        command.extend(["--permission-mode", "bypassPermissions"])

    logger.debug("Running claude command: %s", " ".join(command))
    completed = subprocess.run(
        command,
        input=prompt,
        capture_output=True,
        text=True,
        check=False,
    )

    return _RunResult(
        output=completed.stdout,
        returncode=completed.returncode,
        stderr=completed.stderr,
    )

# =============================================================================
# Gemini CLI
# =============================================================================


def find_gemini_bin() -> str:
    """Find the Gemini CLI binary."""
    # Priority 1: Check standard paths
    for base in [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]:
        gemini_path = base / "gemini"
        if gemini_path.exists():
            return str(gemini_path)

    # Priority 2: Check NVM
    nvm_dir = Path.home() / ".nvm/versions/node"
    if nvm_dir.exists():
        for version_dir in sorted(nvm_dir.iterdir(), reverse=True):
            if not version_dir.is_dir():
                continue
            gemini_path = version_dir / "bin/gemini"
            if gemini_path.exists():
                return str(gemini_path)

    # Priority 3: Fallback to which
    try:
        path = subprocess.run(["which", "gemini"], capture_output=True, text=True).stdout.strip()
        if path:
            return path
    except Exception:
        pass

    raise FileNotFoundError("gemini not found in standard paths, NVM, or PATH")


def _run_gemini_internal(
    prompt: str,
    *,
    model: str,
    auto_approve: bool = False,
) -> _RunResult:
    """Internal Gemini runner."""
    gemini_bin = find_gemini_bin()

    # Pass an empty string to -p so it enters headless mode and reads from stdin
    command = [
        gemini_bin,
        "-p",
        "",
        "-m",
        model,
    ]

    if auto_approve:
        command.extend(["--approval-mode", "yolo"])

    logger.debug("Running gemini command with stdin: %s", " ".join(command))
    completed = subprocess.run(
        command,
        input=prompt,
        capture_output=True,
        text=True,
        check=False,
    )

    return _RunResult(
        output=completed.stdout,
        returncode=completed.returncode,
        stderr=completed.stderr,
    )
