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
    # Phase 4 made the group expression conditional on event type (see
    # test_workflow_concurrency_group_branches_by_event_type below), so
    # "github.event.pull_request.number" no longer appears as its own
    # standalone "${{ ... }}" block -- just check it's still referenced
    # somewhere in the (now single, ternary) expression.
    workflow = _load_workflow()
    concurrency = workflow.get("concurrency")
    assert concurrency is not None, "workflow is missing a concurrency block"
    assert "github.event.pull_request.number" in concurrency["group"]


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


# --- Phase 4: push-to-main trigger, its own concurrency group, and the debounce ---


def test_workflow_triggers_on_push_to_main():
    workflow = _load_workflow()
    # PyYAML parses the bare "on:" key as the boolean True (a YAML 1.1
    # quirk: on/off/yes/no are boolean literals) -- not a string "on".
    triggers = workflow[True]
    assert triggers["push"]["branches"] == ["main"]
    # The existing pull_request trigger must still be intact alongside it.
    assert triggers["pull_request"]["types"] == ["opened", "synchronize", "reopened"]


def test_workflow_concurrency_group_branches_by_event_type():
    workflow = _load_workflow()
    group = workflow["concurrency"]["group"]
    assert "pr-guardian-main-push" in group
    assert "github.event.pull_request.number" in group
    assert "github.event_name == 'push'" in group


def test_workflow_debounces_push_triggered_runs():
    workflow = _load_workflow()
    steps = workflow["jobs"]["guardian"]["steps"]
    debounce_step = next((s for s in steps if "sleep" in s.get("run", "")), None)
    assert debounce_step is not None, "no debounce step found"
    assert debounce_step.get("if") == "github.event_name == 'push'"


def test_workflow_debounce_step_runs_before_checkout():
    workflow = _load_workflow()
    steps = workflow["jobs"]["guardian"]["steps"]
    step_names = [s.get("name", "") for s in steps]
    debounce_index = next(i for i, s in enumerate(steps) if "sleep" in s.get("run", ""))
    checkout_index = next(i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/checkout"))
    assert debounce_index < checkout_index, f"debounce must run before checkout, got order {step_names}"
