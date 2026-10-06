# delivery-harness：AI 编码 Agent 的可验证交付引擎

把「AI Agent 在低人工参与下持续、稳定、高质量地交付」落成机器可执行的规则。每一类改动的「完成」，都从「Agent 说做完了、人去复验」变成：

- 开工前就有可执行的判定；
- 完工后由 Agent 之外的机器重跑判定，并留下证据；
- 人只做机器做不了的事：定义意图、设计约束、审证据、处理例外。

自治程度不靠信任，而是按「可验证性 × 风险」逐级放开：判定器越可靠、风险越低，放得越多。

[English](README.md)

## 理念来源

- 调研报告《低人工干预下 AI Agent 持续高质量交付：理论与全链路最佳实践》（2026-09-23），快照见 [Agent-Notification 仓库](https://github.com/SnowsonZ/Agent-Notification/blob/main/docs/research/2026-09-23-agent-delivery-theory.md)。先读「核心结论」和「3.2 八条设计原则」。
- 本引擎在 [Agent-Notification](https://github.com/SnowsonZ/Agent-Notification) 中建成并验证，每条护栏都对应那里记录过的真实失败（[基线评审](https://github.com/SnowsonZ/Agent-Notification/blob/main/docs/review/2026-09-25-harness-baseline.md)）。

## 原则与落点

| 原则 | 落点 |
|---|---|
| 1. 先有判定器，再放权 | `verify`（本机、CI、派发同一条命令）；`acceptance`（规格验收编号必须有测试或登记缺口）；`taskbook`（任务书不合格不派发）；只有判定可靠的 R0/R1 才自动合并 |
| 2. 确定性优先 | 风险等级由 `risk` 按改动路径判定，不由执行者自报；修复证据由 `evidence` 生成；评审证据包由 `review-pack` 汇总 |
| 3. 生成与验证分离 | 设计评审方与执行方分开；判定在 CI 中重跑；自动合并执行 main 上的定义；Agent 用单独账号，合并须由非推送者批准 |
| 4. 结构防护胜过提示词 | 三层护栏；执行方不能改判定器与合同；已有测试按 base 版本运行（`base-tests`）；判定器本身被检验（修复前必须失败、变异测试、事故回放）；引擎按锁文件哈希校验（`integrity`） |
| 5. 小批量、可回滚 | 一个任务一个 PR，按 R0–R3 分级；回滚方式写进任务书 |
| 6. 状态外置、上下文从简 | 任务书（机器可读的 YAML 头部）、模板、待办清单、运行记录；中断后从文件恢复 |
| 7. 自治度按「任务类别 × 风险」 | `policy` 按 `autonomy.toml` 的类别自治等级与误差预算路由；超预算自动停该类；合并走 GitHub 原生自动合并（须打开仓库设置 Allow auto-merge）；停机先 Disable workflow，再运行 `automerge-off` 撤销已开启的自动合并 |
| 8. 每次失败都沉淀为 harness 改进 | 缺陷编号全局唯一，修复带 `Defect:` 与回归测试；项目的事故回放集；变异与质量基线只升不降；周报与错误分析 |

## 组成

**引擎**（`engine/`，装进业务仓库后位于 `.harness/engine/`，只依赖 Python 标准库）

| 目录 | 内容 |
|---|---|
| `cli.py` | 唯一入口，全部子命令见 `bin/harness --help` |
| `core/` | 项目根与配置定位、git 与路径工具、命令结构解析、回放用例加载、安装与升级 |
| `guards/` | `command_guard`（Agent 工具钩子调用）、`git_guard`（git 钩子调用） |
| `checks/` | `verify` 与各项判定器：卫生、质量棘轮、文档链接、验收映射、任务书准入、完整性、修复证据、base 版本测试、R1 加强判定、事故回放、变异测试、发版核对 |
| `routing/` | 风险判级、合并路由、运行记录复核 |
| `agents/` | 派发、执行方运行层、独立评审与证据包、Agent 身份 |
| `reports/` | 交付度量、周报 |
| `lang/` | 语言插件（Python、Swift）：签名比对、新增依赖识别、函数体量与嵌套深度 |
| `prompts/` | 执行方与评审方的提示词模板 |

**项目自己的部分**（`.harness/` 下，属于 R3，由用户批准）

| 路径 | 内容 |
|---|---|
| `config/rules.toml` | 卫生规则、按路径判级、任务书准入、派发、评审方与模型、git 守卫 |
| `config/autonomy.toml` | 类别自治等级、误差预算、规模阈值 |
| `config/checks.toml` | Agent 身份、运行时、源码与语言、verify 检查清单、发版、变异目标 |
| `state/` | 质量与变异基线（只升不降）、验收缺口与任务书豁免清单（只减不增） |
| `project/replay_cases.py` | 本项目的事故回放用例 |

**三层护栏**

| 层 | 组成 | 挡住什么 |
|---|---|---|
| Agent 层 | `guard-command`，由各宿主钩子调用（`.claude/`、`.codex/`、`.opencode/`、`.pi/`、`.zcode/`） | 改写历史、推 tag、强推 main、合并或批准 PR、设置覆盖变量、删除议题与撤登记标签；执行方不能编辑判定器、合同与运行记录 |
| git 层 | `guard-git` + `.githooks/`，规则读 origin/main 上的版本 | 与用哪家 Agent 无关：保护分支上提交、改写、推送卫生 |
| 服务端 | ruleset、CI、单独的 Agent 账号与批准 App | 本机两层都被绕过时的兜底：必须经 PR、必需检查、非推送者批准 |
**本地事件日志**（可观测性一期）：判定、派发、守卫与路由把结构化事件写入 git 公共目录下的本地库（`harness/harness.db`，不入版本控制）与内容寻址产物目录；按 `(来源, 追踪 ID)` 哈希链串联，事后可校验是否被改动。事件只作观察，不参与任何判定与合并路由；环境变量 `HARNESS_EVENTS=off` 或 `checks.toml` 的 `[events] enabled = false` 可关闭。trace/audit 等查询命令在后续阶段。


## 安装与使用

见 [README.md](README.md) 的 Quick start 与「Platform setup」（GitHub：ruleset、批准 App、单账号模式；`install` 同时写入 `.github/` 下的工作流与 ruleset 模板，批准方式由 `checks.toml [platform] approval` 选择）。要点：引擎以带锁文件的内置副本装进业务仓库（`.harness/engine/` 与 `.harness/engine.lock`），升级只能经 `upgrade` 整体替换并由用户批准的 PR 合并；引擎不带任何使用者自己的默认值，缺必填配置时明确报错。

## 开发

开发入口与规则见 [AGENTS.md](AGENTS.md)；未关闭事项见 [docs/backlog.md](docs/backlog.md)，路线与已定设计见 [docs/plans/2026-09-29-roadmap.md](docs/plans/2026-09-29-roadmap.md)。

本仓库用自己的引擎开发：`.harness/`、`bin/`、git 与 Agent 钩子、`.github/workflows/` 是本项目自己的实例（`.harness/engine/` 是上一次合并的引擎的内置副本），不属于产品；产品是 `engine/` 与 `templates/`。

## 维护

- 引擎仓库的每个改动都属于护栏本身，经 PR 由维护者批准；发布版本号由维护者确定后打 tag。
- 新增或删除组件时同步更新本文件的「组成」与「原则与落点」。

B46可观测性实施拆分草案：[执行计划](docs/plans/2026-09-29-observability-execution-plan.md)、[共用合同](docs/plans/2026-09-29-observability-task-contracts.md)、[需求追溯表](docs/plans/2026-09-29-observability-traceability.md)。OpenCode第二轮已给可提交结论，当前仍待用户审定设计细化。
