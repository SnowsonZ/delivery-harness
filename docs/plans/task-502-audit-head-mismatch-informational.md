---
task: T502
class: K5
risk: R2
designer: codex
size: small
architecture: true
spec_refs: []
no_spec_reason: 待办 B124（回主线前对真实派发的 #201 预演 B46 的 G3 时发现：audit 把 load_ci 的 head_mismatch 当 error）；用户 2026-10-08 决定先修再回主线
budget:
  wall_clock_min: 600
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T502：`audit` 与 `trace --ci` 不再把「旧 head 的运行未导入」当成失败（B124）

负责方：**派发任务**。设计方：Codex；独立评审由现行评审链中的非 Codex 席位承担（Claude Code 优先、OpenCode 候补）。2026-10-08 用户确认 Codex 接手 P5，故修订设计方席位；依据：待办 B124。**路径全部落在 `autonomy.toml [contract_route] allowed` 内**（`engine/**`、`tests/**`、`CHANGELOG.md`）。

**编号与所属期**：属 B46 的 **P5**，编号 `T502`（P5 已有 T501）。它修的是 P4 交付的 `audit`，由回主线前预演 P6 的 G3 时发现，且必须先于 P6 的 T601（T601 的全链夹具含「CI 重跑」，旧 head 运行会让「audit 无发现」断言失败）；与 T501 互不依赖、可并行。编号依据见 `docs/task-splitting.md`「任务编号」。

**已有测试的窄例外**：`docs/task-splitting.md` 的拆分检查通常不让执行方改已有测试；本任务的已合并任务书（#208）明确批准只更新 `tests/test_gh_json_fields.py` 中旧 `headSha` 形状断言，因为 `load_ci` 对畸形记录的诊断将从 `head_mismatch` 改为 `api`。白名单和下文限定的那一段之外不得改已有测试；这不是放宽其他任务的拆分规则。

## 病灶（真实 GitHub 上的预演，设计方 2026-10-08）

`load_ci`（`engine/core/events_io.py`）对一个 PR 只导入**与 PR 当前 head 一致**的运行；PR 曾推送过新提交、且查询结果里仍有这些旧 head 的可信运行时，它们是预期的历史，不导入，并汇总成一条 `head_mismatch` 发现（「N 个其他 head 的运行未导入」，N 是**不同 head 的个数**，不是运行数）。判定运行（`workflow_run` 触发）按运行名里的被评估 head 关联 PR，包自身的 API head 是 `main` 的提交，与此无关。

两个调用方把这条预期历史当成了失败：

1. `engine/reports/audit.py::_import_ci`：对 `load_ci` 的所有发现，除 `artifact_expired`、`hash_mismatch` 外一律映射成 `reference_unavailable` **error**。实测 `audit 201`（T720 的 PR，评审修复时推送过新提交）：`[error] reference_unavailable … CI 事件包不可用（head_mismatch）`；单次推送的 #203 没有这条。
2. `engine/reports/trace.py`（`--ci` 路径，约 417 行）：`return 1 if result["findings"] else 0`，同一条发现让 `trace --ci` 退出 1。

后果：凡是「推送过新提交、旧 head 的可信运行仍在查询结果里」的 PR，`audit` 必带一条 error、`trace --ci` 必退出 1。B46 的 G3 要审计的 T601 实际派发 PR 几乎一定如此，会因此失败。

**还有一个隐患必须一并处理（设计评审 1 严重 1）**：`stale` 集合的计算是「`run.get("head_sha") != resolved` 的一切值」，运行记录里 `head_sha` **缺失**（得到 `None`）或形状不对的畸形 API 响应也会落进 `stale`，同样汇总成 `head_mismatch`。若按发现码整体放过，读取失败就会被降级成成功。所以信息性的只能是**合法旧 head**；畸形记录必须改报非信息性的 `api` 发现。

## 目标终态

0. **`load_ci` 区分「合法旧 head」与「畸形记录」**：算 `stale` 时只统计 `head_sha` 是 40 位小写十六进制（`SHA_RE`）且不等于目标 head 的运行；`head_sha` 缺失或形状不符的运行不进 `stale`，改为汇总成**一条** `api` 发现（文字如「N 个运行的 head_sha 缺失或形状不符，未导入」，N 是运行数），仍不导入。**范围限定**：这只改**分支查询里的可信运行**的 `stale` 来源与诊断；判定运行（`workflow_run` 触发，`events_judge`）仍按既有路径处理——运行名指向的评估 head 与 PR 当前 head 一致时由 `run_identity` 校验该运行自己的 API 身份、畸形则报 `api`，运行名指向合法旧评估 head 时仍计入 `head_mismatch` 的 `stale`，不进入身份校验。因此**整个结果里不保证只有一条 `api`**：同一个畸形运行若同时出现在两段查询里，分支段的汇总和判定段的身份校验各报一条，这是预期，验收不要求去重。导入逻辑（head 等于目标才导入）与返回形状不变。
1. **只有 `head_mismatch` 是「信息性」发现**，其余 `load_ci` 发现保持原有处理不变。`events_io.py` 与 `events_judge.py` 里现有的**非信息性**发现码共 20 个（加上 `head_mismatch` 一共 21 个）：`schema`、`api`、`anchor`、`privacy`、`artifact_missing`、`seq_conflict`、`package_corrupt`、`storage`、`source_mismatch`、`origin_mismatch`、`hash_mismatch`、`future_schema`、`chain_link`、`chain_head_mismatch`、`chain_gap`、`artifact_unsafe`、`artifact_name`、`artifact_hash`、`artifact_expired`、`anchor_mismatch`（设计方 2026-10-08 `grep` 的结果，实现前再扫一遍），**除 `head_mismatch` 外全部是非信息性的**。在 `engine/core/events_io.py` 新增一个模块常量 `INFORMATIONAL_FINDINGS = frozenset({"head_mismatch"})`，两个调用方共用，不各写一份。
2. `audit._import_ci`：发现码在 `INFORMATIONAL_FINDINGS` 内的**不产生 finding**（不进报告的 `findings`，不影响 `ok` 与退出码）。其余分支原样。
3. `trace --ci`：发现仍**全部打印**到 stderr（`head_mismatch` 也打印，让人看见有旧 head 的运行），也仍全部放进 `--json` 的 `ci.findings`；**退出码只由非信息性发现决定**：只有 `head_mismatch` → 退出 0；有任何其他发现 → 退出 1。
4. CHANGELOG「Unreleased」一行（英文）：`audit` no longer reports a `reference_unavailable` error, and `trace --ci` no longer exits 1, merely because older heads of a PR have CI runs that are not imported (`head_mismatch`); any other CI import finding is reported as before. **Migration:** none。

## 白名单

- `engine/core/events_io.py`（加常量 `INFORMATIONAL_FINDINGS`，改 `load_ci` 里 `stale` 的算法并加畸形记录的 `api` 发现；当前 788 行，**净增不超过 5 行，最终必须 ≤ 793**：`tests/test_events_judge_runs.py` 里 T720 加的永久断言要求 `events_io.py` 相对 773 行基线净增 ≤ 20，即 ≤ 793；设计评审 2 在内存里按本任务书实现，净增正好 5 行）
- `engine/reports/audit.py`（只改 `_import_ci`；当前 768 行，净增不超过 6 行，最终必须 ≤ 800）
- `engine/reports/trace.py`（只改 `--ci` 路径的退出码判定；当前 455 行，净增不超过 6 行，最终必须 ≤ 800）
- `tests/test_audit_head_mismatch.py`（新增，新测试全放这里）
- `tests/test_gh_json_fields.py`（**只改** `LoadCiRestShapeTest.test_matches_runs_by_rest_head_sha` 里「旧形状」那一段（`headSha` 键的假运行）的期望：改前断言一条 `head_mismatch`，改后应是**没有** `head_mismatch`、有一条 `api` 发现（head_sha 缺失的畸形记录），其余断言与前面「合法形状」那一段原样不动；这是本任务有意的接口变化，不是削弱断言）
- `CHANGELOG.md`

## 消费方扫描（命令与输出，设计方 2026-10-08 执行；本仓库 cdacde9）

```
$ grep -rn "head_mismatch" engine tests | grep -v "\.pyc"
engine/core/events_io.py:785 （load_ci 产生）  engine/core/events_judge.py（docstring 提及）
engine/reports/ledger.py:193 （评审 head 与合并 head 不一致——是另一个概念：账本的 review 缺项原因，与本任务无关，不改）
tests/test_events_judge_runs.py:806,814,828  tests/test_gh_json_fields.py:429,439（断言 load_ci 返回里有该发现——load_ci 的返回不变，不受影响）
tests/test_trace_events_cli.py:416（断言 --ci 的发现码为 artifact_expired、head_mismatch、package_corrupt 且退出码 1——有其他非信息性发现，仍退出 1，不受影响）
tests/test_ledger_equivalent_head.py 里的 head_mismatch 是账本 review 缺项原因，与 load_ci 无关；tests/test_audit_ledger.py:434 则直接断言账本的 CI 缺项原因 head_mismatch（见下表）
$ grep -n "load_ci" engine/reports/*.py
audit.py:515（_import_ci）  trace.py:411（--ci）  ledger.py（经注入的 client 调 load_ci，自己处理发现，见下）
```

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| `engine/reports/ledger.py:312` 对 `load_ci` 发现的处理 | **本任务不改，保持行为** | 账本构建调用 `load_ci`，下一行把全部发现转成 `missing`（项 `ci_events`、原因 `head_mismatch`，设计评审 1 在内存里复现）。账本 CLI 的退出码只取决于发布结果，不取决于 `ledger["missing"]`，所以不会因此失败；`audit` 里的「账本 verified」只是账本引用核对通过，不能证明没有该缺项。若日后要让账本也不再列该缺项，必须另行授权同时改 `ledger.py` 与下一行的既有测试，且只处理 `ci_events`，保留 review 的同名原因 |
| `tests/test_audit_ledger.py:434` | 无需改，必须仍通过 | 它**直接断言**账本里的 CI 缺项原因是 `head_mismatch`（不是 review 缺项；上一版扫描把它归错了类）。本任务不改账本，这条断言必须原样保持 |
| `tests/test_gh_json_fields.py` 旧形状一段 | 需要改（已列白名单） | 见白名单；畸形记录改报 `api` 后原断言不再成立 |
| `tests/test_trace_events_cli.py:416` 一例 | 无需改，必须仍通过 | 同时含非信息性发现，仍退出 1 |
| 其余引用 `load_ci` 返回值的测试 | 无需改 | `load_ci` 本身的返回不变 |

## 设计方的绕过与失效清单

| 可能的问题 | 处理 |
|---|---|
| 把所有 `load_ci` 发现都放过，真实的坏包、哈希不符被吞 | 只有 `head_mismatch` 在集合里；验收对其余每个发现码逐一断言仍产生 error / 仍使 `trace` 退出 1 |
| 两个调用方各写一份集合，日后漂移 | 常量放 `events_io`，两处引用；变异：在其中一处另写字面量，须被「改常量内容两处同时变化」的断言抓住 |
| `trace` 把信息性发现从输出里吞掉，人看不见旧 head 运行 | 验收断言 stderr 仍含「其他 head」文字，`--json` 的 `ci.findings` 仍含该条 |
| `audit` 报告里的 `coverage` 或其他字段因此变化 | 验收对只含 `head_mismatch` 的世界断言：`findings` 为空、`ok` 为真、`coverage` 与没有旧 head 运行时的同世界逐字相同 |
| 只含 `head_mismatch` 却同时有真失败（如 `artifact_expired`）时误判成功 | 验收有混合用例，且**两种排列顺序**（信息性在前、在后）都测：`head_mismatch` + `artifact_expired` → audit 仍有 `reference_expired` error，trace 仍退出 1；变异「只要存在信息性发现就整体放行」须被抓住 |
| 畸形 API 记录（`head_sha` 缺失、非 40 位十六进制）被归入 `head_mismatch` 后随信息性一起放过，读取失败降级成成功 | 目标终态 0：畸形记录改报非信息性的 `api` 发现；验收有缺字段、非法 SHA、合法旧 SHA 三者混合的用例，只有合法旧 SHA 计入 `head_mismatch` |
| 日后新增的发现码被默认当成信息性吞掉 | 集合只含 `head_mismatch`；验收含一个**未知发现码**（如 `something_new`）仍产生 error / 退出 1 的用例，另有对上面 20 个现有发现码的参数化覆盖 |

## 非目标

- 除「畸形记录的诊断变化」（目标终态 0）外，保留 `load_ci` 既有的发现、返回形状与分页；不改判定运行路径（`events_judge`）；不改账本构建；不处理 B114（评审材料里 CI 结论与 diff 的 head 窗口）。
- 白名单以外的文件一律不改；共用合同 C0 同样适用。

## 前置条件

- 工作区基于最新的 `origin/main`。按函数名定位，行号只作参考；只有函数、调用点或接口本身对不上时，才停下向设计方报告。

## 验收

具名测试为本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B124 | **audit**：PR 在旧 head 上有 harness 运行、当前 head 的运行与事件包齐全（用假 gh，真实 `load_ci` 路径，参考 `tests/test_events_judge_runs.py` 的夹具写法）→ `audit` 报告 `findings` 为空、`ok` 为真；同一世界去掉旧 head 运行后的 `coverage` 与之逐字相同 | 夹具 | `tests/test_audit_head_mismatch.py::AuditHeadMismatchTest::test_older_head_runs_are_not_a_finding` | 当前代码必报 `reference_unavailable` |
| 不挂规格：B124 | **audit 其余发现不变**：对 `events_io.py`、`events_judge.py` 现有的 20 个发现码做**参数化**子测试（直接让 `load_ci` 返回含该码的结果，不必造出每种真实故障）：`artifact_expired` → `reference_expired`、`hash_mismatch` → `hash_mismatch`、其余 18 个 → `reference_unavailable`，各恰一条 error；再加一个未知码 `something_new` → `reference_unavailable`；`head_mismatch` 与 `artifact_expired` 同时存在（**两种顺序**）→ 只剩 `reference_expired` 一条 | 夹具 | `::test_other_ci_findings_are_still_errors`（参数化）、`::test_mixed_findings_keep_only_the_real_ones`（两种顺序）| 全放过或全拒绝 |
| 不挂规格：B124 | **load_ci 的畸形记录**（分支查询）：运行记录 `head_sha` 缺失、非 40 位十六进制、合法旧 SHA 三种混在一起 → `head_mismatch` 只统计合法旧 SHA（N 为不同 head 个数），畸形记录汇总成一条 `api` 发现（N 为运行数），三者都不导入；`audit` 对此世界仍有一条 `reference_unavailable` error（来自 `api`）；同一个畸形运行同时出现在判定查询里时，两段各报一条 `api`（不要求去重），判定运行名指向合法旧评估 head 的畸形运行只计入 `head_mismatch` | 夹具 | `::LoadCiStaleTest::test_malformed_runs_are_api_findings_not_history` | 畸形记录被当成正常旧 head 放过 |
| 不挂规格：B124 | **trace --ci**：只有 `head_mismatch` → 退出 0，stderr 仍有「其他 head」，`--json` 的 `ci.findings` 仍含该条；对上面 20 个现有发现码与未知码 `something_new` 参数化：任一其他发现 → 退出 1；`head_mismatch` + 其他（**两种顺序**）→ 退出 1 | 夹具 | `::TraceHeadMismatchTest::test_exit_code_ignores_only_the_informational_finding` | 吞掉输出，或退出码仍为 1 / 全为 0 |
| 不挂规格：B124 | **共用常量**：`events_io.INFORMATIONAL_FINDINGS == frozenset({"head_mismatch"})`；`audit` 与 `trace` 都引用它（把常量 patch 成 `frozenset({"head_mismatch", "artifact_missing"})` 时两处行为随之变化；patch 成空集合时回到现状） | 夹具 | `::test_both_callers_share_the_events_io_constant` | 一处写字面量 |
| 不挂规格：B124 | 既有测试全部通过（含 `test_audit_ledger.py:434` 对账本 CI 缺项 `head_mismatch` 的断言原样保持）；`events_io.py`（净增 ≤ 5、最终 ≤ 793，`tests/test_events_judge_runs.py` 的体量断言须仍通过）、`audit.py`、`trace.py` 物理行数均 ≤ 800 | 夹具 | `bin/verify --full` | 回归或超标 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 先写测试（确认前几类在当前代码上失败），再改 `load_ci` 的 `stale`、加常量与两处调用方，并改 `test_gh_json_fields.py` 的旧形状期望 | `engine/core/events_io.py`、`engine/reports/audit.py`、`engine/reports/trace.py`、`tests/test_audit_head_mismatch.py`、`tests/test_gh_json_fields.py` | `python3 -W error::ResourceWarning -m unittest tests.test_audit_head_mismatch tests.test_trace_events_cli tests.test_audit_events -v` | 验收第 1–5 行 |
| 2 | CHANGELOG；全量验证，整理交付证据 | `CHANGELOG.md` | `bin/verify --full` | 验收第 6 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收、G2（CI 的 `consumer-contract`）与定向变异复核（变异复核前先确认未变异基线在副本里是绿的），以下变异必须各自被对应断言抓住：
- 把 `head_mismatch` 之外的发现码也加入集合（逐个：`artifact_expired`、`hash_mismatch`、`artifact_missing`、`origin_mismatch`、`package_corrupt`、`api`）；集合改成空（回到现状）；
- `audit` 对 `head_mismatch` 仍产生 finding；`trace` 退出码仍按全部发现判定；`trace` 不再打印该条；`--json` 的 `ci.findings` 丢掉该条；
- 两个调用方之一改用自己的字面量集合；
- `stale` 仍统计一切不等于目标 head 的值（畸形记录继续被当作旧 head）、畸形记录的 `api` 发现被去掉、`head_sha` 校验放宽成非空即可；
- 「只要存在信息性发现就整体放行」（混合用例两种顺序须各自失败）。

**合并并随自举升级生效之后**：设计方在真实仓库复跑 `bin/harness audit 201`，应不再有 `reference_unavailable`（只剩预期内的 `missing_route`，原因是 #201 的判定运行早于 `run-name` 同步），`bin/harness trace --ci 201` 退出 0。
