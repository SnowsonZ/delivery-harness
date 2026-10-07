---
task: T718
class: K5
risk: R2
designer: claude-code
size: medium
architecture: true
spec_refs: []
no_spec_reason: 待办 B116（自治试验验收 G 重跑发现：CI 事件导入链自 T305 起从未在真实 GitHub 上跑通）；用户 2026-10-07 决定先修
budget:
  wall_clock_min: 90
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T718：让 harness 运行的 CI 事件能真正导入（B116，三处缺陷）

负责方：**派发任务**。设计方：claude-code；独立评审方：Codex（单会话）。依据：待办 B116；验收 G 重跑记录 `docs/review/g-rerun-t717.md`。**路径全部落在 `autonomy.toml [contract_route] allowed` 内**（`engine/**`、`tests/**`、`CHANGELOG.md`），本任务同时是合同制路径的第二次真实运行。

## 病灶（真实 GitHub 上的实测，设计方 2026-10-07 在临时副本里逐层打补丁复现）

`bin/harness trace <PR号>`、`trace --ci`、合并账本里的 CI 与路由信息、`audit` 的 CI 部分，全部依赖 `engine/core/events_io.py` 的 `load_ci`。它自 T305 起没有在真实 GitHub 上跑通过：单元测试的桩替它造了真实不存在的字段与形状。三层缺陷叠在一起：

1. **`gh pr view --json baseRepository` 不是合法字段**（GraphQL 里有，gh 的 JSON 字段列表里没有；gh 2.92 报 `Unknown JSON field`）。两处复制：`engine/core/events_io.py` 的 `GhClient.pr`（约 649 行）与 `engine/reports/ledger.py` 的 `GhClient.pr`（约 107 行）。后果：`load_ci` 在第一步就失败，`trace 181`、`trace --ci` 退出码 1，所有合并账本缺 CI 事件与路由结果。
2. **`load_ci` 读 REST 运行记录时用了 gh 命令行的字段名**：`run.get("headSha")`（`events_io.py` 约 779、785 行）。REST（`gh api repos/<仓库>/actions/runs`）的字段是 `head_sha`。每个运行的 `headSha` 都是 `None`，被全部算作「其他 head」，一个也不导入（现象：「1 个其他 head 的运行未导入」）。同一函数里 `expired`、`archive_download_url`、`run_attempt`、`path` 用的已是正确的 REST 名。
3. **`pull_request` 触发的运行，导出的事件包 `origin.head_sha` 是合成合并提交，不是 PR 的 head**：`_origin()`（`events_io.py` 约 165–185 行）用 `git rev-parse HEAD`；`pull_request` 事件检出的是 `refs/pull/<N>/merge`，其 HEAD 是 GitHub 合成的合并提交（实测 `e319f39`），而 API 的 `head_sha` 是 PR 的 head（实测 `602c77f`）。加载器按 API 严格核对，所以所有 PR 运行的事件包都报 `origin_mismatch（head_sha）`。实测对照：`workflow_run` 与 `push` 触发的运行，事件包 `origin.head_sha` 与 API 的 `head_sha` **一致**（都是检出的 HEAD），不需要改。

三处补上之后，设计方在临时副本里实测 `load_ci(181)` 导入 127 个事件。

**本任务不做**：`workflow_run` 触发的运行（auto-merge 的判定，route 事件就在这里）在 API 里 `head_branch` 是 `main`、`head_sha` 是 main 的提交，与 PR 没有关联，`load_ci` 按 PR 分支名查不到它们，所以审计的 `missing_route` 修好本任务之后仍在。这需要设计，记在待办 B117。

## 目标终态

1. **`GhClient.pr`（两处）**：改用 `gh pr view <N> --json headRefName,headRefOid,url`，仓库名从 `url` 解析（`https?://<主机>/<owner>/<repo>/pull/<数字>`，主机任意，兼容企业版）；`url` 缺失或不匹配时 `repository` 为 `None`（调用方既有的形状校验会报「head/仓库信息缺失或形状不符」）。返回字典的键与现在完全相同：`headRefName`、`headRefOid`、`repository`（`owner/repo` 形式）。
2. **`load_ci`**：把对 REST 运行记录的 `run.get("headSha")` 全部改为 `run.get("head_sha")`（两处）。
3. **`_origin()`**：仅当**同时**满足以下条件时，`origin["head_sha"]` 取事件载荷里的 `pull_request.head.sha`：`CI == "true"`；`GITHUB_EVENT_NAME == "pull_request"`；`GITHUB_EVENT_PATH` 指向可读的 JSON 文件，其中 `pull_request.head.sha` 是 40 位十六进制提交号；`GITHUB_SHA` 是 40 位十六进制提交号且**等于 `git rev-parse HEAD`**（即检出的确实是 GitHub 为这次运行合成的合并提交）。**其余情形（其他事件类型、非 CI、文件不可读、JSON 损坏、字段缺失或形状不符、`GITHUB_SHA` 缺失或不等于检出的 HEAD）一律保持现状**（`git rev-parse HEAD`）。不得把事件载荷的路径、内容写进事件包（只取那个 SHA）。最后一个条件的作用：测试夹具在真实 PR 的 CI 里运行时，外层的 `GITHUB_EVENT_*` 仍在环境里，但夹具建的临时仓库的 HEAD 不会等于外层的 `GITHUB_SHA`，所以夹具自动回落到现状，既有测试不必逐个清理环境变量。
4. 防回归：新增一个测试，用 `ast` 遍历 `engine/` 下全部 `.py` 的调用节点，把调用参数里的字符串常量（含列表或元组里的、模块级字符串常量如 `PR_FIELDS`、以及 `+` 拼接如 `PR_FIELDS + ",state"`）按顺序摊平，找到 `"--json"` 后面的字段串，并**按实际命令**（`--json` 之前的 `pr|issue|run|repo` 加 `view|list|checks`）校验：该字段串的每个字段都在该命令的 gh 2.92.0 字段列表快照里。不得因为字段被别的命令接受就放行（例如 `nameWithOwner` 用在 `pr view` 上必须失败）。无法解析的调用点（字段串解析不出常量、命令不是上述几类）**必须让测试失败并列出 文件:行**，只能登记在测试里一个带逐项理由的 `UNRESOLVED_OK` 白名单中放行。快照覆盖 `pr view`、`pr list`、`pr checks`、`issue view`、`issue list`、`run list`、`repo view` 七个命令（设计方实测当前调用点：`pr view` 12、`issue list` 7、`pr list` 5、`run list` 5、`pr checks` 2、`repo view` 2、`issue view` 1，共 34 处，全部可解析），快照来源与取法（`gh <命令> --json __nope__` 离线输出的 Available fields，版本 2.92.0）写在测试注释里。
5. CHANGELOG「Unreleased」一行（英文）：`trace <PR>`、`trace --ci` 与合并账本能读到 harness 运行的 CI 事件；`pull_request` 运行导出的事件包记录 PR 的 head 而非合成合并提交；**Migration**：无；已导出的旧事件包（`origin.head_sha` 为合成合并提交）仍不可导入，只对升级之后的新 PR 运行生效。

## 白名单

- `engine/core/events_io.py`（只改 `GhClient.pr`、`load_ci` 里的 `headSha` 读取、`_origin`）
- `engine/reports/ledger.py`（只改 `GhClient.pr`；如需 `re` 则补 import）
- `tests/test_gh_json_fields.py`（新增）
- `tests/test_audit_ledger.py`、`tests/test_trace_events_cli.py`、`tests/test_audit_completeness.py`、`tests/test_audit_reconstruction.py`（只把 REST 形状假运行里的 `"headSha"` 改为 `"head_sha"`，共 **12 处**，行号见下面的消费方扫描；其余断言一律不动）
- `CHANGELOG.md`

## 消费方扫描（命令与输出，设计方 2026-10-07 执行；本仓库 6056070，消费方 Agent-Notification 81b1b6f）

```
$ grep -rn "baseRepository" engine tests
engine/core/events_io.py:649-651   engine/reports/ledger.py:107-109      （tests/ 下没有任何引用：桩直接替换了 pr()）
$ grep -rn "\.pr(" engine
engine/core/events_io.py:767 （load_ci）  engine/reports/trace.py:387 （只读 headRefName）
$ 多行字典也要找（Codex 设计评审指出按行筛选漏了 7 处）：各文件里所有 headSha
tests/test_audit_ledger.py:350 351 483 530（348 行是 docstring 提及）
tests/test_trace_events_cli.py:381 382 383
tests/test_audit_completeness.py:333 335
tests/test_audit_reconstruction.py:329 331 746
（共 12 处，全是 REST 形状的假运行：同一字典里有 id、run_attempt、path 或 head_branch；test_automerge_rounds.py、test_ci_workflows.py 的 headSha 是 gh run list 的命令行写法，正确，不动）
$ 设置 CI=true 并可能调用 _origin() 的测试
test_audit_ledger.py:267  test_audit_reconstruction.py:305  test_audit_completeness.py:305  test_trace_events_cli.py:474
test_events_route.py:271  test_events_hardening.py:301  test_events.py:264  test_ci_events_workflows.py:408
（它们在真实 PR 的 CI 里运行时，外层的 GITHUB_EVENT_* 与 GITHUB_SHA 仍在环境里；目标终态第 3 条的 GITHUB_SHA 等于检出 HEAD 条件使它们自动回落到现状，由验收第 6 行断言）
$ AST 扫描 engine/ 下全部 --json 调用点（设计方原型，34 处，全部可解析、命令可识别，0 处无法解析）
pr view 12 / issue list 7 / pr list 5 / run list 5 / pr checks 2 / repo view 2 / issue view 1
$ 消费方：git grep -n "GhClient\|load_ci\|baseRepository" origin/main -- tests   （无输出）
  消费方 tests/test_harness_run_check.py:149-152 的 headSha 是 gh run list 的命令行 JSON 写法（正确，不受影响）
```

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| `trace.py:387` | 无需 | 只读返回字典的 `headRefName`，键不变 |
| 四个测试文件里的 12 处 REST 假运行 | 需要（已列白名单） | 现在用 `headSha` 凑出了「能导入」；改成 `head_sha` 才是真实形状。`test_audit_ledger.py` 里的过期运行样本（483、530 行）也要改，否则到不了 `artifact_expired` 分支 |
| 八个设 `CI=true` 的测试 | 无需改动 | 靠 `_origin()` 的 `GITHUB_SHA == HEAD` 条件自动回落；验收第 6 行用「外层有 PR 环境变量」的场景证明，不要求逐个清理环境变量 |
| 其余引用 `load_ci`/`GhClient` 的测试（`test_audit_events`、`test_events_io`、`test_ledger_equivalent_head`、`test_audit_reconstruction`、`test_audit_completeness`、`test_github_events`） | 实现前核对 | 它们的客户端桩直接替换 `pr()` 与 `api()`；若断言了 `headSha` 的运行形状，会随修复失败，**停下报告，不得自行扩白名单** |
| 其他 `gh --json` 用法 | 无需 | 设计方已逐组用真实 gh 核对：除 `baseRepository` 外，代码里全部字段组都被某个 gh 命令接受（`nameWithOwner` 属于 `gh repo view`） |
| 消费方 Agent-Notification | 无需 | 不引用上述符号 |

## 设计方的绕过与失效清单

| 可能的问题 | 处理 |
|---|---|
| 修了 `pr()` 但桩继续返回假形状，测试什么也没证明 | 验收第 1 行用 PATH 上的假 `gh` 脚本（校验 `--json` 字段是否在 gh 2.92.0 的 `pr view` 字段列表内）运行**真实**的 `GhClient.pr`，不调用桩 |
| 只改一处 `pr()` | 两处各有独立断言；变异清单含各自回退 |
| `_origin()` 的事件载荷被伪造（执行方可控） | 加载器仍用 API 的 `head_sha` 严格核对运行元数据（`run_id`、`attempt`、`head_sha`、`head_branch`、仓库），不一致的包被拒；**但这不证明事件内容真实**——PR 代码与测试在同一 job 内执行，伪造者可以填入正确的运行信息并重算事件链，`job` 也是从 artifact 名提取、没有向 jobs API 核实。这是现有安全模型已接受的边界（同账号完整重写事件链），本任务既不放松也不加强加载器 |
| 新的环境条件把真实 PR 的 CI 弄坏 | 条件里 `GITHUB_SHA` 与检出 HEAD 在真实 PR 运行中必然相等（`actions/checkout` 默认检出 `refs/pull/N/merge`，即 `GITHUB_SHA`）；设计方在合并并升级后用真实运行验证（见「交付与升级」） |
| 字段合约测试放过命令与字段的错配 | 按实际命令校验、AST 解析；无法解析的调用点让测试失败、只能显式登记并写理由；变异清单含把 `nameWithOwner` 用到 `pr view` 上 |
| 事件载荷里的路径或内容进入事件包 | 只取 40 位十六进制的 SHA；验收第 5 行断言事件包不含载荷路径 |
| 对 `workflow_run`、`push` 事件也改 `head_sha`，反而弄坏已经一致的情形 | 目标终态第 3 条限定只对 `pull_request`；验收断言其他事件不变 |
| 以为修好了就能看到 route | 「病灶」与非目标明确写出 `workflow_run` 关联问题不在本任务（B117） |

## 非目标

- 不做 `workflow_run` 触发运行与 PR 的关联（B117），不改加载器的核对严格度，不改 `.github/` 与 `templates/`，不改 `audit`、`trace` 的逻辑。
- 白名单以外的文件一律不改；共用合同 C0 同样适用。

## 前置条件

- 工作区基于最新的 `origin/main`。按函数名定位，行号只作参考；只有函数、调用点或接口本身对不上时，才停下向设计方报告。

## 验收

具名测试为本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B116 | 用 PATH 上的假 `gh`（只接受 `pr view` 的合法字段，非法字段输出 `Unknown JSON field` 并退出 1）分别运行真实的 `events_io.GhClient().pr(181)` 与 `ledger` 的 `GhClient().pr(181)`：返回 `headRefName`、`headRefOid`、`repository == "owner/repo"`（由 `url` 解析）；记录到的 `--json` 字段不含 `baseRepository` | 夹具 | `tests.test_gh_json_fields.PrQueryTest.test_both_clients_use_valid_fields_and_derive_repository_from_url` | 又用了不存在的字段，或仓库名取不到 |
| 不挂规格：B116 | `url` 的形状：`https://github.com/o/r/pull/7`、企业版主机 `https://git.example.test/o/r/pull/7`、缺 `url`、`url` 不含 `/pull/`、非字符串，仓库名分别为 `o/r`、`o/r`、`None`、`None`、`None`，且不抛异常 | 夹具 | `…test_repository_parsing_forms` | 形状异常时崩溃或错误解析 |
| 不挂规格：B116 | 用 AST 遍历 `engine/` 全部 `--json` 调用点，**按实际命令**校验字段在 gh 2.92.0 快照里（七个命令，含 `issue view`）；断言 `baseRepository` 不在任何命令的列表里；断言把 `nameWithOwner` 套到 `pr view`、把 `headSha` 套到 `pr checks` 这类错配会被检出（对测试自己的校验函数做正反例）；无法解析的调用点让测试失败并列出 文件:行 | 夹具 | `…test_every_json_call_site_uses_fields_valid_for_its_own_command` | 以后再引入不存在的字段，或字段用在错的命令上 |
| 不挂规格：B116 | `load_ci` 用 REST 形状运行（`head_sha`、`path`、`id`、`run_attempt`）：head 匹配的运行会被处理（对其调用 artifacts 查询），head 不匹配的算「其他 head」；用旧的 `headSha` 形状时一个也不匹配 | 夹具 | `…LoadCiRestShapeTest.test_matches_runs_by_rest_head_sha` | 回到读 `headSha`，一个运行也导入不了 |
| 不挂规格：B116 | `_origin()`：`CI=true`、`GITHUB_EVENT_NAME=pull_request`、`GITHUB_EVENT_PATH` 指向含 `pull_request.head.sha` 的 JSON，且 `GITHUB_SHA` 等于临时仓库的 `git rev-parse HEAD` → `origin["head_sha"]` 等于载荷里的 SHA 而非 HEAD；`push`、`workflow_run`、非 CI、文件不存在、JSON 损坏、字段缺失或不是 40 位十六进制、`GITHUB_SHA` 缺失 → 与现状一致（取 `git rev-parse HEAD`）；序列化后的 `origin` 不含事件载荷路径 | 夹具 | `…OriginTest.test_pull_request_head_comes_from_the_event_payload_only` | PR 运行的事件包仍然对不上 API |
| 不挂规格：B116 | 外层环境带真实形状的 PR 变量（`GITHUB_EVENT_NAME=pull_request`、`GITHUB_EVENT_PATH` 指向有效载荷、`GITHUB_SHA` 为某个**与临时仓库 HEAD 不同**的提交）时，夹具建的临时仓库里 `_origin()` 仍取该仓库自己的 `git rev-parse HEAD`；并在 `test_trace_events_cli` 的真实夹具路径上跑一次这个场景，证明它们在真实 PR 的 CI 里不会被污染 | 夹具 | `…OriginTest.test_ambient_pull_request_env_does_not_leak_into_fixture_repos` | 新逻辑在真实 PR 的 CI 里弄坏既有测试 |
| 不挂规格：B116 | 既有测试全部通过（含四个改了假运行字段的测试文件） | 夹具 | `bin/verify --full` | 回归 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 两处 `GhClient.pr` 改用 `url`，字段合约测试 | `engine/core/events_io.py`、`engine/reports/ledger.py`、`tests/test_gh_json_fields.py` | `python3 -W error::ResourceWarning -m unittest tests.test_gh_json_fields -v` | 验收第 1–3 行 |
| 2 | `load_ci` 读 `head_sha`，更正四个测试文件里 12 处假运行字段 | `engine/core/events_io.py`、`tests/test_audit_ledger.py`、`tests/test_trace_events_cli.py`、`tests/test_audit_completeness.py`、`tests/test_audit_reconstruction.py`、`tests/test_gh_json_fields.py` | 同上，加 `tests.test_audit_ledger tests.test_trace_events_cli tests.test_audit_completeness tests.test_audit_reconstruction` | 验收第 4 行 |
| 3 | `_origin()` 取 PR 的 head（带 `GITHUB_SHA` 等于 HEAD 条件） | `engine/core/events_io.py`、`tests/test_gh_json_fields.py` | 同上，加 `tests.test_trace_events_cli` | 验收第 5、6 行 |
| 4 | CHANGELOG；全量验证，整理交付证据 | `CHANGELOG.md` | `bin/verify --full` | 验收第 7 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收、G2（CI 的 `consumer-contract` 已跑消费方完整 verify）与定向变异复核，以下变异必须各自被对应断言抓住：
- 任一处 `GhClient.pr` 退回 `baseRepository`；`url` 解析把 owner 与 repo 对调；字段合约测试里把 `nameWithOwner` 套到 `pr view` 上（须被检出）；
- `load_ci` 任一处退回 `headSha`；
- `_origin()` 在 `pull_request` 时仍取 `git rev-parse HEAD`；在 `push`、`workflow_run` 时也改取载荷；去掉 `GITHUB_SHA` 等于 HEAD 的条件；把载荷路径写进 `origin`；
- 字段合约测试里把 `baseRepository` 加进某个命令的列表（测试自己的快照不得被这样放过）。

**合并并随自举升级生效之后，设计方在真实仓库做一次 G3 小验证**：下一个 PR 的 harness 运行产生的新事件包，`trace --ci <任务号>` 与 `bin/harness audit <PR号>` 能导入 harness 运行的 CI 事件、不再报 `origin_mismatch`、`head_mismatch` 之外的 CI 错误；`workflow_run` 判定事件仍缺（B117）如实记录。本任务按合同制路径应由 auto-merge 自动合并；某一环没走通就记录卡在哪一条，不得手动绕过。同一失败连续三轮没有新证据时，停止这条路径。不得自行扩大白名单、修改任务书、实现非目标。
