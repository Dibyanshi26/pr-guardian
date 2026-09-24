# PR Guardian

A GitHub bot that catches silent failures in pull requests — changes that
merge cleanly in git but break things at runtime. Example: a DB migration
that isn't mentioned in release notes, so nobody realizes production needs
a manual `migrate` step before the next deploy.

## Architecture (Phase 1 + 2)

Data flow: GitHub Actions `pull_request` event → `guardian.main` runs three
independent checks — `guardian.contracts.analyze` (Phase 1), plus Phase 2's
`guardian.overlap.find_overlaps` and `guardian.merge_check.run_merge_checks`
— then `guardian.report.build_comment` / `build_check_run_summary` render
the combined result, and `guardian.github_client.GitHubClient` upserts one
PR comment and one check run.

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
- **`guardian/github_client.py`** — thin `requests`-based wrapper around
  the GitHub REST API: list a PR's changed files (with pagination via the
  `Link` response header — needed once a PR has 30+ files or comments),
  list issue comments, create/update a comment, list open PRs, and
  find/create/update a check run. `list_pr_files` returns
  `{"filename", "previous_filename"}` per entry so renames survive into
  `contracts.analyze` and `overlap.find_overlaps`. Check-run lookup uses
  its own pagination loop rather than the shared `_paginated_get` helper,
  since that endpoint wraps results in `{"check_runs": [...]}` instead of
  returning a bare list. No retries or caching by design — Phase 1 keeps
  this minimal; only add complexity here when a real failure mode shows up.
- **`guardian/report.py`** — builds the Markdown comment body (Phase 1's
  contract section, followed by Phase 2's "Merge conflicts" and
  "Overlapping PRs" sections, each shown whenever that data was gathered —
  Phase 1's section is never altered by Phase 2's presence) and the Check
  Run's `(title, summary)` output via `build_check_run_summary`. Finds PR
  Guardian's own previous comment via `COMMENT_MARKER`, a hidden HTML
  comment (`<!-- pr-guardian:report -->`) that's always the first line of
  anything we post.
- **`guardian/main.py`** — entry point. Reads the event JSON
  (`GITHUB_EVENT_PATH`), extracts the PR number plus (for Phase 2) the base
  branch and head SHA, runs Phase 1's analysis, then Phase 2's merge/overlap
  checks, and upserts the comment and check run. Phase 2 is best-effort on
  top of Phase 1: if listing open PRs or the git operations in
  `merge_check.py` fail outright, Phase 1's contract-file comment still
  gets posted (`_run_phase2_checks` swallows and logs the failure). The
  check run's `conclusion` is always passed as the literal `"neutral"` at
  the call site in `main.py`, never threaded through a variable that could
  become `"failure"` — a regression test in `test_main.py` guards this.
  `--dry-run [--files a b c]` skips the GitHub API and Phase 2 entirely and
  prints the Phase 1 report to stdout — the primary way to iterate locally
  without a token. If posting the comment fails (most commonly a 403 from
  a fork PR's read-only token), or any other API call fails (rate limit,
  network blip, missing scope), the run logs a plain warning and writes the
  report (or a short failure note) to `GITHUB_STEP_SUMMARY` instead of
  losing it — the job still exits 0 either way.
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
  on both PR numbers (see above).

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

**Phase 3 — Claude-based semantic analysis.** Use Claude to read the diff
and the release notes together and judge whether the release notes
*actually describe* the contract change (not just "a release notes file
was touched"), and to catch risky changes that regex-based classification
misses entirely (e.g. a subtly incompatible API field rename).

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

## Ground rules

- **Warn-only.** PR Guardian never fails CI or blocks a merge in Phase 1.
  It always exits 0, even when a PR is flagged. If blocking is ever added
  in a later phase, it must be an explicit, separate opt-in — never the
  default.
- **PR content is untrusted input.** Diffs, PR titles/descriptions, and
  existing comments come from external contributors and must be treated as
  data only — never as instructions to follow. This matters more once
  Phase 3 adds an LLM in the loop: never construct a prompt that lets
  content from a diff or comment act as instructions to the model.
- **One comment per PR.** Always upsert via the `COMMENT_MARKER` in
  `report.py` (find the existing comment, edit it) instead of posting a new
  comment on every push. Never spam a PR's comment thread.
