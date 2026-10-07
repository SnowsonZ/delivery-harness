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
  wall_clock_min: 60
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T719：`audit` 的账本核对不再对 GitHub 事实快照误报（B118）

负责方：**派发任务**。设计方：claude-code；独立评审方：Codex（单会话）。依据：待办 B118；G3-lite 记录 `docs/review/g3-lite-t718.md`。**路径全部落在 `autonomy.toml [contract_route] allowed` 内**（`engine/**`、`tests/**`、`CHANGELOG.md`）。

## 病灶（真实 GitHub 上的实测）

`bin/harness audit 188`（#188，T718 合并后第一个带新引擎 CI 的 PR）报 2 条 `ledger_mismatch`（合并快照、抽审快照），其余全部通过。

`engine/reports/audit_completeness.py` 的 `_ledger` 把账本里每条链的 `head_hash` 与运行层同一 source 的链头、或运行层该 source 的任一事件哈希比对，都对不上就报错。`github:` 开头的 source 是**内容寻址**的（`github_events._snapshot_source`：`github:<PR号>:<规范化事实的 sha256>`，读取时间不进摘要），同一份事实在合并后的 main 工作流（生成账本的地方）和本机 `audit`（先 `github_events.sync` 重新同步，见 `audit.py` 的 `_sync_github`）各生成一次，**source 相同、事件内容相同，但事件的 `ts` 不同**（#188：工作流 10:25:07，本机 10:28:41）。事件哈希含 `ts`，链头必然不同，所以 `_ledger` 必然误报。这不是篡改，是内容寻址快照每个环境各记一份时间戳。`ci:` 链是 CI 事件包原样导入，哈希在各环境可复现，不受影响。

后果：每个有账本的 PR，`audit` 都带两条以上 error，真实的账本不一致被淹没。

## 目标终态

`_ledger` 对 `github:` 开头的链改为**语义核对**：账本里该 source 的全部事件，与运行层同一 source 的全部事件，逐条比较「不含环境相关字段」的内容，全部一致才算通过；**这类链是不可变快照，不再接受「链头在前缀里」的合法追加**（见下面第 3 条与设计评审 1）；`ci:` 等其他来源的链保持原有的链头核对不变。

1. 账本里的事件取自账本文档的 `stages`（每条是完整事件），运行层的事件取自 `auditor.stages`（`evidence == "event"` 的条目，已含 `inputs`、`outputs`、`decision`、`error`、`actor`、`seq`、`source`、`stage`、`step`、`status`）。
2. **比较键**（固定这九个字段，不多不少）：`seq`、`stage`、`step`、`status`、`actor`、`inputs`、`outputs`、`decision`、`error`；按 `source` 分组后组内比较（`source` 是分组键，不是被比较的字段）。**排除**：`ts`、`hash`、`prev_hash`、`duration_ms`、`engine_version`、`redacted`、`trace_id`（环境相关或由哈希链派生；`trace_id` 在本次审计里本来就是同一个）。两侧按 `seq` 排序后整体相等才通过：事件数量、顺序、每个字段值都必须一致。
3. **`github:` 链的通过条件**只有两条：`head == 运行层该链当前链头`（内容逐字节相同，哈希含内容，所以等价于语义相同），或语义核对全部相等。**不再认「`head in 运行层任一事件哈希`（链头在前缀里）」**：快照按内容寻址、事实变了就另起新 source（`github_events` 的 `_fresh` 保证同 source 不会追加），所以 `github:` 链没有合法追加；若放行前缀，攻击者在合法的链上追加一条改了合并者的事件（链校验仍通过、旧链头仍在前缀里）就能被放过（设计评审 1 在内存里复现过）。`ci:` 与其他来源的两条快速通过路径与现状完全一致，不动。语义核对不通过报 `ledger_mismatch`，`reason` 写清差在哪一类（事件数量不同、第 N 条的哪个字段不同），不贴出事件内容的原文。
4. 账本里没有该 source 的事件（账本只记了链头、没有 `stages` 条目）：无法语义核对，除非链头等于运行层链头，否则报 `ledger_mismatch`（不放宽）。
5. **运行层完全没有该 `github:` source 的事件时，保持现状（`if not known: continue`，不在这里猜）**：这一情形既可能是之后事实合法变化（例如合并后又改了标签，重新同步会生成新 source、旧快照不再被重新生成），也可能是本机库被删，二者无法在这里区分；设计评审 1 指出这是已存在的缺口，**列为非目标，记入待办 B121**，不在本任务里扩大。
6. CHANGELOG「Unreleased」一行（英文）：`audit` no longer reports `ledger_mismatch` for GitHub fact snapshots that the merge workflow and the auditing machine each recorded with their own timestamps; when both sides hold the same snapshot source, any differing event is still reported (a snapshot missing on one side is tracked separately). **Migration:** none。

## 白名单

- `engine/reports/audit_completeness.py`（只改 `_ledger`，可加一个私有辅助函数；当前 313 行，净增不超过 40 行）
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
| 整条 `github:` source 在运行层缺失或被换成别的 source（事实变化另起新链） | 不在本任务处理（目标终态第 5 条，B121）；验收只固定「现状不变」，不假装已解决 |
| 把语义核对扩大到 `ci:` 链，削弱原有的哈希核对 | 只对 `github:` 前缀生效；验收有「`ci:` 链头不同、内容相同仍报」的用例 |
| 账本没带事件原文时放宽 | 目标终态第 4 条：无 `stages` 条目仍报错 |
| 比较键漏字段导致误报或漏报 | 键固定为九个字段，测试逐个字段改动都会被检出（`ts`、`duration_ms`、`engine_version` 的改动不会） |
| 错误信息泄露事件内容 | `reason` 只写数量差与字段名，不写值 |

## 非目标

- 不改 `github_events` 的快照生成与哈希（不让 `ts` 可复现，那会让所有历史账本失效），不改账本格式，不改 `audit.py`，不改 `ci:` 链的核对，不处理 B117（判定运行关联），不处理 B121（运行层缺整条 `github:` source）。
- 白名单以外的文件一律不改；共用合同 C0 同样适用。

## 前置条件

- 工作区基于最新的 `origin/main`。按函数名定位，行号只作参考；只有函数、调用点或接口本身对不上时，才停下向设计方报告。

## 验收

具名测试为本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B118 | **跨环境同事实**：用真实账本写入路径生成账本，其中含 `github:` 快照链（合并事件加两个标签事件、抽审事件）；再在一个全新的事件库里用相同事实、**不同的 `ts` 与 `duration_ms` 与 `engine_version`**重新生成同 source 的事件，对该 PR 做 `audit` 的完整性核对 → 不含 `ledger_mismatch` | 夹具 | `tests/test_audit_ledger_github.py::CrossEnvironmentTest` | 当前代码必然报 `ledger_mismatch` |
| 不挂规格：B118 | **逐字段反例**：同上场景，分别把运行层一侧的 `actor`、`inputs`、`outputs`（合并者、标签、批准者）、`decision`、`error`、`status`、`step`、`stage`、`seq` 之一改掉 → 每一种都报 `ledger_mismatch`，`reason` 指出字段名、不含值；`ts`、`duration_ms`、`engine_version` 单独改不报 | 夹具 | `::EveryFieldTest`（逐字段子测试，九个字段各一个） | 语义核对漏字段 |
| 不挂规格：B118 | **后续事件的差异**：首条合并事件与事件数量都不变，只改**最后一条**标签事件的 `outputs`（以及只改第二条）→ 报 `ledger_mismatch`（防「只比较第一条」） | 夹具 | `::LaterEventTest` | 只比首条 |
| 不挂规格：B118 | **数量与追加**：运行层比账本少一条标签事件、多一条事件、在合法链尾**追加一条改了合并者且哈希链合法的事件**（旧链头仍在前缀里）→ 都报，`reason` 写数量差或字段名；单事件的抽审链在运行层多出一条事件同样报 | 夹具 | `::CountAndAppendTest` | 只比链头、只比存在性或仍认前缀 |
| 不挂规格：B118 | **仍保留的行为**：`github:` 链头等于运行层链头（无差异）不报；`ci:` 或本机链的链头在运行层事件哈希里（合法追加）不报；`ci:` 链头换成别的哈希、内容相同仍报；`github:` 链在账本里没有 `stages` 事件条目且链头不等于运行层链头时仍报；运行层完全没有该 `github:` source 时保持现状（不报，固定现状而非宣称已处理） | 夹具 | `::PreservedBehaviorTest` | 误放宽或误收紧 |
| 不挂规格：B118 | 既有 `tests/test_audit_completeness.py`（含 594–651 行的账本用例）、`tests/test_audit_ledger.py`、`tests/test_audit_reconstruction.py` 原样通过 | 夹具 | `bin/verify --full` | 回归 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 先写 `tests/test_audit_ledger_github.py`，确认前两类用例在当前代码上失败 | `tests/test_audit_ledger_github.py` | `python3 -W error::ResourceWarning -m unittest tests.test_audit_ledger_github -v` | 验收第 1–4 行 |
| 2 | `_ledger` 加语义核对 | `engine/reports/audit_completeness.py` | 同上，加 `tests.test_audit_completeness tests.test_audit_ledger` | 验收第 1–5 行 |
| 3 | CHANGELOG；全量验证，整理交付证据 | `CHANGELOG.md` | `bin/verify --full` | 验收第 5 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收、G2（CI 的 `consumer-contract`）与定向变异复核，以下变异必须各自被对应断言抓住：
- 比较键里漏掉 `actor`、`outputs`、`inputs`、`decision`、`error`、`status`、`step`、`stage`、`seq` 之一；把 `ts`、`duration_ms` 或 `engine_version` 加进比较键（跨环境用例须失败）；
- 只比较事件数量；只比较第一条事件（`::LaterEventTest` 须失败）；「少事件、多事件须被拒绝」是要求的行为，若实现没有独立的数量检查（整体列表相等已包含数量），去掉它的变异属等价变异，允许在交付说明里写明并豁免；
- 对 `github:` 链恢复「链头在前缀里就通过」（追加伪造事件的用例须失败）；
- 把语义核对扩大到所有来源（`ci:` 链头换哈希须仍报）；给 `ci:`/本机链取消前缀通过（合法追加须仍不报）；账本无事件条目时放过；
- `reason` 里带出字段值。
（两条旧快速通过路径各自能否被单独杀死不作要求：设计评审 1 实测，对静态快照删掉其中一条结果不变；要求的是上面的**行为**。）

**合并并随自举升级生效之后，设计方在真实仓库复跑 `bin/harness audit 188`**，应不再有 `ledger_mismatch`，其余结果不变。
