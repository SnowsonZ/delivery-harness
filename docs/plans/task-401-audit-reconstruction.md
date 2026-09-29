---
task: T401
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

# T401：审计引用复原与哈希核对

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

注册 audit <PR号> 或 --all-merged --since 30d（两模式互斥），支持 --json。按 API trace/head、C6账本与本机/CI已导入事件复原安全环节视图；下载CI由C5/T302共享能力，更新GitHub事实由T304共享，不写合并路由。
逐引用核对 path@rev（git show 原始字节）、GitHub URL/ID（规范化API快照或不可变评论正文，定义见C6）、本机artifact（sha256/size）与CI安全JSON。URL只允许当前仓库受支持的 GitHub资源，不执行引用命令、不任意抓外网；path不能逃出仓库；失败区分 unavailable/expired/hash_mismatch，不把未核对当通过。
提供 inspect_pr(pr:int, *, gh=None, cwd=ROOT)->dict，报告基础形状 C7，T402扩展规则。批量分页所有已合并PR，确定性时间范围。退出码本任务有hash_mismatch或必需引用不可得为1，全核对为0，调用/参数故障为2；P4加强规则后仍沿此约定。

## 白名单

- `engine/cli.py`
- `engine/reports/audit.py`
- `tests/test_audit_reconstruction.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T305 已合并且工作区基于含依赖的最新main；T101为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_audit_reconstruction.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T401 | 运行记录/CI/评审/merge夹具复原每阶段输入输出决定及 local/ci/github来源，head一致 | 夹具 | `tests.test_audit_reconstruction.ObservabilityTaskTest.test_rebuild_input_output_decisions_by_source` | 只显示事件名或来源混合时失败 |
| 不挂规格：B46 T401 | git文件、评审评论/API快照、本机artifact、CI JSON分别核对哈希和大小，结尾换行变化也检测 | 夹具 | `tests.test_audit_reconstruction.ObservabilityTaskTest.test_each_reference_kind_matches_original_bytes` | common.git截断换行或使用不同规范化方式时失败 |
| 不挂规格：B46 T401 | 内容修改、30天本机产物到期、90天CI到期与API无权限分别产生不同 findings，不报全通过 | 夹具 | `tests.test_audit_reconstruction.ObservabilityTaskTest.test_mismatch_expired_and_unavailable_distinct` | 取不到引用一律跳过当成功时失败 |
| 不挂规格：B46 T401 | 假gh分页/日期边界与恶意path/URL均有断言，拒绝执行引用内容；CLI退出码0/1/2明确 | 夹具 | `tests.test_audit_reconstruction.ObservabilityTaskTest.test_batch_pagination_since_and_safe_resolution` | 漏页、越范围或安全解析失效时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `engine/cli.py`、`engine/reports/audit.py`、`tests/test_audit_reconstruction.py` | `python3 -W error::ResourceWarning -m unittest tests.test_audit_reconstruction.ObservabilityTaskTest.test_rebuild_input_output_decisions_by_source tests.test_audit_reconstruction.ObservabilityTaskTest.test_each_reference_kind_matches_original_bytes tests.test_audit_reconstruction.ObservabilityTaskTest.test_mismatch_expired_and_unavailable_distinct tests.test_audit_reconstruction.ObservabilityTaskTest.test_batch_pagination_since_and_safe_resolution -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_audit_reconstruction.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
