# delivery-harness

A verifiable delivery harness for AI coding agents. It turns "the agent says it is done" into machine-checked evidence, so agents can deliver continuously with little human involvement, and people only do what machines cannot: define intent, set constraints, review evidence and handle exceptions.

[中文说明](README.zh-CN.md)

## What it does

- **Deterministic gates.** One `verify` command runs the same checks locally, in CI and inside agent dispatch: repository hygiene (secrets, local paths, forbidden files), complexity ratchets, doc links, spec-acceptance ↔ test mapping, task-brief admission, and your project's own lint and test commands.
- **Checks that check themselves.** Fixes carrying a `Defect:` trailer must fail before the fix and pass after it; existing tests run at their base version so an agent cannot weaken them; historic incidents are re-injected (replay) and must be caught; mutation scores and quality baselines only move in one direction.
- **Risk routing instead of trust.** Every change is classified R0–R3 from the paths it touches, not from what the author claims. Low-risk, well-verified classes merge automatically; everything else goes to a human. Autonomy is granted per task class with an error budget and is withdrawn automatically when the budget is exceeded.
- **Three layers of guards.** Agent-tool hooks (Claude Code, Codex, OpenCode, Pi, Zcode) refuse destructive commands and edits to the harness itself; git hooks protect branches and tags regardless of which agent is used; server-side rulesets and a separate agent account are the backstop.
- **Observability and evidence.** Privacy-filtered events are chained per source and trace id. `events` exports/imports safe bundles; `trace --ci` reconstructs timelines; `audit` checks merged-PR references and required stages; `alert` publishes deduplicated warnings. Merge ledgers and fixed anchors make evidence retrievable across machines. Observation and publication failures never change gate or routing decisions.
- **Dispatch and independent review.** `dispatch` hands a merged task brief to an executor agent in an isolated worktree slot, runs the gates outside the executor, opens the PR and escalates when stuck. `dispatch review` has a different agent review R2+ PRs read-only.

## How it is installed

The engine is **vendored** into each project under `.harness/engine/` and pinned by `.harness/engine.lock` (version, engine commit, tree hash). The trusted source of guards and rules is whatever the project owner merged into `main`; a vendored, hash-locked copy keeps that property, while a package in a local environment would not. `verify` fails if the engine files differ from the lock, and the only way to change them is `upgrade`, which produces a reviewable PR.

```
.harness/
  engine/          # this repository's engine/, read-only, hash-locked
  engine.lock
  config/          # rules.toml, autonomy.toml, checks.toml — your project's settings
  state/           # ratchet baselines and shrink-only lists
  project/         # replay_cases.py — your project's incident replays
```

The engine ships no user-specific defaults. Identity (the agent's GitHub account), models, source directories and check commands all come from `.harness/config/`; anything required but missing is an explicit error.

## Quick start

Requirements: Python 3.11+, git, and the GitHub CLI for PR automation. Linux and macOS are supported; Windows is not yet (hooks are POSIX shell).

```sh
git clone https://github.com/SnowsonZ/delivery-harness
python3 delivery-harness/engine/cli.py install --target path/to/your-repo
```

Then, in your repository:

1. Fill `.harness/config/checks.toml`: `[identity] agent_login`, `[sources]`, and your lint/test commands under `[[verify.checks]]`.
2. Review `.harness/config/rules.toml` (risk paths) and `autonomy.toml` (every class starts at human review).
3. `bin/harness guard-git install`, then `bin/verify`.
4. Commit the result through a PR that you approve.

Upgrading: `python3 delivery-harness/engine/cli.py upgrade --target path/to/your-repo` replaces the engine and the lock, nothing else.

## Platform setup (GitHub)

`install` also writes `.github/workflows/{harness,auto-merge,quality}.yml`, `.github/rulesets/*.json` and an `escape` issue template. Project-specific checks come from `checks.toml`; edit the "Prepare project" steps in the workflows for your toolchain. Requirements come in two shapes, chosen by `[platform] approval` in `checks.toml`.

**Two accounts (default, `approval = "app"`).** You (the owner), a separate GitHub account for the agent (`[identity] agent_login`, added as a collaborator with write access; `bin/as-agent` takes its token from `gh auth login`), and two GitHub Apps you create yourself, both installed on the repository:

- the **approval App** with *Pull requests: read and write*. It only approves changes that `policy` routes to auto-merge (R0/R1, and R2 PRs on the contract route when `[contract_route]` is configured).
- the **sync App** with *Contents: read and write* and *Pull requests: read and write*. When a PR branch is behind the default branch, it updates the branch (`gh pr update-branch`) before anything is approved.

The ruleset requires an approval from someone other than the last pusher, so neither the agent nor the workflow can approve alone — and the two jobs must be two separate Apps: an App that syncs the branch becomes the last pusher, and its own approval no longer satisfies that rule, so a PR that was synced could never be auto-merged. If you do not configure a sync App, a behind PR is not synced; the workflow comments on the PR asking for a manual sync, and the next run re-judges. Once per repository (the names below are the defaults; override them under `[platform]`):

```sh
gh api repos/OWNER/REPO/rulesets --method POST --input .github/rulesets/main.json
echo '{"deployment_branch_policy":{"protected_branches":true,"custom_branch_policies":false}}' \
  | gh api repos/OWNER/REPO/environments/harness-auto-merge --method PUT --input -
gh variable set HARNESS_APP_CLIENT_ID --repo OWNER/REPO --body "<approval app client id>"
gh secret set HARNESS_APP_PRIVATE_KEY --repo OWNER/REPO --env harness-auto-merge < approval-app-private-key.pem
gh variable set HARNESS_SYNC_APP_CLIENT_ID --repo OWNER/REPO --body "<sync app client id>"
gh secret set HARNESS_SYNC_APP_PRIVATE_KEY --repo OWNER/REPO --env harness-auto-merge < sync-app-private-key.pem
gh api repos/OWNER/REPO/actions/permissions/workflow --method PUT \
  -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false
```

**One account (`approval = "none"`).** Set `[identity] agent_login` to your own login and apply `.github/rulesets/main-single-account.json` instead (no approval required, everything else the same); no App or environment is needed and `auto-merge` merges with `GITHUB_TOKEN`. This works, but it removes the separation the two-account setup gives you: server-side rules can no longer tell the agent from you. Without a sync App, native auto-merge is enabled with `GITHUB_TOKEN`; a merge made with the workflow token does not trigger the workflows on the default branch, so the push-triggered sync chain (`sync-waiting`) is inactive in this mode and behind PRs wait for the next harness run instead. See [SECURITY.md](SECURITY.md#single-account-mode) before choosing it.

Whichever you choose, turn on **Settings → General → Pull Requests → Allow auto-merge** for the repository: the workflow enables GitHub's native auto-merge so later-required checks, unknown merge states and test-merge commits are handled by GitHub itself. Without the setting, enabling fails, the workflow falls back to an immediate merge and comments on the PR asking you to turn it on. Merge the adoption PR yourself: `auto-merge` is triggered by `workflow_run`, which only runs workflows that already exist on the default branch. Required status checks in both rulesets: `harness` (the job in `harness.yml`); add your own project checks to the ruleset if you have separate workflows.

**Shutting auto-merge down** (also for recycling a class back to human review): disable the workflow first (Actions → auto-merge → **Disable workflow**), cancel or wait for in-flight runs, then run `python3 .harness/engine/cli.py automerge-off` until it exits 0 — it revokes every outstanding auto-merge request and re-checks that no incomplete auto-merge run and no request remains (`--dry-run` lists without changing).

## Commands

All commands run through `bin/harness <command>` (or `python3 .harness/engine/cli.py <command>`). `bin/verify` and `bin/dispatch` are shortcuts.

| Command | Purpose |
|---|---|
| `verify [--quick\|--full] [--strict]` | Run all gates; `--full` adds incident replay |
| `integrity` | Engine files match `engine.lock` |
| `hygiene`, `quality`, `docs`, `acceptance`, `taskbook` | Individual gates |
| `risk`, `r1`, `policy`, `run-check` | Risk level, refactor checks, merge routing, dispatch-record checks |
| `evidence`, `base-tests`, `replay`, `mutate` | Checks on the checks |
| `guard-command`, `guard-git` | Agent-tool and git guards (called by hooks) |
| `dispatch run\|status\|stop\|review` | Executor dispatch and independent review |
| `events [--since 1d] [--stage verify] [--status fail] [--json]` | Filter local events and show counts |
| `events --export <file>` / `events --import <file>` | Export the complete store / validate and idempotently import an EventBundle |
| `trace <task-id\|PR\|branch> [--ci] [--json]` | Reconstruct the timeline; `--ci` downloads CI packages |
| `audit <PR> [--json]` / `audit --all-merged [--since 30d] [--json]` | Audit one merged PR or the merged-PR window |
| `alert <reason> [--pr <n>] [--trace <branch>] [--task <id>] [--bundle <dir\|file>] [--json]` | Publish an evidence-backed, deduplicated alert |
| `metrics`, `weekly` | Delivery metrics and the weekly report, including the event summary |
| `release-check` | Tag matches the project version and is on `main` |
| `install`, `upgrade` | Run from a checkout of this repository |

## Observability

### Inspect and reconstruct

Run these in an installed project, replacing the task and PR ids with local project ids:

```sh
bin/harness events --since 1d --stage verify --json
bin/harness events --export build/harness-events.json
bin/harness events --import build/harness-events.json
bin/harness trace T001 --json
bin/harness trace 42 --ci --json
bin/harness audit 42 --json
bin/harness audit --all-merged --since 30d --json
```

`events --export` exports the complete store and complete chain prefixes. Export/import cannot be combined with display filters; such combinations exit 2. An EventBundle contains safe structured data and content hashes; it does not copy SQLite, WAL/SHM files, raw verify logs or agent conversations. Import checks schema, privacy and chain hashes, deduplicates by hash and refuses conflicts. CI downloads also bind repository, workflow, run/attempt/job and head to API facts. Judge runs are linked by the default-branch `run-name`, with packages checked against the API identity of that run.

`trace` accepts a task id, PR number (also `#42`) or full branch name, reports the longest stage and first failure, and preserves missing-data diagnostics. `head_mismatch` means a legal older head was not imported; it remains visible but is informational. Missing/malformed heads and other import findings remain failures. `audit` covers **merged PRs only**: exit 0 means applicable checks passed, 1 means findings, and 2 means bad arguments, invalid audit configuration or an overall API/un-auditable-PR failure. Missing/expired evidence is never counted as verified. References are parsed as data, never executed.

### Stores, retention and anchors

| Source | Location and lifetime | Scope |
|---|---|---|
| Local observations | `<git common dir>/harness/harness.db`; shared by worktrees, untracked | Events, references and anchors have no automatic retention deletion |
| Local content and raw dispatch streams | `harness/artifacts/` and `dispatch/runs/` under the git common directory | Default 30 days; cleanup on the first emit each day; terminated raw runs stay local |
| CI event packages | Actions artifacts `harness-events-<run>-<attempt>-<job>` | Template retention 90 days; uploads and summaries also run after failures |
| Merge ledger | `harness-audit:<merged UTC year>/<PR>.json` | Merged PRs only; retained in Git without automatic expiry |

Run records fix already-observed stage prefixes; CI packages carry anchors. A `harness-audit:<PR>` PR comment fixes the ledger path, commit, file SHA-256 and chain heads. The writer uses normal fast-forward, accepts identical bytes idempotently and refuses different bytes for the same PR. The ruleset prevents deletion/history rewrite, but permits ordinary commits replacing old files. `audit` checks fixed expectations, including tail deletions whose remaining internal chain is valid. See [SECURITY](SECURITY.md#observability-evidence) for the same-user tampering window and expiry limits.

Optional defaults in `.harness/config/checks.toml`:

```toml
[events]
enabled = true
artifact_days = 30

[audit]
require_review_risk = 2
require_route_for_auto = true
require_run_record_for_task = true
verify_anchors = true
```

The event environment switch documented in [CHANGELOG](CHANGELOG.md) takes precedence over configuration. Invalid `artifact_days` warns and falls back to 30; unknown/invalid audit keys are configuration errors. Audit settings affect reports only. Designer PRs need no dispatch records; task PRs do. Review and approval checks follow the applicable risk/route and platform approval mode.

### Alerts and the weekly report

`alert` writes a PR comment plus an `escalation` label, or an escalation issue without `--pr`. Remote `(trace, reason)` markers deduplicate across machines. Supported reasons are listed by `bin/harness alert --help`. **The following publishes a real alert; run it only with a triggering package and intended publication:**

```sh
bin/harness alert audit_anchor_mismatch --pr 42 --bundle build/harness-events.json --json
```

`--bundle` checks trigger evidence without importing/executing the package; no trigger means no publication. `--help` is a harmless syntax check. Publishing failures do not change original judgments. Trusted default-branch jobs carry alerts after success or failure; PR checks keep read-only tokens. No resident daemon, OTLP endpoint or extra notification channel is added.

Dispatch warnings are optional in `.harness/config/rules.toml`: omitting `[alerts]` disables them; adding the section enables the last-CI-round warning. `guard_denials_threshold` has no enabled default: a positive integer enables the per-round denied-tool-call warning, while an absent/invalid value disables that warning (invalid values emit a notice). These settings do not change the budget or guard decisions.

The weekly report preserves its old sections and sources, then appends two separate parts. **A** uses merged-PR ledgers (event durations only, excluding duplicate record summaries) and checkout run records whose `ended_at` is in the UTC week. **B** is local-only and is always unavailable under `CI=true`. The coverage PR count matches the old human-intervention section. Rule hits and denied tool calls are different units, never cross-checked or summed together. Designer guard denials are unavailable from A; CI does not calculate a local audit-finding count. Missing/unreadable evidence is unavailable, not fabricated as zero.

### Ledger platform setup — owner actions

`install` supplies `.github/rulesets/harness-audit.json`. Upgrades need deliberate template copies; see [upgrade instructions](docs/upgrading.md). In repository **Settings**, the owner opens **Rulesets**, selects **New ruleset → Import a ruleset**, opens the JSON, reviews it and clicks **Create**; update an existing ledger ruleset instead of duplicating it. See [GitHub's import instructions](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/managing-rulesets-for-a-repository#importing-a-ruleset). Confirm branch coverage, active enforcement, deletion/history-rewrite restrictions and bypass actors.

The first successful ledger writer run creates the branch. The trusted `harness.yml` job needs `contents: write`, `pull-requests: write`, full checkout history, git author/committer identity and `gh auth setup-git` for its temporary clone. After a real merge, the owner checks:

```sh
gh api repos/OWNER/REPO/rulesets --paginate
gh api repos/OWNER/REPO/branches/harness-audit
gh api repos/OWNER/REPO/actions/permissions/workflow
```

Confirm the active ledger ruleset contains `deletion` and `non_fast_forward` rules with no unintended bypass, and inspect workflow permissions plus the ledger file/anchor comment. A permission error means that setting was not verified; use authorized owner read access, not a broader agent token. Green main checks alone are insufficient because ledger publication can fail under `continue-on-error`.

## Status

The engine version remains 0.1.0; the maintainer decides the release version and tag. B46 observability implementation is merged, including T501/T502 and the T601 complete-chain fixtures. Phase upgrades, consumer equivalence, real R0/R2 PR reconstruction/audits, final documentation (T602) and the owner's platform check are complete, so B46 is closed; only the release version and tag remain, decided by the maintainer. This does not claim a new release. English messages/prompts, configurable directories, a TypeScript plugin and project adoption remain planned — see [CHANGELOG](CHANGELOG.md).

This repository develops itself with its own engine: `.harness/`, `bin/`, the git and agent hooks and `.github/workflows/` are this project's own instance (with `.harness/engine/` a vendored copy of the previous merged engine), not part of the product. The product is `engine/` and `templates/`.

Security model and limits: [SECURITY.md](SECURITY.md). License: [MIT](LICENSE). Development rules, maintainer workflow, backlog and roadmap (in Chinese): [AGENTS.md](AGENTS.md), [docs/maintainers.md](docs/maintainers.md), [docs/backlog.md](docs/backlog.md), [docs/plans/2026-09-29-roadmap.md](docs/plans/2026-09-29-roadmap.md).

B46 observability design, implementation contracts and gate definitions (in Chinese): [execution plan](docs/plans/2026-09-29-observability-execution-plan.md), [task contracts](docs/plans/2026-09-29-observability-task-contracts.md), [requirements traceability](docs/plans/2026-09-29-observability-traceability.md).

The system's original target-state design (in Chinese) and the underlying research report snapshot moved into this repository on 2026-10-09 from the originating project: [target-state design](docs/plans/2026-09-27-target-state-design.md), [research snapshot](docs/research/2026-09-23-agent-delivery-theory.md), [extraction decision](docs/decisions/0001-harness-extraction.md); build-era reviews under [docs/review/](docs/review/).
