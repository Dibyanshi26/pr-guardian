"""Build the Markdown PR comment and find PR Guardian's own prior comment.

The HTML comment marker lets us upsert a single comment per PR instead of
spamming a new one on every push.
"""

from __future__ import annotations

from guardian.contracts import ContractAnalysis

COMMENT_MARKER = "<!-- pr-guardian:report -->"

_CATEGORY_LABELS = {
    "database": "Database",
    "api": "API",
    "config": "Config",
}


def build_comment(analysis: ContractAnalysis) -> str:
    lines = [COMMENT_MARKER, "## PR Guardian", ""]

    if not analysis.contract_files:
        lines.append("No contract-affecting files (database, API, config) were changed.")
        return "\n".join(lines)

    lines.append("Contract-affecting files changed in this PR:")
    lines.append("")
    for category, paths in sorted(analysis.contract_files.items()):
        label = _CATEGORY_LABELS.get(category, category.title())
        lines.append(f"**{label}**")
        for path in paths:
            lines.append(f"- `{path}`")
        lines.append("")

    if analysis.flagged:
        lines.append(
            "> ⚠️ **Contract change without release notes.** This PR changes "
            "database, API, or config contracts but doesn't touch a "
            "release-notes file (e.g. `CHANGELOG.md`). Consider documenting "
            "the runtime impact so it isn't a silent surprise. This is a "
            "warning only — it does not block merging."
        )
    else:
        lines.append("✅ Release notes were also updated in this PR.")

    return "\n".join(lines)


def find_existing_comment(comments: list[dict]) -> dict | None:
    for comment in comments:
        if comment.get("body", "").startswith(COMMENT_MARKER):
            return comment
    return None
