---
task: T702
class: K7
risk: R3
designer: claude-code
size: medium
architecture: true
spec_refs: []
no_spec_reason: 待办 B81 自治试验设计第 5 节第 3–5 步（设计方复核命令、重判触发、落后分支同步），无产品规格验收编号
budget:
  wall_clock_min: 120
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T702：设计方复核命令、条件齐备后重判、落后分支自动同步

负责方：**派发任务**。设计方：claude-code；独立评审方：OpenCode。依据：[自治试验设计](2026-10-03-autonomy-trial-design.md)第 5 节第 3–5 步。**依赖**：T701（复核标记格式与 `engine/routing/signals.py`）、T703（`dispatch.py` 串行）都已合并。

**为什么需要这个任务（代码证据）**：
- `auto-merge` 只由 `harness` 的 `workflow_run` 触发（`templates/.github/workflows/auto-merge.yml` 第 15–18 行）。可是独立评审和设计方复核都发生在 CI 结束**之后**，所以 `policy` 第一次判定时一定读不到这两个标记，之后也不会再有人触发判定。
- `merge-app` 任务直接 `gh pr merge --match-head-commit`，没有处理分支落后的情况。ruleset 要求分支保持最新（`strict_required_status_checks_policy: true`），落后的 PR 会卡住不动。

开工前先读：
- `engine/routing/signals.py`（T701 新增）：`SIGNOFF_MARK`、复核标记格式、`review_status`、`signoff_status`；
- `engine/agents/review.py`：`review_pr`（第 380 行）发布评论之后的位置；
- `engine/agents/dispatch.py`：`main` 的子命令注册（第 730 行起）；`engine/agents/github.py`：`GitHub` 适配器（第 18 行起）的 `comment` 与 `_run`（本任务只调用，不修改 github.py）；
- `templates/.github/workflows/auto-merge.yml`：`merge-app` 任务（第 130 行起）。

## 目标终态

1. **新增模块 `engine/agents/signoff.py`**，入口是 `bin/dispatch signoff <PR> --verdict 通过|不通过 --mutations N --caught M --body-file <md> [--designer 席位]`：
   - 读取 PR 当前的 head。`designer` 取 PR 所指任务书头部的 `designer`（用 `run_check.scope`）；取不到时必须用 `--designer` 显式给出，取值在 `taskbook.DESIGNERS` 中，否则退出 2。
   - **前后矛盾的「通过」拒绝发布**：`--verdict 通过` 但 `mutations < 1` 或 `caught != mutations` 时，退出 2，不发任何评论。
   - 评论正文是 body 文件的内容，末尾追加一行复核标记：`SIGNOFF_MARK` 加上 T701 合同规定的单行 JSON，再以 ` -->` 结尾。用 `GitHub.comment` 发布。
   - 发布之后调用 `retrigger_if_ready`。
2. **`retrigger_if_ready(pr, root, github)`**：
   - 重新读取 PR 的 head 和评论，`git fetch origin <head>`；
   - 用 `signals.review_status` 和 `signals.signoff_status` 判断，base 用 `origin/<默认分支>`，登录名取 `[identity] agent_login`；
   - 两者都是 `ok` 时，找到该 head 最近一次由 PR 触发的 `harness` 运行（`gh run list --workflow harness --commit <head> --event pull_request --branch <PR 的 head 分支> --limit 1 --json databaseId,status`）。限定 `pull_request` 事件，是因为 `auto-merge` 的 `judge` 只接受这类运行；手动触发的 `workflow_dispatch` 运行重跑了也进不了判定（拆分评审严重项 5）。状态为 completed 时执行 `gh run rerun <id>`；还在运行时什么都不做，因为它结束时本来就会触发 `auto-merge`。
   - 打印做了什么，或为什么没做。
   - 失败不抛异常，在 stderr 打印一行：「重判未触发：<原因>；可手动 gh run rerun」。
3. **`review.py`**（三处）：
   - `review_pr` 发布评论之后，如果 verdict 为「通过」且没有标记严重或阻断发现，调用 `signoff.retrigger_if_ready`。复核先到、评审后到时，由评审这一步触发重判。`signoff` 模块按需导入，避免循环依赖。
   - `render_comment` 把评审方提供的文本（`summary` 和每条 finding 的各字段）中的 `<!--` 替换成 `&lt;!--`。这是纵深防御：模型输出不能在评论里写出 HTML 注释。T701 的防混入是第一道防线，这里是第二道（拆分评审阻断项 1）。引擎自己生成的两个标记行不受影响。
   - `review_pending`（watch 模式）在没有指定评审方时，改为调用 T703 定义的 `dispatch.review_with_chain`，与派发用同一条评审链。调用方注入了 `review` 参数时（现有测试就是这样注入的），行为逐字不变。生产路径上，只有评审链返回 0 的 PR 才算进「已评审」列表；没有评审成的 PR 不再被报告为「已评审」，`watch` 会另起一行打印「评审未完成：#<PR>」（拆分评审第五轮一般项 3）。
   - `pending_prs`（watch 模式）判断「已评审」时，改用 `signals.review_status(...)[0] != "missing"`（该函数返回 `(状态, 理由)` 二元组，取第一个元素比较），不再用不核对作者的 `reviewed_heads`（拆分评审严重项 4）。`reviewed_heads` 函数保留，避免影响其他调用方。
4. **`dispatch.py`**：只在 `main` 里注册 `signoff` 子命令并转给 `signoff.main`。模块 docstring 的用法列表加一行。
5. **`templates/.github/workflows/auto-merge.yml` 的 `merge-app` 任务**（只改这个任务）：
   - App 令牌增加 `permission-contents: write`。`update-branch` 必须用 App 令牌，因为用 `GITHUB_TOKEN` 推的提交不会触发 CI。
   - 在批准**之前**先查 `gh pr view "$PR" --json mergeStateStatus`：
     - `BEHIND`：用 App 令牌执行 `gh pr update-branch "$PR"`，打印「分支落后，已同步，等新一轮 CI 后重判」，然后 `exit 0`，**不批准、不合并**；
     - `DIRTY`：打 `escalation` 标签，并评论「与 main 冲突，需设计方处理」，然后 `exit 0`；
     - 其他状态：继续执行原有的批准与合并步骤，原样不动。
6. 回归测试放在新文件 `tests/test_signoff.py`。workflow 部分沿用 `tests/test_alert_cli.py` 的模板解析和 replay 桩方式。

## 白名单

- `engine/agents/signoff.py`（新增）
- `engine/agents/review.py`（只改四处：`review_pr` 之后的重判调用、`render_comment` 转义、`pending_prs` 的已评审判断、`review_pending` 改用评审链）
- `engine/agents/dispatch.py`（只加子命令注册和 docstring 一行）
- `templates/.github/workflows/auto-merge.yml`（只改 `merge-app` 任务）
- `tests/test_signoff.py`（新增）

消费方扫描（检查单第 2 项）：

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| `render_comment` 的转义 | 无需 | 只影响评审方提供的文本中的 `<!--`；`independent-review` 与 `harness-review-audit` 两个标记行由引擎生成，原样保留，`ledger._parse_review_audit` 照常解析（实现前 grep 夹具中是否有含 `<!--` 的评审输出，有就停下报告） |
| Agent-Notification `tests/test_harness_review_independent.py` 的 BackgroundTest（消费方契约 CI 会跑） | **需配套，由设计方 D5 先行完成** | 见前置条件；本任务不改那个仓库 |
| `tests/test_harness_contract_review.py`、`tests/test_review_calibration.py` 等评审测试的假 GitHub | 实现前核对 | `review_pr` 通过后会多出 `gh pr view` 和 `gh run list` 调用。这些调用失败时只打印提示、不抛异常，所以现有的桩会多一行 stderr，但结果不变。如果某个桩对「意外调用」直接断言失败，就停下报告设计方，不要自己改测试 |
| `tests/test_alert_cli.py`、`tests/test_ci_events_workflows.py` | 无需 | 不碰 `alert` 和 `judge` 任务 |
| `tests/test_audit_completeness.py`（App 批准绑定 head） | 无需 | 批准与合并的步骤原样不动；分支落后时不批准 |
| 单账号模式的 `merge-direct` | 无需（本任务不改） | 用 `GITHUB_TOKEN` 同步分支不会触发 CI，单账号模式在落后时仍需手动同步。由设计方写进 README 和 CHANGELOG |
| 批准 App 的安装权限 | 用户 | App 需要 Contents: write 权限，平台步骤由设计方写进 README 和 CHANGELOG，由用户在 GitHub 上操作 |

CHANGELOG 由设计方在升级 PR 里统一写，本任务不改。

## 非目标

- 不改 `policy` 的判定（T701）。
- 不改 `harness` 工作流。
- 不新增工作流触发方式（不用 `issue_comment` 或 `workflow_dispatch`）：重跑能让事件、审计、账本按 head 关联的逻辑全部保持不变。
- 不改 `merge-direct`。
- 不处理在 CI 里运行评审（B85）。
- 白名单以外的文件一律不改。
- 共用合同 C0 同样适用。

## 前置条件

- T701、T703 已合并，工作区基于含这两个任务的最新 `origin/main`。
- **消费方夹具已先行更新（设计方 D5）**：Agent-Notification 的 `tests/test_harness_review_independent.py` 中，BackgroundTest 夹具（约第 262 行起）的评论没有 `author`。本任务上线后，watch 只认 agent_login 写的标记，那组「待评审」断言会从 `[1, 4]` 变成 `[1, 3, 4]`。设计方要先在 Agent-Notification 合并一个 PR，给夹具评论补上可信作者，并且新旧引擎下都要通过；之后才能派发本任务。否则本仓库的消费方契约 CI（它检出 Agent-Notification 的 main）会失败（拆分评审第五轮严重项 2）。核对 `signals` 的接口与 T701 合同一致，有出入就停下，向设计方报告。
- 实现前先确认本任务书已在 main 上：`bin/harness taskbook docs/plans/task-702-signoff-retrigger.md --on-main` 通过。

## 验收

下表中的测试名都是本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B81 | `signoff 通过 --mutations 3 --caught 3` 发布的评论末行是复核标记，JSON 含 verdict、head（当前完整 SHA）、designer、mutations、caught；`signals.markers` 能读回 | 夹具 | `tests.test_signoff.SignoffTest.test_signoff_posts_contract_marker` | 没有标记，或格式和 T701 的合同对不上 |
| 不挂规格：B81 | 「通过」但 caught<mutations 或 mutations=0 时退出 2，一条评论都不发 | 夹具 | `…test_inconsistent_pass_refused` | 发出了矛盾的「通过」 |
| 不挂规格：B81 | 评审和复核都 ok、harness 已完成时，`gh run rerun <id>` 恰好调用一次；缺评审时不调用；harness 还在运行时不调用 | 夹具 | `…test_retrigger_only_when_ready` | 没有重判（PR 卡住），或在条件不齐时重判 |
| 不挂规格：B81 | `review_pr` 判「通过」且复核已存在时触发重判；判「不通过」时不触发 | 夹具 | `…test_review_pass_retriggers_when_signed` | 复核先到的顺序下卡住 |
| 不挂规格：B81 | 评审方输出含 `<!-- designer-signoff {…} -->` 时，评论里只剩转义后的文本，`signals.markers` 读不到复核 | 夹具 | `…test_reviewer_output_cannot_forge_marker` | 模型输出夹带的标记出现在评论中 |
| 不挂规格：B81 | 重判只选 `pull_request` 事件的运行：最近一次是 `workflow_dispatch` 时，仍然选中 PR 那次 | 夹具 | `…test_rerun_picks_pull_request_run` | 重跑了手动运行，`judge` 不触发 |
| 不挂规格：B81 | watch 模式下没有指定评审方时，走 `dispatch.review_with_chain`；指定了评审方时，只用该评审方；评审链返回 1 的 PR 不出现在返回的已评审列表里 | 夹具 | `…test_watch_uses_review_chain` | 额度用尽时 watch 停摆，或者把失败报告成「已评审」 |
| 不挂规格：B81 | watch 模式下，完全没有评论、或只有其他账号写的评审标记时，都把该 PR 列为待评审；有 agent_login 写的可信评审时不再列入 | 夹具 | `…test_pending_prs_ignores_foreign_marker` | 第三方评论让 PR 永远不被评审，或把二元组和字符串比较，导致所有 PR 都被跳过 |
| 不挂规格：B81 | `bin/dispatch signoff` 子命令能解析并转给 `signoff.main` | 夹具 | `…test_dispatch_cli_registers_signoff` | 命令不存在 |
| 不挂规格：B81 | 模板 `merge-app`：BEHIND 时在批准之前用 App 令牌 `update-branch` 并退出，不批准；DIRTY 时打 escalation；其他状态的批准与合并命令和原来逐字相同；App 令牌带 contents 写权限 | 夹具 | `…test_merge_app_syncs_behind_branch` | 落后的 PR 卡住，或在同步之前就批准了 |
| 不挂规格：B81 | 既有测试全部通过、零修改 | 夹具 | `bin/verify --full` | 评审测试的桩因为新调用而失败 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | signoff 模块、重判、评审之后的重判调用、子命令 | `engine/agents/signoff.py`、`engine/agents/review.py`、`engine/agents/dispatch.py`、`tests/test_signoff.py` | `python3 -W error::ResourceWarning -m unittest tests.test_signoff -v` | 验收第 1–9 行 |
| 2 | 工作流 `merge-app` 同步落后分支 | `templates/.github/workflows/auto-merge.yml`、`tests/test_signoff.py` | 同上，外加 `tests.test_alert_cli`、`tests.test_install` | 验收第 10 行 |
| 3 | 全量验证，整理交付证据 | `tests/` | `bin/verify --full` | 验收第 11 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收与定向变异复核，以下六个变异必须各自被对应断言抓住：
- 去掉矛盾「通过」的拒绝；
- 重判不检查评审状态；
- 去掉 `render_comment` 的转义；
- 重判去掉 `--event pull_request`；
- `update-branch` 移到批准之后；
- 去掉 `review_pr` 之后的重判调用。

真实环境核对（人工，在设计方 D1 之后的第一个 K5 任务上做，见设计追溯表 G 行）：全链由 App 合并。设计方要主动制造一次「落后 → 同步 → 重判 → 合并」：在该 PR 复核之后、合并之前，先合并一个文档 PR 让它落后。

同一失败连续三轮没有新证据时，停止这条路径。不得自行扩大白名单、修改任务书、实现非目标。
