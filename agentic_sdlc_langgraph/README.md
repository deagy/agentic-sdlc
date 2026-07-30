# Agentic SDLC — LangGraph engine

Drives a task through the repository's G1-G10 lifecycle (see
[`../plugins/agentic-sdlc/contracts/lifecycle-gates.json`](../plugins/agentic-sdlc/contracts/lifecycle-gates.json))
as a compiled [LangGraph](https://github.com/langchain-ai/langgraph)
`StateGraph`, built declaratively from that contract plus a provider's
profile/agent-catalog — not from prose an LLM host has to interpret. Gate
sequencing, author/reviewer dispatch, separation-of-duties enforcement, and
human/mutation-gate stops are graph control flow, checked in code and
covered by tests.

This replaces the plugin's earlier skill-based orchestration (six
`SKILL.md` files a Claude Code/Codex CLI host read and followed step by
step). That layer has been retired; this package is the only way to
actually drive a task through the lifecycle now. The deterministic kernel
CLI in [`../plugins/agentic-sdlc/`](../plugins/agentic-sdlc) is unaffected —
it still owns the contracts/schemas this engine is built from, plus
project-overlay bootstrapping (`init`/`detect`).

## Setup

```sh
cd agentic_sdlc_langgraph
uv sync
uv run pytest
```

No `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` is required to run the tests — they
use a deterministic `FakeModelClient`. Set
`AGENTIC_SDLC_LANGGRAPH_FAKE_MODEL=1` to make the CLI/service use it too
(no network calls), instead of a real model-backed client. Otherwise,
`AGENTIC_SDLC_LANGGRAPH_MODEL_PROVIDER` selects which real client:
`anthropic` (default) uses `AnthropicModelClient`; `openai` uses
`OpenAICompatibleModelClient` against any OpenAI-compatible
chat-completions server (OpenAI itself, or a self-hosted/third-party
server mirroring its API shape — vLLM, Ollama, Azure OpenAI, LiteLLM,
etc, via `OPENAI_BASE_URL`), and requires
`AGENTIC_SDLC_LANGGRAPH_OPENAI_MODEL` to name the model to call.

## CLI

Installed as a console script, `agentic-sdlc-lg`:

```sh
export AGENTIC_SDLC_LANGGRAPH_FAKE_MODEL=1   # or a real ANTHROPIC_API_KEY
ROOT=/path/to/project

uv run agentic-sdlc-lg plan --root "$ROOT" --task-id demo-1 \
  --task "Define and review a small internal order-processing API architecture and service"

# Optionally link a GitLab issue as G1 Intent's / G2 Requirements Baseline's
# recorded source (fetched and validated via `glab api`, not just a
# free-text label) -- <project-path>#<iid> form. Never approval evidence;
# gate approval is unaffected either way. See gitlab_issue.py.
uv run agentic-sdlc-lg plan --root "$ROOT" --task-id demo-2 \
  --task "..." --intent-gitlab-issue group/project#42 --requirements-gitlab-issue group/project#43

echo '{"status":"approved","approver":{"id":"product_owner","role":"Product Owner","kind":"human"},"evidence_refs":[]}' \
  > /tmp/decision.json
uv run agentic-sdlc-lg resume --root "$ROOT" --task-id demo-1 --decision /tmp/decision.json

uv run agentic-sdlc-lg status   --root "$ROOT" --task-id demo-1
uv run agentic-sdlc-lg export   --root "$ROOT" --task-id demo-1 --output /tmp/run-record.json
uv run agentic-sdlc-lg validate --root "$ROOT" --task-id demo-1   # exits 0/1/2, see below
uv run agentic-sdlc-lg invalidate --root "$ROOT" --task-id demo-1 --earliest-gate G2 --reason "..." --actor "..."
uv run agentic-sdlc-lg reenter    --root "$ROOT" --task-id demo-1 --earliest-gate G2 --reason "..." --actor "..."
```

Each command is a separate process — state persists across them in
`<root>/.agentic-sdlc/state.db` (a LangGraph `SqliteSaver`) and
`<root>/.agentic-sdlc/runs/<task_id>/graph-config.json` (records what
`plan` resolved, so later commands can rebuild an identical graph).

`validate` follows the kernel CLI's convention: exit `0` (valid and ready),
`2` (structurally valid but blocked — e.g. an authority is unassigned), or
`1` (a real error).

## Service

A minimal FastAPI service exposes the same lifecycle over HTTP —
`POST /tasks`, `POST /tasks/{task_id}/resume`, `GET /tasks/{task_id}` — see
`service.py`. This is what makes the engine runnable with no chat CLI (or
any interactive terminal) in the loop at all: a webhook, cron, or any other
caller can drive a task end to end.

## Modules

| Module | Purpose |
|---|---|
| `state.py` | Graph state schema (`SDLCState`/`GateState`), mirroring `run-record.schema.json` |
| `contracts.py`, `planning.py` | Contract loaders and build-time gate-sequence derivation |
| `provider.py` | Pure-function provider/profile loading (semver, path confinement, separation-of-duties at load time) |
| `agents.py` | `ModelClient` protocol, `FakeModelClient`/`AnthropicModelClient`/`OpenAICompatibleModelClient`, role-prompt resolution |
| `graph.py` | Declarative graph builder: dispatch, gate decisions, human/mutation-gate interrupts |
| `reentry.py` | Invalidate/reenter as `graph.update_state(...)` operations, with real re-execution on reenter |
| `export.py`, `validate.py` | Schema-shaped run-record export and the residual (0/1/2) validator |
| `github_approval.py` | GitHub PR review → `Command(resume=...)` approval adapter |
| `runtime.py`, `cli.py`, `service.py` | Cross-process graph rebuild, CLI, and HTTP service |

Every module's docstring documents which legacy `agentic_sdlc.py` function
(if any) it ports, and calls out deliberate deviations explicitly — most
are either a fix for a legacy bug (e.g. dead/broken authority-role checks
in `validate_repository` weren't ported) or a required architectural
change (e.g. no module-level global state, since this engine needs to be
reentrant across separate processes in a way the original one-shot CLI
never had to be).
