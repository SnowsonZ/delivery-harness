# 可观测性执行计划（B46 实现，路径 A：先自举，再用 Pi 派发）

状态：**草案，待用户审**。依据：[可观测性与审计设计](2026-09-29-observability-design.md)（已审定）。本文把设计落成可执行的任务序列；第 3 节的任务在自举完成后各写成正式任务书（`docs/plans/task-*.md`，走准入检查）再派发，第 4 节各期在前一期合并后再细化。

## 1. 路径与前提

- **路径 A**：先给本仓库自举（B49：装上自己的守卫、`bin/`、CI 判定与派发），再用 `bin/dispatch run <任务书>` 交给 Pi 实现。可观测性用它自己要观测的那套机制开发。
- **执行方只有 Pi**：`dispatch_host.py` 目前只有 `PiHost`。zcode、opencode 作执行方需要先写宿主适配器（B47），不在本计划内；OpenCode 继续做独立评审。
- **分工**：设计、任务书、等价验证、复核由我做；实现由 Pi 做；批准合并与所有平台设置由用户做。
- **提交规则**：我改完只留在工作区；提交、推送、开 PR 逐次由用户确认（含任务书 PR）。Pi 派发出的 PR 由 `bin/dispatch` 以 Agent 账号开出，这是派发流程本身，不需要额外授权，但**派发命令本身每次由用户说了才跑**。

## 2. 阶段 0：自举（B49），我做，约 1 个 PR

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

| 任务 | 类别 | 内容 | 白名单文件 | 依赖 |
|---|---|---|---|---|
| **T101** 事件库 | K5 | `events.py` + `events_db.py`：建库、迁移、哈希链、隐私过滤、追踪 ID、开关、失败吞掉、`verify_chain` | 新增 `engine/core/events.py`、`engine/core/events_db.py`、`tests/test_events.py` | 无 |
| **T102** 判定层埋点 | K5 | `cli.py` 统一包装：每个子命令一个事件（阶段按命令映射、时长、退出码）；`verify` 每项检查一个事件和一个档位汇总；`integrity` 失败带 `error.kind` | `engine/cli.py`、`engine/checks/verify.py`、`engine/checks/integrity.py`、`tests/test_events_verify.py` | T101 |
| **T103** 路由埋点 | K5 | `policy` 每条 `Rule` 一个事件、`route.facts`、`route.result`；`risk` 的判级明细（文件、等级、命中规则名） | `engine/routing/policy.py`、`engine/routing/risk.py`、`tests/test_events_route.py` | T101 |
| **T104** 守卫埋点 | K5 | `command_guard`、`git_guard` 拒绝事件（规则名、角色、工具类别、仓库相对路径；命令全文不记） | `engine/guards/command_guard.py`、`engine/guards/git_guard.py`、`tests/test_events_guard.py` | T101 |
| **T105** 评审与派发埋点 | K5 | `review` 结论与发现计数；`dispatch` 各步（设计 3.2 表）；执行环节的放行/拒绝计数与事件流哈希 | `engine/agents/review.py`、`engine/agents/dispatch.py`、`engine/agents/dispatch_host.py`、`tests/test_events_agents.py` | T101 |
| **T106** 开关不影响判定 | K2 | 集成测试：开启、`HARNESS_EVENTS=off`、库不可写三种情况下 `verify`、`risk`、`policy`、`guard-command` 的输出与退出码逐字相同 | 新增 `tests/test_events_equivalence.py` | T102–T105 |
| **T107** 文档收尾 | K1 | SECURITY.md（事件不进判定、防篡改不防止）、CHANGELOG、README 事件与库位置、`[events]` 配置说明 | `SECURITY.md`、`CHANGELOG.md`、`README.md`、`README.zh-CN.md`、`docs/backlog.md` | T106 |

并行与冲突：T101 先行并合并；T102–T105 白名单互不相交，可同时占 3 个槽位（最多并行 3 个），依次合并——每个 PR 合并后其余分支需要 rebase 才能满足「分支须最新」的 ruleset，冲突面只在各自的测试新文件，预期无冲突。任务书由我一次写完，攒成**一个任务书 PR**（K5 architecture 的由你审）。

### 3.4 验收与等价证据（我做，不交给执行方）

每个任务 PR 我逐个复核：运行记录格式与 exit、CI 两项与 `harness`、独立评审结论、diff 只落在白名单内、无判定逻辑改动（`git diff` 里 `decide` / `evaluate` / `run_check` 的返回值路径未变）。

T101–T106 全部合并后：

1. 走 `upgrade` PR 把新引擎装进本仓库的 `.harness/engine`（此后本仓库自己的流程开始产生事件）。
2. 在 Agent-Notification 的独立 worktree 里 `upgrade --allow-dirty`，跑 `bin/verify --full`，对比升级前：Python 测试数 556、质量指标、变异得分不变；`harness.db` 有事件、链校验通过。
3. 手工删改一条事件，`verify_chain` 报错。
4. 记下升级 Agent-Notification 的 PR 是否需要 Migration 条目（预期新增可选 `[events]`，无必填项）。

## 4. 后续各期（大纲，前一期合并后再细化任务书）

| 期 | 任务 | 关键依赖与注意 |
|---|---|---|
| P2 | 运行记录时间线、链头锚点、`missing_context`、`run-check` 内容检查、本机产物 30 天清理（B36、B38、B40） | 改 `dispatch.py` 与 `run_check.py`；旧记录必须仍通过 |
| P3 | CI 事件导出与 artifact 上传、本机导入、`merge` 事件（合并后 main 上的 `harness` 运行从 GitHub 取回）、审计账本写入 `harness-audit` 分支、`trace`、`events` | 涉及 `.github/workflows` 模板与 ruleset（新分支的保护规则由用户在平台应用）；需要一个真实仓库走通，Agent-Notification 是候选 |
| P4 | `audit` 命令（复原、完整性、防篡改）与 `alert` | 完整性规则按风险与类别配置；告警幂等 |
| P5 | 周报事件小节 | 与现有小节数据对账 |

每期结束都要 `upgrade` 本仓库自己的内置副本（见 2 的代价 1）。P3 及以后涉及模板，装到已接入项目要走 `docs/upgrading.md` 并写 Migration 条目。

## 5. 风险与应对

| 风险 | 应对 |
|---|---|
| Pi 与所选模型完成不了较大的任务 | 任务书把接口、签名、白名单、验收命令写死（第 3.2 节）；一个任务一个 PR；预算内失败按升级包处理，我接手修订任务书而不是放宽范围 |
| 判定被埋点悄悄改动 | T106 的三态逐字相同测试；复核时看 diff；R3 由你批准 |
| 执行方在同一 OS 用户下能改本机库 | 设计已声明：防篡改不防止，B31 才解决；派发不依赖事件做任何决定 |
| 内置副本滞后，自举后短期没有事件可看 | 每期 `upgrade` PR 合并后才生效；P1 的等价证据在 Agent-Notification 上取得 |
| 并行任务合并时 ruleset 要求分支最新 | 白名单不相交；合并一个后其余 rebase（`bin/dispatch` 续跑 `--resume`） |
| zcode / opencode 不能作执行方 | 本计划不含；适配器在 B47，若你要它们也做执行方，另立任务并提前 |

## 6. 需要你定的事

1. **认可阶段 0 的三项代价**（内置副本滞后、`harness` 与 `ci.yml` 重复、不需要批准 App）。
2. **评审方与模型、Pi 执行方的模型**：`[review] reviewer` 与模型写进 `rules.toml`，`bin/dispatch run --model` 指定 Pi 的模型（这些是你的私有选择，不进引擎与模板）。
3. **接口（3.2）是否照此定死**：这是 T102–T105 的共同依赖，改动成本高。
4. **P1 任务书攒成一个 PR，按 K5 architecture 由你审**：同意的话阶段 0 合并后我一次写完。
5. **开始顺序**：先做阶段 0（我改文件，你确认后才提交、推送），你在 0.7 做平台与授权步骤。
