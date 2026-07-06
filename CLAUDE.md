# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Codex, OpenClaw, Hermes, and similar tools) when working with code in this repository.

## Project

`agents` is a small Python library of reusable utilities for driving AI coding-agent CLIs (Codex, Claude Code, Gemini/`agy`) and for calling chat-completion APIs (OpenRouter and DeepSeek). It is consumed by other local projects as a package — modules are imported as `from agents import <module>`.

## Environment

- Use the `ml` conda env interpreter from the global `~/.claude/CLAUDE.md` for all Python/pytest invocations.
- The library is imported as the top-level package `agents`. The parent directory of this repo must be on `sys.path` (this is how sibling projects pick it up); do not add path hacks inside modules.

## Common commands

- Run tests: `/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python -m pytest tests/`
- Run a single test: `... -m pytest tests/test_openrouter.py::test_openrouter_client_sends_chat_completion_request`
- Source Codex bin/node discovery (finds newest NVM node with `codex` installed and exports `CODEX_BIN` / `CODEX_NODE_BIN`): `source scripts/codex-env.sh`
- Run an agent from the shell: `python agents_cli.py [-a claude|codex|gemini] [-m MODEL] "prompt"` — prompt can also be piped via stdin or supplied as a heredoc. Use `--fallback claude,codex,gemini` to walk a fallback chain.

## Architecture

Three layers, all in the top-level `agents/` package:

1. **`env.build_env()`** — sources `~/.zshenv` and `~/.config/secrets.env` and returns an env dict suitable for `subprocess.run(env=...)`. This is the single entry point for getting a usable PATH and API keys inside launchd/cron contexts where the parent env is minimal. Other modules in this repo call it rather than reaching into `os.environ` directly.

2. **`coding_agents_cli`** — unified blocking interface for the three agent CLIs. The shape is:
   - `AgentType` enum (`codex`/`claude`/`gemini`) and `DEFAULT_MODELS` per agent.
   - `AgentConfig` dataclass: all options for all agents live here, prefixed by agent (`claude_*`, `codex_*`). `__post_init__` resolves defaults so callers never need a getter.
   - `run_with_config(prompt, config)` dispatches to per-agent internal runners; `run_with_config_and_fallback` walks `DEFAULT_AGENT_FALLBACK_ORDER` (Codex → Claude → Gemini).
   - Claude defaults to **direct `claude -p`** (`AgentConfig.claude_use_pty_wrapper` default `False`). Set it `True` to route through `claude-pty-wrapper` and drive the interactive Claude Code session while still returning print-style output.
     - **Why `-p` is the default (caveat):** the pty-wrapper drives the *interactive* Claude Code session, which starts plugin-provided MCP stdio servers (e.g. Playwright / chrome-devtools via `npm exec`). Those servers don't shut down on teardown and keep the wrapper's PTY slave open, so the master read never sees EOF — an otherwise-**finished** headless run hangs until its hard timeout (`subprocess.run(timeout=timeout+10)`) kills it, and the killed wrapper orphans the MCP servers (they accumulate as long-lived background processes). Direct `claude -p` neither hangs nor leaks (it reaps its own child MCP servers on exit), so it is the default for the unattended/launchd callers of this library (news summarizers, notes monitor, etc.). This is a bug in the wrapper's child-process teardown, not in Claude itself.
     - `AgentConfig.claude_disable_mcp` (default `False`) / `agents_cli --no-mcp` runs Claude with `--strict-mcp-config` + empty `--mcp-config` so no MCP servers start at all — extra hygiene for agents that need zero MCP tools (e.g. the notes monitor). On the `-p` path `--strict-mcp-config` fully suppresses plugin MCP; the wrapper forwards the flags too, but only `-p` reliably avoids spawning them.
   - Gemini calls are dispatched to the `agy` CLI, not the `gemini` CLI. The prompt is passed via stdin using `--print -` (not as an argv value) to avoid `ARG_MAX` limits on long prompts and to keep the invocation free of `shell=True`.

3. **Streaming variants** — same agent set, but yield `AgentStreamEvent`s as the process runs:
   - `coding_agents_streaming` is the top-level streaming dispatcher. It reuses `coding_agents_cli`'s command/env builders and Claude path is delegated to `claude_pty_wrapper_streaming`.
   - `claude_pty_wrapper_streaming` shells out to `claude-pty-wrapper -p --output-format stream-json` and converts the wrapper's JSONL into readable progress events plus a clean final assistant string. Do not switch this module to `claude -p` — the wrapper path is intentional.

`agents_cli.py` is a thin argparse-based shell entry point on top of `coding_agents_cli`. It accepts the prompt as an argv arg or via stdin, builds a single `AgentConfig`, and delegates to `run_with_config` — or, when `--fallback` is given, builds one `AgentConfig` per agent in the list and calls `run_with_config_and_fallback`. Only the first config in the fallback list receives `--model`; the rest fall back to their per-agent defaults.

`openrouter` is independent of the agent-CLI stack: `OpenRouterChatClient.complete(...)` posts to OpenRouter's chat completions endpoint, with a `RANDOM_FREE` model option that picks from `data/openrouter_free_models.txt` and retries across models on failure.

`deepseek` is a parallel, standalone client for DeepSeek's OpenAI-compatible chat completions endpoint: `DeepSeekChatClient.complete(...)` reads `DEEPSEEK_API_KEY`, defaults to model `deepseek-v4-flash`, and supports `max_tokens`, a `thinking` flag, and `reasoning_effort`. Known model ids live in `DeepSeekModel` (`deepseek-v4-pro`, `deepseek-v4-flash`, `deepseek-chat`, `deepseek-reasoner`). `thinking` defaults to `False`, which sends `{"thinking":{"type":"disabled"}}` to turn off the reasoning pass on hybrid V4 models (faster/cheaper); pass `thinking=True` to send `{"thinking":{"type":"enabled"}}`. When thinking is on, `reasoning_effort` sizes the reasoning depth — DeepSeek only honors `high` (default) and `max` (see `ReasoningEffort`; legacy `low`/`medium`/`xhigh` are server-mapped onto those), and it is ignored when thinking is off. Thinking mode also ignores `temperature` (no error, no effect). It shares no code with `openrouter` — both are deliberately small, self-contained, stdlib-only (`urllib`) clients.

## Conventions specific to this repo

- New runners or wrappers should call `agents.env.build_env()` for their subprocess env — never construct one ad hoc — so cron/LaunchAgent execution keeps working.
- Add new agent options to `AgentConfig` with the agent-name prefix and resolve defaults in `__post_init__`; do not introduce parallel kwargs on `run_with_config`.
- Free-model list lives in `data/openrouter_free_models.txt` (one model id per line); `openrouter.py` reads it relative to its own file.
