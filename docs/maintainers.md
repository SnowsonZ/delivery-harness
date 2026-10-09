# 维护者工作流

适用范围：本仓库维护者及其 Agent（设计方、执行方、评审方）。普通贡献者只需 [AGENTS.md](../AGENTS.md)。待办与计划文档里的「用户」即维护者。

## 身份与推送

- 推送、开 PR、评论用 Agent 账号：`bin/as-agent <命令>`（身份取自 `.harness/config/checks.toml [identity]`）。Agent 不合并、不批准 PR；main 的 ruleset 要求由非推送者批准最后一次推送。
- 推送后用 `git ls-remote origin refs/heads/<分支>` 核对与 HEAD 一致再报告（网络不稳时推送可能静默失败）。
- 版本号与 tag 只由维护者决定；发版前同步 `engine/__init__.py` 的 `__version__` 与 CHANGELOG。

## 合并路由与自治试验

- 放权以 `.harness/config/autonomy.toml` 为准：按类别由批准 App 自动合并；R2 实现 PR 满足合同制条件时也可自动合并（B81，仅本仓库试验，设计见 `docs/plans/2026-10-03-autonomy-trial-design.md`）；其余由维护者批准合并。
- AGENTS.md、本文件、规格与护栏路径不在合同制白名单内，始终由维护者审。

## 角色与派发

- 设计方（Claude Code 或 Codex）写设计与任务书、做设计方复核；执行方 Pi 经 `bin/dispatch` 在槽位工作树里实现。
- 独立评审须由与设计方不同家的评审方做：派发的任务 PR 在 CI 通过后由 dispatcher 按 `rules.toml [review] chain` 自动接上；设计方直接开的 PR（如文档、升级）用 `bin/harness review pr <n> [--reviewer <评审方>]` 手动发起，它只用指定或配置的评审方，不沿 chain 换家。评审方只拿材料路径自行读取，不内联发送正文。
- 一个设计拆成多个派发任务时按 [docs/task-splitting.md](task-splitting.md)：先跑通第一个任务，再拆完其余并做追溯表与独立拆分评审，一并提交。
- `docs/runs/<任务书名>/<序号>.json` 记录一次派发尝试，只由 dispatcher 生成：不手改 exit，历史 stopped/clarify 保留；完成证据是成功记录加 CI、PR、合并资料。
- 续派前确认没有存活的旧执行方占用槽位，核对实际等待对象与输出；父编排进程退出不代表其 Pi 子进程结束（B77）。

## 自举升级与消费方验证

- 首个使用方是 [Agent-Notification](https://github.com/SnowsonZ/Agent-Notification)；抽离决定见其 `docs/decisions/0001-harness-extraction.md`。升级已接入的项目按 [docs/upgrading.md](upgrading.md)。
- CI 的 `consumer-contract` 用本次引擎升级 Agent-Notification 并跑其 `bin/verify --full`（不是必过项，须看结果；Linux 上跳过 Swift 等 macOS 专属检查）。
- 改 Swift 解析、变异、质量指标或回放时，另在 Agent-Notification 的独立 worktree 中运行 `python3 <本仓库>/engine/cli.py upgrade --target <worktree> --allow-dirty` 并跑 `bin/verify --full`，比对测试数、质量指标、变异得分不变。不在其主检出目录试装。
- 平台设置（ruleset、App、secrets、Actions 权限）由维护者执行，Agent 只准备命令与只读核对。

## 本机环境

- 新 worktree 可能默认选到 Python 3.14，提交钩子会因缺 ruff 或 B125 失败：改用 3.12 解释器（如 pyenv 设 `PYENV_VERSION=3.12.x`）。
