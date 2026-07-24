# Repository Guidelines

The portable kernel lives under `plugins/agentic-sdlc/`. Keep it generic:
domain roles, profiles, policies, and extensions belong in external provider
packages. The `agentic_sdlc_langgraph/` package is the orchestration engine
that drives the kernel's contracts through a compiled LangGraph graph.

Run the complete kernel test suite (`plugins/agentic-sdlc/test/`) and the
LangGraph engine's test suite (`agentic_sdlc_langgraph/tests/`, via
`uv run pytest` from that directory) before handoff. Preserve author,
independent reviewer, and human approver separation. Never invent approval
evidence or silently migrate project-owned lifecycle decisions.
