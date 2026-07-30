"""Tests for `agentic_sdlc_langgraph.runtime` (`build_graph_for_task` and
its `graph-config.json` metadata file), the shared graph-rebuild logic
`cli.py` and `service.py` both depend on.

These tests exercise `build_graph_for_task` directly (not through the CLI
or the service) with an explicit `:memory:` checkpointer override, to keep
these tests fast and isolated from the on-disk-sqlite-file behavior (which
is exercised separately, deliberately, by `test_default_checkpointer_is_a_
persistent_on_disk_file` below and by the cross-process tests in
`test_cli.py`).
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from agentic_sdlc_langgraph import runtime
from agentic_sdlc_langgraph.agents import AnthropicModelClient, FakeModelClient, OpenAICompatibleModelClient

TASK_TEXT = "Define and review a small internal order-processing API architecture and service"


def _memory_checkpointer() -> SqliteSaver:
    return SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))


def test_first_call_requires_task_text(tmp_path: Path):
    with pytest.raises(runtime.GraphConfigError, match="task_text is required"):
        runtime.build_graph_for_task(
            tmp_path, "task-1", model_client=FakeModelClient(), checkpointer=_memory_checkpointer()
        )


def test_first_call_writes_graph_config_json(tmp_path: Path):
    graph, config, metadata = runtime.build_graph_for_task(
        tmp_path,
        "task-1",
        task_text=TASK_TEXT,
        model_client=FakeModelClient(),
        checkpointer=_memory_checkpointer(),
    )
    assert config == {"configurable": {"thread_id": "task-1"}}
    assert metadata.gate_sequence_ids == ["G1", "G2", "G3"]

    config_path = tmp_path / ".agentic-sdlc" / "runs" / "task-1" / "graph-config.json"
    assert config_path.is_file()
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": 1,
        "task_id": "task-1",
        "task_text": TASK_TEXT,
        "profile_id": "generic",
        "provider_manifest": None,
        "ignored_gate_ids": [],
        "gate_sequence_ids": ["G1", "G2", "G3"],
        "created_at": payload["created_at"],
    }

    # The graph itself is genuinely usable: it interrupts at G1.
    result = graph.invoke(runtime.initial_state("task-1", TASK_TEXT), config=config)
    assert result["__interrupt__"][0].value["gate_id"] == "G1"


def test_later_call_rebuilds_identical_graph_without_task_text(tmp_path: Path):
    checkpointer = _memory_checkpointer()
    graph, config, _metadata = runtime.build_graph_for_task(
        tmp_path, "task-1", task_text=TASK_TEXT, model_client=FakeModelClient(), checkpointer=checkpointer
    )
    graph.invoke(runtime.initial_state("task-1", TASK_TEXT), config=config)

    # Reconnect: no task_text needed, same checkpointer (simulating "the
    # same on-disk sqlite file, opened again") -> same graph shape, same
    # checkpointed state.
    graph2, config2, metadata2 = runtime.build_graph_for_task(
        tmp_path, "task-1", model_client=FakeModelClient(), checkpointer=checkpointer
    )
    assert config2 == config
    assert metadata2.gate_sequence_ids == ["G1", "G2", "G3"]

    snapshot = graph2.get_state(config2)
    assert snapshot.values["scope"] == TASK_TEXT
    assert snapshot.interrupts[0].value["gate_id"] == "G1"

    approval = {"status": "approved", "approver": {"id": "x", "role": "x", "kind": "human"}, "evidence_refs": []}
    result = graph2.invoke(Command(resume=approval), config=config2)
    assert result["__interrupt__"][0].value["gate_id"] == "G2"


def test_conflicting_task_text_raises(tmp_path: Path):
    runtime.build_graph_for_task(
        tmp_path, "task-1", task_text=TASK_TEXT, model_client=FakeModelClient(), checkpointer=_memory_checkpointer()
    )
    with pytest.raises(runtime.GraphConfigError, match="already exists with different task text"):
        runtime.build_graph_for_task(
            tmp_path,
            "task-1",
            task_text="a completely different task",
            model_client=FakeModelClient(),
            checkpointer=_memory_checkpointer(),
        )


def test_same_task_text_on_existing_task_is_accepted(tmp_path: Path):
    runtime.build_graph_for_task(
        tmp_path, "task-1", task_text=TASK_TEXT, model_client=FakeModelClient(), checkpointer=_memory_checkpointer()
    )
    # Re-supplying the *same* task text for an already-planned task must
    # not raise -- only a *different* task text is a conflict.
    _graph, _config, metadata = runtime.build_graph_for_task(
        tmp_path, "task-1", task_text=TASK_TEXT, model_client=FakeModelClient(), checkpointer=_memory_checkpointer()
    )
    assert metadata.task_text == TASK_TEXT


def test_task_exists(tmp_path: Path):
    assert runtime.task_exists(tmp_path, "task-1") is False
    runtime.build_graph_for_task(
        tmp_path, "task-1", task_text=TASK_TEXT, model_client=FakeModelClient(), checkpointer=_memory_checkpointer()
    )
    assert runtime.task_exists(tmp_path, "task-1") is True


def test_default_checkpointer_is_a_persistent_on_disk_file(tmp_path: Path):
    """The default (no `checkpointer=` override) must be a real on-disk
    sqlite file at `<root>/.agentic-sdlc/state.db`, not `:memory:` -- an
    in-memory checkpointer cannot survive across the separate process
    invocations this module exists to support."""
    graph, config, _metadata = runtime.build_graph_for_task(
        tmp_path, "task-1", task_text=TASK_TEXT, model_client=FakeModelClient()
    )
    db_path = tmp_path / ".agentic-sdlc" / "state.db"
    graph.invoke(runtime.initial_state("task-1", TASK_TEXT), config=config)
    assert db_path.is_file()
    assert db_path.stat().st_size > 0  # real checkpoint data was actually written

    # Reopen a *brand new* graph/checkpointer against the same file (no
    # object shared with the call above) and confirm the checkpointed
    # state is actually there.
    graph2, config2, _metadata2 = runtime.build_graph_for_task(
        tmp_path, "task-1", model_client=FakeModelClient()
    )
    snapshot = graph2.get_state(config2)
    assert snapshot.values["scope"] == TASK_TEXT


def test_default_model_client_is_fake_when_env_var_set(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(runtime.FAKE_MODEL_ENV_VAR, "1")
    assert isinstance(runtime.default_model_client(), FakeModelClient)


def test_default_model_client_is_anthropic_when_env_var_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(runtime.FAKE_MODEL_ENV_VAR, raising=False)
    monkeypatch.delenv(runtime.MODEL_PROVIDER_ENV_VAR, raising=False)
    assert isinstance(runtime.default_model_client(), AnthropicModelClient)


def test_default_model_client_is_anthropic_when_provider_explicit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(runtime.FAKE_MODEL_ENV_VAR, raising=False)
    monkeypatch.setenv(runtime.MODEL_PROVIDER_ENV_VAR, "anthropic")
    assert isinstance(runtime.default_model_client(), AnthropicModelClient)


def test_default_model_client_is_openai_when_provider_set(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(runtime.FAKE_MODEL_ENV_VAR, raising=False)
    monkeypatch.setenv(runtime.MODEL_PROVIDER_ENV_VAR, "openai")
    monkeypatch.setenv(runtime.OPENAI_MODEL_ENV_VAR, "gpt-4o-mini")
    client = runtime.default_model_client()
    assert isinstance(client, OpenAICompatibleModelClient)
    assert client.model == "gpt-4o-mini"


def test_default_model_client_openai_requires_model_env_var(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(runtime.FAKE_MODEL_ENV_VAR, raising=False)
    monkeypatch.setenv(runtime.MODEL_PROVIDER_ENV_VAR, "openai")
    monkeypatch.delenv(runtime.OPENAI_MODEL_ENV_VAR, raising=False)
    with pytest.raises(runtime.GraphConfigError):
        runtime.default_model_client()


def test_default_model_client_rejects_unknown_provider(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(runtime.FAKE_MODEL_ENV_VAR, raising=False)
    monkeypatch.setenv(runtime.MODEL_PROVIDER_ENV_VAR, "bogus")
    with pytest.raises(runtime.GraphConfigError):
        runtime.default_model_client()


def test_fake_model_env_var_takes_precedence_over_provider(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(runtime.FAKE_MODEL_ENV_VAR, "1")
    monkeypatch.setenv(runtime.MODEL_PROVIDER_ENV_VAR, "openai")
    assert isinstance(runtime.default_model_client(), FakeModelClient)


def test_ignored_gates_are_recorded_and_excluded(tmp_path: Path):
    # "service" alone would (per the shipped generic profile's one route)
    # still match the new-service route and pull in G1-G3; ignoring G2
    # should leave G1 and G3 in the derived sequence.
    _graph, _config, metadata = runtime.build_graph_for_task(
        tmp_path,
        "task-1",
        task_text=TASK_TEXT,
        ignored_gate_ids=["G2"],
        model_client=FakeModelClient(),
        checkpointer=_memory_checkpointer(),
    )
    assert metadata.gate_sequence_ids == ["G1", "G3"]
    assert metadata.ignored_gate_ids == ["G2"]
