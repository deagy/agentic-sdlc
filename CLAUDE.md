# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

Agentic SDLC: governed, runner-neutral software-delivery lifecycle tooling. It has three parts:

- **`plugins/agentic-sdlc/`** — the portable kernel. Owns the G1–G10 lifecycle contracts, the project initializer, the deterministic planner/validator, and GitHub-based approval evidence adapters. This is the sole owner of the schemas/contracts and of project bootstrapping (`init`/`detect`).
- **`agentic_sdlc_langgraph/`** — the orchestration engine. Drives the kernel's contracts through an actual compiled LangGraph `StateGraph`: author/reviewer dispatch, gate sequencing, separation-of-duties enforcement, and human/mutation-gate stops are graph control flow, not prose an LLM host has to interpret. It reads the kernel's JSON contracts as plain files (never shells out to `agentic_sdlc.py`) and replaces the kernel's earlier skill-based (`SKILL.md`) orchestration.
- **`providers/agentic-sdlc-defaults/`** — an example versioned provider package (agent catalog + profiles). Domain roles, profiles, policies, and extensions belong in provider packages like this one, external to the kernel.

Projects that adopt this tooling own a `.agentic-sdlc/` overlay (project.json, authorities.json, routing.json, version.lock, per-task run records) — the kernel/engine never becomes authoritative for a project's decisions or evidence.

## Commands

### Kernel (`plugins/agentic-sdlc/`)
```sh
./bin/agentic-sdlc --help
python3 -B -m unittest discover -s plugins/agentic-sdlc/test -p "test_*.py"   # full suite
python3 -m unittest plugins/agentic-sdlc/test/test_agentic_sdlc.py -k <TestName>  # single test (Python 3.10+ unittest -k)
```
CLI entry point `bin/agentic-sdlc` is a POSIX shell shim that runs the checked-out `plugins/agentic-sdlc/agentic_sdlc/` package in place via `plugins/agentic-sdlc/dev_entrypoint.py` (a plain script invocation, not `python3 -m agentic_sdlc`: `-m` would put the caller's own cwd at `sys.path[0]`, ahead of any PYTHONPATH-prepended entry, so a caller running `agentic-sdlc --root .` from inside a target project containing its own top-level `agentic_sdlc` name would silently shadow the real package; a script invocation instead puts the script's own directory at `sys.path[0]`, independent of the caller's cwd) — no install required for dev/CI use, and the caller's own working directory stays intact for relative `--root` arguments without `cd`-ing. `dev_entrypoint.py` is excluded from the packaged distribution (dev/CI use only); `python -m agentic_sdlc` still works standalone for anyone who explicitly wants it (`agentic_sdlc/__main__.py`), just isn't what the shim or tests use internally. `plugins/agentic-sdlc/` is also a real pip/pipx-installable distribution (`pyproject.toml`, package name `agentic-sdlc`, console script `agentic-sdlc`); `contracts/` is bundled into the built wheel via hatchling `force-include` at build time, while the checkout keeps a single canonical copy at `plugins/agentic-sdlc/contracts/` (not duplicated under `agentic_sdlc/`) since the LangGraph engine's `runtime.py` also reads it from that fixed path — see `agentic_sdlc/__init__.py`'s `PLUGIN_ROOT` resolution and `plugins/agentic-sdlc/README.md`'s "Install" section. Key subcommands: `init`, `detect` (advisory, no writes), `plan`, `validate` (exit 0=ready, 2=structurally valid but blocked, 1=error), `status`, `approve-from-github(-pr)`, `invalidate`, `reenter`, `upgrade`, `show-contract`, `provider`/`profile`/`extension` introspection. Use a provider with `--provider /path/to/provider.json --profile <id>`; without `--provider` the kernel runs in "kernel-only mode" (no profiles/agent catalog/extensions).

CI (`.github/workflows/validate.yml`) runs: `pip install -r plugins/agentic-sdlc/requirements-validation.txt`, the unittest discover command above, `./bin/agentic-sdlc --version`, then a separate `pip install ./plugins/agentic-sdlc` + installed-console-script smoke test to verify the packaged distribution itself.

### LangGraph engine (`agentic_sdlc_langgraph/`)
```sh
cd agentic_sdlc_langgraph
uv sync                                              # install deps (python >=3.11)
uv run pytest                                        # full test suite
uv run pytest tests/test_cli.py                      # single file
uv run pytest tests/test_cli.py::test_name -v         # single test
```
`agentic-sdlc-lg` CLI subcommands (`cli.py`): `plan`, `resume`, `status`, `invalidate`, `reenter`, `export`, `validate`. Each invocation is its own process — state persists on disk via a LangGraph `SqliteSaver` (`<root>/.agentic-sdlc/state.db`) plus `graph-config.json`, so separate CLI/service/A2A calls reconnect to an identically-shaped graph. The FastAPI service (`service.py`, routes `POST /tasks`, `POST /tasks/{task_id}/resume`, `GET /tasks/{task_id}`) is run via `uvicorn agentic_sdlc_langgraph.service:app`; it is deliberately minimal (no auth/pagination/throttling).

No ruff/mypy config exists in this package (or elsewhere in the repo) — don't assume lint/typecheck gates that aren't there.

## Architecture

### Lifecycle gates (G1–G10)
Defined in `plugins/agentic-sdlc/contracts/lifecycle-gates.json`: G1 Intent → G2 Requirements Baseline → G3 Architecture → G4 Governance and Data → G5 Security and Crypto → G6 Verification and Test → G7 Evidence → G8 Release Readiness → G9 Deployment Authorization (`human_only: true`) → G10 Runtime Conformance. Each gate declares prerequisites, required contributions, and authority requirements. `mutation-gates.json` defines a separate `human_only` policy for destructive/production/privileged actions, evaluated independently of any provider/profile choice.

### Fail-closed defaults (load-bearing invariant)
- Human authorities start **unassigned**; conditional applicability (data/control ownership, key ownership, UAT, runtime security/governance) starts `unknown` and requires accountable rationale to mark `not-applicable` — unknown-applicable requirements **block** the gate.
- No gate is ever approved by `init`, `detect`, `plan`, or `validate`.
- Quality-gate readiness never substitutes for production/destructive/privileged/exception authorization.
- G9 (Deployment Authorization) is `human_only` — automation cannot grant it.
- Author/reviewer/human-approver separation is structural: the kernel's `validate_repository()` and the LangGraph engine's gate-decision nodes both reject route/dispatch configs where the same identity is both author and independent reviewer.
- Approval evidence must reference an external authoritative system (e.g. GitHub PR review URI `github-review:<owner>/<repo>:pull/<pr>:review/<review-id>:reviewer/<login>`) — the kernel does not authenticate an approver's real-world identity, and evidence is never invented or silently migrated.

When touching gate logic, dispatch/routing, or approval handling in either the kernel or the LangGraph engine, preserve these invariants — they are the point of the project, not incidental validation.

### Providers and profiles
A **provider** (`contracts/provider.schema.json`) is a versioned manifest — e.g. `providers/agentic-sdlc-defaults/provider.json` — declaring `schema_version`, `id`, `version`, `kernel_compatibility {minimum, maximum_exclusive}`, `agent_catalog` path, `profile_roots`, `extension_roots`. Resource paths must resolve inside the manifest's own directory; path-escape, duplicate IDs, or kernel-version incompatibility fail closed. The selected provider's identity/version/manifest digest is recorded in the project's `version.lock`.

A **profile** (`contracts/profile.schema.json`) — e.g. `providers/agentic-sdlc-defaults/profiles/{generic,quick,web-service}/profile.json` — supplies `gate_bindings`/`routing` for gate dispatch.

### LangGraph engine internals
`graph.py`'s `build_graph()` builds the `StateGraph` declaratively from a derived gate sequence (`planning.derive_gate_sequence`) plus the active profile's bindings — no gate id is hardcoded. Per gate: `dispatch_authors_{gate}` → `Send` fan-out to author nodes → `dispatch_reviewers_{gate}` → `Send` fan-out to reviewer nodes → `gate_decision_{gate}` (pure merge + separation-of-duties enforcement) → `human_approval_{gate}` (present only when authority requirements are unresolved; uses `interrupt(...)`/`Command(resume=...)`). A `mutation_gate_check` node runs before any gate and can interrupt/short-circuit to `END` if `state["scope"]` matches a mutation-gate phrase. `state.py`'s `SDLCState`/`GateState` TypedDicts mirror `run-record.schema.json` field-for-field — the checkpointed graph state *is* the run record; `export.py` reassembles it back into that schema shape.

A2A (Agent2Agent) protocol support (`a2a/`) lets external agents (e.g. Codex CLI) participate as dispatch targets: `a2a/server.py` mounts a JSON-RPC surface into the FastAPI service reusing the same `runtime` helpers as REST/CLI, so all three surfaces see identical behavior for a given `task_id`; `a2a/client.py` is the reverse direction, used by `agents.A2AModelClient` to dispatch a node to an external A2A agent instead of an in-process model call.

Several LangGraph engine modules (`contracts.py`, `provider.py`, `planning.py`, `github_approval.py`, `gitlab_issue.py`, `reentry.py`, `validate.py`) are explicitly documented as ports of specific functions from the legacy `plugins/agentic-sdlc/agentic_sdlc/__init__.py` (formerly `scripts/agentic_sdlc.py`, moved for pip/pipx packaging), with deliberate deviations (e.g. no module-level global state, since the engine must be reentrant across processes/CLI invocations).

### GitLab issue linkage for G1 Intent / G2 Requirements Baseline
`intent_record_id`/`requirements_baseline_id` (run-record fields that exist in the schema but, until this feature, nothing ever set) can be populated from a real, fetched-and-validated GitLab issue rather than a free-text label. Deliberately not an approval-evidence adapter like `github_approval.py`/the kernel's `approve-from-gitlab*`: linking a source never marks G1/G2 approved, and gate approval is unaffected by whether a source is linked — the two are orthogonal by design.

- **Kernel**: `link-intent-from-gitlab-issue` / `link-requirements-from-gitlab-issue` (`agentic_sdlc/__init__.py`) fetch the issue via `glab api`, attach it as gate-level `evidence_refs`, and set the run-record field. Authorization mirrors the approval adapters (assigned, applicable authority for the gate).
- **Engine**: `gitlab_issue.py`'s `resolve_issue_reference` is called from `cli.py`'s `plan --intent-gitlab-issue`/`--requirements-gitlab-issue` and `service.py`'s `CreateTaskRequest`, seeding `SDLCState.intent_record_id`/`requirements_baseline_id` once at plan time (the same shape `--task`/`state["scope"]` already uses) — not via `graph.py`'s dispatch/interrupt machinery, since G1/G2's `human_approval_{gate}` interrupt is about authority assignment, not sourcing, and there is no author-dispatch-time tool-call surface to hook into (`agents.py`'s `ModelClient` implementations synthesize their own `evidence_ref.uri`, no external fetch capability exists there). **Deliberate asymmetry with the kernel**: the engine does not attach a matching `GateState.evidence_refs` entry the way the kernel does — that would require special-casing `graph.py`'s `gate_decision_{gate_id}` node for G1/G2, which this feature avoids; only the top-level `intent_record_id`/`requirements_baseline_id` fields are populated engine-side.

## Working across the two subsystems
Changes to gate semantics, contract shape, or provider/profile schema belong in `plugins/agentic-sdlc/contracts/` and must be reflected in the LangGraph engine's `contracts.py` loaders (and vice versa — the engine ports specific kernel functions, so kernel changes can silently desync the port). Run both test suites before considering cross-cutting work done (see AGENTS.md).
