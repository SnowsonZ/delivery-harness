---
task: T708
class: K7
risk: R3
designer: claude-code
size: medium
architecture: true
spec_refs: []
no_spec_reason: 逃逸 #128（PR #120 自动评审并发共用工作区）修复，加上 B81 试验前的评审成本与拆分收尾（关闭 Codex 子代理、B89、merge-app 落后判定加固），无产品规格验收编号
budget:
  wall_clock_min: 150
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T708：评审互斥（逃逸 #128）、评审关闭 Codex 子代理、T707 拆分收尾、merge-app 落后判定加固

负责方：**派发任务**。设计方：claude-code；独立评审方：Codex（单会话）。依据：逃逸 #128、审计 #124/#127、用户 2026-10-04 的决定（评审关闭 Codex 子代理；T708 按本范围起草）。**必须在 D1 之前合并。**

## 病灶（代码证据）

1. **#128 评审并发**：所有评审入口共用同一个工作区，没有任何互斥。`engine/agents/review.py` 的 `review_workspace(root, index=0)` 固定返回 `<仓库名>-review`（T702 合并后的 main 上约在第 293 行）。下列入口都直接 checkout、clean 并覆盖材料：
   - `review_pr`（函数内调用 `review_workspace(root)` 的那一行，约第 405 行）；
   - 较早的校准段 `calibrate`（`def calibrate` 约在第 601 行，函数内两处 `review_workspace` 调用；index 为 0 的那个工作区与 `review_pr` 共用）；
   - `review_calibration.review_calibrate`（第 150 行）；
   - `plan_review`（第 118 行）。

   2026-10-04 11:04，#119 的补审与 #120 的评审同时运行，#120 那次评审读到的全是 T707 的代码（见 #127）。T703 的 `review_after_ci` 让多个派发进程在 CI 通过后各自评审，D1 打开这个开关后，并发会成为常态。
2. **Codex 子代理**：`CodexReviewer.argv`（`engine/agents/review.py` 的 `class CodexReviewer`）没有约束子代理，所以会沿用使用者全局配置里的 `[agents]`。实测（2026-10-03/04 的会话日志）：每次评审另外派生 2 个子代理，子代理占评审输入 token 的 53%；9 条严重发现中，主会话自己就查到了 8 条。2026-10-04 用 `-c agents.enabled=false` 补审 #120，输入从平均 3.8M 降到 0.96M，同样查出了严重问题。
3. **B89（T707 收尾，#124 与 #127 的评审发现）**：
   - 移出模块之间仍按本模块的名字互相调用，在原模块上 patch 这些名字拦不住：`dispatch_slots.py` 第 75、113、138 行；`dispatch_text.py` 第 27、62 行；`review_calibration.py` 第 93、124–131、144、157、169、172 行。
   - `tests/test_split_modules.py` 第 116 行起的 `test_import_order_independent` 只清了 `sys.modules`，包属性 `engine.agents.<名字>` 还留着旧模块，所以「顶层回导入」这种写法也能通过（假阳性）。
4. **merge-app 落后判定**：T702 在批准**之前**用 `gh pr view --json mergeStateStatus` 判断 BEHIND。此时还没有批准，ruleset 要求的审批不满足，GitHub 可能返回 `BLOCKED` 而不是 `BEHIND`，那么落后的分支永远不会被同步。

## 目标终态

1. **评审互斥**：新增模块 `engine/agents/review_lock.py`，提供上下文管理器 `workspace_lock(root, workspace, timeout_seconds)`。
   - 实现方式与槽位锁相同：锁文件放在**评审工作区旁边**，路径为 `<工作区目录>.review.lock`（即 `workspace.parent / f"{workspace.name}.review.lock"`），用 `os.open(O_CREAT|O_EXCL)` 创建，内容为 `{"pid", "started_at", "purpose"}`。（修订记录 2：原稿放在 `dispatch.state_dir(root)` 下，需要 root 是 git 仓库；本仓库和 Agent-Notification 的 calibrate 测试都以非 git 临时目录作 root，加锁就崩。）
   - 锁已存在且持有进程仍存活时，每 2 秒轮询一次，等到超时就抛 `TimeoutError`，错误信息里写明持有者。持有进程已经不存在时，回收这把锁。
   - 退出时删除锁文件。`_alive` 通过 `dispatch._alive` 调用，保持 patch 语义；锁的路径只由工作区路径决定，不依赖 git。
   - 上面列出的四处入口，从 checkout 到「评论已发布 / 报告已写入」整段都在锁内执行；只锁 checkout 这一步不算数。较早那套校准 `calibrate` **按样本持锁**：每个样本从占用工作区（prepare_sample）到评审方结果落盘为一段，样本之间释放；index 为 0 的工作区与 `review_pr` 互斥，index 大于 0 的并行工作区各用各的锁文件，并行不受影响。超时取 `[review] timeout_minutes` 的两倍。
2. **评审关闭 Codex 子代理**：`CodexReviewer.argv` 在推理强度参数之后、`-C` 之前插入 `-c agents.enabled=false`。原有参数的位置保持不变：Agent-Notification 的契约测试断言 `argv[5]` 是模型名。
3. **B89 收尾**：
   - 移出模块里对**原本在同一模块中**的函数的调用，全部改为在函数内按需导入原模块，再以 `dispatch.<名字>` / `review.<名字>` 调用，范围覆盖病灶 3 列出的全部位置。
   - `test_import_order_independent` 改为在全新的 Python 子进程（`sys.executable -c`）中按「三个新模块 → 原模块」的顺序导入，判定看子进程的退出码。
   - 类型注解经 `typing.get_type_hints` 无法解析的问题列为非目标，见下文。
4. **merge-app 落后判定加固**：修改 `templates/.github/workflows/auto-merge.yml` 中 merge-app 的「Sync behind branch before approving」这一步。
   - **落后**：改用 `gh api repos/$GITHUB_REPOSITORY/compare/<默认分支>...<被评估的 head>` 的 `behind_by` 判断，大于 0 即为落后。默认分支取 `github.event.repository.default_branch`。
   - **冲突**：改用 `gh pr view --json mergeable` 等于 `CONFLICTING` 判断。
   - 其余行为保持 T702 不变：落后时用 App 令牌 update-branch 并退出；冲突时打 escalation 标签；两种情况都不批准、不合并；正常情况下批准与合并的命令逐字不变。
5. **回归测试**：
   - 新文件 `tests/test_review_lock.py`，用线程加事件编排，做到确定性、不依赖 sleep 时序。
   - `tests/test_split_modules.py` 中改写 `test_import_order_independent`，并新增 patch 语义断言。
   - `tests/test_signoff.py` 的 merge-app 夹具改为模拟 compare 与 mergeable。

## 白名单

- `engine/agents/review_lock.py`（新增）
- `engine/agents/review.py`（只改两处：四处入口中属于本文件的三处加锁；`CodexReviewer.argv`）
- `engine/agents/review_calibration.py`（加锁，改为通过原模块调用）
- `engine/agents/plan_review.py`（只加锁）
- `engine/agents/dispatch_slots.py`、`engine/agents/dispatch_text.py`（只改为通过原模块调用）
- `templates/.github/workflows/auto-merge.yml`（只改 merge-app 的同步步骤）
- `tests/test_review_lock.py`（新增）
- `tests/test_split_modules.py`（只改 `test_import_order_independent`，并新增 patch 语义断言）
- `tests/test_signoff.py`（只改 merge-app 夹具与 `test_merge_app_syncs_behind_branch` 的判定输入）
- `.harness/project/replay_cases.py` 不在本白名单内，由设计方在本 PR 里另行提交 E128-R1 的回放用例

消费方扫描（检查单第 2 项）：

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| Agent-Notification `tests/test_harness_review_independent.py` 第 73 行 `argv[5]` | 无需 | 新参数插在推理强度之后，第 0–5 位不变（实现前先核对一次；如果位置有冲突就停下报告） |
| 既有评审测试的假 GitHub、假评审方 | 实现前核对 | 加锁只多出本机文件操作，不增加 gh 调用；测试的 `state_dir` 如果指向共享目录，就改为临时目录（只改夹具，不改断言），有冲突就停下报告 |
| `tests/test_signoff.py` 的 merge-app 夹具 | **需配套，已列入白名单** | 判定输入从 mergeStateStatus 改为 compare 加 mergeable |
| `tests/test_split_modules.py` | **需配套，已列入白名单** | 修假阳性，并补 patch 语义断言 |
| 质量棘轮（检查单第 7 项） | 余量足够 | `review.py` 694 行加约 15 行；`review_lock.py` 新增约 80 行 |

CHANGELOG 由设计方在 D1 统一写入，本任务不改。

## 非目标

- `typing.get_type_hints` 解析移出函数的注解（#124 一般项）：注解只在 TYPE_CHECKING 下导入，运行时没有任何代码依赖它；要修就得在新模块顶层导入原模块，会引入循环导入。保持现状，在 B89 中注明原因。
- 每次评审使用独立工作区：用互斥实现串行即可满足 C0 的串行要求，独立工作区留给日后的并行需求。
- 不改评审提示词、材料包（B87、B88）。
- 白名单以外的文件一律不改。
- 共用合同 C0 同样适用。

## 前置条件

- 工作区基于最新的 `origin/main`（含 T702 #126，它改了 `review.py` 和 `auto-merge.yml`）。如果 #126 尚未合并，就停下报告。**按函数名与代码片段定位**，文中行号只作参考，偏差几行不算出入；只有函数、调用点或接口本身对不上时，才停下向设计方报告。
- 实现前先确认本任务书已在 main 上：`bin/harness taskbook docs/plans/task-708-review-mutex-and-cleanup.md --on-main` 通过。

## 验收

具名测试为本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：#128 | 两个线程同时对同一工作区调用 `review_pr`（假评审方在事件上阻塞）：第二个在第一个发布评论之前，不会执行 checkout，也不会写材料；第一个释放后，第二个才开始。两条评论各自绑定自己的 head，材料不串 | 夹具 | `tests.test_review_lock.ReviewLockTest.test_concurrent_reviews_serialized` | 第二个评审覆盖了第一个的工作区（#120 的原样） |
| 不挂规格：#128 | 持有者进程已不存在的锁文件会被回收；持有者仍存活时等到超时抛 `TimeoutError`，信息含持有者 pid 与用途；正常退出、异常退出都会删除锁文件 | 夹具 | `…test_stale_lock_reclaimed_and_timeout` | 死锁，或锁一直留着 |
| 不挂规格：#128 | `plan_review` 与两套校准都经过同一把锁（`calibrate` 按样本持锁）；校准 index 大于 0 的工作区各自独立加锁，互不阻塞；`tests/test_harness_contract_review.py` 的 calibrate 场景（非 git 临时目录作 root）**不改任何测试**照常通过 | 夹具 | `…test_all_entrypoints_use_lock` | 某个入口绕开了锁 |
| 不挂规格：B81 | `CodexReviewer("m","high").argv(...)` 含 `-c agents.enabled=false`，位于推理强度之后、`-C` 之前，第 0–5 位与原来一致 | 夹具 | `…test_codex_reviewer_disables_subagents` | 子代理照常派生 |
| 不挂规格：B89 | 在原模块上 patch `review.load_samples`、`dispatch._salvage_and_remove`、`dispatch._manual_section` 后，调用移出的函数会走到 patch 后的版本 | 夹具 | `tests.test_split_modules.SplitModulesTest.test_patch_semantics_for_internal_calls` | 移出模块仍然直接调用本模块里的名字 |
| 不挂规格：B89 | 在全新子进程里先导入新模块、再导入原模块，退出码为 0；把一个新模块改成顶层回导入时，子进程退出码不为 0 | 夹具 | `…test_import_order_independent` | 假阳性，顶层回导入也能通过 |
| 不挂规格：B81 | merge-app：compare 返回 `behind_by=2`、mergeStateStatus 为 `BLOCKED` 时，仍然同步并退出、不批准；`mergeable=CONFLICTING` 时打 escalation；正常时批准与合并逐字不变；步骤顺序断言保留 | 夹具 | `tests.test_signoff.SignoffTest.test_merge_app_syncs_behind_branch` | 落后时因为 BLOCKED 未被识别而卡住 |
| 不挂规格：#128 | 既有测试全部通过；`bin/verify --full`（含 E128-R1 回放）通过 | 夹具 | `bin/verify --full` | 回归 |
| 不挂规格：#128 | 设计方 G2：Agent-Notification 独立 worktree，升级前后 `bin/verify --full` 一致 | 人工 | PR 描述 | 消费方行为被意外改变 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 评审互斥模块与四处入口加锁，以及回归测试 | `engine/agents/review_lock.py`、`engine/agents/review.py`、`engine/agents/review_calibration.py`、`engine/agents/plan_review.py`、`tests/test_review_lock.py` | `python3 -W error::ResourceWarning -m unittest tests.test_review_lock -v` | 验收第 1–3 行 |
| 2 | Codex 关闭子代理 | `engine/agents/review.py`、`tests/test_review_lock.py` | 同上 | 验收第 4 行 |
| 3 | B89：移出模块改为通过原模块调用；导入顺序测试改为子进程 | `engine/agents/dispatch_slots.py`、`engine/agents/dispatch_text.py`、`engine/agents/review_calibration.py`、`tests/test_split_modules.py` | `python3 -W error::ResourceWarning -m unittest tests.test_split_modules -v` | 验收第 5–6 行 |
| 4 | merge-app 落后判定改为 compare 加 mergeable | `templates/.github/workflows/auto-merge.yml`、`tests/test_signoff.py` | `python3 -W error::ResourceWarning -m unittest tests.test_signoff -v` | 验收第 7 行 |
| 5 | 全量验证，整理交付证据 | `tests/` | `bin/verify --full` | 验收第 8 行 |

修复 #128 的提交带 `Defect: E128-R1`，`tests/test_review_lock.py` 中引用这个编号。

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收、G2，以及**按本任务书原样施加**的定向变异，下面每一项都必须被对应断言抓住：

- 去掉 `review_pr` 的加锁；
- 只在 checkout 时持锁，checkout 一完成就释放；
- 去掉 `calibrate` 的加锁；
- 去掉死锁回收；
- 去掉 `-c agents.enabled=false`；
- 把 `review_calibrate` 调用 `load_samples` 改回直接调用本模块的名字；
- 新模块改为顶层 `from engine.agents.review import make_reviewer`；
- merge-app 的落后判定改回 `mergeStateStatus == BEHIND`。

设计方在本 PR 中补提交 `.harness/project/replay_cases.py` 的 E128-R1 注入用例（注入点为「去掉 `review_pr` 的加锁」）。

同一失败连续三轮没有新证据时，停止这条路径。不得自行扩大白名单、修改任务书、实现非目标。

## 修订记录 1（2026-10-05，设计方：引用改为按函数名定位）

原稿的行号取自 T702 合并之前的 main，T702（#126）合并后，`review.py` 中的调用点向后偏移。前置条件原本要求「行号有出入就停下」，这会让执行方无谓地停工。现改为按函数名与代码片段定位，行号只作参考。范围、白名单、验收均不变。

## 修订记录 2（2026-10-05，设计方裁决执行方升级）

执行方在第 1 次派发中两度停下请求澄清：给 `review.py` 的 `calibrate` 加锁时，`tests/test_harness_contract_review.py`（不在白名单）以非 git 临时目录作 root，且没有 patch `dispatch.state_dir`，锁取不到目录而崩溃。设计方核对后发现，Agent-Notification 的 `tests/test_harness_review_independent.py` 第 213 行起有同样的场景，消费方契约 CI 会用新引擎跑它。

裁决（不采纳「改测试夹具」的方案，那需要两个仓库都改测试）：

- 锁文件改放在评审工作区旁边（`<工作区目录>.review.lock`），路径只由工作区决定，不依赖 git；两个仓库的测试都不用改。
- `calibrate` 按样本持锁，与执行方的推荐一致。
- 验收第 3 行补一条：上述测试场景不改测试也能通过。

白名单、其余目标终态、变异清单均不变；变异清单增加一项「去掉 `calibrate` 的加锁」。已完成的提交（dd6e923–803c2d9）保留，在此基础上续跑。


## 修订记录 3（2026-10-05，设计方裁决 #134 独立评审的两项严重发现）

#134 的 Codex 评审（head `3b6610a`）判「不通过」，设计方核实两项严重发现属实：

1. **锁机制本身有竞争**（设计缺陷，出在本任务书）：目标终态规定的「`O_CREAT|O_EXCL` 创建、内容写 pid、按 pid 判活回收」有两个窗口——①创建与写入之间文件为空，等待方读到空内容就当成死锁删掉并加锁，两方同时进入临界区，先退出的一方还会删掉后者的锁；②多个等待方同时回收同一把死锁时，后一方会删掉前一方刚建的新锁。执行方按任务书原样实现，测试也照任务书写，所以没覆盖到。
2. **落后且冲突时不升级**：`behind_by>0` 且 `mergeable=CONFLICTING` 时脚本先走同步，同步失败步骤退出，escalation 标签与评论都没发出；验收第 7 行只测了 `behind_by=0` 的冲突。

裁决：

- **锁改用内核文件锁 `fcntl.flock`**（README 已声明只支持 Linux 与 macOS）。`workspace_lock` 以 `O_CREAT|O_RDWR` 打开锁文件，`flock(LOCK_EX|LOCK_NB)` 轮询到超时；拿到锁后核对 `os.fstat(fd).st_ino == os.stat(lock).st_ino`，文件已被删除或换了 inode 就关掉重来（防止拿到前一持有者刚删除的旧 inode）；核对通过后截断并写入 `{"pid","started_at","purpose"}`，内容只用于超时提示，不再参与判活。释放时先删文件、再关闭描述符（仍持锁时删除，等待方靠 inode 核对重来）。持有进程退出时内核自动释放，原来的「按 pid 判活回收」随之去掉，`dispatch._alive` 不再被锁调用。
- 同一进程内两个线程各自 `open` 得到不同的打开文件描述，`flock` 照样互斥（不得改用按进程生效的 `fcntl.lockf`/`F_SETLK`）。
- **冲突优先于落后**：先判 `mergeable=CONFLICTING`，冲突就打 escalation 标签、发评论，不同步、不批准、不合并；不冲突且 `behind_by>0` 才同步。
- 白名单不变（改动只在 `engine/agents/review_lock.py`、`templates/.github/workflows/auto-merge.yml`、`tests/test_review_lock.py`、`tests/test_signoff.py`）。

验收调整（其余行不变）：

| 编号 | 验收内容 | 证据类型 | 覆盖 | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：#128 | 替换验收第 2 行：锁文件残留但无人持有（持有进程已退出）时直接拿到锁并覆盖持有信息；另一个描述符持有 `flock` 时等到超时抛 `TimeoutError`，信息含持有者 pid 与用途，且不删除对方的锁文件；正常退出、异常退出都删除锁文件 | 夹具 | `…test_stale_lock_reclaimed_and_timeout` | 死锁，或锁一直留着 |
| 不挂规格：#128 | 创建窗口：持有方已拿到 `flock` 但锁文件仍为空（尚未写入持有信息）时，等待方不得进入、不得删除锁文件 | 夹具 | `…test_lock_held_before_info_written` | 空内容被当成死锁（评审发现①） |
| 不挂规格：#128 | 多等待方：至少 4 个线程反复争用同一把锁，用事件与计数器断言临界区从不重叠，结束后锁文件不存在；持有方删除文件后、等待方拿到旧 inode 时必须重来 | 夹具 | `…test_many_waiters_never_overlap` | 回收或释放时删掉他人的新锁（评审发现②） |
| 不挂规格：B81 | 补入验收第 7 行：`behind_by=2` 且 `mergeable=CONFLICTING` 时打 escalation 标签并评论，步骤成功结束，不调用 `update-branch`、不批准、不合并 | 夹具 | `tests.test_signoff.SignoffTest.test_merge_app_syncs_behind_branch` | 先同步、同步失败后不升级 |

变异清单调整：「去掉死锁回收」改为「去掉 inode 核对」与「改回 `O_EXCL` 创建后再写内容、按 pid 判活」两项；新增「冲突判定排到落后同步之后」。E128-R1 回放用例（注入点仍是去掉 `review_pr` 的加锁）不变。

已完成的提交（dd6e923–3b6610a）保留，执行方在此基础上续跑；派发时附上 #134 的评审意见。
