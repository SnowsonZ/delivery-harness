---
task: T404
class: K7
risk: R3
designer: codex
size: medium
architecture: true
spec_refs: []
no_spec_reason: 已审定的可观测性设计与本次任务书是B46合同，本仓库没有对应产品规格验收编号
budget:
  wall_clock_min: 90
  ci_rounds: 3
  retries: 2
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T404：audit 观察事件与变更记录

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)（C0/C1/C7）、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 动机（合同覆盖缺口，2026-10-01 设计方核对）

- 共用合同 C1「audit/alert 自己记汇总或发布结果，避免递归」与 C7「审计写安全ci/audit.summary及audit.finding观察事件供周报，不新增audit阶段枚举」要求 audit 命令写观察事件；T501 验收 `test_audit_findings_dedup_latest_head` 以该事件存在为前提。全量核对 `docs/plans/task-*.md`：T401–T403 目标终态均未含 emit 侧，T501 仅消费——本任务补齐缺口。
- T401 引入用户可见 `audit` 子命令，CHANGELOG「Unreleased」未记录（派发检查单第3项；2026-10-01 核对 T401 白名单未含 CHANGELOG.md）。

## 目标终态

T401 落地的 `engine/reports/audit.py` 每次运行结束写一条 stage=ci、step=audit.summary 的观察事件，outputs 仅含安全字段（PR号、head_sha、规则计数、severity 计数、coverage、ok）；每条 finding 追加一条 stage=ci、step=audit.finding 的观察事件，outputs 含 rule/severity/source/stage/ref 与 pr/head_sha（供周报按「同PR/head取最新、唯一规则计数」去重，C7 周报段）；reason 原文不入事件（C7 私密边界）。
不新增 audit 阶段枚举（stage 固定 ci）；cli 静默例外已在 QUIET_COMMANDS 登记（engine/cli.py，T102），本任务不改 engine/cli.py。观察写入失败不改 audit 的退出码与 stdout/stderr（C0 三态等价）。
CHANGELOG.md「Unreleased」补 audit 子命令条目（英文，含两种调用模式与 --json、退出码 0/1/2 约定；无 Migration 条目——新增命令无迁移项）。

## 白名单

- `engine/reports/audit.py`
- `CHANGELOG.md`（Unreleased 记 audit 子命令；用户可见行为变化）
- `tests/test_audit_events.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不改动 audit 复原/核对逻辑、finding 规则与退出码口径（T401/T402 范围）；不实现 T402 完整性规则与 [audit] 配置；不改 engine/cli.py、既有测试、模板、`.harness/`、Agent配置、依赖锁与 ruff 配置；不引入第三方依赖、不改版本/tag。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T401 已合并且工作区基于含依赖的最新main。逐项核对 audit.py 实际接口（命令入口、finding 形状、inspect_pr 返回），差异停下报告设计方，不自行补实现。
- `tests/test_audit_events.py` 尚不存在；不编辑 T401 建立的 `tests/test_audit_reconstruction.py`。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T404 | 每次运行恰一条 audit.summary、每条 finding 一条 audit.finding，stage=ci、不新增阶段枚举 | 夹具 | `tests.test_audit_events.ObservabilityTaskTest.test_summary_and_finding_events_per_run` | 不写事件、逐 finding 缺失或新增 stage 枚举时失败 |
| 不挂规格：B46 T404 | 事件 outputs 仅含安全字段（rule/severity/source/stage/ref/pr/head_sha 等），reason 原文与私密内容不入事件 | 夹具 | `tests.test_audit_events.ObservabilityTaskTest.test_event_payload_safe_fields_only` | 泄露 reason 原文/本机路径或周报去重字段缺失时失败 |
| 不挂规格：B46 T404 | 观察写入失败时 audit 退出码与输出和禁用观察时逐字一致（C0） | 夹具 | `tests.test_audit_events.ObservabilityTaskTest.test_observation_failure_isolated` | 写入失败改变退出码/输出或向调用方抛异常时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对 T401 后的 audit.py 接口，实现观察事件写入与对应断言 | `engine/reports/audit.py`、`tests/test_audit_events.py` | `python3 -W error::ResourceWarning -m unittest tests.test_audit_events.ObservabilityTaskTest.test_summary_and_finding_events_per_run tests.test_audit_events.ObservabilityTaskTest.test_event_payload_safe_fields_only tests.test_audit_events.ObservabilityTaskTest.test_observation_failure_isolated -v` | 本任务验收全部行 |
| 2 | CHANGELOG「Unreleased」补 audit 子命令条目并全量验证 | `CHANGELOG.md` | `bin/verify --full`；`git diff -- CHANGELOG.md` 逐行核对 | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；本任务只加观察旁路、不改判定路径，消费方验证是否触发由设计方按追溯表 G2 口径判定。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
