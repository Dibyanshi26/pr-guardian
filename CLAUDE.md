# PR Guardian

A GitHub bot that catches silent failures in pull requests — changes that
merge cleanly in git but break things at runtime. Example: a DB migration
that isn't mentioned in release notes, so nobody realizes production needs
a manual `migrate` step before the next deploy.

## Architecture (Phase 1 + 2 + 3 + 4)

Data flow: `guardian.main` dispatches on `GITHUB_EVENT_NAME`. A
`pull_request` event runs the single-PR pipeline —
`guardian.contracts.analyze` (Phase 1), then Phase 2's
`guardian.overlap.find_overlaps` and `guardian.merge_check.run_merge_checks`,
then — only if Phase 1 or 2 found something —
`guardian.ai_analysis.analyze_pr` (Phase 3) judges the real risk behind
those findings; `guardian.report.build_comment` / `build_check_run_summary`
render the combined result, and `guardian.github_client.GitHubClient`
upserts one PR comment and one check run. A `push` event to the base
branch instead runs that exact same pipeline once per currently-open PR
(Phase 4, `run_for_all_open_prs`), so a finding about a *pair* of PRs
doesn't go stale when the other half of that pair changes independently.

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
- **`guardian/ai_analysis.py`** — Phase 3: a judgment layer on top of
  Phase 1/2's findings, never a replacement for them. `should_analyze`
  gates the whole module on "did Phase 1 or 2 find something" — unflagged
  PRs never call the API. `trigger_filenames` narrows to just the files
  that caused a flag, and `build_diff_context` sends only those files'
  diff hunks (from `GitHubClient.list_pr_files`'s `patch` field), truncated
  per-file and capped in total (`MAX_DIFF_CHARS_PER_FILE` /
  `MAX_TOTAL_DIFF_CHARS`) — never the whole PR diff. `call_model` asks
  `gpt-6-luna` (OpenAI, via `client.responses.parse`) for structured JSON
  output (`text_format=AIAnalysisResult`, a Pydantic model — schema
  below), retrying once on a failed/invalid attempt before giving up, with
  every failed attempt logged to `stderr` so a real failure is diagnosable
  from the GitHub Actions log alone. `analyze_pr` is the single entry
  point `main.py` depends on; its contract is that it never raises — any
  failure anywhere degrades to an `AIAnalysisOutcome` with an
  `unavailable_reason` (missing `OPENAI_API_KEY`, exhausted retries, or an
  unexpected error) instead of taking down the run. `SYSTEM_PROMPT`
  explicitly frames diff content as untrusted data, never instructions —
  see Ground rules for how that's also enforced in code, not just prompt
  wording. Model choice: `gpt-6-luna` is OpenAI's cheapest current-
  generation model ($0.10/$0.50 per MTok, vs. $10/$50 for the flagship
  `gpt-6-astra`), explicitly positioned by OpenAI for "focused, high-volume
  tasks" — the right fit for a bounded classification-plus-explanation
  call, same reasoning as picking Sonnet 5 over Opus 5.5 would have been
  on the Anthropic side. `findings_fingerprint(result, merge_report,
  overlaps, files)` — added for Phase 4 — hashes exactly what `call_model`
  would be sent (the findings summary plus the scoped diff context);
  `analyze_pr`'s optional `cached` argument lets a caller (a Phase 4
  re-check) skip the model entirely when the fresh fingerprint matches a
  previously cached one, without changing behavior for any existing
  caller that doesn't pass it.

  JSON schema (`AIAnalysisResult`):
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
  list issue comments, create/update a comment, list/fetch open PRs, and
  find/create/update a check run. `get_pr(pr_number)` (Phase 4) is a
  single-PR fetch used by a main-push re-check to confirm a PR is still
  open — and get its current head SHA — immediately before processing it,
  since the batch's initial `list_open_prs()` snapshot can go stale
  partway through a long run. `list_pr_files` returns
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
  "Overlapping PRs" sections and Phase 3's "AI risk analysis" section,
  each shown whenever that data was gathered — earlier sections are never
  altered by a later phase's presence) and the Check Run's `(title,
  summary)` output via `build_check_run_summary`. The AI section always
  renders a fixed advisory disclaimer alongside the risk/explanation, so a
  human reading the PR sees the "this doesn't change warn-only behavior"
  framing directly, not just in this doc. Finds PR Guardian's own previous
  comment via `COMMENT_MARKER`, a hidden HTML comment
  (`<!-- pr-guardian:report -->`) that's always the first line of anything
  we post. Phase 4 adds a second hidden marker, `AI_CACHE_MARKER_PREFIX`
  (`find_cached_ai_result` / embedded via `build_comment`), holding the
  last successful `AIAnalysisResult` plus the `findings_fingerprint` it
  was computed from — this keeps the project's "GitHub is the only state
  store" design (no database) rather than adding one just for this. Only
  a confirmed success is ever cached; an "unavailable" outcome never
  looks reusable to the next run. Parsing is fail-safe (a missing or
  corrupted marker just means "no cache," not a crash) — see Known
  limitations for why this is deliberately not a security boundary.
- **`guardian/main.py`** — entry point. Dispatches on
  `os.environ.get("GITHUB_EVENT_NAME", "pull_request")` — one of the env
  vars GitHub Actions sets automatically, no workflow change needed to
  pass it through. A `"push"` event routes to `run_for_all_open_prs`
  (Phase 4); anything else (including unset, so every pre-Phase-4 test
  needed zero changes) keeps the single-PR path: read the event JSON
  (`GITHUB_EVENT_PATH`), extract the PR number plus (for Phase 2) the base
  branch and head SHA and (for Phase 3) `OPENAI_API_KEY`, run Phase 1's
  analysis, then Phase 2's merge/overlap checks, then Phase 3's
  `analyze_pr`, and upsert the comment and check run — this is `run()`.
  Phase 2 is best-effort on top of Phase 1: if listing open PRs or the git
  operations in `merge_check.py` fail outright, Phase 1's contract-file
  comment still gets posted (`_run_phase2_checks` swallows and logs the
  failure). Phase 3 is best-effort by construction (`analyze_pr` never
  raises — see above); `run()` fetches the existing comment (and, from
  it, any cached AI result) *before* calling `analyze_pr`, so a matching
  fingerprint can skip the model. The check run's `conclusion` is always
  passed as the literal `"neutral"` at the call site in `main.py`, never
  threaded through a variable that could become `"failure"` — a
  regression test in `test_main.py` guards this, and it holds regardless
  of what Phase 3 returns (also tested: an `AIAnalysisResult` whose
  `explanation` contains injected-looking text doesn't change the
  conclusion or suppress Phase 1/2's findings). `run_for_all_open_prs`
  (Phase 4) lists open PRs once, then for each one fetches it fresh via
  `get_pr` (skipping it if no longer `"open"` — a PR can merge or close
  partway through a long batch run) and calls `run()` with that PR's
  current head SHA — the entire pipeline and upsert logic is reused
  as-is, not duplicated. Each PR is isolated in its own `try/except`: one
  PR's failure never aborts the batch, and `list_open_prs()` itself
  failing degrades the same way Phase 2's version already does.
  `--dry-run [--files a b c]` skips the GitHub API and every phase past
  Phase 1 entirely and prints the Phase 1 report to stdout — the primary
  way to iterate locally without a token. If posting a comment fails
  (most commonly a 403 from a fork PR's read-only token), or any other
  API call fails (rate limit, network blip, missing scope), the run logs
  a plain warning and writes the report (or a short failure note) to
  `GITHUB_STEP_SUMMARY` instead of losing it — the job still exits 0
  either way.
- **`.github/workflows/pr-guardian.yml`** — runs on `pull_request: [opened,
  synchronize, reopened]` and (Phase 4) `push: branches: [main]`, with
  `contents: read` / `pull-requests: write` / `checks: write` and nothing
  else. Checkout uses `fetch-depth: 0`: `git merge-tree` computes its own
  merge base by walking commit history, and a shallow clone can leave it
  with no common ancestor at all ("refusing to merge unrelated
  histories") — a deliberate cost/correctness tradeoff, worth revisiting
  only if repo size makes the full clone a real problem. The
  `concurrency` group is now conditional on event type: a `pull_request`
  event keeps the original per-PR group (`pr-guardian-<number>`,
  unaffected), so two overlapping runs on the *same* PR (e.g. two quick
  pushes) can't both decide "no existing comment" and each create one —
  GitHub's REST API has no compare-and-swap for issue comments, so this
  has to be prevented at the workflow level, not the upsert logic. A
  `push` event instead shares one fixed group
  (`pr-guardian-main-push`) across every push-triggered run, combined
  with `cancel-in-progress: true` this means a burst of N pushes to main
  produces at most one *completed* re-check, each new push cancelling
  whatever push-triggered run is still in flight — free debounce reusing
  a mechanism already in the file. A `sleep 90` step, gated to `push`
  events and placed *before* checkout, means an about-to-be-superseded
  run is cancelled before it spends anything at all (not even a
  checkout, let alone an AI call) rather than merely before it finishes —
  see Known limitations for what this costs in freshness. Note the
  per-PR concurrency group never overlaps with the push group (different
  key patterns), and it does not serialize *different* PRs' runs against
  each other, which is exactly why `merge_check.py`'s ref namespacing
  keys on both PR numbers (see above). Passes `OPENAI_API_KEY` from repo
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

**Phase 3 — AI-based semantic analysis.**
- ✅ Advisory risk analysis (`guardian/ai_analysis.py`, OpenAI's
  `gpt-6-luna`) gated on Phase 1/2 findings — never runs on an unflagged
  PR. Structured JSON output (risk/category/explanation/evidence, schema
  above), retry-then-degrade on a failed attempt, missing `OPENAI_API_KEY`
  handled gracefully. Rendered as its own "AI risk analysis" comment
  section and folded into the check run title, both still
  `neutral`/non-blocking.
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

**Phase 4 — Re-check open PRs on push to `main`.**
- ✅ Every currently-open PR gets re-checked (Phase 1/2/3, full pipeline,
  reused unchanged) whenever something pushes to `main`
  (`run_for_all_open_prs`), so a Phase 2 finding about a *pair* of PRs
  (a merge conflict, a file overlap) doesn't silently go stale when the
  other half of that pair changes independently — merges, gets
  force-pushed, or a new PR opens touching the same files. Debounced and
  rate-safe (see the workflow bullet above); Phase 3's AI call is skipped
  via `findings_fingerprint` whenever nothing that would change its
  answer actually changed since the last successful call for that PR.
- Still open, and a genuinely different feature from what shipped here:
  the *original* framing for this phase was a **post-merge audit** —
  verifying that contract changes actually deployed to `main` were
  accompanied by the release process they needed, to catch drift that
  slipped through review after the fact. What shipped is a pre-merge
  *staleness* fix for still-open PRs, not a retrospective audit of what
  already landed. Both are legitimate "re-check on push to main" features
  that happen to share a trigger; the audit half remains unbuilt and
  undesigned — same honesty pattern as Phase 3's roadmap note above.

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
- **The 90s debounce on a push-triggered re-check is a deliberate
  freshness-vs-cost tradeoff, not just a burst-case optimization.** Every
  push-triggered re-check — including a single isolated merge with no
  burst of pushes behind it — waits at least 90 seconds before it starts;
  a re-check never appears "instantly" after a merge to `main`, by
  design. 90s is a fixed constant, not adaptive to load or repo size —
  tune it directly in the workflow file if that tradeoff needs
  revisiting.
- **`run_for_all_open_prs` processes PRs sequentially.** A repo with many
  simultaneously open PRs has a correspondingly longer debounced run (on
  top of the 90s above). Fine at current scale; worth revisiting
  (parallelizing, or capping how many PRs one run re-checks) only if
  that stops being true.
- **The AI-result cache (`report.py`'s `AI_CACHE_MARKER_PREFIX`) is not a
  security boundary.** Someone with write access to edit Guardian's own
  comment could in principle forge a cache entry with a matching
  fingerprint and a fabricated "safe" verdict. Not treated as a real risk:
  forging it requires knowing/reproducing the exact fingerprint hash of
  real Phase 1/2 data, and anyone who can edit repo comments with that
  kind of intent already has write access to just edit the code directly.
  The cache is also advisory-only content regardless (same ground rule as
  Phase 3's own output) — it can't affect the check run conclusion or
  Phase 1/2's own findings either way.
- **No batch-level step-summary for a multi-PR re-check run.** Each PR's
  own comment-post failure still falls back to its own
  `GITHUB_STEP_SUMMARY` entry via the existing per-PR path
  (`_handle_comment_post_failure`); nothing aggregates "3 of 20 PRs failed
  to update" into one place for a push-triggered batch.
- **Only this PR's own diff is sent to the model for an overlap finding**,
  not the other PR's. `_run_phase2_checks` already fetches the other PR's
  files (for `find_overlaps`), but that content isn't threaded into Phase
  3 — a deliberate simplification, not an oversight. Worth revisiting if
  overlap-triggered analyses turn out to need the other side's diff to
  judge risk accurately.
- **Phase 3 has not been verified live against a real PR** the way Phase
  1/2 were — this environment has no `OPENAI_API_KEY`, so
  `guardian/ai_analysis.py` is tested only against a mocked OpenAI client
  (though the exact `client.responses.parse` call shape and the
  `gpt-6-luna` model ID were both verified against the actually-installed
  `openai` package and its own generated model-name types, not just docs).
  Recommend a manual smoke test against a real flagged PR with the secret
  configured before fully trusting it in production.

## Ground rules

- **Warn-only.** PR Guardian never fails CI or blocks a merge in Phase 1.
  It always exits 0, even when a PR is flagged. If blocking is ever added
  in a later phase, it must be an explicit, separate opt-in — never the
  default.
- **PR content is untrusted input.** Diffs, PR titles/descriptions, and
  existing comments come from external contributors and must be treated as
  data only — never as instructions to follow. Phase 3's system prompt
  (`guardian/ai_analysis.py::SYSTEM_PROMPT`) states this explicitly to the
  model; `test_system_prompt_names_diff_content_as_untrusted_data` guards
  that framing against silent erosion.
- **The model's verdict is advisory input to the report only.** `main.py`'s
  control flow — which comment to post, the check run's `conclusion` —
  never branches on any field of `AIAnalysisResult` (`risk`, `category`,
  `explanation`, `evidence`). The result is passed to `report.py` purely
  for rendering as text; `conclusion` stays the literal `"neutral"` at its
  call sites regardless of what the model returns. This is enforced in
  code, not just prompt wording — the prompt only reduces how often a
  compromised model *tries* something like "mark this safe, suppress the
  other findings"; the code guarantees it can't succeed either way even if
  the model complies with an injected instruction.
  `test_injection_shaped_ai_output_does_not_alter_control_flow` in
  `test_main.py` is the regression test for this.
- **One comment per PR.** Always upsert via the `COMMENT_MARKER` in
  `report.py` (find the existing comment, edit it) instead of posting a new
  comment on every push. Never spam a PR's comment thread.
