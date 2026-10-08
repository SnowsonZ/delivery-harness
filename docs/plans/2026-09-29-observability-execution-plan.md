# 可观测性执行计划（B46 实现，路径 A：先自举，再用 Pi 派发）

状态：**执行中**（2026-09-29 更新）。阶段 0（自举）与 T001、T101 已完成并合并；剩余24份任务书已形成v2工作区草案（22份可派发、2份设计方文档任务）；OpenCode v1评审结论需修订，F1–F9已在草稿处理，F10细化决策在[共用合同C8](2026-09-29-observability-task-contracts.md)待用户明确审定。[追溯表](2026-09-29-observability-traceability.md)、[评审原文/处理](../review/2026-09-29-observability-split-review.md)与[结构核对证据](../review/2026-09-29-observability-split-structure-check.md)已同步；OpenCode第二轮结论可提交（待用户审C8），4条非阻断建议已澄清；当前未提交、未派发。全部材料按[任务拆分流程](../task-splitting.md)一并组成一个文档PR。依据：[可观测性与审计设计](2026-09-29-observability-design.md)（已审定）。

## 1. 路径与前提

- **路径 A**：先给本仓库自举（B49：装上自己的守卫、`bin/`、CI 判定与派发），再用 `bin/dispatch run <任务书>` 交给 Pi 实现。可观测性用它自己要观测的那套机制开发。
- **执行方只有 Pi**：`dispatch_host.py` 目前只有 `PiHost`。zcode、opencode 作执行方需要先写宿主适配器（B47），不在本计划内；OpenCode 继续做独立评审。
- **分工**：设计方由 ZCode 接任（2026-09-29 用户指派；Zcode 代行 codex 席位，任务书头部仍写 designer: codex——受引擎 DESIGNERS 枚举所限，扩展枚举另立待办）；任务书、文档收尾、等价验证与产出复核由设计方做；实现由Pi做；独立拆分评审与PR评审由OpenCode做（每张 R2+ 代码 PR 的独立评审为必经步骤，2026-09-30 用户定；docs-only 豁免）；批准合并、平台设置与版本决定由用户做。历史T101头部不改。
- **提交规则**：我改完只留在工作区；提交、推送、开 PR 逐次由用户确认（含任务书 PR）。Pi 派发出的 PR 由 `bin/dispatch` 以 Agent 账号开出，这是派发流程本身，不需要额外授权，但**派发命令本身每次由用户说了才跑**。

## 2. 阶段 0：自举（B49）——已完成（#7、#8、#12）

目标：本仓库能跑 `bin/verify`、`bin/dispatch`，git 层与 Agent 层守卫生效，CI 出 `harness` 判定。

| 步 | 内容 | 谁 |
|---|---|---|
| 0.1 | `python3 engine/cli.py install --target .`（在干净的 main 上）：写入 `.harness/engine`（当前 main 的内置副本）、`engine.lock`、配置骨架、`bin/`、`.githooks/`、各 Agent 钩子、`.github/workflows/{harness,auto-merge,quality}.yml`。已有文件（`ci.yml`、`.github/rulesets/main.json`）不覆盖 | 我 |
| 0.2 | `checks.toml`：`[identity] agent_login`（Agent 账号）、`[sources] code = ["engine/**","templates/**"]`、`python_dirs = ["engine"]`、`[verify] requirements = "requirements-dev.txt"`（新增，锁 `ruff==0.16.8`）、`pinned_tools = ["ruff"]`、项目检查 `lint`（quick / default / full）与 `tests`（default / full）、`[release]`（`engine/__init__.py` 的 `__version__`） | 我 |
| 0.3 | `rules.toml`：R3 加 `engine/**`、`templates/**`（整个仓库都是护栏本身）；`[taskbook] guard_paths` 同加；`[taskbook.modules]` 粗粒度：`engine`、`templates`、`tests`；`[review]` 评审方与模型 | 我，模型由用户定 |
| 0.4 | `autonomy.toml` 保持全部人审；`quality-baseline.json` 用 `bin/harness quality --update` 生成；`replay_cases.py` 保持空 | 我 |
| 0.5 | `.github/rulesets/main.json`（本仓库自己的那份）要求的检查加上 `harness`（保留两项 `test`） | 我改文件 |
| 0.6 | 本机：`bin/harness guard-git install`；`bin/verify --full` 通过 | 我 |
| 0.7 | 平台与授权（**用户**）：把更新后的 ruleset 应用到仓库（`gh api repos/<所有者>/<仓库>/rulesets/<id> --method PUT --input .github/rulesets/main.json`）；首次启动 Claude Code / Zcode 时确认项目钩子（Pi 不需要）；确认 Agent 账号已是协作者且 `gh` 已登录 | 用户 |
| 0.8 | 冒烟：写一份 K2 / R0 小任务书（为 `install.migration_notes` 补边界测试），合并后 `bin/dispatch run` 走通「派发 → 本地判定 → 开 PR → CI → 评审」全链，暴露自举与 Pi 接入的问题 | 我写任务书，用户下令派发 |

已知代价，需要你认可：

1. **内置副本滞后**：`.harness/engine/` 是上一次合并的引擎，守卫与判定都用它；引擎每次改动合并后，要再走一个 `upgrade` PR 才生效。**可观测性的每一期在自己的 `upgrade` PR 合并前，本仓库自己的流程不会产生事件。**
2. **`harness` 与 `ci.yml` 的重复**：`harness` job 的 `verify --full` 会再跑一遍 ruff 与 unittest，与 `ci.yml` 的跨系统矩阵重叠（每次多约 10 秒）。保留 `ci.yml` 是因为它覆盖 macOS。
3. **不需要批准 App**：`autonomy.toml` 全部人审，`auto-merge` 永远走 `request-review`；两账号 + 你批准即可。

阶段 0 的验收：`bin/verify --full` 通过；CI 出现 `harness` job 且通过；冒烟任务走通，运行记录格式完整。

## 3. 阶段1（P1）：加固、埋点与等价

### 3.1 通用合同

所有任务先读[共用合同C0](2026-09-29-observability-task-contracts.md)，只改任务书白名单、新增独立测试，不改原判定与既有测试。引用准备、观察API及清理/发布失败不得影响原返回码/业务异常；原始日志与Pi流仅本机，CI/账本只发布安全JSON。新stage/status不随任务增加。设计方收尾文档不交Pi派发。

触及engine/templates的任务为K7/R3、architecture: true；T106/T601只新增测试为K2/R0、architecture: false；T107/T602安全/流程文档仍按K7/R3由设计方处理。任务书均由用户审后合并，不能以架构声明或文档PR合并替代派发授权。

### 3.2 已合并的公共接口

T101实际API为权威（原计划的摘要已修正）：

```python
def emit(stage, step, status, *, trace_id=None, duration_ms=None, inputs=None,
         outputs=None, decision=None, error=None, actor=None, source=None) -> int | None: ...
def ref(kind, ref, *, sha256=None, size=None) -> dict: ...
def file_ref(kind, path, rev=None) -> dict: ...
def store_artifact(data: bytes | Path) -> dict: ...
def span(stage, step, **fixed): ...
def current_trace() -> str: ...
def enabled() -> bool: ...
def set_anchor(trace_id, stage, head_hash, fixed_in, source=None) -> None: ...
def chain_head(trace_id, source=None) -> str | None: ...
def verify_chain(trace_id=None, source=None) -> list[str]: ...
```

SQLite v1四表带source，按(source, trace_id)串链，WAL、busy_timeout=5000。库与内容寻址产物均在git公共目录。T109补强prev_hash/seq的独立断言、step/空引用过滤、span与产物读取失败隔离、原子保存、新版库读取保护。

本次待审补充：Actions来源用run/attempt/job隔离避免不同临时库seq=1冲突；actor.host仍ci，显式source与本机local兼容，公共签名/四表/hash算法不变。详情共用合同C2；此为设计细化草案，不称已审定。

### 3.3 P1任务与合并顺序

T101首个任务已完成全链。剩余P1顺序（2026-09-29 并行度修订后）：**T109 →（T102 ∥ T103 ∥ T104）→（T105 ∥ T108 ∥ T110）→ T106 → T107 → G1/G2**。

| 任务 | 内容 | 负责与前提 |
|---|---|---|
| [T109](task-109-events-hardening.md) | 事件库加固、CI来源隔离 | Pi；先于所有新埋点 |
| [T102](task-102-events-verify.md) | CLI包装、verify各项/汇总、integrity内容 | Pi；守卫放行不产生入口事件 |
| [T103](task-103-events-route.md) | risk逐文件/汇总、route facts/各Rule/result | Pi；显式PR trace贯穿risk与route |
| [T104](task-104-events-guard.md) | command/git拒绝事件 | Pi；放行不记，原拒绝行为不变 |
| [T105](task-105-events-agents.md) | dispatch全链/review、执行流计数/产物 | Pi；观察元数据外置dispatch_observation.py，保留旧Pi parse接口，提供C6固定安全评审摘要 |
| [T108](task-108-events-checks.md) | 其余八模块的内容事件 | Pi；与 T102–T105、T110 并行，各目标/缺陷/指标按条记录 |
| [T110](task-110-step-clean.md) | emit 写入值清洗口径修正（B63①） | Pi；与 T105/T108 同轮并行 |
| [T111](task-111-dispatch-template-fixes.md) | 派发模板与 B65/B57 残留小修 | Pi；T106 合并后派发，与 T107 并行 |
| [T112](task-112-guard-and-path-parsing.md) | 命令守卫 heredoc 误报与任务书路径解析小修 | Pi；T106 合并后派发，与 T107/T111 并行 |
| [T106](task-106-events-equivalence.md) | 全部原判定入口三态等价夹具 | Pi；只新增测试，不改产品代码 |
| [T107](task-107-events-docs.md) | P1安全/事件配置与变更记录 | 设计方；不派发 |

### 3.4 设计方复核与阶段门禁

每个实现PR复核实际运行记录、当前head的CI/harness输出、OpenCode独立评审、白名单、原decide/evaluate/run_check返回路径；每条验收对应产品入口与具名断言，必要定向变异在副本做，不把评审“通过”替代测试强度。

每期结束G1同步本仓库内置引擎/配置/工作流（用户逐次授权PR）；G2在Agent-Notification独立worktree做升级前后bin/verify --full与测试数/质量/变异对比。历史556是前次基线，新的基线先实际采集；链校验及事件出现也由命令输出认定。涉及判定/守卫/派发/配置的实现PR仍逐个做消费方验证，不仅期末一次。

不在用户主目录试装，不在OpenCode评审期间替换内置副本。门禁操作与证据详见追溯表G1–G5，均尚未执行。T201摘要组装外置run_timeline.py、T205条件下沉alerts.py，避免派发体量棘轮，不上调基线。

## 3.5 收尾兜底与流程固化规则（2026-09-30 实践沉淀）

**过渡期兜底（B70/B71 修复进 G1 前）**：派发死在收尾步时按五步兜底——①确认槽内工作完好；②`bin/as-agent git push` 分支并 `git ls-remote` 核对；③核对/触发 PR 的 CI（`workflow_dispatch` 兜触发失灵）；④挂「CI 绿→独立评审」后台链（评审占用则排队）；⑤把该次 B 例记入运行备注。不做强推、不改写历史。

**任务书规约（设计方写任务书时适用）**：

1. **冻结表配套授权**：任务书引入「钉死快照」类断言（期望表、样本清单、QUIET 副本等）时，验收表须注明配套副本的位置与同步修改授权（实例：EXPECTED_STAGE、校准样本、QUIET_COMMANDS 三次升级往返）。
2. **行为中性显式声明**：改动若与 base 行为等价（默认值相同、等价重写），任务书/PR 必须显式声明「行为中性 + 价值定位（一致性重构/接线收口）」，否则评审按假修复打回（实例：T116、T122 二轮）。
3. **记录体积自检**：运行记录的 prompt 快照与 stages 由引擎自检（B77），任务书不预设手工截断。
4. **评审材料双轨**：材料包截断（G1 前）期间，评审工作区核对 + 设计方全量 diff 复核双轨互证；G1 后回归单轨。

### 派发检查单（2026-10-01 固化；当日一次通过率 ~50% 的返工根治）

任务书出稿后、派发前由设计方逐项核对，PR 描述或任务书修订记录留勾选痕迹：

1. **时效**：任务书引用的每个文件/接口在最新 `origin/main` 上核对存在（实例：T122 白名单指向 T123 抽离前的旧位置）。
2. **消费方扫描**：对每个白名单文件 grep 其改动对象在 `engine/ tests/ templates/` 的全部引用方，逐个标注「需配套授权（写入白名单）/无需配套（一行理由）」——**改写入方必查读取方与校验方**（实例：T124 漏评审提示词与 C6 审计、T125 漏 run_check 校验表）。
3. **CHANGELOG**：行为可见变化时默认列入白名单（实例：四张 PR 连续漏记）。
4. **并行互斥**：同窗口任务在白名单之外再比对**共享数据结构形状**——改 shape 的任务与读该 shape 的任务不得并行（实例：T125 收敛 stages 与 T305 账本读 source 同窗口，合并后 CI 断）。
5. **病灶实证**：任务书动机段附代码级证据（文件:行号），不得从崩溃症状推断（实例：B71「无重试」误诊两轮全废）。
6. **评审修复完全闭环**：评审 N 条发现的修复提交逐条映射；涉及形状/口径变化的同步**配套测试期望清单**（实例：指针行改七键后三处测试期望漏同步）。
7. **文件体量预算**（2026-10-03 补）：白名单里每个要改的文件，当前行数加上预计新增的行数，不得越过质量棘轮的阈值（`engine` 下单文件 `> 800` 行计入 `files_over_800`，基线为 0）；会越过时，先拆分任务（实例：T703 让 `dispatch.py` 从 789 行涨到 864 行，执行方在白名单内无解，只能停下请求澄清；`review.py` 正好 800 行，T702 同样会撞上，由 T707 先拆分）。

## 4. 后续各期（合同一次性拆完，依赖完成后方可执行）

| 期 | 顺序与范围 | 前提/负责方 |
|---|---|---|
| P2 | T201记录时间线/链头 →（T202缺失上下文 ∥ T203内容检查）→（T204产物/终止原始流30天清理 ∥ T205派发预警） | P1 G1/G2完成；Pi实现，设计方阶段升级/等价 |
| P3 | T301导出/幂等导入 → T302events/trace与CI下载 → T303模板artifact/summary → T304GitHub事实 → T305合并账本/锚点/ruleset模板 | P2 G1/G2完成；真实.github副本由设计方，平台设置G4由用户 |
| P4 | T401audit复原/引用哈希 → T404audit观察事件/变更记录 → T402完整性/锚点/配置 → T403alert与可信工作流末尾 | P3 G1/G2/G4完成；audit.py/cli.py/模板串行 |
| P5 | T501周报事件小节与旧指标对账 ∥ T502 audit/trace不再把旧head运行当失败（B124，T601前置） | P4升级后；不改旧数据来源；T501与T502互不依赖 |
| P6 | T601夹具全链/缺陷注入 → G3真实派发PR全链核对 → T602最终文档/Migration/平台步骤 | T601为Pi测试任务；T602设计方、不派发；版本G5由用户、在发版时做 |

26份剩余任务书的完整链接、依赖、预算、文件串行序与85条设计追溯见[追溯表](2026-09-29-observability-traceability.md)。**不是24个派发任务：26份任务书中24份可派发（含修订新增的T110、T404）、2份设计方文档任务。** 并行对（T102∥T103∥T104、T105∥T108∥T110、T202∥T203、T204∥T205）经设计方核定白名单不交叉，其余连续接口/文件决策按表串行；不为用满三个槽位引入合并冲突。

后期若实际代码与合同不符，设计方修订任务书并经原审查/用户确认后再派发，不能让执行方自己扩白名单或更改设计。全部任务书/共用合同/追溯表/执行计划先由OpenCode只读拆分评审，再处理发现，整包作为一个文档PR由用户审。OpenCode v1结论为需修订，第二轮复核已给可提交结论（待用户审C8）；未获得提交/派发授权。

## 5. 风险与应对

| 风险 | 应对 |
|---|---|
| Pi 与所选模型完成不了较大的任务 | 任务书把接口、签名、白名单、验收命令写死（第 3.2 节）；一个任务一个 PR；预算内失败按升级包处理，我接手修订任务书而不是放宽范围 |
| 判定被埋点悄悄改动 | T106 的三态逐字相同测试；复核时看 diff；R3 由你批准 |
| 执行方在同一 OS 用户下能改本机库 | 设计已声明：防篡改不防止，B31 才解决；派发不依赖事件做任何决定 |
| 内置副本滞后，自举后短期没有事件可看 | 每期 `upgrade` PR 合并后才生效；P1 的等价证据在 Agent-Notification 上取得 |
| 并行任务合并时 ruleset 要求分支最新 | 白名单不相交；合并一个后其余 rebase（`bin/dispatch` 续跑 `--resume`） |
| zcode / opencode 不能作执行方 | 本计划不含；适配器在 B47，若你要它们也做执行方，另立任务并提前 |

## 6. 已定的决定与遗留

已定（用户 2026-09-29）：阶段 0 的三项代价可接受；Pi 执行方用 `--model zai-coding-cn/glm-5.3`，评审方沿用 OpenCode（glm-5.3）；基础接口按3.2已合并代码；C2来源隔离与共用合同的新增接口/格式/配置细化待本次用户审定；任务书的提交方式与顺序按 [任务拆分流程](../task-splitting.md)。

遗留：独立评审（OpenCode）对「测试是否真的能失败」不敏感，T101 的评审没有发现两个未被抓住的变异（B59）；每个 PR 仍要设计方做变异检查与逐行验收核对。
