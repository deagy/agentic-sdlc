"""`create-gate-issues` / `list-gate-issues`: back the run record's
lifecycle-gate tracking and per-authority approval tracking with real
GitLab issues, idempotently, with the forge itself (queried by label) as
the source of truth for "does this issue already exist" -- never the local
sidecar ledger.

## Why this lives in the kernel, not the LangGraph engine

`authority_gitlab_username()` (`agentic_sdlc/__init__.py:385-389`) reads
from the project overlay's `authorities.json` via `load_overlay`
(`agentic_sdlc/__init__.py:1452-1454`) -- an overlay concept that exists
only in the kernel. The engine's authority map is hardcoded empty
(`agentic_sdlc_langgraph/agentic_sdlc_langgraph/runtime.py:473-495`; that
docstring records "neither the CLI nor the service surface a way to assign
authorities today"), so an engine-side implementation of this feature would
resolve every authority requirement to zero assignable approval issues. The
kernel is also the sole owner of `run-record.schema.json`'s
`lifecycle_gates` array shape and of the `authorities.json`/`run-record.json`
file pair this feature reads. See `github_approval.py:26-34` in the engine
package for the parallel, already-recorded decision *not* to reintroduce
authority-map plumbing there.

## Granularity (two levels, per gate)

- One **gate tracking issue** per lifecycle gate configured for the task,
  `applicability == "applicable"`, not ignored, not invalidated, with no
  pending `required_reentry_gate`.
- One **approval issue** per `authority_requirements[]` entry with
  `applicability == "applicable"`, assigned to the resolved GitLab
  username.

Per-artifact granularity is not implemented: `artifact_bindings` /
`produced_agent_artifacts` are always `[]` kernel-side
(`make_gate_record`, `gate_agent_artifacts()` in `agentic_sdlc/__init__.py`)
and carry no title/description/kind/URL even when populated, and
`reenter_gate` clears `artifact_bindings` on every re-entry
(`agentic_sdlc/__init__.py`'s `reenter`), which would orphan per-artifact
issues. `specialist_attestations` and a task-level roll-up epic/parent
issue are out of scope for v1.

## Idempotency

GitLab (queried by label) is the source of truth, never the sidecar ledger
at `<root>/.agentic-sdlc/runs/<task_id>/gate-issues.json` (+
`gate-issues.lock`, a sibling pair of `requirement-issues.json`/`.lock` in
the LangGraph engine package, `runtime.py:174-184` --
`_requirement_ledger_path`/`_requirement_lock_path`). Existence is
determined by `search_gitlab_issues_by_labels(project, [FIXED_LABEL,
<its own label>])`, `state=all`; `>1` match blocks (ambiguous identity),
`1` match reuses (after label-containment + author-identity validation),
`0` matches creates.

## Markers/labels (domain-separated, spec-mandated)

| Kind | Marker input (NUL-separated, sha256, `[:16]`) | Label prefix |
|---|---|---|
| Gate issue | `"gate"`, `task_id`, `gate_id` | `agentic-sdlc-gate-` |
| Approval issue | `"approval"`, `task_id`, `gate_id`, `authority_id` | `agentic-sdlc-approval-` |

This is domain-separated from the LangGraph engine package's
`requirement_issues.compute_marker(task_id, gate_id, item_key)` (no
leading domain tag, hashes `task_id\x00gate_id\x00item_key` directly) --
different input structure entirely, and disjoint in practice: this module's
markers always begin with a `"gate\x00"`/`"approval\x00"` leading literal
tag, while `requirement_issues.compute_marker` never prepends one, so the
two would only collide if a requirement-issues `task_id` byte-for-byte
matched one of those literal tags. Non-collision here is a consequence of
that disjoint-tag design plus `gate_id` being constrained to the closed
`GATE_IDS` enum (G1-G10) in this module and to a hardcoded `"G2"` in the
requirement-issues module -- not an unconditional structural guarantee (a
requirement-issues `task_id` of literally `"gate"` or `"approval"` could in
principle produce an overlapping byte string before hashing). `task_id`
itself is never emitted raw (it is operator-chosen and possibly sensitive; only its
`sha256(task_id)[:16]` hash -- `task_hash()` below -- ever appears in
issue text), mirroring `requirement_issues.compute_marker`'s reasoning.

## Link primitive (confirmed default: description cross-reference floor)

Every approval issue's description carries a module-emitted
`> parent <project>#<gate_iid>` line -- GitLab auto-renders this as a
working, bidirectional cross-reference system note with zero API-tier
requirement and zero extra calls. This is the *only* trust-relevant
linkage on the "read" side; the actual trust anchor for existence/reuse is
always the label pair, never any link object. `--link-type relates_to` is
an opt-in enhancement that additionally calls the GitLab Issue Links API
(`gitlab_write.create_gitlab_issue_link`); if that API is unavailable
(403/404), the whole run aborts (`GateIssuesBlocked`, CLI exit 2) naming
the unavailable capability -- it never silently degrades back to the
description-only floor.

## Assignee drift (confirmed default: report-only)

On reuse of an approval issue, this module compares GitLab's current
assignee username(s) against the resolved expected username. A mismatch is
recorded as `drift: "assignee_changed"` in both the ledger and the result
payload and forces CLI exit 2; it is never silently overwritten unless the
operator passes `--reconcile-assignees`, which calls
`gitlab_write.update_gitlab_issue_assignee` to make GitLab's state match
`authorities.json`.

## Self-approval / independence (spec §7)

GitLab enforces nothing about who is assigned an approval issue --this
module enforces it at creation time, comparing the candidate authority's
`authorities.json` `assignee` identity (the same identity string already
compared in `has_all_required_human_approvals`/`_resolve_gate_authority` in
`agentic_sdlc/__init__.py`) against every `gate["preparers"][].id` and
`gate["independent_verifier"].id` on the *run record*, not against the
GitLab username. Kernel-side, `preparers` is always `[]` and
`independent_verifier` is always `None`
(`make_gate_record`, `agentic_sdlc/__init__.py:1524-1525`), so this check
currently passes vacuously -- it is implemented anyway (not skipped for
being currently near-inert) because it becomes load-bearing the moment
preparers are populated by any other path. Every approval issue's
description also carries a fixed, module-emitted, non-approval advisory
(`_ADVISORY_TEMPLATE` below) making clear that closing the issue is not
approval evidence. The real control remains `record_gate_decision` /
`record_github_approval` / `record_gitlab_approval` via
`_resolve_gate_authority`, and `can_mark_gate_approved`'s
`independence_declaration.verifier_confirmed_not_preparer` +
non-empty `artifact_bindings`/`evidence_refs` requirement -- a closed
GitLab tracking issue moves none of that state.

## Strictly orthogonal to the approval adapters

This module never imports `record_github_approval`, `record_gitlab_approval`,
`record_gate_decision`, or `record_gitlab_issue_link` from the parent
`agentic_sdlc` package, and never writes `run-record.json`,
`dispatch-plan.json`, or `authorities.json` -- it only *reads* them, plus
its own sidecar ledger file. `test_gate_issues.py`'s `OrthogonalityTests`
class asserts both the module-import restriction (by source inspection,
mirroring the engine package's
`test_graph_module_never_references_requirement_issues`) and the
file-untouched behavior (byte-for-byte comparison of the three input files
before/after a full `--apply` run).

## Pre-existing kernel inconsistency worked around, not fixed (spec §4.3)

`make_gate_record` derives `authority_requirements[].applicability` purely
from `authorities.json`'s `status == "assigned"`
(`agentic_sdlc/__init__.py:1514`), so a legitimately not-applicable
conditional authority (e.g. `human_key_owner` with
`authorities.json["human_key_owner"]["applicability"] ==
"not-applicable"` plus a recorded rationale) still shows
`authority_requirements[].applicability == "unknown"` on the run record.
`build_plan()` below cross-checks `authorities.json`'s own `applicability`
field for exactly this case and classifies it as `skipped` (not
`refused`), surfacing the recorded rationale, with no exit-code effect --
`make_gate_record` itself is deliberately left unmodified.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import CONTRACTS, GATE_IDS, OVERLAY, authority_gitlab_username, confined_path, fingerprint, load_json, now
from . import gitlab_write

MAX_ISSUES_PER_RUN = 40
LEDGER_SCHEMA_VERSION = 1

FIXED_LABEL = gitlab_write.FIXED_LABEL
GATE_LABEL_PREFIX = "agentic-sdlc-gate-"
APPROVAL_LABEL_PREFIX = "agentic-sdlc-approval-"

_LABEL_CHARSET_RE = re.compile(r"^[a-z0-9-]+$")
_MENTION_RE = re.compile(r"(?<!\w)@(\w[\w.-]*)")
_CROSS_REF_RE = re.compile(r"(?<!\w)([\w.-]+/[\w.-]+)?#(\d+)")
_FORBIDDEN_TEXT_CHARS = {"\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"}

_PROVENANCE_LINE = "> Machine-generated by agentic-sdlc."
_REF_LINE_PREFIX = "> ref "
_PARENT_LINE_PREFIX = "> parent "
_RESERVED_PREFIXES = (_PROVENANCE_LINE, _REF_LINE_PREFIX, _PARENT_LINE_PREFIX)

_ADVISORY_TEMPLATE = (
    "{provenance} Tracking artifact only — closing this issue is not approval evidence "
    "and does not approve {gate_id}. The approver must not be a preparer or the independent "
    "verifier of this gate. Record approval via `agentic-sdlc approve-from-gitlab-mr` or "
    "`agentic-sdlc decide`."
)


class GateIssuesError(ValueError):
    """Structural/policy failure -- CLI maps this to exit code 1."""


class GateIssuesBlocked(ValueError):
    """Needs human resolution (ambiguous match, drift, lock held, digest
    mismatch, unavailable capability) -- CLI maps this to exit code 2."""


class _ApprovalRefusal(Exception):
    """Internal control-flow signal only: an approval candidate turned out
    to be unresolvable once a live GitLab call was made (username lookup
    returned 0 or >1 active matches). Caught by run() and folded into the
    refusals[] list -- never escapes this module."""

    def __init__(self, gate_id: str, authority_id: str, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.gate_id = gate_id
        self.authority_id = authority_id
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------
# Identity, markers, labels
# --------------------------------------------------------------------------


def task_hash(task_id: str) -> str:
    return hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16]


def compute_gate_marker(task_id: str, gate_id: str) -> str:
    digest = hashlib.sha256(f"gate\x00{task_id}\x00{gate_id}".encode("utf-8")).hexdigest()
    return digest[:16]


def compute_approval_marker(task_id: str, gate_id: str, authority_id: str) -> str:
    digest = hashlib.sha256(f"approval\x00{task_id}\x00{gate_id}\x00{authority_id}".encode("utf-8")).hexdigest()
    return digest[:16]


def gate_label(marker: str) -> str:
    label = f"{GATE_LABEL_PREFIX}{marker}"
    if not _LABEL_CHARSET_RE.fullmatch(label):
        raise GateIssuesError(f"computed label {label!r} violates the [a-z0-9-] label charset")
    return label


def approval_label(marker: str) -> str:
    label = f"{APPROVAL_LABEL_PREFIX}{marker}"
    if not _LABEL_CHARSET_RE.fullmatch(label):
        raise GateIssuesError(f"computed label {label!r} violates the [a-z0-9-] label charset")
    return label


# --------------------------------------------------------------------------
# Sanitization -- reject and abort, never substitute. Applied only to
# free-text fields sourced from authorities.json/run-record content
# (applicability_rationale, an authority requirement's rationale, and the
# optional --include-scope scope line); module-composed template text
# (banners, ref/parent lines) is never run through this.
# --------------------------------------------------------------------------


def _neutralize_references(text: str) -> str:
    zwsp = "\u200b"

    def _mention(match: re.Match[str]) -> str:
        return "@" + zwsp + match.group(1)

    def _crossref(match: re.Match[str]) -> str:
        prefix = match.group(1) or ""
        return f"{prefix}#{zwsp}{match.group(2)}"

    text = _MENTION_RE.sub(_mention, text)
    text = _CROSS_REF_RE.sub(_crossref, text)
    return text


def sanitize_free_text(text: str, field_name: str) -> str:
    for ch in text:
        if ch in _FORBIDDEN_TEXT_CHARS:
            raise GateIssuesError(
                f"{field_name}: contains a forbidden control/line-separator character (U+{ord(ch):04X})"
            )
        if 0xD800 <= ord(ch) <= 0xDFFF:
            raise GateIssuesError(f"{field_name}: contains a lone Unicode surrogate")
    lines = text.splitlines()
    for line in lines:
        if line.strip().startswith("/"):
            raise GateIssuesError(
                f"{field_name}: contains a line starting with '/' (GitLab quick-action injection risk)"
            )
    for line in lines:
        if any(line.startswith(prefix) for prefix in _RESERVED_PREFIXES):
            raise GateIssuesError(f"{field_name}: contains a reserved provenance/ref/parent-line prefix")
    sanitized = _neutralize_references(text)
    if len(sanitized) > 4000:
        raise GateIssuesError(f"{field_name}: exceeds 4000 characters")
    return sanitized


def sanitize_title_text(text: str, field_name: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    for ch in normalized:
        if not (0x20 <= ord(ch) <= 0x7E):
            raise GateIssuesError(f"{field_name}: contains a non-printable-ASCII character (U+{ord(ch):04X})")
    stripped = normalized.strip()
    if not stripped:
        raise GateIssuesError(f"{field_name}: empty after normalization/strip")
    if stripped[0] in ("-", "/"):
        raise GateIssuesError(f"{field_name}: must not start with '-' or '/'")
    if len(stripped) > 200:
        raise GateIssuesError(f"{field_name}: exceeds 200 characters")
    return stripped


# --------------------------------------------------------------------------
# Title/description rendering
# --------------------------------------------------------------------------


def gate_issue_title(task_id: str, gate_id: str, gate_name: str) -> str:
    raw = f"[agentic-sdlc] {gate_id} {gate_name} ({task_hash(task_id)})"
    return sanitize_title_text(raw, f"{gate_id} gate issue title")


def approval_issue_title(task_id: str, gate_id: str, gate_name: str, role: str) -> str:
    raw = f"[agentic-sdlc] Approve {gate_id} {gate_name} - {role} ({task_hash(task_id)})"
    return sanitize_title_text(raw, f"{gate_id} approval issue title")


def render_gate_description(
    task_id: str, gate_id: str, gate_name: str, phase: str, human_only: bool, marker: str,
    rationale: str | None, scope_text: str | None,
) -> str:
    lines = [
        f"{_PROVENANCE_LINE} Not a human-authored artifact. Not approval evidence.",
        f"{_REF_LINE_PREFIX}{task_hash(task_id)}/{gate_id}/{marker}",
        "",
        f"Gate: {gate_id} {gate_name} (phase: {phase})",
    ]
    if rationale:
        lines.append(
            f"Applicability rationale: {sanitize_free_text(rationale, f'{gate_id} applicability_rationale')}"
        )
    if human_only:
        lines.append(
            "This is a human-only gate -- automation cannot grant it (see contracts/lifecycle-gates.json "
            "human_only)."
        )
    if scope_text:
        lines.append(f"Scope: {sanitize_free_text(scope_text, f'{gate_id} scope')}")
    return "\n".join(lines) + "\n"


def render_approval_description(
    task_id: str, gate_id: str, marker: str, project_path: str, gate_iid: int, rationale: str | None,
) -> str:
    advisory = _ADVISORY_TEMPLATE.format(provenance=_PROVENANCE_LINE, gate_id=gate_id)
    lines = [
        advisory,
        f"{_REF_LINE_PREFIX}{task_hash(task_id)}/{gate_id}/{marker}",
        f"{_PARENT_LINE_PREFIX}{project_path}#{gate_iid}",
        "",
    ]
    if rationale:
        lines.append(sanitize_free_text(rationale, f"{gate_id} authority rationale"))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Self-approval / independence (spec §7)
# --------------------------------------------------------------------------


def _is_self_approval(assignee_id: Any, gate_record: dict[str, Any]) -> bool:
    """Kernel-side, `preparers` is always `[]` and `independent_verifier`
    is always `None` (`make_gate_record`), so this passes vacuously today
    -- implemented anyway since it becomes load-bearing once preparers are
    populated by any other path (spec §7 caveat)."""
    preparers = {item.get("id") for item in gate_record.get("preparers", []) if isinstance(item, dict)}
    verifier = gate_record.get("independent_verifier")
    verifier_id = verifier.get("id") if isinstance(verifier, dict) else None
    return assignee_id in preparers or (verifier_id is not None and assignee_id == verifier_id)


# --------------------------------------------------------------------------
# Eligibility (spec §5.2)
# --------------------------------------------------------------------------


def _configured_gate_ids(dispatch_plan: dict[str, Any]) -> set[str]:
    return {
        item.get("gate_id")
        for item in dispatch_plan.get("gate_dispatch", [])
        if item.get("status") == "required"
    }


def check_gate_eligibility(gate_id: str, dispatch_plan: dict[str, Any], gate_record: dict[str, Any] | None) -> None:
    if gate_id not in GATE_IDS:
        raise GateIssuesError(f"unknown gate id: {gate_id!r}")
    if gate_record is None:
        raise GateIssuesError(
            f"gate {gate_id} not found in the run record's lifecycle_gates array "
            "(lookup is by gate_id, not index; the array must contain exactly G1-G10)"
        )
    if gate_id not in _configured_gate_ids(dispatch_plan):
        raise GateIssuesBlocked(f"gate {gate_id} is not part of the task's configured (dispatch-plan) gate set")
    if gate_record.get("applicability") != "applicable":
        raise GateIssuesBlocked(
            f"gate {gate_id} applicability is {gate_record.get('applicability')!r}, not 'applicable'"
        )
    if gate_record.get("status") == "invalidated":
        raise GateIssuesBlocked(f"gate {gate_id} status is 'invalidated'")
    if gate_record.get("required_reentry_gate") is not None:
        raise GateIssuesBlocked(
            f"gate {gate_id} has a pending required_reentry_gate={gate_record.get('required_reentry_gate')!r}"
        )


def _default_gate_ids(dispatch_plan: dict[str, Any], gate_by_id: dict[str, dict[str, Any]]) -> list[str]:
    configured = _configured_gate_ids(dispatch_plan)
    result = []
    for gate_id in GATE_IDS:
        if gate_id not in configured:
            continue
        gate_record = gate_by_id.get(gate_id)
        if gate_record is None:
            continue
        if gate_record.get("applicability") != "applicable":
            continue
        if gate_record.get("status") == "invalidated":
            continue
        if gate_record.get("required_reentry_gate") is not None:
            continue
        result.append(gate_id)
    return result


# --------------------------------------------------------------------------
# Plan building (pure; shared by dry-run and apply, and re-run before every
# apply-mode item to detect concurrent state changes -- spec §3.5)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GatePlan:
    gate_id: str
    gate_name: str
    phase: str
    human_only: bool
    marker: str
    label: str
    title: str
    description: str


@dataclass(frozen=True)
class ApprovalCandidate:
    gate_id: str
    authority_id: str
    role: str
    marker: str
    label: str
    title: str
    username: str
    rationale: str | None


@dataclass(frozen=True)
class SkippedEntry:
    gate_id: str
    authority_id: str
    reason: str
    rationale: str | None


@dataclass(frozen=True)
class RefusalEntry:
    gate_id: str
    authority_id: str
    reason: str
    detail: str


def build_plan(
    *, task_id: str, project_path: str, gate_ids: list[str], record: dict[str, Any],
    authorities: dict[str, Any], dispatch_plan: dict[str, Any], lifecycle_contracts: dict[str, dict[str, Any]],
    include_scope: bool, scope_text: str | None,
) -> tuple[list[GatePlan], list[ApprovalCandidate], list[SkippedEntry], list[RefusalEntry], dict[str, Any]]:
    gate_by_id = {g["gate_id"]: g for g in record.get("lifecycle_gates", [])}
    gate_plans: list[GatePlan] = []
    approval_candidates: list[ApprovalCandidate] = []
    skipped: list[SkippedEntry] = []
    refusals: list[RefusalEntry] = []
    per_gate_digest: dict[str, Any] = {}

    for gate_id in gate_ids:
        gate_record = gate_by_id.get(gate_id)
        if gate_record is None:
            raise GateIssuesError(f"gate {gate_id} not found in the run record's lifecycle_gates array")
        contract = lifecycle_contracts.get(gate_id, {})
        gate_name = contract.get("name", gate_id)
        phase = contract.get("phase", "")
        human_only = bool(contract.get("human_only"))

        marker = compute_gate_marker(task_id, gate_id)
        label = gate_label(marker)
        title = gate_issue_title(task_id, gate_id, gate_name)
        description = render_gate_description(
            task_id, gate_id, gate_name, phase, human_only, marker,
            gate_record.get("applicability_rationale"),
            scope_text if include_scope else None,
        )
        gate_plans.append(GatePlan(gate_id, gate_name, phase, human_only, marker, label, title, description))

        authority_requirements = gate_record.get("authority_requirements", [])
        resolved_usernames: dict[str, str | None] = {}
        for requirement in authority_requirements:
            authority_id = requirement.get("authority_id")
            authority = authorities.get(authority_id)
            resolved_usernames[authority_id] = (
                authority_gitlab_username(authority) if isinstance(authority, dict) else None
            )

        for requirement in authority_requirements:
            authority_id = requirement.get("authority_id")
            applicability = requirement.get("applicability")
            authority = authorities.get(authority_id)
            role_label = requirement.get("role", authority_id)

            if applicability == "not-applicable":
                skipped.append(SkippedEntry(gate_id, authority_id, "not-applicable", requirement.get("rationale")))
                continue
            if applicability == "unknown":
                authority_applicability = authority.get("applicability") if isinstance(authority, dict) else None
                if authority_applicability == "not-applicable":
                    skipped.append(
                        SkippedEntry(
                            gate_id, authority_id, "authorities-not-applicable",
                            authority.get("rationale") if isinstance(authority, dict) else None,
                        )
                    )
                else:
                    refusals.append(
                        RefusalEntry(
                            gate_id, authority_id, "applicability-unknown",
                            "authority_requirements applicability is 'unknown' and authorities.json does not "
                            "mark this authority not-applicable",
                        )
                    )
                continue
            if applicability != "applicable":
                refusals.append(
                    RefusalEntry(gate_id, authority_id, "applicability-unknown", f"unrecognized applicability {applicability!r}")
                )
                continue
            if not isinstance(authority, dict):
                refusals.append(
                    RefusalEntry(gate_id, authority_id, "authority-unknown", f"role {authority_id} missing from authorities.json")
                )
                continue
            if authority.get("status") != "assigned" or not authority.get("assignee"):
                refusals.append(
                    RefusalEntry(gate_id, authority_id, "authority-unassigned", f"authority {authority_id} is not assigned")
                )
                continue
            username = resolved_usernames.get(authority_id)
            if not username:
                refusals.append(
                    RefusalEntry(gate_id, authority_id, "no-gitlab-binding", f"authority {authority_id} has no GitLab username binding")
                )
                continue
            if _is_self_approval(authority.get("assignee"), gate_record):
                refusals.append(
                    RefusalEntry(
                        gate_id, authority_id, "self-approval",
                        f"authority {authority_id}'s assignee is a preparer or the independent verifier of {gate_id}",
                    )
                )
                continue
            approval_marker = compute_approval_marker(task_id, gate_id, authority_id)
            approval_candidates.append(
                ApprovalCandidate(
                    gate_id=gate_id,
                    authority_id=authority_id,
                    role=role_label,
                    marker=approval_marker,
                    label=approval_label(approval_marker),
                    title=approval_issue_title(task_id, gate_id, gate_name, role_label),
                    username=username,
                    rationale=requirement.get("rationale"),
                )
            )

        per_gate_digest[gate_id] = {
            "applicability": gate_record.get("applicability"),
            "applicability_rationale": gate_record.get("applicability_rationale"),
            "status": gate_record.get("status"),
            "required_reentry_gate": gate_record.get("required_reentry_gate"),
            "authority_requirements": [
                {
                    "authority_id": requirement.get("authority_id"),
                    "applicability": requirement.get("applicability"),
                    "rationale": requirement.get("rationale"),
                }
                for requirement in authority_requirements
            ],
            "resolved_usernames": resolved_usernames,
        }

    total = len(gate_plans) + len(approval_candidates)
    if total > MAX_ISSUES_PER_RUN:
        raise GateIssuesError(
            f"planned issue count {total} exceeds MAX_ISSUES_PER_RUN={MAX_ISSUES_PER_RUN} -- aborting rather "
            "than truncating"
        )

    return gate_plans, approval_candidates, skipped, refusals, per_gate_digest


def compute_plan_digest(
    *, task_id: str, project_path: str, gate_ids: list[str], dispatch_fingerprint_value: str | None,
    per_gate: dict[str, Any], disposition: str | None, classification: str | None, re_entry_count: int,
) -> str:
    payload = {
        "task_id": task_id,
        "project_path": project_path,
        "gate_ids": list(gate_ids),
        "dispatch_fingerprint": dispatch_fingerprint_value,
        "per_gate": per_gate,
        "eligibility": {
            "disposition": disposition,
            "classification": classification,
            "re_entry_count": re_entry_count,
        },
    }
    return fingerprint(payload)


# --------------------------------------------------------------------------
# Sidecar ledger (diagnostics only, never trusted for existence)
# --------------------------------------------------------------------------


def _ledger_path(root: Path, task_id: str) -> Path:
    return confined_path(root, OVERLAY, "runs", task_id, "gate-issues.json")


def _lock_path(root: Path, task_id: str) -> Path:
    return confined_path(root, OVERLAY, "runs", task_id, "gate-issues.lock")


def _empty_ledger(task_id: str) -> dict[str, Any]:
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "task_id": task_id,
        "project_path": None,
        "bot_username": None,
        "mocked": False,
        "entries": {},
    }


def read_ledger(root: Path, task_id: str) -> dict[str, Any]:
    path = _ledger_path(Path(root), task_id)
    if not path.is_file():
        return _empty_ledger(task_id)
    return json.loads(path.read_text(encoding="utf-8"))


def write_ledger(root: Path, task_id: str, ledger: dict[str, Any]) -> None:
    """Same durable-write sequence as the LangGraph engine package's
    `requirement_issues.write_ledger`: full-file rewrite, same-filesystem
    tmp file, fsync data, atomic rename, fsync the containing directory."""
    path = _ledger_path(Path(root), task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(ledger, indent=2, sort_keys=True) + "\n").encode("utf-8")

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".gate-issues.", suffix=".tmp")
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp_name, 0o600)
    os.replace(tmp_name, path)

    dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def acquire_lock(root: Path, task_id: str, *, break_lock: bool) -> Path:
    path = _lock_path(Path(root), task_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    if break_lock and path.is_file():
        path.unlink(missing_ok=True)

    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        holder = "(unreadable)"
        try:
            holder = path.read_text(encoding="utf-8")
        except OSError:
            pass
        raise GateIssuesBlocked(
            f"gate-issues.lock is already held -- pass --break-lock to override "
            f"(never auto-broken on timeout). Holder:\n{holder}"
        ) from None

    payload = json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "started_at": now()}, indent=2)
    os.write(fd, payload.encode("utf-8"))
    os.close(fd)
    return path


def release_lock(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------
# Reuse validation
# --------------------------------------------------------------------------


def _validate_matched_issue(issue: dict[str, Any], *, own_label: str, foreign_prefix: str, context: str) -> None:
    labels = set(issue.get("labels") or [])
    if FIXED_LABEL not in labels:
        raise GateIssuesBlocked(f"{context}: matched issue is missing the {FIXED_LABEL!r} anchor label")
    if own_label not in labels:
        raise GateIssuesBlocked(f"{context}: matched issue is missing its own label {own_label!r}")
    foreign = {label for label in labels if label.startswith(foreign_prefix) and label != own_label}
    if foreign:
        raise GateIssuesBlocked(
            f"{context}: matched issue carries a foreign label {sorted(foreign)} -- possible mismatch/poisoned issue"
        )


# --------------------------------------------------------------------------
# Per-issue processing (spec §3.2-3.6, §5.3-5.4)
# --------------------------------------------------------------------------


def _resolve_active_user_matches(username: str) -> list[dict[str, Any]]:
    matches = gitlab_write.resolve_gitlab_user_id(username)
    return [
        entry
        for entry in matches
        if isinstance(entry, dict) and isinstance(entry.get("id"), int) and entry.get("state", "active") == "active"
    ]


def _resolve_single_active_user(username: str) -> int | None:
    active = _resolve_active_user_matches(username)
    return active[0]["id"] if len(active) == 1 else None


def _process_gate_issue(
    root: Path, task_id: str, project_path: str, gp: GatePlan, ledger: dict[str, Any], *,
    bot_username: str, mocked: bool,
) -> dict[str, Any]:
    try:
        return _process_gate_issue_inner(
            root, task_id, project_path, gp, ledger, bot_username=bot_username, mocked=mocked
        )
    except (GateIssuesError, GateIssuesBlocked):
        raise
    except ValueError as exc:
        # Plain `ValueError` from `gitlab_write.py` (a failed `glab` call,
        # malformed response, ...) is a structural failure, not a
        # human-resolvable ambiguity -- map it to exit code 1, not 2, with
        # gate-id context added (mirrors the LangGraph engine package's
        # `requirement_issues._process_item`).
        raise GateIssuesError(f"gate {gp.gate_id!r}: {exc}") from exc


def _process_gate_issue_inner(
    root: Path, task_id: str, project_path: str, gp: GatePlan, ledger: dict[str, Any], *,
    bot_username: str, mocked: bool,
) -> dict[str, Any]:
    entry_key = gp.gate_id
    existing = gitlab_write.search_gitlab_issues_by_labels(project_path, [FIXED_LABEL, gp.label])
    if len(existing) > 1:
        raise GateIssuesBlocked(
            f"gate {gp.gate_id}: {len(existing)} issues matched labels [{FIXED_LABEL}, {gp.label}] -- "
            "ambiguous identity, needs human resolution"
        )

    if len(existing) == 1:
        issue = existing[0]
        _validate_matched_issue(
            issue, own_label=gp.label, foreign_prefix=GATE_LABEL_PREFIX, context=f"gate {gp.gate_id}"
        )
        iid = issue.get("iid")
        verification = gitlab_write.fetch_gitlab_issue_verification(project_path, iid)
        verified_author = (verification.get("author_username") or "").lower()
        if verified_author != bot_username.lower():
            entry = {
                "kind": "gate", "gate_id": gp.gate_id, "marker": gp.marker, "status": "suspect",
                "issue_iid": iid, "issue_state": verification.get("state"),
                "attempted_at": now(), "recorded_at": now(),
                "detail": "matched issue author does not match the verified bot identity",
            }
            ledger["entries"][entry_key] = entry
            ledger["mocked"] = mocked
            write_ledger(root, task_id, ledger)
            raise GateIssuesBlocked(
                f"gate {gp.gate_id}: matched issue's author does not match the verified bot identity -- "
                "refusing to reuse, needs human resolution"
            )
        entry = {
            "kind": "gate", "gate_id": gp.gate_id, "marker": gp.marker, "status": "reused",
            "issue_iid": iid, "issue_state": verification.get("state"),
            "attempted_at": now(), "recorded_at": now(), "detail": None,
        }
        ledger["entries"][entry_key] = entry
        ledger["mocked"] = mocked
        write_ledger(root, task_id, ledger)
        return {"gate_id": gp.gate_id, "status": "reused", "issue_iid": iid, "issue_state": verification.get("state")}

    entry = {
        "kind": "gate", "gate_id": gp.gate_id, "marker": gp.marker, "status": "creating",
        "issue_iid": None, "issue_state": None, "attempted_at": now(), "recorded_at": None, "detail": None,
    }
    ledger["entries"][entry_key] = entry
    ledger["mocked"] = mocked
    write_ledger(root, task_id, ledger)  # BEFORE the GitLab API call.

    iid = gitlab_write.create_gitlab_issue(project_path, gp.title, gp.description, [FIXED_LABEL, gp.label])
    verification = gitlab_write.fetch_gitlab_issue_verification(project_path, iid)

    failures: list[str] = []
    if verification.get("title") != gp.title:
        failures.append("title")
    if verification.get("state") != "opened":
        failures.append("state")
    if set(verification.get("labels") or []) != {FIXED_LABEL, gp.label}:
        failures.append("labels")
    if verification.get("assignee_count", 0) != 0:
        failures.append("assignee_count")
    if verification.get("confidential", False) is not False:
        failures.append("confidential")
    if verification.get("project_path") != project_path:
        failures.append("project_path")
    if (verification.get("author_username") or "").lower() != bot_username.lower():
        failures.append("author_username")

    if failures:
        entry = dict(entry)
        entry.update(status="suspect", issue_iid=iid, issue_state=verification.get("state"), recorded_at=now())
        entry["detail"] = f"post-creation verification failed: {', '.join(failures)}"
        ledger["entries"][entry_key] = entry
        write_ledger(root, task_id, ledger)
        raise GateIssuesBlocked(
            f"gate {gp.gate_id}: post-creation verification failed ({', '.join(failures)}) -- aborting the "
            "entire run immediately"
        )

    entry = dict(entry)
    entry.update(status="created", issue_iid=iid, issue_state=verification.get("state"), recorded_at=now())
    ledger["entries"][entry_key] = entry
    write_ledger(root, task_id, ledger)
    return {"gate_id": gp.gate_id, "status": "created", "issue_iid": iid, "issue_state": verification.get("state")}


def _process_approval_issue(
    root: Path, task_id: str, project_path: str, ac: ApprovalCandidate, gate_iid: int, ledger: dict[str, Any], *,
    bot_username: str, mocked: bool, link_type: str | None, reconcile_assignees: bool,
) -> dict[str, Any]:
    try:
        return _process_approval_issue_inner(
            root, task_id, project_path, ac, gate_iid, ledger,
            bot_username=bot_username, mocked=mocked, link_type=link_type, reconcile_assignees=reconcile_assignees,
        )
    except (GateIssuesError, GateIssuesBlocked, _ApprovalRefusal):
        raise
    except ValueError as exc:
        # Plain `ValueError` from `gitlab_write.py` (a failed `glab` call,
        # malformed response, ...) is a structural failure, not a
        # human-resolvable ambiguity -- map it to exit code 1, not 2, with
        # gate/authority-id context added (mirrors the LangGraph engine
        # package's `requirement_issues._process_item`).
        raise GateIssuesError(f"gate {ac.gate_id!r} authority {ac.authority_id!r}: {exc}") from exc


def _process_approval_issue_inner(
    root: Path, task_id: str, project_path: str, ac: ApprovalCandidate, gate_iid: int, ledger: dict[str, Any], *,
    bot_username: str, mocked: bool, link_type: str | None, reconcile_assignees: bool,
) -> dict[str, Any]:
    entry_key = f"{ac.gate_id}/{ac.authority_id}"
    context = entry_key
    existing = gitlab_write.search_gitlab_issues_by_labels(project_path, [FIXED_LABEL, ac.label])
    if len(existing) > 1:
        raise GateIssuesBlocked(
            f"{context}: {len(existing)} issues matched labels [{FIXED_LABEL}, {ac.label}] -- ambiguous "
            "identity, needs human resolution"
        )

    if len(existing) == 1:
        issue = existing[0]
        _validate_matched_issue(issue, own_label=ac.label, foreign_prefix=APPROVAL_LABEL_PREFIX, context=context)
        iid = issue.get("iid")
        verification = gitlab_write.fetch_gitlab_issue_assignment_verification(project_path, iid)
        verified_author = (verification.get("author_username") or "").lower()
        if verified_author != bot_username.lower():
            entry = {
                "kind": "approval", "gate_id": ac.gate_id, "authority_id": ac.authority_id, "marker": ac.marker,
                "status": "suspect", "issue_iid": iid, "issue_state": verification.get("state"),
                "attempted_at": now(), "recorded_at": now(),
                "detail": "matched issue author does not match the verified bot identity",
            }
            ledger["entries"][entry_key] = entry
            ledger["mocked"] = mocked
            write_ledger(root, task_id, ledger)
            raise GateIssuesBlocked(
                f"{context}: matched issue's author does not match the verified bot identity -- refusing to "
                "reuse, needs human resolution"
            )

        current_assignees = [u.lower() for u in verification.get("assignee_usernames", [])]
        drift = None
        if current_assignees != [ac.username.lower()]:
            drift = "assignee_changed"
            if reconcile_assignees:
                resolved_id = _resolve_single_active_user(ac.username)
                if resolved_id is None:
                    raise GateIssuesBlocked(
                        f"{context}: cannot reconcile assignee -- username {ac.username!r} did not resolve "
                        "to exactly one active GitLab user"
                    )
                gitlab_write.update_gitlab_issue_assignee(project_path, iid, [resolved_id])
                drift = "assignee_changed (reconciled)"

        entry = {
            "kind": "approval", "gate_id": ac.gate_id, "authority_id": ac.authority_id, "marker": ac.marker,
            "status": "reused", "issue_iid": iid, "issue_state": verification.get("state"),
            "attempted_at": now(), "recorded_at": now(), "detail": drift,
        }
        ledger["entries"][entry_key] = entry
        ledger["mocked"] = mocked
        write_ledger(root, task_id, ledger)
        return {
            "gate_id": ac.gate_id, "authority_id": ac.authority_id, "status": "reused",
            "issue_iid": iid, "issue_state": verification.get("state"), "drift": drift,
        }

    active = _resolve_active_user_matches(ac.username)
    if len(active) == 0:
        raise _ApprovalRefusal(
            ac.gate_id, ac.authority_id, "gitlab-user-unresolved",
            f"username {ac.username!r} resolved to 0 active GitLab users",
        )
    if len(active) > 1:
        raise _ApprovalRefusal(
            ac.gate_id, ac.authority_id, "gitlab-user-ambiguous",
            f"username {ac.username!r} resolved to {len(active)} active GitLab users",
        )
    resolved_id = active[0]["id"]

    entry = {
        "kind": "approval", "gate_id": ac.gate_id, "authority_id": ac.authority_id, "marker": ac.marker,
        "status": "creating", "issue_iid": None, "issue_state": None,
        "attempted_at": now(), "recorded_at": None, "detail": None,
    }
    ledger["entries"][entry_key] = entry
    ledger["mocked"] = mocked
    write_ledger(root, task_id, ledger)  # BEFORE the GitLab API call.

    description = render_approval_description(task_id, ac.gate_id, ac.marker, project_path, gate_iid, ac.rationale)
    iid = gitlab_write.create_gitlab_issue(
        project_path, ac.title, description, [FIXED_LABEL, ac.label], assignee_ids=[resolved_id]
    )
    verification = gitlab_write.fetch_gitlab_issue_assignment_verification(project_path, iid)

    failures: list[str] = []
    if verification.get("title") != ac.title:
        failures.append("title")
    if verification.get("state") != "opened":
        failures.append("state")
    if set(verification.get("labels") or []) != {FIXED_LABEL, ac.label}:
        failures.append("labels")
    if [u.lower() for u in verification.get("assignee_usernames", [])] != [ac.username.lower()]:
        failures.append("assignee_usernames")
    if verification.get("confidential", False) is not False:
        failures.append("confidential")
    if verification.get("project_path") != project_path:
        failures.append("project_path")
    if (verification.get("author_username") or "").lower() != bot_username.lower():
        failures.append("author_username")

    if failures:
        entry = dict(entry)
        entry.update(status="suspect", issue_iid=iid, issue_state=verification.get("state"), recorded_at=now())
        entry["detail"] = f"post-creation verification failed: {', '.join(failures)}"
        ledger["entries"][entry_key] = entry
        write_ledger(root, task_id, ledger)
        raise GateIssuesBlocked(
            f"{context}: post-creation verification failed ({', '.join(failures)}) -- aborting the entire run "
            "immediately"
        )

    if link_type:
        try:
            gitlab_write.create_gitlab_issue_link(project_path, iid, project_path, gate_iid, link_type=link_type)
        except gitlab_write.IssueLinksUnavailable as exc:
            entry = dict(entry)
            entry.update(status="suspect", issue_iid=iid, issue_state=verification.get("state"), recorded_at=now())
            entry["detail"] = f"issue link creation failed: {exc}"
            ledger["entries"][entry_key] = entry
            write_ledger(root, task_id, ledger)
            raise GateIssuesBlocked(
                f"{context}: GitLab Issue Links API unavailable ({exc}) -- re-run without --link-type to rely "
                "on the description cross-reference floor only"
            ) from exc

    entry = dict(entry)
    entry.update(status="created", issue_iid=iid, issue_state=verification.get("state"), recorded_at=now())
    ledger["entries"][entry_key] = entry
    write_ledger(root, task_id, ledger)
    return {
        "gate_id": ac.gate_id, "authority_id": ac.authority_id, "status": "created",
        "issue_iid": iid, "issue_state": verification.get("state"), "drift": None,
    }


# --------------------------------------------------------------------------
# Top-level orchestration
# --------------------------------------------------------------------------


def run(
    *, root: Path, task_id: str, project_path: str, as_bot: str, gates: list[str] | None,
    apply: bool, plan_digest: str | None, allow_classification: str | None,
    link_type: str | None, include_scope: bool, reconcile_assignees: bool,
    break_lock: bool, i_know_this_is_mocked: bool,
) -> dict[str, Any]:
    root = Path(root)
    overlay_dir = confined_path(root, OVERLAY)
    project = load_json(overlay_dir / "project.json")
    record_path = confined_path(root, OVERLAY, "runs", task_id, "run-record.json")
    dispatch_path = confined_path(root, OVERLAY, "runs", task_id, "dispatch-plan.json")
    record = load_json(record_path)
    dispatch_plan = load_json(dispatch_path)
    authorities = load_json(overlay_dir / "authorities.json")
    lifecycle_contracts = {item["id"]: item for item in load_json(CONTRACTS / "lifecycle-gates.json")["gates"]}
    gate_by_id = {item["gate_id"]: item for item in record.get("lifecycle_gates", [])}

    if allow_classification is None or allow_classification != record.get("classification"):
        raise GateIssuesError(
            "--allow-classification must be supplied and exactly match the task's classification "
            f"(got {allow_classification!r}, task classification is {record.get('classification')!r})"
        )

    if gates:
        gate_ids = []
        for gate_id in gates:
            check_gate_eligibility(gate_id, dispatch_plan, gate_by_id.get(gate_id))
            gate_ids.append(gate_id)
    else:
        gate_ids = _default_gate_ids(dispatch_plan, gate_by_id)

    def _build(rec: dict[str, Any], auth: dict[str, Any], plan: dict[str, Any]):
        return build_plan(
            task_id=task_id, project_path=project_path, gate_ids=gate_ids, record=rec, authorities=auth,
            dispatch_plan=plan, lifecycle_contracts=lifecycle_contracts, include_scope=include_scope,
            scope_text=rec.get("scope") if include_scope else None,
        )

    def _digest(rec: dict[str, Any], auth: dict[str, Any], plan: dict[str, Any], per_gate: dict[str, Any]) -> str:
        return compute_plan_digest(
            task_id=task_id, project_path=project_path, gate_ids=gate_ids,
            dispatch_fingerprint_value=plan.get("dispatch_fingerprint"), per_gate=per_gate,
            disposition=rec.get("disposition"), classification=rec.get("classification"),
            re_entry_count=len(rec.get("re_entry_history", [])),
        )

    gate_plans, approval_candidates, skipped, refusals, per_gate_digest = _build(record, authorities, dispatch_plan)
    digest = _digest(record, authorities, dispatch_plan, per_gate_digest)

    mocked = bool(os.environ.get(gitlab_write.ISSUE_CREATE_MOCK_ENV_VAR))

    if not apply:
        return {
            "mode": "dry-run",
            "plan_digest": digest,
            "project_path": project_path,
            "gate_ids": gate_ids,
            "mocked": mocked,
            "gate_issues": [{"gate_id": gp.gate_id, "marker": gp.marker, "label": gp.label} for gp in gate_plans],
            "approval_issues": [
                {"gate_id": ac.gate_id, "authority_id": ac.authority_id, "marker": ac.marker, "label": ac.label}
                for ac in approval_candidates
            ],
            "skipped": [
                {"gate_id": s.gate_id, "authority_id": s.authority_id, "reason": s.reason, "rationale": s.rationale}
                for s in skipped
            ],
            "refusals": [
                {"gate_id": r.gate_id, "authority_id": r.authority_id, "reason": r.reason, "detail": r.detail}
                for r in refusals
            ],
        }

    if plan_digest is None:
        raise GateIssuesError("--apply requires --plan-digest (from a prior --dry-run)")
    if plan_digest != digest:
        raise GateIssuesBlocked(
            f"--plan-digest mismatch: recomputed {digest!r} != supplied {plan_digest!r} -- state changed since "
            "the --dry-run this digest came from; re-run --dry-run"
        )

    if mocked and not i_know_this_is_mocked:
        raise GateIssuesError(
            f"{gitlab_write.ISSUE_CREATE_MOCK_ENV_VAR} is set but --i-know-this-is-mocked was not passed -- "
            "refusing to --apply against a mocked GitLab backend"
        )

    try:
        verified_username = gitlab_write.verify_gitlab_identity(as_bot)
    except ValueError as exc:
        raise GateIssuesError(str(exc)) from exc

    lock_path = acquire_lock(root, task_id, break_lock=break_lock)
    try:
        ledger = read_ledger(root, task_id)
        ledger["schema_version"] = LEDGER_SCHEMA_VERSION
        ledger["task_id"] = task_id
        ledger["project_path"] = project_path
        ledger["bot_username"] = verified_username
        ledger["mocked"] = mocked
        ledger.setdefault("entries", {})

        def _fresh_digest() -> str:
            fresh_record = load_json(record_path)
            fresh_dispatch = load_json(dispatch_path)
            fresh_authorities = load_json(overlay_dir / "authorities.json")
            _, _, _, _, fresh_per_gate = _build(fresh_record, fresh_authorities, fresh_dispatch)
            return _digest(fresh_record, fresh_authorities, fresh_dispatch, fresh_per_gate)

        approvals_by_gate: dict[str, list[ApprovalCandidate]] = {}
        for ac in approval_candidates:
            approvals_by_gate.setdefault(ac.gate_id, []).append(ac)

        gate_results = []
        approval_results = []
        run_refusals = list(refusals)

        for gp in gate_plans:
            if _fresh_digest() != plan_digest:
                raise GateIssuesBlocked(
                    f"plan digest changed before gate {gp.gate_id!r} (a concurrent edit happened) -- aborting "
                    "remaining items; already-created issues are unaffected"
                )
            gate_result = _process_gate_issue(
                root, task_id, project_path, gp, ledger, bot_username=verified_username, mocked=mocked
            )
            gate_results.append(gate_result)
            gate_iid = gate_result["issue_iid"]

            for ac in approvals_by_gate.get(gp.gate_id, []):
                if _fresh_digest() != plan_digest:
                    raise GateIssuesBlocked(
                        f"plan digest changed before approval {gp.gate_id}/{ac.authority_id} (a concurrent "
                        "edit happened) -- aborting remaining items; already-created issues are unaffected"
                    )
                try:
                    approval_result = _process_approval_issue(
                        root, task_id, project_path, ac, gate_iid, ledger,
                        bot_username=verified_username, mocked=mocked,
                        link_type=link_type, reconcile_assignees=reconcile_assignees,
                    )
                except _ApprovalRefusal as refusal:
                    run_refusals.append(RefusalEntry(refusal.gate_id, refusal.authority_id, refusal.reason, refusal.detail))
                    continue
                approval_results.append(approval_result)

        drift_present = any(
            result.get("drift") and str(result["drift"]).startswith("assignee_changed") and "reconciled" not in str(result["drift"])
            for result in approval_results
        )

        return {
            "mode": "apply",
            "plan_digest": digest,
            "project_path": project_path,
            "gate_ids": gate_ids,
            "mocked": mocked,
            "bot_username": verified_username,
            "gate_results": gate_results,
            "approval_results": approval_results,
            "skipped": [
                {"gate_id": s.gate_id, "authority_id": s.authority_id, "reason": s.reason, "rationale": s.rationale}
                for s in skipped
            ],
            "refusals": [
                {"gate_id": r.gate_id, "authority_id": r.authority_id, "reason": r.reason, "detail": r.detail}
                for r in run_refusals
            ],
            "drift_detected": drift_present,
        }
    finally:
        release_lock(lock_path)
