---
task: T705
class: K7
risk: R3
designer: claude-code
size: small
architecture: true
spec_refs: []
no_spec_reason: 待办 B81 自治试验拆分评审阻断项 3（同步 main 后沿用评审结论，账本与审计按合并 head 严格比对会丢证据），无产品规格验收编号
budget:
  wall_clock_min: 90
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T705：账本与审计接受「内容相同」的评审 head

负责方：**派发任务**。设计方：claude-code；独立评审方：OpenCode。依据：[自治试验设计](2026-10-03-autonomy-trial-design.md)第 4 节「head 内容相同」与第 5 节第 5 步；拆分评审阻断项 3。**依赖**：T701（`engine/routing/signals.py` 的 `same_content`）已合并。

**病灶（代码证据）**：
- `engine/reports/ledger.py` 第 182 行，`_parse_review_audit` 要求 `audit["head"] == head_sha`，不相等就报 `head_mismatch`，这条评审不算证据。
- `engine/reports/audit.py` 第 281 行，复用了同一个函数。
- 合同制路径允许分支在评审之后只同步 main（diff 字节相同）就沿用原评审（T701）。这样合并的 head 和评审时的 head 一定不同，账本会丢掉评审证据，审计也会误报「R2 缺评审」。
- 另外，`templates/.github/workflows/harness.yml` 第 141 行，`audit-ledger` 任务的 `actions/checkout@v7` 没有设置 `fetch-depth`，用的是浅克隆。在这种克隆上无法比较两个 head 的 diff。

## 目标终态

1. `ledger._parse_review_audit(body, head_sha, same=None)` 增加一个可选参数 `same: Callable[[str], bool] | None`：
   - `audit["head"] != head_sha`，并且 `same` 给出、`same(audit["head"])` 为真时，按有效证据返回。原 JSON 不做任何改写，其中的 `head` 字段仍是评审时的 head，读的人能看出评审的是哪一版。
   - `same` 为 None 时，行为逐字不变。
2. 调用链：`build_ledger` 构造闭包后传给 `_review_entries`。`_review_entries` 增加可选参数 `same=None`，原样转给 `_parse_review_audit`，缺省时行为逐字不变。`audit.py` 第 281 行的调用处直接传闭包。闭包统一为：`lambda reviewed: signals.same_content(reviewed, head_sha, f"{merge_sha}^1", cwd)`。合并提交的第一个父提交，就是合并前的 main。git 出错时 `same_content` 返回假，退回严格比对。
3. `templates/.github/workflows/harness.yml` 的 `audit-ledger` 任务在 checkout 时加 `fetch-depth: 0`，**只改这一处**。
4. 回归测试放在新文件 `tests/test_ledger_equivalent_head.py`，用夹具 git 仓库：评审 head 是 R，同步 main 之后得到 H，再以 `--no-ff` 合并。

## 白名单

- `engine/reports/ledger.py`（只改三处：`_parse_review_audit` 的签名与判定；`_review_entries` 增加并转传 `same`；`build_ledger` 构造闭包）
- `engine/reports/audit.py`（只改第 281 行附近的调用，传入闭包）
- `templates/.github/workflows/harness.yml`（只在 `audit-ledger` 的 checkout 上加 `fetch-depth: 0`）
- `tests/test_ledger_equivalent_head.py`（新增）

消费方扫描（检查单第 2 项）：

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| `tests/test_audit_ledger.py` 第 449、494 行的 `head_mismatch` 用例 | 无需 | 那里改的是 `"f" * 40` 这种不存在的 head，`same_content` 返回假，仍然报 `head_mismatch` |
| `engine/reports/audit_completeness.py` | 无需 | 它读 `audit.py` 产出的评审阶段，接受的范围变宽，形状不变 |
| 账本 schema（`LEDGER_SCHEMA_VERSION`） | 无需 | 不新增字段；如果实现中发现必须新增字段，就停下报告设计方 |
| `tests/test_ci_events_workflows.py`、`tests/test_alert_cli.py` 对 `harness.yml` 的断言 | 实现前核对 | 只多了一个 `with: fetch-depth: 0`；有逐字比对 checkout 步骤的断言时，停下报告 |

CHANGELOG 由设计方在升级 PR 里统一写，本任务不改。

## 非目标

- 不放宽内容判定：不比较 patch-id，不规范化空白。
- 不改评审标记的格式。
- 不改账本 schema。
- 不处理 B86（审计核对 R2 自动合并的 App 批准）。
- 白名单以外的文件一律不改。
- 共用合同 C0 同样适用。

## 前置条件

- T701 已合并，工作区基于最新的 `origin/main`。核对上面引用的行号与 `signals.same_content` 的签名，有出入就停下，向设计方报告。
- 和 T703、T704 的白名单不交叉，可以并行。与 T702 也不交叉：T702 只改 `auto-merge.yml`，本任务只改 `harness.yml`。
- 实现前先确认本任务书已在 main 上：`bin/harness taskbook docs/plans/task-705-ledger-equivalent-head.md --on-main` 通过。

## 验收

下表中的测试名都是本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B81 | 评审 head R、同步 main 后的合并 head H（diff 字节相同）：账本的评审证据保留，不报 `head_mismatch`；`audit.py` 的评审阶段同样收录 | 夹具 | `tests.test_ledger_equivalent_head.LedgerEquivalentHeadTest.test_synced_head_keeps_review` | 报 `head_mismatch`，证据丢失 |
| 不挂规格：B81 | R 之后又有内容提交（包括只改缩进）时，仍然报 `head_mismatch` | 夹具 | `…test_changed_content_still_mismatch` | 内容变了，旧评审仍被采信 |
| 不挂规格：B81 | 浅克隆、git 出错时退回严格比对；`_parse_review_audit` 和 `_review_entries` 不传 `same` 时，行为与原来逐字相同 | 夹具 | `…test_strict_fallback` | 异常外泄，或缺省行为变了 |
| 不挂规格：B81 | 模板中 `audit-ledger` 的 checkout 带 `fetch-depth: 0` | 夹具 | `…test_ledger_checkout_full_history` | 浅克隆让上面的判定在 CI 上永远退回严格比对 |
| 不挂规格：B81 | 既有测试全部通过、零修改 | 夹具 | `bin/verify --full` | 既有账本、审计用例失败 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 可选的内容相同判定、两处调用、模板检出深度，以及回归断言 | `engine/reports/ledger.py`、`engine/reports/audit.py`、`templates/.github/workflows/harness.yml`、`tests/test_ledger_equivalent_head.py` | `python3 -W error::ResourceWarning -m unittest tests.test_ledger_equivalent_head tests.test_audit_ledger -v` | 验收第 1–4 行 |
| 2 | 全量验证，整理交付证据 | `tests/` | `bin/verify --full` | 验收第 5 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收与定向变异复核，以下四个变异必须各自被对应断言抓住：
- 闭包恒真；
- `_review_entries` 不转传 `same`（通过 `build_ledger` 端到端断言抓住）；
- 去掉 `fetch-depth`；
- base 用合并提交本身，而不是第一个父提交。

同一失败连续三轮没有新证据时，停止这条路径。不得自行扩大白名单、修改任务书、实现非目标。
