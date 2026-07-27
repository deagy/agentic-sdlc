import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PLUGIN_ROOT.parents[1]
DEFAULT_PROVIDER = REPOSITORY_ROOT / "providers" / "agentic-sdlc-defaults" / "provider.json"
sys.path.insert(0, str(PLUGIN_ROOT))
import agentic_sdlc  # type: ignore

# Every subprocess invocation below exercises the checked-out package via
# `-m` (the same invocation bin/agentic-sdlc uses), not an installed
# `agentic-sdlc` distribution -- PYTHONPATH must carry PLUGIN_ROOT since a
# subprocess doesn't inherit this test process's sys.path.insert() above.
CLI_COMMAND = [sys.executable, "-m", "agentic_sdlc"]


def cli_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    merged = {**os.environ, **(overrides or {})}
    merged["PYTHONPATH"] = os.pathsep.join(filter(None, [str(PLUGIN_ROOT), merged.get("PYTHONPATH")]))
    return merged


def tree_hash(root: Path) -> str:
    """Deterministic content hash of every file under root, keyed by relative
    path, so a dry-run invocation can be proven to write zero bytes."""
    hasher = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        hasher.update(str(path.relative_to(root)).encode("utf-8"))
        hasher.update(path.read_bytes())
    return hasher.hexdigest()


class V03MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, *arguments, provider=False, expected=0, env=None):
        command = list(CLI_COMMAND)
        if provider:
            command += ["--provider", str(DEFAULT_PROVIDER)]
        result = subprocess.run(
            command + list(arguments) + ["--root", str(self.root)],
            text=True,
            capture_output=True,
            check=False,
            env=cli_env(env),
        )
        self.assertEqual(expected, result.returncode, result.stderr or result.stdout)
        return json.loads(result.stdout or result.stderr)

    def load(self, relative):
        return json.loads((self.root / relative).read_text(encoding="utf-8"))

    def test_kernel_only_init_is_non_destructive_and_has_contract_digest(self):
        first = self.run_cli("init")
        self.assertIsNone(first["profile"])
        project_path = self.root / ".agentic-sdlc" / "project.json"
        project = self.load(".agentic-sdlc/project.json")
        project["human_note"] = "preserve"
        project_path.write_text(json.dumps(project), encoding="utf-8")
        second = self.run_cli("init", "--force")
        self.assertEqual([], second["created"])
        self.assertEqual("preserve", self.load(".agentic-sdlc/project.json")["human_note"])
        lock = self.load(".agentic-sdlc/version.lock")
        self.assertEqual("0.3.0", lock["kernel_version"])
        self.assertTrue(lock["contract_digest"].startswith("sha256:"))
        self.assertEqual([], self.run_cli("provider", "list"))

    def test_bundled_agent_resources_are_not_in_kernel(self):
        self.assertFalse((PLUGIN_ROOT / "contracts" / "agent-catalog.json").exists())
        self.assertFalse(list((PLUGIN_ROOT / "profiles").glob("*/profile.json")))
        contract = json.loads((PLUGIN_ROOT / "contracts" / "lifecycle-gates.json").read_text(encoding="utf-8"))
        self.assertEqual([f"G{i}" for i in range(1, 11)], [gate["id"] for gate in contract["gates"]])
        for gate in contract["gates"]:
            self.assertNotIn("author_agents", gate)
            self.assertNotIn("review_agents", gate)

    def test_provider_backed_profile_binds_dispatch_and_digests(self):
        result = self.run_cli("init", "--profile", "generic", provider=True, expected=0)
        self.assertEqual("generic", result["profile"])
        plan = self.run_cli("plan", "--task-id", "MIGRATE-1", "--task", "Create the service architecture", provider=True)
        self.assertEqual(["G1", "G2", "G3"], [gate["id"] for gate in plan["required_quality_gates"]])
        gate = next(item for item in plan["gate_dispatch"] if item["gate_id"] == "G3")
        self.assertEqual(["cloud-architect"], gate["agents"])
        self.assertEqual(["define-architecture"], gate["tasks"])
        record = self.load(".agentic-sdlc/runs/MIGRATE-1/run-record.json")
        self.assertEqual("0.3.0", record["kernel_version"])
        self.assertTrue(record["dispatch_binding_digest"].startswith("sha256:"))
        self.assertEqual("agentic-sdlc-defaults", record["provider_bindings"][0]["id"])

    def test_profile_requires_explicit_provider(self):
        result = self.run_cli("init", "--profile", "generic", expected=1)
        self.assertIn("unknown profile", result["error"])

    def test_provider_rejects_reviewer_with_author_capability(self):
        provider = json.loads(DEFAULT_PROVIDER.read_text(encoding="utf-8"))
        root = self.root / "bad-provider"
        (root / "profiles" / "p").mkdir(parents=True)
        (root / "catalog.json").write_text(json.dumps({"schema_version": 1, "agents": {"review": {"kind": "reviewer", "capabilities": ["reviewer", "author"]}}}), encoding="utf-8")
        (root / "profiles" / "p" / "profile.json").write_text(json.dumps({"id": "p", "version": "0.3.0", "gate_bindings": {}}), encoding="utf-8")
        provider.update({"id": "bad-provider", "agent_catalog": "catalog.json", "profile_roots": ["profiles"]})
        manifest = root / "provider.json"
        manifest.write_text(json.dumps(provider), encoding="utf-8")
        result = subprocess.run(
            CLI_COMMAND + ["--provider", str(manifest), "provider", "list"],
            text=True,
            capture_output=True,
            check=False,
            env=cli_env(),
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("reviewer", result.stderr)

    def test_upgrade_preserves_project_decisions(self):
        self.run_cli("init")
        lock_path = self.root / ".agentic-sdlc" / "version.lock"
        lock = self.load(".agentic-sdlc/version.lock")
        lock["kernel_version"] = "0.2.0"
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        check = self.run_cli("upgrade", "--check")
        self.assertEqual("changes-available", check["status"])
        project_path = self.root / ".agentic-sdlc" / "project.json"
        project = self.load(".agentic-sdlc/project.json")
        project["decision"] = "keep"
        project_path.write_text(json.dumps(project), encoding="utf-8")
        applied = self.run_cli("upgrade", "--apply")
        self.assertTrue(applied["mutation"])
        self.assertEqual("keep", self.load(".agentic-sdlc/project.json")["decision"])
        self.assertEqual("0.3.0", self.load(".agentic-sdlc/version.lock")["kernel_version"])

    def test_invalidation_and_reentry_preserve_history_but_clear_stale_bindings(self):
        self.run_cli("init", "--profile", "generic", provider=True)
        self.run_cli("plan", "--task-id", "REENTRY-1", "--task", "Create the service architecture", provider=True)
        self.run_cli("invalidate", "--task-id", "REENTRY-1", "--earliest-gate", "G2", "--reason", "requirements changed", "--actor", "test-owner")
        result = self.run_cli("reenter", "--task-id", "REENTRY-1", "--earliest-gate", "G2", "--reason", "prepare revised baseline", "--actor", "test-owner")
        self.assertEqual("reentered", result["status"])
        record = self.load(".agentic-sdlc/runs/REENTRY-1/run-record.json")
        self.assertEqual(2, len(record["re_entry_history"]))
        self.assertEqual("pending", record["lifecycle_gates"][1]["status"])
        self.assertEqual([], record["lifecycle_gates"][1]["human_approvals"])

    def test_github_latest_change_request_invalidates_older_approval(self):
        reviews = [
            {"id": 1, "state": "APPROVED", "submitted_at": "2030-01-01T00:00:00Z", "commit_id": "abc", "user": {"login": "reviewer"}},
            {"id": 2, "state": "CHANGES_REQUESTED", "submitted_at": "2030-01-02T00:00:00Z", "commit_id": "abc", "user": {"login": "reviewer"}},
        ]
        with self.assertRaises(ValueError):
            agentic_sdlc.select_github_review(reviews, "reviewer", "abc")

    # -- RG-4: init --dry-run -------------------------------------------------

    def test_init_dry_run_on_fresh_root_writes_nothing(self):
        before = tree_hash(self.root)
        result = self.run_cli("init", "--profile", "generic", "--dry-run", provider=True)
        self.assertEqual("dry-run", result["status"])
        self.assertFalse(result["mutation"])
        self.assertEqual("generic", result["profile"])
        self.assertIn(".agentic-sdlc/project.json", result["would_create"])
        self.assertEqual([], result["existing_unchanged"])
        self.assertTrue(result["agent_wrappers_would_create"])
        self.assertEqual([], result["agent_wrappers_existing"])
        self.assertIn("detected", result)
        self.assertEqual("would_create", result["agents_md"])
        after = tree_hash(self.root)
        self.assertEqual(before, after)
        self.assertFalse((self.root / ".agentic-sdlc").exists())
        self.assertFalse((self.root / ".codex").exists())
        self.assertFalse((self.root / ".claude").exists())
        self.assertFalse((self.root / "AGENTS.md").exists())

    def test_init_dry_run_after_real_init_reports_existing_unchanged_and_writes_nothing(self):
        self.run_cli("init", "--profile", "generic", provider=True)
        before = tree_hash(self.root)
        result = self.run_cli("init", "--profile", "generic", "--dry-run", provider=True)
        self.assertEqual("dry-run", result["status"])
        self.assertEqual([], result["would_create"])
        self.assertIn(".agentic-sdlc/project.json", result["existing_unchanged"])
        self.assertIn(".agentic-sdlc/version.lock", result["existing_unchanged"])
        self.assertEqual([], result["agent_wrappers_would_create"])
        self.assertTrue(result["agent_wrappers_existing"])
        # update_agents_md() always rewrites the managed block on a real init,
        # even when AGENTS.md already exists -- dry-run must not claim it's
        # unchanged just because the file is present.
        self.assertEqual("would_update_managed_block", result["agents_md"])
        after = tree_hash(self.root)
        self.assertEqual(before, after)

    def test_init_real_run_reports_agents_md_created_then_updated(self):
        first = self.run_cli("init", "--profile", "generic", provider=True)
        self.assertEqual("created", first["agents_md"])
        second = self.run_cli("init", "--profile", "generic", provider=True)
        self.assertEqual("updated_managed_block", second["agents_md"])

    def test_init_dry_run_rejects_combination_with_force(self):
        result = subprocess.run(
            CLI_COMMAND + ["init", "--dry-run", "--force", "--root", str(self.root)],
            text=True,
            capture_output=True,
            check=False,
            env=cli_env(),
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("not allowed with argument", result.stderr)

    # -- RG-1: GitLab MR approval-evidence adapter ----------------------------

    def test_gitlab_username_and_authority_helpers(self):
        self.assertEqual("alice", agentic_sdlc.gitlab_username_from_identity("gitlab.com/alice"))
        self.assertIsNone(agentic_sdlc.gitlab_username_from_identity("github.com/alice"))
        self.assertIsNone(agentic_sdlc.gitlab_username_from_identity(None))
        self.assertEqual(
            "explicit-alice",
            agentic_sdlc.authority_gitlab_username({"gitlab_username": "explicit-alice", "assignee": "gitlab.com/alice"}),
        )
        self.assertEqual("alice", agentic_sdlc.authority_gitlab_username({"assignee": "gitlab.com/alice"}))
        self.assertIsNone(agentic_sdlc.authority_gitlab_username({"assignee": "not-an-identity"}))

    def test_parse_gitlab_mr_uri(self):
        parsed = agentic_sdlc.parse_gitlab_mr_uri(
            "gitlab-mr:group/project:merge_requests/42:approval/7:approver/alice"
        )
        self.assertEqual(
            {"project_path": "group/project", "iid": "42", "approval_id": "7", "username": "alice"},
            parsed,
        )
        self.assertIsNone(agentic_sdlc.parse_gitlab_mr_uri("gitlab-mr:missing-fields"))

    def test_gitlab_approval_records_from_api_response_drops_name_email_and_avatar(self):
        raw = {
            "approved": True,
            "updated_at": "2030-01-01T00:00:00Z",
            "sha": "abc123",
            "approved_by": [
                {
                    "user": {
                        "id": 9,
                        "username": "alice",
                        "name": "Alice Example",
                        "email": "alice@example.com",
                        "avatar_url": "https://example.com/avatar.png",
                    }
                }
            ],
        }
        records = agentic_sdlc.gitlab_approval_records_from_api_response(raw)
        self.assertEqual(
            [{
                "approval_id": "9",
                "username": "alice",
                "state": "approved",
                "decided_at": "2030-01-01T00:00:00Z",
                "commit_sha": "abc123",
            }],
            records,
        )
        self.assertNotIn("name", records[0])
        self.assertNotIn("email", records[0])
        self.assertNotIn("avatar_url", records[0])

    def test_gitlab_latest_pending_state_invalidates_older_approval(self):
        approvals = [
            {"approval_id": "1", "username": "alice", "state": "approved", "decided_at": "2030-01-01T00:00:00Z", "commit_sha": "abc"},
            {"approval_id": "2", "username": "alice", "state": "pending", "decided_at": "2030-01-02T00:00:00Z", "commit_sha": "abc"},
        ]
        with self.assertRaises(ValueError):
            agentic_sdlc.select_gitlab_approval(approvals, "alice", "abc")

    def test_gitlab_approval_records_from_api_response_uses_approved_by_presence_not_mr_threshold(self):
        # GitLab's `approved_by` lists users who have individually already
        # approved, independent of whether the MR-level approval-rule
        # threshold (`approved`) has been satisfied. A partial-progress
        # response -- one approver in, threshold not yet met -- must still
        # surface that approver's own approval as "approved".
        raw = {
            "approved": False,
            "updated_at": "2030-01-01T00:00:00Z",
            "sha": "abc123",
            "approved_by": [
                {
                    "user": {
                        "id": 9,
                        "username": "alice",
                        "name": "Alice Example",
                        "email": "alice@example.com",
                        "avatar_url": "https://example.com/avatar.png",
                    }
                }
            ],
        }
        records = agentic_sdlc.gitlab_approval_records_from_api_response(raw)
        selected = agentic_sdlc.select_gitlab_approval(records, "alice", "abc123")
        self.assertEqual("approved", selected["state"])
        self.assertEqual("9", selected["approval_id"])

    def test_approval_source_policy_accepts_gitlab_mr_additively(self):
        self.assertEqual(
            {"human_gate_default": "gitlab-mr", "allow_manual_fallback": True},
            agentic_sdlc.approval_source_policy({"approval_sources": {"human_gate_default": "gitlab-mr"}}),
        )
        self.assertEqual(
            {"human_gate_default": "github-review", "allow_manual_fallback": True},
            agentic_sdlc.approval_source_policy({"approval_sources": {"human_gate_default": "github-review"}}),
        )
        with self.assertRaises(ValueError):
            agentic_sdlc.approval_source_policy({"approval_sources": {"human_gate_default": "bogus"}})

    def _assign_gitlab_authority(self, role="product_owner", username="alice"):
        authorities_path = self.root / ".agentic-sdlc" / "authorities.json"
        authorities = self.load(".agentic-sdlc/authorities.json")
        authorities[role].update({"status": "assigned", "assignee": f"gitlab.com/{username}", "applicability": "applicable"})
        authorities_path.write_text(json.dumps(authorities), encoding="utf-8")

    def test_approve_from_gitlab_records_username_only_evidence(self):
        self.run_cli("init", "--profile", "generic", provider=True)
        self._assign_gitlab_authority()
        self.run_cli("plan", "--task-id", "GL-1", "--task", "Create the service architecture", provider=True)
        result = self.run_cli(
            "approve-from-gitlab",
            "--task-id", "GL-1",
            "--gate", "G1",
            "--role", "product_owner",
            "--project-path", "group/project",
            "--mr-iid", "42",
            "--approval-id", "7",
            "--approver-username", "alice",
            "--commit-sha", "DEADBEEF",
            "--decided-at", "2030-01-01T00:00:00Z",
        )
        expected_uri = "gitlab-mr:group/project:merge_requests/42:approval/7:approver/alice"
        self.assertEqual(expected_uri, result["approval_uri"])
        record = self.load(".agentic-sdlc/runs/GL-1/run-record.json")
        gate = next(item for item in record["lifecycle_gates"] if item["gate_id"] == "G1")
        approval = gate["human_approvals"][0]
        self.assertEqual("gitlab.com/alice", approval["approver"]["id"])
        evidence = approval["evidence_refs"][0]
        self.assertEqual(expected_uri, evidence["uri"])
        self.assertEqual("sha256", evidence["hash_algorithm"])
        # Amendment B: only the pseudonymous username is ever persisted -- the
        # serialized run record must not contain any email/name/avatar text.
        serialized = json.dumps(record)
        self.assertNotIn("@", serialized)
        self.assertNotIn("avatar", serialized.lower())

    def test_approve_from_gitlab_rejects_username_mismatch_with_assigned_authority(self):
        self.run_cli("init", "--profile", "generic", provider=True)
        self._assign_gitlab_authority(username="alice")
        self.run_cli("plan", "--task-id", "GL-2", "--task", "Create the service architecture", provider=True)
        result = self.run_cli(
            "approve-from-gitlab",
            "--task-id", "GL-2",
            "--gate", "G1",
            "--role", "product_owner",
            "--project-path", "group/project",
            "--mr-iid", "42",
            "--approval-id", "7",
            "--approver-username", "mallory",
            "--commit-sha", "DEADBEEF",
            expected=1,
        )
        self.assertIn("does not match assigned authority username", result["error"])

    def test_approve_from_gitlab_rejects_role_not_required_by_gate(self):
        self.run_cli("init", "--profile", "generic", provider=True)
        self._assign_gitlab_authority(role="engineering_lead", username="alice")
        self.run_cli("plan", "--task-id", "GL-3", "--task", "Create the service architecture", provider=True)
        result = self.run_cli(
            "approve-from-gitlab",
            "--task-id", "GL-3",
            "--gate", "G1",
            "--role", "engineering_lead",
            "--project-path", "group/project",
            "--mr-iid", "42",
            "--approval-id", "7",
            "--approver-username", "alice",
            "--commit-sha", "DEADBEEF",
            expected=1,
        )
        self.assertIn("does not require authority role", result["error"])

    def test_approve_from_gitlab_mr_fetches_and_filters_by_commit(self):
        self.run_cli("init", "--profile", "generic", provider=True)
        self._assign_gitlab_authority(username="alice")
        self.run_cli("plan", "--task-id", "GL-4", "--task", "Create the service architecture", provider=True)
        mock_path = self.root / "gitlab-approvals-mock.json"
        # This mocks the *raw*, unnormalized `glab api
        # projects/:id/merge_requests/:iid/approvals` response shape (a
        # single MR-level object, with `name`/`email`/`avatar_url` on the
        # `user` objects exactly as GitLab actually returns them) rather
        # than a pre-normalized record list, so this test exercises the
        # real `fetch_gitlab_mr_approvals` -> normalizer wiring instead of
        # bypassing it.
        mock_path.write_text(json.dumps({
            "approved": False,
            "updated_at": "2030-01-02T00:00:00Z",
            "sha": "def",
            "approved_by": [
                {
                    "user": {
                        "id": 2,
                        "username": "alice",
                        "name": "Alice Example",
                        "email": "alice@example.com",
                        "avatar_url": "https://example.com/avatar.png",
                    }
                }
            ],
        }), encoding="utf-8")
        result = self.run_cli(
            "approve-from-gitlab-mr",
            "--task-id", "GL-4",
            "--gate", "G1",
            "--role", "product_owner",
            "--project-path", "group/project",
            "--mr-iid", "42",
            "--commit-sha", "def",
            env={"AGENTIC_SDLC_TEST_GITLAB_APPROVALS_FILE": str(mock_path)},
        )
        self.assertEqual("2", result["selected_approval_id"])
        self.assertEqual("def", result["selected_commit_sha"])
        # Amendment B: the normalizer must have been exercised for real --
        # no name/email/avatar text leaks into the CLI result or the
        # persisted run record.
        serialized_result = json.dumps(result)
        self.assertNotIn("@", serialized_result)
        self.assertNotIn("avatar", serialized_result.lower())
        record = self.load(".agentic-sdlc/runs/GL-4/run-record.json")
        serialized_record = json.dumps(record)
        self.assertNotIn("@", serialized_record)
        self.assertNotIn("avatar", serialized_record.lower())
        self.assertEqual(
            "gitlab-mr:group/project:merge_requests/42:approval/2:approver/alice",
            result["approval_uri"],
        )

    def test_validate_flags_malformed_gitlab_mr_uri(self):
        self.run_cli("init", "--profile", "generic", provider=True)
        self._assign_gitlab_authority(username="alice")
        self.run_cli("plan", "--task-id", "GL-5", "--task", "Create the service architecture", provider=True)
        self.run_cli(
            "approve-from-gitlab",
            "--task-id", "GL-5",
            "--gate", "G1",
            "--role", "product_owner",
            "--project-path", "group/project",
            "--mr-iid", "42",
            "--approval-id", "7",
            "--approver-username", "alice",
            "--commit-sha", "DEADBEEF",
            "--decided-at", "2030-01-01T00:00:00Z",
        )
        record_relative = ".agentic-sdlc/runs/GL-5/run-record.json"
        record_path = self.root / record_relative
        record = self.load(record_relative)
        gate = next(item for item in record["lifecycle_gates"] if item["gate_id"] == "G1")
        gate["human_approvals"][0]["evidence_refs"][0]["uri"] = "gitlab-mr:malformed-uri-missing-fields"
        # The URI-shape check only runs on an approved gate's evidence; force
        # gate status here (independent of the other approval-completeness
        # checks, which are exercised elsewhere) so this test isolates the
        # specific non-regression fix: a malformed gitlab-mr: URI must not
        # pass through validation unvalidated, exactly like github-review:.
        gate["status"] = "approved"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        result = self.run_cli("validate", provider=True, expected=1)
        self.assertTrue(
            any("invalid GitLab MR approval URI" in error for error in result["errors"]),
            result["errors"],
        )


class AgentCatalogSchemaTests(unittest.TestCase):
    """Validates `agent-catalog.schema.json`'s `transport`/`endpoint`
    extension (added for A2A protocol support): both fields are optional
    (so existing catalogs, e.g. `providers/agentic-sdlc-defaults`'s, need
    no changes), but `transport: "a2a"` requires `endpoint`, and
    `additionalProperties: false` still rejects unknown fields.
    """

    @classmethod
    def setUpClass(cls):
        import jsonschema  # type: ignore

        cls.jsonschema = jsonschema
        schema_path = PLUGIN_ROOT / "contracts" / "agent-catalog.schema.json"
        cls.schema = json.loads(schema_path.read_text(encoding="utf-8"))
        cls.validator = jsonschema.Draft202012Validator(cls.schema)

    def assert_valid(self, catalog):
        errors = list(self.validator.iter_errors(catalog))
        self.assertEqual([], errors, [error.message for error in errors])

    def assert_invalid(self, catalog):
        errors = list(self.validator.iter_errors(catalog))
        self.assertNotEqual([], errors)

    def test_default_provider_catalog_is_unaffected_by_the_new_optional_fields(self):
        default_provider_catalog = (
            PLUGIN_ROOT.parents[1] / "providers" / "agentic-sdlc-defaults" / "agent-catalog.json"
        )
        catalog = json.loads(default_provider_catalog.read_text(encoding="utf-8"))
        self.assert_valid(catalog)

    def test_local_transport_entry_without_endpoint_is_valid(self):
        self.assert_valid(
            {
                "schema_version": 1,
                "agents": {
                    "local-author": {
                        "kind": "author",
                        "capabilities": ["author"],
                        "transport": "local",
                    }
                },
            }
        )

    def test_a2a_transport_entry_with_endpoint_is_valid(self):
        self.assert_valid(
            {
                "schema_version": 1,
                "agents": {
                    "external-reviewer": {
                        "kind": "reviewer",
                        "capabilities": ["reviewer"],
                        "transport": "a2a",
                        "endpoint": "https://codex-agent.example.com",
                    }
                },
            }
        )

    def test_a2a_transport_entry_missing_endpoint_is_invalid(self):
        self.assert_invalid(
            {
                "schema_version": 1,
                "agents": {
                    "external-reviewer": {
                        "kind": "reviewer",
                        "capabilities": ["reviewer"],
                        "transport": "a2a",
                    }
                },
            }
        )

    def test_unknown_agent_field_is_still_rejected(self):
        self.assert_invalid(
            {
                "schema_version": 1,
                "agents": {
                    "typo-agent": {
                        "kind": "author",
                        "capabilities": ["author"],
                        "trasnport": "local",
                    }
                },
            }
        )


if __name__ == "__main__":
    unittest.main()
