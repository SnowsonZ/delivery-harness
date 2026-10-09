# 升级已接入的项目

适用：把本仓库的引擎升级到某个已接入的业务仓库（例如 Agent-Notification）。升级只整体替换 `.harness/engine/` 与 `.harness/engine.lock`；`.harness/config/`、`state/`、`project/`、`bin/`、`.githooks/`、各 Agent 钩子、`.github/` 一概不动（模板只在 `install` 时写入，已有文件不覆盖）。

## 步骤

1. **引擎仓库**：`git checkout main && git pull`，工作区干净。`upgrade` 拒绝不在 `origin/main` 上的提交与未提交改动（开发调试才用 `--allow-dirty`，锁文件会标 `-dirty`）。
2. **业务仓库**：开独立 worktree 与分支（主目录只留给用户）。
3. **升级**：`python3 <引擎仓库>/engine/cli.py upgrade --target <worktree>`。输出会列出 CHANGELOG 中 Unreleased 与晚于当前锁文件版本的 `**Migration:**` 条目；版本未更新时 Unreleased 仍提示。
4. **处理迁移项**：逐条照做（通常是补 `.harness/config/` 里的新配置）。模板的新版本（工作流、钩子）不会自动同步，CHANGELOG 有变化时对照合并。
5. **验证**：worktree 里 `bin/verify --full`，全绿。
6. **提交**：用 Agent 账号提交、推送、开 PR；改 `.harness/` 属于 R3，由用户批准合并。

## 可观测性迁移核对

只替换引擎后，新增 events/trace/audit/alert 命令即可用；CI 包、账本和告警仍取决于项目实际工作流与平台设置。

| 项目 | 使用方操作 | 验收证据 |
|---|---|---|
| 工作流 | 对照引擎仓库 `templates/.github/workflows/` 手工合并 harness、auto-merge、quality，保留项目 Prepare 步骤 | full-history 检出、判定 run-name、唯一包名、90天、失败后 export/upload/summary/alert 条件与只读 PR job 一致 |
| 可信发布 | 核对默认分支的 ledger/alert 代码来源及 job 级权限；writer 要有 Git 身份和临时克隆凭据 | 实际主分支 job 日志成功；令牌只有相应 contents/pull-requests/issues/actions 权限 |
| 账本 ruleset | 首次安装模板已提供；旧安装可从模板补 `.github/rulesets/harness-audit.json`，由用户导入或更新 | active、覆盖账本分支、禁删除/改写历史、bypass 列表符合预期；普通覆盖仍由 writer/审计检测 |
| 可选配置 | checks 的 events/audit 与 rules 的 alerts 按实际需要选择；保持默认无需新增键 | README 默认值与对应 `--help` 一致，audit 配置不影响原判定 |
| 真实复原 | 合并后检查账本与 PR 锚点，按任务合同完成独立评审与验收 | trace重复导入新增0、audit适用检查通过、记录/账本/API绑定匹配；过期/缺失不能记通过 |

首次成功 writer 会创建账本分支，无需手工填充假账本。平台规则应用与权限决定由用户操作；Agent只读核对。权限403等不足按未核验列出，由用户已有授权访问确认，不扩大Agent凭据。

本机产物/终止原始流默认30天、CI包模板90天；事件/引用/锚点与Git账本没有自动保留期删除。迁移不回填尚无证据的历史PR，关闭未合并PR仍不纳入账本。T502的信息性旧head修复没有单独迁移动作。

## 发版方的义务

- 改变配置项、命令接口或模板与引擎的配合方式时，在 CHANGELOG 对应版本段写一条以 `**Migration:**` 开头的条目，写明业务仓库要做什么；`upgrade` 靠它提示。
- 改动判定、守卫、派发、配置读取时，先在业务仓库的 worktree 里升级并跑 `bin/verify --full`。本仓库由 CI 的 `consumer-contract` 对 Agent-Notification 自动完成这一步，CI 覆盖不到、仍需本地比对的情形见 [docs/maintainers.md](maintainers.md)。

## 已知缺口

配置缺口不会自动检测，只有 CHANGELOG 提示（待办 B51）；`bin/` 脚本与引擎命令接口不兼容时也只能靠 Migration 条目提醒。
