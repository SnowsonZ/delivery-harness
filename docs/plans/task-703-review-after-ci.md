---
task: T703
class: K7
risk: R3
designer: claude-code
size: small
architecture: true
spec_refs: []
no_spec_reason: 待办 B77 第③项（评审链静默断链，四次复现）与 B81 自治试验设计第 5 节第 2 步，无产品规格验收编号
budget:
  wall_clock_min: 90
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T703：CI 通过后由派发进程直接接上独立评审（B77③）

负责方：**派发任务**。设计方：claude-code；独立评审方：OpenCode。依据：[自治试验设计](2026-10-03-autonomy-trial-design.md)第 5 节第 2 步；待办 B77 第③项（2026-10-01 至 10-03，T401/T404/T402/T403 四单 CI 通过后评审都没有自动接上，靠手动 `bin/dispatch review <PR>` 补跑才通过）。

**病灶（代码证据）**：`engine/agents/dispatch.py` 第 585–587 行，CI 通过后只打印「合并由 auto-merge 判定」（第 586 行）就 `return 0`。评审全靠设计方在后台另起的 `bin/dispatch review --watch`，或者手动串起来的 shell 链；进程只要一断，评审就没人接。

开工前先读：
- `engine/agents/dispatch.py`：`Config`（第 72 行）、`Config.load`（第 80 行）、`run`（第 525 行，槽位在 `finally` 里归还）、`_loop` 中 CI 通过的分支（第 585 行）；
- `engine/agents/review.py`：`review_pr`（第 380 行）；`engine/routing/signals.py`（T701 新增）：`review_status`。注意不要用 `review.reviewed_heads`：它不核对作者，其他账号贴出一条标记，就会让派发误以为已经评审过、跳过评审（拆分评审严重项 4）。

## 目标终态

1. `Config` 新增字段 `review_after_ci: bool = False`，`Config.load` 从 `rules.toml [dispatch] review_after_ci` 读取，缺省为 False。不开这个开关时，行为与现在逐字相同。
2. 开关打开、`_loop` 因 CI 通过返回 0 之后，**先归还槽位，再评审**，评审期间不占用槽位：
   - 用 `signals.review_status(comments, agent_login, head, "origin/main", self.root)` 判断：返回的二元组第一个元素不是 `missing` 时跳过，并打印「已有评审结论，跳过」。`comments` 取 `gh pr view <PR> --json comments`，经由 `self.github._run` 调用，`--json` 输出含作者；`agent_login` 取 `[identity] agent_login`。读取失败时按 `missing` 处理，照常评审，宁可重复评审，也不要漏评。
   - 没有就调用新函数 `review_with_chain(pr, root, github, review=None)`（定义在本模块，T702 的 watch 也会复用）。它读取 `rules.toml [review] chain`，缺省为 `[reviewer]`，即沿用原来的单一评审方。然后按顺序调用 `review.review_pr(pr, 名称, root, github)`：按返回值分别处理（拆分评审第五轮一般项 3）：
     - **0**：停止。不管结论是通过还是否决，都算评审完成。
     - **1**：评审方运行失败，包括额度用尽；`review_pr` 已经发出 `review_error` 告警。换下一家。
     - **2**：配置不可用，比如这一家就是设计方本身（违反分离规则），或者无法选定评审方；这种情况没有告警。向 stderr 打印一行原因，然后换下一家。
     - **抛出异常**（比如评审程序不存在时的 `FileNotFoundError`）：捕获后打印一行原因，换下一家。
     
     全部失败时返回 1，stderr 汇总每一家的结果。`engine.agents.review` 依赖本模块，所以按需导入，沿用 `main` 里已有的写法。
   - 评审链抛异常或全部失败时，派发的退出码仍为 0（CI 已经通过，任务本身已完成）。同时向 stderr 打印一行：「评审未能自动接上：<原因>；请设计方运行 bin/dispatch review <PR>」，不能吞掉。
3. 升级、预算耗尽、本地未完成这些路径都不触发评审，行为不变。

## 白名单

- `engine/agents/dispatch.py`（只改 `Config`、`run` 的收尾，以及上面描述的评审衔接）
- `tests/test_review_after_ci.py`（新增）

消费方扫描（检查单第 2 项）：

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| 测试里直接构造 `dispatch.Config(...)` 的地方（`test_ci_workflows.py`、`test_dispatch_alerts.py`、`test_events_agents.py` 等） | 无需 | 新字段有默认值 False，原来的构造方式和行为都不变 |
| 假 GitHub 桩（各测试） | 无需 | 开关关闭时不读评论 |
| `templates/.harness/config/rules.toml` | 无需（本任务不改） | `review_after_ci` 默认关闭；`[review] chain` 缺省时沿用 `reviewer`，消费方不需要迁移。配置说明由设计方写进 CHANGELOG（设计第 9 节 D1），避免和 T701 改同一个文件 |
| 运行记录、事件 | 无需 | 评审自己的事件由 `review_pr` 照常发出 |

## 非目标

- 不改 `review.py`。
- 不改 `wait_ci`。
- 不在 CI 失败或升级路径上触发评审。
- 不改槽位回收逻辑（T125 已完成）。
- 不改模板配置。
- 白名单以外的文件一律不改。
- 共用合同 C0 同样适用。

## 前置条件

- 工作区基于最新的 `origin/main`。核对上面引用的行号和接口，有出入就停下，向设计方报告。
- **依赖 T701**（`signals` 模块）已合并。和 T704、T705 的白名单不交叉，可以并行。T702 也要改 `dispatch.py`（加子命令），所以必须排在本任务之后。
- 实现前先确认本任务书已在 main 上：`bin/harness taskbook docs/plans/task-703-review-after-ci.md --on-main` 通过。

## 验收

下表中的测试名都是本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B77 | 开关打开、CI 通过时，`review_pr` 对该 PR 恰好调用一次；调用时槽位锁已经释放 | 夹具 | `tests.test_review_after_ci.ReviewAfterCiTest.test_review_runs_after_slot_release` | 没有调用（B77③ 的原样），或调用时槽位锁还在 |
| 不挂规格：B77 | head 已有 agent_login 写的评审结论时不再调用；只有其他账号写的标记时照常调用 | 夹具 | `…test_skip_only_on_trusted_review` | 重复评审；或被第三方评论诱导跳过评审 |
| 不挂规格：B77 | `chain = ["codex", "claude-code", "opencode"]`：codex 返回 1、claude-code 返回 2（就是设计方）、opencode 返回 0 时，三家依次被调用，最终返回 0；codex 抛出 `FileNotFoundError` 时也会换下一家；某一家返回 0（包括否决）后，不再调用后面的；未配置 chain 时只调用 `reviewer` | 夹具 | `…test_review_chain_falls_back` | 额度用尽时评审停摆，或成功后继续多评 |
| 不挂规格：B77 | 评审链全部失败或抛异常时，派发返回 0，stderr 含「评审未能自动接上」和 PR 号 | 夹具 | `…test_review_failure_is_loud_not_fatal` | 静默失败，或派发返回非 0 |
| 不挂规格：B77 | 开关关闭（缺省）时，不读评论，也不调用 `review_pr`；CI 失败、升级路径在开关打开时也不调用 | 夹具 | `…test_off_and_failure_paths_untouched` | 缺省行为变了，或失败路径触发了评审 |
| 不挂规格：B77 | 既有测试全部通过、零修改 | 夹具 | `bin/verify --full` | 既有夹具因为新字段或新调用而失败 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | Config 开关、槽位归还后接上评审、失败提示，以及回归断言 | `engine/agents/dispatch.py`、`tests/test_review_after_ci.py` | `python3 -W error::ResourceWarning -m unittest tests.test_review_after_ci -v` | 验收第 1–5 行 |
| 2 | 全量验证，整理交付证据 | `tests/` | `bin/verify --full` | 验收第 6 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收与定向变异复核，以下四个变异必须各自被对应断言抓住：
- 去掉已评审检查，或去掉作者过滤；
- 评审移到槽位归还之前；
- 吞掉失败提示；
- 评审链在第一家失败后就停止。

同一失败连续三轮没有新证据时，停止这条路径。不得自行扩大白名单、修改任务书、实现非目标。
