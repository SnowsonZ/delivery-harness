---
task: T709
class: K7
risk: R3
designer: claude-code
size: small
architecture: true
spec_refs: []
no_spec_reason: 待办 B92 第 1 部分（运行记录的 stages 只保留派发里程碑），无产品规格验收编号
budget:
  wall_clock_min: 90
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T709：运行记录的 stages 只保留派发里程碑（B92 第 1 部分）

负责方：**派发任务**。设计方：claude-code；独立评审方：Codex（单会话）。依据：待办 B92；用户 2026-10-05 的决定（B92 分两部分，互不依赖，可以并行）。

## 病灶（代码证据）

- `engine/agents/run_timeline.py` 的 `_read_stages` 按 `source="local"` 和 `trace_id` 读出本机链上的**全部**事件，再组装成记录的 `stages`，没有按阶段过滤。
- 派发在槽位里跑 `bin/verify` 时，单测调用的引擎函数也会写事件，而追踪 ID 取当前分支名，正好就是任务的追踪 ID。这些测试事件因此成了记录的 stages。
- 实测 T703 第 2 份记录（`docs/runs/task-703-review-after-ci/2.json`，273KB）有 1424 条 stages，按阶段分：`ci` 879、`route` 392、`verify` 138、`dispatch` **15**。
- `docs/runs/` 下 61 份记录共 11.8MB，其中 73% 是 stages。
- 派发层的事件全部由 `engine/agents/dispatch_observation.py` 发出，`stage` 一律是 `"dispatch"`（admit、claim、slot、guard_preflight、executor_round、local_verify、clarify、push_pr、ci_wait、escalate）。

## 目标终态

1. `_read_stages` 组装 stages 时**只保留 `stage == "dispatch"` 的事件，以及任何阶段里 `status == "fail"` 的事件**：
   - 失败事件要保留：告警的「已发生的阶段」（`engine/core/alerts.py` 的 `stage_lines`）调用的也是 `record_fields`，只留 dispatch 会让 `review/review fail`、`verify/verify.tests fail`、`ci/cli.evidence fail` 这类最关键的行从告警和记录里消失。实测 T708 整条链上非 dispatch 的失败事件只有 11 条，ok 事件 3766 条，保留失败事件不会让记录重新膨胀；
   - attempt 和 round 的编号规则不变（`_ROUND_STEPS`、`_ATTEMPT_BOUNDARIES` 本来就都是派发层的 step）；
   - 更早 attempt 的指针行规则不变；
   - 过滤用的常量与 `_ANCHOR_STAGE` 共用，或者另起一个名字清楚的模块常量。
2. **链头和锚点不变**：链头仍然取整条本机链的最后一个事件（不论 stage），`anchors` 的语义与写法都不变。被过滤掉的事件仍在本机事件库里，靠锚点哈希防篡改。
3. 256KB 与 512KB 的保底截断逻辑保留，不删。
4. 历史记录不改。

## 白名单

- `engine/agents/run_timeline.py`
- `tests/test_run_record_milestones.py`（新增）
- `tests/test_audit_reconstruction.py`（修订记录 1：只把 `run_record_summary` 的 step 集合断言同步为 `{"admit"}`）
- `tests/test_audit_ledger.py`（**只在必要时**：如果它的断言依赖记录 stages 里含有非 dispatch 的事件，就只同步这处口径，不删断言；不依赖则不改）

消费方扫描（检查单第 2 项）：

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| `engine/routing/run_check.py` 对记录 stages 的格式校验 | 无需 | 只校验每一条的字段形状，条数变少不影响 |
| `engine/reports/ledger.py`、`engine/reports/audit.py`（第 352 行起，读记录 stages 作 run_record_summary 证据） | 无需 | 原样复制，条数变少不影响结构；完整性检查依赖的是 anchors |
| `engine/reports/trace.py` 第 180 行起（stages 末项的 `head_hash` 兜底） | 无需 | T125 之后记录 stages 已经没有 `head_hash`，这个兜底本来就用不上 |
| `engine/core/alerts.py` 的阶段摘要（`stage_lines`） | 无需改代码，验收覆盖 | 它调用的正是 `run_timeline.record_fields`，与写记录同一路径，过滤对它同样生效；按目标终态 1 保留失败事件后，告警仍列出评审、verify、CI 的失败，只是去掉了成百行重复的 ok 事件（#134 告警刷屏即由此而来）。由验收第 5 行断言 |
| `tests/test_run_timeline.py`、`tests/test_dispatch_robustness.py`、`tests/test_run_record_privacy.py` | 无需 | 夹具里的事件都是 `stage: dispatch` |
| `tests/test_audit_reconstruction.py` | 需要（修订记录 1） | 断言记录摘要的 step 集合含夹具里的 `verify.tests` ok 事件，口径随本任务同步为 `{"admit"}` |
| `tests/test_audit_ledger.py` | 实现前核对 | 夹具里另有一条 `emit("verify", …)`；见白名单的条件说明 |

CHANGELOG 由设计方在 D1 统一写入，本任务不改。

## 非目标

- 不处理测试事件写进真实事件库这个根因（B92 第 2 部分，T710）。
- 不改事件库的 schema 和事件写入。
- 不改历史记录。
- 白名单以外的文件一律不改。
- 共用合同 C0 同样适用。

## 前置条件

- 工作区基于最新的 `origin/main`。按函数名定位，行号只作参考；只有函数、调用点或接口本身对不上时，才停下向设计方报告。
- 与 T710 的白名单不交叉，可以并行。

## 验收

具名测试为本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B92 | 夹具链上混有 `ci`、`route`、`verify` 的 ok 事件和派发事件时，记录 stages 只含 `stage == "dispatch"` 的条目，条数等于派发事件数 | 夹具 | `tests.test_run_record_milestones.RunRecordMilestonesTest.test_only_dispatch_stage_kept` | 测试事件被写进记录 |
| 不挂规格：B92 | 链头是整条链的最后一个事件；即使最后一个是非 dispatch 事件，`anchors[0].head_hash` 也等于它的哈希 | 夹具 | `…test_head_is_whole_chain_tail` | 锚点指向被截短的链，防篡改链断开 |
| 不挂规格：B92 | 多次 attempt 时，attempt 和 round 的编号、更早 attempt 的指针行，都和只有派发事件时完全相同（非 dispatch 事件不影响编号） | 夹具 | `…test_attempt_numbering_unchanged_by_noise` | 噪声事件打乱了 attempt 的划分 |
| 不挂规格：B92 | 夹具里混入 1000 条非 dispatch 的 ok 事件时，记录 JSON 小于 16KB，且不出现 `stages_truncated` | 夹具 | `…test_noise_does_not_grow_record` | 记录随测试数量膨胀 |
| 不挂规格：B92 | 链上有 `review/review` 与 `verify/verify.tests` 的 fail 事件时，记录 stages 保留这两条；`alerts.stage_lines` 的输出含 `review/review` fail 一行，且不含任何非 dispatch 的 ok 事件 | 夹具 | `…test_failures_kept_for_record_and_alert` | 告警丢掉评审不通过等关键行，或仍被 ok 噪声刷屏 |
| 不挂规格：B92 | 既有测试全部通过 | 夹具 | `bin/verify --full` | 回归 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | `_read_stages` 只保留派发层事件，并补回归断言 | `engine/agents/run_timeline.py`、`tests/test_run_record_milestones.py` | `python3 -W error::ResourceWarning -m unittest tests.test_run_record_milestones tests.test_run_timeline tests.test_dispatch_robustness -v` | 验收第 1–5 行 |
| 2 | 全量验证，整理交付证据 | `tests/` | `bin/verify --full` | 验收第 6 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收与定向变异复核，以下两个变异必须各自被对应断言抓住：
- 去掉 stage 过滤；
- 去掉失败事件的保留（只留 dispatch）；
- 链头改成取过滤后的最后一条。

（attempt 编号的那条验收，防的是实现时把噪声事件也计入 round；由于边界本来都是派发层的步骤，「先过滤再编号」与「先编号再过滤」结果相同，所以不作为变异项。）

同一失败连续三轮没有新证据时，停止这条路径。不得自行扩大白名单、修改任务书、实现非目标。

## 修订记录 1（2026-10-05，设计方裁决执行方升级：消费方扫描遗漏）

执行方第 1 次派发实现与新增测试都已完成（`5f76a60`），`bin/verify --full` 只剩 1 个失败：`tests/test_audit_reconstruction.py` 的 `test_rebuild_input_output_decisions_by_source` 断言账本里 `run_record_summary` 的 step 集合为 `{"admit", "verify.tests"}`。它的夹具在写记录前发了一条 `verify/verify.tests ok`，按本任务的口径这条正该被过滤。设计方写消费方扫描时漏了这个文件，执行方按规则停下，判断正确。

裁决（采纳执行方推荐的方案 A）：

- 白名单加入 `tests/test_audit_reconstruction.py`，**只改这一处断言**：`run_record_summary` 的 step 集合改为 `{"admit"}`。同一测试里按事件库复原 `verify/verify.tests`（`evidence == "event"`）的断言不动，它证明被过滤的事件仍可从本机事件库复原。
- 消费方扫描补一行：`tests/test_audit_reconstruction.py` 断言记录摘要的 step 集合，口径随本任务同步。
- 其余目标终态、验收、变异清单不变。已完成的提交保留，在此基础上续跑。
