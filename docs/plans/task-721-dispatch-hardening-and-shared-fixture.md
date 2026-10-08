---
task: T721
class: K5
risk: R2
designer: claude-code
size: large
architecture: true
spec_refs: []
no_spec_reason: 待办 B119、B122、B123（T718、T719、T720 三次派发暴露的流程缺陷与测试夹具重复）；用户 2026-10-08 决定一并解决，一个 PR
budget:
  wall_clock_min: 600
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T721：派发加固（B119、B122）与共用 GitHub 假客户端（B123）

负责方：**派发任务**。设计方：claude-code；独立评审方：Codex（单会话）。依据：待办 B119、B122、B123。**路径全部落在 `autonomy.toml [contract_route] allowed` 内**（`engine/**`、`tests/**`、`CHANGELOG.md`）。A、B 两部分都改 `engine/agents/dispatch.py`（不同函数），C 部分只动测试；按 A、B、C 顺序实施，各自可独立验收；用户要求一个 PR 解决，只有在确实做不下去时才拆。**C0 例外**：共用合同 C0 禁止改既有测试，C 部分必须改六个既有测试文件里的 `FakeGh` 类与对应 `import`，这是对 C0 的专项例外；除这两处外，这六个文件的其余内容（断言、夹具数据、测试方法）逐节点冻结，验收第 8 行用 AST 逐节点比对核验。

## 病灶

**A（B119）任务书准入不检查白名单文件的行数余量。** T718 的白名单里 `events_io.py` 797 行，任务书要求再加代码，必然撞 800 行质量棘轮（`tests/test_split_modules.py::test_line_budgets`、`quality.LONG_FILE`）。首次派发因此失败，多出一次任务书修订与两轮 CI。准入应当在派发前就发现这种冲突。

**B（B122）派发崩溃会清掉槽位里未提交的成果，续做还会读到过期的任务书。**
1. T719 第 1 次派发：执行方超时，派发在 `write_record` 里 `git commit` 派发记录，被 pre-commit 钩子（lint 看的是整个工作区）拒绝，`subprocess.CalledProcessError` 未处理，进程以回溯退出。结束路径 `return_slot → _salvage_and_remove` 只推送**已提交**的提交做保全，然后无条件 `git worktree remove --force`，未提交的 60 分钟成果随之删除（只能靠会话事件流重放恢复）。
2. T719 第 2 次续做：任务书在 main 上已修订，但 `prepare_slot(resume=True)` 只 `checkout -B <分支> origin/<分支>`，任务分支上还是旧任务书，执行方读到过期的行数预算而误升级，多耗 44 分钟。

**C（B123）七个测试文件各写了一份几乎相同的假 GitHub 客户端 `FakeGh`。** 新增一条 API 路由要同步改多处（T720 加 `actions/workflows` 路由改了八处），测试体量也因此偏大。其中 `test_audit_completeness`、`test_audit_ledger`、`test_audit_reconstruction`、`test_audit_ledger_github` 相似度 0.7 到 1.0，`test_audit_events`、`test_ledger_equivalent_head` 是它们的子集。

## 目标终态

### A. 准入检查白名单行数余量（`engine/checks/taskbook.py`、`engine/agents/dispatch.py`）

1. 新函数 `headroom_errors(path, root)`，读任务书「白名单」一节里每个以 `- ` 开头的条目，取条目中**第一个**反引号括起的路径（同一条目里后面的路径一律忽略）；只处理满足全部条件的：路径是 `*.py`、文件已存在、属于质量棘轮统计范围（用 `quality.sources()` 给出的目录与通配判定，**不要自己再写一套目录规则**）。行数口径与 `quality.measure` 相同（`len(text.splitlines())`），上限用 `quality.LONG_FILE`。
2. 条目里声明了「净增不超过 N 行」「净增不得超过 N 行」（两种写法都认，N 为整数）：现有行数 C 加 N 大于 `LONG_FILE` 就报错，错误文字写明文件、C、N 与上限，并提示「先写明把代码放进新模块或搬迁的步骤，或降低净增」。
3. 没有声明净增、但 C 距上限不足 100 行（C ≥ `LONG_FILE − 100`）：报错，要求补「净增不超过 N 行」且 C + N ≤ 上限。C 更小时不要求声明。
4. 接入点：**只**在两处调用，**不进入 `check_all`**：①`dispatch.admit`（把错误并入 `report.errors`，与 `on_main` 的问题同一处理）；②`bin/harness taskbook <路径>` 显式传路径时（`main` 里 `args.paths` 非空，与 `--on-main` 同一层）。理由：`check_all` 每次 `verify` 都扫全部历史任务书，历史任务书里「当前 773 行」之类的陈述会随文件增长而过期，现状下 T720 的任务书（`events_io.py` 当前 788 行 + 净增 20）一启用就会让 `verify` 失败。
5. **体量提示（不阻断）**：`Report` 新增 `warnings: list[str]`；验收表行数 ≥ 10 时加一条提示「验收项 N 行，T718、T719 均因体量超出一次派发预算而多轮返工，考虑拆分（或确认已拆到无法再拆）」。`main` 在汇总后逐条打印「提示：…」，**不计入不合格数、不改变退出码**；`dispatch.admit` 打印同样的提示到 stderr，不阻断。

### B. 派发保全与续做（`engine/agents/dispatch_slots.py`、`engine/agents/dispatch.py`）

1. **未提交改动先备份再删除**：新增函数 `backup_uncommitted(slot, branch) -> str | None`（放在 `dispatch_slots.py`），`_salvage_and_remove` 在 `git worktree remove --force` 之前调用它。若槽位 `git --no-optional-locks status --porcelain` 非空（含未跟踪文件、已暂存与未暂存、删除的文件；**必须带 `--no-optional-locks`**：普通 `git status` 会刷新并写回真实索引的 stat 缓存，破坏「真实索引字节不变」），先建备份提交：临时索引用 `git read-tree HEAD` 初始化后再 `git add -A`，使备份树反映**工作区的完整状态（包括删除）**：用临时索引（`GIT_INDEX_FILE` 指向临时文件）`git add -A`（遵守 `.gitignore`）、`git write-tree`、`git commit-tree <树> -p HEAD -m <说明>`（用 `-c user.name=… -c user.email=…` 给固定身份，不依赖仓库配置）、`git update-ref refs/backup/dispatch/<分支名斜杠换成短横>/<UTC 时间 yyyymmddThhmmss> <提交>`，并向 stderr 打印备份引用与恢复方法：`git diff --name-status HEAD <引用>` 看全部差异（`D` 项是执行方删除的文件）；`git restore --source=<引用> --worktree --staged :/` 把工作区与索引整体还原到备份状态（默认 no-overlay，**快照里不存在的已跟踪文件会被删除**，即还原执行方的删除；设计方在临时仓库实测过：修改、暂存、未跟踪、删除四类都能复现）。备份引用在主仓库（worktree 共享引用）。返回备份引用名，干净时返回 `None`。**备份失败就抛 `RuntimeError`，不删除工作树**——沿用 `return_slot` 现有的「归还未完成（工作树保留）」处理。工作区干净时不建任何引用。
2. **记录提交失败不再回溯崩溃**（`write_record`）：捕获 `CalledProcessError`，用 `capture_output` 取钩子输出的末 600 字符，抛 `Stop`，文字写明「派发记录提交被钩子拒绝：…；执行方的未提交改动会在归还槽位时备份到 refs/backup/dispatch/…」。`_run_dispatch` 已处理 `Stop`（退出码 2），`finally` 里的 `return_slot` 触发第 1 条的备份。
3. **续做前自动合入默认分支**（`prepare_slot(resume=True)`）：`checkout -B <分支> origin/<分支>` 之后，若 `origin/main` 不是 HEAD 的祖先（`merge-base --is-ancestor` 判定），执行 `git merge --no-edit origin/main`（默认允许快进：任务分支只是落后 main、没有自己的提交时是快进，不产生合并提交；分叉时产生合并提交），提交身份用调用方传入的 `env`（`prepare_slot` 新增可选参数 `env`，`Dispatcher.run` 传 `{**os.environ, **self.identity}`；默认 `None` 保持旧调用兼容）。实现提示：`common.git()` 不接受 `env`，且 `check=False` 会把祖先判定（`merge-base --is-ancestor`）的 0、1、128 返回码都压成空字符串，所以这里用 `subprocess.run` 直接调用（可传 `env`、保留返回码；`dispatch_slots.py` 已在白名单内），返回码 0 = 已含、1 = 未含、其他 = 抛 `RuntimeError`。冲突时 `git merge --abort` 并抛 `Stop`，文字写明「续做前合并 origin/main 冲突，请设计方先解决冲突」。已包含 `origin/main` 时不产生合并提交。非 `resume` 路径完全不变。

### C. 共用假客户端（`tests/gh_fakes.py` 新增，六个测试文件改为使用）

1. 新增 `tests/gh_fakes.py`，导入方式沿用仓库既有写法（`from tests.gh_fakes import FakeGhBase`，测试文件已有 `sys.path.insert(0, 仓库根)`）。`FakeGhBase` 实现六个文件**共有**的行为：`repo()`、`pr()`、`api()` 路由分发、`_page()`（按 `page`、`per_page` 切片）、`download()`、评论写操作（POST 锚点评论，记录到 `self.writes`）、`fail` 子串命中抛 `RuntimeError("HTTP 403: 权限不足（夹具）")`、未配置的路由 `raise AssertionError(f"FakeGh 未配置的路由：{path}")`，覆盖的路由共 **11** 类：`pulls`（批量模式的已关闭 PR 列表）、`pulls/<n>`、`pulls/<n>/reviews`、`pulls/<n>/commits`、`commits/<sha>`、`issues`（按标签）、`issues/<n>/comments`、`issues/comments/<id>`、`actions/runs`、`actions/runs/<id>/artifacts`、`actions/workflows`（空列表，T720 加的）。
2. 以下**六个**文件的 `FakeGh` 改为 `class FakeGh(FakeGhBase)`，只保留各自**真正不同**的行为。设计评审 1 对六个文件逐个 `difflib` 并实例化旧客户端查询同一路由，得到下面的**行为矩阵**（这是已知差异的下限，不是封闭清单——执行方必须自己再对着各文件现有代码核对一遍，把发现的其他差异也保留下来，并写进 `tests/gh_fakes.py` 的文档字符串）：

   | 文件 | 已知差异 |
   |---|---|
   | `test_audit_completeness` | `closed`（批量已关闭 PR 列表）；`actions/runs` 按 `branch` 过滤、runs 与 artifacts **不分页** |
   | `test_audit_ledger_github` | 同上，但没有 `closed` 路由 |
   | `test_audit_reconstruction` | `closed`、`page_size`；`actions/runs` 按 `branch` 过滤、runs 与 artifacts **分页** |
   | `test_audit_ledger` | `page_size`、`audit`/`escape` 构造参数、评论 id 计数器 `_ids`、PATCH 评论、`writes` 记账方式；`actions/runs` **不按 branch 过滤**；没有单条评论 GET；评论时间戳与前三个文件不同 |
   | `test_ledger_equivalent_head` | `repo()` 不记录调用；缺失提交抛 `KeyError`、缺失评论抛 `StopIteration`；`actions/runs` 不过滤 branch |
   | `test_audit_events` | `closed`、`fail_comments`；任何提交查询固定返回两个父节点；API 调用固定记为 `GET` |

   基类提供公共分发，差异通过构造参数或小型覆盖方法保留（例如 `runs_filter_by_branch`、`paginate_runs`、`missing_commit_error` 之类，命名由执行方定）。**不得改变任何断言、夹具数据或被测行为**；做不到不改断言时按派发规则升级，不得自行改断言。
3. **不迁移**的三个文件，各写明理由（写进 `tests/gh_fakes.py` 的模块文档字符串）：`test_trace_events_cli.py`（`FakeGh.api(route)` 按页返回运行列表，协议不同）、`test_gh_json_fields.py`（PATH 上的子进程假 `gh` 与 `_RunsStub`）、`test_events_judge_runs.py`（按工作流分页的运行列表，协议不同）。
4. 之后新增一条 API 路由只改 `tests/gh_fakes.py` 一处（对六个文件生效）。

### 共同

CHANGELOG「Unreleased」两行（英文）：①任务书准入检查白名单文件的行数余量（admission checks `bin/dispatch run` and `bin/harness taskbook <path>`），并对验收项很多的任务书给出拆分提示；②派发在槽位归还前把未提交改动备份到 `refs/backup/dispatch/…`、记录提交被拒绝时不再崩溃、`--resume` 前自动合入默认分支。**Migration:** none。C 部分是纯测试重构，不写 CHANGELOG。

## 白名单

- `engine/checks/taskbook.py`（当前 471 行，净增不超过 80 行，最终必须 ≤ 800）
- `engine/agents/dispatch.py`（当前 703 行，净增不超过 50 行，最终必须 ≤ 800）
- `engine/agents/dispatch_slots.py`（当前 162 行，净增不超过 90 行，最终必须 ≤ 800）
- `tests/test_taskbook_headroom.py`（新增，A 部分的测试）
- `tests/test_dispatch_salvage.py`（新增，B 部分的测试）
- `tests/gh_fakes.py`、`tests/test_gh_fakes.py`（新增，共用基类及其测试）
- `tests/test_audit_completeness.py`、`tests/test_audit_ledger.py`、`tests/test_audit_reconstruction.py`、`tests/test_audit_ledger_github.py`、`tests/test_audit_events.py`、`tests/test_ledger_equivalent_head.py`（只把 `FakeGh` 类换成基类子类并相应调整 `import`，其余逐节点冻结，见 C0 例外）
- `CHANGELOG.md`

## 消费方扫描（命令与输出，设计方 2026-10-08 执行；本仓库 52179ce）

```
$ grep -rn "check_all\|admit(" engine | grep -v "^engine/checks/taskbook.py"
engine/agents/dispatch.py:147 (admit 调 check_all)   engine/checks/verify.py:139 (verify 的 taskbook 检查调命令行，不带路径)；另有 signoff、run_check 等经 admit/check_all 的间接引用（设计评审 1 实跑 grep 的结果比此处更多，执行方实现前自行再扫一遍）
$ grep -ln "check_all\|dispatch.admit\|taskbook.check_text" tests/*.py
tests/test_dispatch_robustness.py tests/test_dispatch_alerts.py tests/test_events_agents.py tests/test_run_timeline.py tests/test_native_automerge.py tests/test_missing_context.py（六处 check_all 桩，均返回 Report；设计评审 1 核对兼容）
$ grep -n "_salvage_and_remove\|return_slot\|prepare_slot\|write_record" engine/agents/*.py tests/*.py | grep -v "^engine/agents/dispatch_slots.py"
engine/agents/dispatch.py: write_record 定义 371、调用 501；prepare_slot 调用 436；return_slot 调用 699（_run_dispatch finally）
tests/test_dispatch_robustness.py: 覆盖 reclaim_stale_slots、return_slot（line 400、516）
$ wc -l engine/checks/taskbook.py engine/agents/dispatch.py engine/agents/dispatch_slots.py → 471 703 162
$ grep -ln "class FakeGh" tests/*.py  →  test_audit_events、test_audit_completeness、test_audit_ledger、test_audit_reconstruction、test_ledger_equivalent_head、test_trace_events_cli、test_audit_ledger_github；另有 test_gh_json_fields（_FakeGh、_RunsStub）、test_events_judge_runs（FakeGh、_PagedGh）
$ 相似度（difflib，对 test_audit_completeness）：test_audit_ledger_github 0.93、test_audit_reconstruction 0.86、test_audit_ledger 0.70、test_ledger_equivalent_head 0.49、test_audit_events 0.42、test_trace_events_cli 0.21、test_events_judge_runs 0.14
$ 测试文件互相导入的既有写法：tests/test_alert_cli.py:32 `from tests.test_ci_events_workflows import …`（仓库根在 sys.path；ci-shard 的 modules() 用 glob("test_*.py") 收集，gh_fakes.py 不以 test_ 开头不会被收集）
$ python3 -c 'import runpy; print(len(runpy.run_path("bin/ci-shard")["modules"]()))' → 65（设计评审 1 实测；新增三个测试模块后应为 68）
```

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| `verify` 的 `taskbook` 检查（`check_all`） | 无需 | A 部分不进入 `check_all`，历史任务书不受影响；新增 `warnings` 字段不改变 `errors` 的判定 |
| `docs/plans/task-*.md` 全部历史任务书 | 无需 | 同上；执行方实现后在本仓库跑 `bin/harness taskbook`（不带路径）必须仍合格 |
| 其他调用 `dispatch_slots.prepare_slot` 的位置 | 实现前核对 | 新增可选参数 `env` 默认 `None`，旧调用不变；若有测试直接调用，必须仍通过 |
| `tests/test_dispatch_robustness.py`（reclaim、return_slot 用例） | 无需改，必须仍通过 | 备份只在工作区非干净时发生；这些用例的槽位是干净的 |
| `ci-shard` 与 `tests/ci_shard_weights.txt` | 无需改 | 未登记权重的模块用 `DEFAULT_WEIGHT=10.0`（不是按文件大小）；新增三个测试模块 `test_taskbook_headroom`、`test_dispatch_salvage`、`test_gh_fakes` 会被收集，模块集合从 65 变 68 |
| 其余测试里的 `FakeGh`（trace_events_cli、gh_json_fields、events_judge_runs） | 不迁移 | 协议不同，理由写进 `tests/gh_fakes.py` 文档字符串 |

## 设计方的绕过与失效清单

| 可能的问题 | 处理 |
|---|---|
| A：行数检查放进 `check_all`，历史任务书因文件增长而过期失败 | 目标终态 A.4：只在 `dispatch.admit` 与显式路径的 `bin/harness taskbook` 两处；验收有「不带路径的 `bin/harness taskbook` 仍全部合格」与「把 `check_all` 里加检查须失败」的变异 |
| A：自己再写一套目录或行数口径，与质量棘轮漂移 | 复用 `quality.sources()` 与 `quality.LONG_FILE`；验收有「改 `LONG_FILE` 时检查随之变」的断言 |
| A：声明写法不统一，漏检 | 「不超过」「不得超过」两种都认；条目里没有声明、距上限不足 100 行也报错 |
| A：新文件或测试文件被误检 | 只检已存在、且在棘轮统计范围内的 `*.py`；验收有新文件、测试文件、非 Python 文件不报的用例 |
| B：备份漏掉未跟踪文件，或把 `.gitignore` 的文件也备份 | 临时索引 `git add -A`（遵守 `.gitignore`）；验收同时有已跟踪改动、未跟踪文件、被忽略文件三种 |
| B：备份污染真实索引或分支 | 只用临时索引文件和 `commit-tree` + `update-ref`，不动当前分支与真实索引；验收断言备份后槽位的分支 HEAD 与索引状态不变 |
| B：备份失败仍然删除 | 失败抛 `RuntimeError`，工作树保留；验收用失败注入（备份命令失败）断言目录仍在 |
| B：钩子输出里带出敏感内容 | 只取末 600 字符；记录提交失败的 `Stop` 文字不含完整钩子输出 |
| B：续做合并 main 引入冲突后卡死 | 冲突 `merge --abort` 并抛 `Stop`；验收有冲突用例，断言工作树干净、分支未被破坏 |
| B：无谓的合并提交 | 已含 `origin/main` 时不合并；验收断言提交数不变 |
| B：非续做路径行为变化 | 验收有「`resume=False` 时不合并、不备份判定外的任何新动作」的断言 |
| C：重构悄悄改了断言或夹具数据，测试变弱 | 白名单明确「不改断言」；验收有「六个文件里的 `assert` 语句数量与重构前一致」的核对（AST 统计 `Assert` 与 `self.assert*` 调用数，设计方在验收时对比 main）；评审按此复核 |
| C：基类吞掉了某个文件独有的行为 | 行为矩阵给出已知差异的下限、逐文件列出，并要求执行方自行再核对一遍；每个文件重构后原有用例全部通过（全量 `unittest`），且除 `FakeGh` 与 `import` 外 AST 逐节点冻结 |
| C：`gh_fakes.py` 被当成用例收集或被 `ci-shard` 漏算 | 文件名不以 `test_` 开头；验收断言 `bin/ci-shard` 的模块集合不含它 |

## 非目标

- 不改派发的其他流程（认领、槽位回收、独立评审衔接），不改 `rules.toml` 与 pre-commit 钩子（记录提交被钩子拒绝的**根因**——钩子对整个工作区而不是暂存区做 lint——不在本任务内，这里只保证它不再造成崩溃和丢失）。
- 不迁移三个协议不同的测试桩；不抽取 `test_audit_ledger_github.py` 之外的测试夹具；不改任何被测引擎行为（C 部分）。
- 白名单以外的文件一律不改；共用合同 C0 同样适用。

## 前置条件

- 工作区基于最新的 `origin/main`。按函数名定位，行号只作参考；只有函数、调用点或接口本身对不上时，才停下向设计方报告。

## 验收

具名测试为本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B119 | **余量检查**：临时仓库里造一个 797 行的 `engine/x.py` 与任务书白名单条目「净增不超过 20 行」→ 报错且文字含文件、C=797、N=20、上限；「净增不得超过 20 行」同样；声明 N=3 → 不报；没有声明、C=797 → 报错要求声明；没有声明、C=699 → 不报，C=700 → 报错（钉住 `LONG_FILE − 100` 边界）；文件不存在（新增）、测试文件、`.md` 文件 → 不报；`LONG_FILE` 被 patch 成别的值时结果随之变化；**`quality.sources()` 被 patch 成自定义目录与通配**（如 `[("lib", "*.py")]`）时，`lib/x.py` 条目被检、默认的 `engine/x.py` 条目不再被检（证明复用而不是写死目录）；同一条目里出现多个反引号路径时只取第一个 | 夹具 | `tests/test_taskbook_headroom.py::HeadroomTest` | 无检查 |
| 不挂规格：B119 | **接入点**：`dispatch.admit` 对余量不足的任务书抛 `Stop`（错误并入准入未通过文字）；`bin/harness taskbook <路径>` 退出码 1；不带路径的 `bin/harness taskbook`（`check_all`）对同一份任务书**不**报余量错误；本仓库全部历史任务书不带路径检查仍合格 | 夹具 | `::AdmissionWiringTest` | 余量检查进了 `check_all`，或没接上准入 |
| 不挂规格：B119 | **体量提示**：验收表 ≥ 10 行 → `warnings` 有一条且 `main` 输出「提示：」、退出码不变（0）；< 10 行无提示；`dispatch.admit` 打印提示到 stderr 但不阻断 | 夹具 | `::WarningTest` | 提示缺失，或提示变成了错误 |
| 不挂规格：B122 | **备份函数**（删除之前就验证）：真实临时 git 仓库与 worktree，槽位里同时有已暂存改动、未暂存改动、**被删除的已跟踪文件**、未跟踪文件、被 `.gitignore` 忽略的文件 → 直接调用 `backup_uncommitted` 后返回引用名 `refs/backup/dispatch/…/<时间>`；备份提交的树里有已暂存与未暂存的改动内容和未跟踪文件、**没有被删除的文件**、没有被忽略的文件；槽位分支 HEAD、真实索引文件的字节、工作区文件内容在调用前后完全相同（含「已跟踪文件内容不变、只有时间戳变化」这一情形与未跟踪改动并存，用来证明状态检测没有写回索引）；**已跟踪但匹配忽略规则的文件**仍留在备份树里；在一个新工作树里按打印的恢复命令实际还原一次，新增、修改、暂存、未跟踪、删除五类都与备份前一致；工作区干净 → 返回 `None`、不建任何引用；再通过 `_salvage_and_remove` 走完整路径：目录已删、引用仍在，且 `git diff --name-status HEAD <引用>` 能列出 `D` 项 | 夹具 | `tests/test_dispatch_salvage.py::BackupTest` | 工作树被删而无备份 |
| 不挂规格：B122 | **备份失败不删除**：注入备份步骤失败（如 `update-ref` 失败）→ 抛 `RuntimeError`、工作树目录仍在、未被 `worktree remove`；`return_slot` 打印「归还未完成（工作树保留）」 | 夹具 | `::BackupFailureKeepsWorktreeTest` | 备份失败仍删除 |
| 不挂规格：B122 | **记录提交被拒不再回溯**：钩子拒绝记录提交 → `write_record` 抛 `Stop`（不是 `CalledProcessError`），文字含「派发记录提交被钩子拒绝」与钩子输出末 600 字符以内的片段；端到端经 `_run_dispatch` 退出码 2，且槽位里未提交改动已备份 | 夹具 | `::RecordCommitRejectedTest` | 回溯崩溃或丢改动 |
| 不挂规格：B122 | **续做合入默认分支**，四种情形各一个子测试，并对 `git merge` 调用做监视（包一层记录参数的 `git` 函数）：①任务分支只落后 main、没有自己的提交 → 快进，HEAD 等于 `origin/main`，不新增合并提交；②任务分支与 main 分叉 → 新增一个合并提交，父节点为任务分支原 HEAD 与 `origin/main`，作者为 `env` 里的身份；③任务分支已含 `origin/main` → **没有调用 `git merge`**、HEAD 不变；④冲突 → `Stop`，工作树干净、分支 HEAD 不变、没有残留合并状态；另：`resume=False` 时不调用 `git merge` | 夹具 | `::ResumeMergeTest` | 续做读到过期任务书 |
| 不挂规格：B123 | **共用基类**：`FakeGhBase` 单独实例化后，**十一类**路由（含 `pulls` 批量列表与 `actions/workflows`）各有返回；未配置的路由抛 `AssertionError`；`fail` 命中抛 `RuntimeError`；评论 POST 被记录；`_page` 按 `page`、`per_page` 切片；给基类加一条新路由，六个子类立即可见 | 夹具 | `tests/test_gh_fakes.py::BaseRoutesTest` | 基类缺路由或行为不一 |
| 不挂规格：B123 | **六个文件重构后不变**：六个测试文件各自的 `FakeGh` 都是 `FakeGhBase` 的子类；这六个文件原有用例全部通过；**除 `FakeGh` 类定义与 `import` 外，每个文件的 AST 与 main 上的版本逐节点完全相同**（`ast.dump` 比对，剔除这两处；这比断言计数强，改一个 `assertEqual(a, b)` 为 `assertEqual(a, a)` 也会被发现）；六个文件里**不再残留旧的完整 `FakeGh` 实现**（每个 `FakeGh` 类体不含 `api` 的完整路由分发，行数合计比重构前至少减少 250 行作为收益指标，不作为唯一依据）；向 `FakeGhBase` 加一条新路由，六个子类立即可见（对每个子类各断言一次） | 夹具 | `tests/test_gh_fakes.py::SubclassesTest` 加设计方复核时的 AST 比对脚本（对比 main） | 断言或夹具数据被改动、旧实现残留、子类没接上基类 |
| 不挂规格：B123 | `bin/ci-shard` 的模块集合：重构前的 65 个原样保留，仅新增 `tests.test_taskbook_headroom`、`tests.test_dispatch_salvage`、`tests.test_gh_fakes` 三个（共 68），不含 `tests.gh_fakes`，所有分片无重复、无遗漏；三个不迁移的测试文件逐字节不变；`tests/gh_fakes.py` 的模块文档字符串写明三条不迁移理由与六个文件的行为矩阵 | 夹具 | `tests/test_gh_fakes.py::ShardSetTest` | 基类被当成用例、模块漏收或重复 |
| 不挂规格：A、B、C | 既有测试全部通过（含 `test_dispatch_robustness`、`test_split_modules`、`bin/harness taskbook` 不带路径） | 夹具 | `bin/verify --full` | 回归 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | A：`headroom_errors`、`warnings`、`admit` 与 `main` 的接入，先写测试 | `engine/checks/taskbook.py`、`engine/agents/dispatch.py`、`tests/test_taskbook_headroom.py` | `python3 -W error::ResourceWarning -m unittest tests.test_taskbook_headroom -v` | 验收第 1–3 行 |
| 2 | B：备份、记录提交被拒、续做合并，先写测试 | `engine/agents/dispatch_slots.py`、`engine/agents/dispatch.py`、`tests/test_dispatch_salvage.py` | `python3 -W error::ResourceWarning -m unittest tests.test_dispatch_salvage tests.test_dispatch_robustness -v` | 验收第 4–7 行 |
| 3 | C：`tests/gh_fakes.py` 与六个文件的重构，先写基类测试 | `tests/gh_fakes.py`、`tests/test_gh_fakes.py`、六个既有测试文件 | 六个文件各自的 `unittest` 加 AST 统计 | 验收第 8–10 行 |
| 4 | CHANGELOG；全量验证，整理交付证据 | `CHANGELOG.md` | `bin/verify --full` | 验收第 11 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收、G2（CI 的 `consumer-contract`）、断言数量对比与定向变异复核，以下变异必须各自被对应断言抓住：
- A：把检查放进 `check_all`；不用 `quality.sources()` 与 `LONG_FILE` 而写死 800；声明写法只认一种；没声明不报；把 100 行余量改成 0；提示变成错误；
- B：备份漏未跟踪文件、把被忽略文件也备份、备份动了真实索引或分支 HEAD、备份失败仍删除、记录提交被拒仍回溯、续做不合并、总是合并（含已最新）、冲突不 abort、`resume=False` 也合并；
- A（补）：把 700 行边界改成 701 或 699；不用 `quality.sources()` 而写死 `engine/`；同一条目取最后一个路径而不是第一个；
- B（补）：备份树里残留被删除的文件（例如用 `git add --ignore-removal`）、临时索引不用 `read-tree HEAD` 初始化（须由「已跟踪但被忽略的文件仍在备份树里」抓住，删除断言抓不住这一项）、状态检测改回普通 `git status`（须由「真实索引字节不变」抓住）、备份后分支 HEAD 或真实索引改变、总是执行 `git merge`（含已最新，须由「没有调用 `git merge`」抓住）、只在冲突时才 abort 之外的路径留下合并状态；
- C：基类缺 `actions/workflows` 或批量 `pulls`、`_page` 不切片、`fail` 不抛、六个子类中某个仍保留旧的完整实现、把某个 `assertEqual` 改成自等（须由 AST 逐节点比对抓住）、某个子类丢掉自己独有的行为（如 `runs` 的 branch 过滤或分页、`audit_events` 的两父节点提交）。

**合并并随自举升级生效之后**：设计方在真实仓库做一次小验证——故意造一份余量不足的任务书，`bin/dispatch run` 应在准入阶段被拒；`bin/dispatch stop --all` 与槽位里留一个未提交文件后归还，应出现 `refs/backup/dispatch/…`。
