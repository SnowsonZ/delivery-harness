---
task: T119
class: K7
risk: R3
designer: codex
size: medium
architecture: true
spec_refs: []
no_spec_reason: 待办 B59 的评审校准集与打分工具，无产品规格验收编号
budget:
  wall_clock_min: 150
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T119：评审校准集与打分工具（B59）

负责方：**派发任务**。设计方：codex 席位（现由 Zcode 代行）；独立评审方 OpenCode。依据：待办 B59；触发实证：T116 的「假修复逃逸」（评审对测试判别力不敏感，PR #49 第二轮才抓回）与 T101 的两处漏检变异。先读 [共用合同](2026-09-29-observability-task-contracts.md)C0 与 `engine/agents/review.py` 的既有评审入口。

## 目标终态

1. 样本清单 `docs/review/calibration/samples.json`（新增）：6 个真实历史样本，每条含 `pr`（本仓库 PR 号）、`head`（40 位提交）、`expected`（通过/不通过/需用户验收）、`reason`（一句裁决依据）。样本与期望由设计方按下表裁决（已逐条对照真实评审历史）：

   | PR | head | expected | 依据 |
   |---|---|---|---|
   | #49 | `bce25432cbc2644d6c11336e302f3e1b97f1c21b` | 不通过 | 空洞接线断言 + macOS CI 失败（第二轮真实评审结论） |
   | #49 | `639bf1cedb7729aa4d6d7d1fa4426c60a40464b7` | 通过 | 确定性锁用例修复后的第三轮真实评审结论 |
   | #51 | `48ad84b1e5e8572ea35c69a52fcf9c9b1e75135a` | 不通过 | 未知键回显（严重）+ 消费方旧格式误拒（G2 实测违约） |
   | #41 | 见 PR head | 通过 | T113 全链复核通过的真实任务 PR |
   | #33 | `b8e48fb99223faa7615b15707e214d2478db4366` | 不通过 | 缺 `[events]` Migration 条目（首轮真实评审结论） |
   | #43 | 见 PR head | 通过 | T201 全链复核通过的真实任务 PR |

   「见 PR head」的样本：manifest 里写该 PR 合并时的 head（执行方用 `gh pr view <n> --json headRefOid` 取当前值并写入 manifest，与任务书表格的语义一致即可）。

2. `engine/agents/review.py` 新增 `calibrate(review_name: str | None, samples_path: Path, output: Path) -> int`：逐样本复用既有 `review_pr` 的材料组装与评审执行（检出样本 head、组材料、跑评审方、解析结论），比对 `expected`，累计 TPR（期望不通过的抓中率）、TNR（期望通过的放行率）与逐样本偏差表；结论与打分写入 `output`（markdown），**不评论到任何 GitHub PR**；样本 head 检出失败或评审方本身失败不计入 TPR/TNR 分母、单独列为 `errors`。
3. `bin/dispatch` 新增子命令 `review-calibrate [--reviewer 名称] [--output 路径]`（缺省跑全部样本、输出 `build/review/calibration-report.md`）；既有子命令行为逐字不变。
4. 回归测试为新增文件（见验收）；既有测试全部不动。

## 白名单

- `engine/agents/dispatch.py`（仅 `review-calibrate` 子命令注册与参数转发）
- `engine/agents/review.py`（`calibrate` 及其样本装载/逐样本执行辅助）
- `docs/review/calibration/samples.json`（新增）
- `tests/test_review_calibration.py`（新增）

## 非目标

不实现其他任务；不实现 B37 的自动修复轮；不修改任何校准样本的期望值；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0同样适用。

## 前置条件

- 工作区基于含依赖的最新main。逐项核对实际接口（`review_pr`/`write_materials`/`run_reviewer`/`parse_output`），差异停下报告设计方，不自行补实现。
- 与同轮其他任务白名单不交叉。**与 T120（B54）共用本任务两个源文件，二者必须串行派发（T119 先）。**
- 真实样本全量打分属验收后的人工执行（设计方跑一次并报告基线分数），测试用假评审方覆盖机制。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；差异先修订任务书并经原审批。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B59 | 打分机制：假评审方按样本期望返回时 TPR=TNR=1.0；全部反着答时 TPR=TNR=0.0；评审方失败进 `errors` 不进分母 | 夹具 | `tests.test_review_calibration.ReviewCalibrationTest.test_scoring_and_errors` | 打分算错或失败样本污染分母时失败 |
| 不挂规格：B59 | 样本装载：manifest 缺字段/`expected` 非法值报明确错误；6 条真实样本可装载且 PR 号与 head 对应 | 夹具 | `…test_manifest_loading_and_real_samples` | 装载静默失败或样本缺失时失败 |
| 不挂规格：B59 | `review-calibrate` 子命令注册且缺省输出路径生效；既有子命令解析逐字不变 | 夹具 | `…test_cli_wiring` | 子命令缺失或既有解析被改时失败 |
| 不挂规格：B59 | 设计方以真实评审方跑一次全量校准并报告 TPR/TNR 基线（写入 PR 评论） | 人工 | PR 评论中的基线分数 | 无基线分数则不通过 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在用户授权派发后，执行方按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | manifest、calibrate 与子命令、回归断言 | `docs/review/calibration/samples.json`、`engine/agents/review.py`、`engine/agents/dispatch.py`、`tests/test_review_calibration.py` | `python3 -W error::ResourceWarning -m unittest tests.test_review_calibration.ReviewCalibrationTest -v` | 验收 1–3 行 |
| 2 | 验证范围，整理交付证据 | `tests/` | `bin/verify --full` | C0与本任务验收 |
| 3 | 设计方真实全量校准（人工，合并前） | —— | `bin/dispatch review-calibrate`（真实评审方） | 验收第 4 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收与定向变异复核（打分公式反向、manifest 校验放松，各自必须被断言抓住）。工具-only、不改既有引擎行为：G2 免（同 T118 先例）。同一失败连续三轮无新证据时停止该路径；不得自行扩大白名单、改原任务书、实现非目标。


## 修订记录（2026-09-30，设计方裁决执行方升级取证）

执行方第二轮实现时取证发现：**main 的 `consumer-contract` job 在红**——消费方契约测试 `test_calibration_stops_after_three_failures_and_resumes`（Agent-Notification `tests/test_harness_review_independent.py`）调用 `review.calibrate(..., resume=...)`，当前引擎的 `calibrate` 不满足该签名。该测试在本地运行时被 skip、仅 CI 环境真实执行，故此前的 556 本地对照未暴露。裁决：

- **calibrate 的目标形状以消费方契约为准**：实现须读消费方 `tests/test_harness_review_independent.py` 中该测试（及其引用的辅助）所期望的 `calibrate` 签名与语义（含 `resume` 参数、三次失败停止、样本回放），按其实现；与 B59 打分需求的合并方式由实现方在该形状内完成（打分入口可另立 `review_calibrate`，消费方契约只约束 `calibrate`）。
- **新增验收（人工）**：以本 PR 引擎 upgrade 消费方 main 后，其 harness 契约测试**全绿**（`consumer-contract` job 在本仓库 CI 同步变绿即同等证据）；这是本任务合并的前置条件。
- **记录卫生**：执行方取证用的 CI 日志含 `/home/runner/...`，prompt 快照须占位后方可提交（本轮已由设计方按此落库）；后续样本执行输出同理。
- 白名单不变（`calibrate` 在 `engine/agents/review.py` 内）；既有 4 行验收不变。
