---
task: T125
class: K7
risk: R3
designer: codex
size: medium
architecture: true
spec_refs: []
no_spec_reason: 待办 B77 的派发收尾健壮性修复（记录收敛 + 槽位归还 + 链路自愈），无产品规格验收编号
budget:
  wall_clock_min: 120
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T125：派发收尾健壮性（B77：stages 收敛、槽位归还、链路自愈）

负责方：**派发任务**。设计方：codex 席位（现由 Zcode 代行）；独立评审方 OpenCode。依据：待办 B77（2026-09-30 四次手动处理实录：T122/T124/T304 记录 stages 超 1MB 被卫生守卫拦、槽位残留占分支三次、CI/评审链静默断链两次）；[共用合同](2026-09-29-observability-task-contracts.md)C0。先读 `engine/agents/run_timeline.py`（`record_fields`/`_read_stages`）、`engine/agents/dispatch.py`（`write_record`/`prepare_slot`/`_loop` 收尾路径）。

## 目标终态

1. **stages 收敛**（记录侧）：
   - `record_fields` 组装 stages 时只取**本次 attempt 窗口**的事件（按 trace + 本次 attempt 起点过滤；更早 attempt 已发生的 push_pr/ci_wait 摘要保留一条指针行 `{step, status:"prior", attempt:前值}`），不重复读全历史；
   - 单条摘要瘦身：只保留 `{stage, step, status, ts, duration_ms, attempt, round}` 标量；`inputs/outputs/decision` 不入记录（细节留在事件库，记录是索引）；
   - anchors 不变；保底：组装后 stages 超 256KB 截断并标注 `stages_truncated/stages_total`。
2. **落盘前自检**（`write_record`）：记录 JSON 超 512KB 或 prompt 快照含 CI/runner 工作区绝对路径时，先自处理（截断标注 / 路径占位 `<ci-workspace>`）再提交——不让卫生守卫在记录提交步杀派发；自处理行为写 stderr 一行提示。
3. **槽位归还**：派发的结束路径（成功、升级、预算耗尽、异常捕获）统一归还槽位（worktree remove + 锁清理）；崩溃路径无法归还时，下次派发 `prepare_slot` 前先清理被死进程占用的槽（锁文件 pid 不存活即强制回收，分支有未推提交则先推送保全再回收）。
4. **等待链自愈**：`wait_ci` 的 CI 查询循环与 `dispatch review` 的评审等待，若上一轮后台链中断（进程退出码非零且产物已推），重入时检测到「分支 head 已在远端且无对应结论」给出明确提示（不自动重跑评审，提示设计方手动接链）。
5. 回归测试为新增文件（见验收）；既有测试除授权的假体/副本同步外零修改。

实现前先核对三个修改点的现状，差异停下报告设计方，不自行补实现。

## 白名单

- `engine/agents/run_timeline.py`
- `engine/agents/dispatch.py`
- `tests/test_dispatch_robustness.py`（新增）
- `tests/test_run_timeline.py`（仅限既有 stages 断言的字段口径同步：瘦身后的字段集；不删除断言）
- `engine/routing/run_check.py`（仅限 stages 校验配套：`_STAGE_FIELDS` 改「七键必填 + 旧六键可选」双形状、status 枚举加 `prior`、顶层 `stages_truncated/stages_total` 入白名单——见修订记录 2）
- `CHANGELOG.md`（Unreleased 补条目）
- `tests/test_audit_ledger.py`（仅限 stages 收敛的消费方同步：断言从「全部行有 source/head_hash」收窄到 evidence_kind=event 行与 step 覆盖——T305 账本对旧十二键的假设随形状收敛失效，见修订记录 3）

## 非目标

不实现其他任务；不改事件库 schema 与事件写入；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件。共用合同C0同样适用。

## 前置条件

- 工作区基于含依赖的最新main。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- 与同轮其他任务白名单不交叉。
- 改派发与记录路径：设计方将做 G2（556 计数对照 + 真实 resume 场景核对）。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；差异先修订任务书并经原审批。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B77 | 三轮 resume 的记录：stages 仅含本次 attempt 事件 + 前置指针行；总 JSON < 512KB（夹具以三轮各 800 事件验证） | 夹具 | `tests.test_dispatch_robustness.DispatchRobustnessTest.test_stages_scoped_to_attempt` | 读全历史时体积/重复断言失败 |
| 不挂规格：B77 | 记录超 512KB 或 prompt 含 runner 路径时：自动截断/占位后提交成功，stderr 有提示，守卫不拦 | 夹具 | `…test_record_self_check_normalizes` | 仍被守卫拦截时失败 |
| 不挂规格：B77 | 死进程槽位（锁 pid 不存活）：下次派发自动回收；分支未推提交先推送保全 | 夹具 | `…test_stale_slot_reclaimed_with_salvage` | 占用冲突（B72 形态）时失败 |
| 不挂规格：B77 | stages 单条仅含七个标量字段；既有时间线断言口径同步后全类通过 | 夹具 | `…test_stage_item_shape` + `tests.test_run_timeline` 全类 | 字段集不符时失败 |
| 不挂规格：B77 | 设计方真实 resume 场景核对（三次连续 resume 的记录体积单调不增）+ G2 556 对照 | 人工 | PR 评论 | 无核对结论则不通过 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在用户授权派发后，执行方按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | stages 收敛与自检、槽位归还、链路提示、回归断言 | `engine/agents/run_timeline.py`、`engine/agents/dispatch.py`、`tests/test_dispatch_robustness.py`、`tests/test_run_timeline.py` | `python3 -W error::ResourceWarning -m unittest tests.test_dispatch_robustness.DispatchRobustnessTest tests.test_run_timeline.ObservabilityTaskTest -v` | 验收 1–4 行 |
| 2 | 验证范围，整理交付证据 | `tests/` | `bin/verify --full` | C0与本任务验收 |
| 3 | 设计方真实场景核对 + G2（人工，合并前） | —— | 真实 resume ×3 + 消费方 556 | 验收第 5 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收与定向变异复核（去掉 attempt 过滤回到全历史、去掉自检、去掉槽位回收，各自必须被对应断言抓住）。设计方做 G2。同一失败连续三轮无新证据时停止该路径；不得自行扩大白名单、改原任务书、实现非目标。


## 修订记录 2（2026-10-01，设计方裁决二轮评审）

- **run_check.py 白名单授权补记**：二轮修复（`5301e9a`）涉及的 `run_check.py` 改动属「冻结表配套授权」规则（执行计划 §3.5 规约 1）的适用对象——stages 瘦身改了写入方，记录内容校验的 `_STAGE_FIELDS` 必须配套，否则新记录被「记录内容」判失败断链（评审核实属实，设计方首轮任务书漏列该文件，现补记授权）。改动范围钉死为三处：双形状字段表、prior 枚举、顶层截断标注键。
- **CHANGELOG**：设计方补 Unreleased 条目（行为变化与消费方兼容性说明）。
- **人工证据**：G2 556 对照与变异复核已在 PR #91 评论（首轮复核）；真实 resume ×3 核对随合并后 G1 升级执行（G1 前内置副本不含本修复，resume 行为未变——核对无意义；G1 后首次真实 resume 即核对该项）。
- diff 截断 = B74 内置副本滞后（最后实例；G1 后出包走 T124 分片）。


## 修订记录 3（2026-10-01，设计方补记授权：终评闭环）

- **`tests/test_audit_ledger.py` 授权补记**（终评严重项）：该文件 3 行断言收窄（source/head_hash 的全行断言 → event 行断言 + step 覆盖）属 stages 收敛的**必要消费方同步**（检查单第 2 项的漏记实例：T305 与 T125 并行开发时，账本测试对旧十二键形状的假设在收敛后失效）。head_hash 核对在 anchors 侧已有等价覆盖（`refs["run_record"]` 断言），非覆盖收窄。修订记录 2 漏列该文件，本记录补记。
- 人工证据时点维持修订记录 2 的裁决（resume ×3 于 G1 后；G2/变异在 PR #91 评论）；diff 截断 = B74 末例。
