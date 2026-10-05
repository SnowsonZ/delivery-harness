---
task: T714
class: K5
risk: R2
designer: claude-code
size: small
architecture: true
spec_refs: []
no_spec_reason: 端到端验收 G 发现的设计缺陷（同一个 App 先同步分支、再批准，违反 ruleset「最后推送由他人批准」）；用户 2026-10-05 选方案 A（第二个 App 专管同步），无产品规格验收编号
budget:
  wall_clock_min: 120
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T714：分支同步改用独立的同步 App，批准 App 只负责批准

负责方：**派发任务**。设计方：claude-code；独立评审方：Codex（单会话）。依据：端到端验收 G 的实测（#147）；用户 2026-10-05 决定（方案 A：第二个 App 专管同步；平台侧已配好）。

## 病灶（代码证据）

- `templates/.github/workflows/auto-merge.yml` 的 `merge-app` 任务用**同一个** App 令牌（「App token (approve and branch sync)」，申请 `pull-requests: write` 与 `contents: write`）做两件事：分支落后时 `gh pr update-branch`，不落后时批准。
- main 的 ruleset 要求 `require_last_push_approval: true`，即最后一次推送必须由**推送者以外**的人批准。#147 实测：App 同步分支后成了最后推送者（同步提交 `f028349`，作者 `delivery-harness-auto-merge[bot]`），随后它的批准不满足这条规则，`gh pr merge` 被拒（「the base branch policy prohibits the merge」）。凡是经过一次同步的 PR，都永远无法自动合并。
- 平台侧已由用户配好第二个 App：仓库变量 `HARNESS_SYNC_APP_CLIENT_ID`，environment `harness-auto-merge` 下的密钥 `HARNESS_SYNC_APP_PRIVATE_KEY`。

## 目标终态

1. **配置**：`checks.toml [platform]` 新增可选键 `sync_app_client_id_var`（默认 `HARNESS_SYNC_APP_CLIENT_ID`）、`sync_app_private_key_secret`（默认 `HARNESS_SYNC_APP_PRIVATE_KEY`）。`engine/routing/policy.py` 的 `platform_outputs` 一并输出，judge 任务把它们列进 `outputs`。模板 `templates/.harness/config/checks.toml` 以注释形式说明。
2. **工作流**（`templates/.github/workflows/auto-merge.yml` 的 `merge-app`）：
   - 批准用的 App 令牌步骤改名为「App token (approve)」，只申请 `permission-pull-requests: write`。
   - 新增「Sync App token」步骤：仅当同步 App 的变量非空时执行（`vars[needs.judge.outputs.sync_app_client_id_var] != ''`），申请 `permission-contents: write` 与 `permission-pull-requests: write`。
   - 分支同步步骤：冲突优先（不变）；不冲突且落后时，有同步令牌就用**同步令牌**执行 `gh pr update-branch`；没有同步令牌时**不同步**，在 PR 上评论一次「分支落后于 main，未配置同步 App，请手动同步后重判」，本任务以成功结束。两种情况都不批准、不合并（与现在相同）。
   - 批准与合并步骤不变，仍用批准 App 的令牌批准。
3. **文档**：README「Platform setup」补第二个 App 的说明（用途、权限 Contents 写与 Pull requests 写、变量与密钥名、为什么不能与批准 App 合并：ruleset 的「最后推送由他人批准」）；批准 App 的权限说明改回只需 Pull requests 写。
4. CHANGELOG「Unreleased」：`**Migration:**` 条目——推荐配置第二个 App；不配置时，落后的 PR 改为评论提示、需手动同步；重新复制 `auto-merge.yml`；批准 App 可以去掉 Contents 写权限。

本仓库 `.github/workflows/auto-merge.yml` 的同步与升级属于 R3，由设计方在升级 PR 中另行提交。

## 白名单

- `engine/routing/policy.py`（只改 `platform_outputs`）
- `templates/.github/workflows/auto-merge.yml`（只改 judge 的 `outputs` 与 `merge-app` 任务）
- `templates/.harness/config/checks.toml`（只加注释说明的可选键）
- `tests/test_signoff.py`（merge-app 回放测试：补同步令牌相关断言）
- `tests/test_sync_app.py`（新增）
- `README.md`
- `CHANGELOG.md`

消费方扫描（检查单第 2 项）：

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| `tests/test_signoff.py` 的 `replay_merge_app` 与 `test_merge_app_syncs_behind_branch` | 需要 | 回放桩要能区分两个令牌、模拟同步 App 未配置；原有断言（冲突优先、正常批准逐字不变、步骤顺序）保留 |
| `tests/test_install.py` 中断言 `app_client_id_var` 的用例 | 实现前核对 | 若断言了 judge 的 outputs 清单或 `platform_outputs` 的键集合，只同步这一处口径；有出入就停下报告 |
| `.github/workflows/auto-merge.yml`（本仓库现装） | 无需（本任务内） | 由设计方随升级 PR 同步 |
| Agent-Notification | 需要迁移（可选） | 不配同步 App 时，落后的 PR 改为评论提示；CHANGELOG 的 Migration 条目说明 |

## 非目标

- 不改 ruleset、不改批准与合并的条件。
- 不改冲突处理。
- 不改本仓库 `.github/` 与 `.harness/config/`。
- 白名单以外的文件一律不改。
- 共用合同 C0 同样适用。

## 前置条件

- 工作区基于最新的 `origin/main`。按函数名定位，行号只作参考；只有函数、调用点或接口本身对不上时，才停下向设计方报告。

## 验收

具名测试为本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：G 发现 | `platform_outputs` 含两个同步键：缺省为 `HARNESS_SYNC_APP_CLIENT_ID`、`HARNESS_SYNC_APP_PRIVATE_KEY`，配置后为配置值；judge 任务的 `outputs` 列出它们 | 夹具 | `tests.test_sync_app.SyncAppTest.test_platform_outputs_and_judge_outputs` | 工作流读不到同步 App 的变量名 |
| 不挂规格：G 发现 | 回放 merge-app：落后（behind_by=2）且同步令牌存在时，`update-branch` 使用的是同步令牌而不是批准令牌；不批准、不合并 | 夹具 | `tests.test_signoff.SignoffTest.test_merge_app_syncs_behind_branch` | 同步者与批准者仍是同一个 App |
| 不挂规格：G 发现 | 回放 merge-app：落后且同步 App 未配置时，不调用 `update-branch`，评论提示一次，不批准、不合并，步骤成功结束 | 夹具 | `tests.test_sync_app.SyncAppTest.test_behind_without_sync_app_comments` | 回退为批准 App 同步，或步骤失败 |
| 不挂规格：G 发现 | 工作流静态断言：批准 App 的令牌步骤只申请 `pull-requests: write`；同步令牌步骤带「变量非空」条件，并申请 `contents: write` | 夹具 | `…test_token_permissions_split` | 批准 App 仍持有写代码的权限 |
| 不挂规格：G 发现 | 不落后时批准与合并的命令逐字不变；冲突优先于落后不变 | 夹具 | `tests.test_signoff.SignoffTest.test_merge_app_syncs_behind_branch` | 回归 |
| 不挂规格：G 发现 | 既有测试全部通过（白名单内两处口径同步除外） | 夹具 | `bin/verify --full` | 回归 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | `platform_outputs` 加同步键，judge outputs 与模板注释 | `engine/routing/policy.py`、`templates/.github/workflows/auto-merge.yml`、`templates/.harness/config/checks.toml`、`tests/test_sync_app.py` | `python3 -W error::ResourceWarning -m unittest tests.test_sync_app -v` | 验收第 1 行 |
| 2 | merge-app 拆分令牌、同步逻辑与回放测试 | `templates/.github/workflows/auto-merge.yml`、`tests/test_signoff.py`、`tests/test_sync_app.py` | `python3 -W error::ResourceWarning -m unittest tests.test_sync_app tests.test_signoff -v` | 验收第 2–5 行 |
| 3 | README、CHANGELOG；全量验证，整理交付证据 | `README.md`、`CHANGELOG.md` | `bin/verify --full` | 验收第 6 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收与定向变异复核，以下变异必须各自被对应断言抓住：
- 同步改回用批准 App 的令牌；
- 批准 App 的令牌仍申请 `contents: write`；
- 同步 App 未配置时仍用批准 App 同步；
- 去掉「同步令牌步骤仅在变量非空时执行」的条件。

本任务修复后，设计方在升级 PR 中同步本仓库的 `auto-merge.yml`，再用 #147、#144 重做端到端验收 G 的「落后 → 同步 → 自动合并」一段。

同一失败连续三轮没有新证据时，停止这条路径。不得自行扩大白名单、修改任务书、实现非目标。
