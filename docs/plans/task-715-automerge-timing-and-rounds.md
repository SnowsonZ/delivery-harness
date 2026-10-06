---
task: T715
class: K5
risk: R2
designer: claude-code
size: medium
architecture: true
spec_refs: []
no_spec_reason: 端到端验收 G 暴露的缺陷（待办 B102、B104）；用户 2026-10-06 决定先修，并决定改用 GitHub 原生自动合并
budget:
  wall_clock_min: 150
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T715：改用 GitHub 原生自动合并（B102）；CI 轮次不计同步合并（B104）

负责方：**派发任务**。设计方：claude-code；独立评审方：Codex（单会话）。依据：待办 B102、B104；用户 2026-10-06 决定先修这两项，并在比较两套方案后决定改用 GitHub 原生自动合并（仓库设置 Allow auto-merge 由用户打开）。

## 病灶（代码证据）

- **B102**：`templates/.github/workflows/auto-merge.yml` 只在 harness 完成时触发，merge-app 批准后用工作流令牌立即 `gh pr merge`：
  - 合并只在这一个时刻尝试一次。必需检查（`ci` 的 macOS 测试常比 harness 晚 2–5 分钟）未完成、合并状态暂时未知、main 刚好前进时都会被拒，之后没有触发，PR 卡住。G 实测：#147 两次、#160 一次。
  - 工作流令牌推送到 main 不会触发任何工作流，一个 PR 自动合并后，其他等待中的 PR 落后了也没人同步。
  - 判定与合并分在两个 job，中间的空档里出现的否决看不到。
- **B104**：`engine/routing/policy.py` 的 `branch_rounds(branch, gh)` 把同步合并提交也算一轮；`gather` 调用时没有传 `cwd` 与主线。G 实测：#144 两次同步后超预算转人审。

## 设计取舍（为什么改用原生自动合并）

前三稿想在现有「判定后立即合并」上补触发、补状态判定，三轮设计评审都在补同一类问题：复刻 GitHub 的合并判定与状态推进。原生自动合并把「什么时候能合并」交给 GitHub（必需检查、test merge commit、提交状态、合并钩子、状态暂时未知都由它处理），我们只做三件事：开启时判定、出现否决时关闭、main 前进时同步落后的 PR。批准仍绑定评估过的 head：新推送会让批准作废，没有新批准就不会合并；审计以「合并者类型为 Bot」判定自动合并，合并者改为同步 App 后仍是 Bot。

## 目标终态

### B102：原生自动合并

1. **merge-app（approval = app）**：落后与冲突的预检查、批准步骤保留不变。原来的立即合并一步改为**开启原生自动合并**：
   - 用同步 App 的令牌执行 `gh pr merge "$PR" --repo "$GITHUB_REPOSITORY" --auto --merge --match-head-commit "$HEAD_SHA"`。由同步 App 开启，GitHub 合并时以同步 App 的身份推送，main 的 `push` 会触发工作流。
   - 未配置同步 App 时用批准 App 的令牌开启（批准 App 无写代码权限时开启会失败，按下一条处理）。
   - 开启失败（仓库未打开 Allow auto-merge、权限不足等）：退回现在的立即合并（`gh pr merge "$PR" --repo "$GITHUB_REPOSITORY" --merge --match-head-commit "$HEAD_SHA"`，命令逐字不变），并在 PR 上评论一次开启失败的原因与「请打开仓库设置 Allow auto-merge」。
   - 抽样审计（`AUDIT=true`）：不再在这里开 audit 议题，改为给 PR 打 `audit:pending` 标签，合并后再开（见第 4 条）。
2. **merge-direct（approval = none）**：同样改为开启原生自动合并（工作流令牌），失败时退回立即合并。单账号模式下合并不会触发 main 的工作流，第 3 条的同步链在这种模式下不生效，写进 SECURITY.md 已有的单账号风险说明之外的 CHANGELOG 条目即可，不另做处理。
3. **main 前进时同步等待中的 PR**：auto-merge 工作流新增 `push`（默认分支）触发和对应 job：列出仍打开、目标为默认分支、head 在本仓库、**已开启原生自动合并**（`autoMergeRequest` 非空）的 PR；对其中落后且不冲突的，用同步 App 的令牌 `gh pr update-branch`。没有配置同步 App 时跳过。不检出、不执行 PR 代码，权限最小。
4. **合并后与自动合并被 GitHub 关闭时**：新增 `pull_request_target` 的 `closed`、`auto_merge_disabled` 触发和对应 job（只用默认分支上的定义，不检出 PR 代码）：
   - `closed` 且已合并、带 `audit:pending`：按原来的正文开 audit 议题，移除 `audit:pending`；
   - `auto_merge_disabled`：在 PR 上评论 GitHub 给出的原因；原因是冲突时打 escalation 标签。
5. **出现否决时关闭自动合并**（引擎侧，`engine/agents/github.py` 新增 `disable_auto_merge(pr)`，执行 `gh pr merge <pr> --disable-auto`，PR 没开自动合并或调用失败时只打印、不抛异常）：
   - `review.py`：发布的独立评审结论不是「通过」（不通过、需用户验收）时调用；
   - `signoff.py`：复核结论为「不通过」时调用；
   - `dispatch.py`：打 `budget-exceeded` 标签时调用；
   - 工作流：每次判定结果为「不自动合并」时（request-review 一侧），用工作流令牌关闭该 PR 的自动合并。
6. **触发**：auto-merge 的 `workflow_run` 仍只监听 harness（原生自动合并会等 `ci` 等其余必需检查，不再需要监听 `ci`）。

### B104：CI 轮次不计同步合并

7. `run_check` 新增 `is_sync_merge(sha, mainline, cwd)`：提交有 ≥ 2 个父提交，且除第一个父提交外，**其余所有**父提交都是 `mainline` 的祖先（或等于它）。提交取不到、git 出错时返回假（照常计入）。
8. `branch_rounds(branch, gh, cwd=ROOT, mainline=None)`：`mainline` 为 `None` 时行为与现在完全相同；给出时排除同步合并的 head。`gather` 调用时传入自己的 `cwd` 与 `base`。同一提交的重跑仍只算一轮。
9. 已接受的风险（写进代码注释与 CHANGELOG）：执行方可以把修复改动夹带进同步合并提交，让这一轮不计入预算。预算是成本控制，不是安全边界；改动仍完整出现在 diff 中，照常经过评审与复核。

### 其他

10. CHANGELOG「Unreleased」：行为变化各一行；`**Migration:**`——打开仓库设置 Allow auto-merge（不开时退回立即合并并提示），重新复制 `auto-merge.yml`（新增 `push`、`pull_request_target` 触发与对应 job）。README「Platform setup」补 Allow auto-merge。
11. 本仓库 `.github/workflows/auto-merge.yml` 的同步属于 R3，由设计方在后续升级 PR 中提交。

## 白名单

- `templates/.github/workflows/auto-merge.yml`
- `engine/agents/github.py`（只加 `disable_auto_merge`）
- `engine/agents/review.py`、`engine/agents/signoff.py`、`engine/agents/dispatch.py`（只在上面列出的三处调用 `disable_auto_merge`）
- `engine/routing/run_check.py`（只加同步合并判定）
- `engine/routing/policy.py`（只改 `branch_rounds` 与 `gather` 对它的调用）
- `tests/test_native_automerge.py`（新增：工作流回放与静态断言、否决时关闭）
- `tests/test_automerge_rounds.py`（新增）
- `tests/test_signoff.py`、`tests/test_sync_app.py`、`tests/test_ci_events_workflows.py`、`tests/test_alert_cli.py`、`tests/test_install.py`（只同步因合并步骤与触发变化而失效的断言口径，原有安全断言——权限、可信代码、ruleset 一致性——保留）
- `tests/test_ci_workflows.py`（只补断言）
- `README.md`、`CHANGELOG.md`

## 消费方扫描（命令与输出，设计方 2026-10-06 执行）

```
$ grep -rn "branch_rounds\|ci_rounds(\|check_ci(" engine tests
engine/routing/policy.py:275:def branch_rounds(branch: str, gh=_gh) -> int | None:
engine/routing/policy.py:284:    return run_check.ci_rounds([run for run in runs if run.get("event") == "pull_request"])
engine/routing/policy.py:369:        facts.run_findings = run_check.check(base, head, branch, cwd, rounds=branch_rounds(branch, gh))
engine/routing/run_check.py:551:def ci_rounds(runs: list[dict]) -> int:
engine/routing/run_check.py:557:def check_ci(rounds: int | None, header: dict) -> Finding:
engine/routing/run_check.py:606:        findings.append(check_ci(rounds, target.header))
engine/reports/metrics.py:67:        "rounds": run_check.ci_rounds(runs),
engine/reports/metrics.py:97:            finding = run_check.check_ci(runs["rounds"] if runs else None, target.header)
tests/test_ci_workflows.py:190:    def test_branch_rounds_counts_distinct_commits_across_listed_workflows(self):
tests/test_ci_workflows.py:197:            self.assertEqual(policy.branch_rounds("b", gh), 2)
tests/test_ci_workflows.py:199:    def test_branch_rounds_is_unknown_when_github_cannot_be_read(self):
tests/test_ci_workflows.py:204:            self.assertIsNone(policy.branch_rounds("b", gh))

$ grep -rln "Label, approve and merge\|merge-app\|gh pr merge" tests
tests/test_events_equivalence.py
tests/test_signoff.py
tests/test_audit_completeness.py
tests/test_sync_app.py

$ grep -n "permissions" -A6 templates/.github/workflows/auto-merge.yml (merge-app 任务)
6:    permissions:
7:      contents: write
8:      issues: write  # 类别标签与 audit 议题
9:      pull-requests: write

$ grep -rln "auto-merge.yml" tests
tests/test_alert_cli.py
tests/test_ci_events_workflows.py
tests/test_signoff.py
tests/test_sync_app.py
tests/test_contract_route.py
tests/test_install.py
tests/test_trace_events_cli.py

$ grep -rn "def replay\|def _replay\|steps\[\|\"steps\"" tests/test_ci_events_workflows.py | head
tests/test_ci_events_workflows.py:454:    def replay_context(self) -> dict[str, str]:

$ grep -rn "policy.gather(\|gather(" tests | cut -c1-120
tests/test_run_record_privacy.py:403:                facts = policy.gather(base, head, None, cwd=self.repo, autonomy=aut

$ grep -rn "\"cli.py\", *\"[a-z-]*\"\|COMMANDS\|wait-checks" engine/cli.py | head -3
16:COMMANDS = {
18:    "verify": "engine.checks.verify",
19:    "hygiene": "engine.checks.hygiene",

$ grep -rn "budget-exceeded\|BUDGET_LABEL" engine --include=*.py
engine/core/shell_structure.py:39:LABEL_ERASE = "撤下或删改 escape、audit、escalation、budget-exceeded、class:K* 标签会让误差预算与
engine/core/shell_structure.py:43:PROTECTED_LABELS = re.compile(r"(^|,)\s*(escape|audit|escalation|budget-exceeded|class:K\d)\s*(,|$)")
engine/agents/dispatch.py:12:  8 等 CI     计 CI 轮次；失败且未超预算带 CI 摘要回到 5，超预算打 budget-exceeded 标签
engine/agents/dispatch.py:527:                self.github.add_label(pr, "budget-exceeded")
engine/routing/policy.py:10:  3. 预算     该类最近 window 次合并中的 escape 议题不超过 max_escapes；PR 不带 budget-exceeded 标签
engine/routing/policy.py:85:BUDGET_LABEL = "budget-exceeded"
engine/routing/policy.py:173:    elif BUDGET_LABEL in facts.labels:
engine/routing/policy.py:174:        rules.append(Rule("预算", False, f"PR 带 {BUDGET_LABEL} 标签"))
engine/guards/command_guard.py:78:        r"\bgh\s+(issue|pr)\s+edit\b[^;&|]*--remove-label[\s=][^;&|]*\b(escape|audit|escalation|budget-exceeded)\b",
engine/guards/command_guard.py:83:        r"\bgh\s+label\s+(delete|edit)\b[^;&|]*\b(escape|audit|escalation|budget-exceeded|class:K\d)\b",
engine/reports/weekly.py:49:    "budget-exceeded": ("d93f0b", "超出任务预算，不自动合并"),

$ grep -rn "def post_marker\|independent-review.*comment\|def _post\|gh\", \"pr\", \"comment\"\|\"comment\"" engine/agents/review.py engine/agents/signoff.py | head

$ grep -n "merger_type\|auto_merged" engine/reports/*.py
engine/reports/audit_completeness.py:9:（2026-10-02 任务书修订口径），设计方 PR 不要求派发记录。「自动合并」取
engine/reports/audit_completeness.py:121:        self.auto_merged = merge_out.get("merger_type") == "Bot"
engine/reports/audit_completeness.py:165:    if config["require_route_for_auto"] and facts.auto_merged and not facts.route_result_ok:
engine/reports/audit_completeness.py:189:    if mode != "app" or not facts.auto_merged:
engine/reports/github_events.py:256:             "merger": _text(merged_by.get("login")), "merger_type": _text(merged_by.get("type")),
engine/reports/github_events.py:263:    outputs = {"pr": pr, **approval, "merger": facts["merger"], "merger_type": facts["merger_type"],
```

结论：

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| `engine/reports/audit_completeness.py:121`（`merger_type == "Bot"` 判自动合并） | 无需 | 原生自动合并由同步 App 执行，合并者类型仍是 Bot；批准者仍是批准 App、绑定 head 不变 |
| `engine/reports/github_events.py:256`（记录合并者） | 无需 | 只记录事实 |
| `engine/agents/dispatch.py:527`（打 budget-exceeded） | 需要 | 目标终态 5 |
| `engine/agents/review.py:454`、`engine/agents/signoff.py:119`（发布评审与复核标记） | 需要 | 目标终态 5 |
| `engine/routing/policy.py` 的 `gather` → `branch_rounds` | 需要 | 目标终态 8 |
| `engine/reports/metrics.py:67、97` | 本任务不改 | 指标侧没有本地 git 历史；口径差异由设计方记入待办 |
| `tests/test_ci_workflows.py:190、199` | 无需修改既有用例 | 新参数有默认值，行为不变 |
| `tests/test_run_record_privacy.py:403`（调用 `policy.gather`） | 实现前核对 | 新增的传参对它透明则无需改；失败就停下报告 |
| `tests/test_signoff.py`、`tests/test_sync_app.py`、`tests/test_ci_events_workflows.py` | 需要 | 合并步骤改为开启自动合并、新增触发与 job，回放随之调整 |
| `tests/test_alert_cli.py:564`（断言工作流触发集合）、`tests/test_install.py:117`（断言模板字面量） | 需要（已列入白名单） | 新增 `push`、`pull_request_target` 触发后，原断言口径失效；只同步口径，安全断言保留 |
| `tests/test_contract_route.py`、`tests/test_trace_events_cli.py`、`tests/test_events_equivalence.py`、`tests/test_audit_completeness.py` | 实现前核对 | 若只断言 judge、alert 或模板存在，不受影响；有失败就停下报告 |

## 设计方的绕过与失效清单

| 可能的问题 | 处理 |
|---|---|
| 必需检查未完成就合并 | 合并时机交给 GitHub；验收第 1 行 |
| 自动合并开启后出现否决（评审或复核不通过、超预算），GitHub 仍合并 | 发出否决的地方关闭自动合并；每次判定不合格时关闭；验收第 5、6 行。残余风险：关闭操作本身失败——与现方案「判定到合并之间的空档」相当，列为已接受的风险 |
| 新推送后批准作废，你若手动批准就立即合并 | 新 head 判定不合格时关闭自动合并；你的手动批准属于人审通过，合并符合预期 |
| 自动合并后其他 PR 落后无人同步 | 由同步 App 开启，合并推送会触发 main 的 `push` job；验收第 3 行 |
| 同步了不该同步的 PR（未开自动合并、冲突、目标不是默认分支、来自 fork） | 只同步已开启自动合并、目标为默认分支、同仓库、落后且不冲突的；验收第 3 行 |
| 仓库没打开 Allow auto-merge | 开启失败时退回立即合并并评论提示；验收第 2 行 |
| `pull_request_target` 执行 PR 代码 | 不检出、不执行 PR 代码，只调用 `gh`；验收第 4 行静态断言 |
| GitHub 自行关闭了自动合并（冲突等），无人知晓 | `auto_merge_disabled` 时评论原因，冲突时打 escalation；验收第 4 行 |
| 普通合并被误判为同步合并 | 其余**所有**父提交都须在主线上；验收第 7 行 |
| 夹带修复的同步合并少计一轮 | 已接受的风险（目标终态 9） |
| 取不到提交或查错仓库时少计 | 照常计入；从 `gather` 传入 `cwd` 与主线；验收第 8 行 |

## 非目标

- 不自写必需检查或合并状态的判定。
- 不改 ruleset、不改判定的其余条件。
- 不改 `metrics.py`、`ci.yml`、`harness.yml`，不改本仓库 `.github/`。
- 白名单以外的文件一律不改。
- 共用合同 C0 同样适用。

## 前置条件

- 工作区基于最新的 `origin/main`。按函数名定位，行号只作参考；只有函数、调用点或接口本身对不上时，才停下向设计方报告。

## 验收

具名测试为本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B102 | 回放 merge-app：批准之后用**同步令牌**执行 `gh pr merge … --auto --merge --match-head-commit <HEAD_SHA>`；不再出现不带 `--auto` 的立即合并（开启成功时）；落后、冲突的预处理与批准命令逐字不变 | 夹具 | `tests.test_native_automerge.NativeAutomergeTest.test_merge_app_enables_auto_merge` | 仍是一次性立即合并 |
| 不挂规格：B102 | 回放：开启失败（桩返回「auto merge is not allowed」）时退回立即合并（命令逐字不变）并评论一次提示；merge-direct 同样先开启、失败再退回 | 夹具 | `…test_fallback_when_auto_merge_unavailable` | 未开设置的仓库无法合并 |
| 不挂规格：B102 | 回放 main 的 `push`：已开启自动合并、目标默认分支、同仓库、落后且不冲突的 PR 用同步令牌 `update-branch`；未开自动合并的、冲突的、目标非默认分支的、来自 fork 的都不动；没有同步 App 时跳过 | 夹具 | `…test_push_syncs_auto_merge_prs` | 落后的 PR 无人同步，或同步了不该同步的 |
| 不挂规格：B102 | 回放 `pull_request_target`：`closed` 且已合并、带 `audit:pending` 时开 audit 议题并移除标签；`auto_merge_disabled` 时评论原因，冲突时打 escalation；静态断言这些 job 不检出 PR head、不执行引擎以外的 PR 代码 | 夹具 | `…test_post_merge_and_disabled_events` | 抽审丢失，或执行了 PR 代码 |
| 不挂规格：B102 | 引擎：独立评审结论不是「通过」、复核「不通过」、打 `budget-exceeded` 时，各调用一次 `gh pr merge <pr> --disable-auto`；结论为「通过」时不调用；调用失败只打印、不改变原有返回值 | 夹具 | `…test_negative_signals_disable_auto_merge` | 否决后 GitHub 仍自动合并 |
| 不挂规格：B102 | 回放：判定为「不自动合并」时（request-review 一侧）关闭该 PR 的自动合并 | 夹具 | `…test_judge_reject_disables_auto_merge` | 新 head 不合格时自动合并仍开着 |
| 不挂规格：B104 | 真实临时 git 仓库（默认分支不叫 main）：执行方提交 A、同步合并 M1、执行方提交 B、同步合并 M2，经 `gather` 只计 A、B 两轮；「第二父提交不在主线上」的普通合并、三父提交中只有部分在主线上的合并照常计入 | 夹具 | `tests.test_automerge_rounds.AutomergeRoundsTest.test_rounds_exclude_sync_merges_via_gather` | 同步合并被计入、普通合并被漏计，或查错仓库 |
| 不挂规格：B104 | 提交取不到、git 出错时照常计入；`mainline=None` 时与改动前一致；同一提交的重跑只算一轮 | 夹具 | `…test_unknown_commits_and_default_behaviour` | 取不到时少计，或默认行为改变 |
| 不挂规格：B102/B104 | 既有测试全部通过（白名单内的口径同步除外） | 夹具 | `bin/verify --full` | 回归 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 工作流：开启原生自动合并与退回、main `push` 同步、`pull_request_target` 的合并后与关闭事件、判定不合格时关闭；回放测试与既有断言口径同步 | `templates/.github/workflows/auto-merge.yml`、`tests/test_native_automerge.py`、`tests/test_signoff.py`、`tests/test_sync_app.py`、`tests/test_ci_events_workflows.py`、`tests/test_alert_cli.py`、`tests/test_install.py` | `python3 -W error::ResourceWarning -m unittest tests.test_native_automerge tests.test_signoff tests.test_sync_app tests.test_ci_events_workflows tests.test_alert_cli tests.test_install -v` | 验收第 1–4、6 行 |
| 2 | 引擎：否决信号关闭自动合并 | `engine/agents/github.py`、`engine/agents/review.py`、`engine/agents/signoff.py`、`engine/agents/dispatch.py`、`tests/test_native_automerge.py` | 同上 | 验收第 5 行 |
| 3 | 同步合并判定与计数，`gather` 传参 | `engine/routing/run_check.py`、`engine/routing/policy.py`、`tests/test_automerge_rounds.py`、`tests/test_ci_workflows.py` | `python3 -W error::ResourceWarning -m unittest tests.test_automerge_rounds tests.test_ci_workflows tests.test_run_record_privacy -v` | 验收第 7–8 行 |
| 4 | README、CHANGELOG；全量验证，整理交付证据 | `README.md`、`CHANGELOG.md` | `bin/verify --full` | 验收第 9 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收、G2 与定向变异复核，以下变异必须各自被对应断言抓住：
- 去掉 `--auto`（改回立即合并）；
- 用批准 App 而不是同步 App 开启（配置了同步 App 时）；
- 开启失败时不退回立即合并；
- main `push` 时同步未开自动合并的或冲突的 PR；
- `pull_request_target` 的 job 检出 PR head；
- 评审「需用户验收」时不关闭自动合并；
- 打 `budget-exceeded` 时不关闭自动合并；
- 判定不合格时不关闭自动合并；
- 同步合并判定把「其余所有父提交」改成「任一」；
- 取不到提交时改为不计入；
- `gather` 不传 `cwd`、主线。

本任务合并后，设计方随升级 PR 同步本仓库的 `auto-merge.yml`，再用一个新的 K5 任务重跑端到端验收 G。

同一失败连续三轮没有新证据时，停止这条路径。不得自行扩大白名单、修改任务书、实现非目标。

## 设计评审处理（派发前，Codex，2026-10-06）

前三轮（共 6 项严重、16 项一般、1 项建议）的发现，大多来自「判定后立即合并、再自己补触发和状态判定」这条路；改用原生自动合并后不再存在（非目标第 1 条）。仍然相关的发现及处理：

| 评审发现 | 处理 |
|---|---|
| 工作流令牌的合并不会触发 main `push`（第三轮严重） | 由同步 App 开启自动合并，合并以同步 App 身份推送；目标终态 1、3 |
| 判定后出现的否决看不到（第二、三轮严重） | 改为事件驱动：发出否决时关闭自动合并；目标终态 5 |
| 等待标签不能代表资格（第三轮） | 不再使用等待标签，以 GitHub 的 `autoMergeRequest` 作为候选索引 |
| `branch_rounds` 缺 `cwd` 与主线、新签名与既有用例冲突 | 新参数带默认值，`gather` 传入；目标终态 8 |
| 消费方扫描遗漏 `test_alert_cli`、`test_install` 的断言冲突（第三轮） | 已列入白名单并规定只同步口径 |
