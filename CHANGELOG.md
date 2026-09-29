# Changelog

This project follows [Semantic Versioning](https://semver.org/). Versions are tagged by the maintainer.

## Unreleased — 0.1.0

First release as a standalone engine, extracted from [Agent-Notification](https://github.com/SnowsonZ/Agent-Notification) with its history (`git subtree split`).

- Package layout `engine/{core,guards,checks,routing,agents,reports,lang,prompts}` with a single entry point `engine/cli.py`.
- Vendored installation: `install` / `upgrade` write `.harness/engine/` and `.harness/engine.lock`; the new `integrity` check fails `verify` when engine files differ from the lock.
- Project settings moved to `.harness/config/` (`rules.toml`, `autonomy.toml`, new `checks.toml`), ratchet state to `.harness/state/`, incident replays to `.harness/project/replay_cases.py`.
- No user-specific defaults: agent identity, runtime interpreter, preserved paths, source directories, UI paths, release version file, verify checks, mutation targets, replay suites, reviewer models and report bots are configuration. Missing required settings are explicit errors.
- Language plugins (`engine/lang/`): Python and Swift signature comparison, dependency detection, function size and nesting.
- Templates for `bin/`, git hooks, agent-tool hooks and config skeletons (`templates/`); installation never overwrites existing files.
- CI templates for third parties: `harness`, `auto-merge` and `quality` workflows, two rulesets (two-account, single-account) and an `escape` issue template are installed under `.github/`. Project checks are read from `checks.toml`; the approval App's variable, secret and environment names and the approval mode are configured under the new `[platform]` section (`approval = "app" | "none"`); single-account mode is documented with its risks in SECURITY.md.
- `base-tests` no longer fails a project that has no `tests/` directory at the base commit.
- **Migration:** the delivery metrics and weekly report no longer print the built-in "v0.8.0 baseline" (it was the originating project's history). A project that wants a baseline line registers it under `[metrics.baseline]` (`label`, `values`) in `checks.toml`.
- `upgrade` refuses an engine checkout whose commit is not on `origin/main` (override: `--allow-dirty`) and prints the entries that start with `**Migration:**` of this changelog newer than the project's current lock; procedure in `docs/upgrading.md`.
- The CI workflows that decide "CI passed" and count CI rounds (`dispatch`, merge routing, delivery metrics, weekly report) are configurable (`rules.toml` `[dispatch] ci_workflows`, default `["harness"]`) instead of the hard-coded name `build`; dispatch waits for every listed workflow on the pushed commit.
- **Migration:** a project whose CI workflow is not named `harness` (for example one that still uses `build`) must set `[dispatch] ci_workflows` in `.harness/config/rules.toml`, otherwise dispatch waits for a workflow that does not exist.
- Migration aid: when `origin/main` still has the flat `harness/` layout, the git guard and dispatch read rules and export guards from the old location. Remove once all consumers have migrated.

Known limits, planned next: English messages and prompts (i18n), configurable directory conventions and default branch, TypeScript language plugin, project adoption with detection (`adopt`), end-to-end tracing (observability), executor host adapters beyond Pi.
