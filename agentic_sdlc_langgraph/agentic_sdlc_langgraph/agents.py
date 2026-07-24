"""Agent-node machinery: the `ModelClient` protocol, a real Anthropic-backed
implementation, a deterministic fake for tests, and the LangGraph node
factory that wraps either one.

Port of the "agent nodes get a factory" idea from the architecture plan
(`make_agent_node(agent_id, kind, role_prompt, model_client)`), simplified
slightly: the role prompt is derived from `agent_id`/`kind` inside the node
rather than threaded through as a separate constructor argument, since this
spike has no per-agent role-description text source beyond the catalog
(agent-catalog.json only carries `kind`/`capabilities`, no prose role
descriptions) — a real port of `agent_wrapper_body`'s role prompt text is
future-phase work.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, TypedDict


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


def _default_role_prompt(agent_id: str, kind: str, gate_id: str) -> str:
    return (
        f"You are the '{agent_id}' agent, acting as {kind} for lifecycle gate "
        f"{gate_id} of the Agentic SDLC. Produce your contribution for the "
        "task described by the user, and set blocking_question if you "
        "cannot proceed without human clarification."
    )


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


def make_agent_node(agent_id: str, kind: str, model_client: ModelClient) -> Callable[[dict], dict]:
    """Build a LangGraph node function bound to one agent + dispatch role.

    The node reads `gate_id` and `task_text` off whatever payload the
    triggering `Send` carried (see graph.py's dispatch conditional edges)
    and returns a state update appending one `AgentOutput` dict to the
    `agent_outputs` map-reduce scratch field.
    """

    def node(payload: dict[str, Any]) -> dict[str, Any]:
        gate_id = payload["gate_id"]
        task_text = payload.get("task_text", "")
        role_prompt = _default_role_prompt(agent_id, kind, gate_id)
        output = model_client.complete(
            agent_id=agent_id,
            kind=kind,
            gate_id=gate_id,
            role_prompt=role_prompt,
            task_text=task_text,
        )
        return {"agent_outputs": [dict(output)]}

    return node
