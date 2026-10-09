# Security model

delivery-harness limits what AI coding agents can do in a repository and makes their work verifiable. It is a set of guard rails for cooperative but fallible agents, not a sandbox against a determined attacker. This page states what each layer stops and what it does not.

## Trust anchor

Guards and rules are trusted only in the form the repository owner merged into `main`. The git guard reads rules from `origin/main`; dispatch exports the guard for executors from `origin/main`; CI auto-merge runs `main`'s workflow and engine against the PR's diff as data. The engine is vendored under `.harness/engine/` and pinned by a tree hash in `.harness/engine.lock`, so a change to the engine outside `upgrade` fails `verify`, and every change to `.harness/` is classified R3 (owner approval).

## Layers

| Layer | Stops | Does not stop |
|---|---|---|
| Agent-tool hooks (`guard-command`) | Destructive git operations, pushing `main` or tags, merging or approving PRs, setting override variables, deleting issues or audit labels; executors editing the harness, task briefs, specs or run records | Agents whose host does not run the hook, or that were started without trusting the project; code the agent writes and runs through channels the hook does not see |
| git hooks (`guard-git`) | Commits and pushes to protected branches, history rewrites, tag moves, pushes that fail hygiene or `verify` | Anyone who removes `core.hooksPath` or sets the human-only override variables |
| Server side (rulesets, CI, separate agent account, approval App) | Merging without a PR, required checks or a non-pusher approval, regardless of what happened locally | Misconfigured rulesets; an owner who approves without reading the evidence |

Human-only overrides (`HARNESS_ALLOW_*`, `HARNESS_SKIP_VERIFY`) exist for the owner; the agent-tool guard refuses commands that set them.

## Single-account mode

With `[platform] approval = "none"` the agent and the owner are the same GitHub account. What is lost compared with two accounts:

- The server can no longer tell agent actions from yours. A credential the agent can use is your credential: it can merge R2/R3 pull requests that should wait for you, and approvals stop being an independent signal (the ruleset requires none).
- Auto-merge runs with `GITHUB_TOKEN` and no environment-protected App credential; routing (`policy`) still decides, but nothing outside the repository backs it up.
- The agent-tool and git guards become the only barrier against merging and history rewrites, and they cover cooperative agents only.

What still holds: pull requests and passing `harness` are required on the default branch, history rewrites and deletions are refused, and `policy` still requires an autonomous class and an eligible risk route: R0/R1, or R2 with the configured contract route and its independent-review, sign-off and budget checks. To narrow the exposure, give the agent a fine-grained personal access token limited to this repository (contents and pull requests only, no administration, so it cannot edit the ruleset) instead of your full `gh` login, and keep every class in `autonomy.toml` at human review until you trust the setup. Moving to two accounts later only needs the second account, an App and `approval = "app"`. In this mode native auto-merge is enabled with the workflow token, and a workflow-token merge does not trigger the default branch's `push` workflows — the push-triggered sync chain for waiting PRs is inactive; behind PRs wait for the next harness run instead.

## Native auto-merge and its residual race

Merging is delegated to GitHub's native auto-merge: the workflow approves the exact head `policy` evaluated and enables the request (`--match-head-commit`), and GitHub alone waits for required checks and mergeable state. What the harness keeps control of: a fresh `policy` evaluation (same command, workflow token) immediately before enabling — a rejection revokes a stale request and skips approval; every veto signal (rejected independent review including a "pass" with serious findings, failed designer sign-off, the `budget-exceeded` label, the judge's request-review path) closes the request; a `push` to the default branch syncs waiting PRs (sync App only; conflicted ones are commented, labelled `escalation` and disabled); and `automerge-off` revokes all outstanding requests for shutdown or class recycling, exiting 0 only after re-checking that nothing remains. Approval stays bound to the evaluated head, so a later push voids it and nothing merges without a new approval; automatic merges are recognizable by the Bot merger.

**Accepted residual risk:** a veto issued in the seconds between that pre-enable re-check and the enabling — or exactly when the last required check completes — can still merge (the workflow's token-revocation race is the same window). Closing it completely would require making contract eligibility a required status check, which would also block owner-initiated manual merges; this was weighed and declined. The window is deliberately accepted and documented here; everything else about the veto still lands: the PR is commented, labelled and audited after the fact.

## Observability evidence

Gates, dispatch, guards and routing append structured events to a local log (`harness/harness.db` in the git common directory, untracked, plus content-addressed artifacts). Properties that matter for security:

- **Observation, not control.** Events never feed routing, guards or merge decisions; every judgment stays reproducible from git and CI evidence alone. Deleting or corrupting the log changes nothing about what merges.
- **Fixed expectations.** Run records fix already-observed stage prefixes; CI packages and PR anchor comments fix further chain heads. The merged-PR ledger records those anchors and safe reference hashes. `audit` checks each expected prefix, so deleting a tail is detectable even when the remaining internal chain is valid. Git files and comments are compared by original bytes; mutable PR drift is distinguished from byte tampering, and GitHub snapshots use the defined nine-field semantics.
- **Same-user window.** Executors share the OS user with the owner and can edit/delete local events and artifacts. Rewriting a whole chain with recomputed hashes before an independent expectation is fixed can conceal changes; locally writable anchors do not protect against an actor who changes both data and its expected values. Fixed Git/CI evidence adds detection after publication, not credential isolation or prevention. Observations never grant merge eligibility.
- **Controlled publication.** CI publishes privacy-filtered EventBundles and summaries, and the default-branch ledger writer publishes structured merged-PR evidence and a PR anchor comment. SQLite/WAL/SHM, raw verify logs, agent conversations and local review-pack content are not published. Imported references and packages are data, never executed.
- **Isolated chains.** Each CI run/attempt/job chains under its own source (`ci:<run_id>:<run_attempt>:<job>`), so one job cannot append to another's history.
- **Silence is possible.** The environment switch or `[events]` configuration can disable writing. A broken sink emits one fixed notice; alert/ledger publication failures are isolated from decisions. Missing evidence is not proof that a required stage succeeded.

### Trusted workflows and ledger branch

Judge, ledger and alert jobs use default-branch engine/workflow code. PR heads and downloaded event packages are treated as data; PR checks keep read-only tokens, while trusted publication jobs hold the narrowly required write permissions. A judge package is bound to its own API run identity and the default-branch run name linking it to the PR.

The `harness-audit` ruleset prevents branch deletion and non-fast-forward updates. **It permits ordinary commits that replace old ledger files.** The writer checks identical-byte idempotence and rejects conflicts, and a PR comment fixes the ledger commit/file hash. A malicious credential with ordinary write access can still change the branch; the fixed reference helps detect that change. Claims of platform protection require actual active ruleset, bypass and permission evidence.

### Retention and incomplete coverage

- Local content artifacts and terminated dispatch raw streams default to 30 days; event/reference/anchor rows are not automatically deleted. CI package templates retain artifacts for 90 days. Git ledgers have no automatic expiry.
- Cleanup, API access failure, artifact deletion or expiry can make original content unavailable. `audit` reports unavailable/expired/unchecked references distinctly and never turns them into successful verification. Safe hashes can remain after their original content expires.
- Only merged PRs receive ledgers. Closed, unmerged PRs remain outside this ledger scope (B52). Audits report applicable legacy/coverage limitations rather than claiming all historic PRs have complete evidence.
- Requirements and design reasoning, conversations, designer commands/manual decisions, platform-setting changes and operations outside dispatch slots are not collected as complete activity history. Their absence from events does not establish that they did not happen.
- `head_mismatch` is informational only for valid older heads not imported. Missing/malformed heads and other CI-import findings remain failures.
- No OTLP integration, resident daemon or extra notification channel is implemented; cross-platform executor credential isolation remains B31. Weekly local-only supplements are disabled in CI; absence of local audit/guard history is not a zero count.

## Known limits

- Guard hooks depend on each agent host's hook support and on the owner trusting the project once; an untrusted project leaves only the git and server layers.
- Executors run with the same OS user as the owner. Credential isolation across operating systems is not implemented yet.
- Command parsing is structural but not a full shell; unparsable commands fall back to conservative string rules.
- Same-user processes can alter local observations and expected values before independent anchors are fixed. Published evidence supports retrospective detection; it does not make the local database immutable or replace access controls.
- Windows is not supported (hooks are POSIX shell).

## Reporting a vulnerability

Please open a private security advisory on this repository rather than a public issue.
