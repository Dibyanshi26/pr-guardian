# PR Guardian

A GitHub bot that catches silent failures in pull requests — changes that
merge cleanly in git but break things at runtime. Example: a DB migration
that isn't mentioned in release notes, so nobody realizes production needs
a manual `migrate` step before the next deploy.

## Architecture (Phase 1 + 2 + 3)

Data flow: GitHub Actions `pull_request` event → `guardian.main` runs
`guardian.contracts.analyze` (Phase 1), then Phase 2's
`guardian.overlap.find_overlaps` and `guardian.merge_check.run_merge_checks`,
then — only if Phase 1 or 2 found something —
`guardian.claude_analysis.analyze_pr` (Phase 3) judges the real risk behind
those findings. `guardian.report.build_comment` / `build_check_run_summary`
render the combined result, and `guardian.github_client.GitHubClient`
upserts one PR comment and one check run.

- **`guardian/contracts.py`** — pure classification logic, no I/O. Matches
  changed file paths against glob patterns for three contract categories
  (`database`, `api`, `config`) and against release-notes file patterns.
  `analyze(files)` returns a `ContractAnalysis` with a `flagged` property:
  true when contract files changed but no release-notes file did. `files`
  accepts plain path strings or `{"filename", "previous_filename"}` dicts;
  for a renamed file, both the new and old path are checked (a migration
  moved out of `migrations/` should still register as a DB change even
  though the new filename alone wouldn't match) — the report always shows
  the current filename, never the stale one. This is the module to extend
  when adding new contract types or file patterns.
- **`guardian/overlap.py`** — pure, no I/O, mirrors `contracts.py`'s rename
  handling. `find_overlaps(this_files, other_prs)` intersects the set of
  paths this PR touches (old + new path for renames) against each other
  open PR's touched paths, reporting matches using this PR's current
  filename. Exists because `git merge-tree` alone misses a real class of
  silent conflict: two PRs editing different parts of the same file merge
  cleanly in git but can still be a meaningful collision worth a second
  look (verified with a real-git-repo test fixture in
  `tests/test_merge_check.py`).
- **`guardian/merge_check.py`** — `subprocess`-based wrapper around `git
  fetch` and `git merge-tree --write-tree`, used to detect real merge
  conflicts against the base branch and against each other open PR.
  Every function takes an explicit `repo_path` and ref names (no env vars,
  no GitHub calls), so it's tested against real git repos built with `git
  init` in a tmp dir rather than mocked subprocess calls. Fetches always
  use GitHub's numeric PR ref (`refs/pull/<N>/head`, which exists for fork
  PRs too) into a ref namespaced by **both** PR numbers —
  `refs/guardian/<this-pr>/...` — never just the other PR's number, because
  the workflow's `concurrency` group only serializes runs for the *same*
  PR; two different PRs' Guardian runs can and do execute in parallel, and
  a ref keyed only by the other PR's number would let them race to
  fetch/delete the same ref. `run_merge_checks` cleans up every ref it
  creates and degrades one bad comparison (deleted branch, no shared
  history) to an error on that result instead of raising, so one bad PR
  can't take down the whole check.
- **`guardian/claude_analysis.py`** — Phase 3: a judgment layer on top of
  Phase 1/2's findings, never a replacement for them. `should_analyze`
  gates the whole module on "did Phase 1 or 2 find something" — unflagged
  PRs never call the API. `trigger_filenames` narrows to just the files
  that caused a flag, and `build_diff_context` sends only those files'
  diff hunks (from `GitHubClient.list_pr_files`'s `patch` field), truncated
  per-file and capped in total (`MAX_DIFF_CHARS_PER_FILE` /
  `MAX_TOTAL_DIFF_CHARS`) — never the whole PR diff. `call_claude` asks
  `claude-sonnet-5` for structured JSON output (`output_format=
  ClaudeAnalysisResult`, a Pydantic model — schema below), retrying once
  on a failed/invalid attempt before giving up. `analyze_pr` is the single
  entry point `main.py` depends on; its contract is that it never raises —
  any failure anywhere degrades to a `ClaudeAnalysisOutcome` with an
  `unavailable_reason` (missing `ANTHROPIC_API_KEY`, exhausted retries, or
  an unexpected error) instead of taking down the run. `SYSTEM_PROMPT`
  explicitly frames diff content as untrusted data, never instructions —
  see Ground rules for how that's also enforced in code, not just prompt
  wording.

  JSON schema (`ClaudeAnalysisResult`):
  ```json
  {
    "risk": "none | low | medium | high",
    "category": "database | api | config | merge_conflict | overlap | other",
    "explanation": "string",
    "evidence": [{"file": "string", "line": "int | null", "note": "string"}]
  }
  ```
  `risk`, `category`, and `explanation` are required; `evidence` defaults
  to `[]` in the Pydantic model, so it's optional in the schema sent to
  the API too.
- **`guardian/github_client.py`** — thin `requests`-based wrapper around
  the GitHub REST API: list a PR's changed files (with pagination via the
  `Link` response header — needed once a PR has 30+ files or comments),
  list issue comments, create/update a comment, list open PRs, and
  find/create/update a check run. `list_pr_files` returns
  `{"filename", "previous_filename", "patch"}` per entry so renames
  survive into `contracts.analyze` and `overlap.find_overlaps`, and diff
  hunks (`patch`, `None` when GitHub omits it — binary/huge files) feed
  Phase 3 without a separate API call. Check-run lookup uses its own
  pagination loop rather than the shared `_paginated_get` helper, since
  that endpoint wraps results in `{"check_runs": [...]}` instead of
  returning a bare list. No retries or caching by design — Phase 1 keeps
  this minimal; only add complexity here when a real failure mode shows up.
- **`guardian/report.py`** — builds the Markdown comment body (Phase 1's
  contract section, followed by Phase 2's "Merge conflicts" and
  "Overlapping PRs" sections and Phase 3's "Claude's analysis" section,
  each shown whenever that data was gathered — earlier sections are never
  altered by a later phase's presence) and the Check Run's `(title,
  summary)` output via `build_check_run_summary`. The Claude section
  always renders a fixed advisory disclaimer alongside the risk/
  explanation, so a human reading the PR sees the "this doesn't change
  warn-only behavior" framing directly, not just in this doc. Finds PR
  Guardian's own previous comment via `COMMENT_MARKER`, a hidden HTML
  comment (`<!-- pr-guardian:report -->`) that's always the first line of
  anything we post.
- **`guardian/main.py`** — entry point. Reads the event JSON
  (`GITHUB_EVENT_PATH`), extracts the PR number plus (for Phase 2) the base
  branch and head SHA and (for Phase 3) `ANTHROPIC_API_KEY`, runs Phase 1's
  analysis, then Phase 2's merge/overlap checks, then Phase 3's
  `analyze_pr`, and upserts the comment and check run. Phase 2 is
  best-effort on top of Phase 1: if listing open PRs or the git operations
  in `merge_check.py` fail outright, Phase 1's contract-file comment still
  gets posted (`_run_phase2_checks` swallows and logs the failure). Phase 3
  is best-effort by construction (`analyze_pr` never raises — see above).
  The check run's `conclusion` is always passed as the literal `"neutral"`
  at the call site in `main.py`, never threaded through a variable that
  could become `"failure"` — a regression test in `test_main.py` guards
  this, and it holds regardless of what Phase 3 returns (also tested: a
  `ClaudeAnalysisResult` whose `explanation` contains injected-looking
  text doesn't change the conclusion or suppress Phase 1/2's findings).
  `--dry-run [--files a b c]` skips the GitHub API, Phase 2, and Phase 3
  entirely and prints the Phase 1 report to stdout — the primary way to
  iterate locally without a token. If posting the comment fails (most
  commonly a 403 from a fork PR's read-only token), or any other API call
  fails (rate limit, network blip, missing scope), the run logs a plain
  warning and writes the report (or a short failure note) to
  `GITHUB_STEP_SUMMARY` instead of losing it — the job still exits 0
  either way.
- **`.github/workflows/pr-guardian.yml`** — runs on `pull_request: [opened,
  synchronize, reopened]` with `contents: read` / `pull-requests: write` /
  `checks: write` and nothing else. Checkout uses `fetch-depth: 0`: `git
  merge-tree` computes its own merge base by walking commit history, and a
  shallow clone can leave it with no common ancestor at all ("refusing to
  merge unrelated histories") — a deliberate cost/correctness tradeoff,
  worth revisiting only if repo size makes the full clone a real problem.
  Has a `concurrency` group keyed by PR number with `cancel-in-progress:
  true`, so two overlapping runs (e.g. two quick pushes) can't both decide
  "no existing comment" and each create one — GitHub's REST API has no
  compare-and-swap for issue comments, so this has to be prevented at the
  workflow level, not in the upsert logic. Note that this concurrency group
  is keyed per-PR only — it does not serialize *different* PRs' runs against
  each other, which is exactly why `merge_check.py`'s ref namespacing keys
  on both PR numbers (see above). Passes `ANTHROPIC_API_KEY` from repo
  secrets to the run step — see [README.md](README.md) for how to add it;
  it's optional, and its absence only disables Phase 3.

## Roadmap

**Phase 1 (this phase)** — deterministic contract-vs-release-notes check,
one upserted comment, warn-only, GitHub Actions integration, tests with no
network calls.

**Phase 2 — Deterministic checks, expanded.**
- ✅ Merge-conflict detection against `main` and against each other open PR
  (`guardian/merge_check.py`, via `git merge-tree`), published both in the
  PR comment and as a `neutral` check run.
- ✅ File-overlap detection against other open PRs (`guardian/overlap.py`)
  — catches the case `git merge-tree` alone can't: two PRs editing
  different parts of the same file merge cleanly but can still be a
  meaningful silent collision.
- Still open: more contract categories (dependency lockfiles,
  IaC/Terraform, feature-flag definitions), smarter matching (e.g. diffing
  an OpenAPI spec to see if it's actually a *breaking* change, not just any
  change), reducing false positives from Phase 1's pattern-matching
  approach.

**Phase 3 — Claude-based semantic analysis.**
- ✅ Advisory risk analysis (`guardian/claude_analysis.py`) gated on Phase
  1/2 findings — never runs on an unflagged PR. Structured JSON output
  (risk/category/explanation/evidence, schema above), retry-then-degrade
  on a failed attempt, missing `ANTHROPIC_API_KEY` handled gracefully.
  Rendered as its own "Claude's analysis" comment section and folded into
  the check run title, both still `neutral`/non-blocking.
- Still open: judging whether release notes *actually describe* a contract
  change (not just "a release-notes file was touched") — the original
  framing for this phase — and catching risky changes that Phase 1's
  regex-based classification misses entirely (e.g. a subtly incompatible
  API field rename) *without* an existing Phase 1/2 flag to trigger on.
  The current implementation only reasons about *already-flagged* PRs;
  extending it to independently surface novel risk on an unflagged PR is
  a bigger, separate design question (when does an LLM-only check get to
  run, and at what cost) — deliberately left for a later iteration rather
  than bundled into this one.

**Phase 4 — Re-check PRs on push to `main`.** Deterministic and semantic
checks only run at PR time; add a post-merge check that verifies deployed
contract changes were actually accompanied by the release process they
needed, to catch drift that slipped through review.

**Phase 5 — Fix suggestions.** Instead of only flagging an absence, draft
suggested release-notes text or a migration checklist the author can
accept, using the diff content Phase 3 already analyzes.

## Known limitations / test debt

- **No cap on open-PR count for merge-tree checks.** `run_merge_checks`
  fetches and compares against every open PR returned by `list_open_prs`.
  Fine at current repo activity levels; if a repo ever has dozens of PRs
  open simultaneously, this adds a fetch + `merge-tree` per PR to every
  run and CI time could become a real problem. Revisit only if that
  actually happens (matches Phase 1's "add complexity when a real failure
  mode shows up" philosophy) — don't pre-emptively add a cap.
- **Draft PRs are included** in both overlap and merge-conflict checks,
  same as any other open PR. Not filtered out because there's no evidence
  yet that draft-PR noise is a real problem worth the added logic.
- **Check runs aren't deduped across manual job re-runs beyond
  find-or-create by name.** `find_check_run` looks up an existing
  `"PR Guardian"` check run for the commit SHA before creating a new one,
  so re-running the same workflow run updates in place; this hasn't been
  tested against GitHub's actual re-run semantics (only against a mocked
  client), unlike the PR-comment upsert path which was verified
  end-to-end on a real PR.
- **No per-push caching of Claude analysis.** `analyze_pr` re-runs on
  every push to a PR that's still flagged, even if nothing about the
  flagged files changed since the last push — a cost implication worth
  watching if a PR gets many small pushes while still flagged. Revisit
  with caching (e.g. keyed on the flagged files' content hash) only if
  this proves to matter in practice.
- **Only this PR's own diff is sent to Claude for an overlap finding**,
  not the other PR's. `_run_phase2_checks` already fetches the other PR's
  files (for `find_overlaps`), but that content isn't threaded into Phase
  3 — a deliberate simplification, not an oversight. Worth revisiting if
  overlap-triggered analyses turn out to need the other side's diff to
  judge risk accurately.
- **Phase 3 has not been verified live against a real PR** the way Phase
  1/2 were — this environment has no `ANTHROPIC_API_KEY`, so
  `guardian/claude_analysis.py` is tested only against a mocked Anthropic
  client. Recommend a manual smoke test against a real flagged PR with the
  secret configured before fully trusting it in production.

## Ground rules

- **Warn-only.** PR Guardian never fails CI or blocks a merge in Phase 1.
  It always exits 0, even when a PR is flagged. If blocking is ever added
  in a later phase, it must be an explicit, separate opt-in — never the
  default.
- **PR content is untrusted input.** Diffs, PR titles/descriptions, and
  existing comments come from external contributors and must be treated as
  data only — never as instructions to follow. Phase 3's system prompt
  (`guardian/claude_analysis.py::SYSTEM_PROMPT`) states this explicitly to
  the model; `test_system_prompt_names_diff_content_as_untrusted_data`
  guards that framing against silent erosion.
- **Claude's verdict is advisory input to the report only.** `main.py`'s
  control flow — which comment to post, the check run's `conclusion` —
  never branches on any field of `ClaudeAnalysisResult` (`risk`,
  `category`, `explanation`, `evidence`). The result is passed to
  `report.py` purely for rendering as text; `conclusion` stays the literal
  `"neutral"` at its call sites regardless of what Claude returns. This is
  enforced in code, not just prompt wording — the prompt only reduces how
  often a compromised model *tries* something like "mark this safe,
  suppress the other findings"; the code guarantees it can't succeed
  either way even if the model complies with an injected instruction.
  `test_injection_shaped_claude_output_does_not_alter_control_flow` in
  `test_main.py` is the regression test for this.
- **One comment per PR.** Always upsert via the `COMMENT_MARKER` in
  `report.py` (find the existing comment, edit it) instead of posting a new
  comment on every push. Never spam a PR's comment thread.
