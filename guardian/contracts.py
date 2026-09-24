"""Classify changed file paths into "contract" categories.

A contract file is one whose change can break something at runtime without
git ever noticing: a DB schema, a public API shape, or deployment config.
This module is pure (no I/O, no network) so it can be unit tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch

DATABASE = "database"
API = "api"
CONFIG = "config"

# Patterns are matched case-insensitively against the full repo-relative
# path (forward slashes) using shell-glob semantics via fnmatch, with "*"
# also matching across "/" (fnmatch has no path-aware "**", so we rely on
# broad patterns like "*migrations/*.sql" instead of anchoring to root).
_CONTRACT_PATTERNS: dict[str, list[str]] = {
    DATABASE: [
        "migrations/*.sql",
        "*/migrations/*.sql",
        "*alembic*/*.py",
        "alembic.ini",
        "*/alembic.ini",
        "*prisma/schema.prisma",
        "schema.prisma",
    ],
    API: [
        "*openapi*.yml",
        "*openapi*.yaml",
        "*openapi*.json",
        "*swagger*.yml",
        "*swagger*.yaml",
        "*swagger*.json",
        "*.proto",
        "*.graphql",
    ],
    CONFIG: [
        ".env.example",
        "*/.env.example",
        "docker-compose.yml",
        "docker-compose.yaml",
        "docker-compose.*.yml",
        "docker-compose.*.yaml",
        "*/docker-compose.yml",
        "*/docker-compose.yaml",
        "*/docker-compose.*.yml",
        "*/docker-compose.*.yaml",
    ],
}

_RELEASE_NOTES_PATTERNS: list[str] = [
    "changelog.md",
    "changelog.rst",
    "changelog.txt",
    "changelog",
    "release-notes/*",
    "releases/*.md",
    "*/changelog.md",
    "*/release-notes/*",
    "*/releases/*.md",
]


def _matches_any(path: str, patterns: list[str]) -> bool:
    normalized = path.strip("/").lower()
    return any(fnmatch(normalized, pattern) for pattern in patterns)


def classify_file(path: str) -> list[str]:
    """Return the contract categories (possibly empty, possibly >1) a path matches."""
    return [
        category
        for category, patterns in _CONTRACT_PATTERNS.items()
        if _matches_any(path, patterns)
    ]


def is_release_notes(path: str) -> bool:
    return _matches_any(path, _RELEASE_NOTES_PATTERNS)


@dataclass
class ContractAnalysis:
    contract_files: dict[str, list[str]] = field(default_factory=dict)
    release_notes_touched: bool = False

    @property
    def flagged(self) -> bool:
        return bool(self.contract_files) and not self.release_notes_touched


def analyze(files: list[str | dict]) -> ContractAnalysis:
    """Classify a PR's changed files and decide whether to flag it.

    Each entry in `files` is either a plain path string, or a dict
    {"filename": ..., "previous_filename": ...} as returned by
    GitHubClient.list_pr_files. For a renamed file, both the new and the
    old path are checked against the contract/release-notes patterns —
    e.g. a migration moved OUT of migrations/ to a non-matching path
    should still register as a database contract change, since something
    about the DB migration history changed even though the new filename
    alone wouldn't match. The path recorded in the report is always the
    current filename, never the stale previous one.
    """
    contract_files: dict[str, list[str]] = {}
    release_notes_touched = False

    for entry in files:
        if isinstance(entry, dict):
            filename = entry["filename"]
            previous_filename = entry.get("previous_filename")
        else:
            filename = entry
            previous_filename = None

        candidates = [filename] if not previous_filename else [filename, previous_filename]

        if any(is_release_notes(c) for c in candidates):
            release_notes_touched = True

        categories = set()
        for c in candidates:
            categories.update(classify_file(c))
        for category in categories:
            contract_files.setdefault(category, []).append(filename)

    return ContractAnalysis(
        contract_files=contract_files,
        release_notes_touched=release_notes_touched,
    )
