---
task: T303
class: K7
risk: R3
designer: codex
size: large
architecture: true
spec_refs: []
no_spec_reason: 已审定的可观测性设计与本次任务书是B46合同，本仓库没有对应产品规格验收编号
budget:
  wall_clock_min: 150
  ci_rounds: 3
  retries: 2
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T303：工作流事件 artifact 与 summary

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

仅编辑模板，禁止改本仓库 .github/ 或 .harness/。harness/auto-merge 的事件导出与 upload、事件 summary 渲染放 if: always()；artifact 逻辑名 harness-events，多 job/run_attempt 的物理名称带唯一后缀，保留 90 天。harness 的 PR/head 与 route 的 --branch trace 对齐。
CI 临时数据库位于 git 公共目录，导出白名单 JSON 包与安全 JSON 内容产物，不上传 db/WAL/SHM 或本机原始日志。summary 从导出的 verify/route 事件渲染，保留所有原有机器输出与状态检查名；旧 summary 逐步替换只展示层。
新增 ci_events 模块仅被工作流调用（python .harness/engine/reports/ci_events.py 或 python -m 模式须支持安装布局），负责 export/summary，不新加 CLI 注册冲突。judge 默认分支执行引擎/模板，head 只当数据；不得下载并执行 PR 代码。合并后同步/账本步骤由 T304/T305 添加，末尾告警由 T403 添加。

## 白名单

- `engine/reports/ci_events.py`
- `templates/.github/workflows/harness.yml`
- `templates/.github/workflows/auto-merge.yml`
- `tests/test_ci_events_workflows.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T302 已合并且工作区基于含依赖的最新main；T101为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_ci_events_workflows.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T303 | 解析模板结构并用假命令重放步骤：verify 非零仍 export/upload/summary，原 job 仍失败 | 夹具 | `tests.test_ci_events_workflows.ObservabilityTaskTest.test_export_runs_after_check_failure` | 只正常路径导出或用 continue-on-error 抹平原失败时失败 |
| 不挂规格：B46 T303 | 两个 job/重跑产出不同物理 artifact 名；90天；上传集合精确等于安全 manifest | 夹具 | `tests.test_ci_events_workflows.ObservabilityTaskTest.test_artifact_allowlist_retention_and_run_identity` | 撞名、上传 *.db/原始日志或缺 retention 时失败 |
| 不挂规格：B46 T303 | 失败/跳过/路由拒绝事件的确定性 summary 含检查/规则、状态/理由引用，与原机器输出相符 | 夹具 | `tests.test_ci_events_workflows.ObservabilityTaskTest.test_summary_renders_event_results` | 继续用独立假汇总或漏失败/skip 时失败 |
| 不挂规格：B46 T303 | 临时 install 后调用 ci_events 能运行；模板 judge 只 checkout 默认分支、PR head 仅 fetch 当数据 | 夹具 | `tests.test_ci_events_workflows.ObservabilityTaskTest.test_trusted_route_and_installed_layout` | 模块 import 路径失效或执行 PR 引擎时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `engine/reports/ci_events.py`、`templates/.github/workflows/harness.yml`、`templates/.github/workflows/auto-merge.yml`、`tests/test_ci_events_workflows.py` | `python3 -W error::ResourceWarning -m unittest tests.test_ci_events_workflows.ObservabilityTaskTest.test_export_runs_after_check_failure tests.test_ci_events_workflows.ObservabilityTaskTest.test_artifact_allowlist_retention_and_run_identity tests.test_ci_events_workflows.ObservabilityTaskTest.test_summary_renders_event_results tests.test_ci_events_workflows.ObservabilityTaskTest.test_trusted_route_and_installed_layout -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_ci_events_workflows.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
