---
task: T112
class: K7
risk: R3
designer: codex
size: small
architecture: true
spec_refs: []
no_spec_reason: 待办 B60/B61 的守卫结构与解析小修，无产品规格验收编号
budget:
  wall_clock_min: 90
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T112：命令守卫 heredoc 误报与任务书路径解析小修

负责方：**派发任务**。设计方：codex 席位（现由 Zcode 代行，见执行计划 §1 分工）；独立评审方 OpenCode。依据：待办 B60、B61；[共用合同](2026-09-29-observability-task-contracts.md)C0。B60 已三次实际误拦（2026-09-29 文档修改、2026-09-30 文档起草与任务书起草）。

## 目标终态

1. B60 — `engine/guards/command_guard.py` 的覆盖变量判定（`override_var` 规则）改为按 shell 结构解析（解析逻辑放 `engine/guards/shell_structure.py`）：

   - heredoc 正文（`<<`、`<<-`、`<<<`）与引号内文字一律当数据，不参与覆盖变量判定；
   - 只在真正的赋值位置判定：命令前缀赋值（`VAR=… cmd`）、`env VAR=…`、`export VAR=…`；
   - 真赋值仍然拒绝且能力不回退：`HARNESS_*`、`*_ALLOW_(MAIN|TAG|REWRITE)`、`_SKIP_VERIFY` 及拼接形式（如 `${P}_ALLOW_TAG=…`）在赋值位置一律拦截；引号内、heredoc 正文、普通参数（如 `grep HARNESS_ …`）不再误拦。

2. B61 — `engine/checks/taskbook.py` 的 `PATH_TOKEN` 识别根目录多点文件名：`README.zh-CN.md` 这类多点根文件名被完整识别（不再截成 `zh-CN.md`），参与「涉及文件」与类别/风险的交叉核对；既有单点与多级路径行为不变。

3. 回归测试为两个新文件（见验收），不修改任何既有测试。

## 白名单

- `engine/guards/command_guard.py`
- `engine/guards/shell_structure.py`
- `engine/checks/taskbook.py`
- `tests/test_command_guard_structure.py`（新增）
- `tests/test_taskbook_step_paths.py`（新增）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫（真赋值拦截能力不得回退）；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0同样适用。

## 前置条件

- 依赖 T104、T108 已合并且工作区基于含依赖的最新main。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- 与 T106/T111 无白名单交叉，可并行派发。
- 改守卫：设计方将在 Agent-Notification 独立 worktree 做等价验证（G2，升级前后 `bin/verify --full` 加 heredoc 真机对照）。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以"按实际代码"为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T112 | heredoc 正文含 `HARNESS_ALLOW_TAG=1` 字样不触发覆盖变量拒绝，命令按其余规则正常判定 | 夹具 | `tests.test_command_guard_structure.CommandGuardStructureTest.test_heredoc_body_is_data` | 正文仍被当赋值扫描时误拒、断言失败 |
| 不挂规格：B46 T112 | here-string（`<<<`）正文同样当数据 | 夹具 | `…test_here_string_is_data` | 同上 |
| 不挂规格：B46 T112 | 引号内文字当数据（`echo 'HARNESS_ALLOW_TAG=1'` 放行），现状行为锁定 | 夹具 | `…test_quoted_text_is_data` | 引号内容被扫描时失败 |
| 不挂规格：B46 T112 | 真赋值仍拒绝：前缀 `VAR=…`、`env VAR=…`、`export VAR=…` 三种位置含 `HARNESS_*` 与拼接 `${P}_ALLOW_TAG=` 全部拦截 | 夹具 | `…test_real_assignment_still_denied` | 任一赋值位置放行即失败（守卫能力回退） |
| 不挂规格：B46 T112 | `PATH_TOKEN` 完整识别根目录多点文件名（如 `README.zh-CN.md`）并参与交叉核对；单点 `README.md` 与多级路径行为不变 | 夹具 | `tests.test_taskbook_step_paths.TaskbookStepPathsTest.test_root_multi_dot_step_paths` | 截断成 `zh-CN.md` 时交叉核对缺失、断言失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在用户授权派发后，执行方按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 守卫结构化解析与路径解析修正、新建两个测试文件 | `engine/guards/command_guard.py`、`engine/guards/shell_structure.py`、`engine/checks/taskbook.py`、`tests/test_command_guard_structure.py`、`tests/test_taskbook_step_paths.py` | `python3 -W error::ResourceWarning -m unittest tests.test_command_guard_structure.CommandGuardStructureTest tests.test_taskbook_step_paths.TaskbookStepPathsTest -v` | 本任务验收全部行 |
| 2 | 验证范围与失败隔离，整理交付证据 | `tests/` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。设计方另做逐行验收与定向变异复核（去掉 heredoc 数据化、放行真赋值、恢复 PATH_TOKEN 旧字符类，各自必须被对应断言抓住）；改守卫由设计方在 Agent-Notification 独立 worktree 做等价验证（G2，含 heredoc 命令真机对照）。同一失败连续三轮无新证据时停止该路径；不得自行扩大白名单、改原任务书、实现非目标。
