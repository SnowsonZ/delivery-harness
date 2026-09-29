---
task: T205
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

# T205：派发预警与升级时间线

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

先定义共用告警 API C4（T403 扩展），dispatch 仍以已知的 Attempt/当前 CI 轮次触发，不读事件作预算判定。最后一轮 CI 即将运行时预警（总预算=1 则首轮即预警），不能跑过预算后才提醒。
可选 rules.toml [alerts] guard_denials_threshold=3：每轮被拒工具数达到阈值即触发；阈值是轮内绝对计数，避免没有历史基线仍无法定义突增。非正/非法配置禁用该预警并提示配置错误，不改执行预算。计数从 T105 的实际工具流来。
已有 stall/timeout/loop/retries/clarify/CI 耗尽升级包补 trace_id、安全阶段摘要和时间线入口（有 PR 指向 PR/CI artifact，无 PR 指向升级议题与 trace 命令），无需新通知渠道。告警发布失败保留原状态与本机 alert.error，不吞原升级。
体量约束（F1）：阈值/末轮条件与安全告警详情组装下沉本任务新建且已列白名单的alerts.py中的内部纯函数；dispatch.py调用已有结果，不新增深分支树，不上调质量基线。原等待/预算条件仍在原判定路径，告警条件不得反过来控制它。

## 白名单

- `engine/agents/dispatch.py`
- `engine/agents/dispatch_host.py`
- `engine/core/alerts.py`
- `templates/.harness/config/rules.toml`
- `tests/test_dispatch_alerts.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T203 已合并且工作区基于含依赖的最新main；T101为已有基础。本任务与 T204 并行派发：白名单不交叉，预警不消费 T204 的保留期配置；逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_dispatch_alerts.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T205 | 预算 3 与 1 两组调用序列断言预警发生在最后 wait_ci 前，原等待/推送数不变 | 夹具 | `tests.test_dispatch_alerts.ObservabilityTaskTest.test_last_ci_round_warns_before_wait` | 超限后才预警或漏预算1时失败 |
| 不挂规格：B46 T205 | 阈值-1/阈值/阈值+1，双拒绝理由算一次，非法配置不启动预警 | 夹具 | `tests.test_dispatch_alerts.ObservabilityTaskTest.test_guard_threshold_uses_denied_tools` | 用理由计数或越阈值规则不明确时失败 |
| 不挂规格：B46 T205 | 同 trace/reason 重跑更新已有标记；无 PR 创建或复用议题；都有 escalation 标签/时间线 | 夹具 | `tests.test_dispatch_alerts.ObservabilityTaskTest.test_alert_and_escalation_share_one_comment` | 预警与升级各新建一个评论/议题或缺标签时失败 |
| 不挂规格：B46 T205 | 假 gh 发布失败时记录观察错误但不改派发退出码、不放宽预算 | 夹具 | `tests.test_dispatch_alerts.ObservabilityTaskTest.test_publish_failure_preserves_original_exit` | 发布异常遮盖原 Stop/失败或变成功时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `engine/agents/dispatch.py`、`engine/agents/dispatch_host.py`、`engine/core/alerts.py`、`templates/.harness/config/rules.toml`、`tests/test_dispatch_alerts.py` | `python3 -W error::ResourceWarning -m unittest tests.test_dispatch_alerts.ObservabilityTaskTest.test_last_ci_round_warns_before_wait tests.test_dispatch_alerts.ObservabilityTaskTest.test_guard_threshold_uses_denied_tools tests.test_dispatch_alerts.ObservabilityTaskTest.test_alert_and_escalation_share_one_comment tests.test_dispatch_alerts.ObservabilityTaskTest.test_publish_failure_preserves_original_exit -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_dispatch_alerts.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
