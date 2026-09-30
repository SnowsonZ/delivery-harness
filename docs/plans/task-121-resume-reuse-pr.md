---
task: T121
class: K7
risk: R3
designer: codex
size: small
architecture: true
spec_refs: []
no_spec_reason: 待办 B70 的派发器健壮性修复（resume 复用既有 PR），无产品规格验收编号
budget:
  wall_clock_min: 90
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T121：--resume 复用既有 PR（B70）

负责方：**派发任务**。设计方：codex 席位（现由 Zcode 代行）；独立评审方 OpenCode。依据：待办 B70（T203/T119 两例实测：resume 在分支已有 PR 时 `gh pr create` 报错崩溃，派发进程死在最后一步、升级与收尾全部丢失）；[共用合同](2026-09-29-observability-task-contracts.md)C0。

## 目标终态

`dispatch` 的 push 后开 PR 环节（`run` 循环内 `pr is None` 分支）：开新 PR 前先查询该分支是否已有开放 PR（`gh pr list --head <branch>`），存在则复用其编号（后续 CI 轮次反馈照旧注入既有 PR），不存在才新建。首轮派发（非 resume）同样受益。既有无分支 PR 的行为逐字不变；查询失败（网络）按「无既有 PR」处理并保留原新建路径。

## 白名单

- `engine/agents/dispatch.py`
- `tests/test_run_timeline.py`（仅限假体方法新增：FakeGitHub 补 `existing_pr`，见修订记录）
- `tests/test_events_agents.py`（新增断言；不删除、不修改既有断言——与 T202 对本文件的单点修订不冲突）

## 非目标

不实现其他任务；不实现 B71/B72；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0同样适用。

## 前置条件

- 工作区基于含依赖的最新main。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- 与同轮其他任务白名单不交叉。
- 工具-only、不改既有引擎行为路径：G2 免（同 T118 先例）；设计方以真实 resume 场景核对一次（人工）。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；差异先修订任务书并经原审批。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B70 | 假 GitHub 记录：分支已有开放 PR 时 `open_pr` 不被调用、既有编号被复用进后续反馈 | 夹具 | `tests.test_events_agents.ObservabilityTaskTest.test_resume_reuses_existing_pr` | 仍新建 PR（重复报错）时失败 |
| 不挂规格：B70 | 无既有 PR 时行为逐字不变（新建路径与编号） | 夹具 | `…test_no_existing_pr_unchanged` | 新建路径被改动时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在用户授权派发后，执行方按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 既有 PR 复用与回归断言 | `engine/agents/dispatch.py`、`tests/test_events_agents.py` | `python3 -W error::ResourceWarning -m unittest tests.test_events_agents.ObservabilityTaskTest.test_resume_reuses_existing_pr tests.test_events_agents.ObservabilityTaskTest.test_no_existing_pr_unchanged -v` | 本任务验收全部行 |
| 2 | 验证范围，整理交付证据 | `tests/` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收与定向变异复核（去掉既有 PR 查询，复用用例必须失败）。设计方做真实 resume 场景核对（人工）。同一失败连续三轮无新证据时停止该路径；不得自行扩大白名单、改原任务书、实现非目标。

## 修订记录（2026-09-30，设计方裁决首轮升级）

首轮实现正确但停滞于结构冲突：`dispatch.py` 796/800 行、`main()` C901 10 顶格，B70 实现无空间（T120 同源裁决先例）；且 `tests/test_run_timeline.py` 的 FakeGitHub 假体缺 `existing_pr`，冻结断言 AttributeError（假体接口对齐属 T105 先例）。裁决：

- **前置依赖 T123**（GitHub 类抽离至 `engine/agents/github.py`，`dispatch.py` 降至 ~650 行）先行合并；本轮第 2 次（`--resume`）在 T123 之上实现：`existing_pr(branch)` 查询落在 `github.py` 的 GitHub 类，`run` 循环调用复用。
- **授权假体更新**：`tests/test_events_agents.py` 与 `tests/test_run_timeline.py` 中的假 GitHub 补 `existing_pr` 方法（只增不改既有断言）。
- 实现位置与白名单按此调整；其余目标终态、验收表不变。
