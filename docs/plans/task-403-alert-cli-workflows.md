---
task: T403
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

# T403：告警汇总命令与工作流末尾步骤

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

注册 alert（--pr/--trace/--json，无PR则复用升级议题）；扩展 T205 C4：完整性失败、review不通过/评审方失败、派发卡死/预算/打转、CI最后一轮、守卫拒绝阈值、误差预算拒自动合并、audit缺环节/锚点失配全部有稳定 reason键。
按下文F3分工：harness仅push main末尾、auto-merge的默认分支workflow_run独立告警job在对应状态结束后if:always()运行；所有pull_request检查保持只读，不沿用judge仅success门槛漏掉失败CI。
review在已有评论发布后调用C4补标签/更新告警，不第二次运行评审。同trace/reason查远端评论标记幂等，库去重只是缓存，换机重跑也不新增；发布失败不改变原路由/检查/评审退出码。未知事件不自动触发告警，不把旧全部历史拒绝当本轮突增。
告警承载明确分工（F3）：harness.yml仅push main的默认分支执行可在末尾调用alert；所有pull_request事件的job/step令牌权限保持只读，同仓库PR与forkPR一视同仁，不在PR定义里授write。PR成功或失败均由auto-merge.yml的workflow_run独立告警job承载，其if条件不继承judge的success门槛，默认分支checkout/引擎、只把PR head/artifact当数据，最小issues/pull-requests:write与actions/contents:read。不能仅在harness.yml加一个默认分支checkout来使PR可修改的job定义拥有写权限。
CHANGELOG.md「Unreleased」补记 alert 子命令与工作流模板告警步骤（英文，含触发家族与 F3 承载分工；无 Migration 条目——模板随 upgrade 同步，消费方无需手动迁移；2026-10-02 派发前核对补授权，检查单第3项，与 T401/T402 同款缺口）。

## 白名单

- `engine/cli.py`
- `engine/core/alerts.py`
- `engine/agents/review.py`
- `templates/.github/workflows/harness.yml`
- `templates/.github/workflows/auto-merge.yml`
- `CHANGELOG.md`（Unreleased 记 alert 子命令与工作流告警步骤；用户可见行为变化）
- `tests/test_alert_cli.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T402 已合并且工作区基于含依赖的最新main；T101为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_alert_cli.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T403 | 设计5七行触发分别断言reason/trace/目标/标签，正常与无预算配置不误报 | 夹具 | `tests.test_alert_cli.ObservabilityTaskTest.test_all_seven_trigger_families` | 任何触发漏接或历史误触发时失败 |
| 不挂规格：B46 T403 | 删除本机缓存后重跑仍更新一条远端评论；两reason两条，无PR复用议题 | 夹具 | `tests.test_alert_cli.ObservabilityTaskTest.test_remote_marker_dedup_survives_new_machine` | 只靠本机DB去重或reason混用时失败 |
| 不挂规格：B46 T403 | 模板结构与假步骤重放确认失败harness/路由后执行告警、默认分支代码、最小write权限；分别重放同仓库PR/forkPR，所有pull_request的job及继承权限均无write；push main与workflow_run失败分别走指定承载 | 夹具 | `tests.test_alert_cli.ObservabilityTaskTest.test_workflows_run_on_failure_and_use_trusted_code` | judge success门槛漏失败或fork PR引擎持write token时失败；同仓库PR取得write也必须失败 |
| 不挂规格：B46 T403 | review否决/verify失败/route拒绝在告警API出错仍维持原结论，报告告警失败 | 夹具 | `tests.test_alert_cli.ObservabilityTaskTest.test_publish_error_preserves_original_result` | 外部评论异常遮盖原结果或转成功时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `engine/cli.py`、`engine/core/alerts.py`、`engine/agents/review.py`、`templates/.github/workflows/harness.yml`、`templates/.github/workflows/auto-merge.yml`、`tests/test_alert_cli.py` | `python3 -W error::ResourceWarning -m unittest tests.test_alert_cli.ObservabilityTaskTest.test_all_seven_trigger_families tests.test_alert_cli.ObservabilityTaskTest.test_remote_marker_dedup_survives_new_machine tests.test_alert_cli.ObservabilityTaskTest.test_workflows_run_on_failure_and_use_trusted_code tests.test_alert_cli.ObservabilityTaskTest.test_publish_error_preserves_original_result -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_alert_cli.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
