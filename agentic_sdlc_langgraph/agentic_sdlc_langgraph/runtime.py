"""Cross-process graph-rebuild logic shared by the CLI (`cli.py`) and the
standalone service (`service.py`).

## The problem this module exists to solve

Every module built in Phases 0-2 (`graph.py`, `reentry.py`, the test suite,
...) assumes one long-lived Python process holds a compiled `StateGraph`
object in memory and drives it through multiple `invoke()`/`Command(resume=
...)` calls within that same process. A real CLI invocation is a brand new
process every time (`agentic-sdlc-lg plan ...`, then, separately and later,
`agentic-sdlc-lg resume ...`); a real service handles each HTTP request
independently and must not assume any previous request's in-memory graph
object still exists. For `resume`/`status`/`invalidate`/`reenter`/`export`/
`validate` to work, each call has to reconnect to the *same compiled graph
shape* (same gates, same `gate_bindings`, same `routes`, same
`agent_catalog`) that `plan` originally built -- otherwise the persistent
checkpointer's thread state (keyed by `task_id` as LangGraph `thread_id`)
won't line up with the graph's node/edge topology, and `get_state`/`invoke`
against a differently-shaped graph is undefined at best.

## The solution: a small per-task `graph-config.json`

At `plan` time (`build_graph_for_task`'s first call for a given `task_id`,
i.e. no `graph-config.json` exists yet), this module derives the gate
sequence, builds the graph, and writes a small, human-readable metadata
file to `<root>/.agentic-sdlc/runs/<task_id>/graph-config.json` recording
everything needed to deterministically rebuild the identical graph later:

    {
      "schema_version": 1,
      "task_id": "...",
      "task_text": "...",           # the task's `scope` text
      "profile_id": "generic",
      "provider_manifest": null,     # absolute path, or null for the
                                      # shipped `agentic-sdlc-defaults`
      "ignored_gate_ids": [],
      "gate_sequence_ids": ["G1", "G2", "G3"],  # derive_gate_sequence's
                                                  # resolved gate ids, for
                                                  # the caller's own
                                                  # bookkeeping/audit trail
                                                  # AND as a staleness
                                                  # tripwire (see below)
      "created_at": "...",
    }

Every later call for the same `task_id` (`resume`/`status`/`invalidate`/
`reenter`/`export`/`validate`, all funneled through this same
`build_graph_for_task`) reads this file first, then rebuilds the graph via
the *exact same* call sequence `plan` used: `derive_gate_sequence` (fed
`task_text`/`routes`/`ignored_gate_ids` straight from the recorded
metadata) -> `provider.load_provider`/`provider.merge_profile` (if a
`provider_manifest` was recorded) or the shipped default fixtures (if not)
-> `build_graph`. The recomputed gate sequence is compared against the
recorded `gate_sequence_ids` as a staleness tripwire: if they no longer
match (e.g. someone edited the provider's routing between `plan` and
`resume`), `build_graph_for_task` raises a clear `GraphConfigError` rather
than silently rebuilding a differently-shaped graph against an
already-checkpointed thread.

This is deliberately *not* a re-export of the whole run record (that's what
`export.py`/`validate.py` are for) -- it's just enough build-time plumbing
to reconstruct the graph shape, nothing about live gate status.

## The two environment-driven defaults

- **Checkpointer**: defaults to a persistent on-disk `SqliteSaver` at
  `<root>/.agentic-sdlc/state.db`. This is the one load-bearing production
  default in this whole module: an in-memory (`:memory:`) checkpointer
  cannot survive across the separate process invocations this module
  exists to support, so `:memory:` is never the default -- only tests that
  explicitly pass their own `checkpointer=` override use it.

- **Model client** (`AGENTIC_SDLC_LANGGRAPH_FAKE_MODEL` environment
  variable): if set to `"1"`, `default_model_client()` returns a
  `FakeModelClient` (deterministic, zero network calls) instead of a real
  `AnthropicModelClient`. **This is a real behavioral switch, not just a
  test convenience** -- set it in any environment (local dry runs, CI,
  demos) that doesn't have `ANTHROPIC_API_KEY` configured, or that wants
  reproducible agent output. It is unset (or any value other than `"1"`)
  in a real deployment, where a live Anthropic-backed run is intended.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from .agents import AnthropicModelClient, FakeModelClient, ModelClient
from .contracts import (
    load_agent_catalog,
    load_lifecycle_gates,
    load_mutation_gates,
    load_profile,
)
from .export import _PHASE_BY_GATE_ID
from .graph import build_graph
from .planning import derive_gate_sequence
from .provider import LoadedProvider, load_provider, merge_profile

# Root of *this* kernel checkout (contains `plugins/` and `providers/`) --
# NOT the project root a CLI/service caller operates against (that's the
# `root` parameter threaded through every function below: an arbitrary
# project directory that owns its own `.agentic-sdlc/` state). Resolved
# relative to this file rather than hardcoded so the module works from
# any checkout location.
KERNEL_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = KERNEL_ROOT / "plugins" / "agentic-sdlc" / "contracts"
DEFAULT_PROVIDER_ROOT = KERNEL_ROOT / "providers" / "agentic-sdlc-defaults"

GRAPH_CONFIG_SCHEMA_VERSION = 1

# See module docstring: set to "1" to force FakeModelClient everywhere
# `default_model_client()` is consulted (no network, fully deterministic).
FAKE_MODEL_ENV_VAR = "AGENTIC_SDLC_LANGGRAPH_FAKE_MODEL"


class GraphConfigError(ValueError):
    """Raised for graph-config.json read/write/consistency problems: an
    unknown task_id with no `task_text` supplied, a `task_text` that
    conflicts with an already-planned task (mirrors the legacy CLI's
    `plan_task`'s "task ID already exists with different task text" guard,
    ~1114/1120 in `agentic_sdlc.py`), or a recomputed gate sequence that no
    longer matches what was recorded at plan time.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _agentic_sdlc_dir(root: Path) -> Path:
    return root / ".agentic-sdlc"


def _runs_dir(root: Path) -> Path:
    return _agentic_sdlc_dir(root) / "runs"


def _graph_config_path(root: Path, task_id: str) -> Path:
    return _runs_dir(root) / task_id / "graph-config.json"


def task_exists(root: str | Path, task_id: str) -> bool:
    """True iff `task_id` already has a `graph-config.json` under `root`
    (i.e. `plan` has already run for it at least once)."""
    return _graph_config_path(Path(root).resolve(), task_id).is_file()


def default_model_client() -> ModelClient:
    """Resolve the default `ModelClient` for `build_graph_for_task`.

    Returns a `FakeModelClient` (deterministic, no network) when the
    `AGENTIC_SDLC_LANGGRAPH_FAKE_MODEL` environment variable is set to
    `"1"`; otherwise a real `AnthropicModelClient`. See module docstring.
    """
    if os.environ.get(FAKE_MODEL_ENV_VAR) == "1":
        return FakeModelClient()
    return AnthropicModelClient()


def default_checkpointer(root: Path) -> SqliteSaver:
    """Persistent on-disk `SqliteSaver` at `<root>/.agentic-sdlc/state.db`.

    Deliberately not `:memory:` -- see module docstring for why an
    in-memory checkpointer would defeat the entire point of this module.
    Creates `<root>/.agentic-sdlc/` if it doesn't exist yet. Each call
    opens its own `sqlite3.Connection` (`check_same_thread=False`, so it's
    safe to hand to a framework that may service a request off the
    creating thread); a fresh connection per call is fine for a plain
    on-disk sqlite file shared across process invocations, which is
    exactly the scenario this module is built for.
    """
    db_dir = _agentic_sdlc_dir(root)
    db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_dir / "state.db"), check_same_thread=False)
    return SqliteSaver(conn)


@dataclass(frozen=True)
class TaskGraphMetadata:
    """In-memory shape of `graph-config.json`. See module docstring for
    field-by-field rationale."""

    task_id: str
    task_text: str
    profile_id: str
    provider_manifest: str | None
    ignored_gate_ids: list[str]
    gate_sequence_ids: list[str]
    created_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": GRAPH_CONFIG_SCHEMA_VERSION,
            "task_id": self.task_id,
            "task_text": self.task_text,
            "profile_id": self.profile_id,
            "provider_manifest": self.provider_manifest,
            "ignored_gate_ids": list(self.ignored_gate_ids),
            "gate_sequence_ids": list(self.gate_sequence_ids),
            "created_at": self.created_at,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "TaskGraphMetadata":
        return cls(
            task_id=payload["task_id"],
            task_text=payload["task_text"],
            profile_id=payload.get("profile_id", "generic"),
            provider_manifest=payload.get("provider_manifest"),
            ignored_gate_ids=list(payload.get("ignored_gate_ids", [])),
            gate_sequence_ids=list(payload.get("gate_sequence_ids", [])),
            created_at=payload.get("created_at", ""),
        )


def _read_graph_config(root: Path, task_id: str) -> TaskGraphMetadata | None:
    path = _graph_config_path(root, task_id)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return TaskGraphMetadata.from_json(payload)


def _write_graph_config(root: Path, metadata: TaskGraphMetadata) -> None:
    path = _graph_config_path(root, metadata.task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_contracts_and_profile(
    provider_manifest: str | None, profile_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], dict[str, Any], str]:
    """Resolve `(all_gates, mutation_gates, agent_catalog, profile,
    provider_root)` for either a caller-supplied provider manifest or the
    shipped `agentic-sdlc-defaults` fixtures (the "works out of the box
    against this repo's own fixtures" default path).

    `lifecycle-gates.json`/`mutation-gates.json` are kernel contracts, not
    provider content, so they're always read from this checkout's own
    `plugins/agentic-sdlc/contracts/` regardless of which provider is
    active.
    """
    all_gates = load_lifecycle_gates(CONTRACTS_DIR / "lifecycle-gates.json")
    mutation_gates = load_mutation_gates(CONTRACTS_DIR / "mutation-gates.json")

    if provider_manifest:
        loaded: LoadedProvider = load_provider(provider_manifest)
        profile = merge_profile(profile_id, loaded.profile_roots, loaded.agent_catalog, all_gates)
        agent_catalog = loaded.agent_catalog
        provider_root = str(Path(provider_manifest).resolve().parent)
    else:
        agent_catalog = load_agent_catalog(DEFAULT_PROVIDER_ROOT / "agent-catalog.json")
        profile_path = DEFAULT_PROVIDER_ROOT / "profiles" / profile_id / "profile.json"
        if not profile_path.is_file():
            raise GraphConfigError(
                f"unknown profile {profile_id!r}: no {profile_path} (and no --provider given)"
            )
        profile = load_profile(profile_path)
        provider_root = str(DEFAULT_PROVIDER_ROOT)

    return all_gates, mutation_gates, agent_catalog, profile, provider_root


def build_graph_for_task(
    root: str | Path,
    task_id: str,
    *,
    task_text: str | None = None,
    profile_id: str = "generic",
    provider_manifest: str | None = None,
    ignored_gate_ids: list[str] | None = None,
    model_client: ModelClient | None = None,
    checkpointer: Any = None,
) -> tuple[Any, dict[str, Any], TaskGraphMetadata]:
    """Build (first call for `task_id`) or rebuild (every later call) the
    compiled graph for `task_id`, returning `(graph, config, metadata)`.

    First call (no `graph-config.json` under `root` for this `task_id`
    yet): `task_text` is required. Derives the gate sequence via
    `derive_gate_sequence`, builds the graph, and writes
    `graph-config.json` recording everything needed to rebuild it later.

    Later calls: `task_text` may be omitted (the recorded one is used).
    If a *different* `task_text` is supplied than what's on record, raises
    `GraphConfigError` -- mirrors the legacy CLI's `plan_task` "task ID
    already exists with different task text; use a new task ID" guard
    (`agentic_sdlc.py` ~1114/1120): a task_id's scope is fixed once
    planned, not silently replaced by a later call.

    `config` is `{"configurable": {"thread_id": task_id}}` -- the task_id
    *is* the LangGraph thread_id, so the persistent checkpointer's history
    for this task is whatever thread the caller passed by name.

    `checkpointer`/`model_client` default to `default_checkpointer(root)`
    (persistent on-disk sqlite) / `default_model_client()` (env-var gated
    fake-or-real) respectively; both accept an explicit override so tests
    can substitute a `:memory:`-backed checkpointer or a `FakeModelClient`
    without relying on process environment.
    """
    root = Path(root).resolve()
    ignored_gate_ids = list(ignored_gate_ids or [])
    existing = _read_graph_config(root, task_id)

    if existing is None:
        if task_text is None:
            raise GraphConfigError(
                f"task {task_id!r} has no existing graph-config.json under {root}; "
                "task_text is required to plan a new task"
            )
        all_gates, mutation_gates, agent_catalog, profile, provider_root = _load_contracts_and_profile(
            provider_manifest, profile_id
        )
        sequence = derive_gate_sequence(task_text, profile["routing"], ignored_gate_ids, all_gates)
        metadata = TaskGraphMetadata(
            task_id=task_id,
            task_text=task_text,
            profile_id=profile_id,
            provider_manifest=str(Path(provider_manifest).resolve()) if provider_manifest else None,
            ignored_gate_ids=ignored_gate_ids,
            gate_sequence_ids=[g["id"] for g in sequence],
            created_at=_now(),
        )
        _write_graph_config(root, metadata)
    else:
        if task_text is not None and task_text != existing.task_text:
            raise GraphConfigError(
                f"task ID {task_id!r} already exists with different task text; use a new task ID"
            )
        metadata = existing
        all_gates, mutation_gates, agent_catalog, profile, provider_root = _load_contracts_and_profile(
            metadata.provider_manifest, metadata.profile_id
        )
        sequence = derive_gate_sequence(
            metadata.task_text, profile["routing"], metadata.ignored_gate_ids, all_gates
        )
        recomputed_ids = [g["id"] for g in sequence]
        if recomputed_ids != metadata.gate_sequence_ids:
            raise GraphConfigError(
                f"task ID {task_id!r} graph-config.json is stale: recorded gate sequence "
                f"{metadata.gate_sequence_ids} no longer matches the recomputed sequence "
                f"{recomputed_ids} for the same task text/profile/ignored gates -- has the "
                "provider's routing changed since this task was planned?"
            )

    resolved_model_client = model_client if model_client is not None else default_model_client()
    resolved_checkpointer = checkpointer if checkpointer is not None else default_checkpointer(root)

    graph = build_graph(
        gates=sequence,
        gate_bindings=profile["gate_bindings"],
        routes=profile["routing"],
        agent_catalog=agent_catalog,
        model_client=resolved_model_client,
        checkpointer=resolved_checkpointer,
        mutation_gates=mutation_gates,
        profile=profile,
        provider_root=provider_root,
    )
    config = {"configurable": {"thread_id": task_id}}
    return graph, config, metadata


# --------------------------------------------------------------------------
# Small, shared "operate on a rebuilt graph" helpers -- used by both cli.py
# and service.py so the two entrypoints render identical output shapes.
# --------------------------------------------------------------------------


def initial_state(task_id: str, task_text: str, classification: str = "internal") -> dict[str, Any]:
    """The initial `SDLCState` payload for a task's first `graph.invoke`.

    `authorities` starts empty (no assigned authorities) -- deliberately
    minimal for this phase; every gate's authority requirements will
    resolve `applicability: "unknown"` until the caller supplies its own
    authority-assignment plumbing. This is a documented simplification,
    not an oversight: neither the CLI nor the service surface a way to
    assign authorities today (see task report).
    """
    return {
        "task_id": task_id,
        "classification": classification,
        "scope": task_text,
        "current_lifecycle_phase": "intent",
        "lifecycle_gates": {},
        "re_entry_history": [],
        "authorities": {},
        "agent_outputs": {},
        "mutation_gate_pending": None,
        "mutation_gate_decision": None,
        "run_halted": False,
    }


def invoke_result_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Render a `graph.invoke(...)` return value as a small JSON-friendly
    dict: either `{"status": "interrupted", "interrupt": <payload>}` or
    `{"status": "complete", "message": "no interrupt, run complete"}`.
    Shared by `plan`/`resume`/`reenter` in both the CLI and the service so
    they render identically.
    """
    interrupts = result.get("__interrupt__") if isinstance(result, dict) else None
    if interrupts:
        return {"status": "interrupted", "interrupt": interrupts[0].value}
    return {"status": "complete", "message": "no interrupt, run complete"}


def status_summary(graph: Any, config: dict[str, Any], metadata: TaskGraphMetadata) -> dict[str, Any]:
    """Render `graph.get_state(config)` as a small, human/script-readable
    status dict: per-gate status/applicability for every gate in the
    task's derived sequence, whether the run is currently suspended at an
    interrupt, and how many re-entry events have been recorded. Shared by
    the CLI's `status` command and the service's `GET /tasks/{task_id}`.
    """
    snapshot = graph.get_state(config)
    values = snapshot.values or {}
    lifecycle_gates = values.get("lifecycle_gates", {})

    gates_summary = []
    for gate_id in metadata.gate_sequence_ids:
        gate = lifecycle_gates.get(gate_id)
        if gate is not None:
            gates_summary.append(
                {
                    "gate_id": gate_id,
                    "status": gate.get("status"),
                    "applicability": gate.get("applicability"),
                    "required_reentry_gate": gate.get("required_reentry_gate"),
                }
            )
        else:
            gates_summary.append(
                {
                    "gate_id": gate_id,
                    "status": "pending",
                    "applicability": "applicable",
                    "required_reentry_gate": None,
                }
            )

    # `state["current_lifecycle_phase"]` is a live-state field no graph
    # node ever updates (it's set once, at initial_state, to "intent",
    # and left there) -- `export_run_record` computes the real value from
    # actual gate statuses at export time rather than trusting it, and
    # this status summary must do the same, or every task would report
    # "intent" forever regardless of actual progress. Scoped to this
    # task's own `gate_sequence_ids` (not the full G1-G10), since that's
    # the only set `gates_summary` actually has data for.
    current_phase = "feedback"
    for gate in gates_summary:
        if gate["applicability"] == "not-applicable":
            continue
        if gate["status"] != "approved":
            current_phase = _PHASE_BY_GATE_ID.get(gate["gate_id"], "intent")
            break

    interrupted = bool(snapshot.interrupts)
    return {
        "task_id": metadata.task_id,
        "current_lifecycle_phase": current_phase,
        "run_halted": values.get("run_halted", False),
        "interrupted": interrupted,
        "interrupt": snapshot.interrupts[0].value if interrupted else None,
        "gates": gates_summary,
        "re_entry_history_length": len(values.get("re_entry_history", [])),
    }
