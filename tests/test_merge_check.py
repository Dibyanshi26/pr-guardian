"""Tests for merge_check.py against real git repositories built in
tmp_path -- no subprocess mocking. fetch_ref is exercised against a real
second local repo acting as "origin" (a file:// remote), the same way it
would be exercised against GitHub in production.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from guardian.merge_check import (
    GitError,
    MergeCheckReport,
    check_merge,
    delete_ref,
    fetch_ref,
    pull_ref,
    run_merge_checks,
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t.com")
    _git(path, "config", "user.name", "t")
    return path


def _commit(repo: Path, filename: str, content: str, message: str) -> str:
    (repo / filename).write_text(content)
    _git(repo, "add", filename)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _checkout_new_branch(repo: Path, branch: str, from_ref: str = "main") -> None:
    _git(repo, "checkout", "-q", from_ref)
    _git(repo, "checkout", "-q", "-b", branch)


# --- check_merge: pure git-object operations, single repo ---


def test_clean_merge_reports_no_conflict(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "base\n", "base commit")
    _checkout_new_branch(repo, "feature")
    _commit(repo, "g.txt", "new file\n", "add g.txt")

    result = check_merge(repo, "main", "feature", label="main")

    assert result.conflicted is False
    assert result.conflicting_files == []
    assert result.error is None


def test_conflicting_merge_reports_conflicted_files(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "line1\n", "base commit")

    _checkout_new_branch(repo, "branch-a")
    _commit(repo, "f.txt", "line1\nlineA\n", "A appends")

    _checkout_new_branch(repo, "branch-b")
    _commit(repo, "f.txt", "line1\nlineB\n", "B appends, same spot")

    result = check_merge(repo, "branch-a", "branch-b", label="branch-a")

    assert result.conflicted is True
    assert "f.txt" in result.conflicting_files
    assert result.error is None


def test_same_file_different_lines_merges_cleanly(tmp_path):
    """The scenario overlap detection exists to catch: two branches edit
    different parts of the same file. git sees no textual conflict here
    -- this is the empirical proof that merge-tree alone is not enough,
    and overlap.py is a genuinely separate, necessary check."""
    repo = _init_repo(tmp_path / "repo")
    lines = "\n".join(str(i) for i in range(1, 21)) + "\n"
    _commit(repo, "f.txt", lines, "base commit")

    _checkout_new_branch(repo, "branch-a")
    content = (repo / "f.txt").read_text().splitlines()
    content[0] = "CHANGED_TOP"
    (repo / "f.txt").write_text("\n".join(content) + "\n")
    _git(repo, "commit", "-q", "-am", "A changes top")

    _checkout_new_branch(repo, "branch-b", from_ref="main")
    content = (repo / "f.txt").read_text().splitlines()
    content[-1] = "CHANGED_BOTTOM"
    (repo / "f.txt").write_text("\n".join(content) + "\n")
    _git(repo, "commit", "-q", "-am", "B changes bottom")

    result = check_merge(repo, "branch-a", "branch-b", label="branch-a")

    assert result.conflicted is False
    assert result.conflicting_files == []


def test_no_common_ancestor_degrades_to_an_error_result_not_an_exception(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "base\n", "base commit")
    _checkout_new_branch(repo, "feature")
    _commit(repo, "g.txt", "new\n", "add g.txt")

    _git(repo, "checkout", "-q", "--orphan", "orphan-branch")
    _git(repo, "rm", "-rf", "--cached", ".")
    (repo / "h.txt").write_text("orphan\n")
    _git(repo, "add", "h.txt")
    _git(repo, "commit", "-q", "-m", "orphan commit")

    result = check_merge(repo, "feature", "orphan-branch", label="feature")

    assert result.conflicted is False
    assert result.error is not None
    assert "unrelated histories" in result.error.lower()


# --- fetch_ref / delete_ref: exercised against a real second local repo
# acting as "origin", the same way GitHub would be fetched from ---


def _init_origin_with_pr(tmp_path: Path, name: str, pr_number: int) -> tuple[Path, str]:
    """A standalone repo with a base commit on main and a PR branch,
    with refs/pull/<N>/head pointing at the PR branch tip -- mirroring
    what GitHub itself maintains for every PR."""
    origin = _init_repo(tmp_path / name)
    _commit(origin, "base.txt", "base\n", "base commit")
    _checkout_new_branch(origin, f"pr-{pr_number}-branch")
    sha = _commit(origin, f"pr{pr_number}.txt", f"pr {pr_number}\n", f"pr {pr_number} commit")
    _git(origin, "update-ref", pull_ref(pr_number), sha)
    return origin, sha


def test_fetch_ref_pulls_a_pr_ref_from_a_real_remote_without_touching_the_worktree(tmp_path):
    origin, sha = _init_origin_with_pr(tmp_path, "origin", pr_number=42)

    clone = _init_repo(tmp_path / "clone")
    _commit(clone, "base.txt", "base\n", "base commit")
    _git(clone, "remote", "add", "origin", str(origin))

    fetch_ref(clone, "origin", pull_ref(42), "refs/guardian/1/other-42")

    assert _git(clone, "rev-parse", "refs/guardian/1/other-42").stdout.strip() == sha
    # Never touched the working tree or checked anything out.
    assert not (clone / "pr42.txt").exists()
    assert _git(clone, "symbolic-ref", "HEAD").stdout.strip() == "refs/heads/main"


def test_fetch_ref_raises_git_error_for_a_ref_that_does_not_exist(tmp_path):
    origin, _ = _init_origin_with_pr(tmp_path, "origin", pr_number=42)

    clone = _init_repo(tmp_path / "clone")
    _commit(clone, "base.txt", "base\n", "base commit")
    _git(clone, "remote", "add", "origin", str(origin))

    with pytest.raises(GitError):
        fetch_ref(clone, "origin", pull_ref(999), "refs/guardian/1/other-999")


def test_delete_ref_removes_a_previously_fetched_ref(tmp_path):
    origin, sha = _init_origin_with_pr(tmp_path, "origin", pr_number=42)
    clone = _init_repo(tmp_path / "clone")
    _commit(clone, "base.txt", "base\n", "base commit")
    _git(clone, "remote", "add", "origin", str(origin))
    fetch_ref(clone, "origin", pull_ref(42), "refs/guardian/1/other-42")

    delete_ref(clone, "refs/guardian/1/other-42")

    result = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "refs/guardian/1/other-42"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_delete_ref_is_a_best_effort_noop_for_a_ref_that_was_never_created(tmp_path):
    clone = _init_repo(tmp_path / "clone")
    _commit(clone, "base.txt", "base\n", "base commit")

    delete_ref(clone, "refs/guardian/1/other-999")  # must not raise


# --- Cross-PR ref collision: two different "this PR" numbers comparing
# against the same other-PR number must never share a ref name ---


def test_two_different_this_pr_namespaces_fetching_the_same_other_pr_do_not_collide(tmp_path):
    origin, sha = _init_origin_with_pr(tmp_path, "origin", pr_number=12)
    clone = _init_repo(tmp_path / "clone")
    _commit(clone, "base.txt", "base\n", "base commit")
    _git(clone, "remote", "add", "origin", str(origin))

    ref_from_pr10 = "refs/guardian/10/other-12"
    ref_from_pr11 = "refs/guardian/11/other-12"

    # Interleaved, the way two concurrent Actions runs for different PRs
    # would race against each other on a shared runner pool.
    fetch_ref(clone, "origin", pull_ref(12), ref_from_pr10)
    fetch_ref(clone, "origin", pull_ref(12), ref_from_pr11)

    assert _git(clone, "rev-parse", ref_from_pr10).stdout.strip() == sha
    assert _git(clone, "rev-parse", ref_from_pr11).stdout.strip() == sha

    # PR #10's run finishes and cleans up its own ref...
    delete_ref(clone, ref_from_pr10)

    # ...PR #11's run's ref (and its data) must be untouched.
    assert _git(clone, "rev-parse", ref_from_pr11).stdout.strip() == sha
    deleted = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", ref_from_pr10],
        capture_output=True,
        text=True,
    )
    assert deleted.returncode != 0


# --- run_merge_checks: full orchestration against a base branch and
# multiple other open PRs, including one that fails to fetch ---


def test_run_merge_checks_against_base_and_other_prs(tmp_path):
    origin = _init_repo(tmp_path / "origin")
    _commit(origin, "base.txt", "base\n", "base commit")

    _checkout_new_branch(origin, "this-pr-branch")
    _commit(origin, "this.txt", "this pr\n", "this pr commit")
    _git(origin, "update-ref", pull_ref(1), "this-pr-branch")

    _checkout_new_branch(origin, "clean-pr-branch", from_ref="main")
    _commit(origin, "clean.txt", "clean pr\n", "clean pr commit")
    _git(origin, "update-ref", pull_ref(2), "clean-pr-branch")

    # PR #3 edits this.txt in a way that conflicts with PR #1.
    _checkout_new_branch(origin, "conflicting-pr-branch", from_ref="main")
    _commit(origin, "this.txt", "conflicting content\n", "conflicting pr commit")
    _git(origin, "update-ref", pull_ref(3), "conflicting-pr-branch")

    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "base.txt", "base\n", "base commit")
    _git(repo, "remote", "add", "origin", str(origin))

    report = run_merge_checks(
        repo_path=repo,
        remote="origin",
        this_pr_number=1,
        base_branch="main",
        other_prs=[(2, "clean pr"), (3, "conflicting pr")],
    )

    assert isinstance(report, MergeCheckReport)
    assert report.against_base.conflicted is False
    assert report.against_base.error is None

    by_pr = {r.pr_number: r for r in report.against_other_prs}
    assert by_pr[2].conflicted is False
    assert by_pr[3].conflicted is True
    assert "this.txt" in by_pr[3].conflicting_files

    # Cleanup: none of this PR's namespaced refs should remain afterwards.
    leftover = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "refs/guardian/1"],
        capture_output=True,
        text=True,
    )
    assert leftover.stdout.strip() == ""


def test_run_merge_checks_degrades_one_pr_whose_branch_was_deleted(tmp_path):
    origin = _init_repo(tmp_path / "origin")
    _commit(origin, "base.txt", "base\n", "base commit")

    _checkout_new_branch(origin, "this-pr-branch")
    _commit(origin, "this.txt", "this pr\n", "this pr commit")
    _git(origin, "update-ref", pull_ref(1), "this-pr-branch")

    _checkout_new_branch(origin, "clean-pr-branch", from_ref="main")
    _commit(origin, "clean.txt", "clean pr\n", "clean pr commit")
    _git(origin, "update-ref", pull_ref(2), "clean-pr-branch")
    # PR #99's ref is never created -- simulates a deleted branch / a PR
    # this fetch has no permission to read.

    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "base.txt", "base\n", "base commit")
    _git(repo, "remote", "add", "origin", str(origin))

    report = run_merge_checks(
        repo_path=repo,
        remote="origin",
        this_pr_number=1,
        base_branch="main",
        other_prs=[(2, "clean pr"), (99, "deleted-branch pr")],
    )

    by_pr = {r.pr_number: r for r in report.against_other_prs}
    assert by_pr[2].conflicted is False
    assert by_pr[2].error is None
    assert by_pr[99].error is not None
    # The one bad comparison doesn't take down the report as a whole.
    assert report.against_base.error is None


def test_run_merge_checks_degrades_entirely_when_this_prs_own_head_cannot_be_fetched(tmp_path):
    origin = _init_repo(tmp_path / "origin")
    _commit(origin, "base.txt", "base\n", "base commit")
    # No ref registered for PR #1 itself.

    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "base.txt", "base\n", "base commit")
    _git(repo, "remote", "add", "origin", str(origin))

    report = run_merge_checks(
        repo_path=repo,
        remote="origin",
        this_pr_number=1,
        base_branch="main",
        other_prs=[(2, "some other pr")],
    )

    assert report.against_base.error is not None
    assert report.against_other_prs[0].error is not None
