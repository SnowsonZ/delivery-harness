# delivery-harness

A verifiable delivery harness for AI coding agents. It turns "the agent says it is done" into machine-checked evidence, so agents can deliver continuously with little human involvement, and people only do what machines cannot: define intent, set constraints, review evidence and handle exceptions.

[中文说明](README.zh-CN.md)

## What it does

- **Deterministic gates.** One `verify` command runs the same checks locally, in CI and inside agent dispatch: repository hygiene (secrets, local paths, forbidden files), complexity ratchets, doc links, spec-acceptance ↔ test mapping, task-brief admission, and your project's own lint and test commands.
- **Checks that check themselves.** Fixes carrying a `Defect:` trailer must fail before the fix and pass after it; existing tests run at their base version so an agent cannot weaken them; historic incidents are re-injected (replay) and must be caught; mutation scores and quality baselines only move in one direction.
- **Risk routing instead of trust.** Every change is classified R0–R3 from the paths it touches, not from what the author claims. Low-risk, well-verified classes merge automatically; everything else goes to a human. Autonomy is granted per task class with an error budget and is withdrawn automatically when the budget is exceeded.
- **Three layers of guards.** Agent-tool hooks (Claude Code, Codex, OpenCode, Pi, Zcode) refuse destructive commands and edits to the harness itself; git hooks protect branches and tags regardless of which agent is used; server-side rulesets and a separate agent account are the backstop.
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

**Two accounts (default, `approval = "app"`).** You (the owner), a separate GitHub account for the agent (`[identity] agent_login`, added as a collaborator with write access; `bin/as-agent` takes its token from `gh auth login`), and a GitHub App you create yourself with *Pull requests: read and write*, installed on the repository. The App only approves R0/R1 changes that `policy` routes to auto-merge; the ruleset requires an approval from someone other than the last pusher, so neither the agent nor the workflow can approve alone. Once per repository (the names below are the defaults; override them under `[platform]`):

```sh
gh api repos/OWNER/REPO/rulesets --method POST --input .github/rulesets/main.json
echo '{"deployment_branch_policy":{"protected_branches":true,"custom_branch_policies":false}}' \
  | gh api repos/OWNER/REPO/environments/harness-auto-merge --method PUT --input -
gh variable set HARNESS_APP_CLIENT_ID --repo OWNER/REPO --body "<app client id>"
gh secret set HARNESS_APP_PRIVATE_KEY --repo OWNER/REPO --env harness-auto-merge < app-private-key.pem
gh api repos/OWNER/REPO/actions/permissions/workflow --method PUT \
  -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false
```

**One account (`approval = "none"`).** Set `[identity] agent_login` to your own login and apply `.github/rulesets/main-single-account.json` instead (no approval required, everything else the same); no App or environment is needed and `auto-merge` merges with `GITHUB_TOKEN`. This works, but it removes the separation the two-account setup gives you: server-side rules can no longer tell the agent from you. See [SECURITY.md](SECURITY.md#single-account-mode) before choosing it.

Whichever you choose, merge the adoption PR yourself: `auto-merge` is triggered by `workflow_run`, which only runs workflows that already exist on the default branch. Required status checks in both rulesets: `harness` (the job in `harness.yml`); add your own project checks to the ruleset if you have separate workflows.

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
| `metrics`, `weekly` | Delivery metrics and the weekly report |
| `release-check` | Tag matches the project version and is on `main` |
| `install`, `upgrade` | Run from a checkout of this repository |

## Status

Version 0.1 extracts the engine from the project where it was built and proven ([Agent-Notification](https://github.com/SnowsonZ/Agent-Notification)); behaviour is unchanged there, verified by identical test counts, quality metrics and mutation scores before and after. Messages and prompts are in Chinese for now; English localisation, configurable directory conventions, a TypeScript language plugin and end-to-end tracing are planned — see [CHANGELOG](CHANGELOG.md).

This repository develops itself with its own engine: `.harness/`, `bin/`, the git and agent hooks and `.github/workflows/` are this project's own instance (with `.harness/engine/` a vendored copy of the previous merged engine), not part of the product. The product is `engine/` and `templates/`.

Security model and limits: [SECURITY.md](SECURITY.md). License: [MIT](LICENSE). Development rules, backlog and roadmap (in Chinese): [AGENTS.md](AGENTS.md), [docs/backlog.md](docs/backlog.md), [docs/plans/2026-09-29-roadmap.md](docs/plans/2026-09-29-roadmap.md).
