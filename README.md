# PR Guardian

A GitHub bot that catches silent failures in pull requests — changes that
merge cleanly in git but break things at runtime. See [CLAUDE.md](CLAUDE.md)
for the full architecture, roadmap, and ground rules.

## Setup

Add the workflow at [.github/workflows/pr-guardian.yml](.github/workflows/pr-guardian.yml)
to a repo and it runs on every `pull_request` event using the default
`GITHUB_TOKEN` — no configuration needed for Phase 1 (contract-file checks)
or Phase 2 (merge-conflict and cross-PR overlap checks).

### Configuration

**`ANTHROPIC_API_KEY`** — optional, only used by Phase 3 (Claude's risk
analysis on top of whatever Phase 1/2 already flagged). Without it, Guardian
logs a notice in the run and still publishes the Phase 1/2 results as
normal; nothing fails.

To add it:

1. Get an API key from the [Claude Console](https://platform.claude.com/settings/keys).
2. In the repo, go to **Settings → Secrets and variables → Actions → New
   repository secret**.
3. Name it `ANTHROPIC_API_KEY` and paste the key.

Claude analysis only runs on PRs that Phase 1 or Phase 2 already flagged,
and only sends the diff hunks for the specific files that triggered a flag
— never the whole PR diff — to keep cost and prompt size bounded (see
CLAUDE.md's Phase 3 notes for the exact scoping and budget).

## Local development

```bash
pip install -r requirements.txt
python -m pytest
```

`python -m guardian.main --dry-run --files a.py b.py` runs Phase 1's
contract-file check against an explicit file list and prints the report to
stdout — no GitHub token or network access needed. Phase 2 and Phase 3
require a real PR context and aren't exercised by `--dry-run`.
