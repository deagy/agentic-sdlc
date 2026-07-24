"""Reassemble the checkpointed graph state into a `run-record.schema.json`
shaped dict, and validate it.

This spike only models G1-G3 live in the graph. Every other gate
(G4-G10) is synthesized here as a minimal, structurally valid
`not-applicable` / `pending` placeholder gate record -- the schema
requires exactly 10 entries in `lifecycle_gates` (`minItems`/`maxItems`:
10), fixed G1..G10 order, so we can't just emit the 3 we modeled.

Likewise, several top-level required fields this spike doesn't compute
(`dispatch_fingerprint`, `contract_digest`, `dispatch_binding_digest`,
`provider_bindings`, `knowledge_retrieval`, `impact_profile`,
`specialist_attestations`, `execution_summary`, ...) are filled with fixed
placeholders. The point of this module is proving the export+validate
round-trip composes with the graph state model, not modeling every field
faithfully -- that's later-phase work per the architecture plan.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

_ZERO_DIGEST = "sha256:" + "0" * 64

ALL_GATE_IDS = [f"G{n}" for n in range(1, 11)]

_GATE_NAMES = {
    "G1": "Intent",
    "G2": "Requirements Baseline",
    "G3": "Architecture",
    "G4": "Governance and Data",
    "G5": "Security and Crypto",
    "G6": "Verification and Test",
    "G7": "Evidence",
    "G8": "Release Readiness",
    "G9": "Deployment Authorization",
    "G10": "Runtime Conformance",
}


def _placeholder_gate(gate_id: str) -> dict[str, Any]:
    return {
        "tier": "lifecycle",
        "gate_id": gate_id,
        "name": _GATE_NAMES[gate_id],
        "applicability": "not-applicable",
        "applicability_rationale": "Out of Phase-0 spike scope (only G1-G3 are modeled)",
        "status": "pending",
        "artifact_bindings": [],
        "preparers": [],
        "independent_verifier": None,
        "independence_declaration": {
            "verifier_confirmed_not_preparer": False,
            "verifier_made_material_correction": False,
        },
        "authority_requirements": [],
        "human_approvals": [],
        "decided_at": None,
        "evidence_refs": [],
        "knowledge_status": "not-applicable",
        "findings": [],
        "exceptions": [],
        "invalidation_history": [],
        "required_reentry_gate": None,
    }


def _placeholder_execution_summary_gate(gate_id: str, configured: bool) -> dict[str, Any]:
    return {
        "configured": configured,
        "ignored": not configured,
        "ignore_reason": None if configured else "Out of Phase-0 spike scope",
        "required_agents": [],
        "dispatched_agents": [],
        "required_tasks": [],
        "completed_tasks": [],
        "required_agent_artifacts": [],
        "produced_agent_artifacts": [],
    }


def export_run_record(state: dict[str, Any], all_gate_ids: list[str] | None = None) -> dict[str, Any]:
    """Build a schema-shaped run-record dict from graph state.

    `all_gate_ids` (default G1..G10) is the full fixed lifecycle sequence;
    any id in it that isn't a key of `state["lifecycle_gates"]` gets a
    synthesized not-applicable placeholder.
    """
    all_gate_ids = all_gate_ids or ALL_GATE_IDS
    modeled_gates: dict[str, Any] = state.get("lifecycle_gates", {})

    lifecycle_gates = [
        modeled_gates[gid] if gid in modeled_gates else _placeholder_gate(gid) for gid in all_gate_ids
    ]

    # current_lifecycle_phase: first non-approved, applicable gate's phase;
    # "feedback" if every applicable gate is approved. This spike doesn't
    # carry a gate_id -> phase map through state, so we derive a
    # reasonable phase name straight from the gate_id for the placeholder
    # entries and fall back to "intent" if nothing else applies.
    phase_by_gate_id = {
        "G1": "intent",
        "G2": "requirements",
        "G3": "architecture",
        "G4": "governance-data",
        "G5": "security-crypto",
        "G6": "verify",
        "G7": "evidence",
        "G8": "release-readiness",
        "G9": "deployment-authorization",
        "G10": "runtime-conformance",
    }
    current_phase = "feedback"
    for gate in lifecycle_gates:
        if gate["applicability"] == "not-applicable":
            continue
        if gate["status"] != "approved":
            current_phase = phase_by_gate_id.get(gate["gate_id"], "intent")
            break

    execution_summary_gates = {
        gid: _placeholder_execution_summary_gate(gid, configured=gid in modeled_gates)
        for gid in all_gate_ids
    }

    return {
        "version": 2,
        "task_id": state.get("task_id", "unknown-task"),
        "dispatch_fingerprint": _ZERO_DIGEST,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "classification": state.get("classification") or "unclassified",
        "mode": "langgraph-spike",
        "baseline_revision": "unresolved",
        "scope": state.get("scope") or "unspecified",
        "disposition": "pending",
        "intent_record_id": None,
        "requirements_baseline_id": None,
        "current_lifecycle_phase": current_phase,
        "knowledge_retrieval": {
            "status": "unavailable",
            "reason": "No portable knowledge source configured in this spike",
            "query_ids": [],
            "evidence_refs": [],
            "influence": "none",
        },
        "impact_profile": {
            "profile_id": "phase0-spike",
            "status": "draft",
            "impact_categories": [],
            "specialized_boms": [],
            "blocking_unknowns": [],
        },
        "lifecycle_gates": lifecycle_gates,
        "specialist_attestations": [],
        "re_entry_history": state.get("re_entry_history", []),
        "execution_summary": {"gates": execution_summary_gates},
        "kernel_version": "0.1.0-langgraph-spike",
        "contract_digest": _ZERO_DIGEST,
        "provider_bindings": [],
        "profile": "generic",
        "profile_digest": None,
        "dispatch_binding_digest": _ZERO_DIGEST,
    }
