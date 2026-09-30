---
task: T123
class: K7
risk: R3
designer: codex
size: medium
architecture: true
spec_refs: []
no_spec_reason: B70 的支撑重构（dispatch.py 结构整理），无产品规格验收编号
budget:
  wall_clock_min: 120
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T123：GitHub 封装类抽离至 engine/agents/github.py

负责方：**派发任务**。设计方：codex 席位（现由 Zcode 代行）；独立评审方 OpenCode。依据：待办 B70 的支撑重构（`dispatch.py` 796/800 行、C901 10 顶格，B70 实现无空间——T120 同源裁决先例）；[共用合同](2026-09-29-observability-task-contracts.md)C0。先读 `engine/agents/dispatch.py` 的 GitHub 类与其引用点。

## 目标终态

1. `engine/agents/dispatch.py` 中的 `GitHub` 类及其专属常量（`CI_QUERY_ATTEMPTS`、`CI_QUERY_RETRY_SECONDS`）与 `# ---- GitHub（推送与写操作以 Agent 身份经 bin/as-agent） ----` 分节整体**逐字搬移**到新模块 `engine/agents/github.py`；仅调整模块内 import。
2. `dispatch.py` 保留再导出行 `from engine.agents.github import GitHub`，使 `dispatch.GitHub` 引用（review.py 的 `dispatch.GitHub(root)`、既有测试）逐字继续可用；`dispatch.py` 行数较 main 下降 ≥100。
3. 搬移为纯移动：类代码零修改；既有全部测试零修改逐字通过。

## 白名单

- `engine/agents/github.py`（新增）
- `engine/agents/dispatch.py`（仅删除搬移代码与新增一行再导出 import）
- `tests/test_github_extraction.py`（新增）

## 非目标

不实现其他任务；不实现 B70/B71/B72；不借机修改 GitHub 类任何行为；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0同样适用。

## 前置条件

- 工作区基于含依赖的最新main。逐项核对实际接口（GitHub 类及其引用点），差异停下报告设计方，不自行补实现。
- 与同轮其他任务白名单不交叉。
- 工具-only、纯搬移：G2 免（556 对照由设计方抽查）。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；差异先修订任务书并经原审批。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B70 | 既有全部测试零修改逐字通过 | 夹具 | `bin/verify --full`（tests 项）与 `tests.test_events_agents` 全类 | 任何既有断言被改动时失败 |
| 不挂规格：B70 | `dispatch.GitHub is github.GitHub` 且两模块无循环导入（双 import 断言） | 夹具 | `tests.test_github_extraction.GithubExtractionTest.test_alias_and_no_cycle` | 再导出缺失或循环导入时失败 |
| 不挂规格：B70 | `dispatch.py` 行数较 main 下降 ≥100 且新模块含完整 GitHub 类 | 夹具 | `…test_dispatch_shrinks_github_moved` | 搬移不完整时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在用户授权派发后，执行方按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | GitHub 类逐字搬移、再导出与抽离断言 | `engine/agents/dispatch.py`、`engine/agents/github.py`、`tests/test_github_extraction.py` | `python3 -W error::ResourceWarning -m unittest tests.test_events_agents tests.test_github_extraction.GithubExtractionTest.test_alias_and_no_cycle tests.test_github_extraction.GithubExtractionTest.test_dispatch_shrinks_github_moved -v` | 本任务验收全部行 |
| 2 | 验证范围，整理交付证据 | `tests/` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收与定向变异复核（删除再导出行，行 2 必须失败；在 github.py 改一处类行为，行 1 的既有测试必须失败）。纯搬移：G2 免（556 对照由设计方抽查）。同一失败连续三轮无新证据时停止该路径；不得自行扩大白名单、改原任务书、实现非目标。
