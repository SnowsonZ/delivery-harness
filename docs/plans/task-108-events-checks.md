---
task: T108
class: K7
risk: R3
designer: codex
size: large
architecture: true
spec_refs: []
no_spec_reason: 已审定的可观测性设计与本次任务书是B46合同，本仓库没有对应产品规格验收编号
budget:
  wall_clock_min: 180
  ci_rounds: 3
  retries: 2
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T108：其余检查的结构化输出埋点

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

只包住现有结构化结果；逐项补设计 3.1、3.3：taskbook.admit 的路径@提交/哈希、通过与问题规则/计数；hygiene 违规条数与规则名；base_tests 是否通过、是否强制与安全摘要；mutation 目标/得分/基线；evidence 每个缺陷有无提交、是否改代码、前败后过；run_check 每个 Finding 的规则/通过/原因引用；quality 各指标当前/基线；metrics 所有数值度量。
列表类结果按每个文件/规则/目标/缺陷/指标一条事件，汇总只含计数，避免往 outputs 放嵌套对象被过滤后信息丢失。完整日志只用本机产物引用；事件不存违规内容、缺陷正文或命令。run-check --github 仍是只报告而不是新拦截；与 T203 增加内容判定分开。

## 白名单

- `engine/checks/hygiene.py`
- `engine/checks/base_tests.py`
- `engine/checks/mutate.py`
- `engine/checks/evidence.py`
- `engine/checks/quality.py`
- `engine/checks/taskbook.py`
- `engine/routing/run_check.py`
- `engine/reports/metrics.py`
- `tests/test_events_checks.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T105 已合并且工作区基于含依赖的最新main；T101为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_events_checks.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T108 | 合法/非法任务书、两类卫生违规夹具核对引用哈希、规则/计数，不保存命中内容 | 夹具 | `tests.test_events_checks.ObservabilityTaskTest.test_taskbook_and_hygiene_metadata` | 入口事件替代内容事件或泄漏违规正文时失败 |
| 不挂规格：B46 T108 | 强制/不强制基线测试、两个变异目标、缺失/完整修复证据分别核对所有字段 | 夹具 | `tests.test_events_checks.ObservabilityTaskTest.test_base_mutation_and_evidence_details` | 目标/缺陷只记汇总或丢基线/前败后过字段时失败 |
| 不挂规格：B46 T108 | 每个 Finding、每个当前/基线质量指标、每个数值度量逐项断言 | 夹具 | `tests.test_events_checks.ObservabilityTaskTest.test_run_quality_and_metrics_details` | 漏任一项或把 list/dict 交 emit 后被丢弃时失败 |
| 不挂规格：B46 T108 | 八个模块的失败结果在关闭、写库失败时保持原输出/退出码 | 夹具 | `tests.test_events_checks.ObservabilityTaskTest.test_disabled_and_failed_sink_preserve_check_results` | 新增埋点改变判定或升高 run-check 退出码时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `engine/checks/hygiene.py`、`engine/checks/base_tests.py`、`engine/checks/mutate.py`、`engine/checks/evidence.py`、`engine/checks/quality.py`、`engine/checks/taskbook.py`、`engine/routing/run_check.py`、`engine/reports/metrics.py`、`tests/test_events_checks.py` | `python3 -W error::ResourceWarning -m unittest tests.test_events_checks.ObservabilityTaskTest.test_taskbook_and_hygiene_metadata tests.test_events_checks.ObservabilityTaskTest.test_base_mutation_and_evidence_details tests.test_events_checks.ObservabilityTaskTest.test_run_quality_and_metrics_details tests.test_events_checks.ObservabilityTaskTest.test_disabled_and_failed_sink_preserve_check_results -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_events_checks.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
