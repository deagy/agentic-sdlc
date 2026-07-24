"""Minimal FastAPI service exposing the G1-G10 lifecycle over HTTP,
with zero chat-CLI (Claude Code / Codex CLI) involvement.

This is what makes "standalone autonomous execution" concrete: a GitHub
webhook, a cron job, or any other HTTP caller can drive a task's lifecycle
end to end just by calling these three routes. Each route handler rebuilds
the graph fresh via `runtime.build_graph_for_task` (see `runtime.py`'s
module docstring) and never holds a graph object across requests -- the
persistent on-disk `SqliteSaver` at `<root>/.agentic-sdlc/state.db` is what
carries state between calls, exactly as it does between separate CLI
process invocations. A worker process, a second replica of this same
service, or the CLI can all interleave calls against the same task/root
and see consistent state, because nothing lives in this process's memory
between requests.

Deliberately minimal per the task spec: no auth, no pagination, no request
throttling, no background workers. `root` is accepted as a plain request
field (not a path-confined server-side setting) because this is a
developer-facing/internal-automation service, not something exposed to
untrusted callers -- adding real multi-tenant path confinement is future
work, not in scope here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from langgraph.types import Command
from pydantic import BaseModel

from . import runtime

app = FastAPI(title="Agentic SDLC LangGraph Service")


class CreateTaskRequest(BaseModel):
    task_id: str
    task: str
    root: str
    profile: str = "generic"
    ignored_gate_ids: list[str] = []
    provider_manifest: str | None = None


class ResumeRequest(BaseModel):
    root: str
    decision: Any


@app.post("/tasks")
def create_task(payload: CreateTaskRequest) -> dict[str, Any]:
    """Plan (or reconnect to) a task, matching the CLI's `plan` behavior:
    first call for `task_id` derives the gate sequence, builds the graph,
    writes `graph-config.json`, and invokes the graph to its first
    interrupt (or completion); a later call for an already-planned
    `task_id` is a no-op that reports its recorded gate sequence instead
    of re-invoking.
    """
    root = Path(payload.root)
    already_planned = runtime.task_exists(root, payload.task_id)
    try:
        graph, config, metadata = runtime.build_graph_for_task(
            root,
            payload.task_id,
            task_text=payload.task,
            profile_id=payload.profile,
            provider_manifest=payload.provider_manifest,
            ignored_gate_ids=payload.ignored_gate_ids,
        )
    except runtime.GraphConfigError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if already_planned:
        return {"status": "already-planned", "gate_sequence": metadata.gate_sequence_ids}

    result = graph.invoke(runtime.initial_state(payload.task_id, payload.task), config=config)
    return runtime.invoke_result_payload(result)


@app.post("/tasks/{task_id}/resume")
def resume_task(task_id: str, payload: ResumeRequest) -> dict[str, Any]:
    """Resume an interrupted task with a decision, matching the CLI's
    `resume` behavior."""
    root = Path(payload.root)
    try:
        graph, config, _metadata = runtime.build_graph_for_task(root, task_id)
    except runtime.GraphConfigError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    result = graph.invoke(Command(resume=payload.decision), config=config)
    return runtime.invoke_result_payload(result)


@app.get("/tasks/{task_id}")
def get_task_status(task_id: str, root: str) -> dict[str, Any]:
    """Status summary, matching the CLI's `status` behavior."""
    try:
        graph, config, metadata = runtime.build_graph_for_task(Path(root), task_id)
    except runtime.GraphConfigError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return runtime.status_summary(graph, config, metadata)
