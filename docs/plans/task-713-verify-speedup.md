---
task: T713
class: K5
risk: R2
designer: claude-code
size: medium
architecture: true
spec_refs: []
no_spec_reason: 用户 2026-10-05 决定给验证提速（单测并行、pre-push 复用验证结果），无产品规格验收编号
budget:
  wall_clock_min: 150
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T713：验证提速——单元测试按文件并行，pre-push 复用同一代码树的验证结果

负责方：**派发任务**。设计方：claude-code；独立评审方：Codex（单会话）。依据：用户 2026-10-05 决定（「全量验证为什么这么久」之后同意提速，检查强度不降）。

## 病灶（代码证据）

- `bin/verify --full` 的耗时几乎全在 `tests` 一项：50 个测试文件、约 295 个用例，`unittest discover` 单进程顺序执行，单独跑约 4.5 分钟，两个派发槽位同时跑时到 8 分钟（D1 实测 478 秒）。本机 14 核基本闲置。其余检查合计不到 10 秒。
- 一次派发要跑多遍：执行方自己跑（T710 一轮里跑了 14 遍）、派发进程的本地复验（`dispatch.verify_signature`）跑 1 遍、推送时 pre-push 钩子（`engine/guards/git_guard.py` 的 `cmd_pre_push` → `_run_verify`）再跑 1 遍。pre-push 那一遍验证的往往是刚刚复验过的同一份代码。

## 目标终态

1. **并行测试运行器**：新模块 `engine/checks/test_runner.py`，CLI 子命令 `run-tests`（在 `engine/cli.py` 登记）。
   - 用法：`python3 <引擎>/cli.py run-tests -s <测试目录> [-p <文件模式，默认 test*.py>] [-j <并发数，默认 min(CPU 核数, 8)>] [--serial <文件名>]...`。
   - 每个测试文件一个子进程，命令与现在的单进程口径一致：`<当前解释器> -W error::ResourceWarning -m unittest discover -s <测试目录> -p <该文件名>`，工作目录与环境继承调用方（verify 的事件重定向变量因此照常传下去，T710）。
   - `--serial` 列出的文件在并行批次结束后逐个顺序执行，留给确有共享状态、不能并行的测试。
   - 汇总：所有子进程结束后，打印每个失败文件的完整输出，再打印一行合计（文件数、`Ran` 用例总数、失败与出错数、耗时）；任何一个文件失败，退出码为 1，否则为 0。没有发现任何测试文件时退出码为 1（避免空跑当通过）。
   - 只用标准库。
2. **pre-push 复用验证结果**：
   - `verify` 在 `default` 或 `full` 档**全部通过**、且没有已跟踪文件的改动时，在 git 公共目录下的 `harness/verify-pass/<HEAD 的 tree 哈希>.json` 写一条通过记录：`{"tree", "engine_tree", "tier", "checks", "at"}`，其中 `engine_tree` 取 `.harness/engine.lock` 的 `tree` 字段（读不到就不写记录）。
   - `cmd_pre_push` 在既有的保护分支、卫生拒绝都不触发之后，决定是否跑 verify：本次推送的每个本地提交（`local_sha`）的 tree 都有通过记录，当前工作区没有已跟踪文件的改动，且记录里的 `engine_tree` 等于当前锁文件的 `tree` 时，打印一行「同一代码树已通过 verify（tier、时间），跳过重跑」并返回 0；任何一条不满足，照旧跑 verify。
   - 读写记录的任何异常都按「没有记录」处理，回到原来的路径，不抛异常、不改变拒绝逻辑。
   - 只复用 pre-push 的那一遍；`verify` 命令本身、派发的本地复验、CI 都不读这份记录，照常完整执行。
3. **已接受的风险，写进代码注释与 CHANGELOG**：通过记录在本机，执行方理论上能伪造它，让 pre-push 跳过验证。后果只是坏代码被推上去，再由派发复验和 CI（两者都不读记录）拦下；不影响任何判定与合并。
4. CHANGELOG「Unreleased」：`run-tests` 子命令与用法；pre-push 复用规则与上述风险说明；消费方要用并行运行器，需把 `checks.toml` 中 tests 检查的命令改成 `run-tests`（可选，不改则行为不变）。
5. 本仓库 `checks.toml` 改用 `run-tests` 属于 R3 配置，由设计方在升级 PR 中另行提交，不在本任务内。

## 白名单

- `engine/checks/test_runner.py`（新增）
- `engine/cli.py`（只登记 `run-tests`）
- `engine/checks/verify.py`（只加写通过记录）
- `engine/guards/git_guard.py`（只改 `cmd_pre_push` 是否跑 verify 的判断，及其辅助函数）
- `tests/test_test_runner.py`（新增）
- `tests/test_prepush_reuse.py`（新增）
- `CHANGELOG.md`

消费方扫描（检查单第 2 项）：

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| `tests/test_events_guard.py`、`tests/test_events_equivalence.py`（pre-push 相关） | 无需 | 夹具仓库里没有通过记录，路径与现在一致；实现前跑这两个文件确认，有失败就停下报告 |
| `engine/agents/dispatch.py` 的 `verify_signature`、`engine/agents/review_pack.py`（读 `build/verify/summary.json`） | 无需 | `summary.json` 的内容与位置不变；通过记录是另外的文件 |
| `.github/workflows/ci.yml`（直接跑 `unittest discover`） | 无需 | CI 不在本任务内，照旧单进程 |
| 本仓库与模板的 `checks.toml` | 无需（本任务内） | 默认命令不变；本仓库改用 `run-tests` 由设计方随升级 PR 提交 |
| Agent-Notification | 无需迁移 | 新命令可选；pre-push 复用在其仓库同样生效，只少跑一遍 |

## 非目标

- 不改任何测试本身来适配并行（确有冲突的放进 `--serial`；若需要改测试，停下报告）。
- 不改 CI 工作流。
- 不改 `verify` 的检查集合、档位与判定。
- 白名单以外的文件一律不改。
- 共用合同 C0 同样适用。

## 前置条件

- 工作区基于最新的 `origin/main`。按函数名定位，行号只作参考；只有函数、调用点或接口本身对不上时，才停下向设计方报告。
- 实现前先确认本任务书已在 main 上。

## 验收

具名测试为本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：验证提速 | 夹具测试目录里 3 个文件（2 个通过、1 个有失败）：`run-tests` 退出码 1，输出含失败文件的完整输出与合计行，合计 `Ran` 等于三个文件用例数之和；全部通过时退出码 0 | 夹具 | `tests.test_test_runner.TestRunnerTest.test_aggregates_results_and_exit_code` | 失败被吞掉，或计数不对 |
| 不挂规格：验证提速 | `-j 2` 时两个各自阻塞到对方开始才结束的测试文件能同时运行（用标记文件同步，不靠固定 sleep）；`--serial` 的文件不与其他文件重叠执行 | 夹具 | `…test_parallel_and_serial` | 实际仍顺序执行，或 serial 被并行 |
| 不挂规格：验证提速 | 没有任何测试文件时退出码为 1；子进程继承调用方环境（夹具里设一个变量，测试读到它） | 夹具 | `…test_empty_fails_and_env_inherited` | 空跑当通过，或事件重定向丢失 |
| 不挂规格：验证提速 | 在本仓库实测：`run-tests -s tests` 的 `Ran` 总数与 `unittest discover -s tests` 相同，全部通过，连续 3 次结果一致；记录两者耗时 | 人工 | PR 描述 | 并行丢用例或偶发失败 |
| 不挂规格：验证提速 | `verify` 全部通过且工作区干净时写通过记录（tree、engine_tree、tier）；有失败、工作区有改动、`quick` 档、锁文件读不到时都不写 | 夹具 | `tests.test_prepush_reuse.PrepushReuseTest.test_pass_record_written_only_when_clean_pass` | 失败或脏工作区也留下可复用记录 |
| 不挂规格：验证提速 | pre-push：推送的提交 tree 有记录、工作区干净、engine_tree 一致时跳过 verify（桩断言 verify 未被调用）；tree 不同、工作区有改动、engine_tree 不同、记录损坏时都照旧调用 verify | 夹具 | `…test_prepush_skips_only_on_matching_record` | 改了代码仍跳过，或从不跳过 |
| 不挂规格：验证提速 | 保护分支、强制推送、卫生拒绝在有通过记录时照样拒绝（复用只影响是否跑 verify） | 夹具 | `…test_denials_unaffected` | 复用绕过了推送拒绝 |
| 不挂规格：验证提速 | 既有测试全部通过、零修改；质量棘轮不上升 | 夹具 | `bin/verify --full` | 回归 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 并行运行器与 CLI 登记，新增回归测试 | `engine/checks/test_runner.py`、`engine/cli.py`、`tests/test_test_runner.py` | `python3 -W error::ResourceWarning -m unittest tests.test_test_runner -v` | 验收第 1–3 行 |
| 2 | 本仓库实测并行与串行一致 | — | `python3 engine/cli.py run-tests -s tests`（3 次）与 `python3 -m unittest discover -s tests` 对比 | 验收第 4 行 |
| 3 | verify 写通过记录、pre-push 复用，新增回归测试 | `engine/checks/verify.py`、`engine/guards/git_guard.py`、`tests/test_prepush_reuse.py` | `python3 -W error::ResourceWarning -m unittest tests.test_prepush_reuse tests.test_events_guard tests.test_events_equivalence -v` | 验收第 5–7 行 |
| 4 | CHANGELOG；全量验证，整理交付证据 | `CHANGELOG.md` | `bin/verify --full` | 验收第 8 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收、G2 与定向变异复核，以下变异必须各自被对应断言抓住：
- 运行器忽略子进程失败（恒返回 0）；
- 运行器改为顺序执行；
- 没有测试文件时返回 0；
- verify 在有失败时也写通过记录；
- pre-push 不比较 tree（有任意记录就跳过）；
- pre-push 不检查工作区是否干净。

同一失败连续三轮没有新证据时，停止这条路径。不得自行扩大白名单、修改任务书、实现非目标。
