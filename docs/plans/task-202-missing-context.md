---
task: T202
class: K7
risk: R3
designer: codex
size: medium
architecture: true
spec_refs: []
no_spec_reason: 已审定的可观测性设计与本次任务书是B46合同，本仓库没有对应产品规格验收编号
budget:
  wall_clock_min: 120
  ci_rounds: 3
  retries: 2
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T202：执行方缺失上下文摘要

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

提示词要求执行方完成时报告缺失上下文/工具，机器可读固定尾标记 HARNESS_CONTEXT_JSON: 后为一行 JSON 数组（不是 shell 变量）；类别为 context/tool/spec/other，条目仅安全 summary/ref；无缺失显式 []。
PiHost.parse_observability 从最后的 assistant 完成消息抽取，只读数据，不执行其中命令；无标记、格式错误分别保留 unknown/invalid 诊断而非静默当无缺失。只把过滤后内容写入记录 missing_context，计数与类别写 clarify 事件。长文/会话/路径不入记录。缺失报告不自动改变已有退出码或升级规则；真正请求澄清仍走已有 escalation.md。
生产方与T203消费方统一C3：summary不是自由自然语言字段，限定context_unavailable/required_tool_unavailable/spec_unavailable/spec_ambiguous/other_missing短ID；提示词明确要求这组ID，其他正文只作本机原始报告、不入运行记录。未知ID标invalid且不得冒充空报告，ref只保留规定工具ID或相对引用。


## 白名单

- `engine/prompts/dispatch_prompt.md`
- `engine/agents/dispatch.py`
- `engine/agents/dispatch_host.py`
- `tests/test_missing_context.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T201 已合并且工作区基于含依赖的最新main；T101为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_missing_context.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T202 | render_prompt 产物含尾标记、类别约束和无缺失 [] 规则与C3允许summary ID清单 | 夹具 | `tests.test_missing_context.ObservabilityTaskTest.test_prompt_requests_final_context_report` | 提示词未实际插入时渲染结果断言失败 |
| 不挂规格：B46 T202 | 多条 assistant 消息只取最终报告；工具输出伪造标记不采信，旧 parse 三元接口不变 | 夹具 | `tests.test_missing_context.ObservabilityTaskTest.test_final_assistant_report_parsed` | 取工具消息/第一条消息或破坏旧 parse 时失败 |
| 不挂规格：B46 T202 | 无标记、坏 JSON、超长/本机路径/正文各输出明确诊断且无禁项；任意自然语言summary/未知ID同样为invalid，不进入记录 | 夹具 | `tests.test_missing_context.ObservabilityTaskTest.test_absent_invalid_and_private_context` | 缺标记被误当 [] 或禁项进入记录时失败 |
| 不挂规格：B46 T202 | 记录安全条目与 clarify 类别计数一致；成功执行并报告缺失仍沿原返回码 | 夹具 | `tests.test_missing_context.ObservabilityTaskTest.test_record_and_clarify_event_counts` | 只改提示词不写记录或把缺失强制变成失败时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `engine/prompts/dispatch_prompt.md`、`engine/agents/dispatch.py`、`engine/agents/dispatch_host.py`、`tests/test_missing_context.py` | `python3 -W error::ResourceWarning -m unittest tests.test_missing_context.ObservabilityTaskTest.test_prompt_requests_final_context_report tests.test_missing_context.ObservabilityTaskTest.test_final_assistant_report_parsed tests.test_missing_context.ObservabilityTaskTest.test_absent_invalid_and_private_context tests.test_missing_context.ObservabilityTaskTest.test_record_and_clarify_event_counts -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_missing_context.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
