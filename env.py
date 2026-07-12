"""Environment helpers for running processes in cron/LaunchAgent contexts.

Provides utilities to build environment dicts that include vars from
~/.zshenv (paths) and ~/.config/secrets.env (API keys).
"""

from __future__ import annotations

import getpass
import os
import shlex
import subprocess
import sys
from pathlib import Path


def build_env() -> dict[str, str]:
    """Build environment dict from shell startup configuration and secrets.

    Sources:
    - macOS: ~/.zshenv (PATH and other non-sensitive env vars)
    - Linux: ~/.bashrc (including ~/.bashrc.d fragments)
    - ~/.config/secrets.env: API keys (should be chmod 600)

    Falls back gracefully if files don't exist or fail to source.
    Always ensures USER and HOME are set (required for Claude auth).

    Returns:
        Environment dict suitable for subprocess.run(env=...).
    """
    env = os.environ.copy()

    # Ensure USER is set (required for Claude auth)
    if "USER" not in env:
        try:
            env["USER"] = getpass.getuser()
        except Exception:
            pass

    # Ensure HOME is set
    if "HOME" not in env:
        env["HOME"] = str(Path.home())

    # macOS uses zsh; the OCI/Linux hosts use Bash. Source the same startup
    # file that non-interactive schedulers otherwise miss, so agent subprocesses
    # receive the PATH and conda setup available to an interactive shell.
    home = Path(env["HOME"])
    if sys.platform == "darwin":
        shell = "/bin/zsh"
        startup_file = home / ".zshenv"
    else:
        shell = "/bin/bash"
        startup_file = home / ".bashrc"

    sources: list[str] = []
    if startup_file.exists():
        sources.append(f"source {shlex.quote(str(startup_file))}")
    secrets_file = home / ".config/secrets.env"
    if secrets_file.exists():
        # secrets.env commonly uses bare KEY=value assignments. Enable
        # auto-export while sourcing so `env` (and thus the subprocess dict)
        # receives them without requiring users to change their secrets file.
        quoted_secrets = shlex.quote(str(secrets_file))
        sources.append(f"set -a && source {quoted_secrets} && set +a")

    if sources:
        try:
            result = subprocess.run(
                [shell, "-c", " && ".join(sources) + " && env"],
                capture_output=True,
                text=True,
                timeout=5,
                # A scheduler can invoke Python with no PATH at all. The shell
                # startup files need basic POSIX tools (dirname, grep, awk,
                # etc.) before they can activate conda and construct the final
                # application PATH.
                env={
                    "HOME": env["HOME"],
                    "USER": env.get("USER", ""),
                    "PATH": env.get("PATH", os.defpath),
                },
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if "=" in line:
                        key, _, value = line.partition("=")
                        env[key] = value
        except Exception:
            pass

    return env
