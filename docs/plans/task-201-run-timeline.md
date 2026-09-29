---
task: T201
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

# T201：运行记录时间线与固定链头

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

write_record 添加 trace_id=task.branch、stages 和 anchors（均为安全摘要，形状见 C3）；在记录落盘前取当前本机链头，写入摘要后 set_anchor(... fixed_in="run_record", source="local")。锚点指向已发生事件的链头，不把尚未创建的 push_pr/ci_wait 伪造进记录。
stages 包含任务准入至本地检查的各已发生阶段及输入/输出引用、决定、角色、时长、轮次，供 T305 在无本机库的 CI 上组装账本。记录摘要不含原始事件流、完整日志或会话。
push/CI 后的事件进入本机库与 CI/GitHub 来源，不为了补时间线追加推送导致 CI 无限循环。保留 RECORD_FIELDS 与旧字段语义；本任务不加强 run-check。
体量约束（F1）：新增run_timeline.py负责安全stages/anchors摘要读取与组装；dispatch.py只调用它并落原记录。模块不能决定退出/预算/推送；原派发行为不变。保留≤800行与复杂度棘轮，不改基线。T105观察模块负责事件，T201模块负责摘要，职责不相互回流。
记录摘要按C3字段类型保存：decision.reason使用稳定结果短ID，数值依据放outputs、原始理由保留其引用/哈希，不把任意原始理由正文塞回记录。摘要的决定/状态仍取真实结果，不重做判定。


## 白名单

- `engine/agents/dispatch.py`
- `engine/agents/run_timeline.py`
- `tests/test_run_timeline.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T107、T108 已合并且工作区基于含依赖的最新main；T101为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_run_timeline.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T201 | 假 host/gh 完成一次本地执行后记录具备安全 stages、trace，anchor 精确等于落盘前 chain_head | 夹具 | `tests.test_run_timeline.ObservabilityTaskTest.test_record_contains_timeline_and_exact_anchor` | 字段缺失、链头抓错时点或 stage 没有输入输出时失败 |
| 不挂规格：B46 T201 | 两轮执行/两次尝试不会混成一次时间线，失败事件与实际时间顺序可见 | 夹具 | `tests.test_run_timeline.ObservabilityTaskTest.test_retry_stages_keep_attempt_and_round` | 只写最后一轮或计数混淆时失败 |
| 不挂规格：B46 T201 | 新增字段以外与原记录夹具一致，现有 run-check 能验证原字段与提示词哈希 | 夹具 | `tests.test_run_timeline.ObservabilityTaskTest.test_legacy_record_fields_preserved` | 删除旧字段或改变 prompt 字节时失败 |
| 不挂规格：B46 T201 | 真实调用序列的假 gh 断言原推送次数、CI 轮次不变，sink 不可用时锚点为空且原任务继续 | 夹具 | `tests.test_run_timeline.ObservabilityTaskTest.test_no_extra_push_for_post_record_stages` | 为补 CI 锚点再写记录推送或 sink 阻断时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `engine/agents/dispatch.py`、`engine/agents/run_timeline.py`、`tests/test_run_timeline.py` | `python3 -W error::ResourceWarning -m unittest tests.test_run_timeline.ObservabilityTaskTest.test_record_contains_timeline_and_exact_anchor tests.test_run_timeline.ObservabilityTaskTest.test_retry_stages_keep_attempt_and_round tests.test_run_timeline.ObservabilityTaskTest.test_legacy_record_fields_preserved tests.test_run_timeline.ObservabilityTaskTest.test_no_extra_push_for_post_record_stages -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_run_timeline.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
