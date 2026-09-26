"""Build the Markdown PR comment and Check Run summary, and find PR
Guardian's own prior comment.

The HTML comment marker lets us upsert a single comment per PR instead of
spamming a new one on every push.

Phase 4 adds a second hidden marker, AI_CACHE_MARKER, embedding the last
successful AIAnalysisResult plus the findings_fingerprint it was computed
from. A re-check (main.py's run_for_all_open_prs) reads this back via
find_cached_ai_result and passes it to ai_analysis.analyze_pr, which
skips the model entirely when the fresh fingerprint still matches --
this is what keeps a push-to-main re-check from re-spending AI calls on
every open PR whose relevant findings haven't actually changed. This
keeps the project's "GitHub is the only state store" design: no
database, just a second invisible comment inside the one Guardian
already posts. It is not a security boundary -- see CLAUDE.md.
"""

from __future__ import annotations

import json

from guardian.ai_analysis import AIAnalysisOutcome, AIAnalysisResult
from guardian.contracts import ContractAnalysis
from guardian.merge_check import MergeCheckReport, MergeCheckResult
from guardian.overlap import PROverlap

COMMENT_MARKER = "<!-- pr-guardian:report -->"
CHECK_RUN_NAME = "PR Guardian"

AI_CACHE_MARKER_PREFIX = "<!-- pr-guardian:ai-cache:"
AI_CACHE_MARKER_SUFFIX = " -->"

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


_RISK_EMOJI = {"none": "✅", "low": "🟡", "medium": "🟠", "high": "🔴"}


def _ai_section(outcome: AIAnalysisOutcome | None) -> list[str]:
    if outcome is None:
        return []

    lines = ["### AI risk analysis", ""]

    if outcome.result is None:
        reason = outcome.unavailable_reason or "unknown error"
        lines.append(f"_AI risk analysis is unavailable ({reason}). Phase 1/2 findings above still stand._")
        return lines

    result = outcome.result
    emoji = _RISK_EMOJI.get(result.risk, "")
    lines.append(f"{emoji} **Risk: {result.risk}** ({result.category})")
    lines.append("")
    lines.append(result.explanation)

    if result.evidence:
        lines.append("")
        for ev in result.evidence:
            location = f"`{ev.file}`" + (f":{ev.line}" if ev.line is not None else "")
            lines.append(f"- {location} — {ev.note}")

    lines.append("")
    lines.append(
        "_This is an automated judgment from an AI model, provided as additional "
        "context for the human reviewer. It does not change PR Guardian's "
        "warn-only behavior or its Phase 1/2 findings above._"
    )
    return lines


def _ai_cache_comment(outcome: AIAnalysisOutcome | None) -> str | None:
    """The hidden AI-cache marker line for this outcome, or None when
    there's nothing worth caching (not triggered, unavailable, or no
    fingerprint). Only a confirmed successful result is ever embedded --
    an "unavailable" outcome must never look cacheable to the next run."""
    if outcome is None or outcome.result is None or outcome.fingerprint is None:
        return None
    payload = {"fingerprint": outcome.fingerprint, "result": outcome.result.model_dump()}
    return f"{AI_CACHE_MARKER_PREFIX}{json.dumps(payload, separators=(',', ':'))}{AI_CACHE_MARKER_SUFFIX}"


def find_cached_ai_result(comment: dict) -> tuple[str, AIAnalysisResult] | None:
    """Extract a (fingerprint, AIAnalysisResult) pair embedded by a prior
    run, for analyze_pr's `cached` parameter. Fails safe: any parse error
    or shape mismatch (missing marker, malformed JSON, a hand-edited
    comment) returns None rather than raising -- the caller then just
    calls the model fresh, which is always the safe direction to fail
    in."""
    body = comment.get("body", "")
    start = body.find(AI_CACHE_MARKER_PREFIX)
    if start == -1:
        return None
    start += len(AI_CACHE_MARKER_PREFIX)
    end = body.find(AI_CACHE_MARKER_SUFFIX, start)
    if end == -1:
        return None
    try:
        payload = json.loads(body[start:end])
        result = AIAnalysisResult(**payload["result"])
        return payload["fingerprint"], result
    except Exception:  # noqa: BLE001 - any malformed/tampered cache just means "no cache"
        return None


def build_comment(
    analysis: ContractAnalysis,
    merge_report: MergeCheckReport | None = None,
    overlaps: list[PROverlap] | None = None,
    ai_outcome: AIAnalysisOutcome | None = None,
) -> str:
    lines = [COMMENT_MARKER]
    cache_line = _ai_cache_comment(ai_outcome)
    if cache_line:
        lines.append(cache_line)
    lines.append("## PR Guardian")
    lines.append("")
    lines.extend(_contract_section(analysis))

    merge_lines = _merge_section(merge_report)
    if merge_lines:
        lines.append("")
        lines.extend(merge_lines)

    overlap_lines = _overlap_section(overlaps)
    if overlap_lines:
        lines.append("")
        lines.extend(overlap_lines)

    ai_lines = _ai_section(ai_outcome)
    if ai_lines:
        lines.append("")
        lines.extend(ai_lines)

    return "\n".join(lines)


def build_check_run_summary(
    analysis: ContractAnalysis,
    merge_report: MergeCheckReport | None = None,
    overlaps: list[PROverlap] | None = None,
    ai_outcome: AIAnalysisOutcome | None = None,
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
    if ai_outcome is not None and ai_outcome.result is not None:
        flags.append(f"AI risk: {ai_outcome.result.risk}")

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

    ai_lines = _ai_section(ai_outcome)
    if ai_lines:
        lines.append("")
        lines.extend(ai_lines)

    return title, "\n".join(lines)


def find_existing_comment(comments: list[dict]) -> dict | None:
    for comment in comments:
        if comment.get("body", "").startswith(COMMENT_MARKER):
            return comment
    return None
