# Agentic SDLC

Portable, governed software-delivery lifecycle tooling for Codex CLI, Claude
Code, and runner-neutral automation.

The installable plugin lives under [`plugins/agentic-sdlc`](plugins/agentic-sdlc).
It owns the G1-G10 lifecycle contracts, project initializer, deterministic
planner and validator, approval evidence adapters, and reusable skills.
Projects own their `.agentic-sdlc/` overlays and records.

```sh
./bin/agentic-sdlc --help
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
