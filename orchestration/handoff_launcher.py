"""Safe cmux/tmux launcher for local and SSH-hosted handoff workers.

The terminal is sent one shell command containing only two shell-quoted paths:
an immutable private wrapper and its private JSON configuration. User content is
loaded by Python and passed directly to the agent as argv, never interpreted by a
shell. Remote requests send kickoff bytes and launch options as JSON over SSH
stdin; only a fixed, shell-quoted module command crosses the remote shell boundary.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from agents import env
from agents.orchestration import handoff
from agents.orchestration import handoff_registry


CMUX_DEFAULT = "/Applications/cmux.app/Contents/Resources/bin/cmux"
CMUX_APP_NAME = "cmux"
LSAPPINFO_DEFAULT = "/usr/bin/lsappinfo"
REMOTE_PYTHON_DEFAULT = "python3"
KIMI_SUBMIT_SETTLE_SECONDS = 0.5
TUI_SUBMIT_SETTLE_SECONDS = 0.7
# A typed orchestrator doorbell lands in the same composer the human types
# into, so it is gated on what that composer currently holds.
COMPOSER_CLEAR = "clear"      # nothing typed: send now
COMPOSER_BUSY = "busy"        # a draft is being edited: defer to a later poll
COMPOSER_FORCED = "forced"    # a parked draft: send, and warn about the draft
COMPOSER_STABLE_SECONDS = 10.0
COMPOSER_PROBE_SECONDS = 1.0
DEFAULT_READINESS_TIMEOUT = 120.0
REMOTE_CODEX_STARTUP_TIMEOUT = 30.0
REMOTE_FOLDER_TRUST_SCROLLBACK_LINES = 120
# Local Codex, unlike remote, shows its folder-trust dialog in the launched
# surface itself; the launcher clears it before releasing the orchestrator.
LOCAL_CODEX_TRUST_TIMEOUT = 30.0
CODEX_TRUST_RENDER_GRACE = 3.0
REMOTE_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,254}$")
AGENT_DEFAULTS = {
    "claude": ("opus", "high"),
    "codex": ("gpt-5.6-terra", "xhigh"),
    "kimi": ("kimi-code/k3", "max"),
}
ORCHESTRATOR_DOORBELL_TITLE = "Handoff orchestrator pending"


FORCED_DOORBELL_PREAMBLE = (
    # The composer inserts at the cursor, so without a leading separator this
    # runs straight into the final word of the human's draft.
    " ⟦handoff doorbell⟧ "
    "Ignore everything before this sentence: it is an unfinished draft the "
    "human was still typing when this doorbell had to be delivered. Do not act "
    "on that draft; quote it back verbatim at the top of your reply so the "
    "human can recover it, then handle the pointer that follows. "
)


def orchestrator_doorbell_body(orchestrator_id: str, *, forced: bool = False) -> str:
    """Return the opaque snapshot-pointer text for an orchestrator doorbell.

    ``forced`` prefixes a fixed warning used when the doorbell is typed into a
    composer that already holds a human draft: submitting clears the composer,
    so the draft is only recoverable if the orchestrator quotes it back.  Both
    variants remain fixed templates carrying no event bodies or credentials,
    and the pointer sentence stays a contiguous suffix so echo verification can
    look for it alone.
    """
    pointer = (
        f"Check handoff orchestrator {orchestrator_id}; "
        "pending worker outbox events are recorded."
    )
    return f"{FORCED_DOORBELL_PREAMBLE}{pointer}" if forced else pointer


def _compact_terminal_text(value: str) -> str:
    return "".join(value.split())


def _kimi_instruction_count(screen: str, instruction: str) -> int:
    welcome_position = screen.rfind("Welcome to Kimi Code!")
    visible_agent_output = screen[welcome_position:] if welcome_position >= 0 else screen
    return _compact_terminal_text(visible_agent_output).count(
        _compact_terminal_text(instruction),
    )


def _tui_submit_pending(screen: str, text: str) -> bool:
    """Heuristic: the typed text still sits near the bottom of the screen.

    Agent TUIs keep their input composer on the last screen lines; text that
    remains there after a synthetic Enter was most likely coalesced into a
    bracketed paste and never submitted.  A transcript echo of a *successful*
    submit can also appear near the bottom, so a positive here only justifies
    a harmless retry Enter (an empty-composer Enter is a no-op), never a
    failure verdict.
    """
    tail = [line for line in screen.splitlines() if line.strip()][-10:]
    return _compact_terminal_text(text) in _compact_terminal_text("\n".join(tail))


COMPOSER_PROMPT_RE = re.compile(r"^\s*(?:[│|]\s*)?[>❯›]\s?(.*?)\s*(?:[│|])?\s*$")
COMPOSER_BORDER_CHARS = set("─━-│|┌┐└┘├┤╭╮╯╰ ")


def _is_box_border(line: str) -> bool:
    return bool(line.strip()) and set(line) <= COMPOSER_BORDER_CHARS


def composer_draft(screen: str) -> str:
    """Return whatever currently sits in the agent TUI's input composer.

    Agent TUIs render the composer as the bottom-most prompt marker inside a
    box, with wrapped continuation lines beneath it and a status line outside
    the closing border.  Scanning upward from the bottom finds the composer
    rather than a transcript line that merely starts with ``>``.  An empty
    string means nothing is typed; an unrecognized screen also reads as empty
    so an unfamiliar TUI keeps today's send-immediately behavior instead of
    silently withholding every doorbell.
    """
    lines = screen.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        match = COMPOSER_PROMPT_RE.match(lines[index])
        if match is None:
            continue
        parts = [match.group(1)]
        for line in lines[index + 1:]:
            if _is_box_border(line):
                break
            parts.append(line.strip(" │|"))
        return " ".join(part.strip() for part in parts if part.strip())
    return ""


def _composer_guard(
    capture: Callable[[], str],
    has_user_focus: Callable[[], bool],
    *,
    stable_seconds: float = COMPOSER_STABLE_SECONDS,
    probe_seconds: float = COMPOSER_PROBE_SECONDS,
) -> str:
    """Decide whether a typed doorbell may enter this composer right now.

    An empty composer is sent into immediately, which covers both an idle
    agent and a running turn (input typed mid-turn simply queues).  A draft on
    an unfocused surface cannot be receiving keystrokes, so it is forced at
    once rather than waiting out a timer the human is not participating in.  A
    draft on the focused surface is watched for a short while: any edit defers
    to a later poll, and text that never changes is treated as parked and
    forced, so a doorbell is delayed but never abandoned.
    """
    draft = composer_draft(capture())
    if not draft:
        return COMPOSER_CLEAR
    if not has_user_focus():
        return COMPOSER_FORCED
    for _ in range(max(1, round(stable_seconds / probe_seconds))):
        time.sleep(probe_seconds)
        current = composer_draft(capture())
        if not current:
            return COMPOSER_CLEAR
        if current != draft:
            return COMPOSER_BUSY
    return COMPOSER_FORCED


def _macos_frontmost_app(binary: str = LSAPPINFO_DEFAULT) -> str | None:
    """Return the frontmost application's display name, or None if unknown.

    ``lsappinfo`` needs no Accessibility grant and never prompts, unlike the
    System Events route.
    """
    try:
        front = _run([binary, "front"], check=False)
        if front.returncode != 0 or not front.stdout.strip():
            return None
        info = _run([binary, "info", "-only", "name", front.stdout.strip()], check=False)
    except AdapterError:
        return None
    if info.returncode != 0:
        return None
    match = re.search(r'"LSDisplayName"\s*=\s*"([^"]*)"', info.stdout)
    return match.group(1) if match else None


def _kimi_input_ready(screen: str) -> bool:
    return any(
        re.match(r"^\s*(?:[│|]\s*)?>\s*(?:[│|])?\s*$", line)
        for line in screen.splitlines()
    )


def _kimi_instruction_accepted(
    screen: str,
    instruction: str,
    *,
    previous_count: int,
) -> bool:
    return (
        _kimi_instruction_count(screen, instruction) > previous_count
        and _kimi_input_ready(screen)
    )


def _wrapper_source(python_executable: str | None = None) -> str:
    executable = python_executable or sys.executable
    if "\n" in executable or "\r" in executable:
        raise AdapterError("Python executable path contains a newline")
    return (
        f"#!{executable}\n"
        "from agents.orchestration import handoff_launcher\n"
        "handoff_launcher.exec_from_config()\n"
    )


class AdapterError(handoff.HandoffError):
    def __init__(self, message: str):
        super().__init__(message, 6)


def _process_env() -> dict[str, str]:
    return env.build_env()


def _run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, env=_process_env(), timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AdapterError(f"session adapter failed: {exc}") from exc
    if check and result.returncode != 0:
        raise AdapterError(f"session adapter command failed ({result.returncode}): {' '.join(argv[:2])}: {result.stderr.strip()}")
    return result


def _remote_host(value: str) -> str:
    if not REMOTE_HOST_RE.fullmatch(value):
        raise handoff.HandoffError("remote host must be an SSH hostname, alias, or user@host without options", 2)
    return value


def _validate_orchestrator_id(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = uuid.UUID(value)
    except (TypeError, ValueError) as exc:
        raise handoff.HandoffError("orchestrator_id must be a UUIDv4 string", 2) from exc
    if parsed.version != 4 or str(parsed) != value:
        raise handoff.HandoffError(
            "orchestrator_id must be a lowercase canonical UUIDv4", 2,
        )
    return value


def _orchestrator_id_from_state(path: Path | None) -> str | None:
    if path is None:
        return None
    path = Path(path)
    if not path.is_absolute():
        raise handoff.HandoffError("orchestrator state path must be absolute", 2)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and "coordinator_id" in value and "orchestrator_id" not in value:
            handoff._pre_orchestrator_rename("coordinator_id", path)  # noqa: SLF001
        orchestrator_id = value["orchestrator_id"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise handoff.HandoffError(f"invalid orchestrator state {path}: {exc}", 5) from exc
    try:
        return _validate_orchestrator_id(orchestrator_id)
    except handoff.HandoffError as exc:
        raise handoff.HandoffError("orchestrator state has an invalid orchestrator_id", 5) from exc


def _run_ssh(
    host: str,
    remote_argv: list[str],
    *,
    stdin: str,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    host = _remote_host(host)
    command = shlex.join(remote_argv)
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
            input=stdin,
            capture_output=True,
            text=True,
            env=_process_env(),
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AdapterError(f"remote SSH launch failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise AdapterError(f"remote SSH launch failed ({result.returncode}): {detail}")
    return result


class TmuxAdapter:
    def __init__(self, binary: str = "tmux"):
        self.binary = binary

    def launch(self, name: str, cwd: Path, terminal_command: str, workspace: str | None = None) -> str:
        if _run([self.binary, "has-session", "-t", name], check=False).returncode == 0:
            raise handoff.HandoffError(f"tmux session already exists: {name}", 4)
        _run([self.binary, "new-session", "-d", "-s", name, "-c", str(cwd)])
        self._type(name, terminal_command)
        return name

    def _type(self, handle: str, text: str) -> None:
        _run([self.binary, "send-keys", "-t", handle, "-l", text])
        _run([self.binary, "send-keys", "-t", handle, "Enter"])

    def _type_tui(self, handle: str, text: str, probe: str | None = None) -> None:
        # Agent TUIs coalesce a same-burst text+Enter into a bracketed paste,
        # turning Enter into an input newline and leaving the text unsent.
        # Settle between the literal insert and the submit, then retry the
        # submit once if the text still sits in the composer.  Keep _type()
        # for shell commands, which have no paste-detection hazard.
        _run([self.binary, "send-keys", "-t", handle, "-l", text])
        time.sleep(TUI_SUBMIT_SETTLE_SECONDS)
        _run([self.binary, "send-keys", "-t", handle, "Enter"])
        time.sleep(TUI_SUBMIT_SETTLE_SECONDS)
        if _tui_submit_pending(self.capture(handle), probe or text):
            _run([self.binary, "send-keys", "-t", handle, "Enter"])

    def probe(self, handle: str, expected_agent: str) -> bool:
        if _run([self.binary, "has-session", "-t", handle], check=False).returncode != 0:
            return False
        result = _run(
            [self.binary, "list-panes", "-t", handle, "-F", "#{pane_dead}\t#{pane_pid}\t#{pane_current_command}"],
            check=False,
        )
        if result.returncode != 0:
            return False
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 3 or fields[0] != "0":
                continue
            command = fields[2].lower()
            if expected_agent in command:
                return True
            # Codex is commonly a Node entry point; confirm the pane leader's
            # full argv rather than treating any live shell as worker liveness.
            ps = _run(["ps", "-o", "command=", "-p", fields[1]], check=False)
            if ps.returncode == 0 and expected_agent in ps.stdout.lower():
                return True
        return False

    def capture(self, handle: str) -> str:
        return _run([self.binary, "capture-pane", "-p", "-t", handle, "-S", "-2000"]).stdout

    def doorbell(self, handle: str, run_id: str, inbox_seq: int) -> None:
        self._type_tui(handle, f"Check handoff run {run_id}; inbox now through seq {inbox_seq}.")

    def orchestrator_doorbell(
        self, handle: str, orchestrator_id: str, *, forced: bool = False,
    ) -> None:
        self._type_tui(
            handle,
            orchestrator_doorbell_body(orchestrator_id, forced=forced),
            probe=orchestrator_doorbell_body(orchestrator_id),
        )

    def composer_guard(self, handle: str) -> str:
        # tmux reports no dependable per-pane focus for a detached, multi-client,
        # or SSH-hosted session, so every draft goes through the stability timer.
        return _composer_guard(lambda: self.capture(handle), lambda: True)

    def press_enter(self, handle: str) -> None:
        _run([self.binary, "send-keys", "-t", handle, "Enter"])

    def send_literal(self, handle: str, text: str) -> bool:
        previous_count = _kimi_instruction_count(self.capture(handle), text)
        _run([self.binary, "send-keys", "-t", handle, "-l", text])
        # Kimi's interactive input widget does not reliably submit on tmux's
        # synthetic Enter key (notably when extended-keys is disabled).  LF is
        # the preferred terminal-level submit sequence.  Some Kimi versions
        # instead leave LF in the input widget, so verify and fall back once to
        # Enter.  Keep _type() on Enter for shell commands and doorbells.
        _run([self.binary, "send-keys", "-t", handle, "C-j"])
        time.sleep(KIMI_SUBMIT_SETTLE_SECONDS)
        if _kimi_instruction_accepted(
            self.capture(handle), text, previous_count=previous_count,
        ):
            return True
        _run([self.binary, "send-keys", "-t", handle, "Enter"])
        time.sleep(KIMI_SUBMIT_SETTLE_SECONDS)
        return _kimi_instruction_accepted(
            self.capture(handle), text, previous_count=previous_count,
        )

    def rescue_command(self, handle: str) -> str:
        return f"tmux capture-pane -p -t {shlex.quote(handle)} -S -2000"


class CmuxAdapter:
    def __init__(self, binary: str = CMUX_DEFAULT):
        self.binary = binary
        self.workspace: str | None = None

    def launch(self, name: str, cwd: Path, terminal_command: str, workspace: str | None = None) -> str:
        if not workspace:
            # ``current-workspace`` follows cmux's globally selected workspace,
            # which can differ from the workspace that owns this orchestrator's
            # terminal.  The inherited ID is scoped to that exact terminal, so
            # prefer it for worker placement.
            workspace = os.environ.get("CMUX_WORKSPACE_ID")
        if not workspace:
            workspace = _run([self.binary, "current-workspace"]).stdout.strip()
        if not workspace:
            raise AdapterError("cmux did not report a current workspace")
        self.workspace = workspace
        result = _run([
            self.binary, "--id-format", "both", "new-surface", "--type", "terminal",
            "--workspace", workspace, "--focus", "false",
        ])
        match = re.search(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", result.stdout)
        if not match:
            raise AdapterError(f"could not parse cmux surface UUID: {result.stdout.strip()}")
        handle = match.group(0).lower()
        _run([self.binary, "rename-tab", "--workspace", workspace, "--surface", handle, name], check=False)
        self._type(handle, terminal_command)
        return handle

    def _workspace_argv(self) -> list[str]:
        # Without a resolved workspace the flag is omitted and the cmux CLI
        # resolves the surface's workspace server-side from --surface alone.
        return ["--workspace", self.workspace] if self.workspace else []

    def _type(self, handle: str, text: str) -> None:
        workspace = self._workspace_argv()
        _run([self.binary, "send", *workspace, "--surface", handle, text])
        _run([self.binary, "send-key", *workspace, "--surface", handle, "enter"])

    def _type_tui(self, handle: str, text: str, probe: str | None = None) -> None:
        # Same paste-coalescing hazard as TmuxAdapter._type_tui: settle before
        # the submit, then retry once if the text still sits in the composer.
        workspace = self._workspace_argv()
        _run([self.binary, "send", *workspace, "--surface", handle, text])
        time.sleep(TUI_SUBMIT_SETTLE_SECONDS)
        _run([self.binary, "send-key", *workspace, "--surface", handle, "enter"])
        time.sleep(TUI_SUBMIT_SETTLE_SECONDS)
        if _tui_submit_pending(self.capture(handle), probe or text):
            _run([self.binary, "send-key", *workspace, "--surface", handle, "enter"])

    def probe(self, handle: str, expected_agent: str) -> bool:
        surfaces = _run(
            [self.binary, "--id-format", "both", "list-pane-surfaces"],
            check=False,
        )
        if surfaces.returncode != 0 or handle.lower() not in surfaces.stdout.lower():
            return False
        processes = _run([
            self.binary, "--id-format", "both", "top", "--all", "--processes",
            "--flat", "--format", "tsv",
        ], check=False)
        if processes.returncode != 0:
            return False
        # Require both the surface and expected agent in the process inventory;
        # this avoids mistaking a live trust/TUI shell for the agent loop.
        lowered = processes.stdout.lower()
        return handle.lower() in lowered and expected_agent.lower() in lowered

    def capture(self, handle: str) -> str:
        return _run([
            self.binary, "read-screen", *self._workspace_argv(),
            "--surface", handle, "--scrollback",
        ]).stdout

    def doorbell(self, handle: str, run_id: str, inbox_seq: int) -> None:
        self._type_tui(handle, f"Check handoff run {run_id}; inbox now through seq {inbox_seq}.")

    def orchestrator_doorbell(self, handle: str, orchestrator_id: str) -> None:
        """Raise the surface's native visible alert rather than typing input.

        A zero exit from ``cmux send`` only proves the socket accepted the
        write; code-mode and desktop-backed agent surfaces never echo that
        input, so a typed doorbell is silently lost while looking
        successful.  ``cmux notify`` raises a user-visible alert on any
        surface type.  Interactive terminal orchestrators reachable through
        tmux keep the raw-input path on :class:`TmuxAdapter`.
        """
        self.notify(
            handle,
            title=ORCHESTRATOR_DOORBELL_TITLE,
            body=orchestrator_doorbell_body(orchestrator_id),
        )

    def orchestrator_doorbell_input(
        self, handle: str, orchestrator_id: str, *, forced: bool = False,
    ) -> bool:
        """Type the doorbell into the orchestrator surface and confirm the echo.

        A visible alert never pushes a surface-hosted agent to act; typed
        input into an interactive agent TUI does — the text lands in the
        composer, Enter submits it, and the agent processes it as a prompt.
        Code-mode and desktop-backed surfaces instead accept the socket write
        with a zero exit and silently drop it, so the send counts as delivered
        only when the doorbell text is visible on the surface afterward.

        Delivery is verified against the pointer sentence alone, which both
        body variants end with: the forced variant is long enough to wrap over
        more lines than the submit-pending heuristic inspects.
        """
        pointer = orchestrator_doorbell_body(orchestrator_id)
        self._type_tui(
            handle,
            orchestrator_doorbell_body(orchestrator_id, forced=forced),
            probe=pointer,
        )
        return _compact_terminal_text(pointer) in _compact_terminal_text(self.capture(handle))

    def _surface_has_user_focus(self, handle: str) -> bool:
        """Best-effort: can the human be typing into this exact surface now?

        ``list-pane-surfaces`` describes the focused pane of the current
        workspace, so a surface that is absent from that listing is not where
        keystrokes are going, and a listed-but-unselected surface is not
        either.  Both answers hold however busy the keyboard and mouse are
        elsewhere, which a machine-wide idle timer cannot distinguish.  An
        unreadable environment resolves to True so the stability timer decides
        rather than a doorbell being forced into a live draft.
        """
        app = _macos_frontmost_app()
        if app is not None and app != CMUX_APP_NAME:
            return False
        try:
            listing = _run(
                [self.binary, "--id-format", "both", "list-pane-surfaces"], check=False,
            )
        except AdapterError:
            return True
        if listing.returncode != 0:
            return True
        for line in listing.stdout.splitlines():
            if handle.lower() in line.lower():
                return "[selected]" in line or line.lstrip().startswith("*")
        return False

    def composer_guard(self, handle: str) -> str:
        return _composer_guard(
            lambda: self.capture(handle),
            lambda: self._surface_has_user_focus(handle),
        )

    def press_enter(self, handle: str) -> None:
        if not self.workspace:
            raise AdapterError("cmux workspace was not resolved before sending a key")
        _run([
            self.binary, "send-key", "--workspace", self.workspace,
            "--surface", handle, "enter",
        ])

    def notify(self, handle: str | None, *, title: str, body: str) -> None:
        """Raise a native cmux alert when terminal input is unavailable."""
        if not self.workspace and handle is None:
            raise AdapterError("cmux notify requires a workspace or a surface handle")
        command = [
            self.binary, "notify", "--title", title, "--body", body,
            *self._workspace_argv(),
        ]
        if handle is not None:
            command.extend(["--surface", handle])
        _run(command)

    def send_literal(self, handle: str, text: str) -> bool:
        if not self.workspace:
            raise AdapterError("cmux workspace was not resolved before sending input")
        # The shell can echo an early, dropped bootstrap instruction into
        # scrollback before Kimi's TUI is ready.  Only accept a newly visible
        # occurrence so stale scrollback cannot produce a false success.
        previous_count = _kimi_instruction_count(self.capture(handle), text)
        _run([
            self.binary, "send", "--workspace", self.workspace,
            "--surface", handle, text,
        ])
        # Prefer Ctrl-J because some Kimi versions treat synthetic Enter as an
        # input newline.  Kimi 0.27.0 can instead leave Ctrl-J unsubmitted, so
        # inspect the input widget and fall back once to Enter.
        _run([
            self.binary, "send-key", "--workspace", self.workspace,
            "--surface", handle, "ctrl+j",
        ])
        time.sleep(KIMI_SUBMIT_SETTLE_SECONDS)
        if _kimi_instruction_accepted(
            self.capture(handle), text, previous_count=previous_count,
        ):
            return True
        _run([
            self.binary, "send-key", "--workspace", self.workspace,
            "--surface", handle, "enter",
        ])
        time.sleep(KIMI_SUBMIT_SETTLE_SECONDS)
        return _kimi_instruction_accepted(
            self.capture(handle), text, previous_count=previous_count,
        )

    def rescue_command(self, handle: str) -> str:
        workspace = self.workspace
        if not workspace:
            return f"{shlex.quote(self.binary)} read-screen --surface {shlex.quote(handle)} --scrollback"
        return (
            f"{shlex.quote(self.binary)} read-screen --workspace {shlex.quote(workspace)} "
            f"--surface {shlex.quote(handle)} --scrollback"
        )


def _agent_argv(config: dict[str, Any], process_env: dict[str, str]) -> list[str]:
    agent = config["agent"]
    executable = shutil.which(agent, path=process_env.get("PATH"))
    if executable is None:
        raise AdapterError(f"agent executable not found: {agent}")
    if agent == "claude":
        prompt = Path(config["kickoff"]).read_text(encoding="utf-8")
        argv = [executable, "--model", config["model"]]
        if config["effort"]:
            argv.extend(["--effort", config["effort"]])
        argv.extend(["--permission-mode", config["pmode"], prompt])
        return argv
    if agent == "codex":
        prompt = Path(config["kickoff"]).read_text(encoding="utf-8")
        argv = [executable, "-m", config["model"]]
        if config["effort"]:
            argv.extend(["-c", f"model_reasoning_effort={config['effort']}"])
        if config["pmode"] == "bypassPermissions":
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            argv.extend(["-a", "on-request"])
        argv.append(prompt)
        return argv
    if agent == "kimi":
        argv = [executable, "-m", config["model"]]
        if config["pmode"] == "bypassPermissions":
            argv.append("--yolo")
        elif config["pmode"] == "auto":
            argv.append("--auto")
        return argv
    raise handoff.HandoffError(f"unsupported agent: {agent}", 2)


def exec_from_config(config_path: str | None = None) -> None:
    """Replace the private wrapper process with the configured agent."""
    if config_path is None:
        if len(sys.argv) != 2:
            raise SystemExit("usage: private-wrapper CONFIG")
        config_path = sys.argv[1]
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    expected = {"agent", "model", "effort", "pmode", "cwd", "run_dir", "worker_token_file", "kickoff"}
    if set(config) != expected:
        raise SystemExit("invalid private launch config")
    process_env = _process_env()
    process_env["HANDOFF_RUN_DIR"] = config["run_dir"]
    process_env["HANDOFF_WORKER_TOKEN_FILE"] = config["worker_token_file"]
    if config["agent"] == "kimi" and config["effort"]:
        process_env["KIMI_MODEL_THINKING_EFFORT"] = config["effort"]
    os.chdir(config["cwd"])
    argv = _agent_argv(config, process_env)
    os.execvpe(argv[0], argv, process_env)


def _private_launch_files(private_dir: Path, config: dict[str, Any]) -> tuple[Path, Path]:
    wrapper = private_dir / "launch_worker.py"; config_path = private_dir / "launch.json"
    handoff._write_new(wrapper, _wrapper_source().encode(), 0o700)  # noqa: SLF001
    handoff._write_new(config_path, handoff._json_bytes(config), 0o600)  # noqa: SLF001
    handoff._fsync_dir(private_dir)  # noqa: SLF001
    return wrapper, config_path


def wait_ready(adapter: Any, handle: str, expected_agent: str, run_dir: Path, *, timeout: float = 120.0, poll_interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if adapter.probe(handle, expected_agent):
            try:
                if handoff.status(run_dir)["revision"] > 1:
                    return True
            except handoff.HandoffError:
                pass
        time.sleep(poll_interval)
    return False


def wait_process(adapter: Any, handle: str, expected_agent: str, *, timeout: float = 120.0, poll_interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if adapter.probe(handle, expected_agent):
            return True
        time.sleep(poll_interval)
    return False


def wait_kimi_input_ready(
    adapter: Any,
    handle: str,
    *,
    timeout: float = 120.0,
    poll_interval: float = 0.25,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _kimi_input_ready(adapter.capture(handle)):
            return True
        time.sleep(poll_interval)
    return False


def _kimi_kickoff_instruction(run_dir: Path) -> str:
    kickoff_path = json.dumps(str(run_dir / "kickoff.md"), ensure_ascii=False)
    return f"Read the handoff kickoff from {kickoff_path} and follow it completely."


def bootstrap_kimi(
    adapter: Any,
    handle: str,
    *,
    run_dir: Path,
    timeout: float = 120.0,
) -> bool:
    started_at = time.monotonic()
    if not wait_process(adapter, handle, "kimi", timeout=timeout):
        return False
    # Process visibility precedes Kimi's interactive input loop.  Wait for the
    # actual input widget: a fixed delay can still send into startup output,
    # where the instruction is echoed into scrollback and then dropped.  Share
    # one startup budget across both phases so either may consume the time it
    # legitimately needs without doubling the caller's configured timeout.
    remaining = max(0.0, timeout - (time.monotonic() - started_at))
    input_ready = remaining > 0.0 and wait_kimi_input_ready(
        adapter,
        handle,
        timeout=remaining,
    )
    if not input_ready:
        return False
    return adapter.send_literal(handle, _kimi_kickoff_instruction(run_dir))


def _select_backend(backend: str | None, cmux_binary: str) -> str:
    if backend is not None:
        if backend not in {"cmux", "tmux"}:
            raise handoff.HandoffError("backend must be cmux or tmux", 2)
        return backend
    cmux_ok = (
        bool(os.environ.get("CMUX_WORKSPACE_ID"))
        and Path(cmux_binary).is_file()
        and _run([cmux_binary, "ping"], check=False).returncode == 0
    )
    if cmux_ok:
        return "cmux"
    if shutil.which("tmux", path=_process_env().get("PATH")):
        return "tmux"
    raise AdapterError("no backend: cmux socket is unavailable and tmux is not installed")


def launch(
    *, name: str, kickoff: Path, cwd: Path, agent: str = "claude", backend: str | None = None,
    model: str | None = None, effort: str | None = None, pmode: str = "bypassPermissions",
    workspace: str | None = None, inputs: list[str] | None = None,
    state_root: Path | None = None, run_dir: Path | None = None,
    readiness_timeout: float = 120.0, cmux_binary: str = CMUX_DEFAULT,
    confirm_ready: bool = False, retain_orchestrator: bool = False,
    credential_dir: Path | None = None, recorded_transport: str | None = None,
    orchestrator_id: str | None = None,
) -> dict[str, Any]:
    cwd = Path(cwd).resolve(strict=True); kickoff = Path(kickoff).resolve(strict=True)
    orchestrator_id = _validate_orchestrator_id(orchestrator_id)
    if agent not in AGENT_DEFAULTS:
        raise handoff.HandoffError("agent must be claude, codex, or kimi", 2)
    default_model, default_effort = AGENT_DEFAULTS[agent]
    model = default_model if model is None else model
    effort = default_effort if effort is None else effort
    recorded_model = model or "configured-default"
    backend = _select_backend(backend, cmux_binary)
    protocol_transport = recorded_transport or backend

    configured_private_dir = credential_dir or os.environ.get("HANDOFF_CREDENTIAL_DIR")
    if configured_private_dir:
        private_dir = Path(configured_private_dir).resolve(strict=False)
        private_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(private_dir, 0o700)
    else:
        credentials_root = Path.home() / ".local/state/agents/handoff/credentials"
        credentials_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(credentials_root, 0o700)
        private_dir = Path(tempfile.mkdtemp(prefix="handoff-launch-", dir=credentials_root))
        os.chmod(private_dir, 0o700)
    initialized = handoff.initialize(
        workspace=cwd, kickoff=kickoff, harness=agent, model=recorded_model, effort=effort,
        transport=protocol_transport, inputs=inputs or [], state_root=state_root, run_dir=run_dir,
        recovery_token_file=private_dir / "recovery.token",
        orchestrator_token_file=private_dir / "orchestrator.token",
        worker_token_file=private_dir / "worker.token",
    )
    actual_run_dir = Path(initialized["run_dir"])
    config = {
        "agent": agent, "model": model, "effort": effort, "pmode": pmode,
        "cwd": str(cwd), "run_dir": str(actual_run_dir),
        "worker_token_file": str(private_dir / "worker.token"),
        "kickoff": str(actual_run_dir / "kickoff.md"),
    }
    wrapper, config_path = _private_launch_files(private_dir, config)
    terminal_command = f"exec {shlex.quote(str(wrapper))} {shlex.quote(str(config_path))}"
    adapter: Any = CmuxAdapter(cmux_binary) if backend == "cmux" else TmuxAdapter()
    handle = adapter.launch(name, cwd, terminal_command, workspace)
    goal_file = Path(str(kickoff) + ".goal")
    ready: bool | None = None
    rescue: str | None = None
    codex_startup: dict[str, Any] = {}
    if agent == "codex":
        # Local Codex renders its folder-trust dialog in this surface and will
        # never reach the agent loop until it is cleared, so clear it here the
        # way remote launches already do — before readiness or goal delivery.
        _rescue_local_codex_folder_trust(
            codex_startup, adapter=adapter, handle=handle, cwd=cwd,
        )
    if agent == "kimi":
        kickoff_sent = bootstrap_kimi(
            adapter,
            handle,
            run_dir=actual_run_dir,
            timeout=readiness_timeout,
        )
        if not kickoff_sent:
            rescue = adapter.rescue_command(handle)
        elif confirm_ready:
            # Kimi process startup and semantic readiness have separate waits.
            # Refresh ownership between them so a slow startup cannot make the
            # eventual launch-only release fail on an expired lease.
            handoff.control_renew(actual_run_dir, (private_dir / "orchestrator.token").read_bytes())
            ready = wait_ready(adapter, handle, agent, actual_run_dir, timeout=readiness_timeout)
            if not ready:
                rescue = adapter.rescue_command(handle)
    elif goal_file.is_file():
        ready = wait_ready(adapter, handle, agent, actual_run_dir, timeout=readiness_timeout)
        if ready:
            first_line = goal_file.read_text(encoding="utf-8").splitlines()[0]
            adapter.send_literal(handle, first_line)
        else:
            rescue = adapter.rescue_command(handle)
    elif confirm_ready:
        ready = wait_ready(adapter, handle, agent, actual_run_dir, timeout=readiness_timeout)
        if not ready:
            rescue = adapter.rescue_command(handle)
    if codex_startup.get("startup_unconfirmed") and rescue is None:
        rescue = adapter.rescue_command(handle)
    orchestrator_released = False
    if not retain_orchestrator:
        orchestrator_token = (private_dir / "orchestrator.token").read_bytes()
        handoff.control_release(actual_run_dir, orchestrator_token)
        orchestrator_released = True
    result = {
        "run_id": initialized["run"]["run_id"],
        "run_dir": str(actual_run_dir), "transport": protocol_transport,
        "session_transport": backend, "handle": handle,
        "agent": agent, "model": recorded_model, "effort": effort,
        "worker_ready": ready,
        "kickoff_sent": bool(agent != "kimi" or kickoff_sent),
        "goal_sent": bool(agent != "kimi" and goal_file.is_file() and ready),
        "orchestrator_released": orchestrator_released,
        "rescue_command": rescue,
    }
    if agent == "codex":
        result["folder_trust_rescued"] = bool(codex_startup.get("folder_trust_rescued"))
        if codex_startup.get("startup_unconfirmed"):
            result["startup_unconfirmed"] = True
    try:
        handoff_registry.register(
            run_id=result["run_id"], name=name, run_dir=str(actual_run_dir),
            run_uri=str(actual_run_dir), host=None, remote_python=None,
            transport=protocol_transport, session_transport=backend, handle=handle,
            agent=agent, model=recorded_model, effort=effort,
            credential_dir=str(private_dir),
            orchestrator_id=orchestrator_id,
        )
        result["registry_recorded"] = True
    except (handoff.HandoffError, OSError) as exc:
        result["registry_recorded"] = False
        result["registry_error"] = str(exc)
    return result


REMOTE_REQUEST_FIELDS = {
    "name", "kickoff_b64", "goal_b64", "cwd", "agent", "model", "effort",
    "pmode", "inputs", "state_root", "run_dir", "readiness_timeout",
    "confirm_ready", "retain_orchestrator", "credential_dir", "orchestrator_id",
}

# Senders always emit the names above.  The receiver additionally accepts the
# pre-rename spellings because the two ends of an SSH launch are updated
# independently: a launcher from a not-yet-updated checkout would otherwise
# fail with an opaque "invalid remote launch request".
REMOTE_LEGACY_REQUEST_KEYS = {
    "retain_coordinator": "retain_orchestrator",
    "coordinator_id": "orchestrator_id",
}


def _remote_path(value: Any, field: str, *, required: bool = False) -> Path | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise handoff.HandoffError(f"remote {field} must be a non-empty absolute path", 2)
    path = Path(value)
    if not path.is_absolute():
        raise handoff.HandoffError(f"remote {field} must be an absolute path", 2)
    return path


def _decode_remote_file(value: Any, field: str, *, required: bool = False) -> bytes | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise handoff.HandoffError(f"remote {field} must be base64 text", 2)
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise handoff.HandoffError(f"remote {field} is not valid base64", 2) from exc
    if len(decoded) > handoff.MAX_KICKOFF:
        raise handoff.HandoffError(f"remote {field} exceeds {handoff.MAX_KICKOFF} bytes", 2)
    return decoded


def receive_remote_request(request: dict[str, Any]) -> dict[str, Any]:
    """Validate a JSON-over-stdin request and launch it on this SSH host."""
    if isinstance(request, dict):
        request = {REMOTE_LEGACY_REQUEST_KEYS.get(key, key): value for key, value in request.items()}
    if isinstance(request, dict) and set(request) == REMOTE_REQUEST_FIELDS - {"orchestrator_id"}:
        request = {**request, "orchestrator_id": None}
    if not isinstance(request, dict) or set(request) != REMOTE_REQUEST_FIELDS:
        raise handoff.HandoffError("invalid remote launch request", 2)
    if not isinstance(request["name"], str) or not request["name"]:
        raise handoff.HandoffError("remote name must be a non-empty string", 2)
    if request["agent"] not in AGENT_DEFAULTS:
        raise handoff.HandoffError("remote agent must be claude, codex, or kimi", 2)
    for field in ("model", "effort"):
        if request[field] is not None and not isinstance(request[field], str):
            raise handoff.HandoffError(f"remote {field} must be a string or null", 2)
    request["orchestrator_id"] = _validate_orchestrator_id(request["orchestrator_id"])
    if not isinstance(request["pmode"], str) or not request["pmode"]:
        raise handoff.HandoffError("remote pmode must be a non-empty string", 2)
    if not isinstance(request["inputs"], list) or not all(isinstance(item, str) for item in request["inputs"]):
        raise handoff.HandoffError("remote inputs must be a list of paths", 2)
    if not isinstance(request["readiness_timeout"], (int, float)) or request["readiness_timeout"] <= 0:
        raise handoff.HandoffError("remote readiness_timeout must be positive", 2)
    for field in ("confirm_ready", "retain_orchestrator"):
        if not isinstance(request[field], bool):
            raise handoff.HandoffError(f"remote {field} must be boolean", 2)

    cwd = _remote_path(request["cwd"], "cwd", required=True)
    state_root = _remote_path(request["state_root"], "state_root")
    run_dir = _remote_path(request["run_dir"], "run_dir")
    credential_dir = _remote_path(request["credential_dir"], "credential_dir")
    if request["retain_orchestrator"] and credential_dir is None:
        raise handoff.HandoffError("managed remote launch requires credential_dir", 2)
    kickoff_bytes = _decode_remote_file(request["kickoff_b64"], "kickoff", required=True)
    goal_bytes = _decode_remote_file(request["goal_b64"], "goal")

    source_dir = Path(tempfile.mkdtemp(prefix="handoff-remote-kickoff-"))
    os.chmod(source_dir, 0o700)
    kickoff = source_dir / "kickoff.md"
    try:
        handoff._write_new(kickoff, kickoff_bytes, 0o600)  # noqa: SLF001
        if goal_bytes is not None:
            handoff._write_new(Path(str(kickoff) + ".goal"), goal_bytes, 0o600)  # noqa: SLF001
        return launch(
            name=request["name"], kickoff=kickoff, cwd=cwd,
            agent=request["agent"], backend="tmux", model=request["model"],
            effort=request["effort"], pmode=request["pmode"],
            inputs=request["inputs"], state_root=state_root, run_dir=run_dir,
            readiness_timeout=float(request["readiness_timeout"]),
            confirm_ready=request["confirm_ready"],
            retain_orchestrator=request["retain_orchestrator"],
            credential_dir=credential_dir, recorded_transport="ssh_tmux",
            orchestrator_id=request["orchestrator_id"],
        )
    finally:
        shutil.rmtree(source_dir, ignore_errors=True)


def _ssh_display_command(host: str, remote_argv: list[str]) -> str:
    return shlex.join(["ssh", host, shlex.join(remote_argv)])


def _remote_tmux_pane_command(host: str, handle: str) -> str:
    """Return the exact tmux pane leader command with the handle as a shell arg."""
    result = _run_ssh(
        host,
        [
            "sh", "-c",
            'pane_pid=$(tmux display-message -p -t "$1" "#{pane_pid}") || exit 1; '
            'ps -o command= -p "$pane_pid"',
            "handoff-folder-trust", handle,
        ],
        stdin="",
        timeout=15.0,
    )
    return result.stdout


def _remote_tmux_capture(host: str, handle: str, *, scrollback_lines: int | None = None) -> str:
    """Capture one exact remote tmux pane; no worker state is mutated."""
    remote_argv = ["tmux", "capture-pane", "-p", "-t", handle]
    if scrollback_lines is not None:
        remote_argv.extend(["-S", f"-{scrollback_lines}"])
    return _run_ssh(host, remote_argv, stdin="", timeout=15.0).stdout


def _is_remote_codex_folder_trust_prompt(
    pane_command: str,
    screen: str,
    remote_cwd: Path,
) -> bool:
    """Recognize only Codex's exact trust dialog for the authorized checkout."""
    return (
        "codex" in pane_command.casefold()
        and f"You are in {remote_cwd}" in screen
        and "Do you trust the contents of this directory?" in screen
        and "1. Yes, continue" in screen
        and "2. No, quit" in screen
        and "Press enter to continue" in screen
    )


def _rescue_remote_codex_folder_trust(
    result: dict[str, Any],
    *,
    host: str,
    handle: str,
    remote_cwd: Path,
) -> None:
    """Advance one verified, pre-agent Codex folder-trust dialog at most once."""
    result["folder_trust_rescued"] = False
    if result.get("worker_ready") is not False:
        return
    try:
        pane_command = _remote_tmux_pane_command(host, handle)
        screen = _remote_tmux_capture(
            host, handle, scrollback_lines=REMOTE_FOLDER_TRUST_SCROLLBACK_LINES,
        )
    except AdapterError:
        result["startup_unconfirmed"] = True
        return
    if not _is_remote_codex_folder_trust_prompt(pane_command, screen, remote_cwd):
        result["startup_unconfirmed"] = True
        return

    try:
        _run_ssh(
            host,
            ["tmux", "send-keys", "-t", handle, "C-m"],
            stdin="",
            timeout=15.0,
        )
        time.sleep(TUI_SUBMIT_SETTLE_SECONDS)
        pane_command = _remote_tmux_pane_command(host, handle)
        screen = _remote_tmux_capture(host, handle)
    except AdapterError:
        result["startup_unconfirmed"] = True
        return
    if "codex" not in pane_command.casefold() or _is_remote_codex_folder_trust_prompt(
        pane_command, screen, remote_cwd,
    ):
        result["startup_unconfirmed"] = True
        return

    result["folder_trust_rescued"] = True
    result.pop("rescue_command", None)


def _codex_folder_trust_dialog_present(screen: str) -> bool:
    """True when Codex's exact folder-trust dialog is on screen.

    Matches the fixed dialog literals only; the directory is verified
    separately, because the path line is width-truncated on a terminal.
    """
    return (
        "Do you trust the contents of this directory?" in screen
        and "1. Yes, continue" in screen
        and "2. No, quit" in screen
        and "Press enter to continue" in screen
    )


def _codex_trust_cwd_matches(screen: str, cwd: Path) -> bool:
    """Confirm the trust dialog names the exact directory we launched into.

    Codex prints ``You are in <cwd>``, but a narrow surface truncates or wraps
    that path, so the shown token is only a *prefix* of the real cwd (observed:
    ``…/tmp.HGz7Inppm`` for a cwd ending ``…/tmp.HGz7Inppmd``).  Requiring the
    visible token to be a non-trivial prefix of the authorized cwd both
    tolerates truncation and rejects a dialog for some other directory.
    """
    marker = "You are in "
    index = screen.rfind(marker)
    if index < 0:
        return False
    shown = screen[index + len(marker):].splitlines()[0].strip()
    target = str(cwd)
    return bool(shown) and target.startswith(shown) and len(shown) >= min(len(target), 12)


def _rescue_local_codex_folder_trust(
    result: dict[str, Any],
    *,
    adapter: Any,
    handle: str,
    cwd: Path,
    timeout: float = LOCAL_CODEX_TRUST_TIMEOUT,
    render_grace: float = CODEX_TRUST_RENDER_GRACE,
) -> None:
    """Advance one verified local Codex folder-trust dialog at most once.

    The launcher started Codex in this exact surface at ``cwd``, so a trust
    dialog here for that directory is the authorized one.  A dialog naming any
    other directory is left untouched and flagged for manual handling.  When
    Codex reaches its input loop without ever showing a dialog the directory
    was already trusted, so the rescue returns promptly after a short grace
    rather than blocking for the whole timeout.
    """
    result["folder_trust_rescued"] = False
    deadline = time.monotonic() + timeout
    process_since: float | None = None
    while time.monotonic() < deadline:
        screen = adapter.capture(handle)
        if _codex_folder_trust_dialog_present(screen):
            if not _codex_trust_cwd_matches(screen, cwd):
                result["startup_unconfirmed"] = True
                return
            break
        if adapter.probe(handle, "codex"):
            now = time.monotonic()
            process_since = process_since if process_since is not None else now
            if now - process_since >= render_grace:
                return  # Codex is up with no dialog: the directory was trusted.
        time.sleep(0.25)
    else:
        # Never saw a dialog and Codex never came up within the window.
        if not adapter.probe(handle, "codex"):
            result["startup_unconfirmed"] = True
        return

    adapter.press_enter(handle)
    time.sleep(TUI_SUBMIT_SETTLE_SECONDS)
    if _codex_folder_trust_dialog_present(adapter.capture(handle)):
        result["startup_unconfirmed"] = True
        return
    result["folder_trust_rescued"] = True


def launch_remote(
    *, host: str, remote_python: str, name: str, kickoff: Path,
    remote_cwd: Path, agent: str = "claude", model: str | None = None,
    effort: str | None = None, pmode: str = "bypassPermissions",
    inputs: list[str] | None = None, state_root: Path | None = None,
    run_dir: Path | None = None, readiness_timeout: float | None = None,
    confirm_ready: bool = False, retain_orchestrator: bool = False,
    credential_dir: Path | None = None, orchestrator_id: str | None = None,
) -> dict[str, Any]:
    """Launch through an installed copy of this module on an SSH host."""
    host = _remote_host(host)
    orchestrator_id = _validate_orchestrator_id(orchestrator_id)
    kickoff = Path(kickoff).resolve(strict=True)
    remote_cwd = _remote_path(str(remote_cwd), "cwd", required=True)
    credential_dir = _remote_path(str(credential_dir), "credential_dir") if credential_dir is not None else None
    if retain_orchestrator and credential_dir is None:
        raise handoff.HandoffError(
            "managed remote launch requires HANDOFF_REMOTE_CREDENTIAL_DIR", 2,
        )
    if readiness_timeout is not None:
        effective_readiness_timeout = readiness_timeout
    elif agent == "codex":
        effective_readiness_timeout = REMOTE_CODEX_STARTUP_TIMEOUT
    else:
        effective_readiness_timeout = DEFAULT_READINESS_TIMEOUT
    if effective_readiness_timeout <= 0:
        raise handoff.HandoffError("readiness_timeout must be positive", 2)
    confirm_ready = confirm_ready or agent == "codex"
    goal_file = Path(str(kickoff) + ".goal")
    payload = {
        "name": name,
        "kickoff_b64": base64.b64encode(kickoff.read_bytes()).decode("ascii"),
        "goal_b64": base64.b64encode(goal_file.read_bytes()).decode("ascii") if goal_file.is_file() else None,
        "cwd": str(remote_cwd),
        "agent": agent,
        "model": model,
        "effort": effort,
        "pmode": pmode,
        "inputs": inputs or [],
        "state_root": str(state_root) if state_root is not None else None,
        "run_dir": str(run_dir) if run_dir is not None else None,
        "readiness_timeout": effective_readiness_timeout,
        "confirm_ready": confirm_ready,
        "retain_orchestrator": retain_orchestrator,
        "credential_dir": str(credential_dir) if credential_dir is not None else None,
        "orchestrator_id": orchestrator_id,
    }
    remote_argv = [
        remote_python, "-m", "agents.orchestration.handoff_launcher",
        "--receive-remote-request",
    ]
    completed = _run_ssh(
        host,
        remote_argv,
        stdin=json.dumps(payload, ensure_ascii=False),
        timeout=max(30.0, effective_readiness_timeout + 30.0),
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise AdapterError("remote launcher returned no JSON response")
    try:
        remote_result = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise AdapterError("remote launcher returned invalid JSON") from exc
    if not isinstance(remote_result, dict) or not isinstance(remote_result.get("run_dir"), str):
        raise AdapterError("remote launcher returned an invalid result object")
    remote_run_dir = remote_result["run_dir"]
    remote_handle = remote_result.get("handle")
    if not isinstance(remote_handle, str):
        raise AdapterError("remote launcher did not return a session handle")
    result = dict(remote_result)
    result.update({
        "run_id": result.get("run_id") or Path(remote_run_dir).name,
        "run_dir": f"ssh://{host}{remote_run_dir}",
        "remote_run_dir": remote_run_dir,
        "remote_host": host,
        "transport": "ssh_tmux",
        "session_transport": "tmux",
        "handle": f"ssh://{host}/tmux/{remote_handle}",
        "remote_handle": remote_handle,
    })
    if agent == "codex":
        _rescue_remote_codex_folder_trust(
            result, host=host, handle=remote_handle, remote_cwd=remote_cwd,
        )
    try:
        handoff_registry.register(
            run_id=result["run_id"], name=name, run_dir=remote_run_dir,
            run_uri=result["run_dir"], host=host, remote_python=remote_python,
            transport="ssh_tmux", session_transport="tmux", handle=remote_handle,
            agent=agent,
            model=result.get("model") or model or AGENT_DEFAULTS[agent][0],
            effort=result.get("effort") or effort or AGENT_DEFAULTS[agent][1],
            credential_dir=None,
            orchestrator_id=orchestrator_id,
        )
        result["registry_recorded"] = True
    except (handoff.HandoffError, OSError) as exc:
        result["registry_recorded"] = False
        result["registry_error"] = str(exc)
    if result.get("rescue_command"):
        result["rescue_command"] = _ssh_display_command(
            host,
            ["tmux", "capture-pane", "-p", "-t", remote_handle, "-S", "-2000"],
        )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch a protocol-managed autonomous coding agent")
    parser.add_argument("name"); parser.add_argument("prompt_file", type=Path)
    parser.add_argument("cwd", nargs="?", type=Path)
    parser.add_argument(
        "--agent",
        choices=tuple(AGENT_DEFAULTS),
        default=os.environ.get("HANDOFF_AGENT", "claude"),
    )
    parser.add_argument("--backend", choices=("cmux", "tmux"), default=os.environ.get("HANDOFF_BACKEND") or None)
    parser.add_argument(
        "--remote-host",
        default=os.environ.get("HANDOFF_REMOTE_HOST") or None,
        help="launch on an SSH host that has this agents package, tmux, and the selected agent installed",
    )
    parser.add_argument(
        "--remote-cwd",
        type=Path,
        default=Path(os.environ["HANDOFF_REMOTE_CWD"]) if os.environ.get("HANDOFF_REMOTE_CWD") else None,
        help="absolute worker checkout path on --remote-host",
    )
    parser.add_argument(
        "--remote-python",
        default=os.environ.get("HANDOFF_REMOTE_PYTHON", REMOTE_PYTHON_DEFAULT),
        help="Python executable on --remote-host",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("HANDOFF_MODEL") or None,
        help="override model (defaults: claude=opus, codex=gpt-5.6-terra, kimi=kimi-code/k3)",
    )
    parser.add_argument(
        "--effort",
        default=os.environ.get("HANDOFF_EFFORT") or None,
        help="override reasoning effort (defaults: claude=high, codex=xhigh, kimi=max)",
    )
    parser.add_argument("--pmode", default=os.environ.get("HANDOFF_PMODE", "bypassPermissions"))
    parser.add_argument("--workspace"); parser.add_argument("--input", action="append", default=[])
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument("--state-root", type=Path); destination.add_argument("--run-dir", type=Path)
    parser.add_argument(
        "--readiness-timeout",
        type=float,
        default=None,
        help=(
            "startup ready-check timeout (default: 120s; remote Codex defaults to 30s "
            "for automatic folder-trust rescue)"
        ),
    )
    parser.add_argument("--wait-ready", action="store_true", help="wait for the worker-ready checkpoint")
    parser.add_argument("--retain-orchestrator", "--retain-coordinator", dest="retain_orchestrator", action="store_true", help="retain the orchestrator lease for active monitoring")
    parser.add_argument("--cmux-binary", default=CMUX_DEFAULT)
    parser.add_argument(
        "--orchestrator-state", "--coordinator-state", dest="orchestrator_state",
        type=Path,
        default=Path(os.environ["HANDOFF_ORCHESTRATOR_STATE"])
        if os.environ.get("HANDOFF_ORCHESTRATOR_STATE") else None,
        help="private orchestrator watcher snapshot that owns this launched run",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    actual_argv = sys.argv[1:] if argv is None else argv
    if actual_argv == ["--receive-remote-request"]:
        try:
            request = json.load(sys.stdin)
            print(json.dumps(receive_remote_request(request), ensure_ascii=False, sort_keys=True))
            return 0
        except (json.JSONDecodeError, handoff.HandoffError) as exc:
            exit_code = exc.exit_code if isinstance(exc, handoff.HandoffError) else 2
            print(f"handoff_agent: {exc}", file=sys.stderr)
            return exit_code
    try:
        args = build_parser().parse_args(actual_argv)
        orchestrator_id = _orchestrator_id_from_state(args.orchestrator_state)
        if args.remote_host:
            remote_cwd = args.remote_cwd or args.cwd
            if remote_cwd is None:
                raise handoff.HandoffError("a remote checkout path is required with --remote-host", 2)
            if args.backend is not None or args.workspace is not None:
                raise handoff.HandoffError("--remote-host cannot be combined with --backend or --workspace", 2)
            configured_remote_private = os.environ.get("HANDOFF_REMOTE_CREDENTIAL_DIR")
            result = launch_remote(
                host=args.remote_host, remote_python=args.remote_python,
                name=args.name, kickoff=args.prompt_file, remote_cwd=remote_cwd,
                agent=args.agent, model=args.model, effort=args.effort, pmode=args.pmode,
                inputs=args.input, state_root=args.state_root, run_dir=args.run_dir,
                readiness_timeout=args.readiness_timeout, confirm_ready=args.wait_ready,
                retain_orchestrator=args.retain_orchestrator,
                credential_dir=Path(configured_remote_private) if configured_remote_private else None,
                orchestrator_id=orchestrator_id,
            )
        else:
            result = launch(
                name=args.name, kickoff=args.prompt_file, cwd=args.cwd or Path.cwd(), agent=args.agent,
                backend=args.backend, model=args.model, effort=args.effort, pmode=args.pmode,
                workspace=args.workspace, inputs=args.input, state_root=args.state_root,
                run_dir=args.run_dir,
                readiness_timeout=(
                    args.readiness_timeout
                    if args.readiness_timeout is not None else DEFAULT_READINESS_TIMEOUT
                ),
                cmux_binary=args.cmux_binary, confirm_ready=args.wait_ready,
                retain_orchestrator=args.retain_orchestrator,
                orchestrator_id=orchestrator_id,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if result["rescue_command"]:
            print(f"worker readiness timed out; inspect with: {result['rescue_command']}", file=sys.stderr)
        return 0
    except handoff.HandoffError as exc:
        print(f"handoff_agent: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
