---
task: T114
class: K7
risk: R3
designer: codex
size: small
architecture: true
spec_refs: []
no_spec_reason: 待办 B68 的评审材料包工具改进（任务书探测回退），无产品规格验收编号
budget:
  wall_clock_min: 60
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T114：评审材料包的任务书探测回退

负责方：**派发任务**。设计方：codex 席位（现由 Zcode 代行）；独立评审方 OpenCode。依据：待办 B68（T107/#33 评审指出材料包任务书写「无」，而 PR 实际按 `docs/plans/task-107-events-docs.md` 执行）；[共用合同](2026-09-29-observability-task-contracts.md)C0。

## 目标终态

独立评审的材料包在既有任务书探测（`run_check.scope` 按改动文件匹配）失败时，增加一条回退路径：

1. 从 PR 标题与正文提取 `docs/plans/task-*.md` 引用，存在且文件在 base 上存在时，把该任务书全文写入材料包的 task.md（与既有探测成功时同格式），pack.md 的任务书条目随之不再为「无」。
2. 既有探测成功时的行为逐字不变；正文无引用、引用文件不存在时维持「无」（现状锁定）。
3. 评审方分离判定（designer_of）与只读性不受影响。
4. 回归测试为新增文件（见验收），不修改任何既有测试。

实现前先在实际代码核对材料组装链（`review.py` 的 `write_materials` 与 `review_pack.py`），与上述不符时停下报告设计方，不自行改合同。

## 白名单

- `engine/agents/review.py`
- `engine/agents/review_pack.py`
- `tests/test_review_pack_taskbook.py`（新增）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0同样适用。

## 前置条件

- 工作区基于含依赖的最新main。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- 与同轮其他任务白名单不交叉。
- 改评审链路：设计方将做等价验证（G2，用一次真实 `bin/dispatch review` 对照）。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；差异先修订任务书并经原审批。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B68 | PR 正文引用 `docs/plans/task-*.md` 且探测未命中时，材料包含该任务书全文 | 夹具 | `tests.test_review_pack_taskbook.ReviewPackTaskbookTest.test_body_reference_included` | pack 仍写「无」时失败 |
| 不挂规格：B68 | 正文无引用或引用文件不存在：维持「无」；既有探测成功路径行为逐字不变 | 夹具 | `…test_no_reference_and_existing_paths_unchanged` | 现状被改动或误包含时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在用户授权派发后，执行方按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 任务书探测回退与回归断言 | `engine/agents/review.py`、`engine/agents/review_pack.py`、`tests/test_review_pack_taskbook.py` | `python3 -W error::ResourceWarning -m unittest tests.test_review_pack_taskbook.ReviewPackTaskbookTest -v` | 本任务验收全部行 |
| 2 | 验证范围与失败隔离，整理交付证据 | `tests/` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。设计方另做逐行验收与定向变异复核（去掉回退提取、对已探测成功路径改变行为，各自必须被对应断言抓住）；设计方做 G2（真实 `bin/dispatch review` 对照材料包含任务书）。同一失败连续三轮无新证据时停止该路径；不得自行扩大白名单、改原任务书、实现非目标。
