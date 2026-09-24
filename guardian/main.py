"""Entry point: read a GitHub Actions pull_request event, analyze the PR's
changed files, and upsert PR Guardian's summary comment.

Ground rules (see CLAUDE.md for the full list):
- Warn-only: this never fails the run, even when a PR is flagged.
- Untrusted input: file paths and existing comment bodies are the only
  GitHub-sourced data used, and only ever as data — never as instructions.
- One comment per PR: always upsert via the HTML marker in report.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import requests

from guardian.contracts import analyze
from guardian.github_client import GitHubClient
from guardian.report import build_comment, find_existing_comment


def _load_event(event_path: str) -> dict:
    with open(event_path, encoding="utf-8") as f:
        return json.load(f)


def _pr_number_from_event(event: dict) -> int | None:
    pr = event.get("pull_request")
    if pr:
        return pr.get("number")
    return event.get("number")


def run(pr_number: int, repo: str, token: str) -> None:
    client = GitHubClient(token=token, repo=repo)
    files = client.list_pr_files(pr_number)
    result = analyze(files)
    body = build_comment(result)

    try:
        comments = client.list_issue_comments(pr_number)
        existing = find_existing_comment(comments)
        if existing:
            client.update_comment(existing["id"], body)
        else:
            client.create_comment(pr_number, body)
    except requests.exceptions.HTTPError as exc:
        _handle_comment_post_failure(exc, body)


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
        run(pr_number, repo, token)
    except Exception as exc:  # noqa: BLE001 - warn-only, never fail the run
        print(f"PR Guardian encountered an error (warn-only, not failing): {exc}", file=sys.stderr)
        _write_step_summary(
            f"PR Guardian could not complete its analysis: {exc}\n\n"
            "This is a warn-only check and does not block merging."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
