---
task: T008
class: K7
risk: R3
designer: claude-code
size: large
architecture: true
spec_refs: []
no_spec_reason: 护栏与流程改动（harness 迁到独立引擎），验收写在 docs/specs/delivery-harness.md 与本任务书中，没有产品规格验收编号
budget:
  wall_clock_min: 600
  ci_rounds: 3
  retries: 2
  tokens: null
rollback: git revert（回到 harness/ 平铺布局；引擎仓库不受影响）
---

> 状态：迁自 Agent-Notification（2026-10-09 快照，正文未改；业务库不再留存 harness 抽取与设计文档，只留引用）。文中指向业务库专属文件的链接已改为该仓库的绝对地址。

# 任务：harness 迁到独立引擎 delivery-harness（v0.1.0）

状态：设计评审方自行实现（2026-09-29，方案 B）。来源：用户 2026-09-29 决定把 harness 抽成独立开源仓库 [delivery-harness](https://github.com/SnowsonZ/delivery-harness)，先让本仓库作为第一个使用方迁过去，再装到第二个项目；安装形态为带锁文件的内置副本，引擎不带任何使用者自己的默认值。

## 目标终态

- `harness/` 删除；引擎以内置副本装在 `.harness/engine/`，`.harness/engine.lock` 记录版本、引擎提交与目录树哈希，`bin/verify` 的 integrity 检查拒绝绕过升级的改动。
- 本项目的规则与配置在 `.harness/config/`（`rules.toml`、`autonomy.toml`、新增 `checks.toml`：Agent 身份、运行时、源码与语言、verify 检查清单、发版版本文件、变异目标、回放套件、周报机器人），棘轮状态在 `.harness/state/`，事故回放用例在 `.harness/project/replay_cases.py`。
- 所有入口经 `bin/harness <子命令>`：`bin/verify`、`bin/dispatch`、`bin/as-agent`、git 钩子、5 个 Agent 工具的守卫钩子与 CI 工作流都改为调用 `.harness/engine/cli.py`。
- 行为不变：harness 测试数、质量指标、变异得分与迁移前完全相同。

## 非目标与禁止动作

- 不改判定逻辑与阈值；不放宽任何规则。
- 不在本任务中做 `adopt`（探测接入）、可观测性、国际化、可配置目录约定、TypeScript 插件（登记为待办）。
- harness 测试（`tests/test_harness*.py`）暂留本仓库，作为引擎在真实项目上的契约测试；移植到引擎仓库另行处理。

## 前置条件（不满足就停下报告）

- 引擎仓库 `restructure/v0.1` 分支的提交已推送，锁文件中的提交能在引擎仓库找到。

## 验收

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或验证步骤） |
|---|---|---|---|
| 不挂规格：护栏 | harness 测试在新布局下全部通过，数量与迁移前相同（248 个，跳过 1 个） | 单测 | `tests.test_harness` |
| 不挂规格：护栏 | 守卫从 origin/main 的新布局读规则、导出守卫；main 仍为旧布局时回退 | 单测 | `tests.test_harness_guard.TamperedRulesTest`、`tests.test_harness_dispatch.GuardBundleTest` |
| 不挂规格：护栏 | 质量指标与变异得分与迁移前相同 | CI 断言 | `bin/harness quality`、`bin/harness mutate --check` |
| 不挂规格：护栏 | 引擎完整性：改动 `.harness/engine/` 任一文件后 integrity 失败 | 人工 | `bin/harness integrity` |

## 步骤与提交顺序

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 装入引擎与配置，删除 `harness/` | `.harness/engine.lock`、`.harness/config/checks.toml`、`bin/harness` | `bin/verify --full` | 第 1、3、4 行 |
| 2 | 入口、钩子与工作流改调新入口 | `bin/verify`、`bin/dispatch`、`bin/as-agent`、`.githooks/pre-commit`、`.claude/settings.json`、`.github/workflows/build.yml` | `bin/verify` | 第 2 行 |
| 3 | 测试与现役文档改到新布局 | `tests/test_harness.py`、`docs/specs/delivery-harness.md`、`AGENTS.md`、`docs/plans/backlog.md` | `bin/verify` | 第 1 行 |

## 升级包

需要改合同或遇到行为差异时停下，写明差异与证据交用户决定。
