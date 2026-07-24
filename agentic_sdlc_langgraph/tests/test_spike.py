"""Phase-0 spike smoke test.

Proves, against the *real* contract/profile/catalog files (no mocks of
those), that:

- `build_graph` (generic, not hardcoded to G1/G2/G3 by name) wires the
  G1-G3 slice of `lifecycle-gates.json` correctly.
- The compiled graph suspends via `interrupt()` at G1's, G2's, and G3's
  human-approval nodes (all three -- not just G2 -- since every gate in
  this contract's `authority_requirements` is typed `human-approver`).
- Resuming with `Command(resume=...)` carries the run forward gate by
  gate to a terminal "all three approved" state.
- `export_run_record` produces a dict that validates against the real
  `run-record.schema.json` with `jsonschema.Draft202012Validator`.
- Separation-of-duties enforcement blocks a gate when the reviewer and an
  author resolve to the same agent id.

Uses `FakeModelClient` throughout -- no ANTHROPIC_API_KEY is configured in
this environment and none of this should hit the network.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import jsonschema
import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from agentic_sdlc_langgraph.agents import FakeModelClient
from agentic_sdlc_langgraph.contracts import (
    load_agent_catalog,
    load_lifecycle_gates,
    load_mutation_gates,
    load_profile,
    mutation_gate_guard,
)
from agentic_sdlc_langgraph.export import export_run_record
from agentic_sdlc_langgraph.graph import build_graph

REPO_ROOT = Path("/home/deagy/sdk/agentic-sdlc")
CONTRACTS = REPO_ROOT / "plugins" / "agentic-sdlc" / "contracts"
PROVIDER_DEFAULTS = REPO_ROOT / "providers" / "agentic-sdlc-defaults"


@pytest.fixture()
def contracts():
    lifecycle_gates = load_lifecycle_gates(CONTRACTS / "lifecycle-gates.json")
    mutation_gates = load_mutation_gates(CONTRACTS / "mutation-gates.json")
    agent_catalog = load_agent_catalog(PROVIDER_DEFAULTS / "agent-catalog.json")
    profile = load_profile(PROVIDER_DEFAULTS / "profiles" / "generic" / "profile.json")
    g1_g3 = [g for g in lifecycle_gates if g["id"] in {"G1", "G2", "G3"}]
    return {
        "lifecycle_gates_full": lifecycle_gates,
        "g1_g3": g1_g3,
        "mutation_gates": mutation_gates,
        "agent_catalog": agent_catalog,
        "profile": profile,
    }


def _make_graph(contracts, model_client=None):
    model_client = model_client or FakeModelClient()
    checkpointer = SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))
    graph = build_graph(
        gates=contracts["g1_g3"],
        gate_bindings=contracts["profile"]["gate_bindings"],
        routes=contracts["profile"]["routing"],
        agent_catalog=contracts["agent_catalog"],
        model_client=model_client,
        checkpointer=checkpointer,
        mutation_gates=contracts["mutation_gates"],
    )
    return graph


TASK_TEXT = "Define and review a small internal order-processing API architecture and service"


def test_g1_g3_interrupt_resume_and_export(contracts):
    graph = _make_graph(contracts)
    config = {"configurable": {"thread_id": "task-001"}}

    initial_state = {
        "task_id": "task-001",
        "classification": "internal",
        "scope": TASK_TEXT,
        "current_lifecycle_phase": "intent",
        "lifecycle_gates": {},
        "re_entry_history": [],
        "authorities": {
            "product_owner": {"status": "assigned"},
            "engineering_lead": {"status": "assigned"},
            "system_architect": {"status": "assigned"},
        },
        "agent_outputs": [],
        "mutation_gate_pending": None,
    }

    result = graph.invoke(initial_state, config=config)

    # --- suspended at G1's human approval ---
    assert "__interrupt__" in result
    interrupts = result["__interrupt__"]
    assert len(interrupts) == 1
    payload = interrupts[0].value
    assert payload["gate_id"] == "G1"
    assert payload["authority_requirements"], "G1 must have non-empty authority_requirements"

    state_snapshot = graph.get_state(config)
    assert state_snapshot.values["lifecycle_gates"]["G1"]["status"] == "ready"
    assert state_snapshot.values["mutation_gate_pending"] is None  # task text matches no mutation phrase

    # --- resume G1 with approval ---
    approval = {
        "status": "approved",
        "approver": {"id": "product_owner", "role": "Product Owner", "kind": "human"},
        "evidence_refs": [],
    }
    result = graph.invoke(Command(resume=approval), config=config)
    assert "__interrupt__" in result
    assert result["__interrupt__"][0].value["gate_id"] == "G2"

    state_snapshot = graph.get_state(config)
    assert state_snapshot.values["lifecycle_gates"]["G1"]["status"] == "approved"
    g1_preparers = state_snapshot.values["lifecycle_gates"]["G1"]["preparers"]
    assert [p["id"] for p in g1_preparers] == ["product-intent-agent"]
    assert state_snapshot.values["lifecycle_gates"]["G1"]["independent_verifier"]["id"] == "code-reviewer"

    # --- resume G2 with approval ---
    result = graph.invoke(Command(resume=approval), config=config)
    assert "__interrupt__" in result
    assert result["__interrupt__"][0].value["gate_id"] == "G3"

    state_snapshot = graph.get_state(config)
    assert state_snapshot.values["lifecycle_gates"]["G2"]["status"] == "approved"

    # --- resume G3 with approval -> terminal ---
    result = graph.invoke(Command(resume=approval), config=config)
    assert "__interrupt__" not in result or not result["__interrupt__"]

    final_state = graph.get_state(config).values
    for gate_id in ("G1", "G2", "G3"):
        assert final_state["lifecycle_gates"][gate_id]["status"] == "approved"

    # --- export + validate against the real schema ---
    record = export_run_record(final_state)
    schema = __import__("json").loads(
        (CONTRACTS / "run-record.schema.json").read_text(encoding="utf-8")
    )
    validator = jsonschema.Draft202012Validator(schema)
    errors = list(validator.iter_errors(record))
    assert errors == [], f"schema validation errors: {errors}"
    assert len(record["lifecycle_gates"]) == 10
    assert [g["gate_id"] for g in record["lifecycle_gates"]] == [f"G{n}" for n in range(1, 11)]


def test_mutation_gate_guard_fires_on_production_deploy_phrase(contracts):
    result = mutation_gate_guard(
        "deploy to production tonight", contracts["mutation_gates"]
    )
    assert result is not None
    assert result["matched"][0]["id"] == "production-deployment"


def test_mutation_gate_guard_does_not_fire_on_ordinary_task(contracts):
    result = mutation_gate_guard(TASK_TEXT, contracts["mutation_gates"])
    assert result is None


def test_separation_of_duties_blocks_gate_when_reviewer_equals_author(contracts):
    """Construct the gate_decision logic's core invariant directly: if the
    independent verifier's id matches a preparer id, status must be
    "blocked", never "approved". We exercise this via a real graph run
    where FakeModelClient is configured so the *same* agent id is
    dispatched as both author and reviewer for G1 -- achieved by pointing
    a synthetic profile's G1 contribution and route reviewers at the same
    agent id.
    """
    gates = [g for g in contracts["lifecycle_gates_full"] if g["id"] == "G1"]
    gate_bindings = {
        "G1": {
            "contributions": {
                "intent": {
                    "agents": ["product-intent-agent"],
                    "tasks": ["capture-intent"],
                    "artifacts": ["intent-record"],
                }
            }
        }
    }
    # Route reviewers deliberately reuse the same author agent id so the
    # independent verifier collides with a preparer.
    routes = [
        {
            "id": "colliding-route",
            "phrases": ["architecture"],
            "agents": [],
            "reviewers": ["product-intent-agent"],
            "support": [],
            "gates": ["G1"],
        }
    ]
    agent_catalog = dict(contracts["agent_catalog"])
    # product-intent-agent is "author" kind in the real catalog; the route
    # dispatches it as a reviewer regardless of catalog kind (route
    # reviewers are taken at face value per spec, not filtered by catalog
    # kind) -- this is exactly the collision scenario.

    model_client = FakeModelClient()
    checkpointer = SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))
    graph = build_graph(
        gates=gates,
        gate_bindings=gate_bindings,
        routes=routes,
        agent_catalog=agent_catalog,
        model_client=model_client,
        checkpointer=checkpointer,
        mutation_gates=[],
    )
    config = {"configurable": {"thread_id": "task-collision"}}
    initial_state = {
        "task_id": "task-collision",
        "classification": "internal",
        "scope": "architecture review task",
        "current_lifecycle_phase": "intent",
        "lifecycle_gates": {},
        "re_entry_history": [],
        "authorities": {"product_owner": {"status": "assigned"}},
        "agent_outputs": [],
        "mutation_gate_pending": None,
    }
    result = graph.invoke(initial_state, config=config)
    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    assert "violation" in payload["reason"].lower() or "cannot approve" in payload["reason"].lower()

    state_snapshot = graph.get_state(config)
    assert state_snapshot.values["lifecycle_gates"]["G1"]["status"] == "blocked"

    # Even resuming with an "approved" decision must not flip it to approved.
    result = graph.invoke(
        Command(resume={"status": "approved", "approver": None, "evidence_refs": []}),
        config=config,
    )
    final = graph.get_state(config).values
    assert final["lifecycle_gates"]["G1"]["status"] == "blocked"
    assert final["lifecycle_gates"]["G1"]["human_approvals"][0]["status"] == "rejected"

    # The blocked-gate record must still be schema-valid (this is what
    # caught a real bug during development: "blocked" is a valid
    # gate.status value but NOT a valid approval.status value -- they are
    # two different enums in run-record.schema.json).
    record = export_run_record(final)
    schema = __import__("json").loads(
        (CONTRACTS / "run-record.schema.json").read_text(encoding="utf-8")
    )
    validator = jsonschema.Draft202012Validator(schema)
    errors = list(validator.iter_errors(record))
    assert errors == [], f"schema validation errors: {errors}"
