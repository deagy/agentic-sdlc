import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PLUGIN_ROOT.parents[1]
CLI = PLUGIN_ROOT / "scripts" / "agentic_sdlc.py"
DEFAULT_PROVIDER = REPOSITORY_ROOT / "providers" / "agentic-sdlc-defaults" / "provider.json"
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
import agentic_sdlc  # type: ignore


class V03MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, *arguments, provider=False, expected=0):
        command = [sys.executable, str(CLI)]
        if provider:
            command += ["--provider", str(DEFAULT_PROVIDER)]
        result = subprocess.run(command + list(arguments) + ["--root", str(self.root)], text=True, capture_output=True, check=False)
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
        result = subprocess.run([sys.executable, str(CLI), "--provider", str(manifest), "provider", "list"], text=True, capture_output=True, check=False)
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


if __name__ == "__main__":
    unittest.main()
