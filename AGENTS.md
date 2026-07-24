# Repository Guidelines

The portable plugin lives under `plugins/agentic-sdlc/`. Keep the lifecycle
kernel generic: domain roles, profiles, policies, and extensions belong in
external provider packages.

Run the complete plugin tests, validate every bundled skill, and validate both
runner manifests before handoff. Preserve author, independent reviewer, and
human approver separation. Never invent approval evidence or silently migrate
project-owned lifecycle decisions.
