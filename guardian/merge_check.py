"""Detect whether this PR would produce merge conflicts against the base
branch, or against another currently-open PR, using `git merge-tree`.

Every function here takes an explicit repo_path and ref names -- nothing
reads env vars or talks to GitHub -- so this module is testable against
real git repos built with `git init` in a tmp dir, no subprocess mocking
needed.

Ref-fetching convention: always fetch by GitHub's numeric PR ref
(refs/pull/<N>/head), never by branch name, since GitHub mirrors that ref
for fork PRs too -- no fork remote is ever needed. Every ref this module
creates is namespaced under refs/guardian/<this-pr-number>/... rather than
refs/guardian/<other-pr-number>/... : Phase 1's workflow concurrency group
only prevents two runs for the *same* PR from overlapping, not two
different PRs' Guardian runs (which GitHub Actions can and does run in
parallel) from racing to fetch/delete a ref for the *same* other PR at
the same time. Namespacing by both PR numbers makes every run's refs
disjoint, so no locking is needed.

`git merge-tree --write-tree` only ever writes a tree object to the
object database -- it never touches HEAD, the index, or the working
tree, so no checkout/reset/stash dance is needed around it.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

GUARDIAN_REF_ROOT = "refs/guardian"


class GitError(Exception):
    """A git operation guardian relies on failed outright (e.g. a fetch
    for a deleted branch, or a missing common ancestor). Distinct from a
    merge conflict, which is a normal, expected MergeCheckResult."""


@dataclass
class MergeCheckResult:
    label: str
    conflicted: bool
    conflicting_files: list[str] = field(default_factory=list)
    error: str | None = None
    pr_number: int | None = None


@dataclass
class MergeCheckReport:
    against_base: MergeCheckResult
    against_other_prs: list[MergeCheckResult] = field(default_factory=list)


def pull_ref(pr_number: int) -> str:
    return f"refs/pull/{pr_number}/head"


def _namespaced_ref(this_pr_number: int, suffix: str) -> str:
    return f"{GUARDIAN_REF_ROOT}/{this_pr_number}/{suffix}"


def fetch_ref(repo_path, remote: str, remote_ref: str, local_ref: str) -> None:
    """Fetch remote_ref (e.g. 'refs/pull/42/head') into local_ref, a
    namespaced ref, without touching the working tree, index, or any
    local branch. Raises GitError on failure (deleted branch, no read
    access, network issue) so callers can degrade just one comparison."""
    proc = subprocess.run(
        ["git", "-C", str(repo_path), "fetch", "--no-tags", remote, f"+{remote_ref}:{local_ref}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git fetch {remote_ref} failed")


def delete_ref(repo_path, local_ref: str) -> None:
    """Best-effort cleanup of a namespaced ref created by fetch_ref."""
    subprocess.run(
        ["git", "-C", str(repo_path), "update-ref", "-d", local_ref],
        capture_output=True,
        text=True,
    )


def check_merge(repo_path, ref_a: str, ref_b: str, label: str) -> MergeCheckResult:
    """Simulate merging ref_b into ref_a with `git merge-tree`. Never
    raises: any outcome other than a clean merge (conflict, or a hard
    git failure like no common ancestor -- refusing to merge unrelated
    histories, which is what a too-shallow clone produces) is reported
    on the result instead, so one bad comparison can't take down the
    whole check."""
    proc = subprocess.run(
        ["git", "-C", str(repo_path), "merge-tree", "--write-tree", "--name-only", ref_a, ref_b],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return MergeCheckResult(label=label, conflicted=False)

    if proc.returncode == 1:
        # Output shape (verified against git 2.53): tree OID, blank line,
        # one conflicting path per line (--name-only), blank line, then
        # informational messages.
        lines = proc.stdout.split("\n")
        conflicting_files: list[str] = []
        for line in lines[1:]:
            if line == "":
                break
            conflicting_files.append(line)
        return MergeCheckResult(label=label, conflicted=True, conflicting_files=conflicting_files)

    error = proc.stderr.strip() or f"git merge-tree exited with status {proc.returncode}"
    return MergeCheckResult(label=label, conflicted=False, error=error)


def run_merge_checks(
    repo_path,
    remote: str,
    this_pr_number: int,
    base_branch: str,
    other_prs: list[tuple[int, str]],
) -> MergeCheckReport:
    """Fetch this PR's head and the base branch, check this PR against
    the base branch and against each other open PR's head, and clean up
    every namespaced ref it creates along the way.

    other_prs is (pr_number, title) for each other open PR to compare
    against. A per-PR fetch failure degrades just that comparison; a
    failure fetching this PR's own head or the base branch degrades the
    whole report (every comparison gets the same error) rather than
    raising, since without those refs nothing here can be evaluated.
    """
    this_ref = _namespaced_ref(this_pr_number, "head")
    base_ref = _namespaced_ref(this_pr_number, "base")

    try:
        fetch_ref(repo_path, remote, pull_ref(this_pr_number), this_ref)
        fetch_ref(repo_path, remote, f"refs/heads/{base_branch}", base_ref)
    except GitError as exc:
        error = f"could not fetch this PR's head or base branch: {exc}"
        against_base = MergeCheckResult(label=base_branch, conflicted=False, error=error)
        against_other_prs = [
            MergeCheckResult(label=title, conflicted=False, error=error, pr_number=pr_number)
            for pr_number, title in other_prs
        ]
        return MergeCheckReport(against_base=against_base, against_other_prs=against_other_prs)

    try:
        against_base = check_merge(repo_path, base_ref, this_ref, label=base_branch)

        against_other_prs: list[MergeCheckResult] = []
        for pr_number, title in other_prs:
            other_ref = _namespaced_ref(this_pr_number, f"other-{pr_number}")
            try:
                fetch_ref(repo_path, remote, pull_ref(pr_number), other_ref)
                result = check_merge(repo_path, this_ref, other_ref, label=title)
                result.pr_number = pr_number
            except GitError as exc:
                result = MergeCheckResult(label=title, conflicted=False, error=str(exc), pr_number=pr_number)
            finally:
                delete_ref(repo_path, other_ref)
            against_other_prs.append(result)

        return MergeCheckReport(against_base=against_base, against_other_prs=against_other_prs)
    finally:
        delete_ref(repo_path, this_ref)
        delete_ref(repo_path, base_ref)
