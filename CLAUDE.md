# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Codex, OpenClaw, Hermes, and similar tools) when working with code in this repository.

## Project

`agents` is a small Python library of reusable utilities for driving AI coding-agent CLIs (Codex, Claude Code, Gemini/`agy`) and for calling OpenRouter chat completions. It is consumed by other local projects as a package — modules are imported as `from agents import <module>`.

## Environment

- Use the `ml` conda env interpreter from the global `~/.claude/CLAUDE.md` for all Python/pytest invocations.
- The library is imported as the top-level package `agents`. The parent directory of this repo must be on `sys.path` (this is how sibling projects pick it up); do not add path hacks inside modules.

## Common commands

- Run tests: `/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python -m pytest tests/`
- Run a single test: `... -m pytest tests/test_openrouter.py::test_openrouter_client_sends_chat_completion_request`
- Source Codex bin/node discovery (finds newest NVM node with `codex` installed and exports `CODEX_BIN` / `CODEX_NODE_BIN`): `source scripts/codex-env.sh`

## Architecture

Three layers, all in the top-level `agents/` package:

1. **`env.build_env()`** — sources `~/.zshenv` and `~/.config/secrets.env` and returns an env dict suitable for `subprocess.run(env=...)`. This is the single entry point for getting a usable PATH and API keys inside launchd/cron contexts where the parent env is minimal. Other modules in this repo call it rather than reaching into `os.environ` directly.

2. **`coding_agents_cli`** — unified blocking interface for the three agent CLIs. The shape is:
   - `AgentType` enum (`codex`/`claude`/`gemini`) and `DEFAULT_MODELS` per agent.
   - `AgentConfig` dataclass: all options for all agents live here, prefixed by agent (`claude_*`, `codex_*`). `__post_init__` resolves defaults so callers never need a getter.
   - `run_with_config(prompt, config)` dispatches to per-agent internal runners; `run_with_config_and_fallback` walks `DEFAULT_AGENT_FALLBACK_ORDER` (Codex → Claude → Gemini).
   - Claude defaults to going through the `claude-pty-wrapper` (not `claude -p`) so it can drive the interactive Claude Code session while still returning print-style output. This is controlled by `AgentConfig.claude_use_pty_wrapper` (default `True`).
   - Gemini calls are dispatched to the `agy` CLI, not the `gemini` CLI. The prompt is passed via stdin using `--print -` (not as an argv value) to avoid `ARG_MAX` limits on long prompts and to keep the invocation free of `shell=True`.

3. **Streaming variants** — same agent set, but yield `AgentStreamEvent`s as the process runs:
   - `coding_agents_streaming` is the top-level streaming dispatcher. It reuses `coding_agents_cli`'s command/env builders and Claude path is delegated to `claude_pty_wrapper_streaming`.
   - `claude_pty_wrapper_streaming` shells out to `claude-pty-wrapper -p --output-format stream-json` and converts the wrapper's JSONL into readable progress events plus a clean final assistant string. Do not switch this module to `claude -p` — the wrapper path is intentional.

`openrouter` is independent of the agent-CLI stack: `OpenRouterChatClient.complete(...)` posts to OpenRouter's chat completions endpoint, with a `RANDOM_FREE` model option that picks from `data/openrouter_free_models.txt` and retries across models on failure.

## Conventions specific to this repo

- New runners or wrappers should call `agents.env.build_env()` for their subprocess env — never construct one ad hoc — so cron/LaunchAgent execution keeps working.
- Add new agent options to `AgentConfig` with the agent-name prefix and resolve defaults in `__post_init__`; do not introduce parallel kwargs on `run_with_config`.
- Free-model list lives in `data/openrouter_free_models.txt` (one model id per line); `openrouter.py` reads it relative to its own file.
