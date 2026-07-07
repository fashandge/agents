"""Reusable messaging client backed by the ``openclaw`` CLI.

`openclaw <https://openclaw.app>`_ is the user's unified chat CLI ("All your
chats, one OpenClaw"). It can deliver a message to Discord, Slack, Telegram,
WhatsApp, Signal, iMessage, and many other channels through one command, using
credentials/sessions stored in openclaw's own config. That means **no bot token
or webhook URL lives in this repo** — auth is owned by openclaw and shared with
the rest of the user's automations.

This module is a thin, dependency-free wrapper around ``openclaw message send``:
Python only builds an argv list, launches the binary via :func:`subprocess.run`,
and inspects the exit code. The actual network call happens inside ``openclaw``.

Usage as a library::

    from agents import openclaw
    openclaw.send_discord("nightly monitor: all healthy")          # convenience
    openclaw.send_message("deploy done", channel="slack",           # generic
                          target="channel:C0123ABCD")

Usage as a CLI (handy for shell scripts / agents)::

    python -m agents.openclaw --message "hello"                     # Discord default
    python -m agents.openclaw --channel slack --target channel:C01 --message hi
    python -m agents.openclaw --message-file report.md --dry-run
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

from agents import env

OPENCLAW_BIN = "openclaw"

# Discord #stock-picker-peter — the channel the notes/monitor automations post to.
# Exposed as a named default so callers can opt in without hardcoding the id.
DEFAULT_DISCORD_CHANNEL_ID = "1500308946529947698"

# Discord's hard limit on a single message body.
DISCORD_MAX_CHARS = 2000


def _truncate(message: str, limit: int) -> str:
    """Trim ``message`` to ``limit`` characters, appending a truncation marker."""
    message = message.strip()
    if len(message) <= limit:
        return message
    marker = "\n…(truncated)"
    return message[: limit - len(marker)].rstrip() + marker


def send_message(
    message: str,
    *,
    channel: str,
    target: str,
    media: Optional[str] = None,
    silent: bool = False,
    reply_to: Optional[str] = None,
    thread_id: Optional[str] = None,
    account: Optional[str] = None,
    extra_args: Optional[Sequence[str]] = None,
    dry_run: bool = False,
    timeout: float = 60,
) -> str:
    """Send ``message`` to any openclaw-supported ``channel``/``target``.

    This is the generic primitive; :func:`send_discord` is a convenience wrapper
    over it. No length truncation is applied here — channel-specific limits are
    the caller's concern (see :func:`send_discord` for Discord's 2000-char cap).

    Args:
        message: Message body. Required unless ``media`` is set.
        channel: openclaw channel name, e.g. ``"discord"``, ``"slack"``,
            ``"telegram"``, ``"imessage"``.
        target: Recipient/channel address in the form openclaw expects for the
            channel, e.g. ``"channel:<id>"`` or ``"user:<id>"`` for Discord/Slack,
            an E.164 number for WhatsApp/Signal, a chat id/@username for Telegram.
        media: Optional local path or URL to attach.
        silent: Send without a notification (Telegram + Discord).
        reply_to: Optional message id to reply to.
        thread_id: Optional thread id (e.g. Telegram forum thread).
        account: Optional channel account id (``--account``) when multiple
            accounts are configured for the channel.
        extra_args: Additional raw ``openclaw message send`` flags to append,
            for options this wrapper does not model directly.
        dry_run: Print the payload and skip sending (openclaw ``--dry-run``).
        timeout: Seconds to wait for the openclaw process.

    Returns:
        The stdout captured from ``openclaw``.

    Raises:
        ValueError: If neither ``message`` nor ``media`` is provided.
        RuntimeError: If ``openclaw`` exits non-zero.
    """
    if not message and not media:
        raise ValueError("send_message requires a non-empty message or media.")

    cmd = [
        OPENCLAW_BIN,
        "message",
        "send",
        "--channel",
        channel,
        "--target",
        target,
    ]
    if message:
        cmd += ["--message", message]
    if media:
        cmd += ["--media", media]
    if account:
        cmd += ["--account", account]
    if reply_to:
        cmd += ["--reply-to", reply_to]
    if thread_id:
        cmd += ["--thread-id", thread_id]
    if silent:
        cmd.append("--silent")
    if dry_run:
        cmd.append("--dry-run")
    if extra_args:
        cmd += list(extra_args)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env.build_env(),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"openclaw message send failed ({result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def send_discord(
    message: str,
    channel_id: str = DEFAULT_DISCORD_CHANNEL_ID,
    *,
    media: Optional[str] = None,
    silent: bool = False,
    reply_to: Optional[str] = None,
    extra_args: Optional[Sequence[str]] = None,
    dry_run: bool = False,
    timeout: float = 60,
) -> str:
    """Post ``message`` to a Discord channel via openclaw.

    Convenience wrapper over :func:`send_message` that fixes ``channel="discord"``,
    formats ``channel_id`` into openclaw's ``channel:<id>`` target, and truncates
    the body to Discord's 2000-char limit.

    Args:
        message: Message body (truncated to :data:`DISCORD_MAX_CHARS`).
        channel_id: Discord channel id. Defaults to
            :data:`DEFAULT_DISCORD_CHANNEL_ID` (#stock-picker-peter).
        media: Optional local path or URL to attach.
        silent: Send without a notification.
        reply_to: Optional message id to reply to.
        extra_args: Additional raw ``openclaw message send`` flags to append.
        dry_run: Print the payload and skip sending.
        timeout: Seconds to wait for the openclaw process.

    Returns:
        The stdout captured from ``openclaw``.

    Raises:
        RuntimeError: If ``openclaw`` exits non-zero.
    """
    return send_message(
        _truncate(message, DISCORD_MAX_CHARS),
        channel="discord",
        target=f"channel:{channel_id}",
        media=media,
        silent=silent,
        reply_to=reply_to,
        extra_args=extra_args,
        dry_run=dry_run,
        timeout=timeout,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agents.openclaw",
        description="Send a message via the openclaw CLI (Discord by default).",
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("-m", "--message", help="Message text to send")
    src.add_argument("--message-file", help="Read the message body from this file")
    parser.add_argument(
        "--channel", default="discord", help="openclaw channel (default: discord)"
    )
    parser.add_argument(
        "--target",
        help="Recipient/channel address. For Discord, defaults to "
        "channel:DEFAULT_DISCORD_CHANNEL_ID if omitted.",
    )
    parser.add_argument("--media", help="Local path or URL to attach")
    parser.add_argument("--silent", action="store_true", help="Send without notification")
    parser.add_argument("--reply-to", help="Message id to reply to")
    parser.add_argument("--dry-run", action="store_true", help="Print payload, do not send")
    parser.add_argument("--timeout", type=float, default=60, help="openclaw timeout (s)")
    args = parser.parse_args(argv)

    message = (
        Path(args.message_file).read_text(errors="replace")
        if args.message_file
        else args.message
    )

    try:
        if args.channel == "discord" and not args.target:
            out = send_discord(
                message,
                media=args.media,
                silent=args.silent,
                reply_to=args.reply_to,
                dry_run=args.dry_run,
                timeout=args.timeout,
            )
        else:
            if not args.target:
                parser.error(f"--target is required for channel '{args.channel}'")
            out = send_message(
                message,
                channel=args.channel,
                target=args.target,
                media=args.media,
                silent=args.silent,
                reply_to=args.reply_to,
                dry_run=args.dry_run,
                timeout=args.timeout,
            )
    except Exception as exc:  # noqa: BLE001 — surface any failure to the shell
        print(f"openclaw send failed: {exc}", file=sys.stderr)
        return 1

    if out.strip():
        print(out.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
