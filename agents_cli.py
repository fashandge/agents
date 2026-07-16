#!/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python
"""CLI wrapper for agents.coding_agents_cli."""

import argparse
import json
import sys

from agents import coding_agents_cli


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agents-cli",
        description="Run a coding agent (claude, codex, gemini) with a prompt.",
        epilog=(
            "environment note:\n"
            "  When agents-cli runs as a background child of a Claude Code session\n"
            "  hosted inside cmux, an agent-process reaper can kill the run ~6 minutes\n"
            "  after the session goes idle. ONLY when the run is expected to be long\n"
            "  (more than ~6 minutes — e.g. high/xhigh reasoning over a large task),\n"
            "  pass --detach OUTPUT_BASE in that environment; short runs don't need it."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
        "--claude-effort",
        choices=["low", "medium", "high", "xhigh", "max"],
        default=None,
        help="Claude reasoning effort (forwarded via --effort). When omitted, "
             "no --effort flag is sent and the ambient CLAUDE_EFFORT env var "
             "(if any) stays in effect. Applies to both `claude -p` and "
             "claude-pty-wrapper runs. Ignored for non-Claude agents.",
    )
    parser.add_argument(
        "--claude-review-command",
        choices=["code-review", "review"],
        default=None,
        help="Invoke an unnamespaced native Claude review command: code-review "
             "for the current working diff, or review for a GitHub pull request.",
    )
    parser.add_argument(
        "--claude-review-target",
        default=None,
        help="Optional PR number or URL passed to --claude-review-command review.",
    )
    parser.add_argument(
        "--codex-reasoning",
        choices=["low", "medium", "high", "xhigh", "max"],
        default=None,
        help="Codex reasoning effort. xhigh/max require a model that honors "
             "extended reasoning (e.g. gpt-5.x reasoning models).",
    )
    parser.add_argument("--codex-working-dir", default=None)
    parser.add_argument(
        "--codex-review",
        action="store_true",
        help="Invoke native headless `codex exec review` with the prompt as "
             "custom review instructions.",
    )
    parser.add_argument(
        "--fallback",
        type=str,
        default=None,
        metavar="AGENTS",
        help="Comma-separated fallback order, e.g. claude,codex,gemini. "
             "Tries each agent in order until one succeeds.",
    )
    parser.add_argument(
        "--detach",
        metavar="OUTPUT_BASE",
        default=None,
        help="Run detached in its own session (double fork + setsid) and exit "
             "immediately, printing one JSON line with the pid and result-file "
             "paths. The final answer goes to OUTPUT_BASE.out, logs/stderr to "
             ".err, and .exitcode is written last (poll for it to detect "
             "completion). Use ONLY for runs expected to exceed ~6 minutes "
             "inside a cmux-hosted Claude Code session (see environment note "
             "below); short runs don't need it.",
    )

    args = parser.parse_args()

    if args.claude_review_target and args.claude_review_command != "review":
        parser.error("--claude-review-target requires --claude-review-command review")
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
                claude_effort=args.claude_effort,
                claude_review_command=args.claude_review_command,
                claude_review_target=args.claude_review_target,
                codex_review=args.codex_review,
            )
            for a in agents
        ]
        runner = lambda: coding_agents_cli.run_with_config_and_fallback(
            prompt, configs, timeout=args.timeout
        )
    else:
        config = coding_agents_cli.AgentConfig(
            agent=args.agent,
            model=args.model,
            auto_approve=auto_approve,
            claude_use_pty_wrapper=args.pty_wrapper,
            claude_disable_mcp=args.no_mcp,
            claude_effort=args.claude_effort,
            claude_review_command=args.claude_review_command,
            claude_review_target=args.claude_review_target,
            codex_reasoning_effort=args.codex_reasoning,
            codex_working_dir=args.codex_working_dir,
            codex_review=args.codex_review,
        )
        runner = lambda: coding_agents_cli.run_with_config(
            prompt, config, timeout=args.timeout
        )

    if args.detach:
        info = coding_agents_cli.run_detached(runner, args.detach)
        print(json.dumps(info))
        sys.exit(0)

    result = runner()
    sys.stdout.write(result.output)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
