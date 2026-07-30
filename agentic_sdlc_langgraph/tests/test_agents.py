"""Tests for `agentic_sdlc_langgraph.agents.resolve_role_prompt`: the
rich-agent-role-prompt mechanism, ported from `agentic_sdlc.py`'s
`agent_wrapper_body` (~708-713) / `rich_agent_content` (~700-705) /
`agent_wrapper_instructions` (~674-681) / `ASK_HUMAN_RULE` (~660-664).

None of the three shipped profiles (`generic`/`quick`/`web-service`) or
the `agentic-sdlc-defaults` agent catalog populate a `definition` field
or set `rich_content_source` -- so today, every agent in this project's
real fixtures gets the generic templated instruction. The tests below
prove both halves of the mechanism with synthetic fixtures: the rich-
content path (for a future provider, e.g. `secure-cloud-agents`, that
does supply real `AGENT.md` role definitions) and the generic-fallback
path (the real, current behavior of every shipped profile).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from types import SimpleNamespace

from langgraph.checkpoint.sqlite import SqliteSaver

from agentic_sdlc_langgraph.agents import (
    ASK_HUMAN_RULE,
    OpenAICompatibleModelClient,
    SUBMIT_CONTRIBUTION_TOOL_NAME,
    resolve_role_prompt,
)
from agentic_sdlc_langgraph.graph import build_graph


def test_resolve_role_prompt_uses_rich_definition_when_profile_opts_in(tmp_path):
    distinctive_text = "You are the Cryptographic Assurance Engineer for Project Nightshade."
    definition_path = tmp_path / "AGENT.md"
    definition_path.write_text(f"\n  {distinctive_text}\n  \n", encoding="utf-8")

    metadata = {"kind": "author", "capabilities": ["author"], "definition": "AGENT.md"}
    profile = {"rich_content_source": True}

    prompt = resolve_role_prompt(
        "cryptographic-assurance-engineer",
        "author",
        metadata,
        profile,
        provider_root=tmp_path,
    )

    assert distinctive_text in prompt
    # Stripped, not the raw padded file content.
    assert not prompt.startswith("\n")
    # Still carries the ask-human rule and an adaptation note, per the
    # legacy `agent_wrapper_body`'s rich-content branch.
    assert ASK_HUMAN_RULE in prompt


def test_resolve_role_prompt_falls_back_to_generic_instructions_when_rich_content_source_unset(tmp_path):
    """This is the real, current behavior of every shipped profile today
    (none set `rich_content_source`)."""
    definition_path = tmp_path / "AGENT.md"
    definition_path.write_text("some rich content that must NOT be used", encoding="utf-8")

    metadata = {"kind": "reviewer", "capabilities": ["reviewer"], "definition": "AGENT.md"}
    profile = {}  # rich_content_source unset, exactly like generic/quick/web-service

    prompt = resolve_role_prompt(
        "code-reviewer",
        "reviewer",
        metadata,
        profile,
        provider_root=tmp_path,
    )

    assert "some rich content that must NOT be used" not in prompt
    assert "code-reviewer" in prompt
    assert "do not modify the artifact under review" in prompt
    assert ASK_HUMAN_RULE in prompt


def test_resolve_role_prompt_falls_back_when_definition_missing_even_if_rich_content_source_set():
    metadata = {"kind": "author", "capabilities": ["author"]}  # no "definition" key at all
    profile = {"rich_content_source": True}

    prompt = resolve_role_prompt("product-intent-agent", "author", metadata, profile)

    assert "product-intent-agent" in prompt
    assert "do not self-review" in prompt
    assert ASK_HUMAN_RULE in prompt


def test_resolve_role_prompt_definition_cannot_escape_provider_root(tmp_path):
    """Path-confinement: a `definition` that tries to escape the
    provider root must be rejected (falls back to the generic
    instruction), never read from outside the root."""
    provider_root = tmp_path / "provider"
    provider_root.mkdir()
    secret_outside_root = tmp_path / "secret.md"
    secret_outside_root.write_text("do not leak this", encoding="utf-8")

    metadata = {"definition": "../secret.md"}
    profile = {"rich_content_source": True}

    prompt = resolve_role_prompt(
        "some-agent",
        "author",
        metadata,
        profile,
        provider_root=provider_root,
    )

    assert "do not leak this" not in prompt
    assert ASK_HUMAN_RULE in prompt


@dataclass
class _RecordingModelClient:
    """Captures every `role_prompt` it's called with, keyed by agent_id,
    instead of doing anything with it -- used to prove `build_graph`
    actually threads a provider's rich role definition through
    `make_agent_node` -> `resolve_role_prompt`, not just that
    `resolve_role_prompt` works in isolation."""

    seen_role_prompts: dict[str, str] = field(default_factory=dict)

    def complete(self, *, agent_id, kind, gate_id, role_prompt, task_text):
        self.seen_role_prompts[agent_id] = role_prompt
        digest_filler = "0" * 64
        return {
            "agent_id": agent_id,
            "kind": kind,
            "gate_id": gate_id,
            "identity": {"id": agent_id, "role": f"{kind}:{agent_id}", "kind": "agent"},
            "artifact_binding": {
                "artifact_id": f"{gate_id}-{agent_id}-artifact",
                "revision": "rev-1",
                "digest": f"sha256:{digest_filler}",
            },
            "evidence_ref": {
                "evidence_id": f"{gate_id}-{agent_id}-evidence",
                "uri": f"fake://evidence/{gate_id}/{agent_id}",
                "hash_algorithm": "sha256",
                "hash": digest_filler,
                "classification": "internal",
            },
            "blocking_question": None,
        }


def test_build_graph_threads_rich_role_definition_through_to_dispatched_agents(tmp_path):
    """End-to-end proof (not just a `resolve_role_prompt` unit test):
    when `build_graph` is given a profile with `rich_content_source` and
    an agent-catalog entry with a `definition`, the agent node actually
    dispatched during a real graph run receives that rich content as its
    `role_prompt` -- i.e. `graph.py`'s wiring of
    `make_agent_node(..., metadata=..., profile=...)` genuinely reaches
    `resolve_role_prompt`, it isn't dead plumbing."""
    distinctive_text = "You are the Bespoke Intent Agent for this synthetic provider."
    definition_path = tmp_path / "AGENT.md"
    definition_path.write_text(distinctive_text, encoding="utf-8")

    gates = [
        {
            "id": "G1",
            "name": "Intent",
            "required_contributions": ["intent"],
            "prerequisites": [],
            "authority_requirements": ["product_owner"],
        }
    ]
    gate_bindings = {
        "G1": {
            "contributions": {
                "intent": {
                    "agents": ["bespoke-intent-agent"],
                    "tasks": ["capture-intent"],
                    "artifacts": ["intent-record"],
                }
            }
        }
    }
    agent_catalog = {
        "bespoke-intent-agent": {
            "kind": "author",
            "capabilities": ["author"],
            "definition": str(definition_path),
        }
    }
    profile = {"rich_content_source": True}

    model_client = _RecordingModelClient()
    checkpointer = SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))
    graph = build_graph(
        gates=gates,
        gate_bindings=gate_bindings,
        routes=[],
        agent_catalog=agent_catalog,
        model_client=model_client,
        checkpointer=checkpointer,
        mutation_gates=[],
        profile=profile,
    )

    config = {"configurable": {"thread_id": "task-rich-content"}}
    initial_state = {
        "task_id": "task-rich-content",
        "classification": "internal",
        "scope": "some task",
        "current_lifecycle_phase": "intent",
        "lifecycle_gates": {},
        "re_entry_history": [],
        "authorities": {"product_owner": {"status": "assigned"}},
        "agent_outputs": {},
        "mutation_gate_pending": None,
        "mutation_gate_decision": None,
        "run_halted": False,
    }
    result = graph.invoke(initial_state, config=config)
    assert "__interrupt__" in result

    assert "bespoke-intent-agent" in model_client.seen_role_prompts
    assert distinctive_text in model_client.seen_role_prompts["bespoke-intent-agent"]
    assert ASK_HUMAN_RULE in model_client.seen_role_prompts["bespoke-intent-agent"]


class _FakeOpenAIClient:
    """Stands in for `openai.OpenAI`: records the `chat.completions.create`
    call it received and returns a canned response shaped like the real
    SDK's (`response.choices[0].message.tool_calls[i].function.{name,arguments}`),
    without importing `openai` itself."""

    def __init__(
        self,
        tool_call_arguments: dict | None,
        *,
        tool_call_name: str = SUBMIT_CONTRIBUTION_TOOL_NAME,
        raw_arguments: str | None = None,
    ):
        self.received_kwargs: dict | None = None
        if raw_arguments is not None:
            tool_calls = [SimpleNamespace(function=SimpleNamespace(name=tool_call_name, arguments=raw_arguments))]
        elif tool_call_arguments is None:
            tool_calls = []
        else:
            tool_calls = [
                SimpleNamespace(
                    function=SimpleNamespace(name=tool_call_name, arguments=json.dumps(tool_call_arguments))
                )
            ]
        self._response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=tool_calls))]
        )
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.received_kwargs = kwargs
        return self._response


def test_openai_compatible_model_client_builds_agent_output_from_tool_call():
    fake_client = _FakeOpenAIClient(
        {
            "artifact_id": "artifact-42",
            "revision": "rev-7",
            "summary": "Did the thing.",
            "blocking_question": None,
        }
    )
    client = OpenAICompatibleModelClient(model="gpt-4o-mini")
    client._client = lambda: fake_client

    output = client.complete(
        agent_id="some-agent",
        kind="author",
        gate_id="G1",
        role_prompt="You are some-agent.",
        task_text="Do the thing.",
    )

    assert output["agent_id"] == "some-agent"
    assert output["kind"] == "author"
    assert output["gate_id"] == "G1"
    assert output["artifact_binding"]["artifact_id"] == "artifact-42"
    assert output["artifact_binding"]["revision"] == "rev-7"
    assert output["evidence_ref"]["uri"] == "openai://response/G1/some-agent"
    assert output["blocking_question"] is None

    assert fake_client.received_kwargs["model"] == "gpt-4o-mini"
    assert fake_client.received_kwargs["messages"][0] == {"role": "system", "content": "You are some-agent."}
    assert fake_client.received_kwargs["messages"][1] == {"role": "user", "content": "Do the thing."}
    tool = fake_client.received_kwargs["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == SUBMIT_CONTRIBUTION_TOOL_NAME


def test_openai_compatible_model_client_surfaces_blocking_question():
    fake_client = _FakeOpenAIClient(
        {
            "artifact_id": "artifact-1",
            "revision": "rev-1",
            "summary": "Need input.",
            "blocking_question": "Which auth provider should this use?",
        }
    )
    client = OpenAICompatibleModelClient(model="gpt-4o-mini")
    client._client = lambda: fake_client

    output = client.complete(
        agent_id="some-agent",
        kind="reviewer",
        gate_id="G3",
        role_prompt="You are some-agent.",
        task_text="Review the thing.",
    )

    assert output["blocking_question"] == "Which auth provider should this use?"


def test_openai_compatible_model_client_falls_back_when_tool_not_called():
    """If the model returns no matching tool call (e.g. an unexpected or
    empty response), the client still returns a well-formed `AgentOutput`
    using the same defaults `AnthropicModelClient` falls back to, rather
    than raising."""
    fake_client = _FakeOpenAIClient(None)
    client = OpenAICompatibleModelClient(model="gpt-4o-mini")
    client._client = lambda: fake_client

    output = client.complete(
        agent_id="some-agent",
        kind="author",
        gate_id="G1",
        role_prompt="You are some-agent.",
        task_text="Do the thing.",
    )

    assert output["artifact_binding"]["artifact_id"] == "G1-some-agent-artifact"
    assert output["artifact_binding"]["revision"] == "rev-1"
    assert output["blocking_question"] is None


def test_openai_compatible_model_client_falls_back_on_malformed_tool_arguments():
    """A non-conformant OpenAI-compatible server (this client's whole reason
    for existing -- vLLM/Ollama/LiteLLM proxies, not just api.openai.com)
    can return a tool call whose `arguments` isn't valid JSON. That must
    fall back to the same defaults as "no matching tool call", not crash
    the LangGraph node."""
    fake_client = _FakeOpenAIClient(None, raw_arguments="{not valid json")
    client = OpenAICompatibleModelClient(model="gpt-4o-mini")
    client._client = lambda: fake_client

    output = client.complete(
        agent_id="some-agent",
        kind="author",
        gate_id="G1",
        role_prompt="You are some-agent.",
        task_text="Do the thing.",
    )

    assert output["artifact_binding"]["artifact_id"] == "G1-some-agent-artifact"
    assert output["artifact_binding"]["revision"] == "rev-1"
    assert output["blocking_question"] is None


def test_openai_compatible_model_client_reads_api_key_and_base_url_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")

    captured = {}

    class _FakeOpenAIModule:
        @staticmethod
        def OpenAI(*, api_key, base_url):
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            return _FakeOpenAIClient(None)

    monkeypatch.setitem(__import__("sys").modules, "openai", _FakeOpenAIModule())

    client = OpenAICompatibleModelClient(model="gpt-4o-mini")
    client._client()

    assert captured["api_key"] == "env-key"
    assert captured["base_url"] == "https://example.invalid/v1"


def test_openai_compatible_model_client_explicit_fields_override_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")

    captured = {}

    class _FakeOpenAIModule:
        @staticmethod
        def OpenAI(*, api_key, base_url):
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            return _FakeOpenAIClient(None)

    monkeypatch.setitem(__import__("sys").modules, "openai", _FakeOpenAIModule())

    client = OpenAICompatibleModelClient(
        model="gpt-4o-mini", api_key="explicit-key", base_url="https://self-hosted.invalid/v1"
    )
    client._client()

    assert captured["api_key"] == "explicit-key"
    assert captured["base_url"] == "https://self-hosted.invalid/v1"
