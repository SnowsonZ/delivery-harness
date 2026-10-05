---
task: T710
class: K7
risk: R3
designer: claude-code
size: medium
architecture: true
spec_refs: []
no_spec_reason: 待办 B92 第 2 部分（verify 跑项目检查时，测试事件不进真实事件库），无产品规格验收编号
budget:
  wall_clock_min: 120
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T710：verify 跑项目检查时把事件导到临时库（B92 第 2 部分）

负责方：**派发任务**。设计方：claude-code；独立评审方：Codex（单会话）。依据：待办 B92；用户 2026-10-05 的决定（B92 分两部分，互不依赖，可以并行）。

## 病灶（代码证据）

- `engine/core/events_db.py` 的 `common_dir()` 按 `ROOT` 解析 git 公共目录，`db_path()` 与 `artifacts_dir()` 都放在它下面的 `harness/` 里。所有 worktree（包括派发槽位）共用同一个事件库。
- `engine/checks/verify.py` 用 `subprocess.run` 启动项目检查（单测、lint，以及消费方的 swift 等），子进程照常写真实事件库。单测调用的引擎函数会写事件，追踪 ID 取当前分支名；在派发槽位里，这个分支名就是任务的追踪 ID。
- 后果：本机事件库、`bin/harness trace`、`audit` 都混进了测试事件。T703 的一条追踪链上有 1409 条测试事件，真正的派发事件只有 15 条。CI 的 harness 任务同样在 verify 里跑测试，导出的事件包也混着测试事件。
- 不能简单关掉事件：设计方实验，把事件默认关闭后全量跑测试，结果 **74 个失败、15 个报错**，可观测性那批测试依赖事件默认开启。守卫也禁止 Agent 在命令行设置 `HARNESS_*` 变量。

## 目标终态

1. **verify 给项目检查的子进程指定临时事件库**：
   - 每次 verify 运行时，新建一个临时目录（运行结束后删除）；
   - 启动**项目检查**（`check.func is None` 的那类）时，在子进程环境里设置引擎内部变量 `HARNESS_EVENTS_REDIRECT`，值为 JSON：`{"dir": "<临时目录>", "for_common_dir": "<当前仓库的 git 公共目录绝对路径>"}`；
   - verify 在进程内运行的**引擎检查**（`check.func` 不为空的，如 hygiene、integrity）不受影响，照常写真实事件库。
2. **`events_db` 只对指定的那个仓库生效**：
   - `common_dir()` 的语义不变。新增内部函数 `_harness_dir()`，`db_path()` 与 `artifacts_dir()` 都改为基于它。
   - `_harness_dir()` 的规则：环境中有 `HARNESS_EVENTS_REDIRECT`，且能解析出 JSON，且当前解析到的 `common_dir()` 与 `for_common_dir` **完全相同**，就返回 `<dir>/harness`；其余一切情况（变量不存在、解析失败、公共目录不同）都返回原来的 `<common_dir>/harness`。
   - 由此可以保证：
     - 真实仓库里的测试，没有 patch 事件库位置，事件写到临时库；
     - patch 了 `events_db.ROOT`（或 `common.ROOT`）、指向夹具仓库的测试，公共目录不同，照旧用夹具库；
     - 夹具仓库里再启动的 verify 或 CLI 子进程（如事件等价测试），公共目录不同，照旧写夹具库。
3. 解析失败时不抛异常，按原路径处理，并且不影响任何判定（事件只是观察）。
4. 不新增配置项、不改配置格式；消费方不需要迁移。

## 白名单

- `engine/core/events_db.py`
- `engine/checks/verify.py`
- `tests/test_events_test_isolation.py`（新增）
- `tests/test_install.py`（修订记录 1：只改 `env()`，清洗环境时保留 `HARNESS_EVENTS_REDIRECT`）

消费方扫描（检查单第 2 项）：

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| 直接 patch `events_db.ROOT` 的测试（`test_alert_cli`、`test_events_*`、`test_audit_*`、`test_harness_contract_dispatch`、`test_review_after_ci` 等） | 无需 | 公共目录不同，规则 2 不生效 |
| 在夹具里再起 verify/CLI 子进程并断言夹具事件库的测试（`test_events_verify`、`test_events_equivalence` 等） | 无需 | 子进程的公共目录是夹具的，规则 2 不生效。**这是本任务的关键风险，实现后必须全量实测**；有失败就停下报告，不改测试 |
| 只调用 `events_db.db_path()` 读写、不 patch 路径的测试 | 实现前核对 | 写和读都经过同一个函数，路径一致；有断言默认路径位于 `.git/harness` 下的，停下报告 |
| `engine/agents/run_timeline.py` 读事件库组装记录 | 无需 | 派发进程不在项目检查子进程里，读的是真实库 |
| CI 的 harness 任务、事件导出 | 无需 | 测试事件不再进入导出包，这是本任务要达到的效果 |
| Agent-Notification | 无需迁移 | 没有配置变化；它的项目检查（python-tests、swift 等）同样不再污染真实库 |

CHANGELOG 由设计方在 D1 统一写入，本任务不改。

## 非目标

- 不改运行记录的组装（B92 第 1 部分，T709）。
- 不改事件库的 schema 与事件内容。
- 不清理历史事件库（30 天保留期会自然清掉）。
- 不新增配置项。
- 白名单以外的文件一律不改。
- 共用合同 C0 同样适用。

## 前置条件

- 工作区基于最新的 `origin/main`。按函数名定位，行号只作参考；只有函数、调用点或接口本身对不上时，才停下向设计方报告。
- 与 T709 的白名单不交叉，可以并行。

## 验收

具名测试为本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B92 | 设置了匹配当前公共目录的 `HARNESS_EVENTS_REDIRECT` 时，`db_path()`、`artifacts_dir()` 都落在临时目录下；`emit` 写入的事件不出现在真实库中 | 夹具 | `tests.test_events_test_isolation.TestEventsIsolation.test_redirect_applies_to_matching_repo` | 测试事件进了真实库 |
| 不挂规格：B92 | `for_common_dir` 与当前公共目录不同（patch ROOT 指向夹具，或在夹具仓库里起子进程）时，路径与原来完全一致 | 夹具 | `…test_redirect_ignored_for_other_repo` | 夹具测试被导走，断言读不到事件 |
| 不挂规格：B92 | 变量值不是合法 JSON、缺字段、类型不对时，按原路径处理且不抛异常 | 夹具 | `…test_bad_redirect_falls_back` | 观察旁路影响了主流程 |
| 不挂规格：B92 | 用一个夹具项目跑 verify：项目检查子进程收到匹配的变量，进程内的引擎检查照常写夹具真实库；运行结束后临时目录被删除 | 夹具 | `…test_verify_sets_redirect_only_for_project_checks` | 引擎检查的事件也被导走，或临时目录残留 |
| 不挂规格：B92 | 既有测试全部通过，**零修改** | 夹具 | `bin/verify --full` | 依赖事件的测试被破坏 |
| 不挂规格：B92 | 设计方实测：在一个派发槽位里跑一次 `bin/verify --full`，事件库里该分支追踪链新增的事件，只有 verify 自己的引擎检查与派发事件，没有 `ci`、`route` 类测试事件 | 人工 | PR 描述 | 根因没有消除 |
| 不挂规格：B92 | 设计方 G2：Agent-Notification 独立 worktree，升级前后 `bin/verify --full` 一致 | 人工 | PR 描述 | 消费方行为被意外改变 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | `events_db` 的 `_harness_dir` 与匹配规则，以及回归断言 | `engine/core/events_db.py`、`tests/test_events_test_isolation.py` | `python3 -W error::ResourceWarning -m unittest tests.test_events_test_isolation -v` | 验收第 1–3 行 |
| 2 | verify 为项目检查设置变量并清理临时目录 | `engine/checks/verify.py`、`tests/test_events_test_isolation.py` | 同上 | 验收第 4 行 |
| 3 | 全量验证，整理交付证据 | `tests/` | `bin/verify --full` | 验收第 5 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收、G2、槽位实测，以及以下定向变异（必须各自被对应断言抓住）：
- 去掉 `for_common_dir` 的匹配判断（任何仓库都导走）；
- verify 对引擎检查也设置变量；
- 解析失败时改成抛异常；
- 不删除临时目录。

同一失败连续三轮没有新证据时，停止这条路径。不得自行扩大白名单、修改任务书、实现非目标。

## 修订记录 1（2026-10-05，设计方槽位实测未达标）

执行方两次派发后 CI 全绿，设计方复核了代码与 4 项定向变异，全部抓住。但验收第 6 行的槽位实测没有达标：在一个装了本任务引擎的探针分支上跑 `bin/verify --full` 之后，查该分支的追踪链：

- `route/risk.file` 的夹具事件已经消失，重定向确实生效。
- 仍有 `ci/cli.install` 25 条、`ci/cli.upgrade` 1 条测试事件写进了真实库。

原因：`tests/test_install.py` 的 `env()` 在给子进程清洗环境时，去掉了所有 `HARNESS_` 开头的变量，本意是不让覆盖开关混进测试。`HARNESS_EVENTS_REDIRECT` 也被一起去掉，安装和升级子进程运行的是本仓库的引擎，公共目录又与当前仓库相同，于是事件写回了真实库。目标终态 2 预见到夹具子进程会忽略这个变量，但没有预见到子进程的环境里根本不会出现它。

裁决：

- 白名单加入 `tests/test_install.py`，**只改 `env()`**：清洗时保留 `HARNESS_EVENTS_REDIRECT`，其余 `HARNESS_` 变量照旧去掉。这个变量只对公共目录相同的仓库生效（目标终态 2），保留它不会影响测试在夹具仓库里的行为。
- 不改变量名。换成不以 `HARNESS_` 开头的名字虽然能避开清洗，但会失去守卫对 `HARNESS_*` 命令行设置的拦截：执行方可以借这个变量把自己的事件导走，逃过审计。
- 实测中只有这一个文件漏出事件；其他会清洗 `HARNESS_` 变量的测试都在夹具仓库里运行，事件本来就写进夹具库。
- 验收第 6 行不变，修复后由设计方重新实测。其余目标终态、验收、变异清单都不变，已完成的提交保留，在此基础上续跑。
