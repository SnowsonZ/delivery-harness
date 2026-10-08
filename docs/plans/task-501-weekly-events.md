---
task: T501
class: K7
risk: R3
designer: claude-code
size: medium
architecture: true
spec_refs: []
no_spec_reason: 已审定的可观测性设计与本次任务书是B46合同，本仓库没有对应产品规格验收编号
budget:
  wall_clock_min: 600
  ci_rounds: 3
  retries: 2
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T501：周报事件汇总与原指标对账

> 「对账」在本任务里**只有一个对账点**：覆盖说明里的本周合并 PR 数 N 必须等于旧文本「人工干预率」里的合并数。守卫拒绝的两个计数口径不同，不做任何对账或比较（见指标表）。

负责方：**派发任务**。设计方：claude-code；独立评审方：Codex（单会话）。派发、提交、推送、开PR由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)（C7）、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

**修订记录**：2026-10-08 设计方按用户决定改数据口径。原稿以「本机事件库」为主，但周报由各仓库的 `quality.yml` 在 GitHub runner 上用 `weekly --publish` 发布（数据取自当前仓库：本周合并的 PR、议题、`docs/runs/` 运行记录），而设计规定事件库只在本机、不上传，runner 上读不到。改为：**以仓库里可复取的持久来源（已合并 PR 的账本、运行记录）为主，本机库只作补充且单列**；CI 里不实算审计发现数。

## 目标终态

保留 `collect`/`compute`/`render` 现有 GitHub 与运行记录来源以及所有已有指标和小节，**只在现有周报文本之后追加**一个「事件汇总（B46）」小节。旧文本是新文本的逐字前缀；发布流程（`publish`、议题、评论、`weekly-data` 注释的解析）不变。

### 数据来源与口径

**A. 主来源（CI 与本机都用，仓库内可复取）**

1. **已合并 PR 的账本**：本周合并的 PR（`mergedAt` 落在 UTC 周窗口，起点含、终点不含，与旧小节同一集合）各自的 `harness-audit` 分支上的 `<合并年份>/<PR号>.json`，用 `git show origin/harness-audit:<路径>` 读取（周报工作流 `fetch-depth: 0`，该分支在 runner 上可读，不需要改工作流）。无账本（早于账本功能、读取失败、分支不存在）的 PR 计入「无账本」，不影响其余统计。
2. **本周运行记录**：现有 `load_records`，窗口按 `ended_at`（与旧小节同口径）。

**B. 补充来源（仅在非 CI 环境运行 `weekly` 且事件库可用时）**：本机事件库，**单列**为「本机补充」子小节，数字**不并入** A。**按运行环境启停，不按库是否存在判断**：`CI=true`（GitHub Actions）时 B 一律关闭，子小节写「不可用（CI 环境不统计本机库）」——设计评审 1 模拟 Actions 环境实测，`quality.yml` 在周报前运行的 `mutate`、`replay`、`quality` 会往 runner 上写临时事件库（得到 `ci:1:1:quality` 事件），所以 runner 上"没有库"不成立，只看库是否存在会把这些临时事件当成本机历史。非 CI 环境下，库不存在、读不到、`PRAGMA user_version` 大于 `events_db.SCHEMA_VERSION`（较新版本）时写「不可用」及原因；**不能只看 `events_io.query` 是否返回空**（无库与较新版本库都返回 `[]`），新模块必须先自己预检（只读打开，读 `user_version`），合法的空库才写「0 个事件」。B 的窗口：`[周起点, 周终点)`（事件 `ts`，`events_io.query` 只有起点参数，终点在 Python 里过滤）；来源范围：只统计 `source == "local"` 的事件，导入的 `ci:`、`github:` 来源不统计。

### 指标

| 指标 | A（账本 + 运行记录） | B（本机补充） |
|---|---|---|
| 阶段耗时 | 账本 `stages` 中 `evidence_kind == "event"` 且 `duration_ms` 非空的事件，按 `stage` 汇总：事件数、合计毫秒、最大毫秒。**`run_record_summary` 条目不计**（与运行记录的 `executor_seconds` 是同一数据，会重复） | 本机事件按 `stage` 汇总（含 `verify`、`dispatch`、`review` 等账本里没有的本机阶段） |
| 执行方耗时与轮次 | 运行记录：`executor_seconds` 合计、尝试数、`exit` 分布 | — |
| 守卫拒绝 | **执行方「拒绝规则命中数」**：运行记录 `guard_denials`（`{规则文本: 次数}`，按**拒绝理由**计数，一次被拒的工具调用可命中多条理由）按规则文本合计与总数。**设计方一侧写「不可得（只记在本机）」**，不写 0 | 本机 `guard` 阶段 `deny` 事件，**按守卫种类（`step`：`command`、`git`）×角色（`actor.role`：`designer`、`implementer`、`engine`）**分列并按规则键汇总；`command_guard` 写 `designer`/`implementer`，`git_guard` 不传 actor、默认角色是 `engine`（设计评审 1 实测真实库：command/designer 16、command/implementer 105、git/engine 35），`engine` 一组标注「不可归属」，不并入执行方也不并入设计方。**另列「被拒工具调用数」**（`executor_round` 事件的 `guard_denied`，一次调用计一次）。**两个数的单位与纳入条件都不同，不做任何相等或大小核对，也不做任务级关联**：运行记录的规则命中数来自 `PiHost.parse`（只处理 `isError=True` 且有列表理由的调用），`guard_denied` 来自 `parse_observability`（出现拒绝标记即计），真实数据里存在「被拒标记但理由数为 0」的调用，T114 是调用 1、命中 0，T706 是调用 2、命中 1，固定周窗口内有 7 个分支「调用数大于命中数」（设计评审 2 实测，用流产物重放两个解析器结果一致）；`executor_round` 事件还没有 `attempt` 字段，事件按 `ts`、记录按 `ended_at` 入窗，同一分支的轮次事件与尝试记录条数也对不上（T121 三条事件两份记录、T403 三条事件一份记录）。所以只**分别列出**两个数并在小节里注明口径，不比较 |
| 升级原因 | 运行记录 `escalation` 与 `exit` 的分布；旧小节已有的 `escalation` 议题数只引用、不重复计 | 本机 `escalate` 事件的 `reason` 分布 |
| 缺上下文分类 | 运行记录 `missing_context` 按 `category` 计数；`missing_context_status` 取 `reported`/`unknown`/`invalid` 之外的值、或历史记录**缺字段**的，计入「覆盖不足」而不是 0 | — |
| 审计发现数 | **CI 与无库时写「不可用（CI 不实算审计；见本机补充）」**，不写 0 | 本机 `ci/audit.summary` 与 `audit.finding` 事件：同一 PR/head 只取**最新一次**的结果（旧结果有发现、最新同 head 结果无发现时按无发现算，不残留旧 finding），按固定规则键去重计数；更新 head 的分别标明 |

### 覆盖说明（每次都写）

「本周合并 PR N 个，有账本 M 个，无账本 K 个（原因分类计数）；运行记录读取 R 份尝试记录，涉及 T 个任务」（**份数与任务数分开写**：设计评审 1 实测运行记录 93 份、59 个任务，一个任务可有多次尝试）。并写明范围：账本只覆盖**已合并**的 PR；运行记录覆盖**当前 checkout 中已落盘**、`ended_at` 在窗口内的尝试（CI 的 checkout 是默认分支；本机运行则是当前所在分支，可能含未合并分支的记录），含同任务中途失败的尝试，**不与本周合并 PR 取交集**；A 部分**不含**设计方一侧的守卫拒绝与本机阶段耗时。N 必须等于旧小节里的本周合并 PR 数——旧文本没有独立小节，该数出现在「人工干预率」的「N 个合并」里，直接复用 `week.prs`（窗口 `[end−7天, end)`，按 `mergedAt` 过滤，沿用旧查询的 100 条上限）。本机补充另写覆盖区间（库里最早/最晚事件时间）与来源（`source` 数）。

### 不可得的表示

任何读不到、算不出的值写「不可用」加原因，**不补造 0**。有数据但为零才写 0。

## 白名单

- `engine/reports/weekly_events.py`（新增，纯函数：读账本、汇总、渲染事件小节；约 250 行以内，不导入 `weekly`，避免循环）
- `engine/reports/weekly.py`（只改 `build`：在旧 `render` 结果之后追加事件小节，并导入新模块；当前 413 行，净增不超过 25 行，最终必须 ≤ 800）
- `tests/test_weekly_events.py`（新增；不存在才可开始）
- `CHANGELOG.md`

## 非目标

不改旧小节的文本、数据来源、`collect`/`compute`/`render` 的现有行为与返回形状，不改 `quality.yml` 与模板工作流，不改账本与运行记录的生成，不在 CI 里实算审计，不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T403 已合并（P4 已完成）且工作区基于最新 main；T101 为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_weekly_events.py` 尚不存在；仅新增本任务测试。测试使用匿名临时 git 仓库、隔离 ROOT、假 gh 与冻结时钟，不碰真实库/PR/工作流；账本夹具**至少有一份由真实 `ledger.build_ledger` + `publish_ledger` 写出**（可复用 `tests/gh_fakes.py` 的 `FakeGhBase`），证明读取方与写入方形状一致，其余可手写 JSON 以便精确算术。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以「按实际代码」为由变更合同。

## 消费方扫描（命令与输出，设计方 2026-10-08 执行；本仓库 cdacde9）

```
$ grep -rn "weekly" .github templates bin README.md | grep -v "^docs"
.github/workflows/quality.yml:51  python .harness/engine/cli.py weekly --publish    templates/.github/workflows/quality.yml（同款，各消费仓库各跑各的）
$ grep -n "fetch-depth" .github/workflows/quality.yml → fetch-depth: 0（全部分支，origin/harness-audit 可读）
$ git ls-tree -r --name-only origin/harness-audit | wc -l → 80（2026/<PR>.json）
$ ls tests | grep -i weekly → 无专门命名的 weekly 测试文件（tests/test_events_verify.py 提及 weekly；消费方仓库有 ../session-manager/tests/test_harness_weekly.py，实际调用 weekly.build、history_from_comments、publish，覆盖指标、突增、历史往返与固定议题发布）
$ 账本形状（2026/201.json）：顶层键 anchors approval chains class head_sha merge_sha merged_at missing pr references repository risk schema_version sources stages trace_id；stages 条目键 source stage step status duration_ms trace_id evidence_kind outputs…；evidence_kind 有 event 与 run_record_summary 等；其中 dispatch/executor_round 是 run_record_summary（outputs 为空，没有 guard_denied），guard 阶段事件为 0 条
$ 运行记录（docs/runs/<任务>/<n>.json）键：attempt branch ci_rounds_before class cost ended_at escalation executor_seconds exit failure_signatures guard_denials（{规则文本: 次数}）guard_ref host_version missing_context missing_context_status prompt_sha256 retries started_at task trace_id …
```

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| `quality.yml` 与模板 | 无需 | 已 `fetch-depth: 0`，`weekly --publish` 入口不变 |
| `weekly.publish`、`history_from_comments` | 无需，必须仍工作 | 新小节追加在旧文本之后；`weekly-data` 注释仍在旧文本里，解析不受影响 |
| 现有 `weekly` 的其他调用（`bin/harness weekly`） | 无需 | 本机运行同入口，多出「本机补充」子小节 |
| 消费方 `session-manager/tests/test_harness_weekly.py` | 无需改，必须仍通过 | 调用 `weekly.build`、`history_from_comments`、`publish`；由设计方在 Agent-Notification 独立 worktree 升级后跑它（追溯表 G2），不是执行方自己写「通过」 |
| `tests/test_events_verify.py` | 实现前核对，必须仍通过 | 提及 weekly；本任务不改它 |

## 设计方的绕过与失效清单

| 可能的问题 | 处理 |
|---|---|
| 把 `run_record_summary` 的耗时和运行记录的 `executor_seconds` 都加，数据翻倍 | 阶段耗时只统计 `evidence_kind == "event"`；验收有同时含两者的世界，断言不重复 |
| 本机补充的数字并入 A，造成同一事实 CI 与本机各算一次、本机运行时翻倍 | 本机补充单列、不并入；验收：同一 PR 同时在账本与本机库里，A 的数字不因本机库存在而改变 |
| 读不到就写 0（无 `harness-audit` 分支、账本缺失、坏 JSON、本机库损坏） | 写「不可用」加原因；验收逐种覆盖，断言输出里没有把它们表示为 0 |
| 周窗口边界错位，与旧小节的本周合并 PR 数对不上 | 同一集合（`week.prs`）、起点含终点不含；验收 N 与旧小节数相等，边界提交各有一例 |
| 旧小节被改（空白、顺序、注释） | 验收断言新输出以 `render(...)` 的旧输出为**逐字前缀**，在有/无账本、无库、坏库各情形下都成立 |
| 把设计方拒绝在 CI 里写成 0 | 明确写「不可得」；验收断言文字，不允许数字 0 |
| `guard_denials` 的键含规则文本，可能夹带路径或私密内容 | 只输出规则文本本身（运行记录已脱敏），不输出路径；验收断言输出不含临时目录与本机用户目录片段 |
| 新模块反向导入 `weekly`（循环） | 新模块只放纯函数；验收用 AST 检查导入（含 `from engine.reports import weekly` 与相对导入两种写法） |
| 账本读取依赖本机 `origin/harness-audit` 引用而在测试里不存在 | 读取函数接受注入的「读文件」回调（缺省为 `git show`）；测试用匿名临时 git 仓库造该分支，不依赖外部引用 |

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T501 | **账本阶段耗时**：两份手写账本加一份真实 `build_ledger` 写出的账本，按 `stage` 汇总事件数、合计、最大毫秒，算术确定；`run_record_summary` 不重复计入；窗口边界（起点含、终点不含）各一例；无账本的 PR 计入「无账本」并给原因分类 | 夹具 | `tests.test_weekly_events.ObservabilityTaskTest.test_ledger_stage_durations_and_window` | 把摘要条目也加进耗时，或窗口错位时失败 |
| 不挂规格：B46 T501 | **运行记录指标**（逐项算术）：`executor_seconds` 合计、尝试数、`exit` 分布各有具体数值断言；执行方守卫拒绝按规则文本合计（标注「规则命中数」）、设计方一侧写「不可得」而不是 0；升级原因（`escalation`/`exit`）与缺上下文分类按类别计数，`missing_context_status` 异常值与缺字段的历史记录计入「覆盖不足」；窗口按 `ended_at`；覆盖说明里份数与任务数分开且数值正确 | 夹具 | `::test_run_record_denials_escalation_and_context_totals` | 把不可得写成 0，或分类计数错时失败 |
| 不挂规格：B46 T501 | **本机补充**（逐项算术）：非 CI 环境且库可用时单列子小节；**本机阶段耗时**按 `stage` 的事件数、合计、最大毫秒各有数值断言（含 `verify`、`dispatch`、`review` 这些账本里没有的本机阶段），跨周事件不计入；**升级原因**按 `escalate` 事件的 `reason` 分布各有数值断言；守卫拒绝按「种类×角色」分列（command/designer、command/implementer、git/engine 各一例，`engine` 标「不可归属」，三者不相加）；被拒工具调用数与规则命中数**分别列出、不做任何比较或关联**（夹具含「调用 1、命中 2」「调用 2、命中 0」两种，断言输出里没有「不一致」「违反」之类的判定文字）；同一事件重复导入不双算；**A 的数字不因本机库存在而变化** | 夹具 | `::test_local_supplement_is_separate_and_units_are_not_conflated` | 并入 A、翻倍、对两个守卫计数做比较判定，或「种类×角色」分组、本机耗时、升级原因算错时失败 |
| 不挂规格：B46 T501 | **按环境关闭 B**：`CI=true` 时即使本机库存在且含本机事件与审计事件（模拟 `quality.yml` 先写入临时事件），B 子小节仍写「不可用（CI 环境不统计本机库）」、不统计任何事件，A 不变；非 CI 环境：无库、`user_version` 较新的库写「不可用」并带原因，合法空库写「0 个事件」（两者表述不同）；只统计 `source == "local"` | 夹具 | `::test_local_supplement_is_disabled_by_environment_not_by_database_presence` | 在 CI 里把临时事件当本机历史，或把较新版本库、无库与空库混为一谈 |
| 不挂规格：B46 T501 | **审计发现数**：CI 写「不可用」；本机补充里同一 PR/head 重复审计不累加，**旧结果有发现、最新同 head 结果无发现时不残留旧 finding**，更新 head 分别标明，规则键计数准确 | 夹具 | `::test_audit_findings_unavailable_in_ci_and_deduped_locally` | 把不可得写成 0，或每次 audit 运行都累加时失败 |
| 不挂规格：B46 T501 | **旧小节不变与失败隔离**：有账本、无账本、无 `harness-audit` 分支、坏 JSON、本机库缺失/损坏各情形下，新输出都以旧 `render(...)` 输出为逐字前缀；覆盖说明里 N 等于旧文本「人工干预率」里的合并数；所有「不可用」带原因，输出里没有把不可得表示为 0；**追加后 `history_from_comments` 仍能解析出同样的历史、`publish`（假 gh）仍创建固定议题并评论完整文本**（两项各一个断言） | 夹具 | `::test_existing_sections_are_a_verbatim_prefix_and_unavailable_is_never_zero` | 改旧文本或把不可得表示为 0 时失败 |
| 不挂规格：B46 T501 | **结构与体量**：`weekly_events.py` 不导入 `weekly`（AST，两种写法）；`weekly.py` 净增 ≤ 25 行、最终 ≤ 800；输出不含临时目录与本机用户目录片段 | 夹具 | `::test_module_boundaries_and_privacy` | 循环导入、超标或泄露路径时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，先写测试，再实现读账本与账本阶段耗时、运行记录指标 | `engine/reports/weekly_events.py`、`tests/test_weekly_events.py` | `python3 -W error::ResourceWarning -m unittest tests.test_weekly_events.ObservabilityTaskTest.test_ledger_stage_durations_and_window tests.test_weekly_events.ObservabilityTaskTest.test_run_record_denials_escalation_and_context_totals -v` | 验收第 1–2 行 |
| 2 | 本机补充与审计发现数、`build` 追加与前缀不变 | `engine/reports/weekly_events.py`、`engine/reports/weekly.py`、`tests/test_weekly_events.py` | `python3 -W error::ResourceWarning -m unittest tests.test_weekly_events.ObservabilityTaskTest.test_ledger_stage_durations_and_window tests.test_weekly_events.ObservabilityTaskTest.test_run_record_denials_escalation_and_context_totals tests.test_weekly_events.ObservabilityTaskTest.test_local_supplement_is_separate_and_units_are_not_conflated tests.test_weekly_events.ObservabilityTaskTest.test_local_supplement_is_disabled_by_environment_not_by_database_presence tests.test_weekly_events.ObservabilityTaskTest.test_audit_findings_unavailable_in_ci_and_deduped_locally tests.test_weekly_events.ObservabilityTaskTest.test_existing_sections_are_a_verbatim_prefix_and_unavailable_is_never_zero tests.test_weekly_events.ObservabilityTaskTest.test_module_boundaries_and_privacy -v`（unittest 要用完整点分名称，`::` 写法不可执行） | 验收第 3–7 行 |
| 3 | CHANGELOG；全量验证，整理交付证据 | `CHANGELOG.md` | `bin/verify --full` | C0 与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核（变异前先确认未变异基线在副本里是绿的），并做 Agent-Notification 独立 worktree 等价验证（追溯表 G2）。必须被抓住的变异：
- 阶段耗时把 `run_record_summary` 也计入；窗口起点或终点边界取反；无账本的 PR 不计数；
- 设计方拒绝写成 0；本机补充并入 A；同一事件重复导入双算；
- 审计发现数在 CI 写成 0；同 PR/head 重复审计累加；
- 旧文本被改（任何空白）；坏账本或缺分支时写 0 而不是「不可用」；
- `weekly_events` 反向导入 `weekly`（两种写法）；`weekly.py` 净增超 25 行；
- 对被拒调用数与规则命中数做相等或大小比较并输出判定（须由「调用 1、命中 2」「调用 2、命中 0」两种合法夹具抓住）；把 `git`/`engine` 守卫事件并入执行方或设计方；
- 运行记录指标：`executor_seconds` 合计漏加或只加一部分、尝试数按任务去重、`exit` 分布全归同一类；本机阶段耗时恒为零或漏加、跨周事件被计入；`escalate.reason` 分布全归同一类；
- B 改按「库是否存在」而不是「是否 CI」启用（CI 中有临时事件时须失败）；较新版本库、无库、合法空库混为一谈；统计了 `source` 不是 `local` 的事件；
- 审计旧结果残留（旧有发现、最新无发现时仍计旧 finding）；缺字段的历史记录被计为 0 而不是「覆盖不足」；覆盖说明里把份数写成任务数。

**合并并随自举升级生效之后**：设计方在真实仓库的周报工作流 `workflow_dispatch` 触发一次（或本机 `bin/harness weekly` 不带 `--publish`），核对新小节：覆盖说明里 N 等于旧小节的本周合并 PR 数，阶段耗时与运行记录指标与手工从若干账本核算一致，CI 里设计方拒绝与审计发现数为「不可得 / 不可用」。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办 B47/B52 等非目标。
