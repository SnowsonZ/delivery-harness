---
task: T106
class: K2
risk: R0
designer: codex
size: medium
architecture: false
spec_refs: []
no_spec_reason: 已审定的可观测性设计与本次任务书是B46合同，本仓库没有对应产品规格验收编号
budget:
  wall_clock_min: 120
  ci_rounds: 3
  retries: 2
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T106：事件三态判定等价验收

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

只新增集成测试，不通过修改产品代码让测试通过。每个命令运行在隔离的临时 git 仓库/可注入根目录；启用事件、环境变量关闭、强制 SQLite 写入 OSError 三态使用完全相同的输入。环境变量只在测试 Python 的子进程 env 或 mock.patch.dict 中设置，Agent 不在 shell 设置覆盖变量。
覆盖 CLI 所有判定入口：verify、integrity、hygiene、acceptance、taskbook、docs、base-tests、evidence、r1、replay、mutate、quality、release-check、risk、policy、run-check、guard-command 与 git 守卫三钩子，含允许/拒绝或通过/失败以及适用的 skip/异常；外部工具和平台读取用确定性夹具。
stdout、业务 stderr、退出码与结构化结果逐字比较；只可排除 T101 固定的一行写入失败提示及已有真实耗时字段，必须列明剔除位置、另断言提示最多一次/时长合法，不能用任意正则忽略差异。三态必须分别证实有事件、无事件、实际进入失败写入点。

## 白名单

- `tests/test_events_equivalence.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T105、T108 已合并且工作区基于含依赖的最新main；T101为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_events_equivalence.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T106 | 各检查入口的成功/失败/skip 三态业务输出与返回码一致，完整覆盖清单 | 夹具 | `tests.test_events_equivalence.ObservabilityTaskTest.test_verify_and_checks_equivalent_three_states` | 缺埋点的命令会在开启态有事件断言失败；行为受 sink 影响时比对失败 |
| 不挂规格：B46 T106 | 判级、误差预算路由与运行记录合格/缺失的三态比对，不做真实网络读写 | 夹具 | `tests.test_events_equivalence.ObservabilityTaskTest.test_risk_policy_and_run_check_equivalent` | 路由因事件改变或无失败态写入证据时失败 |
| 不挂规格：B46 T106 | JSON 与文本命令拒绝及三个 git 钩子三态比对 | 夹具 | `tests.test_events_equivalence.ObservabilityTaskTest.test_guard_command_and_git_hooks_equivalent` | 改拒绝/退出码或把所有 stderr 都过滤时测试失败 |
| 不挂规格：B46 T106 | 注入额外业务 stderr 与不同返回值，比较器必须检测；原时间字段与一次固定 sink 提示仅按白名单处理 | 夹具 | `tests.test_events_equivalence.ObservabilityTaskTest.test_normalization_is_bounded` | 比较器把任意差异吃掉时负例断言失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `tests/test_events_equivalence.py` | `python3 -W error::ResourceWarning -m unittest tests.test_events_equivalence.ObservabilityTaskTest.test_verify_and_checks_equivalent_three_states tests.test_events_equivalence.ObservabilityTaskTest.test_risk_policy_and_run_check_equivalent tests.test_events_equivalence.ObservabilityTaskTest.test_guard_command_and_git_hooks_equivalent tests.test_events_equivalence.ObservabilityTaskTest.test_normalization_is_bounded -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_events_equivalence.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
