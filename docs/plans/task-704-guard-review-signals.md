---
task: T704
class: K7
risk: R3
designer: claude-code
size: small
architecture: true
spec_refs: []
no_spec_reason: 待办 B81 自治试验设计第 6 节第 2 行（执行方不能伪造自动合并读取的评审与复核结论），无产品规格验收编号
budget:
  wall_clock_min: 90
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T704：守卫禁止执行方写评审与复核结论（评论、评审、review、signoff）

负责方：**派发任务**。设计方：claude-code；独立评审方：OpenCode。依据：[自治试验设计](2026-10-03-autonomy-trial-design.md)第 6 节。合同制路径（T701）读取的是 Agent 账号在 PR 上留下的评审标记和复核标记，而执行方用的是同一个账号，所以必须挡住执行方写这类结论的常规路径。

**病灶（代码证据）**：
- `engine/core/shell_structure.py` 的 `_gh`（第 404 行起）与 `_gh_issue_and_label`（第 385 行起）：对执行方只拦议题的改动和标签，`gh pr comment`、`gh issue comment`、`gh pr review`（不带 `--approve`）都会放行。
- `_check_simple`（第 172 行起）对 `bin/dispatch` 不做任何判定。
- 结构化解析成功时，`command_guard.IMPLEMENTER_COMMAND_RULES` 这张正则表**不会被用到**，它只在解析失败时兜底。所以只加正则不够，结构化路径和兜底路径都要加。

## 目标终态

1. `engine/core/shell_structure.py` 新增理由常量 `REVIEW_SIGNAL`，文案为：「执行者不能发表 PR 或议题评论、提交评审、运行独立评审或设计方复核：这些是自动合并读取的结论，由评审方与设计方写」。仅在 `role == "implementer"` 时拒绝以下命令：
   - `gh pr comment`、`gh issue comment`；
   - `gh pr review`，不论带什么参数；
   - 可执行文件名为 `dispatch` 且第一个位置参数是 `review`、`review-calibrate` 或 `signoff` 的命令（例如 `bin/dispatch review 12`、`./bin/dispatch signoff 12`）。
2. `engine/guards/command_guard.py`：
   - `IMPLEMENTER_COMMAND_RULES` 增加对应的兜底正则，规则键为 `review_signal`；
   - `_COMMAND_REASON_KEYS` 加映射 `REVIEW_SIGNAL → "review_signal"`；
   - `IMPLEMENTER_TOOL_RULES` 增加 MCP 评论类工具名（`add_issue_comment`、`create_issue_comment`、`add_pull_request_comment`、`add_comment_to_pending_review`），规则键为 `tool_review_signal`。
3. 设计方角色（缺省）对上面这些命令一律放行，行为与现在相同。
4. `engine/routing/run_check.py` 的 `_TRUSTED_GUARD_REASONS`（第 180 行起）加入 `shell_structure.REVIEW_SIGNAL`。原因：守卫拦下执行方之后，这条理由会进入运行记录的 `guard_denials`；记录内容校验（第 447 行起）只认清单里的理由，不在清单里的会被判为「未知理由」，导致这条记录不合格，派发断链（拆分评审第二轮严重项 4）。
5. 合并、批准两条理由的现有文案（「R0/R1 由…」）**本任务不改**：历史运行记录的 `guard_denials` 里存着旧文案，改了会让这些记录的内容校验失效。文案过时的问题登记进 B50，随国际化一起清理。

## 白名单

- `engine/core/shell_structure.py`
- `engine/guards/command_guard.py`
- `engine/routing/run_check.py`（只在 `_TRUSTED_GUARD_REASONS` 里加一项）
- `tests/test_guard_review_signals.py`（新增）

消费方扫描（检查单第 2 项）：

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| `tests/test_command_guard_structure.py`、`tests/test_events_guard.py` | 无需 | 只新增规则和规则键，已有文案不变 |
| `engine/routing/run_check.py` 的可信理由清单 | **需配套，已列入白名单** | 见目标终态第 4 条 |
| 守卫事件（只记规则键） | 无需 | 新规则键是新增的，已有的键不变 |
| `engine/agents/review.py`、`dispatch.py` 发评论 | 无需 | 它们在设计方的进程里运行，不经过执行方的钩子 |
| 各宿主钩子模板 | 无需 | 角色由钩子参数传入，现有机制不变 |

CHANGELOG 由设计方在升级 PR 里统一写，本任务不改。

## 非目标

- 不处理绕过 CLI、自己写脚本直接调用 API 的情况：这是设计第 6 节写明的残余风险，根治靠 B31/B85。
- 不改其他规则的判定。
- 不改钩子模板与角色传递。
- 白名单以外的文件一律不改。
- 共用合同 C0 同样适用。

## 前置条件

- 工作区基于最新的 `origin/main`。核对上面引用的行号和函数，有出入就停下，向设计方报告。
- 和 T701、T703 的白名单不交叉，可以并行。白名单里有 `run_check.py`：T701 只调用 `run_check.scope`、不修改这个文件，所以不冲突。
- 实现前先确认本任务书已在 main 上：`bin/harness taskbook docs/plans/task-704-guard-review-signals.md --on-main` 通过。

## 验收

下表中的测试名都是本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B81 | 执行方角色下，`gh pr comment 1 -b x`、`gh issue comment 1 -b x`、`gh pr review 1 --comment -b x`、`bin/dispatch review 1`、`./bin/dispatch signoff 1` 都被拒绝，理由是 `REVIEW_SIGNAL`；用 `&&` 串联、包在 `bash -c` 里同样被拒绝 | 夹具 | `tests.test_guard_review_signals.GuardReviewSignalsTest.test_implementer_denied_structured` | 结构化路径放行 |
| 不挂规格：B81 | 无法结构化解析的命令（比如引号不闭合）走正则兜底，同样被拒绝 | 夹具 | `…test_implementer_denied_fallback` | 只加了一条路径 |
| 不挂规格：B81 | 设计方角色下以上命令全部放行；执行方的 `gh pr view`、`gh pr list`、`bin/dispatch status` 放行 | 夹具 | `…test_designer_and_read_only_allowed` | 误伤设计方或只读命令 |
| 不挂规格：B81 | 执行方调用 MCP 评论类工具被拒，事件里的规则键是 `tool_review_signal`；命令被拒时的规则键是 `review_signal` | 夹具 | `…test_tool_rules_and_event_keys` | 没有拦工具，或规则键缺失 |
| 不挂规格：B81 | 一份运行记录的 `guard_denials` 以 `REVIEW_SIGNAL` 为键、计数为 1 时，`run_check` 的记录内容校验通过 | 夹具 | `…test_run_record_accepts_review_signal_denial` | 守卫成功拦截之后，运行记录被判为「未知理由」而断链 |
| 不挂规格：B81 | 既有测试全部通过、零修改 | 夹具 | `bin/verify --full` | 已有规则被破坏 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 结构化规则、兜底正则、工具规则、规则键、运行记录的可信理由清单，以及回归断言 | `engine/core/shell_structure.py`、`engine/guards/command_guard.py`、`engine/routing/run_check.py`、`tests/test_guard_review_signals.py` | `python3 -W error::ResourceWarning -m unittest tests.test_guard_review_signals -v` | 验收第 1–5 行 |
| 2 | 全量验证，整理交付证据 | `tests/` | `bin/verify --full` | 验收第 6 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收与定向变异复核，以下四个变异必须各自被对应断言抓住：
- 去掉结构化路径中的 `pr comment` 判定；
- 去掉兜底正则；
- 去掉角色条件，变成对所有人拒绝；
- 不把 `REVIEW_SIGNAL` 加入可信理由清单。

同一失败连续三轮没有新证据时，停止这条路径。不得自行扩大白名单、修改任务书、实现非目标。
