---
task: T126
class: K7
risk: R3
designer: codex
size: small
architecture: true
spec_refs: []
no_spec_reason: 待办 B73 的 macOS CI flake 根治（诊断全覆盖 + 夹具加固），无产品规格验收编号
budget:
  wall_clock_min: 90
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T126：macOS dispatch 夹具 flake 根治（B73）

负责方：**派发任务**。设计方：codex 席位（现由 Zcode 代行）；独立评审方 OpenCode。依据：待办 B73（第 8 例实测：#91 终评被 macOS 独有的 `test_last_ci_round_warns_before_wait` 阻断，`waits==0` 即 dispatch 在首次 CI 等待前异常退出；同族已咬 #37/#38/#58/#61/#67/#87/#91，频率约每 PR 20%）；[共用合同](2026-09-29-observability-task-contracts.md)C0。先读 `tests/test_events_agents.py`（#59 的诊断断言先例）、`tests/test_dispatch_alerts.py`、`tests/test_run_timeline.py` 的 `run_dispatch`/`executor_script` 夹具与 `engine/agents/dispatch_host.py` 的 `run_monitored`（stderr 已并入事件流文件）。

## 目标终态

1. **诊断全覆盖**：三个测试文件的 `run_dispatch` 夹具统一在断言失败时携带 dispatch 的 stdout/stderr 与执行方事件流尾部（最后 30 行——`run_monitored` 已把子进程 stderr 写进流文件，读出来即是死因），模式沿用 #59 在 `dispatch_once` 的先例；失败现场不再沉默。
2. **夹具加固**：`executor_script` 的 git 操作（add/commit）对瞬时失败做 3 次退避重试（0.1/0.3/0.9 秒，子进程级），并在最终失败时把 stderr 写入流后再退出非零——macOS runner 的偶发 git 瞬断（index.lock 等）不再直接杀执行方。
3. **不改产品代码**：`engine/` 零改动（本任务纯测试基建）；既有断言语义零变化。
4. 回归测试为既有测试文件的夹具增强（无新文件；断言只增失败信息不改判定）。

实现前先核对三个夹具现状与流文件路径的获取方式，差异停下报告设计方，不自行补实现。

## 白名单

- `tests/test_events_agents.py`（run_dispatch 诊断增强 + executor_script 重试）
- `tests/test_dispatch_alerts.py`（同上）
- `tests/test_run_timeline.py`（同上）

## 非目标

不实现其他任务；不改 `engine/` 任何产品代码；不改 CI 工作流；不放宽原判定、预算、准入或守卫；不引入第三方依赖。共用合同C0同样适用。

## 前置条件

- 工作区基于含依赖的最新main。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- 纯测试基建：G2 免（无产品行为变化）。
- **验收的最终裁判是后续 PR 的 CI 稳定性**：本任务合并后 10 张 PR 内 macOS 零复发（或复发时日志含执行方 stderr 死因）由设计方在待办 B73 记录归档。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；差异先修订任务书并经原审批。

## 验收

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B73 | 变异执行方脚本恒退出 1 且写 stderr：三个文件的 dispatch 断言消息均含流尾部与 stderr 死因（30 行内） | 夹具 | 各文件新增 `test_dispatch_failure_diagnostics_visible`（或同名具名断言） | 死因不可见时失败 |
| 不挂规格：B73 | executor_script 的 git 瞬断（前两次失败第三次成功）被重试吞掉：dispatch 正常完成 | 夹具 | `tests.test_events_agents` 新增 `test_executor_script_retries_transient_git` | 无重试时夹具 dispatch 失败 |
| 不挂规格：B73 | 既有全部测试逐字通过（断言语义零变化） | 夹具 | `bin/verify --full` | 任何既有断言判定变化时失败 |

## 步骤与提交顺序

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 三夹具诊断与重试、两条新断言 | 白名单三文件 | `python3 -W error::ResourceWarning -m unittest tests.test_events_agents tests.test_dispatch_alerts tests.test_run_timeline -v` | 验收 1–2 行 |
| 2 | 验证范围 | `tests/` | `bin/verify --full` | 验收 3 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收与定向变异复核（去掉诊断增强→验收 1 失败；去掉重试→验收 2 失败）。G2 免。合并后 B73 转入观察期（10 PR 零复发即关闭；复发则凭新诊断日志定位真因再修）。同一失败连续三轮无新证据时停止该路径；不得自行扩大白名单、改原任务书。
