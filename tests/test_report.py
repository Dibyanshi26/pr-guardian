from guardian.contracts import analyze
from guardian.report import COMMENT_MARKER, build_comment, find_existing_comment


def test_comment_starts_with_marker():
    result = analyze(["src/app.py"])
    body = build_comment(result)
    assert body.startswith(COMMENT_MARKER)


def test_flagged_comment_contains_warning():
    result = analyze(["migrations/0001_init.sql"])
    body = build_comment(result)
    assert result.flagged
    assert "Contract change without release notes" in body
    assert "migrations/0001_init.sql" in body


def test_non_flagged_comment_has_no_warning():
    result = analyze(["migrations/0001_init.sql", "CHANGELOG.md"])
    body = build_comment(result)
    assert not result.flagged
    assert "Contract change without release notes" not in body


def test_no_contract_files_comment_says_so():
    result = analyze(["src/app.py", "README.md"])
    body = build_comment(result)
    assert not result.contract_files
    assert "No contract-affecting files" in body
    assert "Contract change without release notes" not in body


def test_comment_never_uses_blocking_language():
    result = analyze(["migrations/0001_init.sql"])
    body = build_comment(result).lower()
    for blocking_word in ("blocked", "failing", "must fix", "required to merge"):
        assert blocking_word not in body


def test_find_existing_comment_returns_match():
    comments = [
        {"id": 1, "body": "just a regular comment"},
        {"id": 2, "body": f"{COMMENT_MARKER}\n## PR Guardian\nold report"},
        {"id": 3, "body": "another unrelated comment"},
    ]
    found = find_existing_comment(comments)
    assert found is not None
    assert found["id"] == 2


def test_find_existing_comment_returns_none_when_absent():
    comments = [
        {"id": 1, "body": "just a regular comment"},
        {"id": 2, "body": "another unrelated comment"},
    ]
    assert find_existing_comment(comments) is None


def test_find_existing_comment_handles_empty_list():
    assert find_existing_comment([]) is None
