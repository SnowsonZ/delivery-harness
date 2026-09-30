---
task: T122
class: K7
risk: R3
designer: codex
size: small
architecture: true
spec_refs: []
no_spec_reason: 待办 B71 的派发器健壮性修复（workflows 端点重试），无产品规格验收编号
budget:
  wall_clock_min: 60
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T122：wait_ci 的 workflows 端点重试（B71）

负责方：**派发任务**。设计方：codex 席位（现由 Zcode 代行）；独立评审方 OpenCode。依据：待办 B71（T118/T204 两例实测：`gh run list` 已有 3 次重试，但其内部拉取 Actions workflows 列表的调用不在重试覆盖内，TLS/EOF 即死，派发进程死在等 CI 阶段——产物已推送、PR 已开，仅失去自动重跑）；[共用合同](2026-09-29-observability-task-contracts.md)C0。

## 目标终态

`dispatch.GitHub._ci_runs` 的重试覆盖扩大到 workflows 列表拉取失败：`gh run list` 报错含 `couldn't fetch workflows`（或底层网络错误类别）时，同样按既有 `CI_QUERY_ATTEMPTS` 次数与间隔重试；重试耗尽才抛出。既有重试行为、查询参数与返回形状逐字不变；其他 gh 调用不受影响。

## 白名单

- `engine/agents/github.py`（`_ci_runs` 所在——T123 已将其从 dispatch.py 逐字抽离；本任务的目标修改点）
- `tests/test_ci_workflows.py`（新增断言；不删除、不修改既有断言）

## 非目标

不实现其他任务；不实现 B70/B72；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0同样适用。

## 前置条件

- 工作区基于含依赖的最新main。逐项核对实际接口（`_ci_runs` 在 `engine/agents/github.py` 与既有重试常量），差异停下报告设计方，不自行补实现。
- 与同轮其他任务白名单不交叉（与 T121 同文件 `dispatch.py`——**T121 与 T122 必须串行派发**）。
- 工具-only、不改既有行为路径：G2 免（同 T118 先例）。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；差异先修订任务书并经原审批。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B71 | `gh run list` 前两次失败（含 couldn't fetch workflows 错误文本）、第三次成功：`_ci_runs` 返回成功且重试次数符合 `CI_QUERY_ATTEMPTS` | 夹具 | `tests.test_ci_workflows.CiWorkflowsTest.test_workflows_fetch_retried` | 不重试立即抛出时失败 |
| 不挂规格：B71 | 重试耗尽仍失败：抛出行为与错误文本与现状一致 | 夹具 | `…test_retry_exhausted_raises` | 吞错或改变抛出语义时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在用户授权派发后，执行方按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 重试覆盖与回归断言 | `engine/agents/dispatch.py`、`tests/test_ci_workflows.py` | `python3 -W error::ResourceWarning -m unittest tests.test_ci_workflows.CiWorkflowsTest -v` | 本任务验收全部行 |
| 2 | 验证范围，整理交付证据 | `tests/` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收与定向变异复核（去掉重试分支，重试用例必须失败）。G2 免（同 T118 先例）。同一失败连续三轮无新证据时停止该路径；不得自行扩大白名单、改原任务书、实现非目标。


## 修订记录（2026-09-30，设计方裁决首轮评审「不通过」）

评审三条发现全部成立，处置：

- **阻断（引擎改动缺失 + 测试为文本变体）**：首轮未按目标终态修改 `_ci_runs` 的重试覆盖，新测试仅复述既有行为。第二轮**必须**按目标终态实现：`gh run list` 失败且错误文本含 `couldn't fetch workflows`（或底层网络错误类别）时按 `CI_QUERY_ATTEMPTS` 重试——修改点在 `engine/agents/github.py`（白名单已更正）。
- **阻断（接口不符未停下报告）**：任务书白名单原指向 `dispatch.py` 系 T123 抽离前的过时文本（设计方疏漏，本轮更正）；但执行方在 pack 显示接口不符时**未按前置条件停下上报**而自行绕过，违反合同——第二轮如再遇差异必须停下升级。
- **严重（diff 截断）**：= B74（T124 修复中，PR #79）。
- 验收表两行不变；重试错误类别的判定样本（couldn't fetch workflows / EOF / TLS）由执行方在测试中覆盖至少两类。
