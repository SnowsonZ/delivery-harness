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
  wall_clock_min: 600
  ci_rounds: 3
  retries: 2
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T601：可观测性全链与缺陷注入验收

负责方：**派发任务**。设计方 Codex；独立评审使用现行评审链中的非 Codex 席位（Claude Code 优先、OpenCode 候补）。本次修订须先通过独立设计评审并合并；任务书未在 `origin/main` 前不能派发。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

测试专用，不能修改产品代码迎合验收。临时 git 夹具项目安装待测引擎，用假 Pi/gh 与受控远端/API 模拟平台；实际调用 dispatch、verify、risk/policy、review、CI 导出/导入、GitHub 同步、ledger、trace、audit、alert、weekly 的产品入口。假合并只用于夹具，不操作真实仓库。
至少一条任务 PR 完整链与一条无派发的设计方 PR；CI 含 harness 与 route 两种来源、多个 job、重跑及旧 head，删本机库后从记录/账本与 CI 导入复原。合法旧 head 只留 `head_mismatch` 信息，缺失或畸形的 head 仍报非信息性的 `api`，不能把真实故障当历史跳过。人工制造缺独立评审（对 R2 路径）、缺路由、缺记录、改内容、删中间、删尾、锚点不符、引用哈希不符，audit/alert 需逐个命中，恢复后通过。
测试验证新行为本身，不仅测假gh的固定响应；断言引用来自实际产物/记录、账本与假平台事实一致，不能把期望事件预先塞进库替代生产链。真实平台全链仍由设计方G3补，假平台不声称它已通过。

本任务自身只新增测试，实际 PR 为 K2/R0，当前路由可自动合并，独立评审不是该风险等级的合并门禁。夹具仍须覆盖 R2 缺评审的负例；G3 用 T601 的真实 R0 PR 核任务链，再用 P5 的 T502 真实 R2 PR 核独立评审与设计方复核，不能把两条路径混称为同一条 PR。

## 夹具接口与隔离

- 复用 `tests/gh_fakes.py` 的 `FakeGhBase`，仅在本任务新测试文件内派生夹具。基类已有常用 PR、评论、运行、artifact、`actions/workflows` 路由，但 `pr()` 尚不返回 `createdAt`，`actions/workflows` 默认为空，也没有 `actions/workflows/<id>/runs` 路由；子类须提供这三处当前 `load_ci` 所需的真实响应形状，并用带 `auto-merge PR #<号> @ <完整head>` 的 `display_title` 验证判定运行关联。未配置路由继续失败关闭，不把任意请求答成空列表。
- 当前 head 的包必须由产品导出入口生成、经假平台 artifact 下载并由真实 `load_ci` 导入；同一运行重导入只增加 `skipped`。旧 head 与重跑的运行由假平台 API 提供，不预写期望事件替代生产链。`github:` 快照的账本核对按 B118/B121 已合并的九字段语义进行：仅字段顺序或未纳入的 API 元数据变化不能报 `ledger_mismatch`，纳入字段的真实变化必须报。
- 夹具只用匿名临时 Git 仓库与隔离的事件库；不依赖本机 `pi` 可执行文件、`origin/main` 引用、真实凭据、网络或当前仓库 PR。运行产品入口时仍注入假 host/gh，不把自身测试产生的事件混入任务链。

## 白名单

- `tests/test_observability_e2e.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、CI 轮次、准入或守卫；墙钟预算按用户已定的 600 分钟执行，不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T501、T502 已合并，P5 的 G1/G2 门禁完成，工作区基于含依赖的最新 main；T101 为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_observability_e2e.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T601 | 实际产品入口形成从 admit 到 merge 账本的任务链；两种 CI 来源、重跑和带 run-name 的判定运行由假平台按当前 API 形状提供；trace 找到最长/首失败，audit 无适用 error，账本与假平台事实逐项一致；weekly 新事件小节与原文本及持久来源口径一致 | 夹具 | `tests.test_observability_e2e.ObservabilityTaskTest.test_real_entrypoints_deliver_complete_task_chain` | 任一入口未连接、判定运行漏导入、预写假事件替代产品产物、weekly 重算旧指标或把本机库并入主数时失败 |
| 不挂规格：B46 T601 | 无派发设计方 PR 可审；删除本机库后从安全远端资料复原，不要求不存在的 dispatch；重复导入当前 head 幂等，合法旧 head 不使 audit/trace 失败，畸形 head 仍报 `api` | 夹具 | `tests.test_observability_e2e.ObservabilityTaskTest.test_designer_pr_and_cold_machine_reconstruction` | 审计依赖本机库、强制所有 PR 都派发、旧 head 误报或畸形数据被吞时失败 |
| 不挂规格：B46 T601 | 缺评审（R2 路径）/路由/记录、改内容、删中间/尾、锚点/引用哈希不符逐项独立注入并断言稳定 finding；`github:` 快照按九字段语义核对，同义快照不误报，真实字段变化会报；恢复后无适用 error | 夹具 | `tests.test_observability_e2e.ObservabilityTaskTest.test_injected_missing_stages_and_tamper_are_found` | 恒真 finding、把同义快照当篡改、或只靠内部链校验而漏掉删尾时失败 |
| 不挂规格：B46 T601 | 反复注入同 reason 只一条评论/标签；导出/账本/summary 扫描无 db/WAL/SHM/原始日志/会话，且测试不触碰真实平台 | 夹具 | `tests.test_observability_e2e.ObservabilityTaskTest.test_alert_idempotence_and_no_raw_data_publication` | 告警重复、本机数据越界外发或访问真实平台时失败 |

## 步骤与提交顺序

以下是实现与验证顺序；任务书修订合并、依赖与门禁完成后按用户指令派发，执行方按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `tests/test_observability_e2e.py` | `python3 -W error::ResourceWarning -m unittest tests.test_observability_e2e.ObservabilityTaskTest.test_real_entrypoints_deliver_complete_task_chain tests.test_observability_e2e.ObservabilityTaskTest.test_designer_pr_and_cold_machine_reconstruction tests.test_observability_e2e.ObservabilityTaskTest.test_injected_missing_stages_and_tamper_are_found tests.test_observability_e2e.ObservabilityTaskTest.test_alert_idempotence_and_no_raw_data_publication -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_observability_e2e.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与定向变异复核：至少使判定运行漏匹配、旧 head 误报、畸形 head 被放过、账本语义真差异被放过、尾事件缺失、告警重复各自被对应断言抓住；先确认未变异基线通过。仅新增测试通常不触发消费方等价 G2；若实际改了判定/守卫/派发/配置，则由设计方在 Agent-Notification 独立 worktree 另做 G2。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。
