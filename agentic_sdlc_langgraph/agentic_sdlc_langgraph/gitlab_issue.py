"""GitLab issue linkage for G1 Intent / G2 Requirements Baseline.

Ported close to unchanged from `agentic_sdlc.py` (no project-overlay
dependency at all, same as `github_approval.py`):

- `GITLAB_ISSUE_URI` / `parse_gitlab_issue_uri` (kernel ~104-112 / ~514-518)
- `fetch_gitlab_issue` (kernel ~528-558), including the exact
  `AGENTIC_SDLC_TEST_GITLAB_ISSUE_FILE` env-var mocking convention the
  kernel test suite uses, so tests here can run with zero network/`glab`
  dependency.

Deliberately NOT an approval adapter, and deliberately NOT wired through
`resume_gate_with_*`/`Command(resume=...)` the way `github_approval.py` is:
linking a GitLab issue records where a task's intent/requirements content
came from, not a human's sign-off on it, and G1/G2's `human_approval_{gate}`
interrupt is unrelated to this. There is also no equivalent of
`github_review_to_approval` here -- an issue link produces no `Approval`,
only a plain URI string consumed by `cli.py`'s `plan` (see that module) to
seed `SDLCState.intent_record_id` / `requirements_baseline_id` once, at
plan time, the same way `--task` seeds `state["scope"]`.

Scope note (asymmetric with the kernel on purpose): the kernel additionally
attaches a gate-level `evidence_refs` entry when linking an issue
(`record_gitlab_issue_link` in `agentic_sdlc.py`). This package does not --
`GateState.evidence_refs` is populated generically by `graph.py`'s
`gate_decision_{gate_id}` from `agent_outputs`, and a plan-time-seeded
issue link is not an agent output, so reproducing that here would mean
special-casing `graph.py`'s gate-decision node for G1/G2, which this
package's design deliberately avoids (see `cli.py`'s `plan` docstring for
why: no changes to the dispatch/model-call path). `intent_record_id` /
`requirements_baseline_id` alone are still exported into the run record
either way.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

GITLAB_ISSUE_URI = re.compile(
    r"^gitlab-issue:(?P<project_path>[A-Za-z0-9_./-]+):issues/(?P<iid>\d+)$"
)


def parse_gitlab_issue_uri(value: str) -> dict[str, str] | None:
    """Port of `agentic_sdlc.py`'s `parse_gitlab_issue_uri`."""
    match = GITLAB_ISSUE_URI.fullmatch(value)
    if not match:
        return None
    return match.groupdict()


def fetch_gitlab_issue(project_path: str, issue_iid: int) -> dict[str, Any]:
    """Port of `agentic_sdlc.py`'s `fetch_gitlab_issue`. No author/assignee
    identity is ever read here -- an issue link has no approver concept, so
    there is nothing to minimize away; only the fields needed to identify
    and reference the issue are kept.

    When `AGENTIC_SDLC_TEST_GITLAB_ISSUE_FILE` is set, reads a JSON object
    from that file instead of shelling out to `glab api` -- the exact
    mocking convention the kernel test suite uses, ported verbatim so tests
    here need neither network access nor a `glab` binary.
    """
    mock_path = os.environ.get("AGENTIC_SDLC_TEST_GITLAB_ISSUE_FILE")
    if mock_path:
        raw_response = json.loads(Path(mock_path).read_text(encoding="utf-8"))
    else:
        encoded_project = quote(project_path, safe="")
        result = subprocess.run(
            ["glab", "api", f"projects/{encoded_project}/issues/{issue_iid}"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown glab api failure"
            raise ValueError(f"unable to fetch GitLab issue for {project_path} issue {issue_iid}: {detail}")
        raw_response = json.loads(result.stdout)
    if not isinstance(raw_response, dict):
        raise ValueError("GitLab issue API response must be a JSON object")
    title = raw_response.get("title")
    state = raw_response.get("state")
    if not isinstance(title, str) or not title:
        raise ValueError(f"GitLab issue {project_path}#{issue_iid} response is missing a title")
    if state not in {"opened", "closed"}:
        raise ValueError(f"GitLab issue {project_path}#{issue_iid} response has an unrecognized state: {state!r}")
    return {
        "iid": issue_iid,
        "title": title,
        "state": state,
        "web_url": raw_response.get("web_url"),
        "updated_at": raw_response.get("updated_at"),
    }


def gitlab_issue_uri(project_path: str, issue_iid: int) -> str:
    """Build and validate the `gitlab-issue:` URI for an already-fetched
    issue, mirroring the kernel's own parse-your-own-output discipline in
    `record_gitlab_issue_link`."""
    uri = f"gitlab-issue:{project_path}:issues/{issue_iid}"
    if parse_gitlab_issue_uri(uri) is None:
        raise ValueError(f"invalid GitLab issue URI components for {uri}")
    return uri


def resolve_issue_reference(value: str | None) -> str | None:
    """Parse a `<project-path>#<iid>` reference (the `--intent-gitlab-issue`
    / `--requirements-gitlab-issue` CLI flag shape, and `CreateTaskRequest`'s
    equivalent fields in `service.py`), fetch the issue, and return its
    validated `gitlab-issue:...` URI -- or `None` if `value` is `None`.
    Shared by `cli.py` and `service.py` so both surfaces parse/fetch/build
    the URI identically. Raises `ValueError` on a malformed reference or an
    unfetchable/invalid issue; callers translate that into their own
    surface's error shape."""
    if value is None:
        return None
    project_path, separator, iid_text = value.rpartition("#")
    if not separator or not project_path or not iid_text.isdigit():
        raise ValueError(f"GitLab issue reference must be in <project-path>#<iid> form, got {value!r}")
    issue = fetch_gitlab_issue(project_path, int(iid_text))
    return gitlab_issue_uri(project_path, issue["iid"])
