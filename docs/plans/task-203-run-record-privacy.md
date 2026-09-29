---
task: T203
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

# T203：运行记录内容检查（B38）

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

在现有 check_record 完成 JSON/身份/提示词哈希核对后，对新旧记录都递归检查所有字符串（含字典键与列表），出现本机路径、命令正文或会话正文则新增 Finding 不通过；run-check CLI 原只报告行为保持，policy 已有 run_findings 路径负责拒绝自动合并。
采用 C3 的明确可发布字段/值策略：既有枚举、数字、哈希、仓库相对引用/URL、安全规则摘要与结构化 missing_context 可通过；未知自由文本字段按不可信内容拒绝。已知字段也检查路径/换行与命令/对话形态，不能只检查新字段。旧格式无新增字段仍可通过，旧合法枚举/规则摘要不能误拒。
Finding 只写字段位置/规则名，不回显禁项；本任务不读事件来判定记录合格。
最小判定集按C3的字段/值表实现；先拒绝路径/换行等禁项，guard_denials仅精确可信主线内置规则理由可保留（即使理由提及命令），不是放行任意理由正文。其他字段先按类型校验，命令前缀/连接符/会话标记的明确禁例与合法pattern/ref/ID正例均测试。unknown free text fail-closed，但不能把合法模型/规则ID当命令；不宣称启发式可理解所有自然语言。

## 白名单

- `engine/routing/run_check.py`
- `tests/test_run_record_privacy.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T202 已合并且工作区基于含依赖的最新main；T101为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_run_record_privacy.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T203 | 命令全文、Unix/Windows 本机路径、多行会话与短单行命令/对话夹具在顶层/嵌套/列表/键中均令 Finding 不通过；C3的C01-C07禁例与A01-A05放行例逐个具名子用例 | 夹具 | `tests.test_run_record_privacy.ObservabilityTaskTest.test_each_forbidden_content_is_rejected` | 只查长文或新字段会漏负例；仅凭长度或任意摘要前缀放行也失败 |
| 不挂规格：B46 T203 | 真实旧记录形状（匿名值）与新 C3 字段、合法相对 prompt_path、哈希/模型/规则摘要各通过；可信规则中提及git命令/连接符仍可通过，伪造追加正文不行 | 夹具 | `tests.test_run_record_privacy.ObservabilityTaskTest.test_old_and_new_safe_records_pass` | 把所有字符串或旧字段一刀拒绝时失败；不能把未知guard理由当可信例外 |
| 不挂规格：B46 T203 | 失败报告中只出现字段位置与规则，既无原命令也无路径/会话 | 夹具 | `tests.test_run_record_privacy.ObservabilityTaskTest.test_finding_does_not_echo_secret` | 报告拼接原值导致隐私二次泄漏时失败 |
| 不挂规格：B46 T203 | 经现有 policy.gather 读取坏记录时原运行记录 Rule 为 fail；事件库有无不影响这个结论 | 夹具 | `tests.test_run_record_privacy.ObservabilityTaskTest.test_policy_rejects_bad_record_without_event_dependency` | 只输出警告而 Findings 仍 ok 或依赖事件时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `engine/routing/run_check.py`、`tests/test_run_record_privacy.py` | `python3 -W error::ResourceWarning -m unittest tests.test_run_record_privacy.ObservabilityTaskTest.test_each_forbidden_content_is_rejected tests.test_run_record_privacy.ObservabilityTaskTest.test_old_and_new_safe_records_pass tests.test_run_record_privacy.ObservabilityTaskTest.test_finding_does_not_echo_secret tests.test_run_record_privacy.ObservabilityTaskTest.test_policy_rejects_bad_record_without_event_dependency -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_run_record_privacy.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
