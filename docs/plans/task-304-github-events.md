---
task: T304
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

# T304：合并、抽审与逃逸事实同步

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

在合并后 main harness 从 GitHub API 取事实，module main 接受 --pr/--head/--since，默认 push 合并提交解析关联 PR；手工合并与 App 合并均覆盖，不靠 auto-merge job 独占触发。escape 登记后的增量读取顺带放入该同步命令及 audit 查询刷新，不引入 watcher。
merge 事件记录设计 3.5 全字段，审批绑定提交、批准者 login/type、合并者/方式/标签从 API 可核对事实提取；缺批准（单账号 none）记录 none，不捏造 App；无法确定 merge 方式报告 unknown。audit_sample step 用 stage=ci，escape 用 stage=ci，保持 STAGES 不新增枚举。
GitHub 不可变事实快照 source=github:<PR号>:<规范化事实摘要>，每次快照重新成链，digest 同输入事实可复用；不在快照中加入读取时间影响去重。后续新增 escape 用不同快照而不覆盖旧事实。C5 的 JSON manifest 可长期存储事实引用。

## 白名单

- `engine/reports/github_events.py`
- `templates/.github/workflows/harness.yml`
- `tests/test_github_events.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T303 已合并且工作区基于含依赖的最新main；T101为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_github_events.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T304 | 假 API 三种合并覆盖批准者/绑定 head/方式/标签；无法取回字段为 unknown，不伪造 | 夹具 | `tests.test_github_events.ObservabilityTaskTest.test_human_app_and_none_merge_facts` | 只覆盖 App 或把 owner 当批准者时失败 |
| 不挂规格：B46 T304 | 抽样与未抽样、合并后新 escape 更新可见并保留原快照 | 夹具 | `tests.test_github_events.ObservabilityTaskTest.test_sample_and_later_escape_are_new_facts` | 只读合并当时或覆盖旧事实时失败 |
| 不挂规格：B46 T304 | 分页的多个关联 PR/议题重复同步不新增相同事实事件，分别归到 PR headRefName | 夹具 | `tests.test_github_events.ObservabilityTaskTest.test_pagination_and_repeat_sync` | 未分页或读 API 次数改变事件哈希时失败 |
| 不挂规格：B46 T304 | API 无权限/短暂失败明确报告事实缺失，原 harness 判定不改且不推送/合并/批准 | 夹具 | `tests.test_github_events.ObservabilityTaskTest.test_api_failure_does_not_change_merge` | 把不可得当完整或同步执行合并动作时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `engine/reports/github_events.py`、`templates/.github/workflows/harness.yml`、`tests/test_github_events.py` | `python3 -W error::ResourceWarning -m unittest tests.test_github_events.ObservabilityTaskTest.test_human_app_and_none_merge_facts tests.test_github_events.ObservabilityTaskTest.test_sample_and_later_escape_are_new_facts tests.test_github_events.ObservabilityTaskTest.test_pagination_and_repeat_sync tests.test_github_events.ObservabilityTaskTest.test_api_failure_does_not_change_merge -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_github_events.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
