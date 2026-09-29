---
task: T102
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

# T102：命令与 verify、integrity 埋点

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

在 cli.main 分派边界记录 cli.<命令>，含时长、返回码；捕获 SystemExit 仅记录后原样重抛；业务异常也原样重抛。帮助与未知命令不记录参数正文。命令阶段映射与例外见共用合同 C1；guard两入口由T104仅拒绝记录，查询/审计/告警由自身模块处理，入口事件不能替代内容事件。
verify 每项检查在最终 Result（含 strict 转换）后写 verify.<name>；pass 映射 ok，fail 映射 fail，skip 映射 skip。记录 head、档位、退出码、时长、失败签名与完整日志哈希/大小，收尾写 verify.summary 的 tier、ok、dirty。不要只在 run_check 内埋点而漏掉 --skip 与 strict。
integrity 记录 lock 引用、目录摘要、变更文件计数；失败 error.kind=integrity_mismatch。file_ref/store_artifact 的准备也放在不影响原判定的边界内。

## 白名单

- `engine/cli.py`
- `engine/checks/verify.py`
- `engine/checks/integrity.py`
- `tests/test_events_verify.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T101、T109 已合并且工作区基于含依赖的最新main；T101为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_events_verify.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T102 | 成功、返回非零、SystemExit、业务异常四种入口均有 cli 事件，返回或异常与埋点前一致 | 夹具 | `tests.test_events_verify.ObservabilityTaskTest.test_cli_preserves_return_and_exceptions` | 去掉包装则没有事件；吞异常或改返回码则行为比对失败 |
| 不挂规格：B46 T102 | 通过、失败、平台 skip、--skip、strict skip 的夹具断言逐项事件、真实退出码与唯一汇总；日志按原始字节核对 sha256 | 夹具 | `tests.test_events_verify.ObservabilityTaskTest.test_verify_each_check_and_summary` | 漏项、错误映射或只记汇总时事件集合与字段断言失败 |
| 不挂规格：B46 T102 | 临时内置副本被改时原检查失败且事件含 mismatch、lock 哈希与文件计数，成功副本为 ok | 夹具 | `tests.test_events_verify.ObservabilityTaskTest.test_integrity_failure_metadata` | 只打入口事件或缺 lock/计数时具体字段断言失败 |
| 不挂规格：B46 T102 | 引用文件读取及产物保存抛 OSError 时，原 stdout、返回值、业务异常保持一致 | 夹具 | `tests.test_events_verify.ObservabilityTaskTest.test_reference_failure_keeps_check_result` | 在 emit 外准备引用而传播异常时失败 |
| 不挂规格：B46 T102 | 逐个实际COMMANDS入口核对C1阶段映射，install/upgrade/identity/weekly各明确有一次入口事件；guard放行与events/trace查询为零，audit/alert由各自模块记事件。只隔离下游副作用，不mock包装层 | 夹具 | `tests.test_events_verify.ObservabilityTaskTest.test_all_registered_entrypoint_mappings_and_guard_exceptions` | 任何命令被实现特判遗漏、阶段错或守卫放行仍产生日志时对应点名断言失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `engine/cli.py`、`engine/checks/verify.py`、`engine/checks/integrity.py`、`tests/test_events_verify.py` | `python3 -W error::ResourceWarning -m unittest tests.test_events_verify.ObservabilityTaskTest.test_cli_preserves_return_and_exceptions tests.test_events_verify.ObservabilityTaskTest.test_verify_each_check_and_summary tests.test_events_verify.ObservabilityTaskTest.test_integrity_failure_metadata tests.test_events_verify.ObservabilityTaskTest.test_reference_failure_keeps_check_result tests.test_events_verify.ObservabilityTaskTest.test_all_registered_entrypoint_mappings_and_guard_exceptions -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_events_verify.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
