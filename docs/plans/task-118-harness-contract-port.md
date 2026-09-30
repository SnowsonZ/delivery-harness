---
task: T118
class: K7
risk: R3
designer: codex
size: medium
architecture: true
spec_refs: []
no_spec_reason: 待办 B42 的契约测试移植（夹具化），无产品规格验收编号
budget:
  wall_clock_min: 150
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T118：消费方契约测试移植（夹具化，B42）

负责方：**派发任务**。设计方：codex 席位（现由 Zcode 代行）；独立评审方 OpenCode。依据：待办 B42；[共用合同](2026-09-29-observability-task-contracts.md)C0。蓝本：Agent-Notification main 的 `tests/test_harness_dispatch.py`、`tests/test_harness_review_independent.py`（已随 #71 对齐当前引擎接口）。

## 目标终态

把消费方的 harness 契约测试移植为本仓库可独立运行的夹具化测试，本仓库单独 `verify` 即可验证，不依赖检出消费方仓库：

1. 新增 `tests/test_harness_contract_dispatch.py` 与 `tests/test_harness_contract_review.py`：以蓝本两文件的场景为基准（派发成功链/CI 失败重试/预算升级、评审只读性/结论解析/失败判定/校准停止），改用本仓库测试的既有夹具模式（临时 git 仓库、隔离 ROOT、假 gh/host、冻结时钟）。
2. 蓝本中的 `FakeGitHub.wait_ci` 形参、`reviewer.read` 三元组等接口断言按**本仓库当前引擎**逐项核对后移植，不照抄过期形状；与蓝本行为不一致处逐条在文件头注释列明原因。
3. 既有测试全部不动；`tests/test_harness_contract_*` 在消费方仓库的对应文件保留（B44 的 CI job 继续在消费方侧运行，两处互为冗余防线）。

实现前先读蓝本两文件与本仓库既有测试夹具模式，无法夹具化的场景（如强依赖真实 gh 的）列为跳过并注释原因，报告设计方确认。

## 白名单

- `tests/test_harness_contract_dispatch.py`（新增）
- `tests/test_harness_contract_review.py`（新增）

## 非目标

不实现其他任务；不修改消费方仓库任何文件；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0同样适用。

## 前置条件

- 工作区基于含依赖的最新main。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- 与同轮其他任务白名单不交叉。
- 测试-only 任务：G2 不适用（无引擎行为变化）；设计方以「在干净克隆删除断言须失败」抽查测试真实性。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；差异先修订任务书并经原审批。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B42 | 派发契约：成功链写记录开 PR 且 main 不动、CI 失败带摘要重试后通过、超预算打标签升级——全部以夹具仓库运行 | 夹具 | `tests.test_harness_contract_dispatch.DispatchContractTest` 三个具名用例 | 任一场景断言缺失或失败 |
| 不挂规格：B42 | 评审契约：只读评审、结论 JSON 解析（通过/不通过/需验收）、评审方失败不是结论、三次失败校准停止 | 夹具 | `tests.test_harness_contract_review.ReviewContractTest` 四个具名用例 | 同上 |
| 不挂规格：B42 | 移植差异清单：文件头逐条列明与蓝本的形状差异及原因 | 人工 | 文件头注释（设计方核对） | 照抄过期形状或缺差异说明时不通过 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在用户授权派发后，执行方按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 两个契约测试文件 | `tests/test_harness_contract_dispatch.py`、`tests/test_harness_contract_review.py` | `python3 -W error::ResourceWarning -m unittest tests.test_harness_contract_dispatch tests.test_harness_contract_review -v` | 本任务验收全部行 |
| 2 | 验证范围，整理交付证据 | `tests/` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收与真实性抽查（在副本删除被测行为的关键分支，移植测试必须失败）。同一失败连续三轮无新证据时停止该路径；不得自行扩大白名单、改原任务书、实现非目标。
