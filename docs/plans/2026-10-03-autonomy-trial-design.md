# 自治试验：合同制合并路由（设计）

状态：用户 2026-10-03 已定第 0 节的五项决定，待提交。来源：用户与设计方（Claude Code）2026-10-03 对话。待办：B81。

## 0. 决策摘要

**用户 2026-10-03 已定**：

| # | 问题 | 决定 | 代价 | 撤回方式 |
|---|---|---|---|---|
| 1 | `docs/backlog.md` 是否自动合并 | 归 R0，自动合并；合并后抽审兜底 | Agent 若悄悄改了优先级，要到抽审或周报才看得到 | 改 `rules.toml` 一行 |
| 2 | 本批 5 份任务书是否一次提交 | 一次提交，作为《任务拆分流程》「先跑通第一个任务」的一次例外。派发链路已经跑通 50 多个任务，这条规则要防的风险已不存在 | 某份任务书写错时，需要修订，多花一轮 | 随时改成分批 |
| 3 | 试验的预算参数 | 按第 7 节执行 | 太松，逃逸要很晚才停；太紧，刚开始就停 | 改 `autonomy.toml` |
| 4 | 护栏性质的引擎代码和模板（`engine/guards/`、`engine/routing/`、`templates/.github/`、`templates/.harness/`）是否也自动合并 | 自动合并，在升级时把关；升级摘要单独列出这几类改动 | 要到升级时你才看到这些改动，一次要看的量更大 | 从 `[contract_route] allowed` 里去掉这些路径 |
| 5 | 角色分配与额度降级 | 见第 3.1 节。Codex 统一用 `gpt-6.1-sol`，推理强度 high；两家额度都用尽时，由 Zcode `glm-5.3` 接替 | 两家都用尽时评审为同家，独立性弱；这类 PR 一律开 audit 议题，事后补审 | 改 `rules.toml [review]` |

**已经核对的**：
- 5 份任务书都通过了 `bin/harness taskbook`；
- `bin/verify` 全量通过；
- Codex 做了多轮只读拆分评审，每一轮的发现都已逐条处理，记录见第 11 节。

## 1. 背景与证据

最近 103 个已合并 PR，你共批准 106 次：0 次要求修改，PR 上也没有你写的评论。你确认这些批准偏形式，真正的决定是看设计方的汇报后在对话里做的。合并后才发现的缺陷，都是别的环节发现的：

- B76：漏了幂等回归测试，合并后的变异复核才发现；
- B80：账本写入失败没有任何告警，G1 验收时才发现；
- B70、B72、B79：真实派发运行时才暴露。

这说明真正在抓缺陷的是三层：CI、独立评审、设计方的逐行验收加定向变异复核。逐个审批实现 PR，没有额外拦下过问题。

审批量的构成：引擎实现与测试 48 个、任务书与设计 41 个（其中约 17 个只是补白名单授权的修订）、升级与护栏配置 14 个。

## 2. 已定决定（用户 2026-10-03）

- 先只在本仓库试验，Agent-Notification 等有了数据再说。
- 人只审合同（做什么、怎么验收）和信任边界（护栏配置、引擎升级、放权）；实现 PR 满足条件就自动合并。
- 敢于放开，用数据回收：逃逸超出预算就自动停掉该类，必要时整体停机。
- Codex 统一使用 `gpt-6.1-sol`。Codex 和 Claude 的额度都用尽时，由 Zcode（`glm-5.3`）接替工作，不停工（见第 3.1 节）。

## 3. 谁做什么

| 环节 | 试验期 | 依据 |
|---|---|---|
| 任务书、设计、执行计划（合同） | **用户审** | 合同由人定，是整个方案的前提；`docs/plans/task-*.md` 判 K0，设计类文档列入 contracts |
| 实现、提交 | 执行方（Pi） | 不变 |
| 独立评审 | 自动：CI 通过后由派发进程直接接上（T703） | 修复 B77 第③项的断链 |
| 设计方复核（逐行验收与定向变异） | 设计方 Agent，用 `bin/dispatch signoff` 留下机器可读的结论（T702） | 目前最有效的一层，从可选变成合并前提 |
| 实现 PR 合并 | 自动：满足第 4 节条件，由批准 App 批准并合并 | — |
| 待办登记、只补测试、说明文档 | 自动（R0，K1/K2） | — |
| 升级 PR（`.harness/engine`） | **用户审**，附带「本次包含的 PR 与逃逸」摘要 | 本仓库的引擎改动要经升级才对自己生效，这是兜底关 |
| `.harness/`、`.github/`、`bin/`、钩子、`autonomy.toml` | **用户审** | R3 不变 |
| 例外（评审否决、超预算、越界修订、冲突） | **用户决定** | 带 escalation 标签 |

### 3.1 角色分配与额度降级（用户 2026-10-03 定）

模型来自三家：智谱 GLM（Pi、OpenCode、Zcode，额度充足）、OpenAI（Codex）、Anthropic（Claude Code）。Pi 的 `openai-codex` 与 Codex 共用同一个 ChatGPT 订阅，不能当作 Codex 的备选，也不能用来执行。

| 角色 | 正常 | Codex 用尽 | Claude 用尽 | 两家都用尽 |
|---|---|---|---|---|
| 执行方 | Pi + `glm-5.3` | 同左 | 同左 | 同左 |
| 独立评审（自动，按 `[review] chain` 依次尝试） | Codex `gpt-6.1-sol` | Claude Code；但当该任务书的设计方就是 Claude 时，分离规则不允许它评审，会落到 OpenCode `glm-5.3`，按降级处理 | Codex | OpenCode `glm-5.3`（与 Zcode 同一个模型，见下） |
| 设计方：新设计、任务书 | Claude Code | Claude Code | Codex | Zcode `glm-5.3` |
| 设计方：日常运转（派发、验收、变异复核、signoff、记账） | Claude Code | Claude Code | Zcode `glm-5.3` | Zcode `glm-5.3` |
| 拆分评审：与设计方不同家 | Codex | Claude Code（设计方不能同时是 Claude） | Codex（设计方不能同时是 Codex） | Zcode / OpenCode `glm-5.3` |

**两家都用尽时的同家评审**：执行和评审都是 GLM，独立性弱。按你的决定不停工，但这类 PR 记为「降级」：
- 合并照常自动进行；
- 每个降级 PR 合并后**一律开 audit 议题**（复用现有的抽审机制），额度恢复后由 Codex 或 Claude 补审，发现问题按逃逸登记；
- 升级摘要单独列出降级合并的 PR。

任务书和设计无论由谁写，都要你审（K0 合同），所以由 GLM 设计的质量最终由你把关。

**自动评审为什么用 OpenCode 代 Zcode**：Zcode 有无界面的 `-p` 模式，但不能在命令行指定模型。OpenCode 已配置 `glm-5.3`，是同一个模型，而且 `review.py` 已经支持。若要 Zcode 本身做评审，需要新写一个评审方适配器（并入 B47）。

## 4. 合同制路径：R2 实现 PR 自动合并的条件

现在的 `policy` 只允许 R0/R1 自动合并。改动后，R2 的 PR **同时**满足下面所有条件也可以自动合并（T701）：

1. **合同已批**：提交带 `Task: T<编号>`，对应的任务书已在 main 上（不随本 PR 修改），声明的类别和机器判定一致（现有规则）。本 PR 的全部改动路径都在 `autonomy.toml [contract_route] allowed` 白名单内，只要有一个路径在外面就转人审。白名单只列实现路径，本仓库为 `engine/**`、`templates/**`、`tests/**`、`docs/runs/**`、`CHANGELOG.md`；任务书、规格、`AGENTS.md`，以及**本仓库现役的护栏**（`.harness/`、`.github/`、`bin/`、`.githooks/`、各 Agent 配置目录）一律不在其中。`engine/**` 和 `templates/**` 中也有护栏性质的代码和模板（`engine/guards/`、`engine/routing/`、`templates/.github/`、`templates/.harness/`），把它们放进白名单，依据是第 3 节的兜底关：它们要经过你审的升级 PR 才对本仓库生效，消费方同理。代价是这些改动的把关从逐个 PR 推迟到升级时（见第 0 节决策 4）。升级摘要要把这几类路径的改动单独列出。该键缺失时，没有任何 PR 走合同制路径。
2. **运行记录合格**：现有 `run_check` 的全部判定都通过。
3. **类别放权**：机器判定的类别在 `autonomy.toml` 里是 L4，且没有超出误差预算（现有规则）。
4. **独立评审通过**：PR 上有 `<!-- independent-review … -->` 标记，满足：作者是 `checks.toml [identity] agent_login`；verdict 为「通过」；`flagged` 为 false（没有阻断或严重级发现）；评审的 head 与当前 head 内容相同（见下）。如果评审模型与执行模型同家（取模型名的家族前缀比较，如 `glm`、`gpt`、`claude`；任一方的模型未知也按同家处理），这次评审照样计入，但该 PR 记为降级：强制开 audit 议题（第 3.1 节）。
5. **设计方复核通过**：PR 上有 `<!-- designer-signoff … -->` 标记，满足：作者同上；verdict 为「通过」；定向变异数不少于 1 且全部被抓住；head 内容相同。
6. **规模**：非测试行数不超过 `[size] max_lines`。配置里把 `tests/**` 加进 exclude；测试的有效性由变异复核保证。

R3 始终不走这条路径。R0/R1 的判定不变。

**「head 内容相同」**：评审或复核时的 head 记为 R，当前 head 记为 H。如果 R 是 H 的祖先，且两者相对各自与 main 合并基点的 diff（`--binary --full-index`）字节完全相同，就视为同一份内容。这里不用 patch-id，因为它会忽略空白，而 Python 的缩进能改变行为。这样分支只是同步了 main（ruleset 要求分支保持最新），评审结论仍然有效，不用重评。内容有任何变化都必须重新评审、重新复核。账本和审计采用同一判定，所以评审证据不会因为 head 变了而丢失（T705）。

`policy` 的五条规则名不变（风险、类别、预算、规模、运行记录）。合同制的判定写在「风险」这条规则的理由里，所以事件、审计、账本读到的规则形状都不变。

## 5. 触发与时序

1. 派发 → 本地判定 → 开 PR → CI。
2. CI 通过后，派发进程直接对该 PR 跑独立评审（T703，配置项 `[dispatch] review_after_ci`）。
3. 设计方做逐行验收和定向变异，然后运行 `bin/dispatch signoff <PR>`，发出带标记的复核评论（T702）。
4. 评审通过、复核也已存在时，最后到场的那一步会重跑该 head 由 `pull_request` 触发的那次 `harness` 运行（`gh run rerun`）。重跑结束后，`auto-merge` 照旧由 `workflow_run` 触发，`policy` 读到两个标记，交给批准 App 合并（T702）。之所以用重跑而不新增触发方式，是为了让 trace、审计、账本按 head 关联事件的现有逻辑全部不变。
5. 合并前分支落后于 main 时，用 App 令牌执行 `update-branch`。新的提交会触发 CI，之后回到第 4 步；因为内容相同，评审结论继续有效（T702）。如果有冲突，打 escalation 标签，交设计方处理。
6. 其余规则全部通过、只差评审或复核时，`policy` 输出 `pending=review|signoff`。这时工作流只打标签，**不请求你评审**，免得每个 PR 都通知你。只要有任何其他失败（超预算、超规模、评审否决等），就照常请求你评审（T701）。

## 6. 可信性与残余风险

| 风险 | 应对 | 残余 |
|---|---|---|
| 公开仓库里任何人都能发一条带标记的评论 | 只认 `agent_login` 账号写的标记（T701）；派发和 watch 判断「已评审」时也只认这个账号（T703、T702） | — |
| 评审评论正文嵌着评审方模型的输出，模型可能被 diff 里的内容诱导，输出伪造的标记 | 一条评论里两类标记合计恰好一次、独占一行、位置固定，否则整条忽略（T701）；评审方文本里的 `<!--` 一律转义（T702） | — |
| 执行方和设计方用的是同一个 Agent 账号，执行方可能伪造评审或复核结论 | 守卫禁止执行方发 PR 或议题评论、提交评审、运行 `signoff` 和 `review`（T704）；合并后抽审，每 3 个抽 1 个 | 执行方仍能绕开 CLI，自己写脚本直接调用 API，守卫只能挡住常规路径。根治要靠凭据隔离（B31）或在 CI 里跑评审（B85）。试验期接受这一风险，抽审和升级关兜底 |
| 独立评审对「测试是否真能失败」不敏感（B59） | 设计方的变异复核是合并前提 | 设计方漏掉的变异无法自动发现，要等 B4 的按行变异测试 |
| 自动合并的问题在本仓库会一直攒到升级 | 升级 PR 由你审，并附摘要：包含哪些 PR、各自的评审和复核结论、期间登记的逃逸 | 摘要目前由设计方手写；决策包的标准格式见 B83 |
| 你的账号令牌在 Agent 环境里，设计方能用它代你批准 | 不在本批处理 | 见 B84：试验期间留给你审的 PR 由你自己点批准，并把令牌移出 Agent 环境 |

## 7. 试验参数与回收条件

`autonomy.toml`（D1，用户批）：

| 类别 | 等级 | 窗口 / 逃逸上限 | 抽审 |
|---|---|---|---|
| K1 说明与记录 | L4 | 20 / 1 | — |
| K2 补测试 | L4 | 20 / 1 | — |
| K3 行为不变重构 | L4 | 10 / 0（不变） | 每 3 个抽 1 个（不变） |
| K4 缺陷修复 | L4 | 20 / 1 | 每 3 个抽 1 个 |
| K5 功能与规格实现 | L4 | 20 / 1 | 每 3 个抽 1 个 |
| K0、K6、K7 | L3 | 不变 | — |
| K8 | L2 | 不变 | — |

- **周期**：自动合并满 20 个 PR，或满两周，先到为准。结束时出一份对比报告：逃逸率、各类人审覆盖率、你的审批次数，和试验前的基线对比。
- **立即回收**：满足下列任一条，相应类别改回 L3：
  - 升级 PR 审查时发现了已自动合并的缺陷；
  - 出现严重级逃逸；
  - 某一类逃逸超出预算。超预算的那一类 `policy` 会自动停掉。
- **整体停机**：Actions → auto-merge → Disable workflow。
- **逃逸登记**：每个逃逸议题的正文写明「引入的 PR」和「发现方式（CI / 独立评审 / 设计方复核 / 升级审 / 运行时）」。试验前的历史逃逸（B76、B80 等）由设计方补登记（D3）。这些历史 PR 当时是 `class:K7`，不计入试验期 K4/K5 的窗口，只作为基线。

## 8. 配置变更（设计方 D1，用户批；和升级合在一个 PR 里）

- `rules.toml [risk]`：
  - `engine/**`、`templates/**` 从 r3 移到 r2；
  - `docs/backlog.md` 归 r0（决策 1）；`contracts` 删掉写错的 `docs/plans/backlog.md`，加入 `docs/plans/20*.md`（设计、计划、追溯表）；
  - `CHANGELOG.md` 归 r0。
- `rules.toml [taskbook] guard_paths`：去掉 `engine/**`、`templates/**`。以后引擎任务按 K4/K5 声明，任务书不再强制判为 K7。
- `rules.toml [dispatch]`：`review_after_ci = true`。
- `rules.toml [review]`：`reviewer = "codex"`；新增 `chain = ["codex", "claude-code", "opencode"]`；`codex_model = "gpt-6.1-sol"`、`codex_reasoning_effort = "high"`（本 PR 已改）。
- `autonomy.toml`：按第 7 节修改；`[size] exclude` 加 `tests/**`；新增 `[contract_route] allowed = ["engine/**", "templates/**", "tests/**", "docs/runs/**", "CHANGELOG.md"]`。
- `rules.toml [risk] contracts`：同时加入 `docs/specs/**`（与模板一致；即使漏加，有白名单在，规格改动也不会自动合并）。
- 平台（用户）：创建批准 App（需要 Pull requests 和 Contents 两项写权限），并配置仓库变量、密钥和 environment（README「Platform setup」）。本仓库目前三者都是空的。

上面这些都要在 T701–T705 合并、并完成一次自举升级之后才生效，所以和升级放在同一个 PR 里，你只需要批一次。

## 9. 任务拆分与追溯表

| 需求 | 承担 | 验收断言 | 文件 | 依赖 |
|---|---|---|---|---|
| 第 4 节条件 1–6；只认 agent_login；防标记混入；内容相同判定（字节级）；`pending` 只在其余规则全部通过时输出；合同模式改从配置读取 | T701 | `tests.test_contract_route` | `engine/routing/policy.py`、`engine/routing/signals.py`（新）、`templates/.harness/config/rules.toml`、`templates/.harness/config/autonomy.toml`、`templates/.github/workflows/auto-merge.yml`、`tests/test_contract_route.py`（新） | — |
| 第 5 节第 2 步：CI 通过后自动接上评审（「已评审」只认可信标记）；第 3.1 节评审方按 `[review] chain` 依次尝试 | T703 | `tests.test_review_after_ci` | `engine/agents/dispatch.py`、`tests/test_review_after_ci.py`（新） | T701 |
| 第 6 节：执行方禁止评论、评审、复核；运行记录接受新的拒绝理由 | T704 | `tests.test_guard_review_signals` | `engine/guards/command_guard.py`、`engine/core/shell_structure.py`、`engine/routing/run_check.py`（只改可信理由清单）、`tests/test_guard_review_signals.py`（新） | — |
| 第 5 节第 3–5 步：signoff 命令、重跑触发（只选 pull_request 那次运行）、落后时更新分支；评审文本转义；watch 只认可信标记 | T702 | `tests.test_signoff` | `engine/agents/signoff.py`（新）、`engine/agents/review.py`、`engine/agents/dispatch.py`（只加子命令）、`templates/.github/workflows/auto-merge.yml`（merge-app 任务）、`tests/test_signoff.py`（新） | T701（标记格式）、T703（dispatch.py 串行） |
| D1 之前的修复与收尾：评审互斥（逃逸 #128）；评审关闭 Codex 子代理（用户 2026-10-04 决定，实测输入减少约 75%）；B89 收尾；merge-app 的落后判定改为 compare 的 behind_by 加 mergeable | T708 | `tests.test_review_lock`、`tests.test_split_modules`、`tests.test_signoff` | `engine/agents/{review_lock,review,review_calibration,plan_review,dispatch_slots,dispatch_text}.py`、`templates/.github/workflows/auto-merge.yml`、对应测试 | T702 |
| 为 T703、T702 腾出体量余量：行为不变地拆分 `dispatch.py`、`review.py`（T703 首次派发因质量棘轮 800 行上限停下） | T707 | `tests.test_split_modules` | `engine/agents/{dispatch,dispatch_text,dispatch_slots,review,review_calibration}.py`、`tests/test_split_modules.py`（新） | — |
| 第 4 节「内容相同」在账本与审计中同样成立；账本检出完整历史 | T705 | `tests.test_ledger_equivalent_head` | `engine/reports/ledger.py`、`engine/reports/audit.py`、`templates/.github/workflows/harness.yml`（只改 audit-ledger 的 checkout）、`tests/test_ledger_equivalent_head.py`（新） | T701 |
| 自举升级、本仓库 `.github/` 同步、第 8 节配置；升级摘要中单独列出护栏性质路径的改动；整批 CHANGELOG（Unreleased + Migration：`docs/specs/**` 进 contracts、`review_after_ci`、App 需 Contents 写权限）与 README「Platform setup」 | 设计方 D1 | 升级 PR 中逐项核对：① `bin/harness risk` 对 `engine/x.py`、`docs/backlog.md`、`docs/plans/2026-x.md`、`CHANGELOG.md` 的判级分别是 R2、R0、R2、R0；② `bin/harness taskbook` 对一份声明 K5、步骤含 `engine/**` 的样例任务书判定合格；③ autonomy 各类别的等级、窗口、抽审参数与第 7 节逐行一致（PR 描述里贴对照表）；④ `review_after_ci = true`；⑤ `[contract_route] allowed` 与第 8 节逐字一致，且 `bin/harness policy` 在夹具 PR（改 `engine/x.py` 加 `AGENTS.md`）上的候选判断为否；⑥ 现装 `rules.toml [risk] contracts` 含 `docs/specs/**`，且只改 `docs/specs/x.md` 的夹具 PR 机器判类为 K0；⑦ `bin/verify --full` 与 integrity 通过 | `.harness/**`、`.github/workflows/{auto-merge,harness}.yml`、`CHANGELOG.md`、`README*.md` | T701–T705 合并 |
| 批准 App、变量、密钥、environment | 用户（设计方准备操作步骤，并在完成后核对） | 名为 `harness-auto-merge` 的 environment 存在，且只允许默认分支部署；仓库变量 `HARNESS_APP_CLIENT_ID` 存在；该 environment 下有密钥 `HARNESS_APP_PRIVATE_KEY`；App 安装在本仓库，具有 Pull requests 与 Contents 两项写权限（安装页截图或 `gh api` 输出贴进 D1） | 平台 | D1 合并前 |
| Agent-Notification 的 BackgroundTest 夹具给评论补上可信作者（消费方契约 CI 会跑这组测试），新旧引擎下都通过 | 设计方 D5（在 Agent-Notification 的 worktree 里开 PR） | 该仓库 `tests/test_harness_review_independent.py` 在当前引擎和含 T702 的引擎下都通过 | Agent-Notification 的测试 | T702 派发前 |
| 历史逃逸补登记 | 设计方 D3 | escape 议题 | 议题 | 试验开始前 |
| 试验报告 | 设计方 D4 | `docs/review/` 报告，必须包含第 7 节的全部指标：各类别的自动合并数、逃逸数、发现方式、人审覆盖率、你的审批次数，以及与基线的对比；缺任何一项都不算完成 | 文档 | 试验结束 |
| 端到端：真实任务走完自动合并全链 | 设计方 G（D1 后的第一个 K5 任务） | 同一个 PR、同一个 head 上依次有：CI 通过 → 可信评审标记 → 复核标记 → `harness` 重跑 → route 事件 `auto_merge=true` → App 批准且绑定 head → 合并；`bin/harness trace <PR>` 的输出贴进该 PR。此外，设计方必须主动制造一次「落后 → 同步」：在该 PR 复核之后、合并之前，先合并一个文档 PR 让它落后。然后核对同步后的合并仍由 App 完成，且账本和审计里评审证据仍在（T705） | — | D1、平台 |

派发分三波：
- **第一波**：T701 ∥ T704。
- **第二波**：T703 ∥ T705 ∥ T706，T703、T705 依赖 T701 的 `signals` 模块，T706 依赖 T704；另加 T707（拆分），T703 要等 T707 合并后变基续跑。
- **第三波**：T702，依赖 T701、T703、T706、T707。

同一文件的改动全部串行：
- `auto-merge.yml`：T701 只改 `judge` 输出和 `request-review`，T702 只改 `merge-app`；
- `dispatch.py`：先 T703，后 T702；
- `review.py`：只有 T702 改动。

## 10. 非目标与后续

- 决策包的标准格式与可视化（B83）：试验期间人审节点先由设计方按第 0 节的格式手写，标准化之后再说。
- 在 CI 里跑评审，作为可信通道（B85），以及凭据隔离（B31）。
- 判级配置路径失效时报警（B82）：比如某条规则在仓库里一个文件都匹配不到。
- 审计完整性补上 R2 自动合并（B86）：现在 `audit_completeness` 只对 R0/R1 的自动合并核对 App 批准；合同制路径上线后，R2 的自动合并也应核对 App 批准、评审标记和复核标记。
- Agent-Notification 和其他消费方：模板默认仍然全部人审；合同制路径需要显式放权才会启用。

## 11. 拆分评审与处理（Codex，只读，2026-10-03，第一轮结论：需修订）

| # | 严重度 | 发现 | 处理 |
|---|---|---|---|
| 1 | 阻断 | 评审评论嵌着评审方模型的输出，夹带的复核标记能通过作者过滤 | 采纳：T701 加防混入（标记合计恰好一次、独占一行、位置固定）；T702 对评审方文本中的 `<!--` 转义；两边都加伪造回归 |
| 2 | 阻断 | `patch-id --stable` 忽略空白，只改缩进的提交会被当成内容相同 | 采纳：改为字节级 diff 比对；加「只改缩进必须失效」的断言 |
| 3 | 阻断 | 同步 main 之后，账本和审计按合并 head 严格比对，评审证据会丢失 | 采纳：新增 T705，账本和审计采用同一内容判定，账本工作流检出完整历史 |
| 4 | 严重 | `reviewed_heads` 不核对作者，第三方评论能让派发跳过评审 | 采纳：T703 和 T702 的 watch 都改用 `signals.review_status`；T703 改为依赖 T701 |
| 5 | 严重 | 重跑可能选中 `workflow_dispatch` 的运行，`judge` 不接受这类运行 | 采纳：T702 限定 `--event pull_request` 和 PR 分支 |
| 6 | 严重 | `pending` 可能遮住真正需要人处理的例外 | 采纳：T701 只在其余四条规则全部通过、唯一缺口是标记时才输出 pending |
| 7 | 严重 | D1、平台、D4、G 的验收断言不足以证明真的启用了 | 采纳：第 9 节对这四行逐项写明了可核对的断言 |
| 8 | 一般 | 行号、适配器位置、等价夹具的说明有事实错误 | 采纳：T703 改为第 585–587 行；T702 指向 `github.py`；T701 改正 K1 为 L4 的事实，并重写理由 |

### 第二轮（结论：需修订，6 条；第一轮 8 条中 6 条确认闭环，第 3、4 条的剩余缺口即本轮第 3、2 条）

| # | 严重度 | 发现 | 处理 |
|---|---|---|---|
| 1 | 阻断 | 同一 PR 既改任务书又改引擎时仍被判 K5，而任务书随 PR 修改时 `run_check` 不要求运行记录，合同审批和运行记录两道要求都会被绕过 | 采纳：T701 的候选条件要求 `scope.in_pr` 为假，并且没有任何合同路径被改动；新增混合改动回归；设计第 4 节条件 1 同步 |
| 2 | 严重 | T702 拿 `review_status` 返回的二元组和字符串比较，导致所有 PR 都被跳过 | 采纳：T702、T703 都改为取 `[0]` 比较；补「没有评论仍算待评审」的断言 |
| 3 | 严重 | T705 的闭包要经过 `_review_entries` 传递，但白名单没有授权改这个函数 | 采纳：把 `_review_entries` 转传 `same` 写进白名单与目标终态；新增对应变异 |
| 4 | 严重 | T704 新增的拒绝理由不在 `run_check` 的可信理由清单里，守卫成功拦截之后运行记录反而不合格 | 采纳：`run_check.py` 加入 T704 白名单，只改清单；新增对应回归。同时撤回 T704 原定的合并、批准文案修改，因为历史运行记录里存着旧文案，改了会让这些记录失效，文案问题改由 B50 处理 |
| 5 | 一般 | 真实同步链的验收口径不一致 | 采纳：统一为设计方主动制造一次「落后 → 同步」并核对 |
| 6 | 一般 | 一次提交五份任务书，偏离「先跑通第一个任务」的流程，需要你明确决定 | 保留为第 0 节的决策 2，由你决定 |

### 第三轮（结论：需修订，2 条；第二轮第 1–5 条确认闭环，第 6 条仍待你决定）

| # | 严重度 | 发现 | 处理 |
|---|---|---|---|
| 1 | 阻断 | `AGENTS.md` 不在合同模式里，所以 PR 同时改它和引擎时，可以自动合并 | 采纳：候选条件从「排除合同路径」改成「全部路径都在 `[contract_route] allowed` 白名单内」，缺省时没有候选；新增回归 |
| 2 | 严重 | 本仓库现装的 `contracts` 没有 `docs/specs/**`，D1 也没有核对 | 采纳：白名单本身就会挡住规格改动；D1 另外把 `docs/specs/**` 加进 contracts，并在验收里核对 |

### 第四轮（定点复核，结论：需修订，2 条；第三轮 AGENTS.md 问题确认闭环；白名单对 T403、T402、T125 的实际改动路径没有误伤；新配置键与 autonomy 的读取方没有冲突）

| # | 严重度 | 发现 | 处理 |
|---|---|---|---|
| 1 | 阻断 | `templates/**` 里有护栏模板（工作流、ruleset、配置、git 守卫），放进白名单与「护栏人审」的说法冲突 | 部分采纳：第 4 节写明为什么放进白名单（经升级才生效，在升级时把关），并把放不放列为第 0 节决策 4，由你决定；升级摘要单独列出这几类路径的改动 |
| 2 | 严重 | D1 没有核对现装 contracts 里的 `docs/specs/**` | 采纳：D1 验收增加第 ⑥ 项 |

### 第五轮（`gpt-6.1-sol`，定点复核角色与降级条款；结论：需修订，3 条）

| # | 严重度 | 发现 | 处理 |
|---|---|---|---|
| 1 | 严重 | `independent-review` 标记里的 model 是展示串（如「codex 默认模型 max」），按它判断家族会漏掉降级 | 采纳：T701 改为读取同一条评论里 C6 审计标记的 `model`，值为 null 或来源未知时按降级处理；补对应断言 |
| 2 | 严重 | Agent-Notification 的 BackgroundTest 夹具里的评论没有作者，T702 上线后消费方契约 CI 会失败 | 采纳：新增设计方任务 D5，先在 Agent-Notification 更新夹具，再派发 T702；写入 T702 的前置条件 |
| 3 | 一般 | `review_pr` 会返回 2，或者直接抛异常，这两种情况都没有告警；`review_pending` 忽略返回码，会把失败报告成「已评审」 | 采纳：T703 明确返回 0、1、2 和抛异常各自怎么处理；T702 改为生产路径上只有返回 0 才算已评审 |

另外，核对角色表时发现：Codex 额度用尽时，如果设计方本身就是 Claude，分离规则不允许它评审自己设计的任务，评审会落到 GLM，按降级处理。已补进第 3.1 节。

