---
task: T109
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

# T109：事件库加固与来源链隔离

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

不改已有 tests/test_events.py；保留公共签名、v1 四表/列与规范化 hash 规则。新增测试独立算规范化 sha256，证明 prev_hash 确实进入哈希；重算合法哈希但保留 seq 缺口，使 seq 检查不能靠其他哈希错误偶然兜底。
step 按字符串隐私规则过滤，非法整条不写；过滤后的空引用不入表；span fixed 未知键不得使观察失败传播或覆盖业务异常；store_artifact 的 Path 读取/bytes 转换也不得传播观察异常。产物临时文件写完后原子替换，异常不留下半文件，已有正确内容不重复写；读库链校验先看版本，新版库明确报告不可校验且不修改/不当完好。
来源链隔离见共用合同 C2：真实 Actions 上 default_source 返回 ci:<run_id>:<run_attempt>:<job>，actor.host 仍 ci；无 Actions 三键的 CI 兼容返回 ci。source 是既有自由字符串而非枚举，不改表或哈希格式；已显式传 source 的旧调用原样保留。所有后续任务先依赖本任务合并。

## 白名单

- `engine/core/events.py`
- `engine/core/events_db.py`
- `tests/test_events_hardening.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T101 已合并且工作区基于含依赖的最新main；T101为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_events_hardening.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T109 | 相同内容仅 prev_hash 不同，独立计算的摘要不同且与库一致；去掉 prev_hash 的变异必须被抓住 | 夹具 | `tests.test_events_hardening.ObservabilityTaskTest.test_prev_hash_is_in_canonical_digest` | 实现未把 prev_hash 哈希时具体摘要断言失败 |
| 不挂规格：B46 T109 | 人为构造 seq 1/3 且内容哈希与 prev_hash 都正确的链，仍报告 seq 缺口；删 seq 检查的变异失败 | 夹具 | `tests.test_events_hardening.ObservabilityTaskTest.test_seq_gap_with_valid_hashes_is_detected` | 只靠 hash/prev_hash 报错兜底时负例不会被发现而测试失败 |
| 不挂规格：B46 T109 | 非法 step、未知 fixed、全被过滤引用、Path 读取/bytes 转换失败逐个断言不传播且 redacted/行数正确 | 夹具 | `tests.test_events_hardening.ObservabilityTaskTest.test_step_span_refs_and_input_io_fail_safe` | step 泄漏、空引用或 span TypeError 遮盖业务异常时失败 |
| 不挂规格：B46 T109 | 替换前注入失败/双进程相同产物、检查无半文件；新版库校验报告 unavailable 且内容不变 | 夹具 | `tests.test_events_hardening.ObservabilityTaskTest.test_artifact_atomicity_and_newer_schema_read` | 直接 target.write_bytes 或新版库被当完好时失败 |
| 不挂规格：B46 T109 | 两个 run、同 run 两次 attempt、两个 job、无 Actions 的 CI、local 分别断言 source、actor.host 与独立 seq=1/哈希链 | 夹具 | `tests.test_events_hardening.ObservabilityTaskTest.test_ci_run_attempt_job_isolates_chains` | 真实 CI 仍全用 ci 导致多运行 seq 冲突时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `engine/core/events.py`、`engine/core/events_db.py`、`tests/test_events_hardening.py` | `python3 -W error::ResourceWarning -m unittest tests.test_events_hardening.ObservabilityTaskTest.test_prev_hash_is_in_canonical_digest tests.test_events_hardening.ObservabilityTaskTest.test_seq_gap_with_valid_hashes_is_detected tests.test_events_hardening.ObservabilityTaskTest.test_step_span_refs_and_input_io_fail_safe tests.test_events_hardening.ObservabilityTaskTest.test_artifact_atomicity_and_newer_schema_read tests.test_events_hardening.ObservabilityTaskTest.test_ci_run_attempt_job_isolates_chains -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_events_hardening.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
