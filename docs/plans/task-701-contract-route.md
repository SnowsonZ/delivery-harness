---
task: T701
class: K7
risk: R3
designer: claude-code
size: medium
architecture: true
spec_refs: []
no_spec_reason: 待办 B81 自治试验的合同制合并路由（设计 docs/plans/2026-10-03-autonomy-trial-design.md 第 4 节），无产品规格验收编号
budget:
  wall_clock_min: 120
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T701：合同制合并路由（policy 读评审与复核标记，R2 实现 PR 可自动合并）

负责方：**派发任务**。设计方：claude-code；独立评审方：OpenCode。依据：[自治试验设计](2026-10-03-autonomy-trial-design.md) 第 4 节、第 5 节第 6 步、第 6 节第 1 行。

开工前先读：
- `engine/routing/policy.py`：`decide` 的「风险」规则在第 114 行，只认 `level <= 1`；`CONTRACT_PATTERNS` 在第 37 行，路径写死在代码里，`machine_class` 第 101 行对任务书路径也是写死的；`gather` 在第 223 行，只读一次 `gh pr view --json labels`；`main` 写 `GITHUB_OUTPUT` 的位置在第 334 行。
- `engine/agents/review.py`：`render_comment` 在第 371–373 行，独立评审标记的格式是 `<!-- independent-review {"verdict","reviewer","model","head","findings","flagged","parsed"} -->`，其中 `head` 是完整 SHA。本任务只读这个格式，不修改它。

## 目标终态

1. **新增模块 `engine/routing/signals.py`**，只读不写，提供以下内容：
   - `REVIEW_MARK = "<!-- independent-review "`、`SIGNOFF_MARK = "<!-- designer-signoff "`。
   - **复核标记格式**（合同，T702 按这个格式写）：单行 JSON，字段为 `{"verdict": "通过"|"不通过", "head": "<40 位 SHA>", "designer": "<席位>", "mutations": <int>, "caught": <int>}`。
   - `markers(comments, mark, login) -> list[dict]`：输入是 `gh pr view --json comments` 返回的 `comments`，每条形如 `{"author": {"login": …}, "body": …}`。按出现顺序返回作者等于 `login` 的标记 JSON；解析失败的条目跳过；其他作者写的标记一律忽略。
   - **防混入**（拆分评审阻断项 1：评审评论正文里嵌着评审方模型的输出，模型输出中可能夹带伪造的标记）：一条评论只有同时满足下面三条，才算作标记：
     - 两类标记（`REVIEW_MARK`、`SIGNOFF_MARK`）合计**恰好出现一次**；
     - 该标记独占一行；
     - 位置正确：复核标记必须是最后一个非空行；评审标记之后只允许出现以 `<!-- harness-review-audit ` 开头的行。
     
     不满足的评论整条忽略，不取其中任何一个标记。
   - `same_content(reviewed, head, base, cwd) -> bool`：
     - `reviewed == head` 时为真；
     - 否则要求 `reviewed` 是 `head` 的祖先，并且两段 `git diff --binary --full-index --no-ext-diff --no-renames` 的输出**字节完全相同**：一段是 `<merge-base(base, reviewed)>..<reviewed>`，一段是 `<merge-base(base, head)>..<head>`。不做任何空白规范化：`patch-id` 会忽略空白，而 Python 的缩进变化能改变执行路径（拆分评审阻断项 2）；
     - 提交不存在或 git 出错时为假。
   - `review_status(comments, login, head, base, cwd) -> tuple[str, str]`：返回 `("ok"|"fail"|"missing", 理由)`。取最后一个内容与当前 head 相同的评审标记：verdict 为「通过」且 `flagged` 为假时是 `ok`，其余情况是 `fail`；没有这样的标记时是 `missing`。
   - `model_family(model: str | None) -> str | None`：取最后一个 `/` 之后的部分，再取第一个 `-` 之前的部分并转小写（`zai-coding-plan/glm-5.3` 得到 `glm`，`gpt-6.1-sol` 得到 `gpt`，`claude-opus-5-5` 得到 `claude`）；空值得到 None。
   - `signoff_status(comments, login, head, base, cwd) -> tuple[str, str]`：规则同上。`ok` 的条件是 verdict 为「通过」、`mutations >= 1` 且 `caught == mutations`。
2. **`policy.py` 的合同制路径**：
   - **候选**：同时满足以下全部条件：
     - 风险为 R2；
     - `run_check.scope(...)` 不是 None（即任务 PR），**并且 `scope.in_pr` 为假**：任务书随本 PR 新增或修改时，`run_check` 不要求运行记录，「合同已批」也不成立；
     - **本 PR 的全部改动路径都命中 `autonomy.toml [contract_route] allowed`**（新配置键，glob 列表）。这里用白名单而不是排除合同路径：黑名单总会漏掉某个路径，第三轮评审就发现漏了 `AGENTS.md` 和现装配置里的 `docs/specs/**`。只要有一个路径不在白名单内（任务书、规格、`AGENTS.md`、护栏、未列出的任何文件），就不是候选。**该键缺失或为空时，没有任何 PR 是候选**，安全缺省（拆分评审第二轮阻断项 1、第三轮阻断项 1 与严重项 2）；
     - 机器判定类别在 autonomy 里是 `L4`；
     - 有 PR 编号。
   - **读评论**：只对候选 PR 额外调用一次 `gh pr view <PR> --json comments`。非候选 PR 的 gh 调用序列逐字不变。读取失败按 `fail` 处理，理由写「读不到评论」。
   - **登录名**：取 `setting("identity", "agent_login")`，缺失时按 `fail` 处理，不抛异常。
   - **「风险」规则**：
     - R0/R1：判定与理由文字都不变。
     - R2 候选：只有评审和复核都是 `ok` 才通过。理由格式：`R2：合同制路径——独立评审 <状态>，设计方复核 <状态>`，未通过时附上原因。
     - R2 非候选、R3：判定与现在相同，理由文字不变。
   - **降级（同家评审，设计第 3.1 节）**：评审为 `ok` 时，比较评审模型的家族，和执行模型的家族。评审模型**不取** `independent-review` 标记里的 `model`：那是给人看的展示串，未配置模型时会写成「codex 默认模型」，还会带上推理档位。改取同一条评论中 `<!-- harness-review-audit {…} -->` 标记的 `model` 字段（C6 审计摘要，未知时为 null；拆分评审第五轮严重项 1）。缺少审计标记、该字段为 null，或 `model_basis` 为 `unknown` 时，评审模型都按 None 处理，与本 PR 新增运行记录（`docs/runs/<任务>/<序号>.json`，用 `git show <head>:<路径>` 读取）中全部 `gen_ai.request.model` 的家族。相同，或者任一方为 None，就判为降级。降级不改变「风险」规则的结果，但理由要追加「同家评审（降级）」；`main` 计算 `audit` 时，自动合并且降级就强制为 true（原来的 `audit_sampled` 条件保留，两者是「或」的关系）。这样一来，现有的 `merge-app` 在合并后就会开 audit 议题。
   - **规则列表**：仍然是「风险、类别、预算、规模、运行记录」五条，名字和顺序都不变。
   - **`pending` 输出**：只有当其余四条规则（类别、预算、规模、运行记录）**全部通过**，并且「风险」规则不通过的**唯一原因**是缺标记时，才输出 pending：评审 `missing` 时输出 `review`；评审 `ok` 但复核 `missing` 时输出 `signoff`。只要有任何一条 `fail`，或其他规则不通过，pending 就为空，照常请求所有者评审（拆分评审严重项 6：例外由人决定，不能被「等待中」遮住）。`--github` 模式下，在 `GITHUB_OUTPUT` 里追加一行 `pending=<值>`，原有各行不变。`render` 在 pending 不为空时追加一行「等待：独立评审 / 设计方复核」。
3. **合同模式改从配置读取**：删掉 `CONTRACT_PATTERNS` 常量，改成函数，返回 `rules.toml [risk]` 里 `taskbooks` 与 `contracts` 的并集。`machine_class` 中 R0 分支对任务书路径的判断也改用 `taskbooks`。
4. **模板**：
   - `templates/.harness/config/rules.toml`：`[risk] contracts` 加入 `"docs/specs/**"`，注释说明「contracts 与 taskbooks 同时决定 K0（合同）类别」。
   - `templates/.harness/config/autonomy.toml`：注释说明 R2 合同制路径（设计第 4 节的六个条件），并给出**注释掉的** `[contract_route]` 示例（`allowed = ["src/**", "tests/**", "docs/runs/**", "CHANGELOG.md"]`，注明按项目目录调整）。各类别的等级保持 L3 不变。
   - `templates/.github/workflows/auto-merge.yml`：`judge.outputs` 增加 `pending`；`request-review` 任务只在 `needs.judge.outputs.pending == ''` 时执行 `--add-reviewer "$OWNER"`，打标签的步骤不变。**只改这两处**，`merge-app` 任务留给 T702。
5. **回归测试**：放在新文件 `tests/test_contract_route.py`，用夹具 git 仓库加假 gh 桩，参考 `tests/test_events_route.py` 的 `fake_gh` 写法。既有测试一行不改。

## 白名单

- `engine/routing/signals.py`（新增）
- `engine/routing/policy.py`
- `templates/.harness/config/rules.toml`（只改 `[risk] contracts` 一行及其注释）
- `templates/.harness/config/autonomy.toml`（只改注释，包括注释掉的 `[contract_route]` 示例）
- `templates/.github/workflows/auto-merge.yml`（只改 `judge.outputs` 和 `request-review` 两处）
- `tests/test_contract_route.py`（新增）

消费方扫描（检查单第 2 项）：

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| `tests/test_events_route.py` 的 `ROUTE_STEPS`、规则名列表 | 无需 | 规则名和数量都不变 |
| `tests/test_events_equivalence.py` 的假 gh | 无需 | 夹具里 K1 是 L4（第 112–114 行），但用到它的都是 R0 文档案例，而候选必须是 R2 任务 PR，所以不会多出评论调用（实现前逐个核对用例；遇到 R2 加 L4 加任务 PR 的组合就停下报告） |
| `engine/reports/audit_completeness.py`、`ledger.py` | 本任务无需 | route 事件形状不变；同步 main 之后评审 head 与合并 head 不再相等，这个问题由 T705 处理 |
| `tests/test_alert_cli.py`、`tests/test_ci_events_workflows.py` 对 `auto-merge.yml` 的断言 | 无需 | 没有改 `alert` 和 `judge` 的 steps；新增 output 不影响已有断言（实现前逐个核对，有冲突就停下报告） |
| Agent-Notification 消费方契约 | 无需 | 那边的模式全是 L3，路径不变；`docs/specs/**` 从 R2 移进 contracts 后风险等级不变，只有类别标签从 K5 变成 K0（由设计方写 Migration 条目） |

CHANGELOG 由设计方在升级 PR 里统一写（设计第 9 节 D1），本任务不改。

## 非目标

- 不实现 `signoff` 命令，也不实现重跑触发（T702）。
- 不改评审标记的写入方 `review.py`。
- 不改 `autonomy.toml` 中各类别的默认等级。
- 不改本仓库 `.github/`、`.harness/`。
- 不放宽 R0/R1/R3 的任何判定。
- 不引入依赖。
- 白名单以外的文件一律不改。
- 共用合同 C0 同样适用。

## 前置条件

- 工作区基于最新的 `origin/main`。逐项核对「开工前先读」里的行号和接口；有出入就停下，向设计方报告。
- 和 T704 的白名单不交叉，可以并行。T702、T703、T705 依赖本任务的 `signals` 模块，必须排在本任务合并之后。
- 实现前先确认本任务书已在 main 上：`bin/harness taskbook docs/plans/task-701-contract-route.md --on-main` 通过。

## 验收

下表中的测试名都是本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B81 | R2 任务 PR、类别 L4、agent_login 写的评审标记（通过且未标记严重）和复核标记（mutations=caught=2）都指向当前 head 时，`auto_merge=true`，「风险」理由含「合同制路径」 | 夹具 | `tests.test_contract_route.ContractRouteTest.test_r2_contract_route_merges` | 「风险」规则仍只认 R0/R1，`auto_merge=false` |
| 不挂规格：B81 | 缺复核时为 false，并输出 `pending=signoff`；缺评审时输出 `pending=review`；缺标记的同时还超出规模或预算，或评审 `fail` 时，pending 为空 | 夹具 | `…test_pending_signals` | 没有 pending 行；或者例外被「等待中」遮住 |
| 不挂规格：B81 | 其他账号写的评审或复核标记被忽略，判为 missing | 夹具 | `…test_foreign_author_ignored` | 伪造的标记让 `auto_merge=true` |
| 不挂规格：B81 | agent_login 写的评审评论在评审正文里夹带复核标记（或第二个评审标记）时，整条评论被忽略，复核仍是 missing；复核标记不在最后一行时同样忽略 | 夹具 | `…test_embedded_marker_rejected` | 模型输出夹带的标记被当成复核 |
| 不挂规格：B81 | 评审「不通过」或 `flagged=true`，或复核 caught<mutations、mutations=0，都判 false | 夹具 | `…test_failed_signals_block` | 任一组合放行 |
| 不挂规格：B81 | head 只多了合并 main 的提交（diff 字节相同）时，旧标记仍有效；多了内容提交时失效；只改了缩进的提交也必须失效 | 夹具 | `…test_same_content_after_main_sync` | 同步后不能合并，或改了内容、改了缩进仍然放行 |
| 不挂规格：B81 | 审计标记 `model` 为 `zai-coding-plan/glm-5.3`、执行模型为 `glm-5.3` 时，合并结果不变，但 `audit=true`，理由含「同家评审（降级）」；评审模型 `gpt-6.1-sol` 时，`audit` 按原来的抽样；任一模型缺失时按降级处理；`independent-review` 的 model 写着「codex 默认模型 max」、但审计标记里为 null 或 `model_basis=unknown` 时，同样降级 | 夹具 | `…test_same_family_review_degrades_to_audit` | 同家评审悄悄合并，合并后无人补审 |
| 不挂规格：B81 | 白名单外的路径不是候选：`allowed` 为 `engine/**`、`tests/**`、`docs/runs/**`、`CHANGELOG.md` 时，同一 PR 另外改了任务书、`AGENTS.md` 或 `docs/specs/x.md` 中任意一个，即使带齐两个标记、类别为 K5 L4，`auto_merge` 仍为 false、pending 为空；`[contract_route]` 缺失时，纯 `engine/` 的 PR 也不是候选 | 夹具 | `…test_paths_outside_allowlist_not_candidate` | 合同或仓库指令的改动搭车自动合并；或缺省配置下放行 |
| 不挂规格：B81 | R3 PR 带齐两个标记仍为 false；非候选 PR 不调用评论接口（桩遇到 comments 调用即抛错），规则名列表仍是五条 | 夹具 | `…test_non_candidates_unchanged` | R3 放行，或多出 gh 调用 |
| 不挂规格：B81 | 合同模式来自配置：`contracts` 含 `docs/backlog.md` 时，只改待办的 PR 判 K0；从配置中移除后不再是 K0 | 夹具 | `…test_contract_patterns_from_config` | 仍按写死的 `docs/plans/backlog.md` 判 |
| 不挂规格：B81 | 模板 `request-review` 只在 pending 为空时请求所有者评审；`judge.outputs` 含 pending | 夹具 | `…test_workflow_skips_owner_request_while_pending` | 模板没改，断言失败 |
| 不挂规格：B81 | 既有测试全部通过、零修改（特别是 `test_events_route`、`test_events_equivalence`、`test_audit_completeness`、`test_alert_cli`） | 夹具 | `bin/verify --full` | 形状变化导致既有测试失败 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | signals 模块与合同制判定、pending 输出、合同模式改从配置读取 | `engine/routing/signals.py`、`engine/routing/policy.py`、`tests/test_contract_route.py` | `python3 -W error::ResourceWarning -m unittest tests.test_contract_route -v` | 验收第 1–10 行 |
| 2 | 模板配置注释与工作流两处 | `templates/.harness/config/rules.toml`、`templates/.harness/config/autonomy.toml`、`templates/.github/workflows/auto-merge.yml` | 同上，外加 `tests.test_alert_cli`、`tests.test_ci_events_workflows` | 验收第 11 行 |
| 3 | 全量验证，整理交付证据 | `tests/` | `bin/verify --full` | 验收第 12 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收与定向变异复核，以下八个变异必须各自被对应断言抓住：
- 去掉作者过滤；
- 去掉防混入的「恰好出现一次」检查；
- `same_content` 改用 patch-id，或恒真；
- 复核不检查 caught；
- 候选判断去掉 L4 条件；
- 候选判断去掉 `in_pr` 或白名单检查；
- 白名单缺失时按「全部允许」处理；
- 降级时不强制 audit。

同一失败连续三轮没有新证据时，停止这条路径。不得自行扩大白名单、修改任务书、实现非目标。
