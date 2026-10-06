---
task: T715
class: K5
risk: R2
designer: claude-code
size: large
architecture: true
spec_refs: []
no_spec_reason: 端到端验收 G 暴露的缺陷（待办 B102、B104）；用户 2026-10-06 决定先修，改用 GitHub 原生自动合并，并选定竞态方案 A
budget:
  wall_clock_min: 180
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T715：改用 GitHub 原生自动合并（B102）；CI 轮次不计同步合并（B104）

负责方：**派发任务**。设计方：claude-code；独立评审方：Codex（单会话）。依据：待办 B102、B104；用户 2026-10-06 决定先修这两项，改用 GitHub 原生自动合并（仓库设置 Allow auto-merge 已由用户打开），并选定**方案 A：接受「否决与开启之间」的残余竞态，开启前再判定一次**。

## 病灶（代码证据）

- **B102**：`templates/.github/workflows/auto-merge.yml` 只在 harness 完成时触发，merge-app 批准后用工作流令牌立即 `gh pr merge`：
  - 合并只尝试一次。必需检查（`ci` 的 macOS 测试常比 harness 晚 2–5 分钟）未完成、合并状态暂时未知、main 刚好前进时都会被拒，之后没有触发，PR 卡住（G 实测：#147 两次、#160 一次）。
  - 工作流令牌推送到 main 不触发任何工作流，一个 PR 合并后，其他等待中的 PR 落后了也没人同步。
- **B104**：`engine/routing/policy.py` 的 `branch_rounds(branch, gh)` 把同步合并提交也算一轮；`gather` 调用时没传 `cwd` 与主线（G 实测：#144 两次同步后超预算转人审）。

## 设计取舍

原生自动合并把「什么时候能合并」交给 GitHub（必需检查、合并状态暂时未知、test merge commit 都由它处理）；我们只做：开启前判定、否决时关闭、main 前进时同步等待中的 PR、停机时撤销存量请求。批准仍绑定评估过的 head：新推送作废批准，没有新批准就不会合并；审计以「合并者类型为 Bot」判定自动合并，由同步 App 或批准 App 开启的合并，合并者仍是 Bot。

**竞态方案 A（用户 2026-10-06 选定）**：否决（评审/复核不通过、严重发现、超预算）发出的时刻，可能恰好落在 merge-app「再判定」与「开启」之间，或原生合并恰好先于关闭调用执行；这两种交错不再用额外机制堵（第 4 轮评审严重 1 指出再堵就要把合同资格做成 GitHub 必需检查，会挡住用户手动合并）。缓解：①开启前在 merge-app 内用与 judge 相同的命令再判定一次，不通过就不批准、不开启；②否决发出处关闭自动合并；③窗口等于 merge-app 的「再判定 → 开启」之间（秒级）加上最后一个必需检查完成瞬间，写进 SECURITY.md 与 CHANGELOG 作为已接受的残余风险。

## 目标终态

### B102：原生自动合并

1. **merge-app（approval = app）**，步骤顺序：
   1. **再判定**（新增，最先执行）：检出默认分支、setup-python、`git fetch` 评估过的 head（只当数据），执行与 judge 完全相同的命令（`python .harness/engine/cli.py policy --base "origin/$BASE_BRANCH" --head "$HEAD_SHA" ${PR:+--pr "$PR"} --branch "$HEAD_BRANCH" --github`，id `recheck`）；该步骤的环境里只有 `GITHUB_TOKEN`，**不含任何 App 令牌**（令牌步骤放在它之后）。`recheck.outputs.auto_merge != 'true'` 时，之后的批准、开启一律跳过（job 仍以成功结束，打印原因）。
   2. 两个 App 令牌（批准、同步）、落后与冲突预检查：保持原样（含条件与注释）。
   3. **登记抽审**（`AUDIT=true` 时，在任何可能合并的命令之前）：幂等开 audit 议题——先 `gh issue list --label audit --state all --search 'in:title "Audit: PR #N"'` 并用 jq 精确比对标题，已存在则不再开。议题正文沿用原文，另加一句「登记于开启合并时；若该 PR 最终未合并，关闭本议题并注明」。
   4. **批准**：命令逐字不变。
   5. **开启**：令牌 = 配置了同步 App 时用**同步令牌**，否则用批准令牌；命令 `gh pr merge "$PR" --repo "$GITHUB_REPOSITORY" --auto --merge --match-head-commit "$HEAD_SHA"`。命令失败时先查 `gh pr view --json autoMergeRequest`，非空视为成功（已开启过）；仍为空才退回：
   6. **退回立即合并**：命令 `gh pr merge "$PR" --repo "$GITHUB_REPOSITORY" --merge --match-head-commit "$HEAD_SHA"` 逐字不变，**令牌与开启一致**（配置了同步 App 时用同步令牌，使退回合并也触发 main 的 `push` 工作流）；并在 PR 评论一次开启失败的原因与「请打开仓库设置 Allow auto-merge」。
2. **merge-direct（approval = none）**：登记抽审（同上）→ 工作流令牌开启 → 失败按同样规则退回立即合并并评论。单账号用工作流令牌合并，不会触发 main 的 `push`，第 3 条同步链在该模式下不生效，写进 CHANGELOG 与 SECURITY.md 单账号一节。
3. **main 前进时同步等待中的 PR**：auto-merge 工作流新增 `push`（默认分支）触发：
   - `platform` job（只在 `push` 事件运行）：检出默认分支，`python .harness/engine/cli.py automerge platform` 把 environment 名与同步 App 的变量名、密钥名写入 job 输出（取自 `policy` 已用的 `setting("platform", …)`，不另造配置读取）。
   - `sync-waiting` job（`needs: platform`，使用其 environment，权限 contents/pull-requests/issues 写）：同步 App 未配置则打印后结束。列出仍打开、目标为默认分支、head 在本仓库、`autoMergeRequest` 非空的 PR；对每个：先查 `mergeable`（`UNKNOWN` 时最多重试 3 次、每次间隔 10 秒，仍未知就打印并跳过）；`CONFLICTING` → 评论、打 `escalation` 标签、用工作流令牌 `gh pr merge --disable-auto`（冲突的 PR 不会自行消失，不关闭会一直挂着）；落后（compare `behind_by` > 0）且不冲突 → 同步令牌 `gh pr update-branch`。不检出、不执行 PR 代码。
   - 其余 job（judge、merge-app、merge-direct、request-review）已有 `workflow_run` 条件会自然跳过；**`alert` 的 `if: always()` 必须补上 `github.event_name == 'workflow_run'`**，否则 push 时会误跑告警。
   - `concurrency: group: auto-merge-push-sync, cancel-in-progress: false`，避免同时同步。
4. **否决时关闭自动合并**（引擎侧）：`engine/agents/github.py` 新增 `disable_auto_merge(pr)`，执行 `gh pr merge <pr> --disable-auto`；PR 没开自动合并或调用失败时只打印、**返回假，不抛异常**；成功返回真。调用点与条件：
   - `review.py`：发布评审评论之后，条件与合同判据一致——结论不是「通过」，**或** `flagged` 为真（含「通过」但带严重/阻断发现，`engine/routing/signals.py::review_status` 的 fail 条件）；
   - `signoff.py`：发布复核评论之后，条件与 `signals.signoff_status` 判 fail 一致（结论不是「通过」、mutations 缺失或 < 1、caught ≠ mutations）；
   - `dispatch.py`：打 `budget-exceeded` 标签之后；
   - 为避免判据复制，把 `review_status` 与 `signoff_status` 里对标记数据的判定抽成纯函数（`signals.py` 白名单内，只抽出、不改行为），引擎调用点与状态函数共用。
   - 工作流：request-review job（judge 判定为「不自动合并」）里，用工作流令牌对该 PR `gh pr merge --disable-auto`（`|| true`）；这一路覆盖 flagged、escalation、预算、新 head 等一切判定不合格的情形。
5. **停机与类别回收撤销存量请求**：禁用 auto-merge 工作流不会撤销 GitHub 上已开启的自动合并，类别改回 L3 或逃逸预算超限同样不会。新增命令 `python .harness/engine/cli.py automerge-off [--dry-run]`（新模块 `engine/agents/automerge.py`，同模块提供第 3 条的 `platform` 子命令；`automerge` 与 `automerge-off` 在 `cli.py` 的 COMMANDS 注册）：
   1. 列出 `auto-merge.yml` 中 `queued`/`in_progress` 的运行（它们可能还在开启自动合并），打印并提示先取消或等它们结束；
   2. 列出全部 `autoMergeRequest` 非空的打开 PR，逐个关闭；
   3. 关闭后重新查询，仍有开启的就列出并以非零退出，不得假装成功；`--dry-run` 只列出不改。
   - 同步修改：`engine/routing/policy.py` 的停机提示（原 `停机：Actions → auto-merge → Disable workflow。`改为「停机：Disable workflow 后再运行 `automerge-off` 撤销已开启的自动合并」，只改这一行文案）、`templates/.github/workflows/auto-merge.yml` 与 `templates/.harness/config/autonomy.toml` 文件头注释的停机说明、README「Platform setup」与 SECURITY.md。
6. **触发**：`workflow_run` 仍只监听 harness（原生自动合并会等 `ci` 等其余必需检查）。**不新增 `pull_request_target`**：GitHub 不会因冲突自行关闭自动合并（由第 3 条在 main 前进时处理），抽审已在合并前登记，合并后无需再处理。

### B104：CI 轮次不计同步合并

7. `run_check` 新增 `is_sync_merge(sha, mainline, cwd)`：提交有 ≥ 2 个父提交，且除第一个父提交外，**其余所有**父提交都是 `mainline` 的祖先（或等于它）。提交取不到、git 出错时返回假（照常计入）。
8. `branch_rounds(branch, gh, cwd=ROOT, mainline=None)`：`mainline` 为 `None` 时行为与现在完全相同；给出时排除同步合并的 head。`gather` 调用时传入自己的 `cwd` 与 `base`。同一提交的重跑仍只算一轮。
9. 已接受的风险（写进代码注释与 CHANGELOG）：执行方可以把修复改动夹带进同步合并提交，让这一轮不计入预算。预算是成本控制，不是安全边界；改动仍完整出现在 diff 中，照常经过评审与复核。

### 其他

10. CHANGELOG「Unreleased」：行为变化各一行；`**Migration:**`——打开仓库设置 Allow auto-merge（不开时退回立即合并并提示）；重新复制 `auto-merge.yml`（新增 `push` 触发与 `platform`、`sync-waiting` job、merge-app 再判定）；停机改用 `automerge-off`。README「Platform setup」补 Allow auto-merge 与停机说明；SECURITY.md 补第 3 条与方案 A 的残余风险。
11. 本仓库 `.github/workflows/auto-merge.yml` 的同步属于 R3，由设计方在后续升级 PR 中提交。

## 白名单

- `templates/.github/workflows/auto-merge.yml`、`templates/.harness/config/autonomy.toml`（只改头部停机注释）
- `engine/agents/github.py`（只加 `disable_auto_merge`）
- `engine/agents/automerge.py`（新增）、`engine/cli.py`（只加 COMMANDS 两项）
- `engine/agents/review.py`、`engine/agents/signoff.py`、`engine/agents/dispatch.py`（只在上面列出的三处调用 `disable_auto_merge`）
- `engine/routing/signals.py`（只把标记数据的判定抽成纯函数，行为不变）
- `engine/routing/run_check.py`（只加同步合并判定）
- `engine/routing/policy.py`（只改 `branch_rounds`、`gather` 对它的调用、一行停机文案）
- `tests/test_native_automerge.py`、`tests/test_automerge_rounds.py`、`tests/test_automerge_off.py`（新增）
- 因新增 `disable_auto_merge` 直接调用而需补桩方法的测试适配器（只加该方法，记录调用，不改既有断言）：`tests/test_review_lock.py`、`tests/test_alert_cli.py`、`tests/test_review_pack_taskbook.py`、`tests/test_harness_contract_dispatch.py`、`tests/test_events_agents.py`、`tests/test_dispatch_alerts.py`、`tests/test_review_after_ci.py`、`tests/test_run_timeline.py`、`tests/test_ci_workflows.py`、`tests/test_signoff.py`（后两者与下一条合并同一口径，补桩同样限定为只加方法）
- `tests/test_signoff.py`、`tests/test_sync_app.py`、`tests/test_ci_events_workflows.py`、`tests/test_alert_cli.py`、`tests/test_install.py`（只同步因合并步骤与触发变化而失效的断言口径，原有安全断言——权限、可信代码、ruleset 一致性——保留）
- `tests/test_ci_workflows.py`（只补断言）
- `README.md`、`SECURITY.md`、`CHANGELOG.md`

## 消费方扫描（命令与输出，设计方 2026-10-06 执行）

业务符号与模板引用（沿用前几稿，结论见下表）：

```
$ grep -rn "branch_rounds\|ci_rounds(\|check_ci(" engine tests
engine/routing/policy.py:275:def branch_rounds(branch: str, gh=_gh) -> int | None:
engine/routing/policy.py:369:        facts.run_findings = run_check.check(base, head, branch, cwd, rounds=branch_rounds(branch, gh))
engine/reports/metrics.py:67:        "rounds": run_check.ci_rounds(runs),
tests/test_ci_workflows.py:190:    def test_branch_rounds_counts_distinct_commits_across_listed_workflows(self):
（其余见 engine/routing/run_check.py:551、557）

$ grep -rln "Label, approve and merge\|merge-app\|gh pr merge" tests
tests/test_events_equivalence.py  tests/test_signoff.py  tests/test_audit_completeness.py  tests/test_sync_app.py

$ grep -rn "Audit: PR\|audit:pending" engine templates tests
templates/.github/workflows/auto-merge.yml（两处原建议）、tests/test_github_events.py:292、336（只构造议题数据，与创建时机无关）

$ grep -n "merger_type\|auto_merged" engine/reports/*.py
engine/reports/audit_completeness.py:121: self.auto_merged = merge_out.get("merger_type") == "Bot"
```

适配器扫描（第 4 轮评审一般 6 要求补）：临时在 `review.py`、`signoff.py`、`dispatch.py` 三处加直接调用 `disable_auto_merge`，跑 `python3 -W error::ResourceWarning -m unittest discover -s tests`，因缺方法失败的测试文件即白名单中「测试适配器」一项：

```
ERROR/FAIL 共 20 条，分布于 10 个测试模块（因 AttributeError 缺 disable_auto_merge）：
test_review_lock(2)  test_alert_cli(2)  test_review_pack_taskbook(2)  test_harness_contract_dispatch(1)
test_signoff(4)  test_events_agents(4)  test_dispatch_alerts(2)  test_review_after_ci(1)
test_ci_workflows(1)  test_run_timeline(1)
（设计方 2026-10-06 在临时工作树实测，三处调用为无条件调用；真实实现按目标终态 4 的条件调用，失败范围不会更大）
```

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| `engine/reports/audit_completeness.py:121` | 无需 | 合并者仍是 Bot；批准者仍是批准 App、绑定 head 不变 |
| `dispatch.py:527`、`review.py` 发布处、`signoff.py:119` | 需要 | 目标终态 4 |
| `engine/routing/policy.py` 的 `gather` → `branch_rounds`；停机文案 | 需要 | 目标终态 8、5 |
| `engine/reports/metrics.py:67、97` | 本任务不改 | 指标侧无本地 git 历史；口径差异由设计方记入待办 |
| 上表的测试适配器 | 需要（补桩方法） | 桩记录 `disable_auto_merge` 调用；**不得**用「缺方法时静默跳过」掩盖遗漏 |
| `tests/test_signoff.py`、`test_sync_app.py`、`test_ci_events_workflows.py`、`test_alert_cli.py`、`test_install.py` | 需要 | 合并步骤、新触发与 job 使回放与字面量断言变化 |
| `tests/test_run_record_privacy.py:403`（`policy.gather`）、`test_contract_route.py`、`test_trace_events_cli.py`、`test_events_equivalence.py`、`test_audit_completeness.py` | 实现前核对 | 只断言 judge、alert 或模板存在则不受影响；有失败就停下报告 |

## 设计方的绕过与失效清单

| 可能的问题 | 处理 |
|---|---|
| 必需检查未完成就合并 | 合并时机交给 GitHub；验收第 1 行；平台验收 P1 |
| 否决发出后旧判定仍开启自动合并（第 4 轮严重 1） | **方案 A：开启前再判定**（目标终态 1.1）+ 否决处关闭（4）；残余竞态记入 SECURITY.md；验收第 6、7 行 |
| 「通过」但带严重发现的否决漏掉（严重 2） | 关闭条件取合同判据（verdict 不是通过 **或** flagged），与 `signals` 共用纯函数；验收第 5 行 |
| 停机、类别回收后存量请求仍会合并（严重 3） | `automerge-off` 撤销并复查，列出在途运行；验收第 9 行；平台验收 P4 |
| 退回路径身份不触发 main push（一般 4） | 退回用与开启相同的令牌；验收第 2 行 |
| 立即合并时抽审议题丢失、工作流令牌路径无 closed 事件（一般 5） | 抽审在任何合并命令之前幂等登记，不依赖 `closed`；验收第 2 行 |
| 测试桩缺方法、静默跳过（一般 6） | 适配器扫描与白名单；桩记录调用并断言次数；验收第 5 行 |
| 端到端没有走真正的「等待」路径（一般 7） | 平台验收 P1–P4（设计方在真实仓库执行，见「交付与升级」）；回放桩对未登记的 gh 子命令**直接失败**，不静默成功 |
| 仓库没打开 Allow auto-merge | 开启失败退回立即合并并评论；验收第 2 行 |
| 同步了不该同步的 PR | 只处理已开启自动合并、默认分支、同仓库、落后且不冲突的；验收第 3 行 |
| push 事件误跑 `alert` | 补事件条件；验收第 3 行静态断言 |
| 冲突的自动合并 PR 一直挂着 | `sync-waiting` 对冲突者评论、打 escalation 并关闭；验收第 3 行 |
| 普通合并被误判为同步合并 | 其余**所有**父提交都须在主线上；验收第 7 行（B104） |
| 夹带修复的同步合并少计一轮 | 已接受（目标终态 9） |

## 非目标

- 不自写必需检查或合并状态的判定。
- 不改 ruleset、不改判定的其余条件、不新增 `pull_request_target`。
- 不改 `metrics.py`、`ci.yml`、`harness.yml`，不改本仓库 `.github/`。
- 白名单以外的文件一律不改。
- 共用合同 C0 同样适用。

## 前置条件

- 工作区基于最新的 `origin/main`。按函数名定位，行号只作参考；只有函数、调用点或接口本身对不上时，才停下向设计方报告。

## 验收

具名测试为本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。回放用的 gh 桩须对未登记的子命令直接失败。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B102 | 回放 merge-app：再判定在两个 App 令牌步骤**之前**，其环境不含 App 令牌；再判定结果为不合并时批准与开启都不执行；通过时批准之后用**同步令牌**执行 `gh pr merge … --auto --merge --match-head-commit <HEAD_SHA>`（未配置同步 App 时用批准令牌）；开启成功时不出现不带 `--auto` 的合并；落后、冲突预处理与批准命令逐字不变 | 夹具 | `tests.test_native_automerge.NativeAutomergeTest.test_merge_app_rechecks_then_enables` | 仍是一次性立即合并，或否决后仍开启 |
| 不挂规格：B102 | 回放：开启失败且 `autoMergeRequest` 为空时退回立即合并（命令逐字不变，令牌与开启一致，配置同步 App 时为同步令牌）并评论一次；开启失败但 `autoMergeRequest` 已非空视为成功不退回；merge-direct 同样先开启、失败再退回；抽审议题在**任何**合并命令之前登记，重复运行不重复开题，被抽中的 PR 无论走开启还是退回都有议题 | 夹具 | `…test_fallback_and_audit_registration` | 未开设置的仓库无法合并；抽审丢失或重复 |
| 不挂规格：B102 | 回放 main 的 `push`：已开启自动合并、目标默认分支、同仓库、落后且不冲突的 PR 用同步令牌 `update-branch`；冲突的评论、打 escalation 并 `--disable-auto`；`mergeable` 为 UNKNOWN 时重试后仍未知则跳过；未开自动合并的、目标非默认分支的、来自 fork 的都不动；未配置同步 App 时整体跳过；静态断言 `alert` 带 `workflow_run` 事件条件、`push` 事件下 judge 等 job 不运行、新增 job 不检出 PR head、不执行 PR 代码 | 夹具 | `…test_push_syncs_waiting_prs` | 落后的 PR 无人同步，同步了不该同步的，或 push 时误跑告警 |
| 不挂规格：B102 | 引擎：评审结论为「不通过」「需用户验收」或「通过」但 `flagged`（带严重发现）、复核判 fail（结论不通过、mutations < 1、caught ≠ mutations）、打 `budget-exceeded`，各调用一次 `disable_auto_merge`，次数与先后（评论/标签之后）可由桩断言；评审与复核全部通过时不调用；调用失败只打印、不改变原返回值；判据与 `signals` 的状态函数共用同一纯函数 | 夹具 | `…test_negative_signals_disable_auto_merge` | 否决后 GitHub 仍自动合并，或「通过＋严重发现」漏掉 |
| 不挂规格：B102 | 回放：judge 判「不自动合并」时 request-review 对该 PR `--disable-auto`；判合并时不调用 | 夹具 | `…test_judge_reject_disables_auto_merge` | 新 head 不合格时自动合并仍开着 |
| 不挂规格：B102 | `automerge-off`：列出在途 `auto-merge.yml` 运行；逐个关闭存量请求；关闭后复查，仍有则非零退出；`--dry-run` 不改；`platform` 子命令输出 environment 与变量名，取自 `setting("platform", …)` | 夹具 | `tests.test_automerge_off.AutomergeOffTest.*` | 停机后存量请求仍会合并 |
| 不挂规格：B104 | 真实临时 git 仓库（默认分支不叫 main）：执行方提交 A、同步合并 M1、执行方提交 B、同步合并 M2，经 `gather` 只计 A、B 两轮；「第二父提交不在主线上」的普通合并、三父提交中只有部分在主线上的合并照常计入 | 夹具 | `tests.test_automerge_rounds.AutomergeRoundsTest.test_rounds_exclude_sync_merges_via_gather` | 同步合并被计入、普通合并被漏计，或查错仓库 |
| 不挂规格：B104 | 提交取不到、git 出错时照常计入；`mainline=None` 时与改动前一致；同一提交的重跑只算一轮 | 夹具 | `…test_unknown_commits_and_default_behaviour` | 取不到时少计，或默认行为改变 |
| 不挂规格：B102/B104 | 既有测试全部通过（白名单内的口径同步与补桩除外） | 夹具 | `bin/verify --full` | 回归 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 工作流：再判定、开启与退回、抽审登记、`push` 同步、`alert` 事件条件、request-review 关闭；回放测试与既有断言口径同步 | `templates/.github/workflows/auto-merge.yml`、`tests/test_native_automerge.py`、`tests/test_signoff.py`、`tests/test_sync_app.py`、`tests/test_ci_events_workflows.py`、`tests/test_alert_cli.py`、`tests/test_install.py` | `python3 -W error::ResourceWarning -m unittest tests.test_native_automerge tests.test_signoff tests.test_sync_app tests.test_ci_events_workflows tests.test_alert_cli tests.test_install -v` | 验收第 1–3、5 行 |
| 2 | 引擎：`disable_auto_merge`、判据纯函数、三处调用、补桩 | `engine/agents/github.py`、`engine/routing/signals.py`、`engine/agents/review.py`、`engine/agents/signoff.py`、`engine/agents/dispatch.py`、桩所在测试、`tests/test_native_automerge.py` | 同上，加桩所在测试 | 验收第 4 行 |
| 3 | `automerge` / `automerge-off` 命令、`platform` 子命令、停机文案与说明 | `engine/agents/automerge.py`、`engine/cli.py`、`engine/routing/policy.py`、`templates/.harness/config/autonomy.toml`、`tests/test_automerge_off.py` | `python3 -W error::ResourceWarning -m unittest tests.test_automerge_off tests.test_install -v` | 验收第 6 行 |
| 4 | 同步合并判定与计数，`gather` 传参 | `engine/routing/run_check.py`、`engine/routing/policy.py`、`tests/test_automerge_rounds.py`、`tests/test_ci_workflows.py` | `python3 -W error::ResourceWarning -m unittest tests.test_automerge_rounds tests.test_ci_workflows tests.test_run_record_privacy -v` | 验收第 7–8 行 |
| 5 | README、SECURITY.md、CHANGELOG；全量验证，整理交付证据 | `README.md`、`SECURITY.md`、`CHANGELOG.md` | `bin/verify --full` | 验收第 9 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收、G2 与定向变异复核，以下变异必须各自被对应断言抓住：
- 去掉 `--auto`（改回立即合并）；
- 用批准 App 而不是同步 App 开启（配置了同步 App 时）；
- 开启失败时不退回立即合并；退回立即合并改用工作流令牌（配置了同步 App 时）；
- 去掉 merge-app 的再判定，或再判定放在令牌步骤之后；
- 抽审登记移到合并命令之后，或不做幂等；
- main `push` 时同步未开自动合并的或冲突的 PR；冲突的 PR 不关闭；
- `alert` 去掉事件条件；
- 新增 job 检出 PR head；
- 评审「需用户验收」时不关闭自动合并；「通过」但 `flagged` 时不关闭；
- 复核判 fail 时不关闭；打 `budget-exceeded` 时不关闭；
- request-review 判定不合格时不关闭；
- `automerge-off` 关闭后不复查；
- 同步合并判定把「其余所有父提交」改成「任一」；
- 取不到提交时改为不计入；
- `gather` 不传 `cwd`、主线。

**平台验收（设计方，本任务合并并升级本仓库后，在真实仓库执行；这是新的 K5 任务重跑验收 G 之外的补充，目的是证明确实走过原生「等待」路径）：**
- P1：故意让一个必需检查保持 pending，确认 `autoMergeRequest` 非空且开启者为同步 App；放行检查后核对合并者为 Bot、main 的 `push` 工作流被触发。
- P2：自动合并开启后推新提交，确认批准作废、不会合并，新一轮判定后重新批准并开启。
- P3：开启后发布「不通过」评审，确认 `autoMergeRequest` 被清空。
- P4：运行 `automerge-off`，确认存量请求被撤销并复查为零。

本任务合并后，设计方随升级 PR 同步本仓库的 `auto-merge.yml`，再用新的 K5 任务重跑端到端验收 G。

同一失败连续三轮没有新证据时，停止这条路径。不得自行扩大白名单、修改任务书、实现非目标。

## 设计评审处理（派发前，Codex）

| 轮次 | 发现 | 处理 |
|---|---|---|
| 1–3（共 6 严重、16 一般、1 建议） | 立即合并路线上的时序与状态判定、`branch_rounds` 签名、消费方遗漏等 | 改用原生自动合并后大多不再存在；保留的见下 |
| 1–3 | 工作流令牌合并不触发 main `push`；判定后出现的否决看不到；等待标签不能代表资格 | 同步 App 开启与退回都用同步令牌；否决处关闭；以 `autoMergeRequest` 作候选索引 |
| 4 严重 1 | 关闭挡不住旧判定随后开启；最后检查完成与关闭竞态 | 用户选方案 A：开启前再判定 + 残余窗口入 SECURITY.md（不把合同资格做成必需检查） |
| 4 严重 2 | 「通过＋严重发现」漏判 | 关闭条件为 verdict 不通过或 flagged，判据共用纯函数 |
| 4 严重 3 | 停机与类别回收不撤销存量请求 | `automerge-off` 撤销并复查，列出在途运行，同步说明与提示 |
| 4 一般 4 | 退回路径用工作流令牌 | 退回用与开启相同的令牌 |
| 4 一般 5 | 抽审迁移遗漏立即合并与工作流令牌路径 | 合并前幂等登记，取消 `closed` 处理，不新增 `pull_request_target` |
| 4 一般 6 | 测试桩缺 `disable_auto_merge` | 适配器扫描与白名单、桩记录调用 |
| 4 一般 7 | 端到端未走等待路径 | 平台验收 P1–P4，回放桩未登记命令即失败 |
