---
task: T113
class: K7
risk: R3
designer: codex
size: small
architecture: true
spec_refs: []
no_spec_reason: 待办 B67 的既有缺陷修复（r1 判定在无 main 提交仓库的行为未定义），无产品规格验收编号
budget:
  wall_clock_min: 90
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T113：r1 判定对无 main 提交仓库的既定行为

负责方：**派发任务**。设计方：codex 席位（现由 Zcode 代行）；独立评审方 OpenCode。依据：待办 B67（PR #35 独立评审发现的既有缺陷，非本次引入）；[共用合同](2026-09-29-observability-task-contracts.md)C0。

## 目标终态

仓库不存在 `origin/main` 引用（空仓库首个 PR、fresh clone 场景）时，r1 加强判定不再以未处理异常方式失败：

1. `r1_checks` 的各判定入口在 base 引用缺失时返回「无法核验」的明确结果（不抛 `git` 错误）；`risk.py` 接到该结果按既有降级口径处理（r1 → R2），给出可读理由（如「无法核验：base 引用不存在」）。
2. base 引用存在时全部既有行为逐字不变；新增函数/新增依赖/迁移语句/规模阈值四项判定逻辑不改。
3. 回归测试为新增文件（见验收），不修改任何既有测试。

实现前先在实际代码核对现状（失败点、异常类型、调用链），与上述不符时停下报告设计方，不自行改合同。

## 白名单

- `engine/checks/r1_checks.py`
- `engine/routing/risk.py`（仅允许把「base 缺失」的异常/未定义结果转为既有降级路径与理由文案）
- `tests/test_r1_missing_base.py`（新增）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0同样适用。

## 前置条件

- 工作区基于含依赖的最新main。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- 与同轮其他任务白名单不交叉。
- 改判定路径：设计方将在 Agent-Notification 独立 worktree 做等价验证（G2）。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；差异先修订任务书并经原审批。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B67 | 无 origin/main 的仓库跑 r1 判定：不抛异常，返回无法核验结果，risk 层降级 R2 且理由含「无法核验」 | 夹具 | `tests.test_r1_missing_base.R1MissingBaseTest.test_missing_base_degrades_to_r2` | git 错误传播或误判 r1 时失败 |
| 不挂规格：B67 | base 存在时既有四项判定（签名/依赖/迁移/规模）结果与现状逐字一致（既有仓库夹具对照） | 夹具 | `…test_existing_base_behaviour_unchanged` | 既有行为被改动时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在用户授权派发后，执行方按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 缺失 base 的既定行为与回归断言 | `engine/checks/r1_checks.py`、`engine/routing/risk.py`、`tests/test_r1_missing_base.py` | `python3 -W error::ResourceWarning -m unittest tests.test_r1_missing_base.R1MissingBaseTest -v` | 本任务验收全部行 |
| 2 | 验证范围与失败隔离，整理交付证据 | `tests/` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。设计方另做逐行验收与定向变异复核（去掉缺失 base 的保护、改动既有判定路径，各自必须被对应断言抓住）；设计方在 Agent-Notification 独立 worktree 做等价验证（G2）。同一失败连续三轮无新证据时停止该路径；不得自行扩大白名单、改原任务书、实现非目标。
