---
task: T120
class: K7
risk: R3
designer: codex
size: medium
architecture: true
spec_refs: []
no_spec_reason: 待办 B54 的拆分评审工具化，无产品规格验收编号
budget:
  wall_clock_min: 120
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T120：任务拆分评审工具化 `review-plan`（B54）

负责方：**派发任务**。设计方：codex 席位（现由 Zcode 代行）；独立评审方 OpenCode。依据：待办 B54；[任务拆分流程](../task-splitting.md)（现按其手工执行拆分评审）；先读 `engine/agents/review.py` 既有评审入口。

## 目标终态

1. `engine/agents/review.py` 新增 `review_plan(plan_doc: Path, output: Path) -> int`：以指定设计/计划文档（如 `docs/plans/2026-09-29-observability-execution-plan.md`）为主材料，自动收集同目录关联材料（该文档正文中链接到的 `docs/plans/*.md`、`docs/*.md` 本地文件，含追溯表、共用合同与全部 `task-*.md`），复用既有材料组装与评审执行（`write_materials` 同格式落盘、`run_reviewer`、`parse_output`），在只读评审工作区运行；结论（含 findings）与材料清单写入 `output`（markdown），**不评论到任何 GitHub PR**；设计文档不存在或链接目标缺失时列出缺失清单并在输出中标注（不静默跳过）。
2. `bin/dispatch` 新增子命令 `review-plan <设计文档路径> [--output 路径]`（缺省 `build/review/plan-verdict.md`）；既有子命令行为逐字不变。
3. 回归测试为新增文件（见验收）；既有测试全部不动。

实现前先核对 `task-splitting.md` 的材料范围描述与 `write_materials`/`run_reviewer` 现状，差异停下报告设计方，不自行补实现。

## 白名单

- `engine/agents/plan_review.py`（新增；实现所在模块，见修订记录）
- `engine/cli.py`（仅 `COMMANDS` 注册表追加 `review-plan` 一条与既有通用路由）
- `tests/test_review_plan.py`（新增）

## 非目标

不实现其他任务；不实现 B37 自动修复轮；不改任务拆分流程文档；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0同样适用。

## 前置条件

- 工作区基于含依赖的最新main。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- 与同轮其他任务白名单不交叉。**与 T119（B59）共用本任务两个源文件：必须等 T119 合并后再派发（串行）。**
- 工具-only、不改既有引擎行为：G2 免（同 T118 先例）；设计方以真实 `review-plan` 跑一次 B46 文档并核对材料清单（人工验收）。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；差异先修订任务书并经原审批。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B54 | 材料收集：以夹具文档为中心收集其链接的本地 docs 文件（含 task-*.md），缺失链接进缺失清单 | 夹具 | `tests.test_review_plan.ReviewPlanTest.test_material_collection_and_missing_list` | 漏收任务书或不报缺失时失败 |
| 不挂规格：B54 | 评审执行与产物：假评审方下结论与 findings 写入 output，无 GitHub 评论发出 | 夹具 | `…test_verdict_written_no_github_post` | 结论缺失或误发评论时失败 |
| 不挂规格：B54 | `review-plan` 子命令注册、缺省输出路径、既有子命令解析逐字不变 | 夹具 | `…test_cli_wiring` | 注册缺失或既有解析被改时失败 |
| 不挂规格：B54 | 设计方以真实评审方对 B46 文档跑一次并核对材料清单完整性（报告写入 PR 评论） | 人工 | PR 评论中的材料清单与结论 | 缺材料清单或结论则不通过 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在用户授权派发后，执行方按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | `review_plan` 与子命令、回归断言 | `engine/agents/review.py`、`engine/agents/dispatch.py`、`tests/test_review_plan.py` | `python3 -W error::ResourceWarning -m unittest tests.test_review_plan.ReviewPlanTest -v` | 验收 1–3 行 |
| 2 | 验证范围，整理交付证据 | `tests/` | `bin/verify --full` | C0与本任务验收 |
| 3 | 设计方真实运行（人工，合并前） | —— | `bin/dispatch review-plan docs/plans/2026-09-29-observability-execution-plan.md` | 验收第 4 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收与定向变异复核（材料收集漏掉 task-*.md、结论不写 output，各自必须被断言抓住）。工具-only：G2 免（同 T118 先例）。同一失败连续三轮无新证据时停止该路径；不得自行扩大白名单、改原任务书、实现非目标。


## 修订记录（2026-09-30，设计方裁决执行方升级）

执行方首轮实现完成并验证后停下报告：任务书原定实现位置（`review.py` +797/800 行、`dispatch.py` +796/800 行且 `main()` C901 10 顶格）与质量棘轮结构性冲突，无合规写法。设计方核实属实（行数与复杂度均逐字属实），裁决如下：

- **采纳执行方方案 1 的变体**：实现整体平移至新模块 `engine/agents/plan_review.py`；CLI 注册走 `engine/cli.py` 的 `COMMANDS` 注册表（数据驱动、123 行余量充足、C901 干净）；`engine/agents/review.py` 与 `engine/agents/dispatch.py` **从白名单移除并恢复 main 原样**——两个最大文件不越 800 行红线，棘轮意图最忠实。
- 首轮已提交的实现（`97a9a2c`，测试与定向变异全过）平移即可，测试仅改导入路径。
- **第 2 次（`--resume`）步骤**：平移实现至 `plan_review.py`、恢复两文件为 main 原样、`engine/cli.py` 注册、全类测试与 `bin/verify --full`；平移提交即步骤 1 的落地形态（「每步一提交」按此解释）。
- G2 免维持；人工验收（真实 `review-plan` 跑 B46 文档）维持。
