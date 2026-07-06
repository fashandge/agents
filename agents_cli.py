#!/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python
"""CLI wrapper for agents.coding_agents_cli."""

import argparse
import sys

from agents import coding_agents_cli


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agents-cli",
        description="Run a coding agent (claude, codex, gemini) with a prompt.",
    )
    parser.add_argument(
        "prompt", nargs="?",
        help="Prompt text (only recommended for short, single-line prompts). "
             "For anything longer, prefer stdin to avoid shell quoting issues "
             "and ARG_MAX limits: echo '...' | agents-cli, or use a heredoc: "
             "agents-cli <<'EOF'\\n...\\nEOF",
    )
    parser.add_argument(
        "-a", "--agent",
        choices=["claude", "codex", "gemini"],
        default="claude",
    )
    parser.add_argument("-m", "--model", default=None)
    parser.add_argument(
        "--no-auto-approve",
        action="store_true",
        help="Disable auto-approve (interactive mode).",
    )
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument(
        "--pty-wrapper",
        action="store_true",
        help="Drive Claude through claude-pty-wrapper (interactive session) "
             "instead of the default `claude -p`. Note: the wrapper starts plugin "
             "MCP servers that can hang headless teardown; only use it when you "
             "need interactive-session behavior.",
    )
    parser.add_argument(
        "--no-mcp",
        action="store_true",
        help="Run Claude with no MCP servers (--strict-mcp-config + empty "
             "--mcp-config). Use for unattended runs that need no MCP tools: "
             "avoids the headless-teardown hang from orphaned stdio MCP servers "
             "holding the PTY open, and stops them leaking as background procs.",
    )
    parser.add_argument(
        "--codex-reasoning",
        choices=["low", "medium", "high"],
        default=None,
    )
    parser.add_argument("--codex-working-dir", default=None)
    parser.add_argument(
        "--fallback",
        type=str,
        default=None,
        metavar="AGENTS",
        help="Comma-separated fallback order, e.g. claude,codex,gemini. "
             "Tries each agent in order until one succeeds.",
    )

    args = parser.parse_args()

    if args.prompt:
        prompt = args.prompt
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read()
    else:
        parser.error("No prompt provided. Pass as argument or pipe via stdin.")

    auto_approve = not args.no_auto_approve

    if args.fallback:
        valid = {"claude", "codex", "gemini"}
        agents = [a.strip() for a in args.fallback.split(",")]
        for a in agents:
            if a not in valid:
                parser.error(f"Unknown agent in --fallback: {a!r} (valid: {', '.join(sorted(valid))})")
        configs = [
            coding_agents_cli.AgentConfig(
                agent=a,
                model=args.model if a == agents[0] else None,
                auto_approve=auto_approve,
                claude_disable_mcp=args.no_mcp,
            )
            for a in agents
        ]
        result = coding_agents_cli.run_with_config_and_fallback(prompt, configs, timeout=args.timeout)
    else:
        config = coding_agents_cli.AgentConfig(
            agent=args.agent,
            model=args.model,
            auto_approve=auto_approve,
            claude_use_pty_wrapper=args.pty_wrapper,
            claude_disable_mcp=args.no_mcp,
            codex_reasoning_effort=args.codex_reasoning,
            codex_working_dir=args.codex_working_dir,
        )
        result = coding_agents_cli.run_with_config(prompt, config, timeout=args.timeout)

    sys.stdout.write(result.output)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
