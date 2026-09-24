# PR Guardian

A GitHub bot that catches silent failures in pull requests — changes that
merge cleanly in git but break things at runtime. Example: a DB migration
that isn't mentioned in release notes, so nobody realizes production needs
a manual `migrate` step before the next deploy.

## Architecture (Phase 1)

Data flow: GitHub Actions `pull_request` event → `guardian.main` →
`guardian.contracts.analyze` → `guardian.report.build_comment` →
`guardian.github_client.GitHubClient` upserts one PR comment.

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
- **`guardian/github_client.py`** — thin `requests`-based wrapper around
  the GitHub REST API: list a PR's changed files (with pagination via the
  `Link` response header — needed once a PR has 30+ files or comments),
  list issue comments, create/update a comment. `list_pr_files` returns
  `{"filename", "previous_filename"}` per entry so renames survive into
  `contracts.analyze`. No retries or caching by design — Phase 1 keeps this
  minimal; only add complexity here when a real failure mode shows up.
- **`guardian/report.py`** — builds the Markdown comment body and finds
  PR Guardian's own previous comment via `COMMENT_MARKER`, a hidden HTML
  comment (`<!-- pr-guardian:report -->`) that's always the first line of
  anything we post.
- **`guardian/main.py`** — entry point. Reads the event JSON
  (`GITHUB_EVENT_PATH`), extracts the PR number, runs the analysis, and
  upserts the comment. `--dry-run [--files a b c]` skips the GitHub API
  entirely and prints the report to stdout — the primary way to iterate
  locally without a token. If posting the comment fails (most commonly a
  403 from a fork PR's read-only token), or any other API call fails
  (rate limit, network blip, missing scope), the run logs a plain warning
  and writes the report (or a short failure note) to `GITHUB_STEP_SUMMARY`
  instead of losing it — the job still exits 0 either way.
- **`.github/workflows/pr-guardian.yml`** — runs on `pull_request: [opened,
  synchronize, reopened]` with `contents: read` / `pull-requests: write`
  and nothing else. Has a `concurrency` group keyed by PR number with
  `cancel-in-progress: true`, so two overlapping runs (e.g. two quick
  pushes) can't both decide "no existing comment" and each create one —
  GitHub's REST API has no compare-and-swap for issue comments, so this
  has to be prevented at the workflow level, not in the upsert logic.

## Roadmap

**Phase 1 (this phase)** — deterministic contract-vs-release-notes check,
one upserted comment, warn-only, GitHub Actions integration, tests with no
network calls.

**Phase 2 — Deterministic checks, expanded.** More contract categories
(dependency lockfiles, IaC/Terraform, feature-flag definitions), smarter
matching (e.g. diffing an OpenAPI spec to see if it's actually a *breaking*
change, not just any change), reducing false positives from Phase 1's
pattern-matching approach.

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
