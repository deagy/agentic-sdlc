# Agentic SDLC

Governed software-delivery lifecycle tooling, runner-neutral by design. See
[docs/usage-overview.md](docs/usage-overview.md) for what the pieces are and
how they fit together before diving into the commands below.

The portable kernel lives under [`plugins/agentic-sdlc`](plugins/agentic-sdlc).
It owns the G1-G10 lifecycle contracts, project initializer, deterministic
planner and validator, and approval evidence adapters. Projects own their
`.agentic-sdlc/` overlays and records.

Install it as a real executable, or run it straight from a checkout:

```sh
pipx install ./plugins/agentic-sdlc   # puts `agentic-sdlc` on PATH, no checkout needed at runtime
agentic-sdlc --help

./bin/agentic-sdlc --help              # or, from a checkout without installing anything
python3 -B -m unittest discover -s plugins/agentic-sdlc/test -p "test_*.py"
```

Domain suites contribute agents, profiles, and impact extensions through an
explicit, versioned provider manifest:

```sh
./bin/agentic-sdlc \
  --provider /path/to/provider.json \
  init --root /path/to/project --profile secure-cloud
```

See the [plugin guide](plugins/agentic-sdlc/README.md) for lifecycle operation,
provider behavior, upgrades, and fail-closed defaults.

## Orchestration: the LangGraph engine

[`agentic_sdlc_langgraph/`](agentic_sdlc_langgraph) drives a task through the
G1-G10 lifecycle as an actual compiled graph — author/reviewer dispatch, gate
sequencing, separation-of-duties enforcement, and human/mutation-gate stops
are graph control flow, not prose an LLM host has to interpret. It replaces
the plugin's earlier skill-based orchestration — see that package's README
for the `agentic-sdlc-lg` CLI and standalone service, and this repository's
commit history for the phase-by-phase design rationale.

```sh
cd agentic_sdlc_langgraph && uv sync && uv run pytest
```

## Examples

See [docs/examples/](docs/examples/) for end-to-end workflow documentation:

- [Full lifecycle workflow](docs/examples/full-lifecycle.md) — from G1 intent through G10 post-release review

Additional documentation:

- [docs/gate-rationale.md](docs/gate-rationale.md) — why these ten gates (and not eight or twelve)
- [docs/usage-overview.md](docs/usage-overview.md) — high-level usage guide
