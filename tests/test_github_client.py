"""Tests for GitHubClient. No real network calls: requests.Session.get/post
are monkeypatched to return in-memory requests.Response objects."""

import json
from unittest.mock import Mock

import requests

from guardian.github_client import GitHubClient


def _make_response(json_data, status_code=200, next_url=None):
    response = requests.Response()
    response.status_code = status_code
    response._content = json.dumps(json_data).encode("utf-8")
    if next_url:
        response.headers["Link"] = f'<{next_url}>; rel="next"'
    return response


def _client() -> GitHubClient:
    return GitHubClient(token="fake-token", repo="acme/widgets")


# --- Pagination: a single page of results ---

def test_list_pr_files_single_page(monkeypatch):
    client = _client()
    files = [{"filename": "src/app.py"}]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_make_response(files)))

    result = client.list_pr_files(42)

    assert result == [{"filename": "src/app.py", "previous_filename": None}]


# --- Pagination: multiple pages via the Link header ---

def test_list_pr_files_follows_link_header_across_pages(monkeypatch):
    client = _client()
    # A PR with 130 changed files: GitHub paginates at 100 per page.
    page1_files = [{"filename": f"file_{i}.py"} for i in range(100)]
    page2_files = [{"filename": f"file_{i}.py"} for i in range(100, 130)]

    page1_url = "https://api.github.com/repos/acme/widgets/pulls/1/files"
    page2_url = f"{page1_url}?page=2"

    page1_response = _make_response(page1_files, next_url=page2_url)
    page2_response = _make_response(page2_files)  # no Link header -> last page

    mock_get = Mock(side_effect=[page1_response, page2_response])
    monkeypatch.setattr(client.session, "get", mock_get)

    result = client.list_pr_files(1)

    assert len(result) == 130
    assert result[0]["filename"] == "file_0.py"
    assert result[-1]["filename"] == "file_129.py"
    assert mock_get.call_count == 2
    # The second call must hit the "next" URL from the Link header, not the
    # first page's URL again (that would be an infinite loop or missed data).
    assert mock_get.call_args_list[1].args[0] == page2_url


def test_list_issue_comments_follows_link_header_across_pages(monkeypatch):
    client = _client()
    # A PR with 45 existing comments across two pages of 30 (default GitHub
    # per-page cap for comments in some contexts) — exercise pagination
    # generically regardless of the exact page size GitHub happens to use.
    page1_comments = [{"id": i, "body": f"comment {i}"} for i in range(30)]
    page2_comments = [{"id": i, "body": f"comment {i}"} for i in range(30, 45)]

    page1_url = "https://api.github.com/repos/acme/widgets/issues/1/comments"
    page2_url = f"{page1_url}?page=2"

    mock_get = Mock(
        side_effect=[
            _make_response(page1_comments, next_url=page2_url),
            _make_response(page2_comments),
        ]
    )
    monkeypatch.setattr(client.session, "get", mock_get)

    result = client.list_issue_comments(1)

    assert len(result) == 45
    assert {c["id"] for c in result} == set(range(45))


def test_pagination_stops_when_no_link_header(monkeypatch):
    client = _client()
    mock_get = Mock(return_value=_make_response([{"id": 1, "body": "only comment"}]))
    monkeypatch.setattr(client.session, "get", mock_get)

    result = client.list_issue_comments(1)

    assert len(result) == 1
    assert mock_get.call_count == 1


# --- Renamed files: previous_filename must survive the API call ---

def test_list_pr_files_includes_previous_filename_for_renames(monkeypatch):
    client = _client()
    files = [
        {"filename": "archive/0001_init.sql", "previous_filename": "migrations/0001_init.sql"},
        {"filename": "src/app.py"},
    ]
    monkeypatch.setattr(client.session, "get", Mock(return_value=_make_response(files)))

    result = client.list_pr_files(1)

    assert result == [
        {"filename": "archive/0001_init.sql", "previous_filename": "migrations/0001_init.sql"},
        {"filename": "src/app.py", "previous_filename": None},
    ]


# --- create/update comment ---

def test_create_comment_posts_body(monkeypatch):
    client = _client()
    mock_post = Mock(return_value=_make_response({"id": 99, "body": "hi"}))
    monkeypatch.setattr(client.session, "post", mock_post)

    result = client.create_comment(1, "hi")

    assert result["id"] == 99
    assert mock_post.call_args.kwargs["json"] == {"body": "hi"}


def test_create_comment_raises_on_http_error(monkeypatch):
    client = _client()
    forbidden = _make_response({"message": "Forbidden"}, status_code=403)
    monkeypatch.setattr(client.session, "post", Mock(return_value=forbidden))

    try:
        client.create_comment(1, "hi")
        assert False, "expected HTTPError"
    except requests.exceptions.HTTPError as exc:
        assert exc.response.status_code == 403
