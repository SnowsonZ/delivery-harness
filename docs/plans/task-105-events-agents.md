---
task: T105
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

# T105：派发全链与独立评审埋点

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

dispatch 的 admit、claim、slot、guard_preflight、executor_round、local_verify、clarify、push_pr、ci_wait、escalate 各在实际操作结果后记录，失败退出也有事件；字段逐项见设计 3.2 与 C1。已有准入、预算、打转、等待与升级行为不变。
RunResult/Attempt 增加末尾带默认值的 guard_allowed（int）；保留 PiHost.parse 的三元返回接口，新增 parse_observability(events: Path)->dict 返回 {guard_allowed, guard_denied, missing_context}，本任务 missing_context 先为空列表，T202 填充。按每个 tool_execution_end 计一次完成；守卫拒绝工具计 denied，普通工具失败仍计 allowed（表示守卫放行，不代表工具成功）。不能以拒绝理由条数代替被拒工具数。
每轮 Pi JSONL 与 verify 日志保存本机 artifact，事件只有哈希/大小。review 取 PR headRefName 显式 trace，材料包引用、reviewer/model、时长、结论、按严重度计数、身份是否分离、评论 URL/摘要哈希与失败 kind 均记录；评论内附安全的审计摘要与材料哈希供 T305 取回，不上传会话或原始评审事件流。
体量约束（F1）：dispatch.py只接线，元数据组装、观察失败隔离、产物引用准备放新增dispatch_observation.py；不把整套观察分支塞进_loop/local_rounds。所有文件≤800行、项目complex_functions/files_over_800棘轮不得上升，不申请上调基线；预算内不能保持质量就升级。
评审评论在原independent-review标记旁新增C6的harness-review-audit JSON标记，字段与材料编码/截取规则固定。原标记的读回/待评审判定逻辑不变，T305只解析新标记；两任务的夹具使用同一已固定形状，materials含task/diff/ci/pr以及C6规定的pack_v1引用，缺pack引用的负例必须失败。

## 白名单

- `engine/agents/review.py`
- `engine/agents/dispatch.py`
- `engine/agents/dispatch_host.py`
- `engine/agents/dispatch_observation.py`
- `tests/test_events_agents.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T103、T104 已合并且工作区基于含依赖的最新main；T101为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_events_agents.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T105 | 假 host/gh 跑成功全链及 admit、claim、preflight、executor、verify、CI、escalate 失败分支，核对设计 3.2 每个事件 | 夹具 | `tests.test_events_agents.ObservabilityTaskTest.test_dispatch_success_and_failure_steps` | 漏失败路径、事件先于真实操作或错 trace 时失败 |
| 不挂规格：B46 T105 | 混合守卫拒绝、双理由拒绝、普通失败、成功、损坏行；断言 allowed/denied 数与原始字节 artifact 哈希 | 夹具 | `tests.test_events_agents.ObservabilityTaskTest.test_tool_counts_and_artifact_hash` | 理由数冒充调用数、普通失败当守卫拒绝或改流内容时失败 |
| 不挂规格：B46 T105 | 通过/不通过/评审方退出非零分别核对结论、计数、模型、材料哈希、评论 URL 与 error.kind；按C6新标记精确断言字段/材料哈希，旧标记仍可被现有reviewed_heads读取 | 夹具 | `tests.test_events_agents.ObservabilityTaskTest.test_review_metadata_and_failure` | 漏评审失败或未保存审计摘要时失败；标记或形状不同也必须失败 |
| 不挂规格：B46 T105 | 观察 API 与读取引用失败时仍执行相同的 host/gh 调用序列与原退出码 | 夹具 | `tests.test_events_agents.ObservabilityTaskTest.test_observation_does_not_change_dispatch_or_review` | 事件影响派发、发布或评审结论时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `engine/agents/review.py`、`engine/agents/dispatch.py`、`engine/agents/dispatch_host.py`、`engine/agents/dispatch_observation.py`、`tests/test_events_agents.py` | `python3 -W error::ResourceWarning -m unittest tests.test_events_agents.ObservabilityTaskTest.test_dispatch_success_and_failure_steps tests.test_events_agents.ObservabilityTaskTest.test_tool_counts_and_artifact_hash tests.test_events_agents.ObservabilityTaskTest.test_review_metadata_and_failure tests.test_events_agents.ObservabilityTaskTest.test_observation_does_not_change_dispatch_or_review -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_events_agents.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
