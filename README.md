# harness：可验证交付的判定器与护栏

本目录把「AI Agent 在低人工参与下持续、稳定、高质量地交付」落成机器可执行的规则。本项目是这套做法的标杆：目标是**有效**（有数据证明）且**可复制**（脚本、模板和文档能被其他项目照搬）。

## 愿景

每一类改动的「完成」，都从「Agent 说做完了、人去复验」变成：

- 开工前就有可执行的判定；
- 完工后由 Agent 之外的机器重跑判定，并留下证据；
- 人只做机器做不了的事：定义意图、设计约束、审证据、处理例外。

自治程度不靠信任，而是按「可验证性 × 风险」逐级放开：判定器越可靠、风险越低，放得越多（R0/R1 自动合并，R2 以上由用户审批）。

## 理念来源

- 调研报告《低人工干预下 AI Agent 持续高质量交付：理论与全链路最佳实践》（2026-09-23）：入库快照 [docs/research/2026-09-23-agent-delivery-theory.md](../docs/research/2026-09-23-agent-delivery-theory.md)，编辑源为 [Claude 文档](https://claude.ai/code/artifact/487213ef-a1a8-44fd-9399-2e37f91c8b0c)。先读它的「核心结论」和「3.2 八条设计原则」。
- 开工时的落地计划：[2026-09-25 落地交接说明](../docs/plans/2026-09-25-harness-rollout-handoff.md)（原为报告最后一节：基线、失败分类、可验证性地图、P0–P6 路线），历史文档。
- 落到本仓库的方案与决定：[可验证交付方案](../docs/plans/verifiable-delivery.md)（§1 目标、§2「可验证」的操作定义、§10 决定记录）。
- 本项目自己的失败样本：v0.8.0 交付中的 R1–R19 与 X1–X6，分类见 [2026-09-25 基线评审](../docs/review/2026-09-25-harness-baseline.md)。每条护栏都对应其中至少一种失败。

## 原则与落点

报告的八条设计原则，在本仓库分别由这些部分承担：

| 原则（报告 3.2） | 本仓库的落点 |
|---|---|
| 1. 先有判定器，再放权 | `verify.py`（本机、云端、CI 同一条命令）；`acceptance.py`（规格验收编号必须有测试或登记缺口）；只有判定可靠的 R0/R1 才自动合并 |
| 2. 确定性优先 | 风险等级由 `risk.py` 按改动路径判定，不由执行者自报；修复证据由 `evidence.py` 生成；评审证据包由 `review_pack.py` 汇总 |
| 3. 生成与验证分离 | 角色分工：Codex、Claude Code 设计与评审，Zcode、OpenCode、Pi 执行；判定在 CI 中重跑；`auto-merge.yml` 执行 main 上的定义；Agent 用单独账号，合并须由非推送者批准 |
| 4. 结构防护胜过提示词 | 三层护栏（见下）；执行方不能改判定器；已有测试按 base 版本运行（`base_tests.py`）；判定器本身被检验（修复前必须失败、变异测试、事故回放） |
| 5. 小批量、可回滚 | 一个任务一个 PR，按 R0–R3 分级；回滚方式写进任务书 |
| 6. 状态外置、上下文从简 | 任务书与模板（`docs/templates/`）、[待办清单](../docs/plans/backlog.md)、决定记录；中断后从文件恢复 |
| 7. 自治度按「任务类别 × 风险」 | `rules.toml` 的 `[risk]`：R0/R1 由 App 批准后自动合并，R2 以上自动请用户评审 |
| 8. 每次失败都沉淀为 harness 改进 | 缺陷编号全局唯一，修复带 `Defect:` 与回归测试；`replay_cases.py` 事故回放集；变异与质量基线只升不降 |

## 组成

**判定器**（回答「做对了吗」，都由 `verify.py` 或 CI 调用）

| 文件 | 作用 |
|---|---|
| `verify.py` | 统一验证入口：lint、卫生、验收映射、Python 与 Swift 测试、回放 |
| `acceptance.py`、`acceptance-gaps.txt` | 规格验收编号 ↔ 测试的映射；暂缺的登记在缺口清单，只能缩减 |
| `evidence.py` | 修复证据：退回带 `Defect:` 的提交，引用该编号的测试必须以断言失败结束，恢复后通过 |
| `base_tests.py`、`base_tests_runner.py` | 已有测试按 base 版本在新代码上运行，防止执行方改测试迁就代码 |
| `replay.py`、`replay_cases.py` | 事故回放：把历史缺陷注入代码副本，对应检查必须失败 |
| `mutate.py`、`mutation-baseline.json` | 关键函数的变异测试，得分只升不降 |
| `quality.py`、`quality-baseline.json` | 熵治理：复杂度与体量只降不升 |
| `hygiene.py` | 禁止提交的路径、超大文件、凭据、本机真实路径 |
| `risk.py` | 按改动路径判定 R0–R3 |
| `release_check.py` | 发版前核对版本号与 tag |
| `metrics.py` | 交付度量，与 v0.8.0 基线并列 |
| `review_pack.py` | 评审证据包：把机器结论汇成一页 |

**三层护栏**（回答「最坏会怎样」）

| 层 | 组成 | 挡住什么 |
|---|---|---|
| Agent 层 | `command_guard.py` + `shell_structure.py`，由各宿主的钩子调用：`.claude/settings.json`、`.codex/hooks.json`、`.opencode/plugin/`、`.pi/extensions/`、`.zcode/config.json` | 执行前拒绝改写历史、推 tag、强推 main、合并或批准 PR、设置覆盖变量；执行方不能编辑判定器 |
| git 层 | `git_guard.py` + `.githooks/`，规则读 origin/main 上的 `rules.toml` | 与用哪家 Agent 无关：保护分支上提交、改写、推送卫生 |
| 服务端 | ruleset（`.github/rulesets/`）、`build.yml`、`auto-merge.yml`、单独的 Agent 账号与批准 App | 本机两层都被绕过时的兜底：必须经 PR、必需检查、非推送者批准 |

**配置**：`rules.toml`（风险、卫生、守卫规则）、`common.py`（公共工具）。

## 从哪里读起

1. 本文件。
2. 报告的「核心结论」与「3.2 八条设计原则」。
3. [现役规范 delivery-harness.md](../docs/specs/delivery-harness.md)：命令、约定、一次性设置、验证状态、已知边界。
4. [待办清单](../docs/plans/backlog.md)：还没做完的事。
5. 需要追溯决定时读 [可验证交付方案](../docs/plans/verifiable-delivery.md) §10，追溯失败样本时读 [基线评审](../docs/review/2026-09-25-harness-baseline.md)。

## 维护

- 新增或删除 harness 组件时，同步更新本文件的「组成」与「原则与落点」。
- 本目录由评审方维护，执行方不能编辑（唯一例外：`acceptance-gaps.txt` 只能删行）；改动属于 R3，由用户批准。
- 报告有实质更新时，评审方重新导出快照，覆盖 `docs/research/2026-09-23-agent-delivery-theory.md` 并更新其中的导出日期与版本。
