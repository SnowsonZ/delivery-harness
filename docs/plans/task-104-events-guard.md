---
task: T104
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

# T104：命令守卫与 git 守卫拒绝埋点

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

仅在已有 evaluate/钩子拒绝结果的报告边界记事件，避免纯规则函数被重复调用时重复计数。guard.command 含稳定规则键、角色、工具类别、可安全提取的仓库相对目标；guard.git 含 pre-commit/pre-push/reference-transaction、分支、规则键。原始拒绝理由含命令时只取规则标识，不复制命令。
放行路径不逐条写事件、不写计数文件；Pi 执行环节的放行数由 T105 从工具完成事件汇总。设计方放行不记。不可安全提取的目标省略，不把绝对路径清洗成貌似可信的相对路径。拒绝信息、stdin 消费、退出码不变。

## 白名单

- `engine/guards/command_guard.py`
- `engine/guards/git_guard.py`
- `tests/test_events_guard.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T102 已合并且工作区基于含依赖的最新main；T101为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_events_guard.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T104 | 命令与编辑拒绝各含规则键、角色、类别和安全相对目标；数据库扫描无命令/正文/绝对路径 | 夹具 | `tests.test_events_guard.ObservabilityTaskTest.test_command_denial_metadata_without_payload` | 复制原拒绝载荷或缺规则角色时失败 |
| 不挂规格：B46 T104 | 三类 git 钩子拒绝夹具逐个核对 hook、分支、规则与 deny 状态 | 夹具 | `tests.test_events_guard.ObservabilityTaskTest.test_all_git_hooks_record_denial` | 任一拒绝入口未埋点则事件缺失 |
| 不挂规格：B46 T104 | 设计方与执行方各若干放行调用，断言新增事件数为零 | 夹具 | `tests.test_events_guard.ObservabilityTaskTest.test_allow_does_not_emit` | 逐条记录放行时行数非零 |
| 不挂规格：B46 T104 | 关闭、写入失败时同一拒绝的 JSON/文本与退出码不变 | 夹具 | `tests.test_events_guard.ObservabilityTaskTest.test_sink_failure_preserves_denial` | 吞拒绝或追加业务输出时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `engine/guards/command_guard.py`、`engine/guards/git_guard.py`、`tests/test_events_guard.py` | `python3 -W error::ResourceWarning -m unittest tests.test_events_guard.ObservabilityTaskTest.test_command_denial_metadata_without_payload tests.test_events_guard.ObservabilityTaskTest.test_all_git_hooks_record_denial tests.test_events_guard.ObservabilityTaskTest.test_allow_does_not_emit tests.test_events_guard.ObservabilityTaskTest.test_sink_failure_preserves_denial -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_events_guard.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
