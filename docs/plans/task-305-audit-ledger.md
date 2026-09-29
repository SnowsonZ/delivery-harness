---
task: T305
class: K7
risk: R3
designer: codex
size: large
architecture: true
spec_refs: []
no_spec_reason: 已审定的可观测性设计与本次任务书是B46合同，本仓库没有对应产品规格验收编号
budget:
  wall_clock_min: 180
  ci_rounds: 3
  retries: 2
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T305：合并账本与 PR 锚点

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

C6 定义账本格式和 writer：只对已合并 PR，在 harness-audit 写 <UTC合并年份>/<PR号>.json，metadata 来自 API，收集所有 attempt/head 的安全运行记录 stages/anchors、CI artifact、route artifact、评审评论的审计摘要与 GitHub事实；CI无本机库也可完成。引用与决定摘要缺失必须列 missing，不用最新 head 的记录代替合并 head。
账本包含安全可长期保存的事件/引用/锚点与 manifest；原始日志和 Pi 流不入账本。过期 CI artifact 仅靠既存账本复原，写入时尚缺环节也如实标示。合并后才发布，关闭未合并 PR 不写（B52）。
工作流新增独立受信任 job（默认分支引擎、contents:write/pull-requests:write 的最小范围）用 GITHUB_TOKEN 正常 fast-forward 追加；禁强推，竞争失败 fetch 后有限重试，同 PR相同字节幂等，不同字节冲突不覆盖旧文件。PR留带稳定标记的简短锚点评论含账本路径/commit/hash/链头；分支 ruleset 模板禁删除/强推，不能宣称它独自禁止普通覆盖，writer 自查补这个边界。
对后登记 escape，作为独立 GitHub事实查询追加，不覆写已固定的合并账本。平台创建分支/应用 ruleset 由用户，源代码测试只用假 gh/本地 remote。
评审摘要仅按C6固定harness-review-audit标记/JSON v1解析；使用T105相同形状的匿名夹具，缺标记/未来版本/字段不完整列missing，而不是猜旧评论正文。材料必须覆盖C6的pack_v1本机引用（不上传原文），材料哈希复取按C6既有write_materials的raw_bytes/recipe规则，不统一改成另一种规范化哈希。


## 白名单

- `engine/reports/ledger.py`
- `templates/.github/workflows/harness.yml`
- `templates/.github/rulesets/harness-audit.json`
- `tests/test_audit_ledger.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T304 已合并且工作区基于含依赖的最新main；T101为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_audit_ledger.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T305 | 假平台提供运行记录、CI/route artifact、评审与 merge 事实；无本机库仍生成完整引用/决定/锚点账本；评审样例必须符合T105/C6精确形状，缺标记/字段、错误head、未来版本各报missing | 夹具 | `tests.test_audit_ledger.ObservabilityTaskTest.test_ledger_reconstructs_without_local_database` | writer 依赖本机库或漏评审/route/local摘要时失败；生产者/消费者自造不同JSON格式也失败 |
| 不挂规格：B46 T305 | 关闭未合并不写；合并PR选匹配head及全部尝试，缺材料列 missing | 夹具 | `tests.test_audit_ledger.ObservabilityTaskTest.test_only_merged_and_exact_head_are_written` | 覆盖 B52 或选最新非合并head时失败 |
| 不挂规格：B46 T305 | 本地bare remote模拟两PR竞争、同PR幂等/差异冲突，历史与旧文件不变，有限重试 | 夹具 | `tests.test_audit_ledger.ObservabilityTaskTest.test_append_conflict_and_concurrent_writers` | force push、覆盖旧账本或丢另一个PR文件时失败 |
| 不挂规格：B46 T305 | 评论标记更新而非新增；锚点含账本摘要与chain head；ruleset禁删/强推；库/原始流不外发 | 夹具 | `tests.test_audit_ledger.ObservabilityTaskTest.test_anchor_comment_ruleset_and_privacy` | 只写分支无PR锚点、假称append-only保证或泄漏时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `engine/reports/ledger.py`、`templates/.github/workflows/harness.yml`、`templates/.github/rulesets/harness-audit.json`、`tests/test_audit_ledger.py` | `python3 -W error::ResourceWarning -m unittest tests.test_audit_ledger.ObservabilityTaskTest.test_ledger_reconstructs_without_local_database tests.test_audit_ledger.ObservabilityTaskTest.test_only_merged_and_exact_head_are_written tests.test_audit_ledger.ObservabilityTaskTest.test_append_conflict_and_concurrent_writers tests.test_audit_ledger.ObservabilityTaskTest.test_anchor_comment_ruleset_and_privacy -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_audit_ledger.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
