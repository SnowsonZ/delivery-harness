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
- Migration aid: when `origin/main` still has the flat `harness/` layout, the git guard and dispatch read rules and export guards from the old location. Remove once all consumers have migrated.

Known limits, planned next: English messages and prompts (i18n), configurable directory conventions and default branch, TypeScript language plugin, project adoption with detection (`adopt`), end-to-end tracing (observability), executor host adapters beyond Pi.
