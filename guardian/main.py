"""Entry point: read a GitHub Actions pull_request event, analyze the PR's
changed files, check it for merge conflicts and file overlap against
other open PRs, optionally ask an AI model to judge the risk behind
whatever got flagged, and upsert PR Guardian's summary comment and check
run.

Ground rules (see CLAUDE.md for the full list):
- Warn-only: this never fails the run, even when a PR is flagged. The
  check run conclusion is always "neutral", never "failure".
- Untrusted input: file paths, diff content, and existing comment bodies
  are the only GitHub-sourced data used, and only ever as data — never as
  instructions. Symmetrically, the model's JSON output is treated as data
  by this module too: no field of it is ever branched on.
- One comment per PR: always upsert via the HTML marker in report.py.
- Phase 2 (merge conflicts / overlap) is best-effort on top of Phase 1:
  if listing open PRs or the git operations in merge_check.py fail
  outright, the contract-file comment from Phase 1 still gets posted.
- Phase 3 (AI risk analysis) only runs when Phase 1/2 found something to
  investigate, and is itself best-effort on top of Phase 1/2 -- see
  ai_analysis.analyze_pr, whose contract is that it never raises.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import requests

from guardian.ai_analysis import analyze_pr
from guardian.contracts import analyze
from guardian.github_client import GitHubClient
from guardian.merge_check import run_merge_checks
from guardian.overlap import find_overlaps
from guardian.report import CHECK_RUN_NAME, build_check_run_summary, build_comment, find_existing_comment


def _load_event(event_path: str) -> dict:
    with open(event_path, encoding="utf-8") as f:
        return json.load(f)


def _pr_number_from_event(event: dict) -> int | None:
    pr = event.get("pull_request")
    if pr:
        return pr.get("number")
    return event.get("number")


def _base_ref_from_event(event: dict) -> str | None:
    pr = event.get("pull_request")
    if not pr:
        return None
    return (pr.get("base") or {}).get("ref")


def _head_sha_from_event(event: dict) -> str | None:
    pr = event.get("pull_request")
    if not pr:
        return None
    return (pr.get("head") or {}).get("sha")


def run(
    pr_number: int,
    repo: str,
    token: str,
    *,
    repo_path: str = ".",
    base_ref: str | None = None,
    head_sha: str | None = None,
    openai_api_key: str | None = None,
) -> None:
    client = GitHubClient(token=token, repo=repo)
    files = client.list_pr_files(pr_number)
    result = analyze(files)

    merge_report, overlaps = _run_phase2_checks(client, pr_number, files, repo_path, base_ref)

    ai_outcome = analyze_pr(openai_api_key, result, merge_report, overlaps, files)

    body = build_comment(result, merge_report=merge_report, overlaps=overlaps, ai_outcome=ai_outcome)

    try:
        comments = client.list_issue_comments(pr_number)
        existing = find_existing_comment(comments)
        if existing:
            client.update_comment(existing["id"], body)
        else:
            client.create_comment(pr_number, body)
    except requests.exceptions.HTTPError as exc:
        _handle_comment_post_failure(exc, body)

    if head_sha:
        _publish_check_run(client, head_sha, result, merge_report, overlaps, ai_outcome)


def _run_phase2_checks(client, pr_number, files, repo_path, base_ref):
    """Merge-conflict and file-overlap checks against main and other open
    PRs. Best-effort: Phase 1's contract-file comment must still get
    posted even if listing open PRs or the underlying git operations
    fail outright, so any unexpected failure here is logged and swallowed
    rather than propagated -- this is on top of the per-comparison
    degrading run_merge_checks already does for one bad PR or ref."""
    if not base_ref:
        return None, None

    try:
        other_prs = [p for p in client.list_open_prs() if p["number"] != pr_number]
        other_pr_files = [(p["number"], p["title"], client.list_pr_files(p["number"])) for p in other_prs]
        overlaps = find_overlaps(files, other_pr_files)

        merge_report = run_merge_checks(
            repo_path=repo_path,
            remote="origin",
            this_pr_number=pr_number,
            base_branch=base_ref,
            other_prs=[(p["number"], p["title"]) for p in other_prs],
        )
        return merge_report, overlaps
    except Exception as exc:  # noqa: BLE001 - Phase 2 is best-effort, never blocks Phase 1
        print(
            f"PR Guardian: Phase 2 checks (merge conflicts / overlap) failed "
            f"({exc}); posting the contract-file report only.",
            file=sys.stderr,
        )
        return None, None


def _publish_check_run(client, head_sha: str, result, merge_report, overlaps, ai_outcome) -> None:
    title, summary = build_check_run_summary(
        result, merge_report=merge_report, overlaps=overlaps, ai_outcome=ai_outcome
    )
    try:
        existing = client.find_check_run(head_sha, CHECK_RUN_NAME)
        if existing:
            client.update_check_run(existing["id"], title, summary, conclusion="neutral")
        else:
            client.create_check_run(head_sha, CHECK_RUN_NAME, title, summary, conclusion="neutral")
    except requests.exceptions.HTTPError as exc:
        # Never fail the run over the check run alone -- the PR comment above
        # already carries the same information.
        print(f"PR Guardian: could not publish check run ({exc}).", file=sys.stderr)


def _handle_comment_post_failure(exc: requests.exceptions.HTTPError, body: str) -> None:
    """Posting a comment can fail with 403 on PRs from forks, which get a
    read-only GITHUB_TOKEN with no permission to write comments. That's an
    expected, not-our-bug situation for a warn-only tool — log it plainly
    and fall back to the job summary so the analysis isn't just lost."""
    status = exc.response.status_code if exc.response is not None else None
    if status == 403:
        print(
            "PR Guardian: got 403 Forbidden posting a comment. This is expected "
            "for pull requests from forks, which run with a read-only token. "
            "Writing the report to the job summary instead.",
            file=sys.stderr,
        )
    else:
        print(
            f"PR Guardian: could not post a comment ({exc}). Writing the report "
            "to the job summary instead.",
            file=sys.stderr,
        )
    _write_step_summary(body)


def _write_step_summary(text: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError as exc:  # best-effort only — never fail the run over this
        print(f"PR Guardian: could not write GITHUB_STEP_SUMMARY: {exc}", file=sys.stderr)


def run_dry_run(files: list[str]) -> None:
    result = analyze(files)
    print(build_comment(result))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PR Guardian")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the report to stdout instead of calling the GitHub API. No token needed.",
    )
    parser.add_argument(
        "--files",
        nargs="*",
        default=None,
        help="Changed file paths to analyze in --dry-run mode (overrides reading the event JSON).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    event_path = os.environ.get("GITHUB_EVENT_PATH")

    if args.dry_run:
        run_dry_run(args.files or [])
        return 0

    if not event_path:
        print("GITHUB_EVENT_PATH is not set; nothing to do.", file=sys.stderr)
        return 0

    event = _load_event(event_path)
    pr_number = _pr_number_from_event(event)
    if pr_number is None:
        print("No pull_request number found in event payload; skipping.", file=sys.stderr)
        return 0

    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        print("GITHUB_REPOSITORY/GITHUB_TOKEN not set; skipping.", file=sys.stderr)
        return 0

    try:
        run(
            pr_number,
            repo,
            token,
            base_ref=_base_ref_from_event(event),
            head_sha=_head_sha_from_event(event),
            openai_api_key=os.environ.get("OPENAI_API_KEY"),
        )
    except Exception as exc:  # noqa: BLE001 - warn-only, never fail the run
        print(f"PR Guardian encountered an error (warn-only, not failing): {exc}", file=sys.stderr)
        _write_step_summary(
            f"PR Guardian could not complete its analysis: {exc}\n\n"
            "This is a warn-only check and does not block merging."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
