"""Agent-node machinery: the `ModelClient` protocol, a real Anthropic-backed
implementation, a deterministic fake for tests, and the LangGraph node
factory that wraps either one.

Port of the "agent nodes get a factory" idea from the architecture plan
(`make_agent_node(agent_id, kind, role_prompt, model_client)`), simplified
slightly: the role prompt is derived from `agent_id`/`kind`/an agent's own
catalog metadata/the active profile inside the node (via
`resolve_role_prompt`, a Phase 2 port of `agent_wrapper_body` /
`rich_agent_content` / `agent_wrapper_instructions`) rather than threaded
through as a separate constructor argument. `resolve_role_prompt` supports
both a provider-supplied rich role definition (`profile["rich_content_source"]`
+ an agent's `definition` file) and the generic templated instruction
fallback used whenever no richer source is available or opted into --
today, every shipped profile/catalog in this project uses the generic
fallback (see `resolve_role_prompt`'s docstring).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, TypedDict

from .provider import provider_resource


class Identity(TypedDict):
    id: str
    role: str
    kind: str  # "human" | "agent" | "service" -- schema's Identity.kind


class ArtifactBinding(TypedDict):
    artifact_id: str
    revision: str
    digest: str


class EvidenceRef(TypedDict):
    evidence_id: str
    uri: str
    hash_algorithm: str
    hash: str
    classification: str


class AgentOutput(TypedDict):
    agent_id: str
    kind: str  # "author" | "reviewer" -- dispatch role, not schema Identity.kind
    gate_id: str
    identity: Identity
    artifact_binding: ArtifactBinding
    evidence_ref: EvidenceRef | None
    blocking_question: str | None


class ModelClient(Protocol):
    def complete(
        self,
        *,
        agent_id: str,
        kind: str,
        gate_id: str,
        role_prompt: str,
        task_text: str,
    ) -> AgentOutput:
        ...


ASK_HUMAN_RULE = (
    "You are a dispatched subagent: you cannot ask the human directly. If you reach a "
    "decision only a human can make, stop and return a clearly labeled blocking question "
    "in your result instead of guessing or proceeding."
)

RICH_CONTENT_ADAPTATION_NOTE = (
    "Adapted from a role definition bundled with a provider's agent catalog. Review and "
    "tailor this role for this project's own stack, policies, and gates before relying on "
    "it -- shared-policy references in the source repository it came from will not resolve "
    "here."
)


def _agent_wrapper_instructions(agent_id: str, reviewer: bool) -> str:
    """Port of `agent_wrapper_instructions` (agentic_sdlc.py ~674-681):
    the generic templated role instruction used whenever a richer,
    provider-supplied role definition isn't available (or isn't opted
    into by the profile)."""
    return (
        f"Act as the portable Agentic SDLC role {agent_id}. "
        "Bind work to the task revision and lifecycle gate. "
        "Never approve a lifecycle or mutation gate. "
        + (
            "Remain independent and do not modify the artifact under review."
            if reviewer
            else "Prepare artifacts for independent review; do not self-review."
        )
        + " "
        + ASK_HUMAN_RULE
    )


def _rich_agent_content(definition: Any, provider_root: str | Path | None) -> str | None:
    """Port of `rich_agent_content` (agentic_sdlc.py ~700-705), extended
    with path confinement (`provider_resource`) when a `provider_root` is
    supplied. Returns `None` (triggering the generic-instruction
    fallback) whenever `definition` is missing, escapes its provider
    root, or doesn't resolve to a real file -- never raises."""
    if not isinstance(definition, str) or not definition:
        return None
    if provider_root is not None:
        try:
            path = provider_resource(Path(provider_root), definition, "definition", directory=False)
        except ValueError:
            return None
    else:
        # No root supplied: trust `definition` only if it is already an
        # absolute path (the expected shape once a provider has been
        # loaded via `provider.load_provider`, which resolves and
        # confines `definition` once, at load time -- see its
        # docstring). A relative path with no root to confine against
        # is treated as unresolved rather than resolved against cwd,
        # which would be an implicit, unconfined escape hatch.
        candidate = Path(definition)
        if not candidate.is_absolute():
            return None
        path = candidate
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip()


def resolve_role_prompt(
    agent_id: str,
    kind: Literal["author", "reviewer"],
    metadata: dict[str, Any],
    profile: dict[str, Any],
    *,
    provider_root: str | Path | None = None,
) -> str:
    """Port of `agent_wrapper_body` (agentic_sdlc.py ~708-713).

    If `profile.get("rich_content_source")` is truthy and
    `metadata.get("definition")` points to a real, path-confined file,
    returns that file's stripped contents plus `RICH_CONTENT_ADAPTATION_NOTE`
    plus `ASK_HUMAN_RULE`. Otherwise returns the generic templated
    instruction (`_agent_wrapper_instructions`, which already ends in
    `ASK_HUMAN_RULE`).

    None of the three shipped profiles (`generic`/`quick`/`web-service`)
    or the `agentic-sdlc-defaults` agent catalog set `rich_content_source`
    or `definition` today, so in this project's real fixtures every agent
    still gets the generic templated instruction -- this mechanism exists
    for a future provider (e.g. `secure-cloud-agents`, which ships real
    `AGENT.md` role definitions) to opt into.

    Deviation from the task spec's literal 4-argument signature: an
    optional keyword-only `provider_root` was added. Confinement ("a
    definition can't escape its provider root") is meaningless without a
    root to confine against. In production, `metadata["definition"]` is
    already an absolute, pre-confined path by the time it reaches here --
    `provider.load_provider` resolves and confines it once, at catalog-load
    time (mirroring the legacy CLI's `load_agent_catalog()`) -- so
    `provider_root` is normally omitted. It exists so this function can
    also be unit-tested directly against a relative `definition` without
    first going through the full provider loader.
    """
    if profile.get("rich_content_source"):
        rich = _rich_agent_content(metadata.get("definition"), provider_root)
        if rich is not None:
            return "\n\n".join([rich, RICH_CONTENT_ADAPTATION_NOTE, ASK_HUMAN_RULE])
    return _agent_wrapper_instructions(agent_id, kind == "reviewer")


@dataclass
class FakeModelClient:
    """Deterministic, no-network stand-in for tests / the phase-0 smoke
    test. Never calls out to Anthropic. Canned output is a pure function of
    `agent_id`/`kind`/`gate_id` so runs are reproducible."""

    blocking_agents: set[str] = field(default_factory=set)

    def complete(
        self,
        *,
        agent_id: str,
        kind: str,
        gate_id: str,
        role_prompt: str,
        task_text: str,
    ) -> AgentOutput:
        digest_filler = "0" * 64
        blocking = agent_id in self.blocking_agents
        return AgentOutput(
            agent_id=agent_id,
            kind=kind,
            gate_id=gate_id,
            identity=Identity(id=agent_id, role=f"{kind}:{agent_id}", kind="agent"),
            artifact_binding=ArtifactBinding(
                artifact_id=f"{gate_id}-{agent_id}-artifact",
                revision="rev-1",
                digest=f"sha256:{digest_filler}",
            ),
            evidence_ref=EvidenceRef(
                evidence_id=f"{gate_id}-{agent_id}-evidence",
                uri=f"fake://evidence/{gate_id}/{agent_id}",
                hash_algorithm="sha256",
                hash=digest_filler,
                classification="internal",
            ),
            blocking_question=(
                f"{agent_id} needs clarification before proceeding" if blocking else None
            ),
        )


@dataclass
class AnthropicModelClient:
    """Real Anthropic-backed implementation. Not exercised in this
    environment (no ANTHROPIC_API_KEY configured) -- exists so the spike
    isn't fake-only, per the phase-0 spec. Uses tool-use for a structured
    reply so we don't have to hand-roll prose parsing.
    """

    model: str = "claude-sonnet-4-5"
    api_key: str | None = None

    def _client(self):  # -> anthropic.Anthropic
        import anthropic  # local import: keep this optional dependency lazy

        return anthropic.Anthropic(api_key=self.api_key or os.environ.get("ANTHROPIC_API_KEY"))

    def complete(
        self,
        *,
        agent_id: str,
        kind: str,
        gate_id: str,
        role_prompt: str,
        task_text: str,
    ) -> AgentOutput:
        tool = {
            "name": "submit_contribution",
            "description": "Submit this agent's structured contribution for the gate.",
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["artifact_id", "revision", "summary"],
                "properties": {
                    "artifact_id": {"type": "string"},
                    "revision": {"type": "string"},
                    "summary": {"type": "string"},
                    "blocking_question": {"type": ["string", "null"]},
                },
            },
        }
        client = self._client()
        response = client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=role_prompt,
            tools=[tool],
            tool_choice={"type": "tool", "name": "submit_contribution"},
            messages=[{"role": "user", "content": task_text}],
        )
        payload: dict[str, Any] = {}
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "submit_contribution":
                payload = block.input
                break
        digest_filler = "0" * 64
        return AgentOutput(
            agent_id=agent_id,
            kind=kind,
            gate_id=gate_id,
            identity=Identity(id=agent_id, role=f"{kind}:{agent_id}", kind="agent"),
            artifact_binding=ArtifactBinding(
                artifact_id=payload.get("artifact_id", f"{gate_id}-{agent_id}-artifact"),
                revision=payload.get("revision", "rev-1"),
                digest=f"sha256:{digest_filler}",
            ),
            evidence_ref=EvidenceRef(
                evidence_id=f"{gate_id}-{agent_id}-evidence",
                uri=f"anthropic://response/{gate_id}/{agent_id}",
                hash_algorithm="sha256",
                hash=digest_filler,
                classification="internal",
            ),
            blocking_question=payload.get("blocking_question"),
        )


@dataclass
class A2AModelClient:
    """Dispatches `.complete()` to one external, A2A-reachable agent
    (e.g. a Codex CLI agent) over `message/send`, translating the
    returned `Task` back into an `AgentOutput` of the same shape
    `AnthropicModelClient.complete` builds.

    Deliberately synchronous, single-shot (`message/send`, not
    `message/stream`): `ModelClient.complete` is called from inside
    `make_agent_node`'s `node(payload)` closure, which runs synchronously
    as one LangGraph node and has no way to consume a streamed partial
    result anyway -- streaming is only exposed on the A2A *server* side
    (`a2a/server.py`), for external callers watching this engine's own
    gates progress. If the external agent's task ends in
    `input-required`, that's surfaced as a `blocking_question` rather
    than treated as an error, matching how a human-in-the-loop author
    reports "I can't decide this" today.
    """

    endpoint: str
    client: Any = None  # A2AClient, lazily constructed if not supplied

    def _a2a_client(self):
        from .a2a.client import A2AClient  # local import: avoid a hard dependency for callers that never use A2A

        if self.client is None:
            self.client = A2AClient(self.endpoint)
        return self.client

    def complete(
        self,
        *,
        agent_id: str,
        kind: str,
        gate_id: str,
        role_prompt: str,
        task_text: str,
    ) -> AgentOutput:
        task = self._a2a_client().send_message(f"{role_prompt}\n\n{task_text}")
        digest_filler = "0" * 64
        blocking_question = None
        if task.status.state.value == "input-required":
            message = task.status.message
            blocking_question = (
                message if isinstance(message, str) else f"{agent_id} needs clarification before proceeding"
            )
        return AgentOutput(
            agent_id=agent_id,
            kind=kind,
            gate_id=gate_id,
            identity=Identity(id=agent_id, role=f"{kind}:{agent_id}", kind="agent"),
            artifact_binding=ArtifactBinding(
                artifact_id=f"{gate_id}-{agent_id}-artifact",
                revision="rev-1",
                digest=f"sha256:{digest_filler}",
            ),
            evidence_ref=EvidenceRef(
                evidence_id=f"{gate_id}-{agent_id}-evidence",
                uri=f"a2a://{self.endpoint}/{task.id}",
                hash_algorithm="sha256",
                hash=digest_filler,
                classification="internal",
            ),
            blocking_question=blocking_question,
        )


@dataclass
class DispatchingModelClient:
    """Routes `.complete()` to a per-`agent_id` `ModelClient` based on the
    agent catalog's `transport` field: `transport: "a2a"` entries go to an
    `A2AModelClient` built from the entry's `endpoint`; everything else
    (including agents absent from the catalog) goes to `default`.

    This is the one place the local-vs-external decision is made. It
    exists so `graph.py`'s `build_graph`/`make_agent_node` -- which take
    exactly one shared `model_client` for every node -- need no changes
    at all to support a mix of local and external agents.
    """

    default: ModelClient
    agent_catalog: dict[str, Any] = field(default_factory=dict)
    _a2a_clients: dict[str, A2AModelClient] = field(default_factory=dict, repr=False)

    def _client_for(self, agent_id: str) -> ModelClient:
        entry = self.agent_catalog.get(agent_id, {})
        if entry.get("transport") != "a2a":
            return self.default
        endpoint = entry["endpoint"]
        if agent_id not in self._a2a_clients:
            self._a2a_clients[agent_id] = A2AModelClient(endpoint=endpoint)
        return self._a2a_clients[agent_id]

    def complete(
        self,
        *,
        agent_id: str,
        kind: str,
        gate_id: str,
        role_prompt: str,
        task_text: str,
    ) -> AgentOutput:
        return self._client_for(agent_id).complete(
            agent_id=agent_id,
            kind=kind,
            gate_id=gate_id,
            role_prompt=role_prompt,
            task_text=task_text,
        )


def make_agent_node(
    agent_id: str,
    kind: str,
    model_client: ModelClient,
    metadata: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
    provider_root: str | Path | None = None,
) -> Callable[[dict], dict]:
    """Build a LangGraph node function bound to one agent + dispatch role.

    The node reads `gate_id` and `task_text` off whatever payload the
    triggering `Send` carried (see graph.py's dispatch conditional edges)
    and returns a state update writing one `AgentOutput` dict to its own
    `f"{gate_id}:{kind}:{agent_id}"` slot in the `agent_outputs`
    map-reduce scratch field (see `state.merge_agent_outputs` for why this
    is a keyed dict, not an append-only list: a redispatch of the same
    agent/role/gate -- e.g. after `reenter_gate` -- must overwrite its own
    prior output, not duplicate it alongside a stale one).

    `metadata` (the agent's own agent-catalog entry, e.g.
    `agent_catalog.get(agent_id, {})`) and `profile` (the active profile
    dict) are threaded through to `resolve_role_prompt` so a
    provider-supplied rich role definition is used when the profile opts
    into it (`profile["rich_content_source"]`), falling back to the
    generic templated instruction otherwise -- both default to `{}`,
    which always takes the generic-instruction path, so existing callers
    that don't pass them are unaffected.
    """
    metadata = metadata or {}
    profile = profile or {}

    def node(payload: dict[str, Any]) -> dict[str, Any]:
        gate_id = payload["gate_id"]
        task_text = payload.get("task_text", "")
        role_prompt = resolve_role_prompt(agent_id, kind, metadata, profile, provider_root=provider_root)
        output = model_client.complete(
            agent_id=agent_id,
            kind=kind,
            gate_id=gate_id,
            role_prompt=role_prompt,
            task_text=task_text,
        )
        slot_key = f"{gate_id}:{kind}:{agent_id}"
        return {"agent_outputs": {slot_key: dict(output)}}

    return node
