# 落地交接说明：Agent-Notification 标杆项目（2026-09-25）

> 状态：迁自 Agent-Notification（2026-10-09 快照，正文未改；业务库不再留存 harness 抽取与设计文档，只留引用）。文中指向业务库专属文件的链接已改为该仓库的绝对地址。
> 历史文档。原为调研报告《低人工干预下 AI Agent 持续高质量交付：理论与全链路最佳实践》（[入库快照](../research/2026-09-23-agent-delivery-theory.md)）的最后一节，2026-09-26 拆出单独保存，报告原文已删去该节。它记录 2026-09-25 开工时的基线、失败分类、可验证性地图与 P0–P6 路线。现行做法见 [可验证交付方案](2026-09-25-verifiable-delivery.md) 与 [harness 说明](https://github.com/SnowsonZ/delivery-harness/blob/main/README.zh-CN.md)，未关闭事项见 [待办清单](https://github.com/SnowsonZ/Agent-Notification/blob/main/docs/plans/backlog.md)。


**当前状态（2026-09-26）**：P0–P6 已完成并合并；已有四次执行方试跑（trial-001、任务 002–004）。下方计划保留作历史记录；现役规则见仓库 `docs/specs/delivery-harness.md`，未关闭事项以 `docs/plans/backlog.md`（待办清单）为准。

- **合并方式**：Agent 用单独的 GitHub 账号推送和开 PR；main 要求非推送者批准最后一次推送。R0/R1 由专用 GitHub App 批准后自动合并，R2 以上由用户批准并合并。2026-09-26 两类都已在 GitHub 上实测。
- **本机护栏**：Claude Code、OpenCode、Pi、Zcode 桌面版的命令守卫按命令结构判断，拒绝改写历史、推 tag、强推 main、合并或批准 PR；git 钩子兜底。
- **判定器**：统一 `bin/verify`、验收编号映射、修复证据（Python 与 Swift）、30 余个事故回放、变异测试基线。
- **仍未闭合**：V2、V4 已于 2026-09-26 完成；其余未关闭事项（首次发版审批、SwiftUI 截图回归、真实数据回放与预算自动比对等）统一见[待办清单](https://github.com/SnowsonZ/Agent-Notification/blob/main/docs/plans/backlog.md)。

目标：以 [SnowsonZ/Agent-Notification](https://github.com/SnowsonZ/Agent-Notification) 为核，把本报告的全链路落成机器可强制执行的流程，让每一类改动都有可判定的标准。本节用于中断后恢复：新对话只需读这一节并重新克隆仓库。

## 当前状态

- 技术栈：Python（scripts/，CLI 与采集）+ Swift（native/，原生界面与桌面组件），CI 为 GitHub Actions macos-26。基线 main = d8bde8d（v0.8.0）。
- 云端可跑：`python3 -W error::ResourceWarning -m unittest discover -s tests`（2026-09-25 实测 267 项通过）、ruff。Swift 与真机界面只能在 macOS（CI 或本机）验证。
- 已有强项：规范即合同（docs/specs）、偏离需写证据、“查不到价格不编造”、验收表区分证据类型、CI 结构断言、AGENTS.md。
- 主要缺口：规则多为文字约定，缺少机器强制的 harness。

## 错误分析：v0.8.0 交付中的 R1–R19（docs/plans/usage-cost-widgets-delivery.md）

| 失败类型 | 实例 | 应有的防线 |
| --- | --- | --- |
| 声称已修但代码未变 | R8、R15、R10 共三次，修复未提交即被历史改写清掉 | 修复证据自动生成，验收由 CI 重跑 |
| 回归测试不足 | 十几项修复测试只 +1，混入 R17 | 每个修复先有会失败的测试 |
| 自述代替 CI | 主 workflow 从未运行却写“同口径通过”（R5、R18） | 只认 CI run 链接 |
| 隐私泄露 | 真实快照被 git add -A 提交到公开仓库（R1） | pre-commit / hook 拦截 |
| 破坏性操作 | filter-repo 改写本地 main 与 18 个 tag | deny hook：禁改写历史、推 tag、强推 main |
| 数据降级 | 27 天报告被降级（R2），源于规格漏洞 | 迁移定 R3 风险，不变量测试 + 真实数据回放 |
| 没有预算 | D0 探针约 20 轮 CI | 轮次上限与升级 |

## 可验证性地图（初判）

| 任务类别 | 判定器 | 缺口 |
| --- | --- | --- |
| 来源采集（7 家） | 真实格式夹具 + 期望输出 | 覆盖不均，无新格式报警 |
| 计价与金额 | 不变量（周期合计 = 逐日之和等） | 改属性测试 |
| 报告迁移与固化 | 不变量“任何重写不降级” | 自动化回放 |
| CLI 输出 | 黄金 JSON 快照 | 缺快照比对 |
| Swift 纯逻辑 | 纯函数单测 | 状态机抽纯函数，修复先写失败测试 |
| SwiftUI 界面与组件 | CI 截图回归 | 目前只有人工截图 |
| 真机导航与授权 | 人工验收 | 清单结构化、缩小范围 |

## 路线（每阶段一个 PR）

1. P0 基线：Agent Readiness 体检、上述失败分类入库、记录基线指标；路线写入 docs/plans/。
2. P1 判定器：统一 verify 入口；金额与迁移的属性测试；关键模块变异测试。
3. P2 结构护栏：Claude Code / Codex 共用 hooks，禁止历史改写、推 tag、强推 main、提交 scratch 与快照，实现类任务不改测试。
4. P3 规格可执行化：验收编号映射到测试名，CI 检查；任务模板；模块风险等级。
5. P4 独立评审：评审清单来自失败分类；修复证据自动生成。
6. P5 界面可验证化：SwiftUI 截图回归，人工只验真机导航。
7. P6 度量与飞轮：复验轮数、“声称已修”次数、逃逸缺陷；R 系列变成可重放评测，测 pass^k。

## 待用户决定

- [x] 改动落库方式：已决定。Agent 用单独账号 `Snowson` 推送与开 PR，不碰 main 与 tag；合并规则见上方「当前状态」（方案 §13 D3、D4）。
- [x] 本地 Agent 组合：已决定。Codex 与 Claude Code 负责设计、评审和顾问；Zcode、OpenCode、Pi 负责实现、测试和部署。

## 恢复方式

新对话中对 Claude 说：“克隆 SnowsonZ/Agent-Notification，读 AGENTS.md、docs/plans/backlog.md（待办清单）与 docs/plans/verifiable-delivery.md 的 §10 决定记录，按待办清单继续。”
