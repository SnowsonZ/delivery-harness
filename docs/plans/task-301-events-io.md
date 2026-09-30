---
task: T301
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

# T301：事件导出与幂等导入

负责方：**派发任务**。设计方 Codex；独立评审方 OpenCode。当前任务书为待拆分评审草案，合并前不能派发；派发、提交、推送、开PR均由用户逐次明确授权。

依据：[已审定设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[追溯表](2026-09-29-observability-traceability.md)。先读共用合同C0，再读本任务所引用的接口；本任务白名单与验收不能由执行方扩大或缩减。

## 目标终态

定义 C5 的 EventBundle v1、查询/导出/导入 API；原事件 hash/seq/prev_hash/source 一字不改，导入不再 emit/re-hash。按事件 hash 去重、引用/锚点幂等；同 source/trace/seq 不同 hash 为冲突，原库不覆盖，返回明确发现。
导出完整链前缀与锚点，展示用 --since/--stage 过滤结果不是可导入 bundle。事件形状与设计 JSON 一致（inputs、outputs、decision、error、actor），内部 canonical 恢复规则与 T101 一致。导入验证 schema、来源、隐私、规范化 hash、链完整性后事务提交，坏包整体不写；未来版本不写，旧 v1 可读。
CI 内容输出只允许经过过滤的结构化 JSON 文本产物（比如检查名/状态/数值/安全规则理由），以 sha256/size 引用；原始命令输出、verify 完整日志与 Pi 流绝不导出。JSON 文本放内容寻址 artifacts，manifest 指明允许外发的文件及哈希，不上传 SQLite/WAL/SHM。

## 白名单

- `engine/core/events_io.py`
- `engine/core/events_db.py`
- `tests/test_events_io.py`（新增；不存在才可开始）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0的隐私、失败隔离和非目标同样适用。

## 前置条件

- 依赖 T205 已合并且工作区基于含依赖的最新main；T101为已有基础。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- `test_events_io.py` 尚不存在；仅新增本任务测试。测试使用匿名临时git仓库、隔离ROOT、假gh/host及冻结时钟，不碰真实库/PR/工作流。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；后期发现新接口差异先修订任务书并经原审批，不能以“按实际代码”为由变更合同。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B46 T301 | 往返后事件所有 canonical 字段/refs/hash 一致；manifest 只含过滤的 JSON，扫描无库、日志、会话 | 夹具 | `tests.test_events_io.ObservabilityTaskTest.test_export_shape_hashes_and_safe_manifest` | 重新 emit 或把原始 artifact 一并上传时失败 |
| 不挂规格：B46 T301 | 同包导入两次行数不变，两 run/attempt/job 的同 trace seq=1 均存在且链各自完整 | 夹具 | `tests.test_events_io.ObservabilityTaskTest.test_reimport_is_idempotent_and_sources_isolated` | 统一 source=ci 或未去重会冲突/重复 |
| 不挂规格：B46 T301 | 坏哈希、缺中间项、冲突 seq、未来 schema、隐私禁项各令导入失败且原库字节/逻辑行不变 | 夹具 | `tests.test_events_io.ObservabilityTaskTest.test_bad_bundle_is_atomic_and_not_repaired` | 先写一半后失败或重算哈希掩盖篡改时失败 |
| 不挂规格：B46 T301 | 查询 since/stage 正确缩小显示；导出仍包含验证所需前缀并核对链头 | 夹具 | `tests.test_events_io.ObservabilityTaskTest.test_filtered_display_does_not_export_partial_chain` | 把筛选后的断链子集当可导入包时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在将来用户授权派发后，执行方按派发提示词提交。设计方当前只留工作区改动。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 核对依赖接口，完成目标终态与对应断言 | `engine/core/events_io.py`、`engine/core/events_db.py`、`tests/test_events_io.py` | `python3 -W error::ResourceWarning -m unittest tests.test_events_io.ObservabilityTaskTest.test_export_shape_hashes_and_safe_manifest tests.test_events_io.ObservabilityTaskTest.test_reimport_is_idempotent_and_sources_isolated tests.test_events_io.ObservabilityTaskTest.test_bad_bundle_is_atomic_and_not_repaired tests.test_events_io.ObservabilityTaskTest.test_filtered_display_does_not_export_partial_chain -v` | 本任务验收全部行 |
| 2 | 验证失败隔离与范围，整理交付证据 | `tests/test_events_io.py` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`，不修改既有测试、质量基线来换通过。验收必须调用产品入口并核对具体结果，不能仅 mock emit 返回值或检查测试函数存在。设计方另做逐行验收与针对本任务断言的变异复核；修改判定/守卫/派发/配置时还由设计方做Agent-Notification独立worktree等价验证（追溯表G2），不是执行方自己写“通过”。

同一失败连续三轮无新证据时停止该路径；预算或接口不符则按既有升级方式报告当前差距、已尝试与新证据、推荐备选及待决定问题。每条验收的正负断言必须保留，不能为赶预算删减断言或失败方式；如夹具样本需缩小，先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围。不得自行扩大白名单、改原任务书、实现待办B47/B52等非目标。


## 修订记录（2026-09-30，设计方裁决首轮评审与变异复核）

- **评审一般项（采纳为第二轮修复）**：导入校验 `refs.size` 允许数字字符串与负整数（`_validate_scalar` 的短字符串口径误及 size）——此类包导入成功后 SQLite INTEGER 亲和转换使 `events_db.verify()` 报「内容与 hash 不符」。第二轮改为 **size 仅接受非负 int**（与 emit 写入口径一致），数字字符串/负数/浮点记 `schema` finding 并补正反例回归。
- **变异复核发现的测试缺口（补测试）**：`_plan_anchors` 的锚点幂等（重导入时与库内相同的锚点跳过）无测试覆盖——定向变异「禁用锚点去重」下 `test_reimport_is_idempotent_and_sources_isolated` 仍通过。第二轮补断言：重导入相同锚点零重写（skipped/库内行数不变）。事件侧幂等的 M1b 变异已被该测试抓住 ✓。
- **材料包限制（登记 B74，不在本任务）**：`write_materials` 的 diff 200K 截断使本 PR 大 diff 在评审材料中被裁剪（评审严重项由来）；修 separable，随引擎材料包任务处理。
- 第 2 次（`--resume`）步骤：上述两项修复 + 回归；验收表各行不变，另有本节。
