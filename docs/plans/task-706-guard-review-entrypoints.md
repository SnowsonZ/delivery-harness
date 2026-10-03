---
task: T706
class: K7
risk: R3
designer: claude-code
size: small
architecture: true
spec_refs: []
no_spec_reason: 待办 B81 自治试验设计第 6 节；T704（PR #113）设计方验收发现的入口缺口与 Codex 独立评审的两条严重发现（gh 带值选项错位、管道交给 shell 的 dispatch），无产品规格验收编号
budget:
  wall_clock_min: 90
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T706：守卫补漏——评审与复核的其他入口、gh 带值选项错位、管道交给 shell 的命令

负责方：**派发任务**。设计方：claude-code；独立评审方：Codex。依据：[自治试验设计](2026-10-03-autonomy-trial-design.md)第 6 节；T704（PR #113）验收中设计方发现的缺口，以及 #113 的 Codex 独立评审（不通过）的两条严重发现。**依赖**：T704 已合并。**必须在 T702 合并之前合并**：T702 会新增 `signoff` 命令，到那时这个缺口就能被用来伪造设计方复核。

**病灶（设计方在 PR #113 的 head `8c5b01d` 上用 `command_guard.check_command(..., role="implementer")` 实测）**：

| 命令 | 结果 |
|---|---|
| `bin/dispatch review 12` | 拒绝 |
| `bin/harness dispatch review 12` | **放行** |
| `bin/harness review pr 12` | **放行** |
| `python3 .harness/engine/cli.py dispatch signoff 12` | **放行** |
| `python3 .harness/engine/cli.py review pr 12` | **放行** |

缺口一的原因有两条：
- `bin/dispatch` 只是 `exec bin/harness dispatch "$@"`（见 `bin/dispatch` 末行），而 T704 只拦了程序名为 `dispatch` 的命令（`engine/core/shell_structure.py` 的 `_check_simple`）；
- `engine/cli.py` 的 `COMMANDS` 表里 `review` 与 `dispatch` 都是顶层子命令（第 39–40 行）。解释器后面跟脚本文件的调用（`python3 <路径>/cli.py …`），`_check_simple` 在解释器分支里直接返回，不检查脚本的参数。

**缺口二（Codex 严重项 1，设计方复现）**：`_gh`（`engine/core/shell_structure.py` 第 417–418 行，T704 合并后的 main）取位置参数时，只过滤以 `-` 开头的参数，选项后面的**值**会被当成子命令。所以 `gh pr -R o/r comment 1`、`gh issue --repo o/r comment 1`、`gh pr -R o/r review 1` 都会放行。这个缺陷早于 T704：`gh pr -R o/r merge 3`、`gh issue -R o/r close 3` 同样放行，影响守卫里所有基于 gh 子命令的规则。

**缺口三（Codex 严重项 2，设计方复现）**：`dispatch` 的兜底正则（T704 加在 `command_guard.py` 第 127 行）要求命令出现在行首或分隔符之后。经管道交给 shell 执行时，整段命令按字符串规则判断，`printf "bin/dispatch review 12" | sh` 中的 `dispatch` 前面是引号，所以正则不命中。相比之下，`gh pr comment` 的正则没有锚定，同样的写法会被拦下。

## 目标终态

仅在 `role == "implementer"` 时，以下调用都以 `REVIEW_SIGNAL` 为理由拒绝（复用 T704 的常量和规则键 `review_signal`）：

1. 程序名为 `harness` 或 `cli.py` 时：
   - 第一个位置参数是 `dispatch`，且第二个位置参数在 `IMPLEMENTER_DISPATCH_ACTIONS`（`review`、`review-calibrate`、`signoff`）中；
   - 或者第一个位置参数是 `review`。这是顶层命令 `review`，包括 `pr`、`calibrate`。`review-pack`、`review-plan` 只写本地文件，属于其他顶层命令，照常放行。
2. 解释器（`python`、`python3`、`python3.x`）后面第一个非选项参数的文件名是 `cli.py` 时，按第 1 条检查它后面的参数。
3. 结构化解析失败时的兜底：在 `command_guard.IMPLEMENTER_COMMAND_RULES` 加一条正则，覆盖 `(harness|cli\.py)` 后接 `dispatch (review|review-calibrate|signoff)`，或后接 `review`、且 `review` 后面不是 `-pack` / `-plan` 的情况。规则键为 `review_signal`。
4. **gh 带值选项**：`_gh` 在提取 `sub`、`action` 之前，先消费带值的全局选项及其值。选项包括 `-R`、`--repo`、`--hostname`，形式有分开写（`-R x`）和连写（`--repo=x`、`-Rx`）两种。修完之后，原有的 gh 规则（merge、approve、release、issue 与 label、api）对带 `-R` 的写法也必须同样生效。这是收紧，不放宽任何规则。
5. **管道交给 shell**：第 3 条的兜底正则，以及 T704 的 `dispatch` 兜底正则，都改为不要求锚定在行首或分隔符之后，只要求命令名前面不是字母、数字或下划线（`(?<![\w])`）。这样引号内、`printf … | sh` 的写法也会命中。设计方角色不受这些规则约束（它们只在 `IMPLEMENTER_COMMAND_RULES` 中）。
6. 设计方角色对这些命令全部放行。`bin/harness verify`、`bin/harness taskbook …`、`bin/harness review-pack`、`bin/harness review-plan`、`bin/dispatch status`、`bin/dispatch run …` 对执行方也照常放行。

## 白名单

- `engine/core/shell_structure.py`
- `engine/guards/command_guard.py`
- `tests/test_guard_review_entrypoints.py`（新增）

消费方扫描（检查单第 2 项）：

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| `engine/routing/run_check.py` 的可信理由清单 | 无需 | 复用 T704 已登记的 `REVIEW_SIGNAL` |
| `tests/test_guard_review_signals.py`（T704）、`tests/test_command_guard_structure.py` | 无需 | 只新增拒绝面，已有断言不变。实现前核对 `test_command_guard_structure` 里有没有「带 `-R` 时应放行」或「引号内文本应放行」的断言；如果有，而且被本任务的收紧打破，就停下报告设计方 |
| `engine/core/shell_structure.py` 中其他调用 `_gh` 的规则 | 随第 4 条一起收紧 | 都是拒绝面变大，不放宽任何规则 |
| 执行方提示词与派发流程 | 无需 | 执行方本来就不该调用评审和复核；派发流程中自检用的 `bin/harness verify` 照常放行 |

CHANGELOG 由设计方在升级 PR 里统一写，本任务不改。

## 非目标

- 不处理执行方自写脚本、直接 import 引擎模块或直接调用 API 的情况。这是设计第 6 节已经说明的残余风险，根治靠 B31 和 B85。
- 不改 T704 已有的规则。
- 白名单以外的文件一律不改。
- 共用合同 C0 同样适用。

## 前置条件

- T704 已合并，工作区基于最新的 `origin/main`。核对上面引用的行号与常量，有出入就停下，向设计方报告。
- 和 T703、T705 的白名单不交叉，可以并行。
- 实现前先确认本任务书已在 main 上：`bin/harness taskbook docs/plans/task-706-guard-review-entrypoints.md --on-main` 通过。

## 验收

具名测试为本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B81 | 执行方角色下，病灶表里放行的四条命令，以及 `./bin/harness dispatch signoff 3`、`python3 /x/.harness/engine/cli.py review calibrate` 都被拒绝，理由是 `REVIEW_SIGNAL`；用 `&&` 串联或包在 `bash -c` 里同样被拒绝 | 夹具 | `tests.test_guard_review_entrypoints.GuardReviewEntrypointsTest.test_implementer_denied_all_entrypoints` | 病灶原样放行 |
| 不挂规格：B81 | 结构化解析失败的命令（比如引号不闭合）走正则兜底，同样被拒绝 | 夹具 | `…test_fallback_regex` | 只修了结构化这一条路径 |
| 不挂规格：B81 | 带值选项不再错位：执行方角色下，`gh pr -R o/r comment 1 -b x`、`gh issue --repo=o/r comment 1 -b x`、`gh pr -Ro/r review 1 --comment -b x`、`gh issue -R o/r close 3` 都被拒绝；任何角色下 `gh pr -R o/r merge 3` 都被拒绝；`gh pr -R o/r view 3` 放行 | 夹具 | `…test_gh_value_options_do_not_shift_action` | 选项值被当成子命令，规则失效 |
| 不挂规格：B81 | 管道交给 shell 执行：执行方角色下，把 `printf "bin/dispatch review 12"`、`printf "./bin/dispatch signoff 12"`、`echo "bin/harness review pr 3"` 分别经管道交给 `sh` 或 `bash` 时都被拒绝；设计方角色下放行 | 夹具 | `…test_piped_to_shell_denied` | 引号内的命令名不命中正则 |
| 不挂规格：B81 | 执行方角色下，`bin/harness verify`、`bin/harness review-pack 3`、`bin/harness review-plan x.md`、`bin/dispatch run docs/plans/task-1-x.md`、`python3 .harness/engine/cli.py taskbook` 都放行；设计方角色下病灶命令全部放行 | 夹具 | `…test_allowed_commands_untouched` | 误伤自检或设计方 |
| 不挂规格：B81 | 既有测试全部通过、零修改 | 夹具 | `bin/verify --full` | 已有规则被破坏 |
| 不挂规格：B81 | 设计方 G2：在 Agent-Notification 的独立 worktree 中，用本任务的引擎执行 `upgrade` 后跑 `bin/verify --full`，与升级前的测试数、质量指标逐项一致 | 人工 | PR 评论 | 消费方行为被意外改变 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 结构化规则（harness、cli.py、解释器加 cli.py）、gh 带值选项、兜底正则去锚定，以及回归断言 | `engine/core/shell_structure.py`、`engine/guards/command_guard.py`、`tests/test_guard_review_entrypoints.py` | `python3 -W error::ResourceWarning -m unittest tests.test_guard_review_entrypoints -v` | 验收第 1–5 行 |
| 2 | 全量验证，整理交付证据 | `tests/` | `bin/verify --full` | 验收第 6 行 |
| 3 | 设计方 G2（人工，合并前） | —— | 消费方升级前后 `bin/verify --full` 对照 | 验收第 7 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收与定向变异复核，以下五个变异必须各自被对应断言抓住：
- 去掉解释器后接 `cli.py` 的分支；
- 恢复 `_gh` 只过滤 `-` 开头参数的旧写法；
- 恢复正则的行首、分隔符锚定；
- 把 `review` 的判定放宽到也拦截 `review-pack`；
- 去掉兜底正则。

同一失败连续三轮没有新证据时，停止这条路径。不得自行扩大白名单、修改任务书、实现非目标。
