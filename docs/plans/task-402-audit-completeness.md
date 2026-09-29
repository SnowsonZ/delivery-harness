---
task: T402
class: K7
risk: R3
designer: codex
size: large
architecture: true
spec_refs: []
no_spec_reason: 已审定的可观测性设计与本次任务书是B46合同，本仓库没有对应产品规格验收编号
budget:
  wall_clock_min: 150
  ci_rounds: 3
  retries: 2
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T402：审计完整性与锚点防篡改

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

在 C7 报告加完整性/防篡改 findings：R2/R3 必须有独立评审且身份不同，自动合并必须有 main来源 route.result，任务PR须运行记录与阶段锚点；R0/R1 app自动合并批准者App且绑定合并head，none模式明确无App适用，不误报。
校验各(source,trace)的原始链、固定运行记录/CI/PR评论锚点与对应前缀链头、账本/运行层一致性；不同阶段的前缀锚点不是最终链头，不把合法后续追加当篡改。尾删即使内部链仍合法也能由固定锚点发现；未知/无法取得链不能当完整。
可选 checks.toml [audit] require_review_risk=2, require_route_for_auto=true, require_run_record_for_task=true, verify_anchors=true；缺省执行设计规则，非法配置报告 configuration_error/退出2，不静默放宽。配置只能影响 audit 报告，不改变原有 policy/guard/verify 判定；报告有发现退出1。

## 白名单

- `engine/reports/audit.py`
- `templates/.harness/config/checks.toml`
- `tests/test_audit_completeness.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T401 已合并且工作区基于含依赖的最新main；T101为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_audit_completeness.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T402 | 人为删R2评审/auto路由/task记录及锚点各报对应稳定 finding；设计方PR不需派发记录 | 夹具 | `tests.test_audit_completeness.ObservabilityTaskTest.test_required_stages_by_risk_and_pr_kind` | 只跑链校验而未检查应有环节时失败 |
| 不挂规格：B46 T402 | app批准旧head/人冒充App被发现；none模式不凭空要求App；评审同设计方被发现 | 夹具 | `tests.test_audit_completeness.ObservabilityTaskTest.test_app_approval_binds_merged_head_and_none_is_supported` | 只看有APPROVED状态或误报none时失败 |
| 不挂规格：B46 T402 | 正常后续追加不报错；删尾/改中间/账本对运行层不同分别被发现 | 夹具 | `tests.test_audit_completeness.ObservabilityTaskTest.test_prefix_anchor_tail_deletion_and_ledger_mismatch` | 比较最终head造成误报或仅verify_chain漏尾删时失败 |
| 不挂规格：B46 T402 | 缺省/合法可选/非法配置以及库关闭/坏库都如实报告；原路由返回不受审计配置影响 | 夹具 | `tests.test_audit_completeness.ObservabilityTaskTest.test_config_defaults_invalid_and_event_independence` | 非法值静默关规则或让audit进入policy时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `engine/reports/audit.py`、`templates/.harness/config/checks.toml`、`tests/test_audit_completeness.py` | `python3 -W error::ResourceWarning -m unittest tests.test_audit_completeness.ObservabilityTaskTest.test_required_stages_by_risk_and_pr_kind tests.test_audit_completeness.ObservabilityTaskTest.test_app_approval_binds_merged_head_and_none_is_supported tests.test_audit_completeness.ObservabilityTaskTest.test_prefix_anchor_tail_deletion_and_ledger_mismatch tests.test_audit_completeness.ObservabilityTaskTest.test_config_defaults_invalid_and_event_independence -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_audit_completeness.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
