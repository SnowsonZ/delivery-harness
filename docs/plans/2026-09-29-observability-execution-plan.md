# 可观测性执行计划（B46 实现，路径 A：先自举，再用 Pi 派发）

状态：**执行中**（2026-09-29 更新）。阶段 0（自举）与 T001、T101 已完成并合并；剩余任务按 [任务拆分流程](../task-splitting.md) 一次性拆完、做追溯表与独立拆分评审后一并提交。依据：[可观测性与审计设计](2026-09-29-observability-design.md)（已审定）。

## 1. 路径与前提

- **路径 A**：先给本仓库自举（B49：装上自己的守卫、`bin/`、CI 判定与派发），再用 `bin/dispatch run <任务书>` 交给 Pi 实现。可观测性用它自己要观测的那套机制开发。
- **执行方只有 Pi**：`dispatch_host.py` 目前只有 `PiHost`。zcode、opencode 作执行方需要先写宿主适配器（B47），不在本计划内；OpenCode 继续做独立评审。
- **分工**：设计、任务书、等价验证、复核由我做；实现由 Pi 做；批准合并与所有平台设置由用户做。
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

## 3. 阶段 1（P1）：事件库与埋点

### 3.1 通用约束（写进每份任务书）

- 只依赖标准库；不改任何判定逻辑；`emit()` 永不抛异常、永不影响退出码；`HARNESS_EVENTS=off` 关闭。
- 只改任务书白名单里的文件；不改已有测试（新增测试写在新文件）；不编辑 `CHANGELOG.md`、`README*`、`SECURITY.md`（由我在收尾任务统一写）。
- 事件只放引用、哈希、计数、枚举、时长；值的过滤规则见设计 2.4。
- 类别 K5（功能），涉及公共接口，`architecture: true`，按规则由你审任务书。

### 3.2 接口（T101 定义，T102–T105 依赖，先定死）

`engine/core/events.py`（公共 API）：

```python
def emit(stage: str, step: str, status: str, *, trace_id: str | None = None,
         duration_ms: int | None = None, inputs: list[dict] | None = None,
         outputs: dict | None = None, decision: dict | None = None,
         error: dict | None = None, actor: dict | None = None) -> None: ...
def ref(kind: str, ref: str, *, sha256: str | None = None, size: int | None = None) -> dict: ...
def file_ref(kind: str, path: Path, rev: str | None = None) -> dict: ...   # 读文件算 sha256，路径转仓库相对
@contextmanager
def span(stage: str, step: str, **fixed) -> Iterator[Span]: ...            # 计时；span.outputs / span.status 可在块内设置
def current_trace() -> str: ...                                            # 当前分支名；main 上为 main@<短提交>；CI 取 GITHUB_HEAD_REF / GITHUB_REF_NAME
def enabled() -> bool: ...
```

`stage` ∈ `guard verify dispatch ci route review merge alert`；`status` ∈ `ok fail skip deny error`；不在枚举内的值被拒绝并记入 `redacted`，不抛异常。`actor` 缺省为 `{"role": "engine", "host": "ci" 或 "local"}`。

`engine/core/events_db.py`（存储，不对外承诺接口）：建库与迁移（`PRAGMA user_version`）、带哈希链的写入、`verify_chain(trace_id=None)`、隐私过滤、按 `ts` 清理、查询。库文件 `<git 公共目录>/harness/harness.db`，WAL + `busy_timeout=2000`；表结构见设计 2.2。

### 3.3 任务清单与并行

T001（冒烟）与 **T101 已完成并合并**（T101 派发 23 分钟、一次成功、独立评审通过，全链含 `wait_ci` 走通）。以下按 T101 已定的接口继续；类别一律 K7（触及 `engine/**`、`templates/**`）、风险 R3、`architecture: true` 由用户审任务书。

| 任务 | 内容 | 白名单文件 | 依赖 |
|---|---|---|---|
| **T102** 判定层埋点 | `cli.py` 统一包装：每个子命令一个事件（阶段按命令映射、时长、退出码）；`verify` 每项检查一个事件和一个档位汇总；`integrity` 失败带 `error.kind` | `engine/cli.py`、`engine/checks/verify.py`、`engine/checks/integrity.py`、`tests/test_events_verify.py` | T101 |
| **T103** 路由埋点 | `policy` 每条 `Rule` 一个事件、`route.facts`、`route.result`；`risk` 的判级明细；**显式传 `trace_id`**（CI 里 `workflow_run` 触发的判定拿到的是 `main@…`，应取 PR 分支） | `engine/routing/policy.py`、`engine/routing/risk.py`、`tests/test_events_route.py` | T101 |
| **T104** 守卫埋点 | `command_guard`、`git_guard` 拒绝事件（规则名、角色、工具类别、仓库相对路径；命令全文不记） | `engine/guards/command_guard.py`、`engine/guards/git_guard.py`、`tests/test_events_guard.py` | T101 |
| **T105** 评审与派发埋点 | `review` 结论与发现计数；`dispatch` 各步；执行环节的放行/拒绝计数与事件流哈希（`store_artifact`） | `engine/agents/review.py`、`engine/agents/dispatch.py`、`engine/agents/dispatch_host.py`、`tests/test_events_agents.py` | T101 |
| **T108** CI 检查输出埋点 | hygiene、base_tests、mutation、evidence、run_check、quality、metrics、taskbook 的带内容事件（设计 3.1、3.3） | `engine/checks/hygiene.py`、`base_tests.py`、`mutate.py`、`evidence.py`、`quality.py`、`taskbook.py`、`engine/routing/run_check.py`、`engine/reports/metrics.py`、`tests/test_events_checks.py` | T101 |
| **T109** 事件库加固 | 补 `prev_hash` 与 `seq` 连续性的测试（T101 复核时两个变异未被抓住）；`step` 过滤；`span` 传未知参数不抛异常；引用过滤后不存空字典；产物原子写；`verify` 读库前检查版本 | `engine/core/events.py`、`engine/core/events_db.py`、`tests/test_events_hardening.py`（新增，不改 T101 的测试） | T101 |
| **T106** 开关不影响判定 | 集成测试：事件开启、环境变量关闭、库不可写三种情况下 `verify`、`risk`、`policy`、`guard-command` 的输出与退出码逐字相同 | 新增 `tests/test_events_equivalence.py` | T102–T105、T108 |
| **T107** 文档收尾 | SECURITY.md（事件不进判定、防篡改不防止）、CHANGELOG、README 事件与库位置、`[events]` 配置说明 | `SECURITY.md`、`CHANGELOG.md`、`README.md`、`README.zh-CN.md` | T106 |

并行与冲突：最多同时 3 个槽位。T102、T103、T104 先并行，T105、T108、T109 接着；同一文件的任务串行（P2 的 `dispatch.py`：T201 → T202 → T205）；`cli.py` 命令注册处的冲突是小冲突。每个 PR 合并后其余分支要更新才能满足「分支须最新」的 ruleset。

### 3.4 验收与等价证据（我做，不交给执行方）

每个任务 PR 我逐个复核：运行记录格式与 exit、CI 两项与 `harness`、独立评审结论、diff 只落在白名单内、无判定逻辑改动（`git diff` 里 `decide` / `evaluate` / `run_check` 的返回值路径未变）。

T101–T106 全部合并后：

1. 走 `upgrade` PR 把新引擎装进本仓库的 `.harness/engine`（此后本仓库自己的流程开始产生事件）。
2. 在 Agent-Notification 的独立 worktree 里 `upgrade --allow-dirty`，跑 `bin/verify --full`，对比升级前：Python 测试数 556、质量指标、变异得分不变；`harness.db` 有事件、链校验通过。
3. 手工删改一条事件，`verify_chain` 报错。
4. 记下升级 Agent-Notification 的 PR 是否需要 Migration 条目（预期新增可选 `[events]`，无必填项）。

## 4. 后续各期（任务级清单，前一期合并后按实际代码再修订任务书）

| 期 | 任务 | 关键依赖与注意 |
|---|---|---|
| P2 | **T201** 运行记录增加 `trace_id`、`stages` 时间线、链头锚点（旧记录仍通过）；**T202** `missing_context`（提示词要求执行方报告缺失的上下文，B36）；**T203** `run-check` 内容检查（B38）；**T204** 本机产物按 `[events] artifact_days` 清理（B40）；**T205** 派发侧告警触发（CI 只剩最后一轮、槽位内守卫拒绝突增，升级信息补 `trace_id` 与时间线链接） | T201、T202、T205 同改 `dispatch.py`，串行；T204 可并行 |
| P3 | **T301** 事件导出与本机导入（幂等）；**T302** `events`、`trace` 命令（`--ci`）；**T303** 工作流接入（上传 artifact、summary 渲染，模板与本仓库副本）；**T304** GitHub 侧事件同步（merge、audit_sample、escape）；**T305** 审计账本（写入 `harness-audit` 分支，加 PR 锚点评论，分支 ruleset 模板） | 涉及 `templates/.github/workflows`；本仓库自己 `.github/` 的同步由设计方做；新分支的 ruleset 由用户在平台应用 |
| P4 | **T401** `audit` 复原（取回引用并核对哈希）；**T402** 完整性规则与防篡改（链头对锚点，`[audit]` 配置）；**T403** `alert` 命令与工作流末尾步骤 | T401、T402 同改 `audit.py`，串行 |
| P5 | **T501** 周报事件小节 | 与现有小节数据对账 |
| P6 | **T601** 端到端验收（夹具仓库加假 `gh`，走全链并断言人为缺环节、断链能被发现）；**T602** 文档与迁移（README 命令表、账本与 ruleset 设置、SECURITY、Migration 条目） | 收尾；版本号由用户定 |

**设计方自己做的（不派发）**：每期结束升级本仓库内置副本；同步本仓库 `.github/workflows/`；在 Agent-Notification 的独立 worktree 上做等价验证；平台设置与版本号（用户）。

**共 23 个派发任务**（P1 剩 8：T102–T109，P2 五个、P3 五个、P4 三个、P5 一个、P6 两个）。

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

已定（用户 2026-09-29）：阶段 0 的三项代价可接受；Pi 执行方用 `--model zai-coding-cn/glm-5.3`，评审方沿用 OpenCode（glm-5.3）；接口按 3.2 定死（T101 已实现）；任务书的提交方式与顺序按 [任务拆分流程](../task-splitting.md)。

遗留：独立评审（OpenCode）对「测试是否真的能失败」不敏感，T101 的评审没有发现两个未被抓住的变异（B59）；每个 PR 仍要设计方做变异检查与逐行验收核对。
