"""Detect when this PR touches the same files as another open PR.

This is a distinct signal from a merge conflict: two PRs can touch the
same file without git ever flagging a textual conflict (e.g. both append
unrelated code to different parts of the same file), and that's exactly
the kind of silent overlap this module exists to catch. Pure, no I/O —
mirrors contracts.py, including its rename handling: a file's old and new
path both count as identities it "touches", but overlap is always
reported using the current filename, never the stale one.
"""

from __future__ import annotations

from dataclasses import dataclass


def _identity_map(files: list[str | dict]) -> dict[str, str]:
    """Map every path identity (current + old name for renames) a PR
    touches to that file's current filename."""
    mapping: dict[str, str] = {}
    for entry in files:
        if isinstance(entry, dict):
            filename = entry["filename"]
            previous_filename = entry.get("previous_filename")
        else:
            filename = entry
            previous_filename = None

        mapping[filename] = filename
        if previous_filename:
            mapping[previous_filename] = filename
    return mapping


@dataclass
class PROverlap:
    pr_number: int
    title: str
    shared_files: list[str]


def find_overlaps(
    this_files: list[str | dict],
    other_prs: list[tuple[int, str, list[str | dict]]],
) -> list[PROverlap]:
    """Find open PRs that touch at least one file this PR also touches.

    other_prs is an iterable of (pr_number, title, files) for other open
    PRs, using the same file-entry shape as `files` (plain paths or
    {"filename", "previous_filename"} dicts). Returned overlaps are
    sorted by pr_number, and each one's shared_files are sorted and shown
    using this PR's current filenames.
    """
    this_map = _identity_map(this_files)
    this_identities = set(this_map)

    overlaps: list[PROverlap] = []
    for pr_number, title, other_files in other_prs:
        other_identities = set(_identity_map(other_files))
        shared_identities = this_identities & other_identities
        if not shared_identities:
            continue
        shared_files = sorted({this_map[identity] for identity in shared_identities})
        overlaps.append(PROverlap(pr_number=pr_number, title=title, shared_files=shared_files))

    return sorted(overlaps, key=lambda o: o.pr_number)
