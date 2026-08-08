"""Lightweight worker spawning: a new tab, an agent, a prompt, and nothing else.

This is the deliberate opposite of :mod:`handoff_launcher`.  It borrows that
module's terminal adapters and agent argv construction — the parts that are pure
spawn mechanics — and drops everything the durable local-v1 protocol adds: no
run directory, no worker token, no lease, no registry entry, no watcher, no
outbox, no review loop.  Nothing is written under ``~/.local/state/agents/handoff``.

The worker it starts is an ordinary agent session that happens to live in
another tab.  It receives a plain task prompt and is never told that an
orchestrator exists, so it has no protocol vocabulary to follow and nothing to
report back through.  Whoever spawned it reads the tab if they want to know how
it went, exactly as a human would.

The one mechanic kept from the heavy launcher is its safe-exec boundary: the
terminal is sent a command containing only two shell-quoted paths — a private
0700 wrapper and its 0600 JSON config — so prompt content never crosses a shell.
Remote spawns send the prompt as base64 over SSH stdin to this same module
running on the host, which then takes the identical local path there.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from agents import env
from agents.orchestration import handoff
from agents.orchestration import handoff_launcher


SPAWN_STATE_DIRNAME = ".local/state/agents/spawn"
# Kimi is the one agent with no argv prompt, so it is handed a path to read
# instead of having the prompt typed into its composer.
KIMI_BOOTSTRAP_TIMEOUT = 120.0
REMOTE_REQUEST_FIELDS = {
    "label", "prompt_b64", "cwd", "agent", "model", "effort", "pmode",
    "workspace_label",
}
REMOTE_STALE_HINT = (
    "the remote agents checkout has no spawn_worker module; refresh it on the "
    "host with: ssh <host> '~/projects/agents/scripts/update_agents.sh'"
)


def _spawn_root() -> Path:
    root = Path.home() / SPAWN_STATE_DIRNAME
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def _private_dir(label: str) -> Path:
    """Return a fresh 0700 directory for one spawn's private launch files."""
    safe = "".join(character if character.isalnum() or character in "-_" else "-" for character in label)
    return Path(tempfile.mkdtemp(prefix=f"{safe[:40] or 'worker'}-", dir=_spawn_root()))


def _wrapper_source() -> str:
    executable = sys.executable
    if "\n" in executable or "\r" in executable:
        raise handoff_launcher.AdapterError("Python executable path contains a newline")
    return (
        f"#!{executable}\n"
        "from agents.orchestration import spawn_worker\n"
        "spawn_worker.exec_from_config()\n"
    )


def _write_launch_files(private_dir: Path, config: dict[str, Any]) -> tuple[Path, Path]:
    wrapper = private_dir / "launch_worker.py"
    config_path = private_dir / "launch.json"
    handoff._write_new(wrapper, _wrapper_source().encode(), 0o700)  # noqa: SLF001
    handoff._write_new(config_path, handoff._json_bytes(config), 0o600)  # noqa: SLF001
    handoff._fsync_dir(private_dir)  # noqa: SLF001
    return wrapper, config_path


def exec_from_config(config_path: str | None = None) -> None:
    """Replace the private wrapper process with the configured agent.

    Deliberately separate from :func:`handoff_launcher.exec_from_config`, whose
    exact-key validation requires the protocol's ``run_dir`` and
    ``worker_token_file``.  A lightweight worker has neither, and must not have
    ``HANDOFF_RUN_DIR`` exported into its environment: that variable is what
    tells an agent it is participating in a handoff run.
    """
    if config_path is None:
        if len(sys.argv) != 2:
            raise SystemExit("usage: spawn-wrapper CONFIG")
        config_path = sys.argv[1]
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    expected = {"agent", "model", "effort", "pmode", "cwd", "kickoff", "private_dir"}
    if set(config) != expected:
        raise SystemExit("invalid spawn launch config")
    process_env = env.build_env()
    # build_env() starts from os.environ, so a lightweight worker spawned from
    # inside a handoff worker would otherwise inherit *that* run's pointers and
    # act on someone else's protocol state. Drop them rather than merely
    # declining to set them.
    for leaked in ("HANDOFF_RUN_DIR", "HANDOFF_WORKER_TOKEN_FILE"):
        process_env.pop(leaked, None)
    if config["agent"] == "kimi" and config["effort"]:
        # Kimi carries thinking effort in the environment, not on its CLI.
        process_env["KIMI_MODEL_THINKING_EFFORT"] = config["effort"]
    os.chdir(config["cwd"])
    # Build argv before deleting anything: `_agent_argv` reads the prompt file.
    argv = handoff_launcher._agent_argv(config, process_env)  # noqa: SLF001
    _discard_launch_files(Path(config["private_dir"]))
    os.execvpe(argv[0], argv, process_env)


def _discard_launch_files(private_dir: Path) -> None:
    """Remove the wrapper and config, keeping any prompt written alongside them.

    A piped prompt stays readable so a human can see exactly what the worker was
    asked to do; the executable wrapper and its config have no further use once
    argv is built.  When the caller supplied their own prompt file there is
    nothing left to keep, so the directory goes too — a mode that keeps no
    records should not leave a trail of empty ones.
    """
    for name in ("launch_worker.py", "launch.json"):
        try:
            (private_dir / name).unlink()
        except OSError:
            pass
    try:
        private_dir.rmdir()
    except OSError:
        pass  # Still holds a piped prompt; leave it.


def _resolve_prompt(prompt_file: Path, label: str) -> tuple[Path, Path | None]:
    """Return the prompt path, plus the directory created to hold it if any.

    A caller-supplied file is used where it already lives rather than copied:
    this mode keeps no records, so there is nothing to be gained by owning a
    second copy of a file the caller already manages.  A prompt piped on stdin
    has no home, so it gets one.
    """
    if str(prompt_file) == "-":
        prompt = sys.stdin.read()
        if not prompt.strip():
            raise handoff.HandoffError("the prompt read from stdin was empty", 2)
        private_dir = _private_dir(label)
        path = private_dir / "prompt.md"
        handoff._write_new(path, prompt.encode(), 0o600)  # noqa: SLF001
        return path, private_dir
    try:
        return Path(prompt_file).resolve(strict=True), None
    except OSError as exc:
        raise handoff.HandoffError(f"prompt file not readable: {prompt_file}", 2) from exc


def spawn(
    *, label: str, prompt: Path, cwd: Path, agent: str = "claude",
    backend: str | None = None, model: str | None = None, effort: str | None = None,
    pmode: str = "bypassPermissions", workspace: str | None = None,
    private_dir: Path | None = None,
    cmux_binary: str = handoff_launcher.CMUX_DEFAULT,
    herdr_binary: str = handoff_launcher.HERDR_DEFAULT,
    split: str | None = None, ratio: float | None = None,
) -> dict[str, Any]:
    """Open a tab, start ``agent`` on ``prompt``, and return without waiting.

    ``split`` moves the worker in beside the caller's own pane instead of
    leaving it in its own tab — for the case where the human wants to watch the
    worker rather than walk away from it. herdr only.
    """
    cwd = Path(cwd).resolve(strict=True)
    prompt = Path(prompt).resolve(strict=True)
    if agent not in handoff_launcher.AGENT_DEFAULTS:
        raise handoff.HandoffError(
            f"agent must be one of: {', '.join(handoff_launcher.AGENT_DEFAULTS)}", 2,
        )
    default_model, default_effort = handoff_launcher.AGENT_DEFAULTS[agent]
    model = default_model if model is None else model
    effort = default_effort if effort is None else effort
    backend = handoff_launcher._select_backend(backend, cmux_binary, herdr_binary)  # noqa: SLF001
    if private_dir is None:
        private_dir = _private_dir(label)

    config = {
        "agent": agent, "model": model, "effort": effort, "pmode": pmode,
        "cwd": str(cwd), "kickoff": str(prompt), "private_dir": str(private_dir),
    }
    wrapper, config_path = _write_launch_files(private_dir, config)
    terminal_command = f"exec {shlex.quote(str(wrapper))} {shlex.quote(str(config_path))}"
    adapter: Any = handoff_launcher.build_adapter(backend, cmux_binary, herdr_binary)
    # The adapters all create the tab unfocused: a worker tab is background work
    # by definition, and stealing the human's focus is the one thing a spawn
    # should never do.
    handle = adapter.launch(label, cwd, terminal_command, workspace)

    result: dict[str, Any] = {
        "label": label, "agent": agent, "model": model, "effort": effort,
        "backend": backend, "handle": handle, "cwd": str(cwd),
        "prompt_path": str(prompt),
    }
    if split:
        if backend != "herdr":
            raise handoff.HandoffError(
                f"--split needs the herdr backend; this session resolved to {backend}", 2,
            )
        adapter.split_into_current(handle, direction=split, ratio=ratio)
        result["split"] = split
    _deliver(result, adapter=adapter, handle=handle, agent=agent, cwd=cwd, prompt=prompt)
    return result


def _deliver(
    result: dict[str, Any], *, adapter: Any, handle: str, agent: str, cwd: Path, prompt: Path,
) -> None:
    """Finish the per-agent startup that argv alone cannot cover.

    All three of Claude, Codex, and Kimi block below their agent loop on a
    first-launch folder-trust dialog, which would leave a spawned tab dead until
    a human noticed — and nobody is watching this one.  Kimi additionally takes
    no prompt on its command line, so its dialog has to be cleared *before* the
    bootstrap wait rather than alongside it.  Everything here is borrowed from
    the heavy launcher rather than reimplemented.
    """
    startup: dict[str, Any] = {}
    if agent == "codex":
        handoff_launcher._rescue_local_codex_folder_trust(  # noqa: SLF001
            startup, adapter=adapter, handle=handle, cwd=cwd,
        )
    elif agent == "claude":
        handoff_launcher._rescue_local_claude_folder_trust(  # noqa: SLF001
            startup, adapter=adapter, handle=handle, cwd=cwd,
        )
    elif agent == "kimi":
        handoff_launcher._rescue_local_kimi_folder_trust(  # noqa: SLF001
            startup, adapter=adapter, handle=handle, cwd=cwd,
        )
    if agent in ("codex", "claude", "kimi"):
        result["folder_trust_rescued"] = bool(startup.get("folder_trust_rescued"))
        if startup.get("startup_unconfirmed"):
            result["startup_unconfirmed"] = True
    if agent != "kimi":
        result["prompt_sent"] = True
        return
    result["prompt_sent"] = _bootstrap_kimi(adapter, handle, prompt)
    if not result["prompt_sent"]:
        result["rescue_command"] = adapter.rescue_command(handle)


def _bootstrap_kimi(adapter: Any, handle: str, prompt: Path) -> bool:
    """Point Kimi at the prompt file; never type the prompt body into its TUI.

    Process visibility and the interactive input widget are separate waits, and
    they share one budget the way ``handoff_launcher.bootstrap_kimi`` does:
    giving each phase the full timeout would let a stuck launch stall for twice
    as long as the caller was told to expect.
    """
    started_at = time.monotonic()
    if not handoff_launcher.wait_process(adapter, handle, "kimi", timeout=KIMI_BOOTSTRAP_TIMEOUT):
        return False
    remaining = KIMI_BOOTSTRAP_TIMEOUT - (time.monotonic() - started_at)
    if remaining <= 0.0:
        return False
    if not handoff_launcher.wait_kimi_input_ready(adapter, handle, timeout=remaining):
        return False
    pointer = json.dumps(str(prompt), ensure_ascii=False)
    return adapter.send_literal(handle, f"Read the task in {pointer} and do it.")


def spawn_remote(
    *, host: str, remote_python: str, label: str, prompt: Path, remote_cwd: Path,
    agent: str = "claude", model: str | None = None, effort: str | None = None,
    pmode: str = "bypassPermissions",
    workspace_label: str = handoff_launcher.HERDR_REMOTE_WORKSPACE_LABEL,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """Spawn a worker tab in the remote host's own herdr server.

    Only herdr is supported remotely.  The heavy launcher carries a tmux
    fallback and has to reconcile the backend the host actually used against the
    one it asked for; a mode that keeps no records cannot carry that
    reconciliation, so an unreachable remote herdr is an error instead.
    """
    host = handoff_launcher._remote_host(host)  # noqa: SLF001
    prompt_bytes = Path(prompt).resolve(strict=True).read_bytes()
    if len(prompt_bytes) > handoff.MAX_KICKOFF:
        raise handoff.HandoffError(
            f"prompt exceeds {handoff.MAX_KICKOFF} bytes", 2,
        )
    payload = {
        "label": label, "prompt_b64": base64.b64encode(prompt_bytes).decode("ascii"),
        "cwd": str(remote_cwd), "agent": agent, "model": model, "effort": effort,
        "pmode": pmode, "workspace_label": workspace_label,
    }
    remote_argv = [
        remote_python, "-m", "agents.orchestration.spawn_worker", "--receive-spawn-request",
    ]
    try:
        completed = handoff_launcher._run_ssh(  # noqa: SLF001
            host, remote_argv, stdin=json.dumps(payload, ensure_ascii=False), timeout=timeout,
        )
    except handoff_launcher.AdapterError as exc:
        if "No module named" in str(exc) and "spawn_worker" in str(exc):
            raise handoff_launcher.AdapterError(f"{exc}: {REMOTE_STALE_HINT}") from exc
        raise
    try:
        remote_result = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise handoff_launcher.AdapterError(
            f"remote spawn returned non-JSON output: {completed.stdout.strip()[:200]}",
        ) from exc
    if not isinstance(remote_result, dict) or "handle" not in remote_result:
        raise handoff_launcher.AdapterError("remote spawn returned no handle")
    remote_result["host"] = host
    remote_result["backend"] = f"ssh_{remote_result.get('backend', 'herdr')}"
    return remote_result


def receive_spawn_request(request: dict[str, Any]) -> dict[str, Any]:
    """Validate a JSON-over-stdin request and spawn it on this SSH host."""
    if not isinstance(request, dict):
        raise handoff.HandoffError("spawn request must be a JSON object", 2)
    unknown = set(request) - REMOTE_REQUEST_FIELDS
    if unknown:
        raise handoff.HandoffError(
            f"unknown spawn request fields: {', '.join(sorted(unknown))}", 2,
        )
    label = request.get("label")
    cwd = request.get("cwd")
    if not isinstance(label, str) or not label:
        raise handoff.HandoffError("spawn request needs a label", 2)
    if not isinstance(cwd, str) or not cwd:
        raise handoff.HandoffError("spawn request needs a cwd", 2)
    encoded = request.get("prompt_b64")
    if not isinstance(encoded, str):
        raise handoff.HandoffError("spawn request needs prompt_b64", 2)
    try:
        prompt_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise handoff.HandoffError("prompt_b64 is not valid base64", 2) from exc
    if len(prompt_bytes) > handoff.MAX_KICKOFF:
        raise handoff.HandoffError(f"prompt exceeds {handoff.MAX_KICKOFF} bytes", 2)

    private_dir = _private_dir(label)
    prompt_path = private_dir / "prompt.md"
    handoff._write_new(prompt_path, prompt_bytes, 0o600)  # noqa: SLF001
    workspace_label = request.get("workspace_label") or handoff_launcher.HERDR_REMOTE_WORKSPACE_LABEL
    # An SSH command inherits no HERDR_WORKSPACE_ID, so the worker is placed by
    # label into the workspace that collects this host's worker tabs.
    workspace = handoff_launcher._herdr_workspace_by_label(  # noqa: SLF001
        workspace_label, Path(cwd),
    )
    return spawn(
        label=label, prompt=prompt_path, cwd=Path(cwd),
        agent=request.get("agent") or "claude", backend="herdr",
        model=request.get("model"), effort=request.get("effort"),
        pmode=request.get("pmode") or "bypassPermissions",
        workspace=workspace, private_dir=private_dir,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Spawn a coding agent in a new terminal tab and return immediately. "
            "No handoff protocol, no run directory, no monitoring, no bookkeeping."
        ),
    )
    parser.add_argument("label", help="tab label; also names the private spawn directory")
    parser.add_argument(
        "prompt_file", type=Path,
        help="file holding the task prompt, or - to read it from stdin",
    )
    parser.add_argument("cwd", nargs="?", type=Path, help="worker checkout (default: current directory)")
    parser.add_argument(
        "--agent", choices=tuple(handoff_launcher.AGENT_DEFAULTS), default="claude",
    )
    parser.add_argument(
        "--model", default=None, help=handoff_launcher._agent_default_help("model", 0),  # noqa: SLF001
    )
    parser.add_argument(
        "--effort", default=None,
        help=handoff_launcher._agent_default_help("reasoning effort", 1),  # noqa: SLF001
    )
    parser.add_argument("--pmode", default="bypassPermissions")
    parser.add_argument(
        "--backend", choices=("herdr", "cmux", "tmux"), default=None,
        help="local session backend (default: herdr inside herdr, else cmux inside cmux, else tmux)",
    )
    parser.add_argument("--workspace", default=None, help="local backend workspace override")
    parser.add_argument(
        "--split", choices=("right", "down"), default=None,
        help=(
            "herdr only: put the worker in a split beside this pane instead of its own "
            "tab, for a worker you want to watch (a reviewer) rather than walk away from"
        ),
    )
    parser.add_argument(
        "--ratio", type=float, default=None,
        help="split ratio for --split (herdr default when omitted)",
    )
    parser.add_argument(
        "--remote-host", default=None,
        help="SSH host with this agents package, the selected agent, and a reachable herdr server",
    )
    parser.add_argument(
        "--remote-cwd", type=Path, default=None,
        help="absolute worker checkout path on --remote-host",
    )
    parser.add_argument(
        "--remote-python", default=handoff_launcher.REMOTE_PYTHON_DEFAULT,
        help="Python executable on --remote-host",
    )
    parser.add_argument(
        "--remote-workspace", default=handoff_launcher.HERDR_REMOTE_WORKSPACE_LABEL,
        help=(
            "herdr workspace label on --remote-host that collects worker tabs "
            f"(default: {handoff_launcher.HERDR_REMOTE_WORKSPACE_LABEL}); created on first use"
        ),
    )
    parser.add_argument("--cmux-binary", default=handoff_launcher.CMUX_DEFAULT)
    parser.add_argument("--herdr-binary", default=handoff_launcher.HERDR_DEFAULT)
    return parser


def main(argv: list[str] | None = None) -> int:
    actual_argv = sys.argv[1:] if argv is None else argv
    if actual_argv == ["--receive-spawn-request"]:
        try:
            request = json.load(sys.stdin)
            print(json.dumps(receive_spawn_request(request), ensure_ascii=False, sort_keys=True))
            return 0
        except (json.JSONDecodeError, handoff.HandoffError) as exc:
            exit_code = exc.exit_code if isinstance(exc, handoff.HandoffError) else 2
            print(f"spawn_worker: {exc}", file=sys.stderr)
            return exit_code
    try:
        args = build_parser().parse_args(actual_argv)
        if args.remote_host:
            if args.backend is not None or args.workspace is not None:
                raise handoff.HandoffError(
                    "--remote-host cannot be combined with --backend or --workspace", 2,
                )
            if args.split is not None:
                # The remote worker lands in the remote host's herdr server; there
                # is no pane of this session's to split it against.
                raise handoff.HandoffError(
                    "--split cannot be combined with --remote-host", 2,
                )
            remote_cwd = args.remote_cwd or args.cwd
            if remote_cwd is None:
                raise handoff.HandoffError(
                    "a remote checkout path is required with --remote-host", 2,
                )
            # The prompt is shipped as bytes, so a stdin prompt is read here.
            prompt, local_dir = _resolve_prompt(args.prompt_file, args.label)
            result = spawn_remote(
                host=args.remote_host, remote_python=args.remote_python,
                label=args.label, prompt=prompt, remote_cwd=remote_cwd,
                agent=args.agent, model=args.model, effort=args.effort,
                pmode=args.pmode, workspace_label=args.remote_workspace,
            )
            if local_dir is not None:
                # The host now owns the only copy the worker reads; a staging
                # copy of a piped prompt would be a record this mode does not keep.
                shutil.rmtree(local_dir, ignore_errors=True)
        else:
            prompt, private_dir = _resolve_prompt(args.prompt_file, args.label)
            result = spawn(
                label=args.label, prompt=prompt, cwd=args.cwd or Path.cwd(),
                agent=args.agent, backend=args.backend, model=args.model,
                effort=args.effort, pmode=args.pmode, workspace=args.workspace,
                private_dir=private_dir,
                cmux_binary=args.cmux_binary, herdr_binary=args.herdr_binary,
                split=args.split, ratio=args.ratio,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if result.get("rescue_command"):
            print(
                f"prompt delivery unconfirmed; inspect with: {result['rescue_command']}",
                file=sys.stderr,
            )
        return 0
    except handoff.HandoffError as exc:
        print(f"spawn_worker: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
