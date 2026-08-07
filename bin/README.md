
# bin/ — Agentic SDLC CLI

This directory contains the executable entry point for the Agentic SDLC lifecycle kernel.

## Entry points

| File | Purpose |
|------|---------|
| `agentic-sdlc` | POSIX shell shim — the primary CLI for lifecycle governance |

## Dispatch mechanism

`bin/agentic-sdlc` runs the checked-out kernel package in place via `plugins/agentic-sdlc/dev_entrypoint.py`. This avoids `python3 -m agentic_sdlc` shadowing issues: a script invocation puts the script's own directory at `sys.path[0]`, independent of the caller's cwd.

### Running commands

```sh
./bin/agentic-sdlc --help              # Show all available subcommands
./bin/agentic-sdlc --version           # Show kernel version
./bin/agentic-sdlc init --root .       # Bootstrap a project overlay
./bin/agentic-sdlc detect --root .     # Advisory: inspect existing repo signatures
./bin/agentic-sdlc plan --task "..."   # Create a dispatch plan with G1-G10 gates
./bin/agentic-sdlc status <task-id>    # Check pending gates for a task
./bin/agentic-sdlc validate            # Validate project configuration
./bin/agentic-sdlc approve-from-github --repo owner/repo --pr 42 --...   # Record GitHub PR approval evidence
./bin/agentic-sdlc approve-from-gitlab --project-path ns/project --mr 1 --...  # Record GitLab MR approval evidence
./bin/agentic-sdlc invalidate <task-id> <gate>  # Invalidate a gate decision
./bin/agentic-sdlc reenter <task-id> <gate>    # Re-enter from a gate with new state
./bin/agentic-sdlc show-contract <name>     # Show a lifecycle contract schema
./bin/agentic-sdlc provider list            # List available provider packages
./bin/agentic-sdlc profile list             # List available profiles
```

### Subcommand categories

- **Lifecycle management**: `init`, `detect`, `plan`, `status`, `validate`, `upgrade`
- **Gate approval**: `approve-from-github`, `approve-from-github-pr`, `approve-from-gitlab`, `approve-from-gitlab-mr`, `decide`
- **Gate modification**: `invalidate`, `reenter`
- **Introspection**: `show-contract`, `provider list`, `profile list`, `extension list`

## Transport layers

The kernel supports three transport surfaces (all sharing identical gate semantics):

1. **CLI** — `bin/agentic-sdlc` (this directory)
2. **HTTP service** — `agentic_sdlc_langgraph/service.py` (FastAPI + uvicorn)
3. **A2A JSON-RPC** — `agentic_sdlc_langgraph/a2a/server.py` (Agent2Agent protocol)

## See also

- [README.md](../README.md) — full repository documentation
- [docs/usage-overview.md](../docs/usage-overview.md) — usage overview
- [CLAUDE.md](../CLAUDE.md) — architecture notes and developer guidance
