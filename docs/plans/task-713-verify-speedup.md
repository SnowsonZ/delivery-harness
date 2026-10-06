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
- `tests/test_events_equivalence.py`（修订记录 1：只在 pre-push 各状态调用前清空通过记录目录）
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

## 修订记录 1（2026-10-05，设计方裁决执行方升级：等价测试与复用语义冲突）

执行方第 1 次派发完成了步骤 1–3（`36c5d5b`、`13aaeb0`），新增测试全过。步骤 2 的实测：`run-tests -s tests` 连续 3 次都是 `Ran 282`、全部通过、结果一致，耗时 108、117、127 秒；`unittest discover -s tests` 同样 `Ran 282`，耗时 471 秒。

执行方停在步骤 3 的验证命令：`tests/test_events_equivalence.py` 在同一个装有引擎的夹具仓库里，用三种事件开关状态各调用一次 pre-push，比较结果是否等价。第一次调用跑完 verify 后按本任务的规格写下通过记录，第二次调用就按规格跳过 verify，而夹具预置的 `summary.json` 为空，测试读取时崩溃。这是新行为的正确结果，问题在于测试没有隔离各状态；该文件不在白名单，执行方按规则停下，判断正确。

裁决（采纳执行方推荐的方案 A）：

- 白名单加入 `tests/test_events_equivalence.py`，**只改一处**：pre-push 各状态调用之前，清空夹具仓库 git 公共目录下的 `harness/verify-pass/`，使每个状态都真正跑 verify，等价比较的对象不变。其余断言不动。
- 消费方扫描补一行：`tests/test_events_equivalence.py` 的 pre-push 三态比较依赖「每次 pre-push 都跑 verify」，需按上条隔离。
- 其余目标终态、验收、变异清单不变；已完成的提交保留，在此基础上续跑步骤 3 的验证与步骤 4。

## 修订记录 2（2026-10-06，设计方采纳 #154 独立评审的两项严重发现）

#154 的 Codex 评审（head `c05b865`）判「不通过」，设计方核实两项严重发现属实：

1. **运行器只发现顶层测试文件**：`unittest discover` 会递归进带 `__init__.py` 的子包，`run-tests` 只列测试目录顶层。子包里有失败测试时，串行 discover 失败，`run-tests` 却漏跑并返回 0，违反「检查强度不降」。本仓库与 Agent-Notification 目前都没有测试子包，眼下没有实际漏跑，但引擎面向所有接入方，口径必须与 discover 一致。
2. **通过记录可能绑错代码树**：`dirty` 在检查开始前采集，记录用的 HEAD tree 却在检查结束后读取。验证期间提交、切换分支或改动已跟踪文件时，旧代码的验证结果会被写成新代码树的通过记录，之后 pre-push 会错误地跳过验证。

裁决：

- 目标终态 1 补一条：**发现口径与 `unittest discover -s <目录> -p <模式>` 一致**。按 discover 的规则递归进带 `__init__.py` 的子包；每个测试文件仍一个子进程，以「顶层目录 + 模块的点分路径」定位（例如 `-s tests` 下的 `pkg/test_x.py` 用 `python -m unittest pkg.test_x`，工作目录与 `-t` 的取法同 discover），保证不漏、不重；不同子包里的同名文件各算一个。
- 目标终态 2 补一条：verify **开始时**固定待验证的 HEAD tree 与引擎标识（`engine.lock` 的 `tree`）；写记录前重新读取，HEAD tree、引擎标识任一变化，或已跟踪文件不再干净，就不写记录。
- 验收补两行：

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：验证提速 | 夹具：顶层 `test_ok.py` 通过、子包 `pkg/test_bad.py`（带 `__init__.py`）失败、另一子包有同名 `test_ok.py`：`run-tests` 退出码 1，`Ran` 总数与 `unittest discover` 相同，两个同名文件都执行 | 夹具 | `tests.test_test_runner.TestRunnerTest.test_discovery_matches_unittest` | 子包测试漏跑，失败被当成通过 |
| 不挂规格：验证提速 | verify 期间（检查运行中，用桩在某项检查里制造变化）分别发生：已跟踪文件改动、提交新 HEAD、引擎锁 `tree` 变化，三种情况都不写通过记录；不变时照常写 | 夹具 | `tests.test_prepush_reuse.PrepushReuseTest.test_no_record_when_tree_changes_during_verify` | 旧验证结果绑到新代码树 |

- 变异清单补两项：「运行器只列顶层文件」；「写记录时不再核对开始时固定的 tree」。另：原清单「verify 在有失败时也写通过记录」，单删函数内部检查是等价变异（调用方在失败时已提前返回），设计方复核按「写入挪到失败返回之前」施加。
- 交付证据要求不变，设计方在最终 head 上补齐（三次并行与一次串行的原始输出、G2、变异）。
- 白名单、其余目标终态与验收不变；已完成的提交保留，在此基础上续跑。

## 修订记录 3（2026-10-06，设计方改方案：发现交给 unittest，只分片执行）

#154 第二轮 Codex 评审（head `3efc03f`）判「不通过」，设计方核实以下几项属实：

1. **自写的文件遍历不等同于 discover**（严重）：进入子包时，没有加载包本身（`__init__.py`）里的测试，也没有执行包级 `load_tests`。例如顶层测试通过、`pkg/__init__.py` 里定义了一个失败的 TestCase、`pkg/test_ok.py` 通过：discover 失败，运行器却返回 0。
2. **改变了工作目录**（严重）：子进程的 cwd 被设成测试目录，而 `unittest discover` 只调整 `sys.path`、不切换工作目录。从项目根目录读取相对路径夹具的既有测试，换用运行器后会读错位置。
3. **`--serial` 校验有误**（一般）：只要有一个参数能匹配，其他拼错的参数也会被放过。

设计方判断：问题在方案，不在实现。「自己重写一遍 discover 的发现规则」要同时复刻 `load_tests` 协议、包级测试、pattern 的传递、`sys.path` 与工作目录的处理，每一处都可能和原版不一致，补一个洞还会冒出下一个。因此改方案：

- **目标终态 1 改为「发现交给 unittest，只分片执行」**：
  - 每个子进程在调用方的工作目录里（不切换 cwd，环境照常继承）执行与 `python -m unittest discover -s <目录> -p <模式>` **完全相同**的发现（`unittest.TestLoader().discover(start, pattern, top_level_dir)`，参数取法与 `unittest discover` 的默认值一致），把得到的测试套件按顶层测试模块（`test.__class__.__module__` 的完整点分名）分组，只运行分配给本片的模块组，结果交回父进程汇总。
  - 分片按模块做（同一模块的 `setUpModule`、`setUpClass` 不被拆散）；分配方式由实现决定，但必须确定、可复现。
  - `--serial <模块名>`：列出的模块从并行分片中剔除，在所有并行分片结束后单独顺序运行。**每个**参数都必须恰好对应一个已发现的模块，否则退出码 2，不执行任何测试。
  - 父进程同样执行一次相同的发现，得到全部测试 id 的集合；汇总时，所有分片实际运行的测试 id 集合必须与它**完全相等**（不漏、不重），否则退出码 1 并列出差异。发现本身出错（导入失败等 `_FailedTest`）按 discover 的行为算作失败用例。
  - 没有任何测试时退出码 1（与原条款一致）。
- 目标终态 1 中原来「每个测试文件一个子进程」「以点分模块名定位」以及修订 2 关于发现口径的条款，由本条替代。
- 验收第 1–3 行的测试保留（按新方案调整夹具与断言口径），修订 2 新增的 `test_discovery_matches_unittest` 改为下面第一行；另补两行：

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：验证提速 | 夹具：顶层测试通过；子包 `pkg/__init__.py` 里定义一个失败的 TestCase；`pkg/test_ok.py` 通过；另有子包带包级 `load_tests`，以及依赖 pattern 的模块级 `load_tests`；不同子包里有同名文件。`run-tests` 的退出码、运行的测试 id 集合都与 `unittest discover` 完全相同 | 夹具 | `tests.test_test_runner.TestRunnerTest.test_discovery_matches_unittest` | 漏跑包级测试，或 `load_tests` 不生效 |
| 不挂规格：验证提速 | 夹具里有测试按调用方工作目录（项目根目录）读取相对路径夹具文件：`unittest discover` 与 `run-tests` 都通过 | 夹具 | `…test_cwd_preserved` | 切换了工作目录 |
| 不挂规格：验证提速 | `--serial` 同时给一个有效、一个拼错的模块名：退出码 2，不执行任何测试 | 夹具 | `…test_serial_specs_validated_individually` | 拼错的串行项被放过，该串行的测试被并行执行 |

- 变异清单：删除修订 2 的「运行器只列顶层文件」（新方案不再遍历文件）；新增「子进程切换到测试目录运行」「去掉『实际运行的测试 id 集合与发现集合相等』的核对」「`--serial` 只要有一个参数匹配就放行」。
- 交付证据：本仓库的三次并行与一次串行实测，要记录每次的退出码、`Ran` 总数与耗时，以及并行实际运行的测试 id 数与发现集合大小。
- 白名单不变；已完成的提交保留，执行方在此基础上重写 `test_runner.py` 的发现与分片部分。

## 修订记录 4（2026-10-06，设计方采纳 #154 第三轮独立评审；用户选择继续修复）

#154 第三轮 Codex 评审（head `516b734`）判「不通过」，设计方核实两项严重发现属实，都在修订 3 方案的实现细节里：

1. **子进程提前还原了导入路径**：`discover_groups` 在发现之后立即把 `sys.path` 恢复原样，子进程随后执行测试时，discover 加进来的测试目录已经不在 `sys.path` 里。测试运行期间延迟导入测试目录里的辅助模块（文件名不匹配 `test*.py`），在串行 discover 下能通过，到了运行器里会导入失败。
2. **「已运行的测试 id」取自待运行清单，而不是实际执行的记录**：某个用例调用 `result.stop()` 之后，后面的用例不会再执行，分片却仍报告全部 id 并返回 0，父进程的完整性核对因此失效。设计方此前复核时注意到这一点，但只考虑了子进程崩溃的情形（不写结果文件，会被核对抓住），判断失误。

本仓库实测：修订 3 方案下，并行三次分别为 101、103、100 秒，`Ran 303`，测试 id 一致；串行 356 秒，`Ran 303`。约快 3.5 倍，方案值得保留。用户 2026-10-06 选择继续修复（方案 A）。

裁决：

- 目标终态 1 补两条：
  - 子进程在**整个执行期间**保留 discover 建立的导入路径，与 `python -m unittest discover` 一致；只有父进程（只做发现、不执行测试）在发现后还原 `sys.path`。
  - 分片上报的测试 id 必须来自 `TestResult` 的**实际执行事件**（`startTest` 记下的用例，跳过的用例也算已执行，`addSkip` 同样会经过 `startTest`）；分配给本片却没有执行的用例不得上报。由此，`result.stop()` 等原因造成的漏跑，会在父进程的 id 集合核对中暴露，退出码为 1。
- 验收补两行：

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：验证提速 | 夹具：测试方法与 `setUpModule` 在运行时才导入测试目录里的辅助模块 `helpers_x.py`（不匹配 `test*.py`）：`unittest discover` 与 `run-tests` 都通过 | 夹具 | `tests.test_test_runner.TestRunnerTest.test_lazy_import_of_test_dir_helper` | 子进程执行时导入路径缺失 |
| 不挂规格：验证提速 | 夹具：同一模块里第一个用例调用 `result.stop()`（真实执行，不篡改结果载荷），第二个用例因此没有执行：`run-tests` 退出码 1，并列出缺少的测试 id | 夹具 | `…test_stopped_run_detected` | 漏跑被当成完整执行 |

- 变异清单补两项：「子进程发现后立即还原 `sys.path`」；「上报的 id 改回取自待运行清单」。
- 交付证据要求不变：设计方在最终 head 上补齐原始输出（三次并行、一次串行，记录退出码、`Ran`、耗时、实际 id 数与发现数）、定向变异与 G2。
- 白名单、其余目标终态与验收不变；已完成的提交保留，在此基础上续跑。
