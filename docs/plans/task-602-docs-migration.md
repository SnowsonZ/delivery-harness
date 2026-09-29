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
  wall_clock_min: 90
  ci_rounds: 3
  retries: 2
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T602：最终文档、平台步骤与迁移收尾

负责方：**设计方**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

设计方任务，不交Pi派发。README双语命令表含events/trace/audit/alert、导入导出、账本路径/留存/标记、ruleset应用步骤；给出用户需执行的可审阅平台步骤但不代执行。SECURITY说明默认分支可信链、同用户篡改窗口、ruleset不防普通覆盖、产物过期/不可取证与被拦下PR账本B52边界。
CHANGELOG Unreleased按实际已合并行为汇总，**Migration:**列新增命令、可选checks [events]/[audit]、rules [alerts]、模板与工作流同步及harness-audit分支设置；不定版本或打tag。docs/upgrading说明模板不自动覆盖已有工作流/配置，README与AGENTS同步入口。
设计方完成G1/G2/G3与用户G4证据后才声明B46功能验收完成；G5版本与发布仅在用户决定发版时执行，不因版本尚未定阻塞功能收尾。真实平台未验证如实写状态，不先关闭待办。

## 白名单

- `README.md`
- `README.zh-CN.md`
- `SECURITY.md`
- `CHANGELOG.md`
- `docs/upgrading.md`
- `AGENTS.md`

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

本任务由设计方写文档，不运行派发；未来产生的文档改动仍须用户明确授权提交/推送/开PR。

## 前置条件

- 依赖 T601 已合并且工作区基于含依赖的最新main；T101为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- 设计方核对本任务书已经合并且与origin/main一致，再开始文档收尾；此为人工核对，不调用派发。后期接口差异先修订合同并经原审批。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T602 | 按产品 --help 与匿名临时夹具执行README命令例，核对双语配置键/默认值/错误码 | 人工 | 设计方逐条核对 `final_commands_config`，留命令输出与差异；`bin/harness docs`只查链接，不替代内容核对 | 命令不支持或双语默认值不同则人工验收不通过 |
| 不挂规格：B46 T602 | upgrade migration_notes能读到新增Migration条目；平台操作步骤及权限/ruleset/工作流同步无缺项 | 人工 | 设计方逐条核对 `final_migration_platform`，留命令输出与差异；`bin/harness docs`只查链接，不替代内容核对 | 只写CHANGELOG正文无Migration标记或缺用户设置时不通过 |
| 不挂规格：B46 T602 | 逐项对照设计2.5/3.7/4/7和C6，确认OTLP/常驻进程/B52/B31边界与引用过期说明 | 人工 | 设计方逐条核对 `final_security_limits`，留命令输出与差异；`bin/harness docs`只查链接，不替代内容核对 | 宣传可阻止同用户篡改/全部PR长期账本则不通过 |
| 不挂规格：B46 T602 | 追溯表G1-G5的命令输出/用户平台确认齐全，版本未定明确留给用户；没有凭文档手写通过 | 人工 | 设计方逐条核对 `final_gate_evidence`，留命令输出与差异；`bin/harness docs`只查链接，不替代内容核对 | 缺真实全链或升级等价证据却宣称完成则不通过 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `README.md`、`README.zh-CN.md`、`SECURITY.md`、`CHANGELOG.md`、`docs/upgrading.md`、`AGENTS.md` | `bin/harness docs` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | 无额外文件 | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
