---
task: T204
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

# T204：本机产物保留期与每日清理（B40）

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

checks.toml 可选 [events] artifact_days=30；只清理过期的本机 artifacts 文件与索引，events/refs/anchors 永不因保留期删除。不清理 dispatch 原始流所在范围之外的数据；T105 的内容寻址副本是审计引用入口，已有 dispatch/runs 原始事件流也按 30 天清理（限定 git 公共目录 dispatch/runs 下终止运行的 jsonl，保留运行状态/仍活动流）。
每天首次 emit 顺带执行，跨进程锁/每日标志在 git 公共目录、原子更新；不增常驻进程，不改 v1 四表。读取 artifact_days 缺省 30；非法/非正整数明确提示一次并按 30 处理，不影响原判定。以 UTC created 时间判断，边界与时钟回退见验收。
删除仅限内容寻址目录内符合哈希名的普通文件及明确终止的原始流；拒绝路径穿越/符号链接，异常不改变 emit 或业务返回。执行方产物按 T105 已转存后才能清理旧原始流。
活动流规则（F4）：只读现有dispatch.state_dir(root)指向的<git公共目录>/dispatch；锁为slots/<n>.json，字段pid/task/branch/started_at及可选attempt；原始流为runs/<任务ID>/<attempt>/round-N.jsonl。任一pid>0且存活（kill(pid,0)，PermissionError视为存活）的锁保护该task的全部attempt目录，避免旧尝试与新尝试并行/续跑误删。pid不存在且系统明确ProcessLookupError才视为不活动；无锁的task按原始流mtime UTC超过artifact_days清理。坏JSON/缺pid/读取或探活不确定时当轮跳过原始流清理并报告，不按“读不到等于终止”删除。重新lstat确认普通文件/哈希或round文件名、mtime与锁状态后删除；不追随符号链接。不修改锁与派发状态，也不依赖新增完成标记，因此dispatch.py无需进白名单。

## 白名单

- `engine/core/events.py`
- `engine/core/events_db.py`
- `templates/.harness/config/checks.toml`
- `tests/test_artifact_retention.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T203 已合并且工作区基于含依赖的最新main；T101为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_artifact_retention.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T204 | 冻结时钟的 29/30/31 天、用户天数与非法配置夹具断言删除集合，事件/引用/锚点完整 | 夹具 | `tests.test_artifact_retention.ObservabilityTaskTest.test_default_custom_and_boundary_days` | 错删边界或连元数据一起清理时失败 |
| 不挂规格：B46 T204 | 两个进程当天仅一轮成功清理；跨日再清理，时钟回退不提前删 | 夹具 | `tests.test_artifact_retention.ObservabilityTaskTest.test_once_per_day_and_concurrent_emit` | 每次 emit 清理或跨进程重复清理/错过跨日时失败 |
| 不挂规格：B46 T204 | 符号链接/伪造索引/活动原始流均保留，过期终止 jsonl 删除且其已转存引用在报告/查询层可标 reference_expired（不新增或修改refs表状态列）；活PID/死PID/权限不明/坏锁、同task旧attempt、续跑更新mtime分别核对集合 | 夹具 | `tests.test_artifact_retention.ObservabilityTaskTest.test_cleanup_stays_inside_artifacts_and_finished_runs` | 删到范围外或仍写入的运行流时失败；未知状态被当终止或只保护当前attempt时失败 |
| 不挂规格：B46 T204 | 权限、锁、删除失败时正常业务继续且最多一条清理提示 | 夹具 | `tests.test_artifact_retention.ObservabilityTaskTest.test_cleanup_error_does_not_break_emit` | 清理异常传播或一直刷 stderr 时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `engine/core/events.py`、`engine/core/events_db.py`、`templates/.harness/config/checks.toml`、`tests/test_artifact_retention.py` | `python3 -W error::ResourceWarning -m unittest tests.test_artifact_retention.ObservabilityTaskTest.test_default_custom_and_boundary_days tests.test_artifact_retention.ObservabilityTaskTest.test_once_per_day_and_concurrent_emit tests.test_artifact_retention.ObservabilityTaskTest.test_cleanup_stays_inside_artifacts_and_finished_runs tests.test_artifact_retention.ObservabilityTaskTest.test_cleanup_error_does_not_break_emit -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_artifact_retention.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
