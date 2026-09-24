"""Guards against regressing the workflow's concurrency control, which is
what actually prevents two overlapping runs (e.g. two quick pushes) from
each deciding "no existing comment" and creating a duplicate. The
create-or-update logic in main.py can't fix this by itself: GitHub's REST
API has no compare-and-swap for issue comments, so overlapping runs must be
prevented at the workflow level instead."""

from pathlib import Path

import yaml

WORKFLOW_PATH = Path(__file__).resolve().parent.parent / ".github/workflows/pr-guardian.yml"


def _load_workflow() -> dict:
    with open(WORKFLOW_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_workflow_has_a_concurrency_group_keyed_by_pr():
    workflow = _load_workflow()
    concurrency = workflow.get("concurrency")
    assert concurrency is not None, "workflow is missing a concurrency block"
    assert "${{ github.event.pull_request.number }}" in concurrency["group"]


def test_workflow_cancels_in_progress_runs():
    workflow = _load_workflow()
    assert workflow["concurrency"]["cancel-in-progress"] is True


def test_workflow_still_has_minimal_permissions():
    workflow = _load_workflow()
    assert workflow["permissions"] == {
        "contents": "read",
        "pull-requests": "write",
        "checks": "write",
    }


def test_workflow_checkout_has_full_history_for_merge_tree():
    # git merge-tree computes its own merge base by walking commit
    # history; a shallow clone can leave no common ancestor at all,
    # which merge-tree reports as "refusing to merge unrelated histories".
    workflow = _load_workflow()
    steps = workflow["jobs"]["guardian"]["steps"]
    checkout_step = next(s for s in steps if s.get("uses", "").startswith("actions/checkout"))
    assert checkout_step.get("with", {}).get("fetch-depth") == 0
