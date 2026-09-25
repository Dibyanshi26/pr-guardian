"""Minimal GitHub REST API client used by PR Guardian.

Deliberately small: list a PR's changed files, read/create/update issue
comments, list open PRs, and create/update a check run. No retries, no
caching — Phase 1 keeps this simple and adds robustness later if it
proves necessary; Phase 2 hasn't needed to change that.
"""

from __future__ import annotations

import requests

API_BASE = "https://api.github.com"


class GitHubClient:
    def __init__(self, token: str, repo: str, base_url: str = API_BASE):
        self.repo = repo
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def _paginated_get(self, path: str) -> list[dict]:
        results: list[dict] = []
        url = f"{self.base_url}{path}"
        params = {"per_page": 100}
        while url:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            results.extend(response.json())
            url = response.links.get("next", {}).get("url")
            params = None  # the "next" link already carries query params
        return results

    def list_pr_files(self, pr_number: int) -> list[dict]:
        """Return each changed file as {"filename", "previous_filename", "patch"}.

        previous_filename is included (as None when absent) so callers can
        classify renamed files by their old path too, not just their new
        one. patch is the file's unified-diff hunk (None when GitHub omits
        it -- binary files, or files too large) -- Phase 3's AI risk
        analysis uses it to scope prompt content to just the flagged files
        instead of the whole PR diff.
        """
        files = self._paginated_get(f"/repos/{self.repo}/pulls/{pr_number}/files")
        return [
            {
                "filename": f["filename"],
                "previous_filename": f.get("previous_filename"),
                "patch": f.get("patch"),
            }
            for f in files
        ]

    def list_issue_comments(self, pr_number: int) -> list[dict]:
        return self._paginated_get(f"/repos/{self.repo}/issues/{pr_number}/comments")

    def create_comment(self, pr_number: int, body: str) -> dict:
        url = f"{self.base_url}/repos/{self.repo}/issues/{pr_number}/comments"
        response = self.session.post(url, json={"body": body})
        response.raise_for_status()
        return response.json()

    def update_comment(self, comment_id: int, body: str) -> dict:
        url = f"{self.base_url}/repos/{self.repo}/issues/comments/{comment_id}"
        response = self.session.patch(url, json={"body": body})
        response.raise_for_status()
        return response.json()

    def list_open_prs(self) -> list[dict]:
        """Return each open PR as {"number", "title", "head_sha", "head_ref"}."""
        prs = self._paginated_get(f"/repos/{self.repo}/pulls?state=open")
        return [
            {
                "number": pr["number"],
                "title": pr["title"],
                "head_sha": pr["head"]["sha"],
                "head_ref": pr["head"]["ref"],
            }
            for pr in prs
        ]

    def find_check_run(self, head_sha: str, name: str) -> dict | None:
        """Look up an existing check run by name for head_sha, so a rerun
        of the same commit updates it instead of creating a duplicate.

        Uses its own pagination loop rather than _paginated_get: this
        endpoint wraps results in {"check_runs": [...]} instead of
        returning a bare list.
        """
        url = f"{self.base_url}/repos/{self.repo}/commits/{head_sha}/check-runs"
        params = {"check_name": name, "per_page": 100}
        while url:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            for run in response.json().get("check_runs", []):
                if run.get("name") == name:
                    return run
            url = response.links.get("next", {}).get("url")
            params = None
        return None

    def create_check_run(self, head_sha: str, name: str, title: str, summary: str, conclusion: str) -> dict:
        url = f"{self.base_url}/repos/{self.repo}/check-runs"
        payload = {
            "name": name,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": title, "summary": summary},
        }
        response = self.session.post(url, json=payload)
        response.raise_for_status()
        return response.json()

    def update_check_run(self, check_run_id: int, title: str, summary: str, conclusion: str) -> dict:
        url = f"{self.base_url}/repos/{self.repo}/check-runs/{check_run_id}"
        payload = {
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": title, "summary": summary},
        }
        response = self.session.patch(url, json=payload)
        response.raise_for_status()
        return response.json()
