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
| `reports/` | 事件时间线、CI 包、GitHub 事实、合并账本、审计、交付度量与周报 |
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
**可观测性证据**：事件按来源和追踪 ID 串联，运行记录、CI 包与合并账本固定锚点。`events` 导出/导入安全包，`trace --ci` 复原时间线，`audit` 核对已合并 PR 的引用与完整性，`alert` 发布去重告警；观察或发布故障不改变原判定。


## 安装与使用

见 [README.md](README.md) 的 Quick start 与「Platform setup」（GitHub：ruleset、批准 App、单账号模式；`install` 同时写入 `.github/` 下的工作流与 ruleset 模板，批准方式由 `checks.toml [platform] approval` 选择）。要点：引擎以带锁文件的内置副本装进业务仓库（`.harness/engine/` 与 `.harness/engine.lock`），升级只能经 `upgrade` 整体替换并由用户批准的 PR 合并；引擎不带任何使用者自己的默认值，缺必填配置时明确报错。

## 可观测性命令

命令经 `bin/harness` 或内置引擎入口执行。任务编号与 PR 号使用当前项目的值：

| 命令 | 用途 |
|---|---|
| `events [--since 1d] [--stage verify] [--status fail] [--json]` | 过滤展示本机事件及计数 |
| `events --export <文件>` / `events --import <文件>` | 导出整库 EventBundle / 校验并幂等导入 |
| `trace <任务编号\|PR号\|完整分支> [--ci] [--json]` | 复原时间线、最长阶段与首失败；`--ci` 下载 PR 的 CI 包 |
| `audit <PR> [--json]` / `audit --all-merged [--since 30d] [--json]` | 审计已合并 PR 的引用、完整性与锚点 |
| `alert <reason> [--pr <号>] [--trace <分支>] [--task <任务>] [--bundle <目录\|JSON文件>] [--json]` | 按触发证据发布去重告警 |
| `weekly` | 旧指标之后追加事件汇总；本机补充单列 |

```sh
bin/harness events --since 1d --stage verify --json
bin/harness events --export build/harness-events.json
bin/harness events --import build/harness-events.json
bin/harness trace T001 --json
bin/harness trace 42 --ci --json
bin/harness audit 42 --json
bin/harness audit --all-merged --since 30d --json
```

`--export` 导出整库完整链前缀；导出/导入不能与展示过滤参数同用，混用退出 2。包只含安全结构化数据与内容哈希，不复制 SQLite、WAL/SHM、原始验证日志或会话。导入核对 schema、隐私与链哈希，按哈希去重，不覆盖冲突；CI 下载还按 API 核对仓库、工作流、run/attempt/job 与 head。默认分支工作流的 `run-name` 关联判定运行，包按该运行自己的 API 身份核对。

合法旧 head 未导入会显示信息性的 `head_mismatch`，不使 trace/audit 失败；缺失、畸形 head 和其他导入发现仍是失败。`audit` 只覆盖已合并 PR：退出 0 表示适用检查通过，1 表示有发现，2 表示参数/审计配置错误或整体 API/不可审计 PR 故障。缺失和过期资料不当作已核验，引用只按数据解析，不执行。

### 数据、留存与配置

| 来源 | 位置与留存 | 范围 |
|---|---|---|
| 本机观察 | git 公共目录下 `harness/harness.db`，worktree 共用、不入库 | 事件、引用、锚点不按保留期自动删除 |
| 本机产物与派发原始流 | 公共目录下 `harness/artifacts/`、`dispatch/runs/` | 默认 30 天；每天首次 emit 清理，已终止的原始流仅留本机 |
| CI 包 | Actions artifact：`harness-events-<run>-<attempt>-<job>` | 模板保留 90 天，失败后也上传并生成 summary |
| 合并账本 | `harness-audit:<合并UTC年份>/<PR号>.json` | 只写已合并 PR，Git 中无自动过期 |

运行记录固定已发生阶段的前缀锚点，CI 包携带锚点；PR 的 `harness-audit:<PR>` 评论固定账本路径、提交、文件 SHA-256 与链头。writer 正常 fast-forward 追加，同字节幂等、同 PR 不同字节拒绝。ruleset 禁删除和改写历史，仍允许普通提交覆盖旧文件；审计以固定期望核对，内部链仍合法的删尾也能被发现。安全边界见 [SECURITY](SECURITY.md#observability-evidence)。

`.harness/config/checks.toml` 的可选默认值：

```toml
[events]
enabled = true
artifact_days = 30

[audit]
require_review_risk = 2
require_route_for_auto = true
require_run_record_for_task = true
verify_anchors = true
```

事件环境开关见 [CHANGELOG](CHANGELOG.md)，优先于配置；非法 `artifact_days` 提示后按 30 处理。未知/非法 audit 配置报错，不静默放宽；该配置只影响审计报告。设计方 PR 豁免派发记录，任务 PR 要有记录；评审、批准要求按当时适用的风险/路由与平台批准模式核对。

### 告警与周报

告警使用 PR 评论加 `escalation` 标签，无 PR 时复用升级议题；远端 `(trace, reason)` 标记保证换机后仍去重。reason 见 `bin/harness alert --help`。**下例会真实发布，仅在存在触发证据且确需发布时运行：**

```sh
bin/harness alert audit_anchor_mismatch --pr 42 --bundle build/harness-events.json --json
```

`--bundle` 只读取触发证据，不导入或执行；无证据不发布。只查语法用 `--help`。发布失败不改变原判定；默认分支可信 job 承载成功或失败的告警，PR 检查令牌保持只读。不新增常驻守护、OTLP 或通知渠道。

派发预警由 `.harness/config/rules.toml` 的可选 `[alerts]` 控制：缺整节则关闭，增加该节则启用最后一轮 CI 预警。`guard_denials_threshold` 默认不启用；正整数启用每轮被拒工具调用数预警，缺省/非法值关闭该预警，非法值另有提示。这些配置不改变预算或守卫判定。

周报保留旧小节与来源：A 主数取本周合并 PR 的账本事件耗时（不重复计运行记录摘要）及当前 checkout 中 `ended_at` 落在 UTC 周窗口的运行记录；B 本机补充单列，`CI=true` 一律不可用。覆盖说明 PR 数与旧人工干预率合并数一致。规则命中数与被拒工具调用数单位不同，不相加、不对账；A 不含设计方拒绝，CI 不实算本机审计发现数。资料缺失/读失败写不可用，不补造 0。

### 账本平台设置（用户执行）

`install` 提供 `.github/rulesets/harness-audit.json`；升级需主动同步模板，见 [升级步骤](docs/upgrading.md)。用户在仓库 **Settings** 打开 **Rulesets**，选择 **New ruleset → Import a ruleset**，打开 JSON、核对后点 **Create**；已有账本 ruleset 则更新，避免重复创建。参见 [GitHub 导入步骤](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/managing-rulesets-for-a-repository#importing-a-ruleset)。核对分支、active、禁删除/改写历史与 bypass actors。

首次成功 writer 创建账本分支。可信 `harness.yml` job 需要 `contents: write`、`pull-requests: write`、完整历史检出、Git 作者/提交者身份及临时克隆的 `gh auth setup-git` 凭据。真实合并后只读核对：

```sh
gh api repos/OWNER/REPO/rulesets --paginate
gh api repos/OWNER/REPO/branches/harness-audit
gh api repos/OWNER/REPO/actions/permissions/workflow
```

确认 active ruleset 包含 `deletion`、`non_fast_forward` 且无意外 bypass，并核对实际工作流权限及账本文件/锚点评论。权限错误表示该项尚未核验，使用用户已有授权读取，不扩大 Agent 令牌。writer 有 `continue-on-error` 隔离，main 检查绿不能代替账本发布成功证据。

## 当前状态

引擎版本仍为 0.1.0，发版版本/tag 由用户决定。B46 实现（含 T501/T502 与 T601 全链夹具）已合并；阶段升级、消费方等价、真实 R0/R2 链复原与审计已核验。最终文档与用户平台证据是剩余收尾门禁，不据此声称已发布新版本。英文文案、可配置目录、TypeScript 插件与接入探测仍在待办。

## 开发

开发入口与规则见 [AGENTS.md](AGENTS.md)，维护者工作流见 [docs/maintainers.md](docs/maintainers.md)；未关闭事项见 [docs/backlog.md](docs/backlog.md)，路线与已定设计见 [docs/plans/2026-09-29-roadmap.md](docs/plans/2026-09-29-roadmap.md)。

本仓库用自己的引擎开发：`.harness/`、`bin/`、git 与 Agent 钩子、`.github/workflows/` 是本项目自己的实例（`.harness/engine/` 是上一次合并的引擎的内置副本），不属于产品；产品是 `engine/` 与 `templates/`。

## 维护

- 引擎仓库的每个改动都属于护栏本身，经 PR 由维护者批准；发布版本号由维护者确定后打 tag。
- 新增或删除组件时同步更新本文件的「组成」与「原则与落点」。

B46 可观测性设计、实现合同与门禁定义：[执行计划](docs/plans/2026-09-29-observability-execution-plan.md)、[共用合同](docs/plans/2026-09-29-observability-task-contracts.md)、[需求追溯表](docs/plans/2026-09-29-observability-traceability.md)。
