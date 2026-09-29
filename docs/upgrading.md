# 升级已接入的项目

适用：把本仓库的引擎升级到某个已接入的业务仓库（例如 Agent-Notification）。升级只整体替换 `.harness/engine/` 与 `.harness/engine.lock`；`.harness/config/`、`state/`、`project/`、`bin/`、`.githooks/`、各 Agent 钩子、`.github/` 一概不动（模板只在 `install` 时写入，已有文件不覆盖）。

## 步骤

1. **引擎仓库**：`git checkout main && git pull`，工作区干净。`upgrade` 拒绝不在 `origin/main` 上的提交与未提交改动（开发调试才用 `--allow-dirty`，锁文件会标 `-dirty`）。
2. **业务仓库**：开独立 worktree 与分支（主目录只留给用户）。
3. **升级**：`python3 <引擎仓库>/engine/cli.py upgrade --target <worktree>`。输出会列出 CHANGELOG 中晚于当前锁文件版本的 `**Migration:**` 条目。
4. **处理迁移项**：逐条照做（通常是补 `.harness/config/` 里的新配置）。模板的新版本（工作流、钩子）不会自动同步，CHANGELOG 有变化时对照合并。
5. **验证**：worktree 里 `bin/verify --full`，全绿。
6. **提交**：用 Agent 账号提交、推送、开 PR；改 `.harness/` 属于 R3，由用户批准合并。

## 发版方的义务

- 改变配置项、命令接口或模板与引擎的配合方式时，在 CHANGELOG 对应版本段写一条以 `**Migration:**` 开头的条目，写明业务仓库要做什么；`upgrade` 靠它提示。
- 改动判定、守卫、派发、配置读取时，先在业务仓库的 worktree 里升级并跑 `bin/verify --full`（见 AGENTS.md）。

## 已知缺口

配置缺口不会自动检测，只有 CHANGELOG 提示（待办 B51）；`bin/` 脚本与引擎命令接口不兼容时也只能靠 Migration 条目提醒。
