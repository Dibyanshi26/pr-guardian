"""Tests for main.py's error handling: fork PRs (403 on comment posting)
must degrade to writing the job summary, not crash the job; other API
failures must also be warn-only. No real GitHub client is used — a fake
in-memory client stands in via monkeypatching."""

import json

import pytest
import requests

from guardian import main as main_module


def _http_error(status_code: int) -> requests.exceptions.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    return requests.exceptions.HTTPError(f"{status_code} error", response=response)


class FakeClient:
    def __init__(
        self,
        files=None,
        comments=None,
        list_files_error=None,
        create_error=None,
        update_error=None,
    ):
        self._files = files or []
        self._comments = comments or []
        self._list_files_error = list_files_error
        self._create_error = create_error
        self._update_error = update_error
        self.created = []
        self.updated = []

    def list_pr_files(self, pr_number):
        if self._list_files_error:
            raise self._list_files_error
        return self._files

    def list_issue_comments(self, pr_number):
        return self._comments

    def create_comment(self, pr_number, body):
        if self._create_error:
            raise self._create_error
        self.created.append(body)

    def update_comment(self, comment_id, body):
        if self._update_error:
            raise self._update_error
        self.updated.append(body)


def _patch_client(monkeypatch, fake_client):
    monkeypatch.setattr(main_module, "GitHubClient", lambda token, repo: fake_client)


# --- Fork PRs: 403 on create_comment must degrade gracefully ---

def test_fork_pr_403_does_not_raise(monkeypatch):
    fake_client = FakeClient(files=["migrations/0001.sql"], create_error=_http_error(403))
    _patch_client(monkeypatch, fake_client)

    main_module.run(pr_number=1, repo="acme/widgets", token="x")  # must not raise


def test_fork_pr_403_writes_report_to_step_summary(monkeypatch, tmp_path):
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    fake_client = FakeClient(files=["migrations/0001.sql"], create_error=_http_error(403))
    _patch_client(monkeypatch, fake_client)

    main_module.run(pr_number=1, repo="acme/widgets", token="x")

    content = summary_file.read_text()
    assert "migrations/0001.sql" in content
    assert "Contract change without release notes" in content


def test_fork_pr_403_logs_a_clear_warning_not_a_raw_traceback(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    fake_client = FakeClient(files=["migrations/0001.sql"], create_error=_http_error(403))
    _patch_client(monkeypatch, fake_client)

    main_module.run(pr_number=1, repo="acme/widgets", token="x")

    err = capsys.readouterr().err
    assert "fork" in err.lower()


def test_full_main_exits_zero_on_fork_pr_403(monkeypatch, tmp_path):
    event = {"pull_request": {"number": 7}}
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))

    fake_client = FakeClient(files=["migrations/0001.sql"], create_error=_http_error(403))
    _patch_client(monkeypatch, fake_client)

    exit_code = main_module.main([])

    assert exit_code == 0


# --- General API failures: also warn-only, also surfaced in the summary ---

def test_general_api_failure_during_list_files_does_not_raise(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    event = {"pull_request": {"number": 3}}
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
    monkeypatch.setenv("GITHUB_TOKEN", "x")

    fake_client = FakeClient(list_files_error=requests.exceptions.ConnectionError("network blip"))
    _patch_client(monkeypatch, fake_client)

    exit_code = main_module.main([])

    assert exit_code == 0


def test_general_api_failure_is_noted_in_step_summary(monkeypatch, tmp_path):
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    event = {"pull_request": {"number": 3}}
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
    monkeypatch.setenv("GITHUB_TOKEN", "x")

    fake_client = FakeClient(list_files_error=requests.exceptions.ConnectionError("network blip"))
    _patch_client(monkeypatch, fake_client)

    main_module.main([])

    content = summary_file.read_text()
    assert "could not complete its analysis" in content
    assert "does not block merging" in content


def test_general_api_failure_does_not_crash_without_step_summary_env(monkeypatch, tmp_path):
    # GITHUB_STEP_SUMMARY isn't guaranteed to be set; _write_step_summary
    # must no-op rather than raise when it's absent.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    event = {"pull_request": {"number": 3}}
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
    monkeypatch.setenv("GITHUB_TOKEN", "x")

    fake_client = FakeClient(list_files_error=requests.exceptions.ConnectionError("network blip"))
    _patch_client(monkeypatch, fake_client)

    exit_code = main_module.main([])

    assert exit_code == 0


# --- Sequential upsert correctness (the non-racy, common case) ---

def test_second_run_updates_existing_comment_instead_of_creating_a_new_one(monkeypatch):
    from guardian.report import COMMENT_MARKER

    fake_client = FakeClient(files=["migrations/0001.sql"])
    _patch_client(monkeypatch, fake_client)

    main_module.run(pr_number=1, repo="acme/widgets", token="x")
    assert len(fake_client.created) == 1
    assert len(fake_client.updated) == 0

    # Simulate the comment now existing when the next run (e.g. next push)
    # lists comments, the way GitHub actually would report it back.
    fake_client._comments = [{"id": 55, "body": fake_client.created[0]}]
    assert fake_client._comments[0]["body"].startswith(COMMENT_MARKER)

    main_module.run(pr_number=1, repo="acme/widgets", token="x")
    assert len(fake_client.created) == 1  # unchanged
    assert len(fake_client.updated) == 1
