# Agentic SDLC plugin

This plugin makes the repository's G1–G10 Agentic SDLC portable. It supplies a versioned lifecycle kernel — the G1–G10 gate contracts, mutation-gate definitions, run-record/agent-catalog/profile/provider JSON Schemas — plus a deterministic CLI (`agentic_sdlc/`, the pip/pipx-installable `agentic-sdlc` distribution) for bootstrapping a target project's overlay, planning a task's dispatch, and validating lifecycle state, while leaving project-specific authority and lifecycle state in the target repository.

## Install

```sh
pipx install ./plugins/agentic-sdlc        # from a checkout
pipx install git+https://github.com/deagy/agentic-sdlc.git#subdirectory=plugins/agentic-sdlc
```

Either form puts a real `agentic-sdlc` executable on `PATH`, isolated in its own environment, with no repository checkout required at runtime — `contracts/` is bundled into the installed distribution at build time (see `pyproject.toml`). `pip install` works the same way if you'd rather manage the environment yourself; use `pip install -e ./plugins/agentic-sdlc` for an editable install while developing the kernel itself. Requires Python 3.10+.

Orchestrating actual work against this kernel — dispatching author/reviewer roles, stopping at human/mutation gates, tracking gate state across a task's lifetime — is done by the LangGraph engine in [`../../agentic_sdlc_langgraph/`](../../agentic_sdlc_langgraph), not by this plugin. See that package's README for the `agentic-sdlc-lg` CLI and the standalone service. (An earlier version of this plugin shipped that orchestration as six Claude Code/Codex CLI skills an LLM host had to interpret step by step; those were retired once the LangGraph engine replaced them with real, testable control flow.)

The intended adoption path is:

```text
Initialize target repository -> review detected overlay -> assign human
authorities and resolve applicability -> plan or orchestrate work via
agentic-sdlc-lg
```

Initialization makes a project immediately usable for planning, artifact preparation, independent review, and validation. It does not make unresolved organizational decisions merely to produce a green result.

## Initialize a project

The canonical command is the installed `agentic-sdlc` executable (see
"Install" above), or `./bin/agentic-sdlc` / `python3 -m agentic_sdlc` from a
checkout during development, without installing anything:

```sh
agentic-sdlc init --root /path/to/target
```

The CLI `init` command detects candidate stack and command information, writes the project overlay, preserves unknowns and unassigned authorities, and reports blockers. Run `validate` separately afterward. Review generated files before using them as policy.

Use `--help` for the exact options supported by the installed plugin version:

```sh
agentic-sdlc init --help
```

## Portable architecture

The distribution has three deliberately separate layers:

| Layer | Owner | Contents |
|---|---|---|
| Portable kernel | Plugin maintainer | G1–G10 definitions, mutation-gate separation, schemas, lifecycle state, and validation. |
| Project overlay | Target project | Technology and command detection, routing/profile choices, authority assignments, environment declarations, applicability decisions, and kernel version lock. |
| Project state | Target project | Dispatch plans, run records, findings, exceptions, invalidations, evidence references, and human approval references. |

The plugin may be upgraded independently. It never becomes the authoritative location for a project's decisions or evidence.

Initialization creates or manages this target-repository structure:

```text
.agentic-sdlc/
├── project.json
├── authorities.json
├── impact-profile.json
├── routing.json
├── commands.json
├── version.lock
└── runs/<task-id>/
    ├── dispatch-plan.json
    └── run-record.json
.codex/agents/                 # Profile-selected project agent wrappers (Codex CLI)
.claude/agents/                # Profile-selected project agent wrappers (Claude Code)
AGENTS.md                      # Small managed Agentic SDLC instruction block
```

`init --runner {codex,claude,both}` (default `both`) controls which wrapper set is generated; both are safe to keep even if only one runner is in active use. Existing custom agent wrapper files are never overwritten, and existing managed overlay files (`.agentic-sdlc/project.json`, `authorities.json`, `impact-profile.json`, `routing.json`, `commands.json`) are never overwritten either. In the current release, `--force` does not change this: `init`, with or without `--force`, is non-destructive and idempotent with respect to already-written wrapper and overlay files. Do not rely on `--force` to refresh managed files or intentionally replace prior project decisions; always check `init --help` for the installed version's actual behavior before assuming otherwise.

## Safe defaults

Initialization and orchestration fail closed where a correct decision cannot be derived from repository content:

- Human decision authorities start unassigned unless explicitly configured.
- Conditional authorities for data/control ownership, key ownership, UAT, and
  runtime-implicated Security or Governance Leads start with `unknown`
  applicability. Marking one `not-applicable` requires an accountable rationale.
- Compliance, jurisdiction, specialized BOM, and extension applicability remain `unknown` until an accountable owner decides them.
- Environments are not assumed disposable, persistent, or production from a name alone.
- No gate is approved by initialization, detection, planning, or validation.
- Quality-gate readiness never substitutes for production, destructive, persistent-migration, privileged-identity, exception, or risk-acceptance authorization.
- Unknown applicable requirements block the affected gate instead of being treated as not applicable.

These defaults allow work products to be prepared immediately while preventing an incomplete bootstrap from silently granting authority.

## Profiles and extensions

A profile supplies provider-owned routing and contribution bindings. The kernel ships no profiles or agent catalog. Use kernel-only mode without `--profile`, or load an external provider such as `agentic-sdlc-defaults`.

Mutation gates are evaluated independently of providers, so production, destructive, persistent-migration, privileged-identity, and risk-acceptance requests still stop for human approval.

The kernel ships no domain extensions. A provider contributes profiles, an
agent catalog, and optional extensions through a versioned manifest:

```json
{
  "schema_version": 1,
  "id": "secure-cloud-agents",
  "version": "0.3.0",
  "kernel_compatibility": {
    "minimum": "0.3.0",
    "maximum_exclusive": "0.4.0"
  },
  "agent_catalog": "agent-catalog.json",
  "profile_roots": ["profiles"],
  "extension_roots": ["extensions"]
}
```

Load providers explicitly before the subcommand:

```sh
agentic-sdlc --provider /path/to/provider.json \
  init --root /path/to/project --profile secure-cloud --extension sqs-platform
```

Provider paths resolve relative to the manifest and must remain inside its
directory. Duplicate profile or extension IDs, incompatible versions, missing
resources, and path escapes fail closed. The selected provider identity,
version, and manifest digest are recorded in the project version lock.

## Commands

The bundled command entry point is `plugins/agentic-sdlc/agentic_sdlc/` (the `agentic-sdlc` distribution's `[project.scripts]` entry point; see "Install" above):

```text
init        Create or update a project overlay using safe defaults.
detect      Inspect a repository and report candidate project characteristics.
plan        Produce a reviewable dispatch plan for a task.
validate    Validate the overlay and lifecycle records.
status      Report lifecycle and gate state for a task.
approve-from-github  Record a human lifecycle approval from a GitHub PR review.
approve-from-github-pr  Fetch an approved GitHub PR review and record it as lifecycle approval evidence.
approve-from-gitlab  Record a human lifecycle approval from a GitLab MR approval. Speculative: not the approval source this kernel's own default provider uses (see "Current limitations").
approve-from-gitlab-mr  Fetch an approved GitLab MR approval and record it as lifecycle approval evidence. Speculative: not the approval source this kernel's own default provider uses (see "Current limitations").
link-intent-from-gitlab-issue  Link a GitLab issue as the recorded source for G1 Intent.
link-requirements-from-gitlab-issue  Link a GitLab issue as the recorded source for G2 Requirements Baseline.
invalidate  Record a material change and invalidate the earliest affected gate and its dependents.
```

Always inspect command-specific help before scripting an interface:

```sh
agentic-sdlc --help
agentic-sdlc plan --help
```

Task IDs are preserved exactly and must already use only letters, numbers, dots, underscores, and hyphens. The CLI rejects lossy normalization so distinct external IDs cannot share lifecycle state.

Representative invocations are:

```sh
agentic-sdlc detect --root /path/to/target
agentic-sdlc init --root /path/to/target --classification internal
agentic-sdlc plan --root /path/to/target --task-id TEAM-DEMO-001 --task "Define requirements traceability for the order API"
agentic-sdlc validate --root /path/to/target
agentic-sdlc status --root /path/to/target --task-id TEAM-DEMO-001
agentic-sdlc approve-from-github --root /path/to/target --task-id TEAM-DEMO-001 --gate G2 --role product_owner --repo example/service --pr 42 --review-id 314159 --reviewer-login octocat --commit-sha 0123abcd
agentic-sdlc approve-from-github-pr --root /path/to/target --task-id TEAM-DEMO-001 --gate G2 --role product_owner --repo example/service --pr 42 --commit-sha 0123abcd
agentic-sdlc invalidate --root /path/to/target --task-id TEAM-DEMO-001 --earliest-gate G2 --reason "Approved intent changed" --actor "product-owner"
```

Projects that want GitHub PR reviews to be the authoritative human-approval source can opt in through `.agentic-sdlc/project.json`:

```json
"approval_sources": {
  "human_gate_default": "github-review",
  "allow_manual_fallback": false
}
```

When that mode is enabled, approved human gates must carry `github-review:` evidence in the form:

```text
github-review:<owner>/<repo>:pull/<pr>:review/<review-id>:reviewer/<login>
```

Assigned human authorities should also include a GitHub identity binding, either through `github_login` or an assignee in `github.com/<login>` form, so validation can confirm the review author matches the assigned approver.

`approve-from-github-pr` uses the GitHub CLI (`gh api repos/<owner>/<repo>/pulls/<pr>/reviews`) to fetch reviews, select the latest matching `APPROVED` review for the authority login, and record it through the same run-record approval path. Supply `--commit-sha` when you need the review tied to an exact reviewed revision; otherwise the command picks the latest approved review for the matching login. It fails closed if `gh` cannot reach GitHub or if no matching approved review exists.

An analogous GitLab MR approval-evidence adapter is available (`approve-from-gitlab` / `approve-from-gitlab-mr`, opt in via `human_gate_default: "gitlab-mr"`), for projects whose authoritative human-approval source is a GitLab merge request rather than a GitHub PR review. It has the same trust level as the GitHub adapter above — a trusted API attestation read from GitLab's own approval state, not independent non-repudiation or signing — and persists only the approver's pseudonymous GitLab username, never their name, email, or avatar. Only `gitlab.com/<username>` identities are recognized by convention; a self-hosted GitLab instance requires an explicit `gitlab_username` authority field. Because GitLab's approvals API exposes MR-level rather than per-approver timestamps and reviewed-commit values, `decided_at` and `commit_sha` in the resulting evidence are MR-level approximations, not exact per-approver facts, and `--commit-sha` filtering correctness depends on the GitLab project having "reset approvals on push" enabled.

### Linking a GitLab issue as an intent/requirements source

`link-intent-from-gitlab-issue` and `link-requirements-from-gitlab-issue` record where a task's G1 Intent or G2 Requirements Baseline actually came from, by fetching and validating a real GitLab issue rather than accepting a free-text label. This is a deliberately new capability, not a "speculative" one the way the GitLab MR approval adapter above is — it fills a gap that existed for every task until now: `intent_record_id`/`requirements_baseline_id` are run-record fields that have always existed in the schema but, before this, nothing ever set them.

```sh
agentic-sdlc link-intent-from-gitlab-issue --root /path/to/target --task-id TEAM-DEMO-001 --role product_owner --project-path group/project --issue-iid 42
agentic-sdlc link-requirements-from-gitlab-issue --root /path/to/target --task-id TEAM-DEMO-001 --role engineering_lead --project-path group/project --issue-iid 42
```

Each command fetches the issue via `glab api projects/<project>/issues/<iid>`, records it as gate-level evidence in the form `gitlab-issue:<project-path>:issues/<iid>`, and sets the corresponding run-record field to that URI. Re-linking replaces the gate's prior source-link evidence rather than accumulating it — including when the new link points at a different issue than the one previously linked, so the gate always carries at most one source-link entry, matching `intent_record_id`/`requirements_baseline_id`'s single-URI semantics. `invalidate`/`reenter` on G1/G2 clear the linked source (both the gate evidence and the run-record field) along with the rest of the gate's contribution, since a re-baselined gate no longer has a settled source.

**This is deliberately not approval evidence.** Linking a GitLab issue never marks G1/G2 approved, and gate approval (`approve-from-github`/`approve-from-gitlab*` above, or the LangGraph engine's `human_approval_{gate}` interrupt) is completely unaffected by whether a source is linked — the two are orthogonal by design. Authorization still requires the caller's `--role` to be an assigned, applicable authority for the target gate (the same discipline the approval adapters use), so only accountable humans can attach a source, but attaching one is not itself a sign-off.

Unlike the approval adapters, no per-person identity is ever fetched or persisted here — an issue link has no "approver" concept, so there is nothing to data-minimize away. Only the issue's `iid`, `title`, `state`, and `web_url` are used.

`validate` exits with `0` when valid and ready, `2` when structurally valid but blocked by unresolved decisions, and `1` for errors. Treat both `1` and `2` as non-ready in CI.

Initialization, detection, planning, status, and invalidation work with Python 3.10+ and the standard library. Install the pinned validation dependencies before using `validate`; validation fails closed when they are absent. Enable complete Draft
2020-12 structural validation in CI or assurance environments with:

```sh
python3 -m pip install -r plugins/agentic-sdlc/requirements-validation.txt
```

This kernel CLI covers bootstrapping and bookkeeping (`init`/`detect`/`validate`/`status`/`invalidate`/`approve-from-github*`). For actually dispatching and driving a task through the G1–G10 lifecycle — author/reviewer dispatch, human/mutation-gate interrupts, invalidation with real re-execution — use the LangGraph engine's `agentic-sdlc-lg` CLI or service in [`../../agentic_sdlc_langgraph/`](../../agentic_sdlc_langgraph).

## Team demonstration

Use a synthetic or non-production repository for the first demonstration:

1. Initialize the repository (`agentic-sdlc init`).
2. Show the generated unknown/unassigned values and explain why they fail closed.
3. Use `detect` to review observable stack and command candidates.
4. Use `plan` for an intent-and-requirements task and inspect the selected workflow, agents, `required_quality_gates`, and separate `human_gates`.
5. From `../../agentic_sdlc_langgraph/`, run `agentic-sdlc-lg plan`/`resume` against the same task and show it suspending at each gate's human-approval interrupt and at any matched mutation-gate phrase, with author/reviewer separation enforced structurally rather than by convention.
6. Validate and display the exported run record (`agentic-sdlc-lg export` / `validate`).
7. Change a material upstream assumption and demonstrate downstream invalidation (`agentic-sdlc-lg invalidate` then `reenter`) without granting a new approval.

## Upgrades and version lock

The generated overlay records both the kernel and plugin versions it was created against. Treat that lock as a compatibility declaration, not as proof that the project has adopted a newer lifecycle.

For an upgrade:

1. Update the plugin package to the new kernel version.
2. Review release and schema changes before changing the project lock.
3. Run `detect` and `validate` against the existing overlay and records.
4. Review any generated overlay differences; do not overwrite local authority or applicability decisions without an accountable owner.
5. Migrate incompatible records explicitly, rerun validation, and commit the lock change with the reviewed overlay changes.

Keep lifecycle state in version control according to the project's evidence-classification and retention rules. Do not commit secrets or raw approval credentials.

## Current limitations

- The development CLI requires Python 3.10 or newer; standalone executables are not bundled.
- Detection is advisory and inspects repository-root signatures rather than deeply evaluating every component. Candidate commands are not automatically trusted or executed.
- It cannot identify human authorities, legal obligations, risk acceptance, evidence-retention policy, or production authorization.
- The portable validator fails closed unless `requirements-validation.txt` is installed. With it, validation enforces lifecycle safety semantics and exhaustive Draft 2020-12 structural and format validation against the bundled schemas; CI enables this mode.
- The plugin prepares and validates decision records but does not authenticate an approver's real-world identity; projects must reference evidence from their authoritative approval system.
- The GitLab approval-evidence adapter is speculative: this kernel's own default provider profile uses GitHub PR reviews for approvals (GitLab is only its CI/CD platform). The GitLab adapter is fully wired and callable, but is not currently the approval source any bundled provider or profile actually selects.
- It does not deploy, apply infrastructure, run persistent migrations, accept risk, merge, or approve gates.
- Project-specific agent wrappers, knowledge-store integrations, CI wiring, and organization-specific impact extensions may require an overlay customization.
- Specialized SQS/BOM semantics remain unavailable until an authorized owner supplies definitions and applicability.

Use `show-contract lifecycle-gates` for the normative lifecycle contract. Provider-specific operating guidance belongs to the provider package.
