#!/usr/bin/env python3
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
        help="Prompt text. If omitted, reads from stdin. "
             "For long or multiline prompts, pipe via stdin: "
             "echo '...' | agents-cli, or use a heredoc: "
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
        "--no-pty-wrapper",
        action="store_true",
        help="Use claude -p instead of claude-pty-wrapper (claude only).",
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
            coding_agents_cli.AgentConfig(agent=a, model=args.model if a == agents[0] else None, auto_approve=auto_approve)
            for a in agents
        ]
        result = coding_agents_cli.run_with_config_and_fallback(prompt, configs, timeout=args.timeout)
    else:
        config = coding_agents_cli.AgentConfig(
            agent=args.agent,
            model=args.model,
            auto_approve=auto_approve,
            claude_use_pty_wrapper=not args.no_pty_wrapper,
            codex_reasoning_effort=args.codex_reasoning,
            codex_working_dir=args.codex_working_dir,
        )
        result = coding_agents_cli.run_with_config(prompt, config, timeout=args.timeout)

    sys.stdout.write(result.output)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
