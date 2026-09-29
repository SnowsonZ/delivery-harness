---
task: T110
class: K7
risk: R3
designer: codex
size: small
architecture: true
spec_refs: []
no_spec_reason: 已审定的可观测性设计与本次任务书是B46合同，本仓库没有对应产品规格验收编号
budget:
  wall_clock_min: 45
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T110：emit 写入值清洗口径修正

负责方：**派发任务**。设计方：codex 席位（现由 Zcode 代行，见执行计划 §1 分工 2026-09-29 修订）；独立评审方 OpenCode。依据：T109 独立评审发现①（待办 B63）、[共用合同](2026-09-29-observability-task-contracts.md)C0。先读共用合同C0，再读本任务引用的接口；白名单与验收不能由执行方扩大或缩减。

## 目标终态

`emit` 的校验与写入使用同一 step 值：`_build_payload` 收到 strip 后的 `step_text`，库中 step 列不再出现首尾空白（含换行）的原值，「非法整条不写、合法写清洗值」的口径在边界一致。补负例断言：step 含首尾空白（含换行）时事件可写，且 payload 的 step 列等于 strip 后的值。不改其他列、不改公共签名；既有断言只增不改。

## 白名单

- `engine/core/events.py`
- `tests/test_events_hardening.py`（新增断言；不删除、不修改既有断言）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试的其他断言、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T109 已合并且工作区基于含依赖的最新main。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- 与 T102–T105、T108 并行派发：白名单不交叉，不调用其新增接口。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以"按实际代码"为由变更合同。

## 验收

具名测试为本任务要新增的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T110 | step " ok \n"（首尾空白与换行）可写事件，库中 step 列等于 "ok"；step "a\nb"（内部换行）仍整条不写 | 夹具 | `tests.test_events_hardening.ObservabilityTaskTest.test_step_written_value_is_stripped` | `_build_payload` 仍收 `str(step)` 原值时，step 列断言失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在用户授权派发后，执行方按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | emit 改传 step_text，新增负例断言 | `engine/core/events.py`、`tests/test_events_hardening.py` | `python3 -W error::ResourceWarning -m unittest tests.test_events_hardening.ObservabilityTaskTest.test_step_written_value_is_stripped tests.test_events_hardening.ObservabilityTaskTest.test_step_span_refs_and_input_io_fail_safe -v` | 本任务验收全部行 |
| 2 | 验证范围与失败隔离，整理交付证据 | `tests/test_events_hardening.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。设计方另做逐行验收与本任务断言的变异复核（把 `_build_payload` 改回收 `str(step)` 的定向变异，必须被新断言抓住）。同一失败连续三轮无新证据时停止该路径；不得自行扩大白名单、改原任务书、实现非目标。
