"""Standalone CLI entrypoint: `agentic-sdlc-lg <subcommand> ...`.

Each invocation is its own process. Every subcommand below (other than
`plan`, which may create a new task) reconnects to an already-planned
task's compiled graph via `runtime.build_graph_for_task` -- see
`runtime.py`'s module docstring for why that reconnection step exists and
how `graph-config.json` makes it deterministic.

Subcommand shape is ported in *spirit* from the legacy CLI's
`plan`/`detect`/`validate`/`status`/`invalidate`/`reenter`/
`approve-from-github` (`agentic_sdlc.py`), not its exact argument surface:
this is a new graph-shaped runtime, not a drop-in replacement.

Exit-code convention for `validate` mirrors the legacy CLI's
`validate_repository` exactly (`agentic_sdlc.py`'s `validate_repository`,
tail end): `0` valid and ready (no errors, no blockers), `2` structurally
valid but blocked on an unresolved decision, `1` a real structural/semantic
defect. This matters for CI integration (a build step can treat `0` as
"proceed", `2` as "needs a human decision, not a build failure", `1` as
"fix the run record").
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from langgraph.types import Command

from . import runtime
from .contracts import load_lifecycle_gates
from .export import export_run_record
from .reentry import invalidate_gates, reenter_gate
from .validate import validate_run_record


def _parse_ignored_gates(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _load_decision(value: str) -> Any:
    """Parse `--decision`: either a path to a JSON file, or `-` to read a
    JSON document from stdin."""
    text = sys.stdin.read() if value == "-" else Path(value).read_text(encoding="utf-8")
    return json.loads(text)


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2))


def _error(message: str) -> None:
    print(json.dumps({"error": message}, indent=2), file=sys.stderr)


def _rebuild(root: Path, task_id: str, **kwargs: Any):
    """Common `build_graph_for_task` call + error handling for every
    subcommand except `plan` (which needs bespoke first-time handling).
    Returns `None` (after printing an error to stderr) on failure so
    callers can `return 1` immediately."""
    try:
        return runtime.build_graph_for_task(root, task_id, **kwargs)
    except runtime.GraphConfigError as exc:
        _error(str(exc))
        return None


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------


def cmd_plan(args: argparse.Namespace) -> int:
    root = Path(args.root)
    ignored_gate_ids = _parse_ignored_gates(args.ignored_gates)
    already_planned = runtime.task_exists(root, args.task_id)

    built = _rebuild(
        root,
        args.task_id,
        task_text=args.task,
        profile_id=args.profile,
        provider_manifest=args.provider,
        ignored_gate_ids=ignored_gate_ids,
    )
    if built is None:
        return 1
    graph, config, metadata = built

    if already_planned:
        _print(
            {
                "status": "already-planned",
                "message": f"task {args.task_id!r} was already planned; use resume/status instead",
                "gate_sequence": metadata.gate_sequence_ids,
            }
        )
        return 0

    result = graph.invoke(runtime.initial_state(args.task_id, args.task), config=config)
    _print(runtime.invoke_result_payload(result))
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    root = Path(args.root)
    built = _rebuild(root, args.task_id)
    if built is None:
        return 1
    graph, config, _metadata = built

    decision = _load_decision(args.decision)
    result = graph.invoke(Command(resume=decision), config=config)
    _print(runtime.invoke_result_payload(result))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.root)
    built = _rebuild(root, args.task_id)
    if built is None:
        return 1
    graph, config, metadata = built

    _print(runtime.status_summary(graph, config, metadata))
    return 0


def cmd_invalidate(args: argparse.Namespace) -> int:
    root = Path(args.root)
    built = _rebuild(root, args.task_id)
    if built is None:
        return 1
    graph, config, metadata = built

    if args.earliest_gate not in metadata.gate_sequence_ids:
        _error(
            f"gate {args.earliest_gate!r} is not part of task {args.task_id!r}'s derived "
            f"gate sequence {metadata.gate_sequence_ids}"
        )
        return 1

    record = invalidate_gates(
        graph, config, args.earliest_gate, args.reason, args.actor, metadata.gate_sequence_ids
    )
    _print({"status": "invalidated", "record": record})
    return 0


def cmd_reenter(args: argparse.Namespace) -> int:
    root = Path(args.root)
    built = _rebuild(root, args.task_id)
    if built is None:
        return 1
    graph, config, metadata = built

    if args.earliest_gate not in metadata.gate_sequence_ids:
        _error(
            f"gate {args.earliest_gate!r} is not part of task {args.task_id!r}'s derived "
            f"gate sequence {metadata.gate_sequence_ids}"
        )
        return 1

    record = reenter_gate(
        graph, config, args.earliest_gate, args.reason, args.actor, metadata.gate_sequence_ids
    )
    _print({"status": "reentered", "record": record})

    # reenter_gate redirects the checkpoint's position but does not itself
    # resume execution (see reentry.py's docstring) -- actually re-dispatch
    # the reentered gate's agents here so `reenter` is a complete,
    # observable operation from the CLI, not just a state patch.
    result = graph.invoke(None, config=config)
    _print(runtime.invoke_result_payload(result))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    root = Path(args.root)
    built = _rebuild(root, args.task_id)
    if built is None:
        return 1
    graph, config, metadata = built

    snapshot = graph.get_state(config)
    record = export_run_record(
        snapshot.values,
        sequence_gate_ids=metadata.gate_sequence_ids,
        ignored_gate_ids=metadata.ignored_gate_ids,
    )
    text = json.dumps(record, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    root = Path(args.root)
    built = _rebuild(root, args.task_id)
    if built is None:
        return 1
    graph, config, metadata = built

    snapshot = graph.get_state(config)
    record = export_run_record(
        snapshot.values,
        sequence_gate_ids=metadata.gate_sequence_ids,
        ignored_gate_ids=metadata.ignored_gate_ids,
    )
    schema = json.loads((runtime.CONTRACTS_DIR / "run-record.schema.json").read_text(encoding="utf-8"))
    all_gates = load_lifecycle_gates(runtime.CONTRACTS_DIR / "lifecycle-gates.json")
    gate_contracts = {gate["id"]: gate for gate in all_gates}

    code, messages = validate_run_record(record, schema, gate_contracts=gate_contracts)
    _print(
        {
            "valid": code != 1,
            "ready": code == 0,
            "errors": messages if code == 1 else [],
            "blockers": messages if code == 2 else [],
        }
    )
    return code


# --------------------------------------------------------------------------
# argparse wiring
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentic-sdlc-lg")
    sub = parser.add_subparsers(dest="command", required=True)

    plan_p = sub.add_parser("plan", help="Plan a new task: derive its gate sequence and run it to the first interrupt.")
    plan_p.add_argument("--root", required=True)
    plan_p.add_argument("--task-id", required=True)
    plan_p.add_argument("--task", required=True)
    plan_p.add_argument("--profile", default="generic")
    plan_p.add_argument("--ignored-gates", default="", help="Comma-separated gate ids, e.g. G4,G5")
    plan_p.add_argument("--provider", default=None, help="Path to a provider manifest (provider.json)")
    plan_p.set_defaults(func=cmd_plan)

    resume_p = sub.add_parser("resume", help="Resume an interrupted task with a decision.")
    resume_p.add_argument("--root", required=True)
    resume_p.add_argument("--task-id", required=True)
    resume_p.add_argument("--decision", required=True, help="Path to a JSON file, or '-' for stdin")
    resume_p.set_defaults(func=cmd_resume)

    status_p = sub.add_parser("status", help="Print a task's current gate/interrupt status.")
    status_p.add_argument("--root", required=True)
    status_p.add_argument("--task-id", required=True)
    status_p.set_defaults(func=cmd_status)

    invalidate_p = sub.add_parser("invalidate", help="Invalidate a gate and every gate after it.")
    invalidate_p.add_argument("--root", required=True)
    invalidate_p.add_argument("--task-id", required=True)
    invalidate_p.add_argument("--earliest-gate", required=True)
    invalidate_p.add_argument("--reason", required=True)
    invalidate_p.add_argument("--actor", required=True)
    invalidate_p.set_defaults(func=cmd_invalidate)

    reenter_p = sub.add_parser("reenter", help="Reset a gate (and downstream gates) and re-dispatch it.")
    reenter_p.add_argument("--root", required=True)
    reenter_p.add_argument("--task-id", required=True)
    reenter_p.add_argument("--earliest-gate", required=True)
    reenter_p.add_argument("--reason", required=True)
    reenter_p.add_argument("--actor", required=True)
    reenter_p.set_defaults(func=cmd_reenter)

    export_p = sub.add_parser("export", help="Export the run record (run-record.schema.json shape).")
    export_p.add_argument("--root", required=True)
    export_p.add_argument("--task-id", required=True)
    export_p.add_argument("--output", default=None, help="Write to this file instead of stdout")
    export_p.set_defaults(func=cmd_export)

    validate_p = sub.add_parser("validate", help="Export + validate the run record; exit 0/1/2.")
    validate_p.add_argument("--root", required=True)
    validate_p.add_argument("--task-id", required=True)
    validate_p.set_defaults(func=cmd_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def run() -> None:
    """Console-script entry point (see `pyproject.toml`'s
    `[project.scripts]`)."""
    sys.exit(main())


if __name__ == "__main__":
    run()
