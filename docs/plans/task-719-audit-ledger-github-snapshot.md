---
task: T719
class: K5
risk: R2
designer: claude-code
size: small
architecture: true
spec_refs: []
no_spec_reason: 待办 B118（G3-lite 在 #188 上实测：audit 对内容寻址的 GitHub 事实快照链跨环境必然误报 ledger_mismatch）；用户 2026-10-07 决定先修再回主线
budget:
  wall_clock_min: 90
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T719：`audit` 的账本核对不再对 GitHub 事实快照误报，并补上快照缺失的核对（B118、B121）

负责方：**派发任务**。设计方：claude-code；独立评审方：Codex（单会话）。依据：待办 B118、B121；G3-lite 记录 `docs/review/g3-lite-t718.md`。**路径全部落在 `autonomy.toml [contract_route] allowed` 内**（`engine/**`、`tests/**`、`CHANGELOG.md`）。

## 病灶（真实 GitHub 上的实测）

`bin/harness audit 188`（#188，T718 合并后第一个带新引擎 CI 的 PR）报 2 条 `ledger_mismatch`（合并快照、抽审快照），其余全部通过。

`engine/reports/audit_completeness.py` 的 `_ledger` 把账本里每条链的 `head_hash` 与运行层同一 source 的链头、或运行层该 source 的任一事件哈希比对，都对不上就报错。`github:` 开头的 source 是**内容寻址**的（`github_events._snapshot_source`：`github:<PR号>:<规范化事实的 sha256>`，读取时间不进摘要），同一份事实在合并后的 main 工作流（生成账本的地方）和本机 `audit`（先 `github_events.sync` 重新同步，见 `audit.py` 的 `_sync_github`）各生成一次，**source 相同、事件内容相同，但事件的 `ts` 不同**（#188：工作流 10:25:07，本机 10:28:41）。事件哈希含 `ts`，链头必然不同，所以 `_ledger` 必然误报。这不是篡改，是内容寻址快照每个环境各记一份时间戳。`ci:` 链是 CI 事件包原样导入，哈希在各环境可复现，不受影响。

后果：每个有账本的 PR，`audit` 都带两条以上 error，真实的账本不一致被淹没。

## 目标终态

`_ledger` 对 `github:` 开头的链改为**语义核对**：账本里该 source 的全部事件，与运行层同一 source 的全部事件，逐条比较「不含环境相关字段」的内容，全部一致才算通过；**这类链是不可变快照，不再接受「链头在前缀里」的合法追加**（见下面第 3 条与设计评审 1）；`ci:` 等其他来源的链保持原有的链头核对不变。

1. 账本里的事件取自账本文档的 `stages`（每条是完整事件），运行层的事件取自 `auditor.stages`（`evidence == "event"` 的条目，已含 `inputs`、`outputs`、`decision`、`error`、`actor`、`seq`、`source`、`stage`、`step`、`status`）。
2. **比较键**（固定这九个字段，不多不少）：`seq`、`stage`、`step`、`status`、`actor`、`inputs`、`outputs`、`decision`、`error`；按 `source` 分组后组内比较（`source` 是分组键，不是被比较的字段）。**排除**：`ts`、`hash`、`prev_hash`、`duration_ms`、`engine_version`、`redacted`、`trace_id`（环境相关或由哈希链派生；`trace_id` 在本次审计里本来就是同一个）。两侧按 `seq` 排序后整体相等才通过：事件数量、顺序、每个字段值都必须一致。
3. **同一个 `github:` source 在账本与运行层都存在时，通过条件**只有两条（source 在运行层完全没有事件时不走这条，走第 6 条）：`head == 运行层该链当前链头`（内容逐字节相同，哈希含内容，所以等价于语义相同），或语义核对全部相等。**不再认「`head in 运行层任一事件哈希`（链头在前缀里）」**：快照按内容寻址、事实变了就另起新 source（`github_events` 的 `_fresh` 保证同 source 不会追加），所以 `github:` 链没有合法追加；若放行前缀，攻击者在合法的链上追加一条改了合并者的事件（链校验仍通过、旧链头仍在前缀里）就能被放过（设计评审 1 在内存里复现过）。`ci:` 与其他来源的两条快速通过路径与现状完全一致，不动。语义核对不通过报 `ledger_mismatch`，`reason` 写清差在哪一类（事件数量不同、第 N 条的哪个字段不同），不贴出事件内容的原文。
4. 账本里没有该 source 的事件（账本只记了链头、没有 `stages` 条目）：无法语义核对，除非链头等于运行层链头，否则报 `ledger_mismatch`（不放宽）。
5. **合并事实一致性：独立必检项，与「账本的 source 在不在运行层」无关（B121，本任务一并解决）**。对账本里每个 `github.merge` 事件，先取运行层中**同一 PR** 的全部 `step == "github.merge"` 事件作为候选：限定为 source 以 `github:<账本的 pr>:` 开头的事件（`audit.py` 按分支名收集事件，同一分支名被两个 PR 复用时，另一个 PR 的合并事件也会混进来，设计评审 4 用真实同步复现；source 前缀里就带 PR 号，不要按 head、合并提交或合并者过滤，否则会隐藏待检测的差异）。这个检查在第 3 条的快速通过**之前、之外**执行，不能被同 source 的链头相等或语义相等短路（设计评审 4 构造了「账本 A 与运行层 A 一致，运行层另有合并者不同的快照 C」，同 source 通过后根本不会再看 C）。
   - 通过条件：候选**至少一条**，且**全部**与账本的合并事件在**稳定身份投影**上相等（不是「任一条相等即通过」：设计评审 3 用真实同步构造了 A、加标签后的 B、合并者不同的 C，「任一相等」会让 B 掩盖 C）。
   - **稳定身份投影** = `stage`、`status`、`actor`、`decision`、`error`、`inputs`（PR 引用、合并提交、head）与 `outputs` 里的 `pr`、`merger`、`merger_type`、`merged_at`；这些在 PR 合并后不会合法变化。**例外与归一**：`inputs` 里的 PR 引用形如 `<owner>/<repo>#<N>`，GitHub 的仓库名不区分大小写，**比较时仓库部分一律转小写**（第 3 条的九字段语义核对对 `inputs` 里的同类引用采用同一归一，两处共用一个函数，设计评审 5 实测只改大小写会让两侧误判为不同）；仓库被**改名**则与 `merger` 改名一样，**有意接受**为差异（reason 写 `inputs` 字段名，人工确认）。
   - **明确排除**：`label_count`（标签随时间变化）、`merge_method`（`github_events._merge_method` 用**当前 PR 标题**去匹配合并提交主题，标题是可编辑字段，改标题会让同一个 squash 合并被重判为 `unknown`）、`approver`、`approver_type`、`approval_commit`、`approval_bound`（来自**当前**批准列表，评审被撤销会变，reviews 或提交读取失败时输出 `unknown`；设计评审 3 用真实 `github_events.sync` 逐一复现）。**覆盖范围如实说明**：被排除字段不由本核对检查；批准信息在同 source 存在时仍受第 3 条的九字段语义核对，其余情形没有任何核对；`audit_completeness._approval` 目前只核对 App 模式 R0/R1 的自动合并，R2 直接返回，`merge_method` 没有任何规则检查，这一缺口已登记为 B86（R2 自动合并的批准核对），**本任务不扩大实现，也不宣称已有兜底**。
   - 两种失败要区分原因，**不能把「读不到」说成「事实不同」**：候选为空 → reason「运行层没有该 PR 的合并事实（可能同步失败，见同步发现）」；有候选但某条投影不同 → reason「账本固定的合并事实与运行层不同」（只写字段名，不写值）。
   - **`merger` 是 login，不是账号 ID**（事件里没有保存用户 id）：GitHub 允许账号改名，改名后同一个人的 login 变化会被报告为「不同」。这是**有意接受**的行为：审计宁可多报一条人工一眼能确认的差异，也不在不知道是改名还是换人时静默放过；reason 里写明字段名 `merger`，处理方式是人工确认后在账本评论里说明。给事件加用户 id 需要改快照的生成与哈希、并兼容没有 id 的历史账本，是另一个设计，不在本任务内。
6. **`github:` 快照缺失按种类处理（只在账本的 source 在运行层完全没有事件时进入；`github.merge` 种类已由第 5 条独立检查，这里不再重复判断）**：
   - **种类按这个 source 的整个事件集合判定，不按单条事件的 `step`**：事件集合为「首条 `github.merge` + 零到多条 `github.merge_label`」的是**合并快照**（真实同步把标签事件放在同一个 source 里，设计评审 5 实测逐条分类会把合法加标签误判成未知种类）；全部是 `github.audit_sample` 单条的是审计抽样快照；全部是 `github.escape` 单条的是 escape 快照。
   - 合并快照：这个 source 本身不要求在运行层存在（事实合法变化会生成新 source），由第 5 条的一致性检查承担核对。
   - `github.audit_sample`、`github.escape`：这两类事实本来就会被后来的事实取代（审计议题合并后才创建、escape 事后登记），旧 source 在运行层缺失是合法的 → 不报。
   - 其他情形一律**失败关闭**，报 `ledger_mismatch`：事件集合里混有上面以外的 `step`、只有标签事件而没有 `github.merge`、同一 source 混合不同种类、账本里该 source 没有事件原文。
   - `ci:` 与本机来源保持现状（运行层没有该链仍 `continue`，由引用核对与 `missing_route` 报告）。
7. CHANGELOG「Unreleased」一行（英文）：`audit` no longer reports `ledger_mismatch` for GitHub fact snapshots that the merge workflow and the auditing machine each recorded with their own timestamps; differing events are still reported, and a recorded merge snapshot with no matching merge fact on the auditing side is now reported too (a later label change, or a superseded audit-sample or escape snapshot, is not). **Migration:** none。

## 白名单

- `engine/reports/audit_completeness.py`（只改 `_ledger`，可加一个私有辅助函数；当前 313 行，净增不超过 150 行，最终必须 ≤ 800）
- `tests/test_audit_ledger_github.py`（新增，新测试全放这里；不往 788 行的 `test_audit_completeness.py` 里加）
- `CHANGELOG.md`

## 消费方扫描（命令与输出，设计方 2026-10-07 执行；本仓库 cf45449）

```
$ grep -rn "_ledger(\|ledger_mismatch" engine
engine/reports/audit_completeness.py:256 (def _ledger)   :312 (findings += _ledger(auditor))   :278 (唯一产生点)
$ grep -rn "ledger_mismatch" tests docs
tests/test_audit_completeness.py:594 651 637（已有：账本链头换成别的哈希须报；真实 writer 账本须不报）
$ wc -l engine/reports/audit_completeness.py → 313
$ 账本文档事件字段（真实 harness-audit:2026/188.json 的 stages 条目）：
actor decision duration_ms engine_version error evidence_kind hash inputs outputs prev_hash redacted seq source stage status step trace_id ts
$ auditor.stages 事件条目字段（audit.py _collect_events）：
evidence source source_class stage step status ts duration_ms seq hash inputs outputs decision error actor
```

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| `tests/test_audit_completeness.py` 的 594–651 行用例 | 无需改动，必须仍通过 | 「账本链头换成别的哈希」的用例用的是 `ci:` 或本机链（见实现前核对），语义核对只对 `github:` 生效；若该用例用的是 `github:` 链，执行方先停下向设计方报告 |
| `audit.py` | 无需 | 只提供 `auditor.stages` 与 `ledger_doc`，不改 |
| `engine/core/alerts.py:209` | 无需 | 只按规则名与 finding 结构消费 `ledger_mismatch`，两者都不变（设计评审 1 核对） |
| 合并账本的写入（`ledger.py`） | 无需 | 账本格式不变 |

## 设计方的绕过与失效清单

| 可能的问题 | 处理 |
|---|---|
| 语义核对放过真实篡改（例如改了合并者或标签） | 比较键含 `actor`、`inputs`、`outputs`、`decision`、`error`、`seq`、`stage`、`step`、`status`；验收有逐字段的反例 |
| 少一条或多一条事件被放过；在合法链尾追加一条伪造事件（链校验仍通过、旧链头还在前缀里） | 两侧事件数量必须相等，按 `seq` 逐条比；目标终态第 3 条取消 `github:` 链的前缀通过；验收有「少一个标签事件」「多一个事件」「追加一条改了合并者的合法哈希事件」的反例 |
| 整条 `github:` source 在运行层缺失或被换成别的 source（事实变化另起新链）；合并者被换成别人而 source 随之改变 | 目标终态第 5 条：合并快照按「稳定身份投影」到运行层核对所有合并事件，找不到就报；合法的标签变化（`label_count` 与标签事件）不报；审计议题与 escape 的旧快照可被取代；未知种类失败关闭；验收逐类有正反例，变异覆盖 |
| 把可变字段（`label_count`、`merge_method`、批准四字段）算进投影，合法的加标签、改标题、评审撤销、API 读取失败就误报 | 投影只含稳定身份字段（目标终态第 5 条）；验收用真实 `github_events.sync` 路径逐一构造「加标签」「改 PR 标题」「批准列表变化」「reviews 读取失败」「提交读取失败」，都必须不报；变异把任一可变字段加进投影须失败 |
| 历史快照遮蔽当前差异（账本快照 A 缺失，运行层有同投影的 B 和合并者不同的 C，「任一相等」放过 C） | 目标终态第 5 条：同一 PR 的**所有**运行层合并事件都要相等，且是独立必检项（A 仍存在时也执行）；验收有 A 缺失与 A 存在两种 A/B/C 反例，变异改回「任一相等」或让同 source 通过短路它须失败 |
| 同一分支名被两个 PR 复用，另一个 PR 的合并事件被当成冲突事实 | 目标终态第 5 条：候选限定为 source 前缀 `github:<账本的 pr>:`；验收有「同分支两个 PR 不误报、同 PR 的 C 仍被拒」；变异去掉 PR 限定须失败 |
| 账号改名被报成换人 | 有意接受并在目标终态第 5 条写明（改名与换人在没有用户 id 时无法区分，宁可多报）；不是遗漏 |
| 把语义核对扩大到 `ci:` 链，削弱原有的哈希核对 | 只对 `github:` 前缀生效；验收有「`ci:` 链头不同、内容相同仍报」的用例 |
| 账本没带事件原文时放宽 | 目标终态第 4 条：无 `stages` 条目仍报错 |
| 比较键漏字段导致误报或漏报 | 键固定为九个字段，测试逐个字段改动都会被检出（`ts`、`duration_ms`、`engine_version` 的改动不会） |
| 错误信息泄露事件内容 | `reason` 只写数量差与字段名，不写值 |

## 非目标

- 不改 `github_events` 的快照生成与哈希（不让 `ts` 可复现，那会让所有历史账本失效），不改账本格式，不改 `audit.py`，不改 `ci:` 链的核对，不处理 B117（判定运行关联）。
- 白名单以外的文件一律不改；共用合同 C0 同样适用。

## 前置条件

- 工作区基于最新的 `origin/main`。按函数名定位，行号只作参考；只有函数、调用点或接口本身对不上时，才停下向设计方报告。

## 验收

具名测试为本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B118 | **跨环境同事实**：用真实账本写入路径生成账本，其中含 `github:` 快照链（合并事件加两个标签事件、抽审事件）；再在一个全新的事件库里用相同事实、**不同的 `ts` 与 `duration_ms` 与 `engine_version`**重新生成同 source 的事件，对该 PR 做 `audit` 的完整性核对 → 不含 `ledger_mismatch` | 夹具 | `tests/test_audit_ledger_github.py::CrossEnvironmentTest` | 当前代码必然报 `ledger_mismatch` |
| 不挂规格：B118 | **逐字段反例（必须落在不受合并投影保护的事件上）**：改动的目标是**标签事件与抽审事件**（它们不在第 5 条的投影里，只能由第 3 条的语义核对抓住），同上场景，分别把运行层一侧的 `actor`、`inputs`、`outputs`（合并者、标签、批准者）、`decision`、`error`、`status`、`step`、`stage`、`seq` 之一改掉 → 每一种都报 `ledger_mismatch`，`reason` 指出字段名、不含值；`ts`、`duration_ms`、`engine_version` 单独改不报 | 夹具 | `::EveryFieldTest`（逐字段子测试，九个字段各一个） | 语义核对漏字段 |
| 不挂规格：B118 | **后续事件的差异**：首条合并事件与事件数量都不变，只改**最后一条**标签事件的 `outputs`（以及只改第二条）→ 报 `ledger_mismatch`（防「只比较第一条」） | 夹具 | `::LaterEventTest` | 只比首条 |
| 不挂规格：B118 | **数量与追加**：运行层比账本少一条标签事件、多一条事件、在合法链尾**追加一条改了合并者且哈希链合法的事件**（旧链头仍在前缀里）→ 都报，`reason` 写数量差或字段名；单事件的抽审链在运行层多出一条事件同样报 | 夹具 | `::CountAndAppendTest` | 只比链头、只比存在性或仍认前缀 |
| 不挂规格：B118、B121 | **整条快照在运行层缺失（B121）**，全部经真实 `github_events.sync` 路径生成两份快照：账本含 `github.merge`（含标签事件）、`github.audit_sample`、`github.escape` 三类，运行层没有这些 source：①合并后加标签 → **不报**；②合并后**改 PR 标题**（`merge_method` 变 `unknown`）→ **不报**；③批准列表变化、reviews 读取失败（批准字段 `unknown`）、合并提交读取失败（`merge_method` `unknown`）→ **不报**；④运行层合并事件的 `merger`、`merger_type`、`merged_at` 或合并提交与账本不同 → 报，reason「与运行层不同」；⑤运行层没有任何 `github.merge` 事件 → 报，reason 区分为「运行层没有合并事实」；⑥**A/B/C 三快照**：账本 A、运行层只有同投影的 B（加标签后）与合并者不同的 C → 报（遮蔽反例）；⑥a **A 仍存在**：运行层有与账本逐字节相同的 A、加标签的 B 与合并者（或合并时间、合并提交）不同的 C → 报（不能被同 source 通过短路）；⑥b **同分支两个 PR**：运行层另有同 `head.ref` 的另一个 PR 的合并快照，账本 PR 的事实一致 → **不报**；账本 PR 自己有 C → 仍报；⑦账本只有 `github.audit_sample` 或 `github.escape` 快照、运行层没有（或有 `sampled`/新议题不同的新 source）→ **不报**；⑧未知 `step` 的 `github:` 快照、运行层没有 → 报；⑨账本里该 source 没有事件原文且运行层没有 → 报；⑩ `ci:` 链运行层没有 → 仍不报（现状）；⑪ **旧 source 缺失、新 source 里逐项改变稳定投影字段**（每项一个子测试，证明第 5 条自己保留了这些字段，不靠第 3 条）：`stage`、`status`、`actor`、`decision`、`error`、`inputs` 里的 head、合并提交与 PR 号、`outputs` 里的 `pr`、`merger`、`merger_type`、`merged_at` → 各报；⑫ **改名策略**：同一用户 id、不同 login → 报（reason 写 `merger`）；仓库名只改大小写 → **不报**；仓库名改成别的 → 报（reason 写 `inputs`）；⑬ **种类判定**：合并快照加一条标签事件（旧 source 缺失）→ 不报；事件集合混有未知 `step`、只有标签事件、混合种类 → 报 | 夹具 | `::SupersededSnapshotTest`（上述各一个子测试） | 漏报、误报，或把全部种类一律放过或一律拒绝 |
| 不挂规格：B118 | **仍保留的行为**：`github:` 链头等于运行层链头（无差异）不报；`ci:` 或本机链的链头在运行层事件哈希里（合法追加）不报；`ci:` 链头换成别的哈希、内容相同仍报；`github:` 链在账本里没有 `stages` 事件条目且链头不等于运行层链头时仍报；运行层没有该 `github:` source 时的处理见上一行 `SupersededSnapshotTest`（按快照种类区分，不再是一律不报） | 夹具 | `::PreservedBehaviorTest` | 误放宽或误收紧 |
| 不挂规格：B118 | 既有 `tests/test_audit_completeness.py`（含 594–651 行的账本用例）、`tests/test_audit_ledger.py`、`tests/test_audit_reconstruction.py` 原样通过 | 夹具 | `bin/verify --full` | 回归 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 先写 `tests/test_audit_ledger_github.py`，确认前两类用例在当前代码上失败 | `tests/test_audit_ledger_github.py` | `python3 -W error::ResourceWarning -m unittest tests.test_audit_ledger_github -v` | 验收第 1–6 行（先让前几类在当前代码上失败） |
| 2 | `_ledger` 加 `github:` 链语义核对、独立的合并事实一致性检查（第 5 条）与缺失快照分类（第 6 条），共用仓库名归一函数 | `engine/reports/audit_completeness.py` | 同上，加 `tests.test_audit_completeness tests.test_audit_ledger` | 验收第 1–6 行 |
| 3 | CHANGELOG；全量验证，整理交付证据 | `CHANGELOG.md` | `bin/verify --full` | 验收第 7 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收、G2（CI 的 `consumer-contract`）与定向变异复核，以下变异必须各自被对应断言抓住：
- 投影里删掉 `stage`、`status`、`actor`、`decision`、`error`、`inputs` 的 head 或合并提交或 PR 引用、`outputs` 的 `pr`、`merger`、`merger_type`、`merged_at` 之一（⑪ 须失败）；去掉仓库名小写归一（⑫ 须失败）；把种类判定改回逐条按 `step`（⑬ 须失败）；
- 比较键里漏掉 `actor`、`outputs`、`inputs`、`decision`、`error`、`status`、`step`、`stage`、`seq` 之一；把 `ts`、`duration_ms` 或 `engine_version` 加进比较键（跨环境用例须失败）；
- 只比较事件数量；只比较第一条事件（`::LaterEventTest` 须失败）；「少事件、多事件须被拒绝」是要求的行为，若实现没有独立的数量检查（整体列表相等已包含数量），去掉它的变异属等价变异，允许在交付说明里写明并豁免；
- 对 `github:` 链恢复「链头在前缀里就通过」（追加伪造事件的用例须失败）；
- 把语义核对扩大到所有来源（`ci:` 链头换哈希须仍报）；给 `ci:`/本机链取消前缀通过（合法追加须仍不报）；账本无事件条目时放过；
- `reason` 里带出字段值；
- B121：把 `label_count`、`merge_method`、批准四字段之一加进投影（②③①须失败）；把「所有合并事件都相等」改回「任一相等」（⑥须失败）；让同 source 的通过短路一致性检查（⑥a 须失败）；去掉 PR 限定（⑥b 须失败）；把所有种类都当作「可被取代」（④⑤⑧⑨须失败）；把所有种类都当作「失败关闭」（①②③⑦须失败）；投影漏掉 `merger`、`merger_type`、`merged_at`、`inputs` 里的合并提交之一；运行层没有 `github.merge` 事件时放过；「读不到」与「事实不同」用同一个 reason；`ci:` 链也报（⑩须失败）。
（两条旧快速通过路径各自能否被单独杀死不作要求：设计评审 1 实测，对静态快照删掉其中一条结果不变；要求的是上面的**行为**。）

**合并并随自举升级生效之后，设计方在真实仓库复跑 `bin/harness audit 188`**，应不再有 `ledger_mismatch`，其余结果不变。

## 修订记录

**修订 1（2026-10-07，第 1 次派发超时之后）**：第 1 次派发在 60 分钟预算用完时超时，且派发在提交「派发记录」时被 lint 钩子拦下而崩溃（执行方写出的 17 处未用变量没来得及修；崩溃后槽位目录被清理，未提交的改动由设计方从事件流重放恢复，已作为恢复提交推到任务分支）。预算与行数按实际调整，验收与范围不变：①`wall_clock_min` 60 → 90，续做用 `--resume`；②`audit_completeness.py` 净增上限 80 → 150 行（实测产出净增约 133 行，十来个私有函数，最终仍须 ≤ 800 行，质量棘轮不变）。续做要完成：`test_kind_classification` 失败（合并快照加标签事件的种类判定）、CHANGELOG、`bin/verify --full`；已有的恢复提交里的代码按任务书逐条自查，不要为凑行数删验收要求的行为。
