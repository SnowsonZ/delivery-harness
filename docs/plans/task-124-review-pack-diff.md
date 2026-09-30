---
task: T124
class: K7
risk: R3
designer: codex
size: small
architecture: true
spec_refs: []
no_spec_reason: 待办 B74 的评审材料包修复（diff 截断），无产品规格验收编号
budget:
  wall_clock_min: 90
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T124：评审材料包 diff 截断修复（B74）

负责方：**派发任务**。设计方：codex 席位（现由 Zcode 代行）；独立评审方 OpenCode。依据：待办 B74（已提级：#67/#72/#75/#78 四张 PR 的独立评审材料因截断失效）；[共用合同](2026-09-29-observability-task-contracts.md)C0。先读 `engine/agents/review.py` 的 `write_materials` 与 `engine/agents/review_pack.py` 的材料组装。

## 目标终态

1. diff 导出逻辑移入新模块 `engine/agents/review_pack_io.py`（`review.py` 的 `write_materials` 相应瘦身、调用新模块；`review.py` 净减行，不越 800 红线）：
   - 单文件 diff 总量 ≤ 2MB 时行为与现状一致（一个 `diff.patch`）；
   - 超过 2MB 时**按文件分片**落盘 `diff-01.patch`、`diff-02.patch`…（每片 ≤ 2MB，文件边界对齐，不拆半个文件），不再有静默截断；
   - 任何情况下 pack.md 的材料清单列出全部 diff 文件与各自行数；分片时 pack.md 顶部加一行显式说明「diff 分 N 片，无截断」。
2. `write_materials` 与调用方行为不变（签名、其余材料文件逐字不变）；`review_pack.py` 若引用 diff 文件名需同步分片清单（作为消费者）。
3. 回归测试为新增文件（见验收）。

实现前先核对 `write_materials` 现状与 `review_pack.py` 的 diff 引用点，差异停下报告设计方，不自行补实现。

## 白名单

- `engine/agents/review_pack_io.py`（新增）
- `engine/agents/review.py`（仅 `write_materials` 的 diff 段替换为对新模块的调用，净减行）
- `engine/agents/review_pack.py`（仅 diff 文件清单消费的同步）
- `tests/test_review_pack_io.py`（新增）
- `engine/prompts/review_prompt.md`（材料清单句改为「diff.patch，或超 2MB 时的 diff-01.patch、diff-02.patch… 全部分片」）
- `engine/agents/dispatch_observation.py`（`review_materials` 读取 diff 的单文件名改为 glob 分片集合，仍走观察旁路）
- `CHANGELOG.md`（Unreleased 补分片行为条目；无配置键无 Migration）

## 非目标

不实现其他任务；不改评审提示词与评审方配置；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0同样适用。

## 前置条件

- 工作区基于含依赖的最新main。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- 与同轮其他任务白名单不交叉（T122：dispatch/test_ci_workflows；T303：ci_events/工作流模板/新测试——零交叉）。
- 工具-only：G2 免（同 T118 先例）；设计方以一张真实大 diff PR 的出包做人工核对（合并前）。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；差异先修订任务书并经原审批。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B74 | 小 diff（<2MB）：单文件 `diff.patch` 内容与现状逐字一致 | 夹具 | `tests.test_review_pack_io.ReviewPackIoTest.test_small_diff_unchanged` | 行为变化时失败 |
| 不挂规格：B74 | 超 2MB 的 diff：按文件分片落盘、文件边界对齐、总量无丢失（与原始 diff 拼接等价） | 夹具 | `…test_large_diff_sharded_no_loss` | 截断或半文件分片时失败 |
| 不挂规格：B74 | 分片时 pack 材料清单列全量片文件与行数、顶部含「分 N 片，无截断」说明 | 夹具 | `…test_pack_lists_shards` | 清单缺失或无说明时失败 |
| 不挂规格：B74 | 设计方以一张真实大 diff PR 出包人工核对（结论写 PR 评论） | 人工 | PR 评论 | 无人工核对结论则不通过 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在用户授权派发后，执行方按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 新模块、write_materials 瘦身接线、回归断言 | `engine/agents/review_pack_io.py`、`engine/agents/review.py`、`engine/agents/review_pack.py`、`tests/test_review_pack_io.py` | `python3 -W error::ResourceWarning -m unittest tests.test_review_pack_io.ReviewPackIoTest -v` | 验收 1–3 行 |
| 2 | 验证范围，整理交付证据 | `tests/` | `bin/verify --full` | C0与本任务验收 |
| 3 | 设计方真实大 diff PR 出包核对（人工，合并前） | —— | 任一 ≥1MB diff 的 PR | 验收第 4 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收与定向变异复核（去掉分片逻辑回退为截断，行 2 必须失败；清单不列片文件，行 3 必须失败）。G2 免（同 T118 先例）。同一失败连续三轮无新证据时停止该路径；不得自行扩大白名单、改原任务书、实现非目标。


## 修订记录（2026-09-30，设计方裁决首轮评审「不通过」）

评审四条发现全部成立（两个消费方未接上是最关键的真问题——分片后大 diff 场景评审提示词找不到材料、C6 审计缺条目，修复在目标场景不能兑现）。第二轮补齐：

1. **评审提示词**（`engine/prompts/review_prompt.md`）：材料清单改为分片感知（单文件或全部分片）。
2. **C6 审计**（`dispatch_observation.py`）：`review_materials` 读 diff 改为 glob 分片集合，观察旁路保持；补大 diff 夹具断言。
3. **CHANGELOG**：Unreleased 补条目（分片行为、pack 新清单节）。
4. **人工验收（验收第 4 行）**：设计方以真实 ≥2MB diff PR 出包核对，结论写 PR 评论（本轮由设计方在合并前执行）。
