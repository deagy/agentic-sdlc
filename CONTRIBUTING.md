
# Contributing to Agentic SDLC

This repository is hosted on GitHub. Contributions are reviewed through GitHub
pull requests and validated by the repository's GitHub Actions checks.

## What this repository is

Agentic SDLC provides governed, runner-neutral software-delivery lifecycle tooling with three parts:

- **Kernel** (`plugins/agentic-sdlc/`) — G1-G10 lifecycle contracts, project initialization, deterministic planning/validation, and approval evidence adapters
- **LangGraph Engine** (`agentic_sdlc_langgraph/`) — real control-flow orchestration via LangGraph StateGraph
- **Provider** (`providers/agentic-sdlc-defaults/`) — example versioned provider package with agent catalog and profiles

## Prerequisites

- Python 3.10+ for the kernel
- Python 3.11+ for the LangGraph engine
- `uv` (recommended) or `pip` for dependency management in the LangGraph sub-project

## Setup

### Kernel (pip/pipx installable)

```sh
# From the repository root
pip install -e ./plugins/agentic-sdlc
```

### LangGraph Engine

```sh
cd agentic_sdlc_langgraph
uv sync --locked   # Installs locked dependencies; fails on lockfile drift
uv run pytest      # Run the test suite
```

## Running tests

```sh
# Kernel tests (no API keys required — uses FakeModelClient)
python3 -B -m unittest discover -s plugins/agentic-sdlc/test -p "test_*.py"

# LangGraph engine tests
cd agentic_sdlc_langgraph
uv run pytest
```

All tests are designed to run without external API keys. The kernel uses
`FakeModelClient` for all model interactions, so no credentials are needed
for validation.

## Architecture notes

- The kernel owns the lifecycle gate schemas (G1-G10), run-record validation, and gate-authority semantics. This ownership is permanent.
- The LangGraph engine is a port of legacy kernel functions into compiled StateGraph control flow. Modules explicitly marked as ports in `agentic_sdlc_langgraph/` should be kept in sync with their source functions in `plugins/agentic-sdlc/agentic_sdlc/__init__.py`.
- A2A (Agent2Agent) protocol support is under-documented. See `agentic_sdlc_langgraph/a2a/` for the current implementation.
- The `impact_categories` mechanism in gate contracts exists but is currently empty — planned for compliance framework adapter integration.

## Change flow

```text
understand scope -> make a focused change -> run relevant tests
-> regenerate packaged artifacts when required -> inspect the diff
-> open a GitHub pull request -> obtain independent review -> merge
```

### When changing gate semantics or contract shapes

1. Edit `plugins/agentic-sdlc/contracts/` for schema changes
2. Reflect changes in `agentic_sdlc_langgraph/agentic_sdlc_langgraph/contracts.py` (the engine port)
3. Run both test suites before considering the change complete

### When changing provider/profile schema

1. Edit the provider's `provider.json` and associated schemas
2. Update `kernel_compatibility` ranges if the change is incompatible
3. Run `agentic-sdlc show-contract` to validate the schema

## Pull request checklist

- [ ] The scope and affected decisions are described.
- [ ] Security, authority, lifecycle, and generated-artifact implications are called out.
- [ ] Relevant tests were run for both the kernel and LangGraph engine (if applicable).
- [ ] LangGraph port functions remain in sync with kernel source (if touching ports).
- [ ] Independent review is assigned for implementation or policy changes.
- [ ] No secrets or sensitive source material are included.
- [ ] Human-only approvals remain explicit and are not inferred from agent work.

## Cross-references

- [cadre](https://github.com/deagy/cadre) — The Agent Suite (role definitions, orchestration, knowledge store)
- [cadre-lifecycle](https://github.com/deagy/cadre-lifecycle) — The Plugin Distribution (Claude Code / Codex plugins)

## Security

Report security vulnerabilities via GitHub Security Advisories. See
[AGENTS.md](AGENTS.md) for repository-wide security rules.
