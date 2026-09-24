"""Minimal GitHub REST API client used by PR Guardian.

Deliberately small: just enough to list a PR's changed files, read issue
comments, and create/update a comment. No retries, no caching — Phase 1
keeps this simple and adds robustness later if it proves necessary.
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
        """Return each changed file as {"filename", "previous_filename"}.

        previous_filename is included (as None when absent) so callers can
        classify renamed files by their old path too, not just their new one.
        """
        files = self._paginated_get(f"/repos/{self.repo}/pulls/{pr_number}/files")
        return [
            {"filename": f["filename"], "previous_filename": f.get("previous_filename")}
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
