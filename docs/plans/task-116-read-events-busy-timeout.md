---
task: T116
class: K7
risk: R3
designer: codex
size: small
architecture: true
spec_refs: []
no_spec_reason: 待办 B69 的健壮性小修（读取连接参数），无产品规格验收编号
budget:
  wall_clock_min: 60
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T116：run_timeline 读取沿用 busy_timeout

负责方：**派发任务**。设计方：codex 席位（现由 Zcode 代行）；独立评审方 OpenCode。依据：待办 B69（PR #43 评审一般级发现）；[共用合同](2026-09-29-observability-task-contracts.md)C0。

## 目标终态

`engine/agents/run_timeline.py` 的 `_read_events`（及其他直接 `sqlite3.connect` 的读取点，如有）改用 `events_db._connect`（或等价设置 `busy_timeout=5000`），使库锁竞争或恢复窗口内的读取等待而非立即失败；读取失败时的既有退化行为（空摘要、方向安全）保持不变。公共签名不变；既有断言只增不改。

实现前先核对 `_read_events` 现状与 `events_db._connect` 的可见性，差异停下报告设计方，不自行补实现。

## 白名单

- `engine/agents/run_timeline.py`
- `tests/test_run_timeline.py`（新增断言；不删除、不修改既有断言——注意与 T202 对同一文件的单点修订不冲突：T116 只新增，不改既有断言）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0同样适用。

## 前置条件

- 依赖 T201 已合并且工作区基于含依赖的最新main。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- 与 T202/T203/T115 白名单不交叉（对 `tests/test_run_timeline.py` 只增不改，与 T202 的单点修订分属不同断言）。
- 改派发消费路径：设计方将做等价验证（G2，556 计数对照）。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；差异先修订任务书并经原审批。

## 验收

具名测试为本任务要新增的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B69 | 持有写事务期间调用记录时间线组装：读取等待后成功，时间线与锚点完整（或按既有退化路径安全为空），不抛 `database is locked` | 夹具 | `tests.test_run_timeline.ObservabilityTaskTest.test_read_events_waits_for_lock` | 裸 connect 立即抛锁异常时失败 |
| 不挂规格：B69 | 既有 4 条时间线断言逐字通过（行为不变对照） | 夹具 | 既有 `ObservabilityTaskTest` 全类 | 读取行为被改变时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在用户授权派发后，执行方按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 读取连接参数与新增断言 | `engine/agents/run_timeline.py`、`tests/test_run_timeline.py` | `python3 -W error::ResourceWarning -m unittest tests.test_run_timeline.ObservabilityTaskTest -v` | 本任务验收全部行 |
| 2 | 验证范围，整理交付证据 | `tests/` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收与定向变异复核（去掉 busy_timeout 设置，锁用例必须失败）。设计方做 G2（556 计数对照）。同一失败连续三轮无新证据时停止该路径；不得自行扩大白名单、改原任务书、实现非目标。
