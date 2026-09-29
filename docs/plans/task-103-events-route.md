---
task: T103
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

# T103：风险判级与路由埋点

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

在已有 classify/gather/decide 结果之外记录，不改变 FileRisk、RiskReport、Facts、Rule 的语义与决定。risk 逐文件事件带仓库相对路径、命中规则、等级；汇总带风险、R1 声明/降级、改动已有测试数。
policy.main 从 --branch（优先）、PR API headRefName（有 PR 且 branch 缺省）、current_trace（无 PR）获得 trace；传给风险采集与所有 route 事件。为 risk.classify 加可选 keyword-only trace_id=None（旧调用仍有效）；不是从 PR 正文取 ID。Facts、各 Rule、result 的字段见 C1；main 规则/配置用带提交的引用。事件写入与引用读取失败不得改变 auto_merge 或 GITHUB_OUTPUT。

## 白名单

- `engine/routing/policy.py`
- `engine/routing/risk.py`
- `tests/test_events_route.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T102 已合并且工作区基于含依赖的最新main；T101为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_events_route.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T103 | R0/R2/R3 文件、修改已有测试、R1 降级夹具核对逐文件命中规则与汇总 | 夹具 | `tests.test_events_route.ObservabilityTaskTest.test_risk_file_rules_and_summary` | 漏文件、漏降级或计数错时事件字段断言失败 |
| 不挂规格：B46 T103 | 允许与误差预算耗尽两组 Facts，核对每个 Rule、facts/result、决定者与配置提交哈希 | 夹具 | `tests.test_events_route.ObservabilityTaskTest.test_route_records_all_rules_and_facts` | 只记最终布尔或用 PR 配置代替 main 配置时失败 |
| 不挂规格：B46 T103 | CI 的 GITHUB_REF_NAME=main，--branch 或 PR API 提供另一分支；risk 与 route 的 trace 全为 PR 分支 | 夹具 | `tests.test_events_route.ObservabilityTaskTest.test_workflow_run_uses_pr_branch` | 沿用 main@ trace 或只修 route 未修 risk 时失败 |
| 不挂规格：B46 T103 | 库及 API 引用准备失败下原 Rule 列表、路由文本和输出文件逐字相同 | 夹具 | `tests.test_events_route.ObservabilityTaskTest.test_event_failure_does_not_change_policy` | 让事件影响决定或输出时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `engine/routing/policy.py`、`engine/routing/risk.py`、`tests/test_events_route.py` | `python3 -W error::ResourceWarning -m unittest tests.test_events_route.ObservabilityTaskTest.test_risk_file_rules_and_summary tests.test_events_route.ObservabilityTaskTest.test_route_records_all_rules_and_facts tests.test_events_route.ObservabilityTaskTest.test_workflow_run_uses_pr_branch tests.test_events_route.ObservabilityTaskTest.test_event_failure_does_not_change_policy -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_events_route.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
