# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Codex, OpenClaw, Hermes, and similar tools) when working with code in this repository.

## Project

`agents` is a small Python library of reusable utilities for driving AI coding-agent CLIs (Codex, Claude Code, Gemini/`agy`) and for calling chat-completion APIs (OpenRouter and DeepSeek). It is consumed by other local projects as a package — modules are imported as `from agents import <module>` (resolved via the `~/projects` editable install, per the global CLAUDE.md).

## Common commands

- Run tests: `/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python -m pytest tests/`
- Run a single test: `... -m pytest tests/test_openrouter.py::test_openrouter_client_sends_chat_completion_request`
- Source Codex bin/node discovery (finds newest NVM node with `codex` installed and exports `CODEX_BIN` / `CODEX_NODE_BIN`): `source scripts/codex-env.sh`
- Run an agent from the shell: `python agents_cli.py [-a claude|codex|gemini] [-m MODEL] "prompt"` — prompt can also be piped via stdin or supplied as a heredoc. Use `--fallback claude,codex,gemini` to walk a fallback chain.
- Invoke native code review: add `--codex-review` to pass the prompt through headless `codex exec review`, or `--claude-review-command code-review|review` (and an optional PR `--claude-review-target` for review).
- Long runs (> ~6 min) launched from a cmux-hosted Claude Code session: add `--detach OUTPUT_BASE` — runs in its own session (immune to the cmux idle reaper), answer in `OUTPUT_BASE.out`, `.exitcode` written last (poll for it). Short runs don't need it.

## Architecture (details: docs/architecture.md)

- **`env.build_env()`** — sources `~/.zshenv` + `~/.config/secrets.env` into an env dict for `subprocess.run(env=...)`; the single entry point for PATH/API keys under launchd/cron.
- **`coding_agents_cli`** — unified blocking interface for the three agent CLIs: `AgentConfig` dataclass (all options, including native-review routing, are agent-name-prefixed), `run_with_config()`, `run_with_config_and_fallback()` (Codex → Claude → Gemini).
- **`coding_agents_streaming`** / **`claude_pty_wrapper_streaming`** — same agents, yielding `AgentStreamEvent`s as the process runs.
- **`openrouter`** / **`deepseek`** — small, self-contained, stdlib-only chat-completion clients (deliberately share no code).
- **`openclaw`** — messaging wrapper over the `openclaw` CLI (`send_message` / `send_discord`); no network call or token in this repo.
- `agents_cli.py` — thin argparse shell entry point over `coding_agents_cli`.

## Rules that change what you do

- Blocking Claude runs default to **direct `claude -p`** (`claude_use_pty_wrapper=False`): the pty-wrapper leaks/hangs on plugin MCP servers in headless runs (wrapper teardown bug — see docs/architecture.md). Keep `-p` the default for unattended callers; `claude_disable_mcp=True` / `--no-mcp` suppresses MCP entirely.
- Do **not** switch `claude_pty_wrapper_streaming` to `claude -p` — the wrapper path is intentional there.
- Gemini dispatches to the `agy` CLI (not `gemini`), prompt via stdin `--print -` (avoids `ARG_MAX`, no `shell=True`).
- New runners or wrappers must call `agents.env.build_env()` for their subprocess env — never construct one ad hoc — so cron/LaunchAgent execution keeps working.
- Add new agent options to `AgentConfig` with the agent-name prefix and resolve defaults in `__post_init__`; do not introduce parallel kwargs on `run_with_config`.
- Free-model list lives in `data/openrouter_free_models.txt` (one model id per line); `openrouter.py` reads it relative to its own file.
