---
task: T601
class: K2
risk: R0
designer: codex
size: large
architecture: false
spec_refs: []
no_spec_reason: 已审定的可观测性设计与本次任务书是B46合同，本仓库没有对应产品规格验收编号
budget:
  wall_clock_min: 180
  ci_rounds: 3
  retries: 2
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T601：可观测性全链与缺陷注入验收

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

测试专用，不能修改产品代码迎合验收。临时git夹具项目安装待测引擎，用假Pi/gh与受控远端/API模拟平台；实际调用 dispatch、verify、risk/policy、review、CI导出/导入、GitHub同步、ledger、trace、audit、alert、weekly 的产品入口。假合并只用于夹具，不操作真实仓库。
至少一条任务PR完整链与一条无派发的设计方PR；CI两个来源job/重跑，删本机库后从记录/账本与CI导入复原。人工制造缺独立评审、缺路由、缺记录、改内容、删中间、删尾、锚点不符、引用哈希不符，audit/alert需逐个命中，恢复后通过。
测试验证新行为本身，不仅测假gh的固定响应；断言引用来自实际产物/记录、账本与假平台事实一致，不能把期望事件预先塞进库替代生产链。真实平台全链仍由设计方G3补，假平台不声称它已通过。

## 白名单

- `tests/test_observability_e2e.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T501 已合并且工作区基于含依赖的最新main；T101为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_observability_e2e.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T601 | 实际产品入口形成从admit到merge账本的完整链，trace找到最长/首失败，audit无发现，假平台事实逐项一致 | 夹具 | `tests.test_observability_e2e.ObservabilityTaskTest.test_real_entrypoints_deliver_complete_task_chain` | 任何入口未连接或预写假事件替代产品产物时失败 |
| 不挂规格：B46 T601 | 无派发设计方PR可审；删除本机库后安全远端资料复原且不要求不存在的dispatch | 夹具 | `tests.test_observability_e2e.ObservabilityTaskTest.test_designer_pr_and_cold_machine_reconstruction` | 审计依赖本机库或强制所有PR都派发时失败 |
| 不挂规格：B46 T601 | 每个缺环节/篡改单独注入并对稳定finding断言，再恢复后无发现 | 夹具 | `tests.test_observability_e2e.ObservabilityTaskTest.test_injected_missing_stages_and_tamper_are_found` | 恒真finding断言或删尾仅靠内部链校验时失败 |
| 不挂规格：B46 T601 | 反复注入同reason只一条评论/标签；导出/账本/summary扫描无db/WAL/SHM/原始日志/会话 | 夹具 | `tests.test_observability_e2e.ObservabilityTaskTest.test_alert_idempotence_and_no_raw_data_publication` | 告警重复或本机数据越界外发时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `tests/test_observability_e2e.py` | `python3 -W error::ResourceWarning -m unittest tests.test_observability_e2e.ObservabilityTaskTest.test_real_entrypoints_deliver_complete_task_chain tests.test_observability_e2e.ObservabilityTaskTest.test_designer_pr_and_cold_machine_reconstruction tests.test_observability_e2e.ObservabilityTaskTest.test_injected_missing_stages_and_tamper_are_found tests.test_observability_e2e.ObservabilityTaskTest.test_alert_idempotence_and_no_raw_data_publication -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_observability_e2e.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
