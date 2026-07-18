"""Command-line adapter for :mod:`agents.orchestration.handoff`."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from agents.orchestration import handoff


RESULT_NOTIFICATION_TITLE = "Handoff result ready"
RESULT_NOTIFICATION_BODY = "Awaiting coordinator review"
RESULT_NOTIFICATION_TIMEOUT_SECONDS = 5


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def _read_bytes(path: str | None, *, default: bytes = b"") -> bytes:
    if path is None:
        return default
    if path == "-":
        return sys.stdin.buffer.read()
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise handoff.HandoffError(f"cannot read {path}: {exc}", 6) from exc


def _read_text(path: str | None, *, default: str = "") -> str:
    raw = _read_bytes(path, default=default.encode())
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise handoff.HandoffError(f"input {path} is not UTF-8", 2) from exc


def _json_input(path: str | None, default: Any) -> Any:
    if path is None:
        return default
    raw = _read_bytes(path)
    # Reuse the strict duplicate-key parser. Input validation errors are usage
    # failures rather than corruption failures.
    try:
        return handoff._decode_json(raw, path)  # noqa: SLF001 - shared CLI parser
    except handoff.HandoffError as exc:
        raise handoff.HandoffError(str(exc), 2) from exc


def _token(args: argparse.Namespace, role: str) -> bytes:
    path = getattr(args, "token_file", None)
    if path is None:
        variable = f"HANDOFF_{role.upper()}_TOKEN_FILE"
        path = os.environ.get(variable)
        if path is None:
            raise handoff.HandoffError(f"--token-file or {variable} is required", 3)
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise handoff.HandoffError(f"cannot read credential file {path}: {exc}", 3) from exc


def _mutation_inputs(args: argparse.Namespace) -> dict[str, Any]:
    stdin_count = sum(value == "-" for value in (args.body_file, args.data_file, args.attachments_file))
    if stdin_count > 1:
        raise handoff.HandoffError("at most one input file may be '-'", 2)
    data = _json_input(args.data_file, {})
    attachments = _json_input(args.attachments_file, [])
    if not isinstance(attachments, list) or not all(isinstance(value, str) for value in attachments):
        raise handoff.HandoffError("attachments-file must contain a JSON array of strings", 2)
    return {
        "type": args.type, "message_id": args.message_id, "reply_to": args.reply_to,
        "body": _read_text(args.body_file), "data": data, "attachments": attachments,
        "snapshot_attachments": args.snapshot_attachments,
    }


def _notify_result_ready(run_dir: Path) -> None:
    """Best-effort cmux alert after the durable result is authoritative."""
    try:
        run = handoff._load_json(  # noqa: SLF001 - same-package immutable run reader
            Path(run_dir) / "run.json",
            handoff._validate_run,  # noqa: SLF001 - reject a malformed transport record
        )
        surface = os.environ.get("CMUX_SURFACE_ID")
        if run["worker"]["transport"] != "cmux" or not surface:
            return
        subprocess.run(
            [
                "cmux", "notify", "--surface", surface,
                "--title", RESULT_NOTIFICATION_TITLE,
                "--body", RESULT_NOTIFICATION_BODY,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=RESULT_NOTIFICATION_TIMEOUT_SECONDS,
        )
    except (handoff.HandoffError, OSError, subprocess.SubprocessError):
        # The outbox event and awaiting_review projection were already made
        # durable. Notification delivery is only an operator convenience.
        pass


def _add_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-dir", required=True, type=_absolute)


def _add_token(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--token-file", type=Path)


def _add_message(parser: argparse.ArgumentParser) -> None:
    _add_run(parser); _add_token(parser)
    parser.add_argument("--type", required=True)
    parser.add_argument("--message-id")
    parser.add_argument("--reply-to")
    parser.add_argument("--body-file")
    parser.add_argument("--data-file")
    parser.add_argument("--attachments-file")
    parser.add_argument("--snapshot-attachments", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="handoffctl")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    init.add_argument("--workspace", required=True, type=Path)
    init.add_argument("--kickoff", required=True, type=Path)
    init.add_argument("--harness", required=True); init.add_argument("--model", required=True)
    init.add_argument("--transport", required=True); init.add_argument("--effort")
    init.add_argument("--input", action="append", default=[])
    destination = init.add_mutually_exclusive_group()
    destination.add_argument("--state-root", type=Path); destination.add_argument("--run-dir", type=_absolute)
    init.add_argument("--recovery-token-file", required=True, type=Path)
    init.add_argument("--coordinator-token-file", required=True, type=Path)
    init.add_argument("--worker-token-file", required=True, type=Path)

    read = commands.add_parser("read"); _add_run(read)
    read.add_argument("--journal", required=True, choices=("inbox", "outbox"))
    read.add_argument("--after", type=int, default=0); read.add_argument("--through", type=int)
    status = commands.add_parser("status"); _add_run(status)
    ready = commands.add_parser("ready"); _add_run(ready); _add_token(ready)
    send = commands.add_parser("send"); _add_message(send)
    emit = commands.add_parser("emit"); _add_message(emit)

    control = commands.add_parser("control")
    controls = control.add_subparsers(dest="control_command", required=True)
    show = controls.add_parser("show"); _add_run(show)
    consume = controls.add_parser("consume"); _add_run(consume); _add_token(consume); consume.add_argument("--through", required=True, type=int)
    for name in ("renew", "release"):
        item = controls.add_parser(name); _add_run(item); _add_token(item)
    integrate = controls.add_parser("integrate"); _add_run(integrate); _add_token(integrate); integrate.add_argument("--commit", required=True)
    abandon = controls.add_parser("abandon"); _add_run(abandon); _add_token(abandon); abandon.add_argument("--reason-file", required=True); abandon.add_argument("--commit")
    takeover = controls.add_parser("takeover"); _add_run(takeover)
    takeover.add_argument("--new-token-file", required=True, type=Path)
    takeover.add_argument("--recovery-token-file", required=True, type=Path)
    takeover.add_argument("--reason-file", required=True); takeover.add_argument("--force", action="store_true")
    rotate = controls.add_parser("rotate-worker"); _add_run(rotate); _add_token(rotate)
    rotate.add_argument("--new-token-file", required=True, type=Path)
    rotate.add_argument("--reason-file", required=True); rotate.add_argument("--confirmed-dead", action="store_true")

    doctor = commands.add_parser("doctor"); _add_run(doctor); _add_token(doctor)
    repairs = doctor.add_mutually_exclusive_group()
    repairs.add_argument("--repair-tail", choices=("inbox", "outbox"))
    repairs.add_argument("--repair-status", action="store_true")
    repairs.add_argument("--repair-control", action="store_true")
    repairs.add_argument("--repair-progress", action="store_true")
    doctor.add_argument("--recovery-token-file", type=Path)
    doctor.add_argument("--new-token-file", type=Path)
    doctor.add_argument("--new-worker-token-file", type=Path)
    return parser


def dispatch(args: argparse.Namespace) -> dict[str, Any] | list[Any]:
    if args.command == "init":
        return handoff.initialize(
            workspace=args.workspace, kickoff=args.kickoff, harness=args.harness,
            model=args.model, transport=args.transport, effort=args.effort,
            inputs=args.input, state_root=args.state_root, run_dir=args.run_dir,
            recovery_token_file=args.recovery_token_file,
            coordinator_token_file=args.coordinator_token_file,
            worker_token_file=args.worker_token_file,
        )
    if args.command == "read": return handoff.read(args.run_dir, args.journal, after=args.after, through=args.through)
    if args.command == "status": return handoff.status(args.run_dir)
    if args.command == "ready": return handoff.ready(args.run_dir, _token(args, "worker"))
    if args.command == "send": return handoff.send(args.run_dir, _token(args, "coordinator"), **_mutation_inputs(args))
    if args.command == "emit":
        result = handoff.emit(args.run_dir, _token(args, "worker"), **_mutation_inputs(args))
        if args.type == "result":
            _notify_result_ready(args.run_dir)
        return result
    if args.command == "control":
        action = args.control_command
        if action == "show": return handoff.control_show(args.run_dir)
        if action == "consume": return handoff.control_consume(args.run_dir, _token(args, "coordinator"), through=args.through)
        if action == "renew": return handoff.control_renew(args.run_dir, _token(args, "coordinator"))
        if action == "release": return handoff.control_release(args.run_dir, _token(args, "coordinator"))
        if action == "integrate": return handoff.control_integrate(args.run_dir, _token(args, "coordinator"), commit=args.commit)
        if action == "abandon": return handoff.control_abandon(args.run_dir, _token(args, "coordinator"), reason=_read_text(args.reason_file), commit=args.commit)
        if action == "takeover":
            recovery = args.recovery_token_file.read_bytes()
            return handoff.control_takeover(args.run_dir, recovery, new_token_file=args.new_token_file, reason=_read_text(args.reason_file), force=args.force)
        if action == "rotate-worker":
            return handoff.control_rotate_worker(args.run_dir, _token(args, "coordinator"), new_token_file=args.new_token_file, reason=_read_text(args.reason_file), confirmed_dead=args.confirmed_dead)
    if args.command == "doctor":
        if args.repair_tail:
            role = "coordinator" if args.repair_tail == "inbox" else "worker"
            return handoff.repair_tail(args.run_dir, args.repair_tail, _token(args, role))
        if args.repair_status: return handoff.repair_status(args.run_dir, _token(args, "worker"))
        if args.repair_progress: return handoff.repair_progress(args.run_dir, _token(args, "worker"))
        if args.repair_control:
            new_token = args.new_token_file.read_bytes() if args.new_token_file else None
            new_worker_token = args.new_worker_token_file.read_bytes() if args.new_worker_token_file else None
            if args.recovery_token_file:
                return handoff.repair_control(
                    args.run_dir, args.recovery_token_file.read_bytes(), recovery=True,
                    new_token=new_token, new_worker_token=new_worker_token,
                )
            return handoff.repair_control(
                args.run_dir, _token(args, "coordinator"),
                new_token=new_token, new_worker_token=new_worker_token,
            )
        return handoff.doctor(args.run_dir)
    raise AssertionError("unreachable")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        result = dispatch(args)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        if args.command == "doctor" and not any((args.repair_tail, args.repair_status, args.repair_control, args.repair_progress)) and not result["ok"]:
            return 5
        return 0
    except handoff.HandoffError as exc:
        if exc.report is not None:
            print(json.dumps(exc.report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        print(f"handoffctl: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
