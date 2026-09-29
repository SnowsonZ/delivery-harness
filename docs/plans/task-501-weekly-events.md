---
task: T501
class: K7
risk: R3
designer: codex
size: medium
architecture: true
spec_refs: []
no_spec_reason: 已审定的可观测性设计与本次任务书是B46合同，本仓库没有对应产品规格验收编号
budget:
  wall_clock_min: 120
  ci_rounds: 3
  retries: 2
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T501：周报事件汇总与原指标对账

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

保留 collect/compute 现有GitHub与运行记录来源以及所有已有指标，只追加可观测性小节：阶段耗时、守卫拒绝、升级原因、审计发现数、missing_context分类计数。事件按周UTC时间/来源/hash去重，本机事件覆盖不完整要注明覆盖范围。
执行环节拒绝计数与逐条guard拒绝是同一批操作，周报拒绝总数采用 executor_round计数，设计方拒绝另列，不相加重复；与旧guard_denials按同任务/尝试对账，不拿所有本机历史与本周合并PR混比。
audit观察汇总以最新一次同PR/head结果、固定规则键去重；没有事件或事件无法读时新小节写无数据/不可用，旧小节字节内容和发布流程不变，不补造零。

## 白名单

- `engine/reports/weekly.py`
- `tests/test_weekly_events.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T403 已合并且工作区基于含依赖的最新main；T101为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_weekly_events.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T501 | 确定性周窗口事件核对阶段耗时与升级/缺上下文分类，边界与重复导入不双算 | 夹具 | `tests.test_weekly_events.ObservabilityTaskTest.test_stage_duration_escalation_and_context_totals` | 时间过滤或hash去重遗漏时失败 |
| 不挂规格：B46 T501 | 同任务的executor总数、guard细条与旧记录计数对账；设计方拒绝独列 | 夹具 | `tests.test_weekly_events.ObservabilityTaskTest.test_denials_reconcile_without_double_count` | 把两类同源计数相加导致翻倍时失败 |
| 不挂规格：B46 T501 | 重复审计同head不累加，更新head分别标明，类别计数准确 | 夹具 | `tests.test_weekly_events.ObservabilityTaskTest.test_audit_findings_dedup_latest_head` | 每次audit运行都加一次历史发现时失败 |
| 不挂规格：B46 T501 | 无库/坏库与完整库情况下旧周报小节逐字相同，新小节明确coverage与unavailable | 夹具 | `tests.test_weekly_events.ObservabilityTaskTest.test_existing_sections_unchanged_when_events_missing` | 改原数据来源或把不可得表示零时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `engine/reports/weekly.py`、`tests/test_weekly_events.py` | `python3 -W error::ResourceWarning -m unittest tests.test_weekly_events.ObservabilityTaskTest.test_stage_duration_escalation_and_context_totals tests.test_weekly_events.ObservabilityTaskTest.test_denials_reconcile_without_double_count tests.test_weekly_events.ObservabilityTaskTest.test_audit_findings_dedup_latest_head tests.test_weekly_events.ObservabilityTaskTest.test_existing_sections_unchanged_when_events_missing -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_weekly_events.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
