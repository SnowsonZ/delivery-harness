---
task: T101
class: K7
risk: R3
designer: claude-code
size: large
architecture: true
spec_refs: []
no_spec_reason: 设计文档 docs/plans/2026-09-29-observability-design.md 是本任务的规格（可观测性 B46 的 P1 首个任务），本仓库没有产品规格验收编号
budget:
  wall_clock_min: 120
  ci_rounds: 3
  retries: 2
  tokens: null
rollback: git revert
---

# 任务：事件库（events.py 与 events_db.py）

来源：可观测性设计（docs/plans/2026-09-29-observability-design.md）与执行计划（docs/plans/2026-09-29-observability-execution-plan.md）的 T101。这是后续埋点任务（T102–T108）共同依赖的基础库；接口在本任务书里定死，后续任务按它调用，**不得自行增删或改名**。

## 目标终态

新增两个模块与一个测试文件，引擎其余部分一行不改：

- `engine/core/events_db.py`：SQLite 存储层（建库与迁移、带哈希链的写入、链校验、产物与锚点）。
- `engine/core/events.py`：公共 API（下面「接口」一节逐个函数写死）。
- `tests/test_events.py`：覆盖下面「验收」全部行为。
- `templates/.harness/config/checks.toml` 增加**注释掉的** `[events]` 配置说明（`enabled`）。

只依赖标准库（Python ≥ 3.11）。库文件位于 `<git 公共目录>/harness/harness.db`，产物文件位于 `<git 公共目录>/harness/artifacts/<sha256>`，都在 `.git/` 内，不会被跟踪。

## 接口（`engine/core/events.py`，后续任务的调用契约）

```python
STAGES = ("guard", "verify", "dispatch", "ci", "route", "review", "merge", "alert")
STATUSES = ("ok", "fail", "skip", "deny", "error")

def enabled() -> bool: ...
    # 环境变量 HARNESS_EVENTS=off 或 checks.toml [events] enabled = false 时为 False；环境变量优先。

def current_trace() -> str: ...
    # 当前分支名；detached HEAD 为 "HEAD@<7 位提交>"；在默认分支（main）上为 "main@<7 位提交>"；
    # CI 中优先取环境变量 GITHUB_HEAD_REF（PR），其次 GITHUB_REF_NAME（若为 main 则同样写成 "main@<7 位提交>"）。

def ref(kind: str, ref: str, *, sha256: str | None = None, size: int | None = None) -> dict: ...
    # 返回 {"kind":…, "ref":…, "sha256":…, "size":…}（值为 None 的键省略）。

def file_ref(kind: str, path: Path, rev: str | None = None) -> dict: ...
    # 读文件算 sha256 与大小；ref 为仓库相对路径（POSIX 形式），rev 非空时写成 "<路径>@<rev>"。

def store_artifact(data: bytes | Path) -> dict: ...
    # 内容寻址保存到 artifacts/<sha256>（已存在则不重复写），在 artifacts 表登记，返回
    # {"kind":"artifact","ref":<sha256>,"sha256":<sha256>,"size":<字节数>}。

def emit(stage: str, step: str, status: str, *, trace_id: str | None = None, duration_ms: int | None = None,
         inputs: list[dict] | None = None, outputs: dict | None = None, decision: dict | None = None,
         error: dict | None = None, actor: dict | None = None, source: str | None = None) -> int | None: ...
    # 写一个事件，返回事件 id；关闭、失败、被过滤掉整条事件时返回 None。永不抛异常。
    # trace_id 缺省 current_trace()；source 缺省：环境变量 CI == "true" 时 "ci"，否则 "local"。
    # actor 缺省 {"role":"engine","host":<source>}；role ∈ designer / implementer / reviewer / approver / engine。
    # decision 形如 {"by":…, "rule":…, "reason":…}；error 形如 {"kind":…, "signature":…}。

@contextmanager
def span(stage: str, step: str, **fixed) -> Iterator[Span]: ...
    # 计时；进入时记 monotonic，退出时 emit 一个事件。Span 上可设置：status（默认 "ok"）、inputs、outputs、
    # decision、error。块内抛异常：status 设为 "error"、error={"kind":异常类型名}，事件写入后**重新抛出**原异常。
    # fixed 里的键（trace_id、actor、source 等）原样传给 emit。

def set_anchor(trace_id: str, stage: str, head_hash: str, fixed_in: str, source: str | None = None) -> None: ...
    # 在 anchors 表登记一条链头锚点；fixed_in ∈ "run_record" / "ci_artifact" / "pr_comment"。永不抛异常。

def chain_head(trace_id: str, source: str | None = None) -> str | None: ...
    # 该 (source, trace) 最后一个事件的 hash；没有事件为 None。

def verify_chain(trace_id: str | None = None, source: str | None = None) -> list[str]: ...
    # 校验哈希链，返回问题描述列表，空列表表示完好；trace_id、source 为 None 时校验全部。
    # 问题包括：事件内容被改（重算 hash 不符）、中间事件被删（seq 不连续）、prev_hash 与前一事件 hash 不符。
```

## 存储（`engine/core/events_db.py`）

- 库位置：`git rev-parse --git-common-dir`（在引擎的项目根 `ROOT` 下执行）得到的目录下 `harness/harness.db`，所有 worktree 共用。不在 git 仓库里时 `emit` 等一律不写、不报错。
- SQLite 设置：WAL、`busy_timeout=5000`；`PRAGMA user_version = 1`。**每次操作用 `contextlib.closing` 关闭连接**（测试以 `-W error::ResourceWarning` 运行，未关闭的连接会使测试失败）。
- 表（列名照此，不增减）：
  - `events`：`id INTEGER PRIMARY KEY`、`ts TEXT`（UTC 毫秒 ISO8601）、`source TEXT`、`trace_id TEXT`、`seq INTEGER`、`prev_hash TEXT`、`hash TEXT`、`stage TEXT`、`step TEXT`、`status TEXT`、`duration_ms INTEGER`、`actor_role TEXT`、`actor_host TEXT`、`model TEXT`、`decision_by TEXT`、`decision_rule TEXT`、`decision_reason TEXT`、`error_kind TEXT`、`error_signature TEXT`、`outputs TEXT`（JSON）、`engine_version TEXT`、`redacted INTEGER`；`(source, trace_id, seq)` 唯一。
  - `refs`：`event_id INTEGER`、`direction TEXT`（`in` / `out`）、`kind TEXT`、`ref TEXT`、`sha256 TEXT`、`size INTEGER`。
  - `artifacts`：`sha256 TEXT PRIMARY KEY`、`size INTEGER`、`path TEXT`、`created TEXT`。
  - `anchors`：`id INTEGER PRIMARY KEY`、`source TEXT`、`trace_id TEXT`、`stage TEXT`、`head_hash TEXT`、`fixed_in TEXT`、`ts TEXT`。
- 哈希链：`hash` = sha256(规范化 JSON)，规范化 JSON 指下列字段（除 `id`、`hash` 外的全部列，加上 `inputs` 引用列表）按键排序、`separators=(",",":")`、`ensure_ascii=False` 序列化；第一个事件的 `prev_hash` 为空字符串 `""`。`seq` 从 1 起，按 `(source, trace_id)` 各自递增。**分配 `seq`、读取 `prev_hash`、插入必须在同一个 `BEGIN IMMEDIATE` 事务里**，多进程并发写同一条 trace 时链仍然完好。
- 迁移只增不改（此版本只有 v1）：库的 `user_version` **大于**本代码已知版本时，本进程不写入、不报错、不修改库（较新引擎写的库，旧引擎不得破坏）；等于则正常使用；为 0（新库）则建表并置 1。

## 隐私过滤（`emit` 写入前强制）

对 `outputs`、`decision`、`error`、`actor`、`inputs` 里的值：

- 只允许 `int`、`float`、`bool`、`None`、`str`；`str` 长度 ≤ 120（`decision.reason` ≤ 200，`ref` 字段 ≤ 200）。
- 拒绝形似本机路径的字符串：以 `/Users/`、`/home/` 开头，或含 `C:\`；拒绝含换行的字符串。
- `outputs` 必须是扁平字典，键为 `[A-Za-z0-9_.-]{1,64}`；嵌套的值、不合规的键被丢弃。
- 被丢弃的键或值累计写入该事件的 `redacted`（整数）；`stage`、`status` 不在枚举内、`step` 为空时，**整条事件不写**（`emit` 返回 None）。
- 过滤只丢弃、不改写、不截断。

## 非目标与禁止动作

- 本任务不做：任何埋点（不改 `verify.py`、`policy.py`、守卫、`dispatch.py`、`review.py`、`cli.py` 等既有引擎文件）；`trace`、`events`、`audit` 等命令；CI 导出与导入；文档（README、SECURITY、CHANGELOG 由后续任务写）。
- 禁止：改动已有测试；编辑 `.github/`、`.harness/`、`docs/plans/` 下的文件；引入第三方依赖；改写已推送历史；推 tag。
- 事件功能失败不得影响调用方：`emit`、`set_anchor` 永不抛异常（包括库被锁、目录不可写、磁盘满、JSON 序列化失败）；失败时向 stderr 打一行提示，**同一进程内只提示一次**。

## 前置条件（不满足就停下报告，不要硬做）

- `engine/core/common.py` 提供 `ROOT`、`git()`、`setting()`；`engine/__init__.py` 有 `__version__`。
- 仓库里还不存在 `engine/core/events.py`、`engine/core/events_db.py`、`tests/test_events.py`。

## 验收

每行都在 `tests/test_events.py` 的 `EventsTest` 里有对应测试，测试用临时 git 仓库（`git init`）与临时目录，不碰真实仓库的库。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或验证步骤） |
|---|---|---|---|
| 不挂规格：可观测性设计 2.2 | 新库建出 `events`、`refs`、`artifacts`、`anchors` 四张表，列名与本任务书一致（含 `source`），`user_version` 为 1 | 单测 | `test_events.EventsTest.test_schema_has_four_tables_and_source_column` |
| 不挂规格：可观测性设计 2.2 | `emit` 写入事件与其输入引用，返回 id；`seq` 从 1 递增；`prev_hash` 与前一事件的 `hash` 相接，首个为空串 | 单测 | `test_events.EventsTest.test_emit_writes_event_with_refs_and_hash_chain` |
| 不挂规格：可观测性设计 2.5 | 直接改库里一个事件的内容、删除中间事件、交换两个事件的 `seq`，`verify_chain` 各自报出问题；未改动时返回空列表 | 单测 | `test_events.EventsTest.test_verify_chain_detects_edit_delete_and_reorder` |
| 不挂规格：可观测性设计 2.5 | 链按 `(source, trace_id)` 各自独立：同一 trace 的 local 与 ci 事件各有自己的 seq 与链；`chain_head` 分来源返回 | 单测 | `test_events.EventsTest.test_chains_are_per_source_and_trace` |
| 不挂规格：可观测性设计 2.4 | 隐私过滤：超长字符串、`/Users/…`、`/home/…`、`C:\…`、含换行的值、嵌套值、非法键都被丢弃，`redacted` 计数正确，合规的值原样保留 | 单测 | `test_events.EventsTest.test_privacy_filter_drops_forbidden_values_and_counts_them` |
| 不挂规格：可观测性设计 2.2 | `stage` 或 `status` 不在枚举内、`step` 为空时 `emit` 返回 None 且不写库、不抛异常 | 单测 | `test_events.EventsTest.test_unknown_stage_or_status_is_rejected_without_raising` |
| 不挂规格：可观测性设计 3 | `HARNESS_EVENTS=off` 时 `emit` 返回 None，不创建库文件；`checks.toml` 的 `[events] enabled = false` 同样关闭，环境变量优先 | 单测 | `test_events.EventsTest.test_off_switch_writes_nothing`、`test_events.EventsTest.test_config_can_disable_events` |
| 不挂规格：可观测性设计 3 | 库目录不可写、库文件损坏时 `emit` 返回 None、不抛异常、stderr 只提示一次 | 单测 | `test_events.EventsTest.test_write_failure_is_swallowed_and_reported_once` |
| 不挂规格：可观测性设计 2.3 | `current_trace`：普通分支返回分支名；detached HEAD 返回 `HEAD@<7 位>`；在 main 上返回 `main@<7 位>`；CI 环境变量 `GITHUB_HEAD_REF`、`GITHUB_REF_NAME` 的取值规则如上 | 单测 | `test_events.EventsTest.test_current_trace_follows_branch_detached_main_and_ci` |
| 不挂规格：可观测性设计 3 | `span` 记录时长与 outputs；块内抛异常时事件 status 为 error、异常被重新抛出 | 单测 | `test_events.EventsTest.test_span_times_and_records_errors_then_reraises` |
| 不挂规格：可观测性设计 2.4 | `file_ref` 给出仓库相对路径、sha256、大小，带 rev 时为 `<路径>@<rev>`；`ref` 省略为 None 的键 | 单测 | `test_events.EventsTest.test_file_ref_and_ref_shapes` |
| 不挂规格：可观测性设计 2.4 | `store_artifact` 内容寻址、重复保存不重复写、登记 `artifacts` 表、返回引用；对 bytes 与 Path 都可用 | 单测 | `test_events.EventsTest.test_store_artifact_is_content_addressed_and_idempotent` |
| 不挂规格：可观测性设计 2.5 | 4 个进程并发各写 25 个事件到同一 trace，共 100 个事件、`seq` 连续、`verify_chain` 为空 | 单测 | `test_events.EventsTest.test_concurrent_processes_keep_a_valid_chain` |
| 不挂规格：可观测性设计 2.2 | 库的 `user_version` 大于已知版本时，`emit` 不写、不报错、库内容不变 | 单测 | `test_events.EventsTest.test_newer_database_version_is_left_untouched` |
| 不挂规格：可观测性设计 2.2 | 库在 git 公共目录下；同一仓库的两个 worktree 写入同一个库 | 单测 | `test_events.EventsTest.test_database_lives_in_git_common_dir_and_is_shared_by_worktrees` |
| 不挂规格：可观测性设计 2.5 | `set_anchor` 登记锚点，`chain_head` 与之可比对 | 单测 | `test_events.EventsTest.test_anchor_records_chain_head` |
| 不挂规格：可观测性设计 2.2 | 两个新模块只 import 标准库与 `engine.*`（用 `ast` 与 `sys.stdlib_module_names` 检查） | 单测 | `test_events.EventsTest.test_modules_import_only_stdlib_and_engine` |
| 不挂规格：可观测性设计 3 | 模板 `checks.toml` 仍是合法 TOML，且含被注释的 `[events]` 与 `enabled` 说明 | 单测 | `test_events.EventsTest.test_checks_template_documents_events_section` |

## 步骤与提交顺序

一次只做一件事，每步一个提交。

| # | 改动 | 涉及文件 | 验证方式（命令或测试） | 对应验收 |
|---|---|---|---|---|
| 1 | 存储层：建库与迁移、带哈希链的事务写入、`verify_chain`、产物与锚点、版本保护，含对应测试 | `engine/core/events_db.py`、`tests/test_events.py` | `python3 -W error::ResourceWarning -m unittest tests.test_events -v` | 表结构、哈希链、链校验、分来源、并发、新版本库、位置、锚点、产物 |
| 2 | 公共 API：`enabled`、`current_trace`、`ref`、`file_ref`、`store_artifact`、`emit`、`span`、`set_anchor`、`chain_head`、`verify_chain`，隐私过滤、开关、失败吞掉，含对应测试 | `engine/core/events.py`、`tests/test_events.py` | 同上 | 隐私、枚举、开关、失败、trace、span、引用、纯标准库 |
| 3 | 模板说明 | `templates/.harness/config/checks.toml`、`tests/test_events.py` | 同上，另跑 `python3 -m ruff check engine tests` | 模板 |

预算见头部 `budget`：超出即停止，把升级包交给评审方；同一失败的每次重试必须带新的信息（报错、失败测试、评审意见）。

## 参考

- 设计：docs/plans/2026-09-29-observability-design.md 的第 2 节（模型、追踪 ID、隐私、可信度）与第 3 节。
- 已有的写法参考：`engine/core/common.py`（`ROOT`、`git`、`setting`）；`tests/test_install.py` 里用临时 git 仓库与 `subprocess` 的测试风格。

## 交付要求

- 提交前 `bin/verify` 通过；改动测试或产品逻辑后跑 `bin/verify --full`。
- 测试要能失败：验收里的每个行为，在没有对应实现时测试必须失败，不写恒真断言。
- 新增测试写在 `tests/test_events.py`，**不改已有测试**；测试代码遵守 ruff 配置（`ruff.toml`，行宽 120）。
- PR 正文、评论先写进 `build/` 下的文件，再用 `--body-file` 传入。
- PR 附当前 head 的 CI 链接；「已修复」「测试通过」由 CI 的 harness job 生成，不手写。
- 卡住时按下方格式升级，不要无限重试。

## 升级包（卡住或需要澄清时填写）

验收无法判定、前置条件不满足、存在多个合理方案时停下提交，不自行扩大或缩小范围。

- 当前状态与目标差距：
- 已尝试的方案与结果：
- 证据（失败日志、测试输出、截图）：
- 可选方案与推荐：
- 需要评审方或用户决定的具体问题：
