"""Environment helpers for running processes in cron/LaunchAgent contexts.

Provides utilities to build environment dicts that include vars from
~/.zshenv (paths) and ~/.config/secrets.env (API keys).
"""

from __future__ import annotations

import getpass
import os
import subprocess
from pathlib import Path


def build_env() -> dict[str, str]:
    """Build environment dict by sourcing ~/.zshenv and ~/.config/secrets.env.

    Sources:
    - ~/.zshenv: PATH and other non-sensitive env vars
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

    # Source ~/.zshenv and ~/.config/secrets.env
    home = Path(env["HOME"])
    env_files = [home / ".zshenv", home / ".config/secrets.env"]
    sources = [f"source {f}" for f in env_files if f.exists()]

    if sources:
        try:
            result = subprocess.run(
                ["/bin/zsh", "-c", " && ".join(sources) + " && env"],
                capture_output=True,
                text=True,
                timeout=5,
                env={"HOME": env["HOME"], "USER": env.get("USER", "")},
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if "=" in line:
                        key, _, value = line.partition("=")
                        env[key] = value
        except Exception:
            pass

    return env
