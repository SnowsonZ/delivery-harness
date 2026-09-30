---
task: T111
class: K7
risk: R3
designer: codex
size: small
architecture: true
spec_refs: []
no_spec_reason: 待办 B65/B57 的派发模板与观察旁路小修，本仓库没有对应产品规格验收编号
budget:
  wall_clock_min: 90
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T111：派发模板与 B65 残留小修

负责方：**派发任务**。设计方：codex 席位（现由 Zcode 代行，见执行计划 §1 分工 2026-09-30 修订）；独立评审方 OpenCode。依据：待办 B65、B57；[共用合同](2026-09-29-observability-task-contracts.md)C0。先读共用合同C0；白名单与验收不能由执行方扩大或缩减。

## 目标终态

1. `engine/cli.py` `_record_dispatch`：延迟导入 `from engine.core import events` 与 emit 调用纳入异常保护——导入失败（如引擎副本损坏）或 emit 异常时静默丢弃观察，原 SystemExit/业务异常/返回码与 stderr 逐字不变（C0 三态等价）。
2. `tests/test_events_verify.py` `test_reference_failure_keeps_check_result`：三态比对补 stderr（stdout、stderr、返回码分别比对，仅允许 T101 固定的一次写入失败提示行差异）——这是本任务唯一允许修改的既有断言。
3. `engine/agents/dispatch.py`：PR 标题不再重复任务 id 前缀（`_title` 结果已含「TXXX：」时不再拼接），补回归断言。
4. `engine/agents/dispatch.py` `pr_body`：「需要人工验收的部分」一节按任务书验收表条件化——验收表存在证据类型为「人工」的行时写现有指引，否则写「无」，补两种情形的回归断言（B57）。

## 白名单

- `engine/cli.py`
- `engine/agents/dispatch.py`
- `tests/test_events_verify.py`（新增导入失败用例；`test_reference_failure_keeps_check_result` 补 stderr 比对为唯一既有断言修改点）
- `tests/test_events_agents.py`（新增标题与 pr_body 断言；不删除、不修改既有断言）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑既有测试的其他断言、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T102、T105 已合并且工作区基于含依赖的最新main。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- 与 T106 并行派发：白名单不交叉。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以"按实际代码"为由变更合同。

## 验收

具名测试为本任务要新增/补强的断言；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T111 | events 导入失败时 `_record_dispatch` 静默丢弃，原 SystemExit/返回码与 stderr 逐字不变 | 夹具 | `tests.test_events_verify.ObservabilityTaskTest.test_record_dispatch_import_failure_preserves_outcome` | ImportError 顶替原异常/返回码时失败 |
| 不挂规格：B46 T111 | 引用失败三态的 stdout、stderr、返回码分别等价（stderr 仅容 T101 固定提示行） | 夹具 | `tests.test_events_verify.ObservabilityTaskTest.test_reference_failure_keeps_check_result` | 缺 stderr 比对时引入 stderr 差异的变异不被抓住 |
| 不挂规格：B46 T111 | 任务书标题已含任务 id 时 PR 标题不重复前缀 | 夹具 | `tests.test_events_agents.ObservabilityTaskTest.test_pr_title_has_no_duplicate_prefix` | 仍拼接出「TXXX：TXXX：」时失败 |
| 不挂规格：B46 T111 | 验收表有/无「人工」行时 pr_body 的人工验收一节分别写指引与「无」 | 夹具 | `tests.test_events_agents.ObservabilityTaskTest.test_pr_body_manual_section_conditional` | 固定文案时两种情形之一失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在用户授权派发后，执行方按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 四项小修与对应断言 | `engine/cli.py`、`engine/agents/dispatch.py`、`tests/test_events_verify.py`、`tests/test_events_agents.py` | `python3 -W error::ResourceWarning -m unittest tests.test_events_verify.ObservabilityTaskTest.test_record_dispatch_import_failure_preserves_outcome tests.test_events_verify.ObservabilityTaskTest.test_reference_failure_keeps_check_result tests.test_events_agents.ObservabilityTaskTest.test_pr_title_has_no_duplicate_prefix tests.test_events_agents.ObservabilityTaskTest.test_pr_body_manual_section_conditional -v` | 本任务验收全部行 |
| 2 | 验证范围与失败隔离，整理交付证据 | `tests/` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过（第 2 条既有断言补强除外）。设计方另做逐行验收与定向变异复核（如：去掉导入异常保护、恢复标题重复拼接、恢复固定文案，各自必须被对应断言抓住）。同一失败连续三轮无新证据时停止该路径；不得自行扩大白名单、改原任务书、实现非目标。
