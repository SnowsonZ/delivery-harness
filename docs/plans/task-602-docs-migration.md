---
task: T602
class: K7
risk: R3
designer: codex
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

# T602：最终文档、平台步骤与迁移收尾

负责方：**设计方**，不派发。设计方 Codex；由非 Codex 席位独立评审文档 PR。本次修订须先通过独立设计评审并合并；T602 实施仍在 T601 与 G3 之后。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

设计方任务，不交 Pi 派发。README 双语命令表按实际 `--help` 核对 `events`（含 `--export`/`--import`）、`trace <标识> --ci`、`audit <PR>`/`--all-merged`、`alert <reason> --bundle`；写清本机事件库与 CI artifact 的范围、`harness-audit:<合并年份>/<PR>.json` 路径/留存/锚点、ruleset 应用步骤。平台步骤列出由用户执行的操作与只读核对命令，设计方不代执行。
SECURITY 说明默认分支可信链、同用户篡改窗口、ruleset 不防普通覆盖、产物过期/不可取证与被拦下 PR 账本 B52 边界；`head_mismatch` 仅表示合法旧 head 未导入，不等于所有 CI 导入发现都可忽略。周报章节写明 T501 的持久来源主数与本机补充单列、CI 不实算审计发现、守卫两种计数不对账。
CHANGELOG Unreleased 按实际已合并行为汇总，**Migration:** 只列确需使用方操作的新增命令/可选配置、模板与工作流同步及 `harness-audit` 分支设置；T502 的信息性发现修复若无迁移动作，不虚构迁移步骤。不定版本或打 tag。`docs/upgrading.md` 说明模板不自动覆盖已有工作流/配置，README 与 AGENTS 同步入口。
设计方核对 P5 G1/G2、G3、T602 后的 P6 G1/G2 的实际输出与用户 G4 的平台证据后才声明 B46 功能验收完成；G5 版本与发布仅在用户决定发版时执行，不因版本尚未定阻塞功能收尾。真实平台未验证如实写状态，不先关闭待办。

## 白名单

- `README.md`
- `README.zh-CN.md`
- `SECURITY.md`
- `CHANGELOG.md`
- `docs/upgrading.md`
- `AGENTS.md`

## 非目标

不实现其他任务；不放宽原判定、CI 轮次、准入或守卫；墙钟预算按用户已定的 600 分钟执行，不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

本任务由设计方写文档，不运行派发；文档示例不得对真实 PR 发布测试告警或修改平台设置。

## 前置条件

- 依赖 T501、T502、T601 已合并，P5 的 G1/G2 与 G3 真实全链证据已完成，工作区基于含依赖的最新 main；T101 为已有基础。平台 G4 若仍待用户操作，可准备步骤与文档，但不得写成已完成验收。
- 设计方核对本任务书已经合并且与 `origin/main` 一致，再开始文档收尾；此为人工核对，不调用派发。后期接口差异先修订合同并经原审批。

## 验收

本任务不新增测试；下表是实施时必须逐项保存命令输出、平台资料或人工核对差异的验收，不可把任务书当成已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T602 | 按产品 `--help` 与匿名临时夹具执行 README 双语命令例，核对参数、配置键/默认值/退出码；`alert` 仅以假平台或只读 help 验证，不给真实 PR 发测试告警 | 人工 | 设计方逐条核对 `final_commands_config`，留命令输出与差异；`bin/harness docs` 只查链接，不替代内容核对 | 命令不支持、双语口径不同或示例造成真实平台写入则不通过 |
| 不挂规格：B46 T602 | `upgrade` 的 migration_notes 能读到确需迁移的 **Migration:** 条目；平台权限/ruleset/工作流与账本分支步骤无缺项，T502 无需迁移时不补造条目 | 人工 | 设计方逐条核对 `final_migration_platform`，留命令输出与差异；`bin/harness docs` 只查链接，不替代内容核对 | 只写 CHANGELOG 正文无 Migration 标记、虚构迁移动作或缺用户设置时不通过 |
| 不挂规格：B46 T602 | 逐项对照设计2.5/3.7/4/7和C6，确认OTLP/常驻进程/B52/B31边界与引用过期说明 | 人工 | 设计方逐条核对 `final_security_limits`，留命令输出与差异；`bin/harness docs`只查链接，不替代内容核对 | 宣传可阻止同用户篡改/全部PR长期账本则不通过 |
| 不挂规格：B46 T602 | G1/G2/G3 的实际命令输出、T601 R0 与 P5 R2 见证 PR 的真实路由/账本、用户 G4 平台确认齐全；G5 版本未定明确留给用户，不凭文档手写通过 | 人工 | 设计方逐条核对 `final_gate_evidence`，保存 PR/CI/评审/合并/账本 URL 与输出及差异；`bin/harness docs` 只查链接 | 缺真实全链、路由、升级等价或平台证据却宣称完成则不通过 |

## 步骤与提交顺序

以下是设计方完成文档与验证的顺序；T602 不派发。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `README.md`、`README.zh-CN.md`、`SECURITY.md`、`CHANGELOG.md`、`docs/upgrading.md`、`AGENTS.md` | `bin/harness docs` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | 无额外文件 | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成 `bin/harness docs` 与 `bin/verify --full`，逐行对照实际命令、模板和 G 项证据；独立评审检查文档有无过度声称。此文档任务不做源码变异复核，也不因只改文档而重复 Agent-Notification 引擎等价验证。缺 G3/G4 等实际证据时保留未完成状态并列出缺口，不为赶进度降低验收或关闭 B46；不得扩大白名单或实现 B47/B52 等非目标。
