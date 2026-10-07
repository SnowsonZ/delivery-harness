---
task: T717
class: K5
risk: R2
designer: claude-code
size: small
architecture: true
spec_refs: []
no_spec_reason: 待办 B110（T715 平台验收 P3 发现的注释与返回值不符）；同时作为自治试验端到端验收 G 的重跑任务，验证 R2 合同制路径自动合并
budget:
  wall_clock_min: 45
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T717：`disable_auto_merge` 的注释与返回值一致（B110）

负责方：**派发任务**。设计方：claude-code；独立评审方：Codex（单会话）。依据：待办 B110；同时是自治试验端到端验收 G 的重跑任务（`docs/plans/2026-10-03-autonomy-trial-design.md`：R2 实现 PR 满足「任务书已批、运行记录合格、独立评审通过、设计方复核通过、CI 全绿」且全部改动路径都在合同白名单内时自动合并）。本任务刻意取小：改动面小、路径全部落在 `autonomy.toml [contract_route] allowed` 内，目的是验证合同制路径本身，而不是考验实现。

## 病灶（实测证据）

`engine/agents/github.py` 的 `GitHub.disable_auto_merge` 文档字符串写着「PR 本来就没开或调用失败时只打印、返回假」。T715 平台验收 P3 在真实仓库实测（`docs/review/t715-platform-acceptance.md`）：请求本就不存在时 `gh pr merge --disable-auto` 退出码为 0，方法返回**真**。文档字符串对「PR 本来就没开」这一半说错了，返回值只表示命令成功与否，不表示是否真有请求被关闭。三个生产调用方（`review.py:517`、`signoff.py:127`、`dispatch.py:530`）都不使用返回值，所以只让文档与行为一致，不改行为。

## 目标终态

1. 把 `disable_auto_merge` 的文档字符串**改为下面这段原文**（规范化空白后须逐字相同）：

   ```
   关闭 PR 已开启的自动合并（T715 否决信号）。

   返回值只表示 `gh pr merge --disable-auto` 这条命令是否成功：成功返回真（PR 本来没有开启自动合并时 `gh` 也退出 0，同样返回真）；命令失败（RuntimeError）或 `gh` 无法执行（OSError）时只向标准错误打印诊断并返回假，不抛异常。它不告诉调用方是否真有请求被关闭——否决本体已经完成，这里的失败不改变调用方的结论与退出码。
   ```

   **只改文档字符串，不改签名、装饰器、方法体。**
2. 新增 `tests/test_disable_auto_merge.py`，用 `GitHub` 的子类覆盖 `_run` 作桩（不调用真实 `gh`），断言上面的契约与「只改了文档」。

## 白名单

- `engine/agents/github.py`（只改 `disable_auto_merge` 的文档字符串）
- `tests/test_disable_auto_merge.py`（新增）

## 消费方扫描（命令与输出，设计方 2026-10-07 执行；本仓库 6bee972，消费方 81b1b6f）

```
$ grep -rn "disable_auto_merge" engine tests   （全部定义与引用，文件:行）
engine/agents/dispatch.py:530  signoff.py:125、127  review.py:515、517  github.py:71（定义）
tests/test_review_after_ci.py:154  test_alert_cli.py:133、134、140  test_harness_contract_dispatch.py:152
tests/test_events_agents.py:186、187、255  test_review_pack_taskbook.py:75  test_review_lock.py:113
tests/test_signoff.py:96、98  test_run_timeline.py:176、177  test_ci_workflows.py:315
tests/test_dispatch_alerts.py:161、162
tests/test_native_automerge.py:398、400、468、530、531、545、549、617、618

$ git -C <Agent-Notification> grep -n disable_auto_merge origin/main -- tests
origin/main:tests/test_harness_dispatch.py:150:    def disable_auto_merge(self, pr):
```

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| `review.py:517`、`signoff.py:127`、`dispatch.py:530` | 无需 | 三处都不使用返回值，方法体、签名不变 |
| 上面各测试文件里的桩（`test_review_after_ci`、`test_alert_cli`、`test_harness_contract_dispatch`、`test_events_agents`（两处）、`test_review_pack_taskbook`、`test_review_lock`、`test_signoff`、`test_run_timeline`、`test_ci_workflows`、`test_dispatch_alerts`） | 无需 | 不改签名与行为；桩只记录调用，返回值口径（成功真、失败假）与新文档一致 |
| `tests/test_native_automerge.py:617`（真实方法的现有契约测试） | 无需，本任务**不替代、不删除** | 它已覆盖空串成功返回真、`RuntimeError`/`OSError` 返回假与命令参数；本任务新增的测试补的是：诊断文案含 PR 号与重试命令、文档字符串全文、方法体只改文档 |
| 消费方 `tests/test_harness_dispatch.py:150` 的 `FakeGitHub` | 无需 | 签名不变 |

## 设计方的绕过与失效清单

| 可能的问题 | 处理 |
|---|---|
| 顺手改了方法体、签名或装饰器 | 白名单限定只改文档字符串；验收第 4 行用 AST 比较完整函数定义（含签名、装饰器），只忽略第一个文档字符串节点 |
| 文案断言被绕过（如把「命令成功返回真；PR 本来就没开；返回假。」拼进去） | 验收第 3 行比较**完整的规范化文档字符串**与本任务书审定的原文，不做关键词或切句判断；变异清单含该跨分号写法 |
| 桩调用了真实 `gh` | 测试用 `GitHub` 子类覆盖 `_run`，不启动子进程 |
| 本任务用的评审链还是旧版（评审提示词还要设计方本机输出、`ci.md` 旧实现），重跑 G 失去意义 | 前置条件要求内置引擎已含 #178，并核对实际使用的提示词与实现 |
| G 只看「有没有自动合并」，漏掉链路上的其他环节 | 「交付与升级」写明 G 的完整判据（同一 head 上的全链、`trace` 输出、复核后制造落后再同步） |

## 非目标

- 不改 `disable_auto_merge` 的行为、返回值语义或调用方；不让它去判断请求是否真被关闭。
- 不改白名单以外的文件，不改 CHANGELOG（无用户可见行为变化）；共用合同 C0 同样适用。

## 前置条件

- 工作区基于最新的 `origin/main`。按函数名定位，行号只作参考；只有函数、调用点或接口本身对不上时，才停下向设计方报告。
- **派发前，设计方先核对本仓库内置引擎已含 #178（B108 的修复）**：自举升级 PR 已合并，`.harness/engine.lock` 的 `commit` 不早于 6bee972，且 `.harness/engine/prompts/review_prompt.md` 含「## 不属于你评审的」一节、`.harness/engine/agents/review.py` 的 `ci_summary` 含回退实现（`_rollup_lines`）。`bin/dispatch` 与独立评审实际运行的是内置副本，不是 `engine/`。

## 验收

具名测试为本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B110 | `_run` 成功（含返回空串，即 PR 本来没开自动合并）时 `disable_auto_merge` 返回真，并以 `["gh", "pr", "merge", "<号>", "--disable-auto"]`、`agent=True` 调用一次 `_run` | 夹具 | `tests.test_disable_auto_merge.DisableAutoMergeContractTest.test_returns_true_when_the_command_succeeds_even_without_a_request` | 行为被改动或契约被误解 |
| 不挂规格：B110 | `_run` 抛 `RuntimeError` 或 `OSError` 时返回假、不向外抛、向标准错误打印含 PR 号与 `gh pr merge <号> --disable-auto` 的诊断 | 夹具 | `…test_returns_false_and_prints_on_command_failure_or_missing_gh` | 失败时抛异常或静默 |
| 不挂规格：B110 | `inspect.getdoc(GitHub.disable_auto_merge)` 规范化空白后与目标终态第 1 条的原文逐字相同 | 夹具 | `…test_docstring_is_exactly_the_approved_text` | 文档仍与行为不符，或被改成含旧说法的变体 |
| 不挂规格：B110 | 把测试内固定的基线源码（`git show 6bee972:engine/agents/github.py` 中 `disable_auto_merge` 的完整源码，作为字符串字面量，注明来源提交）与当前源码，同在当前解释器下 `ast.parse`，各自删去函数体第一个文档字符串节点后，`ast.dump(..., include_attributes=False)` 相等：证明签名、装饰器、方法体都没变，只变了文档 | 夹具 | `…test_function_is_unchanged_apart_from_the_docstring` | 顺手改了行为或签名 |
| 不挂规格：B110 | 既有测试全部通过 | 夹具 | `bin/verify --full` | 回归 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 改文档字符串，补契约测试 | `engine/agents/github.py`、`tests/test_disable_auto_merge.py` | `python3 -W error::ResourceWarning -m unittest tests.test_disable_auto_merge -v` | 验收第 1–4 行 |
| 2 | 全量验证，整理交付证据 | （无新增文件） | `bin/verify --full` | 验收第 5 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收、G2（CI 的 `consumer-contract` 已跑消费方完整 verify，不需要再手做）与定向变异复核，以下变异必须各自被对应断言抓住：
- 成功时返回假；失败时抛异常；失败时返回真；
- 文档字符串恢复「本来就没开……返回假」的旧说法，或改成「命令成功返回真；PR 本来就没开；返回假。」这类跨分号变体；
- 方法体被顺手改动，或签名、装饰器被改动。

**验收 G 的判据（设计方执行，原文见自治设计第 11 节 G 行）**——同一个 PR、同一个 head 上依次有：CI 通过 → 可信评审标记 → 复核标记 → `harness` 重跑 → route 事件 `auto_merge=true` → App 批准且绑定 head → 合并（现在由同步 App 开启原生自动合并、GitHub 以同步 App 身份合并，批准仍由批准 App 绑定 head）；`bin/harness trace <PR>` 的输出贴进该 PR；并且设计方必须在**复核之后、合并之前**主动制造一次「落后 → 同步」：先合并一个文档 PR 让本 PR 落后，然后核对同步后的合并仍由 App 完成，且账本和审计里评审证据仍在（T705；待办 B103「内容相同按字节比对」可能在这里暴露，如实记录）。**某一环没走通就记录卡在哪一条，不得手动绕过**；手动合并只用于收尾并如实记为 G 未通过。同一失败连续三轮没有新证据时，停止这条路径。不得自行扩大白名单、修改任务书、实现非目标。
