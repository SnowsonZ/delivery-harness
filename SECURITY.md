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

What still holds: pull requests and passing `harness` are required on the default branch, history rewrites and deletions are refused, and `policy` still routes only R0/R1 in autonomous classes to auto-merge. To narrow the exposure, give the agent a fine-grained personal access token limited to this repository (contents and pull requests only, no administration, so it cannot edit the ruleset) instead of your full `gh` login, and keep every class in `autonomy.toml` at human review until you trust the setup. Moving to two accounts later only needs the second account, an App and `approval = "app"`.

## Known limits

- Guard hooks depend on each agent host's hook support and on the owner trusting the project once; an untrusted project leaves only the git and server layers.
- Executors run with the same OS user as the owner. Credential isolation across operating systems is not implemented yet.
- Command parsing is structural but not a full shell; unparsable commands fall back to conservative string rules.
- Windows is not supported (hooks are POSIX shell).

## Reporting a vulnerability

Please open a private security advisory on this repository rather than a public issue.
