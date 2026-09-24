"""Build the Markdown PR comment and Check Run summary, and find PR
Guardian's own prior comment.

The HTML comment marker lets us upsert a single comment per PR instead of
spamming a new one on every push.
"""

from __future__ import annotations

from guardian.contracts import ContractAnalysis
from guardian.merge_check import MergeCheckReport, MergeCheckResult
from guardian.overlap import PROverlap

COMMENT_MARKER = "<!-- pr-guardian:report -->"
CHECK_RUN_NAME = "PR Guardian"

_CATEGORY_LABELS = {
    "database": "Database",
    "api": "API",
    "config": "Config",
}


def _contract_section(analysis: ContractAnalysis) -> list[str]:
    lines: list[str] = []

    if not analysis.contract_files:
        lines.append("No contract-affecting files (database, API, config) were changed.")
        return lines

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

    return lines


def _merge_result_line(result: MergeCheckResult) -> str:
    target = f"PR #{result.pr_number}" if result.pr_number is not None else f"`{result.label}`"
    if result.error:
        return f"- {target}: could not check ({result.error})"
    if result.conflicted:
        files = ", ".join(f"`{f}`" for f in result.conflicting_files)
        return f"- {target}: conflicts in {files}"
    return f"- {target}: no conflicts"


def _merge_section(merge_report: MergeCheckReport | None) -> list[str]:
    if merge_report is None:
        return []

    all_results = [merge_report.against_base, *merge_report.against_other_prs]
    any_conflicted = any(r.conflicted for r in all_results)
    any_error = any(r.error for r in all_results)

    lines = ["### Merge conflicts", ""]

    if not any_conflicted and not any_error:
        lines.append("✅ No merge conflicts detected against `main` or other open PRs.")
        return lines

    if any_conflicted:
        lines.append(
            "> ⚠️ **Merge conflicts detected.** This PR would not merge "
            "cleanly. This is a warning only — it does not block merging."
        )
        lines.append("")

    for result in all_results:
        lines.append(_merge_result_line(result))

    return lines


def _overlap_section(overlaps: list[PROverlap] | None) -> list[str]:
    if overlaps is None:
        return []

    lines = ["### Overlapping PRs", ""]

    if not overlaps:
        lines.append("✅ No other open PR touches the same files.")
        return lines

    lines.append(
        "> ⚠️ **Files also touched by other open PRs.** Git may not flag a "
        "textual conflict here (e.g. both PRs could add unrelated code to "
        "the same file), but a silent overlap like this is worth a second "
        "look. This is a warning only — it does not block merging."
    )
    lines.append("")
    for overlap in overlaps:
        files = ", ".join(f"`{f}`" for f in overlap.shared_files)
        lines.append(f"- PR #{overlap.pr_number} ({overlap.title}): {files}")

    return lines


def build_comment(
    analysis: ContractAnalysis,
    merge_report: MergeCheckReport | None = None,
    overlaps: list[PROverlap] | None = None,
) -> str:
    lines = [COMMENT_MARKER, "## PR Guardian", ""]
    lines.extend(_contract_section(analysis))

    merge_lines = _merge_section(merge_report)
    if merge_lines:
        lines.append("")
        lines.extend(merge_lines)

    overlap_lines = _overlap_section(overlaps)
    if overlap_lines:
        lines.append("")
        lines.extend(overlap_lines)

    return "\n".join(lines)


def build_check_run_summary(
    analysis: ContractAnalysis,
    merge_report: MergeCheckReport | None = None,
    overlaps: list[PROverlap] | None = None,
) -> tuple[str, str]:
    """Return (title, summary_markdown) for the Check Run output field."""
    flags = []
    if analysis.flagged:
        flags.append("contract change without release notes")
    if merge_report is not None:
        all_results = [merge_report.against_base, *merge_report.against_other_prs]
        if any(r.conflicted for r in all_results):
            flags.append("merge conflicts detected")
    if overlaps:
        flags.append(f"{len(overlaps)} overlapping PR{'s' if len(overlaps) != 1 else ''}")

    title = "; ".join(flags) if flags else "No issues detected"

    lines = ["## PR Guardian", ""]
    lines.extend(_contract_section(analysis))

    merge_lines = _merge_section(merge_report)
    if merge_lines:
        lines.append("")
        lines.extend(merge_lines)

    overlap_lines = _overlap_section(overlaps)
    if overlap_lines:
        lines.append("")
        lines.extend(overlap_lines)

    return title, "\n".join(lines)


def find_existing_comment(comments: list[dict]) -> dict | None:
    for comment in comments:
        if comment.get("body", "").startswith(COMMENT_MARKER):
            return comment
    return None
