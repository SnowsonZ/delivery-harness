---
task: T115
class: K7
risk: R3
designer: codex
size: small
architecture: true
spec_refs: []
no_spec_reason: 待办 B44 的 CI 工作流任务（消费方契约测试入 CI），无产品规格验收编号
budget:
  wall_clock_min: 90
  ci_rounds: 3
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T115：CI 消费方契约测试（B44）

负责方：**派发任务**。设计方：codex 席位（现由 Zcode 代行）；独立评审方 OpenCode。依据：待办 B44；[共用合同](2026-09-29-observability-task-contracts.md)C0。

## 目标终态

`.github/workflows/ci.yml` 新增一个 `consumer-contract` job（与现有 test 矩阵并列）：

1. 检出 [Agent-Notification](https://github.com/SnowsonZ/Agent-Notification) main（浅克隆即可）到独立目录；
2. 用本仓库当前检出（`${{ github.workspace }}`）的 `engine/cli.py upgrade --target <消费方目录> --allow-dirty` 升级其 `.harness`；
3. 在消费方目录跑其 harness 契约测试：`python3 -W error::ResourceWarning -m unittest discover -s tests -p "test_harness*.py"`；
4. 在消费方目录跑 `bin/verify --quick`；
5. 任一步失败即 job 失败；job 名称登记进 ruleset 所需检查的方式不在本任务内（沿用现有 harness/test 检查名，本 job 为附加信号，不改 `.github/rulesets/`）。

前置说明：消费方 main 当前已运行升级后引擎且契约测试全绿（#71 已合并），本 job 的价值在于引擎后续变更破坏契约时 CI 立即变红。工作流语法正确性以本 PR 自己的 CI 运行为准（job 必须在本 PR 上真实跑过并通过）。

实现前先核对现有 ci.yml 结构与消费方测试布局，与上述不符时停下报告设计方，不自行改合同。

## 白名单

- `.github/workflows/ci.yml`
- `tests/test_consumer_contract_workflow.py`（新增；对工作流 YAML 的结构断言：job 名、步骤要点、升级命令与测试模式存在）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、`.github/rulesets/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0同样适用。

## 前置条件

- 工作区基于含依赖的最新main。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- 与同轮其他任务白名单不交叉。
- CI-only 改动：无引擎行为变化，G2 不适用；验收以本 PR CI 实跑为准。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；差异先修订任务书并经原审批。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B44 | ci.yml 含 `consumer-contract` job：含消费方检出、`engine/cli.py upgrade --target`、`test_harness*` 发现式运行、`verify --quick` 四要素 | 夹具 | `tests.test_consumer_contract_workflow.ConsumerContractWorkflowTest.test_job_structure` | 缺任一要素时失败 |
| 不挂规格：B44 | 本 PR 的 CI 中该 job 真实运行且通过（以 checks 页为证据，设计方复核核对） | 人工 | PR checks 的 `consumer-contract` 结果（设计方复核核对） | job 未跑或失败时不通过 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在用户授权派发后，执行方按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | ci.yml 新 job 与结构断言 | `.github/workflows/ci.yml`、`tests/test_consumer_contract_workflow.py` | `python3 -W error::ResourceWarning -m unittest tests.test_consumer_contract_workflow.ConsumerContractWorkflowTest -v`；本 PR CI | 本任务验收全部行 |
| 2 | 验证范围，整理交付证据 | `tests/` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收与定向变异复核（删除 job 关键步骤、改错升级命令，各自必须被结构断言抓住）。同一失败连续三轮无新证据时停止该路径；不得自行扩大白名单、改原任务书、实现非目标。
