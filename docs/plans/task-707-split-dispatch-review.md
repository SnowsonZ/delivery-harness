---
task: T707
class: K7
risk: R3
designer: claude-code
size: medium
architecture: true
spec_refs: []
no_spec_reason: 待办 B81 自治试验第二波阻塞：dispatch.py 与 review.py 逼近质量棘轮 800 行上限，T703、T702 在白名单内无解（T703 执行方升级说明），先做行为不变的拆分，无产品规格验收编号
budget:
  wall_clock_min: 120
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T707：行为不变地拆分 dispatch.py 与 review.py（为 T703、T702 腾出体量余量）

负责方：**派发任务**。设计方：claude-code；独立评审方：Codex。依据：[自治试验设计](2026-10-03-autonomy-trial-design.md)第 9 节；T703 第 1 次派发的执行方升级说明（退出方式 clarify）。

**病灶（代码证据）**：
- 质量棘轮 `.harness/state/quality-baseline.json` 的 `files_over_800` 为 0，统计口径见 `engine/checks/quality.py` 的 `measure`：`engine` 下 `**/*.py` 物理行数 `> 800` 才计入。
- `engine/agents/dispatch.py` 现为 789 行。T703 实现后达到 864 行，`bin/verify --full` 卡在 quality，pre-push 拒绝推送。T702 还要往这个文件里加 `signoff` 子命令。
- `engine/agents/review.py` 现为 800 行，已到上限。T702 要往里加四处改动，约 30–40 行。

## 目标终态

1. **只移动代码，不改逻辑**：移出的函数逐字搬走（允许的改动只有 import 与调用方式，见第 3 条），原模块用 `from … import …` 重新导出这些名字。所有现有的 `dispatch.X`、`review.X` 写法照常可用，包括 `engine/agents/plan_review.py` 的 `from engine.agents.review import _cell` 等。
2. **移出范围**：
   - `engine/agents/dispatch.py` → 新模块 `engine/agents/dispatch_text.py`：PR 与升级文本组装，即 `_title`、`_pr_title`、`_manual_section`、`pr_body`、`escalation_body`（现第 617–681 行附近）。
   - `engine/agents/dispatch.py` → 新模块 `engine/agents/dispatch_slots.py`：槽位管理，即 `acquire_slot`、`update_slot`、`release_slot`、`_registered_worktrees`、`_salvage_and_remove`、`reclaim_stale_slots`、`return_slot`、`prepare_slot`（现第 176–304 行附近）。
   - `engine/agents/review.py` → 新模块 `engine/agents/review_calibration.py`：B59 真实历史样本校准段，即 `load_samples`、`prepare_head_sample`、`sample_deviates`、`score_samples`、`_cell`、`render_calibration_report`、`review_calibrate`（现第 630–780 行附近，标题「评审校准（B59：真实历史样本）」）。
   - **不移**：`review.py` 中较早的校准段（`calibration_samples`、`prepare_sample`、`calibrate`、`score`，第 511–629 行）。`tests/test_harness_contract_review.py` 第 182–187 行对这一段在 review 模块上 patch 了 `calibration_samples`、`prepare_sample`、`git`、`review_workspace`、`write_materials`、`EVALS`，搬走会破坏 patch 语义。
3. **保持 patch 语义**：现有测试在原模块上 patch 的名字，必须继续拦截到移出后的代码。已知被 patch 的名字：`review`（`make_reviewer`、`review_pr`、`review_pending`、`watch`、`review_calibrate`、`load_rules`）和 `dispatch`（`state_dir`、`status`、`stop_all`、`prepare_guard`、`GitHub`、`Dispatcher`、`Config`）。规则如下：
   - 移出模块要调用原模块里的函数，或调用会被 patch 的名字时，**在函数体内**按需 `from engine.agents import dispatch`（或 `review`），再以 `dispatch.<名字>` 的形式调用。不在模块顶层 from-import，这样既保留 patch 语义，又避免循环导入。
   - 原模块调用移出的函数时，照常使用重新导出的名字。
4. **体量目标**：拆分后 `dispatch.py` 不超过 660 行，`review.py` 不超过 680 行；三个新模块各自不超过 400 行。`quality-baseline.json` 不改，其余质量指标（如 `complex_functions`）不得上升。
5. 回归测试放在新文件 `tests/test_split_modules.py`，内容见验收表。

## 白名单

- `engine/agents/dispatch.py`
- `engine/agents/dispatch_text.py`（新增）
- `engine/agents/dispatch_slots.py`（新增）
- `engine/agents/review.py`
- `engine/agents/review_calibration.py`（新增）
- `tests/test_split_modules.py`（新增）

消费方扫描（检查单第 2 项）：

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| 本仓库全部测试，尤其是上面列出的 patch 点，以及 `tests/test_events_agents.py:934` 直接调用的 `dispatch.pr_body` | 无需（零修改） | 原模块重新导出这些名字；会被 patch 的名字按第 3 条，通过原模块属性调用 |
| `engine/agents/plan_review.py`、`engine/agents/review_pack.py` 等引擎内的 from-import | 无需 | 重新导出后，导入路径不变 |
| Agent-Notification 的 `tests/test_harness*.py`（消费方契约 CI 会跑） | 无需 | 只 patch `review.write_materials`、`review_workspace`、`prepare_sample`、`git`、`calibration_samples`、`EVALS` 和 `dispatch.prepare_guard`，这些都留在原模块 |
| `.harness/state/quality-baseline.json` | 不改 | 拆分本身让指标回落，不借机调整基线 |

CHANGELOG：行为不变，用户不可见，不写。

## 非目标

- 不改任何函数的逻辑、签名、文案或事件。
- 不移动较早的校准段。
- 不实现 T703、T702 的任何功能。
- 不改质量基线。
- 白名单以外的文件一律不改。
- 共用合同 C0 同样适用。

## 前置条件

- 工作区基于最新的 `origin/main`。核对第 2 条中的行号、函数名与 patch 点清单，有出入就停下，向设计方报告。
- 和正在运行的 T705 白名单不交叉（T705 改的是 `ledger.py`、`audit.py`、`harness.yml`），可以并行。
- 实现前先确认本任务书已在 main 上：`bin/harness taskbook docs/plans/task-707-split-dispatch-review.md --on-main` 通过。

## 验收

具名测试为本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B81 | 拆分后 `dispatch.py` 不超过 660 行，`review.py` 不超过 680 行，三个新模块各自不超过 400 行；`files_over_800` 为 0 | 夹具 | `tests.test_split_modules.SplitModulesTest.test_line_budgets` | 没拆，或拆得不够，体量断言失败 |
| 不挂规格：B81 | 第 2 条列出的每个名字，仍可从原模块取到，而且与新模块里的是同一个对象（`dispatch.pr_body is dispatch_text.pr_body` 等） | 夹具 | `…test_reexports_are_identical` | 漏了重新导出，或者复制了一份 |
| 不挂规格：B81 | 在原模块上 patch `review.make_reviewer` 之后调用 `review.review_calibrate`，确实会走到 patch 后的函数；`dispatch.state_dir` 也按同样方式核对一个移出的槽位函数 | 夹具 | `…test_patch_semantics_preserved` | 移出的代码在模块顶层就绑定了引用，测试的 patch 拦不到 |
| 不挂规格：B81 | 先单独 `import engine.agents.review_calibration`、`dispatch_slots`、`dispatch_text`，再导入原模块，都没有循环导入错误 | 夹具 | `…test_import_order_independent` | 循环导入 |
| 不挂规格：B81 | 既有测试全部通过、零修改；`bin/verify --full` 中 quality 通过 | 夹具 | `bin/verify --full` | patch 语义或行为变了 |
| 不挂规格：B81 | 设计方 G2：在 Agent-Notification 的独立 worktree 中升级后跑 `bin/verify --full`，与升级前一致 | 人工 | PR 描述 | 消费方行为被意外改变 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 移出 PR 文本段与槽位段，重新导出并改为按需调用 | `engine/agents/dispatch.py`、`engine/agents/dispatch_text.py`、`engine/agents/dispatch_slots.py` | `python3 -W error::ResourceWarning -m unittest discover -s tests` | 验收第 5 行 |
| 2 | 移出 B59 校准段，重新导出并改为按需调用 | `engine/agents/review.py`、`engine/agents/review_calibration.py` | 同上 | 验收第 5 行 |
| 3 | 回归断言 | `tests/test_split_modules.py` | `python3 -W error::ResourceWarning -m unittest tests.test_split_modules -v` | 验收第 1–4 行 |
| 4 | 全量验证，整理交付证据 | `tests/` | `bin/verify --full` | 验收第 5 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收、G2 与定向变异复核，以下三个变异必须各自被对应断言抓住：
- 去掉一个重新导出；
- 把一个移出函数改为在模块顶层 from-import 原模块的名字；
- 把 `review_calibrate` 移回 `review.py`，让体量超标。

同一失败连续三轮没有新证据时，停止这条路径。不得自行扩大白名单、修改任务书、实现非目标。

## 后续（设计方）

T707 合并后，设计方把 T703 已完成的提交（`13bef1d`，已打包保存）变基到新的 main，推回 `task/703-review-after-ci`，再用 `bin/dispatch run --resume` 续跑。T703 的任务书不变：它新增的 `review_with_chain` 仍定义在 `dispatch.py` 中，拆分后有足够余量。

## 修订记录 1（2026-10-04，设计方：体量断言的口径）

验收第 1 行「拆分后 `dispatch.py` 不超过 660 行、`review.py` 不超过 680 行」是 T707 拆分当时要腾出余量的**一次性目标**，不是长期约束。设计方写任务书时把它写成了永久生效的测试，结果 T703 按自己的任务书在 `dispatch.py` 新增 `review_with_chain` 后为 691 行（远低于质量棘轮的 800 行），也被拦下。修订如下：

- 长期约束只有与质量棘轮同口径的全局断言：`engine` 下任何文件不超过 800 行。三个新模块各自不超过 400 行的断言保留。
- 测试改动已随 #120（T703）由设计方提交（`32c8e54`）。#127 的补审指出，那次改动越出了 T703 的白名单，也没有走合同修订。本条即为补记的合同依据。
- B89 收尾（移出模块之间调用时保持 patch 语义、导入顺序测试的假阳性）由 T708 完成。

