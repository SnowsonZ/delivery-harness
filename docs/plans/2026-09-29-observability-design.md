# 可观测性设计（B46，v0.2）

状态：**草案，待用户审**。审定前不写实现。来源：路线文档第 4 节的设计要点，结合 v0.1 代码现状展开；与要点不同处在第 3 节标明。收 B36、B38、B40。

## 1. 要回答的问题

一个任务或 PR 出了事，能在一处回答：

1. 卡在哪一步、花了多久、每一步为什么这样判？（贯穿全程的时间线）
2. 这个 PR 为什么自动合并了 / 为什么转了人审？（路由理由留存，现在只在 job summary 里，过期即无）
3. 守卫拒绝了什么、是否突增？（现在只有派发链路里的计数）
4. 出了异常，谁在什么时候知道？（现在只有周报）

**不做**：常驻守护进程（已定）；接入 Agent-Notification 收件箱（已定）；OTLP 导出与外部后端（字段按 OTel 命名，导出留口子，v0.2 不做）；用事件做任何放行或拦截判定（见 2.4）。

## 2. 事件模型

### 2.1 契约

一行一个 JSON（JSONL），单条不超过 4 KB（保证 `O_APPEND` 单次写入原子）。

```json
{"ts":"2026-09-30T02:11:04.512Z","trace_id":"task/T005-x","stage":"verify","step":"python-tests",
 "status":"fail","duration_ms":48210,"inputs":{"tier":"full"},"outputs":{"exit_code":1},
 "error":{"kind":"check_failed","signature":"a1b2c3d4"},
 "actor":{"role":"engine","host":"local"},"engine_version":"0.2.0"}
```

| 字段 | 说明 |
|---|---|
| `ts`、`duration_ms` | UTC 毫秒；无时长的瞬时事件省略后者 |
| `trace_id` | 见 2.2 |
| `stage` | `guard` `verify` `dispatch` `ci` `route` `review` `alert` |
| `step` | 阶段内的步骤名（检查名、派发步骤、规则名） |
| `status` | `ok` `fail` `skip` `deny` `error` |
| `inputs`、`outputs` | 扁平字典；键用 OTel 风格（如 `gen_ai.request.model`、`gen_ai.usage.input_tokens`、`harness.risk`） |
| `error` | `kind`（枚举）与 `signature`（失败摘要哈希，沿用派发已有的打转判定口径） |
| `actor` | `role`（designer / implementer / reviewer / engine）、`host`（local / ci）、可选 `model` |
| `engine_version` | 来自锁文件 |

### 2.2 追踪 ID

`trace_id` 取**分支名**（如 `task/T005-x`）；尝试序号、head 提交放 `inputs`（`attempt`、`head`）。

与路线要点的区别：要点写「dispatch 生成，写进 PR 正文，CI、review、policy 读回」。改为分支名的理由：

- 分支名是五处（本机钩子、dispatch、CI 的 `head_branch`、review 的 `headRefName`、policy）天然共有的键，不需要任何一处读回另一处写的东西。
- PR 正文由 Agent 可写，靠它传递 ID 既脆弱又不可信；设计方自己开的、没经过 dispatch 的 PR 也自然有 ID。
- 代价：分支被删除后同名重建会并入同一条时间线。用 `attempt` 与 `ts` 区分，可接受。

主分支上的事件（推送后的 `harness` 运行、周报）用 `main@<短提交>`。

### 2.3 隐私

只存管理所需的元数据：ID、哈希、计数、枚举、时长。`emit()` 在写入前强制过滤：

- 值只允许数字、布尔、≤ 120 字符的字符串；更长的丢弃。
- 拒绝形似本机路径（`/Users/`、`/home/`、`C:\`）、含换行、含空白分隔的长文本的值；被丢的键计数写入事件的 `redacted`。
- 命令全文、会话正文、diff 内容不进事件。需要复盘时看本机完整事件流（派发已有，不入库）。

### 2.4 事件不是证据

执行方与主人是同一个 OS 用户，本机事件文件执行方能改能删。所以：**事件只用于展示、统计、告警，任何放行、拦截、路由判定都不读事件**。CI 侧事件由 main 上的工作流产生，比本机事件可信，但同样不进判定。这条写进 SECURITY.md，并加一条测试：事件写入失败或被关闭时，所有判定结果与开启时逐字相同。

## 3. 埋点

`emit()` 只包住**已有的结构化结果**，不改判定逻辑。新增 `engine/core/events.py`（约 150 行，标准库）。写入失败一律吞掉并向 stderr 提示一次，永不影响退出码；`HARNESS_EVENTS=off` 关闭。

| 阶段 | 事件 | 来源（现有结构） |
|---|---|---|
| `guard` | 拒绝：命中的规则名、角色；**放行不记**（钩子每次工具调用都会跑，放行事件量大且无诊断价值） | `command_guard.evaluate`、`git_guard` 三个钩子 |
| `verify` | 每项检查一个事件（状态、时长）+ 一个汇总事件（档位、通过与否）；integrity 失败带 `error.kind = integrity` | `verify.Result` |
| `dispatch` | 准入、认领、取槽位、守卫预检、每轮执行（时长、退出、用量、守卫拒绝数）、每轮本地判定、推送与开 PR、每轮等 CI、升级 | `dispatch.py` 各步 |
| `ci` | 风险等级、base-tests、run-check、evidence、卫生各一个事件 | 各命令的结果对象 |
| `route` | 每条路由规则一个事件（规则名、是否满足、理由）+ 最终 `auto_merge` | `policy.Rule` |
| `review` | 结论、发现按严重度计数、评审方与模型、时长；评审方自身失败标 `error` | `review.Verdict` |
| `alert` | 见第 5 节 | — |

运行记录（`docs/runs/`，入库）保留为持久摘要，增加：`trace_id`、`stages`（各阶段时长与结果的时间线）、`missing_context`（B36：派发提示词要求执行方结束时报告缺失的上下文或工具，写进记录）。`run-check` 增加内容检查（B38）：记录里出现命令全文、本机路径、会话正文即失败，复用 2.3 的过滤规则。

## 4. 存放与查询

**本机**：`<git 公共目录>/harness/events/<日期>.jsonl`（所有 worktree 共用，与派发的完整事件流同处）。保留 30 天（B40）：没有守护进程，清理在每天第一次 `emit()` 时顺带做。

**CI**：事件写到 `build/events/`，`if: always()` 上传为 artifact `harness-events`（保留 30 天），同时把时间线渲染进 job summary。CI 事件与本机事件是两份视图：`trace` 用 `--ci` 经 `gh run download` 合并；不合并时也各自完整。

**查询**：

```
bin/harness trace <任务编号 | PR 号 | 分支> [--ci] [--json]   按时间排序的时间线，标出耗时最长的步骤与首个失败
bin/harness events [--since 1d] [--stage guard] [--status deny]  过滤与计数
```

**周报**：v0.2 首版不动数据来源（现在的 `gh` 抓取已稳定）；事件稳定运行一段后再加「事件汇总」小节（阶段耗时分布、守卫拒绝、升级原因）。

已知盲区：设计方在本机被守卫拒绝的事件只在本机，CI 与周报看不到。这是 2.4 的自然后果，不为此引入上传通道。

## 5. 告警

不引入常驻进程，也不新增通知渠道：告警就是在 PR（无 PR 时在议题）上加评论并贴 `escalation` 标签，靠 GitHub 通知送达（已定）。在 CI 的 `harness` job 末尾与 `auto-merge` 判定后、`if: always()` 运行 `bin/harness alert`；同一 trace 同一原因只评论一次（评论带标记，重复运行更新而不新增）。

| 触发 | 现状 |
|---|---|
| 完整性检查失败 | 只在 CI 日志 |
| 评审结论不通过，或评审方自身失败 | 有评论，无标签 |
| 派发卡死、超预算、打转 | 已升级，补 `trace_id` 与时间线链接 |
| CI 轮次只剩最后一轮 | 无 |
| 槽位内守卫拒绝数突增（由 dispatch 判，阈值在 `rules.toml`） | 只记数 |
| 路由因误差预算耗尽而拒绝自动合并 | 只在 job summary |

## 6. 分期与验收

| 期 | 内容 | 验收 |
|---|---|---|
| P1 | `events.py`、追踪 ID、verify / guard / route / review / dispatch 埋点、本机写入 | 判定在事件开启、关闭、写入失败三种下逐字相同（测试）；隐私过滤有针对每类禁项的断言；Agent-Notification 上 `verify --full` 测试数、质量指标、变异得分不变 |
| P2 | 运行记录时间线与 `missing_context`、`run-check` 内容检查、30 天清理（B36、B38、B40） | 记录含禁项时 run-check 失败；旧记录不受影响 |
| P3 | CI 上传 artifact 与 summary 渲染、`trace`、`events`、`alert` | 在真实仓库走通一条从派发到合并的链路，`trace` 的时间线与 GitHub 上的实际一致；告警幂等 |
| P4 | 周报事件小节 | 与现有小节数据一致处对账 |

每期一个 PR，R3。P1 引入 `[events]` 可选配置（开关、保留天数）；无迁移项。开销预算：`verify` 增加的耗时可忽略（每检查一次 append）；守卫钩子只在拒绝时写，不增加放行路径的延迟。

## 7. 需要你定的事

1. **追踪 ID 用分支名**（2.2，与路线要点不同）。推荐：同意。
2. **CI 事件存 artifact（30 天）+ summary**，不放 PR 评论。推荐：同意；评论只留告警。
3. **守卫放行不记录**，只记拒绝。推荐：同意。
4. **事件不进任何判定**（2.4）。推荐：作为不可变更的原则写进 SECURITY.md。
5. **告警渠道只用 PR / 议题评论 + `escalation` 标签**，不接其他通道。推荐：同意，需要时再加。
6. **OTLP 导出不在 v0.2**，字段已按 OTel 命名，留待有后端需求时加。推荐：同意。
