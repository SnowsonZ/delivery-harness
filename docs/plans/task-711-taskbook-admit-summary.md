---
task: T711
class: K5
risk: R2
designer: claude-code
size: small
architecture: true
spec_refs: []
no_spec_reason: 待办 B94（taskbook.admit 事件粒度），无产品规格验收编号；同时是自治试验端到端验收 G 的第一个 K5 任务
budget:
  wall_clock_min: 90
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T711：任务书检查的事件改为每次一条汇总（B94）

负责方：**派发任务**。设计方：claude-code；独立评审方：Codex（单会话）。依据：待办 B94；用户 2026-10-05 决定（JSON 与事件噪声治理优先，D1 后的第一个 K5 任务走自动合并全链，即设计第 9 节 G）。

## 病灶（代码证据）

- `engine/checks/taskbook.py` 的 `_record_admission` 对**每一份**任务书各发一条 `ci/taskbook.admit` 事件，与本次改动无关。仓库现有 54 份任务书，每次 `bin/verify` 就是 54 条，随任务书增多线性增长。
- T709 之后这些事件不再进运行记录，但仍写进本机事件库和 CI 事件包，干扰 `bin/harness trace`、`audit` 的阅读；T710 槽位实测里它是剩下的最大一块（54 条，其次是 `quality` 6 条）。
- 引擎内没有读取 `taskbook.admit` 的报表或判定（`engine/reports`、`engine/routing` 均无引用）；只有两处测试断言它。

## 目标终态

1. `_record_admission` 每次运行发**一条** `ci/taskbook.summary` 事件：
   - `status`：全部合格为 `ok`，有任何不合格为 `fail`；
   - `outputs`：`{"total": <检查份数>, "failed": <不合格份数>}`；
   - `decision`：`{"by": "taskbook", "rule": "admit", "reason": "合格"}`，有不合格时 `reason` 为 `"<不合格份数> 份不合格"`；
   - 不带 `inputs`。
2. **只有不合格的任务书**才各发一条 `ci/taskbook.admit` 事件，字段（inputs、outputs、decision）与现在完全相同。合格的任务书不再单独发事件。
3. 发事件的顺序：先发各不合格任务书的 `taskbook.admit`，最后发 `taskbook.summary`。
4. 观察旁路不变：事件写入失败不影响退出码与输出（保留现有的 `except Exception` 兜底）。
5. CHANGELOG「Unreleased」加一行说明事件形状变化（`taskbook.admit` 只为不合格的任务书发，新增 `taskbook.summary`）。

## 白名单

- `engine/checks/taskbook.py`
- `tests/test_taskbook_summary_event.py`（新增）
- `tests/test_events_checks.py`（只同步 `test_taskbook_and_hygiene_metadata` 里 taskbook 部分的断言口径，hygiene 部分不动）
- `tests/test_events_equivalence.py`（只同步 taskbook 两处 `three_states` 的期望 step）
- `CHANGELOG.md`

消费方扫描（检查单第 2 项）：

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| `tests/test_events_checks.py` 的 `test_taskbook_and_hygiene_metadata` | 需要 | 断言合法、非法任务书各一条 `taskbook.admit`；改为非法的一条 `taskbook.admit`（字段不变）加一条 `taskbook.summary`（total 2、failed 1） |
| `tests/test_events_equivalence.py` 的 taskbook 两处 `three_states` | 需要 | 期望 step 改为：通过时 `("cli.taskbook", "taskbook.summary")`；失败时 `("cli.taskbook", "taskbook.summary", "taskbook.admit")` |
| `engine/reports/**`、`engine/routing/**`、`engine/core/alerts.py` | 无需 | 不读 `taskbook.admit`（实现前用 `grep -rn "taskbook\.admit\|taskbook\.summary" engine` 复核，有出入就停下报告） |
| `engine/agents/run_timeline.py`（T709） | 无需 | 记录只留 dispatch 与失败事件；`taskbook.summary` 失败、`taskbook.admit` 失败照常进记录 |
| Agent-Notification 的 `tests/test_harness*.py` | 无需 | 不引用 `taskbook.admit`（设计方已核对） |

## 非目标

- 不改任务书检查的判定、输出文案与退出码。
- 不改其他检查（hygiene、quality 等）的事件。
- 不改事件库 schema。
- 白名单以外的文件一律不改。
- 共用合同 C0 同样适用。

## 前置条件

- 工作区基于最新的 `origin/main`。按函数名定位，行号只作参考；只有函数、调用点或接口本身对不上时，才停下向设计方报告。
- 与 T712 白名单不交叉，可以并行。

## 验收

具名测试为本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B94 | 夹具里 5 份合格、1 份不合格的任务书：一次检查只产生 1 条 `taskbook.summary`（status fail，outputs total 6、failed 1）和 1 条 `taskbook.admit`（不合格那份，字段与改动前一致），合格的不产生任何 `taskbook.admit` | 夹具 | `tests.test_taskbook_summary_event.TaskbookSummaryEventTest.test_one_summary_and_failures_only` | 仍按份数发事件，或汇总计数错误 |
| 不挂规格：B94 | 全部合格时只有 1 条 `taskbook.summary`（status ok，failed 0，reason「合格」），没有 `taskbook.admit` | 夹具 | `…test_all_pass_single_summary` | 合格时仍发逐份事件 |
| 不挂规格：B94 | 事件写入抛异常时，检查的退出码与标准输出和不发事件时完全一致 | 夹具 | `…test_event_failure_does_not_change_result` | 观察旁路影响判定 |
| 不挂规格：B94 | 既有测试全部通过（两处口径同步除外，零其他修改） | 夹具 | `bin/verify --full` | 回归 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | `_record_admission` 改为汇总加不合格逐份，新增回归测试，同步两处既有断言 | `engine/checks/taskbook.py`、`tests/test_taskbook_summary_event.py`、`tests/test_events_checks.py`、`tests/test_events_equivalence.py` | `python3 -W error::ResourceWarning -m unittest tests.test_taskbook_summary_event tests.test_events_checks tests.test_events_equivalence -v` | 验收第 1–3 行 |
| 2 | CHANGELOG 一行；全量验证，整理交付证据 | `CHANGELOG.md` | `bin/verify --full` | 验收第 4 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收与定向变异复核，以下变异必须各自被对应断言抓住：
- 合格的任务书也发 `taskbook.admit`；
- 汇总的 `failed` 计数改为固定 0；
- 不合格的任务书不再发 `taskbook.admit`。

本任务是自治试验端到端验收 G 的第一个 K5 任务：执行方照常交付，合并由合同制路径自动完成，设计方另行核对全链（设计第 9 节 G）。设计方补的提交同样带 `Task: T711`。

同一失败连续三轮没有新证据时，停止这条路径。不得自行扩大白名单、修改任务书、实现非目标。
