---
task: T720
class: K5
risk: R2
designer: claude-code
size: medium
architecture: true
spec_refs: []
no_spec_reason: 待办 B117（T718 病灶调查发现：auto-merge 判定运行的事件包与 PR 没有任何可核对的关联，route 事件永远导入不进来）；用户 2026-10-07 决定先修再回主线
budget:
  wall_clock_min: 90
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T720：把 auto-merge 判定运行关联到 PR，让 route 事件能导入（B117）

负责方：**派发任务**。设计方：claude-code；独立评审方：Codex（单会话）。依据：待办 B117；G3-lite 记录 `docs/review/g3-lite-t718.md`。**路径全部落在 `autonomy.toml [contract_route] allowed` 内**（`engine/**`、`templates/**`、`tests/**`、`CHANGELOG.md`）。本仓库自己的 `.github/workflows/auto-merge.yml` **不在白名单**（`.github/` 是信任边界），合并后由设计方另开 R3 PR 同步。

## 病灶（真实 GitHub 上的实测，设计方 2026-10-07）

auto-merge 的判定任务（`judge`）由 `workflow_run` 触发，route 事件（`route.facts`、`route.result`，审计的 `missing_route` 规则要看它）就在它导出的事件包里。实测 #187 的判定运行 37597033237：

```
API 运行记录：event=workflow_run  head_branch=main  head_sha=8f42c2b…（main 的提交）  path=.github/workflows/auto-merge.yml
              display_title="auto-merge"  pull_requests=[]
事件包 origin：head_branch=main  head_sha=8f42c2b…  job=judge  run_id=37597033237  run_attempt=1
事件：26 个，source 都是 ci:37597033237:1:judge；trace_id 有两种：PR 分支 task/718-…（判定的主体事件）与 main@8f42c2b
```

`load_ci`（`engine/core/events_io.py`）按 PR 分支名查运行（`actions/runs?branch=<PR 分支>`），而 `workflow_run` 触发的运行在 API 里归在 `main`，所以**这个运行永远查不到**；API 上也没有任何字段把它和 PR 关联（`pull_requests` 为空，`display_title` 只是工作流名）。包的 `origin` 与 API 记录其实是一致的（都是 main 的提交），只是加载器找不到它（设计评审 2：按包自己的 main 信息核对 `findings=[]`；只有错用 PR 的 head 与分支去核对才会出现 `origin_mismatch`，那是新接线必须避免的回归，不是现状）。

后果：每个自动合并的 PR，合并账本没有路由结果，`audit` 报 `missing_route`；`trace <PR>` 的时间线缺「路由判定」一环。这是 B46 的 G3 验收点之一。

## 目标终态（设计方案）

核心：**让信任来自 API 返回、由默认分支上的工作流定义生成的运行名，而不是包的自述。**

1. **模板 `templates/.github/workflows/auto-merge.yml` 加顶层 `run-name`**：**仅当 `judge` 会真正执行时**，运行名为 `auto-merge PR #<PR号> @ <被评估的 head 的 40 位 SHA>`（取 `github.event.workflow_run.pull_requests[0].number` 与 `github.event.workflow_run.head_sha`，与 `judge`、`request-review` 等 job 已在用的变量完全一致）；条件与 `judge.if` **逐字相同**（`workflow_run.event == 'pull_request'`、`conclusion == 'success'`、`head_repository.full_name == github.repository`），其余情形（`push` 触发、上游 CI 失败因而 `judge` 被跳过、来自 fork 的运行）一律保持 `auto-merge`。理由：设计评审 2 实测，上游 CI 失败时 `judge` 被跳过、没有事件包，但运行整体仍是 `success`；若这类运行也带关联名，加载器会把「正常跳过」当成「包缺失」，审计再把它映射成 `reference_unavailable`。REST 运行记录的 `display_title` 字段就是运行名。
2. **信任论证**：`workflow_run` 触发的运行执行的是**默认分支上的**工作流定义，PR 作者改不了它（这也是 auto-merge 选 `workflow_run` 的原因，见模板顶部说明）；`display_title` 由该定义用 GitHub 提供的载荷渲染，包内容与 PR 分支代码都影响不到它。加载器只信 API 返回的这个字符串，与现在「只信 API」的原则一致，没有放宽。
3. **加载器（新模块 `engine/core/events_judge.py` + `load_ci` 里一小段）**：分支运行导入完成后，**按工作流列运行，不带任何筛选参数**：先查 `actions/workflows?per_page=100`（工作流很少，一页即可，仍用 `_list_all`，键为 `workflows`），挑出 `path` 在 `_TRUSTED_PATHS` 内且为 `auto-merge` 的工作流（兼容 `.yml` 与 `.yaml`），再对每个这样的工作流查 `actions/workflows/<id>/runs?per_page=100`（分页，键为 `workflow_runs`）。**不得带 `event`、`branch`、`status`、`head_sha`、`created`、`actor`、`check_suite_id` 任何一个参数**：GitHub 文档规定带这些参数的运行列表每次搜索最多返回 1,000 条，超出会静默截断而 `_list_all` 发现不了（设计评审 2 用 `total_count=1001` 的桩复现：返回 1000 条、无任何发现、目标丢失）；本仓库 `event=workflow_run` 的运行已有 479 条，约一个月就会超限。查询与挑选整体写成 `events_judge` 里的一个函数（`list_all` 以参数注入，不导入 `events_io`），`events_io.load_ci` 只调用它并把挑出的运行交给 `_download_run`。结果里客户端筛出满足以下**全部**条件的运行：`path` 在 `_TRUSTED_PATHS` 内、`event == "workflow_run"`、`display_title` **整串等于** `auto-merge PR #<pr> @ <resolved>`（`resolved` 是 PR 当前的 head；用字符串相等比较，不用子串或宽松正则）。形状为 `auto-merge PR #<pr> @ <另一个 40 位小写十六进制>` 的运行归入「其他 head」，与分支运行一样汇总成一条 `head_mismatch` 发现，不导入。
4. **导入核对**：对挑出的判定运行，下载其事件包，走现有的 `_download_run` / `_import_package`，但期望值取该运行**自己的** API 记录：`head_sha` 取运行的 `head_sha`（main 的提交），`head_branch` 取运行的 `head_branch`（`main`），而不是 PR 的值；`run_id`、`run_attempt`、`job`、仓库、事件 `source` 的核对都不变。API 记录里 `head_sha` 不是 40 位十六进制、`head_branch` 不是非空字符串时，记 `api` 发现并跳过该运行。
5. 第二段查询（工作流列表或任一工作流的运行列表）失败（`_list_all` 返回 `None`）时，已导入的分支运行结果保持，只多一条 `api` 发现，不整体失败；分页 50 页不收敛沿用 `_list_all` 的既有报告。
6. 事件导入后 `trace_id` 保持包里原样，不改写：**同一个包里有两种 trace**（实测 #187 的判定包：`cli.policy` 等事件是 `main@<短 SHA>`，`route.facts`、`route.result` 等判定主体事件是 PR 分支），两种都原样保留；`trace <PR>` 按 PR 分支查到判定主体事件。
6a. **判定运行没有事件包的语义**：运行名只在 `judge` 会执行时才带关联名（第 1 条），所以关联名运行无事件包就是真缺失，沿用现有 `artifact_missing` 发现；上游 CI 失败而 `judge` 被跳过的运行是普通名，根本不会被挑出，不产生任何发现。
7. CHANGELOG「Unreleased」两行（英文）：加载器能导入 auto-merge 判定运行的事件（route 结果进入 `trace <PR>`、合并账本与 `audit`）；**Migration:** 业务仓库升级后需从 `templates/` 重新复制 `.github/workflows/auto-merge.yml`（新增 `run-name`，`upgrade` 不碰 `.github/`）；没复制时判定运行的事件照旧导入不进来（与现状相同，不报错）。

## 白名单

- `templates/.github/workflows/auto-merge.yml`（只加顶层 `run-name`）
- `engine/core/events_judge.py`（新增，约 50 行：运行名生成与解析、判定运行挑选）
- `engine/core/events_io.py`（只改 `load_ci` 加第二段查询与导入循环，并导入新模块；**当前 773 行，净增不得超过 20 行**，最终必须 ≤ 800）
- `tests/test_events_judge_runs.py`（新增，新测试全放这里；可以 `import` 其他测试模块里的桩与夹具辅助）
- 七个既有测试文件的**假客户端路由**（设计评审 2 实测：新增的 `actions/workflows` 查询会撞上它们，`AssertionError` 或「api 响应形状不符」发现，而 `trace` 测试要求 `findings=[]`）。**每处只加一条路由，不改任何断言、不动其他代码**：
  - `tests/test_audit_events.py`（`FakeGh` 路由分发，`raise AssertionError(f"FakeGh 未配置的路由…")` 之前）
  - `tests/test_audit_completeness.py`、`tests/test_audit_ledger.py`、`tests/test_audit_reconstruction.py`、`tests/test_ledger_equivalent_head.py`（同样的 `FakeGh` 路由分发，同一位置）
  - `tests/test_trace_events_cli.py`（`FakeGh.api` 里，在按页返回运行列表之前）
  - `tests/test_gh_json_fields.py`（T718 新增的 `_RunsStub.api`，在 `raise AssertionError(f"未预期的 API 路线…")` 之前）
  新增的路由统一为：路径匹配 `repos/<owner>/<repo>/actions/workflows` 时返回 `{"total_count": 0, "workflows": []}`（`FakeGh.api` 版本按 `"/actions/workflows?" in route` 判断，且不影响 `calls` 记录）。不得靠捕获 `AssertionError`、忽略坏响应或识别测试桩绕过；这七处桩的断言一条都不许动。
- `CHANGELOG.md`

## 消费方扫描（命令与输出，设计方 2026-10-07 执行；本仓库 cf45449）

```
$ wc -l engine/core/events_io.py → 773（质量棘轮 800：tests/test_split_modules.py::test_line_budgets；B119 的教训，任务书先算余量）
$ grep -rn "load_ci" engine
engine/core/events_io.py:734 (def)  engine/reports/audit.py:515  engine/reports/ledger.py（经 events_io.load_ci / 注入的 client）  engine/reports/trace.py（--ci）
→ 所有 CI 事件导入都经 load_ci，改一处全部生效
$ grep -rn "_TRUSTED_PATHS\|_download_run\|_import_package" engine tests
engine/core/events_io.py（定义与 load_ci 内使用）；tests 下无直接引用（设计评审 2 另用 AST 扫描确认），既有测试都经公共入口 `load_ci` 与注入的桩客户端，签名不改
$ gh api repos/<仓库>/actions/runs?event=workflow_run&per_page=1 --jq .total_count → 479（约两周的量）。GitHub 文档：带 event/branch/status 等参数的运行列表每次搜索最多 1,000 条，所以改用「按工作流列运行、不带筛选」（目标终态第 3 条）
$ 真实判定运行 37597033237：display_title="auto-merge"（现状）、pull_requests=[]、origin.head_sha == API head_sha == main 提交（见「病灶」）
$ diff templates/.github/workflows/auto-merge.yml .github/workflows/auto-merge.yml → 无差异（本仓库的副本与模板一致；同步由设计方另开 PR）
```

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| `audit.py`、`ledger.py`、`trace.py` | 无需 | 都经 `load_ci`，返回字典形状不变 |
| 引用 `load_ci` 的七个既有测试文件的假客户端 | **需要（已列白名单）** | 设计评审 2 逐个实例化并经真实 `_list_all` 查询 `actions/workflows?per_page=100`（设计方补查出第七个：T718 的 `tests/test_gh_json_fields.py::_RunsStub` 对未知路线同样 `AssertionError`；判定标准是「假客户端对 `actions/runs` 有路由」：`grep -n "actions/runs" tests/*.py` 命中的七个文件，`test_events_io.py` 只有一条注释提到 `load_ci`、无假客户端）：`test_audit_events`、`test_ledger_equivalent_head`、`test_audit_ledger`、`test_audit_reconstruction`、`test_audit_completeness` 抛 `AssertionError`（未配置的路由），`test_trace_events_cli` 返回了运行列表而产生「api 响应形状不符」发现。每处只加一条返回空 `workflows` 的路由（见白名单）；`test_events_io.py` 的桩直接换掉 `load_ci` 的依赖，不受影响（实现前再核对，必须通过） |
| 七个假客户端以外的测试 | 无需 | `grep -n "actions/runs" tests/*.py` 的其余命中（`test_run_record_privacy.py` 的一个 URL 字符串）与 `load_ci` 无关 |
| 模板相关既有测试（`test_native_automerge`、`test_contract_route`、`test_ci_events_workflows`、`test_install`） | 实现前核对，必须仍通过 | 它们用自带的简化 YAML 解析器（`tests/test_ci_events_workflows.py::parse_workflow`）读模板，没试过顶层的 `run-name`；表达式里有 `&&`、`||`、引号与冒号，按已有 `if: >-` 的折叠块写法书写并先跑这几个测试。若解析器处理不了，**停下向设计方报告**（改解析器在白名单外，不得自行扩大） |
| 本仓库 `.github/workflows/auto-merge.yml` | 设计方另开 PR | 不在白名单 |
| 消费方 Agent-Notification | 升级时按 Migration 重新复制 | 它的 `consumer-contract` CI 只跑升级与 verify，不依赖 run-name |

## 设计方的绕过与失效清单

| 可能的问题 | 处理 |
|---|---|
| 伪造运行名把别人的判定事件导进这个 PR | 运行名只能由默认分支上的工作流定义生成（`workflow_run` 不执行 PR 分支的定义）；且必须同时满足 `path` 可信、`event == workflow_run`、包 `origin` 与该运行 API 记录逐项一致、事件 `source` 与运行一致；验收有各条件单独失败的反例 |
| 运行名匹配太宽（子串、大小写、尾随空白、换行） | 只做整串相等；验收覆盖：多余前后缀、大写十六进制、短 SHA、PR 号前缀相同（`#18` 对 `#187`）、换行 |
| 被评估的 head 过期后仍导入 | 运行名里的 SHA 必须等于 `resolved`；其他 head 的归入 `head_mismatch`，不导入 |
| 沿用 PR 的 `head_sha`/`head_branch` 去核对判定运行包，必然 `origin_mismatch`（新接线的回归风险） | 对判定运行改用该运行自己的 API 值；验收有「包 origin 写成 PR head 反而被拒」的反例，防止核对被架空 |
| 第二次查询失败拖垮已导入的结果 | 目标终态第 5 条；验收覆盖 |
| 运行列表超过 1,000 条被平台静默截断 | 不带任何筛选参数（目标终态第 3 条）；验收有「目标运行落在第 11 页之后仍能找到」与「请求路由不含筛选参数」两条，变异把 `event=workflow_run` 加回去须失败 |
| 判定运行很多时分页过多 | 沿用 `_list_all` 的 50 页上限与不收敛报告；按工作流列、无筛选，当前约 7 页，已知成本，按 PR 创建时间过滤列为后续待办，本任务不做 |
| 上游 CI 失败、`judge` 被跳过的运行被误当成「包缺失」 | 运行名只在 `judge` 会执行时带关联名（第 1 条）；验收有「跳过的运行是普通名、不被挑出、无发现」与「`judge` 已执行但包缺失 → `artifact_missing`」两个反例；耦合测试断言 `run-name` 的条件与 `judge.if` 逐字相同 |
| 老模板（没有 run-name）的仓库 | 判定运行匹配不到，行为与现状相同，不新增报错；Migration 条目提示重新复制 |
| `events_io.py` 超 800 行 | 新逻辑放新模块，`events_io` 净增 ≤ 20 行；验收断言两个文件的物理行数 |
| 新模块与 `events_io` 循环导入 | 新模块只放纯函数（运行名生成与解析、挑选），不导入 `events_io`；验收用 AST 断言（含 `from engine.core import events_io` 与相对导入两种写法，参考 T718 的 `imports_events_io`） |
| 模板与代码对运行名的格式各写一份，漂移 | 验收有耦合测试：从模板里取出 `run-name` 的格式串，代入示例值，必须等于 `events_judge` 生成的名字 |

## 非目标

- 不改 `judge` 以外任何 job 的行为，不改模板里其他步骤，不改 `.github/`（本仓库自己的副本，由设计方同步）。
- 不加按创建时间的过滤、不处理同一运行多次重试（`run_attempt`）导致的旧 attempt 包的物理名发现（既有行为）。
- 不改 `audit` 的 `missing_route` 规则，不处理 B118。
- 白名单以外的文件一律不改；共用合同 C0 同样适用。

## 前置条件

- 工作区基于最新的 `origin/main`。按函数名定位，行号只作参考；只有函数、调用点或接口本身对不上时，才停下向设计方报告。

## 验收

具名测试为本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B117 | **运行名耦合**：模板顶层有 `run-name`，格式串经示例值（PR 号、40 位 SHA）代入后等于 `events_judge` 生成的名字；其他情形的取值为 `auto-merge`；取值用的是 `github.event.workflow_run.pull_requests[0].number` 与 `github.event.workflow_run.head_sha`；**`run-name` 里的条件与 `judge.if` 三个条件逐字相同**（从模板文本里取出两处比较，忽略空白） | 夹具 | `tests/test_events_judge_runs.py::RunNameTest` | 模板或代码任一侧改格式；`judge` 条件改了而运行名没跟着改 |
| 不挂规格：B117 | **挑选**：给定一组 API 形状的运行（`id`、`path`、`event`、`display_title`、`head_branch`、`head_sha`、`run_attempt`），只有 `path` 可信、`event == workflow_run`、`display_title` 整串等于目标名的被挑出；`#18`、`#1870`、大写十六进制 SHA、短 SHA、前后多空白、尾随换行、`display_title` 缺失或非字符串、`event` 为 `push` 的都不被挑出；另一个 head 的同 PR 判定运行计入「其他 head」 | 夹具 | `::SelectTest` | 匹配过宽或过窄 |
| 不挂规格：B117 | **端到端导入**：假客户端提供 PR 信息、分支运行（一个 harness 运行）、工作流列表（含 auto-merge 工作流）、该工作流的运行列表（一个匹配的判定运行，`head_branch=main`、`head_sha` 为 main 的提交）与两个运行的事件包 zip；判定包按真实形状含**两种 trace**（`cli.policy` 为 `main@<短 SHA>`，`route.facts`/`route.result` 为 PR 分支）；`load_ci` 导入两个包的事件，两种 trace 都原样保留；再调用一次，全部「跳过」、新增为 0 | 夹具 | `::LoadJudgeRunsTest` | 当前代码查不到判定运行；改写 trace |
| 不挂规格：B117 | **消费者效果**：把上一行导入后的事件（`events_io.query`）装进最小的假审计对象，调用 `audit_completeness.check`：自动合并案例（合并事件 `merger_type=Bot`）不再报 `missing_route`；把 `route.result` 的 `auto_merge` 改为 `false` 时仍报 `missing_route`；没导入判定包时仍报 | 夹具 | `::ConsumerEffectTest` | 导入了却没被审计认出，或审计被放宽 |
| 不挂规格：B117 | **进入合并账本**：复用上一行的导入夹具与 `tests/test_audit_ledger.py` 的账本夹具，调用真实 `ledger.build_ledger`：账本含该 PR trace 的 `route.facts`、`route.result` 及对应 `ci:…:judge` 链，**`main@<短 SHA>` trace 的事件不混入该 PR 的账本**；没导入判定包时账本没有 `route.result` | 夹具 | `::LedgerEffectTest` | 导入了却没进账本，或把别的 trace 混进账本 |
| 不挂规格：B117 | **分页上限与请求形状**：运行列表共 1,001 条、目标运行在第 11 页 → 仍被找到并导入；断言发给客户端的路由里不含 `event=`、`branch=`、`status=`、`head_sha=`、`created=`、`actor=`、`check_suite_id=`；工作流列表里没有 auto-merge 工作流（老仓库）→ 不查运行、无发现、无报错；`.yaml` 扩展名的工作流同样被识别 | 夹具 | `::PaginationAndShapeTest` | 回到带筛选的查询而被截断 |
| 不挂规格：B117 | **核对仍严格**：判定运行的包 `origin.head_sha` 写成 PR 的 head（而不是该运行的 API `head_sha`）→ `origin_mismatch` 且不导入；`origin.head_branch` 写成 PR 分支 → 同；`origin.run_id` 不符、`repository` 不符、事件 `source` 不是 `ci:<run>:<attempt>:<job>`、artifact 物理名与运行不符、artifact 过期、zip 损坏，各有对应发现；运行 API 记录的 `head_sha` 不是 40 位十六进制或 `head_branch` 为空 → `api` 发现并跳过该运行 | 夹具 | `::StrictVerificationTest` | 核对被架空 |
| 不挂规格：B117 | **隔离与降级**：另一个 PR 号的判定运行不导入；`path` 不可信的运行不导入；运行名里 head 过期的计入一条 `head_mismatch` 发现且不导入；第二段查询（工作流列表或运行列表）失败（`api` 抛错或形状不符）时，分支运行已导入的结果保持，只多一条 `api` 发现；没有任何判定运行时不报错、不新增发现；**同一 PR/head 上游失败、`judge` 被跳过**的运行（普通名 `auto-merge`、无事件包）不被挑出、无任何发现；**`judge` 已执行（关联名）但包缺失** → 一条 `artifact_missing` | 夹具 | `::IsolationAndDegradationTest` | 失败扩散、误导入或误报缺包 |
| 不挂规格：B117 | **体量与结构**：`engine/core/events_io.py` 与 `engine/core/events_judge.py` 的物理行数都不超过 800，且 `events_io.py` 相对基线 773 行净增不超过 20 行；`events_judge` 的 AST 中没有指向 `events_io` 的导入（`import`、`from … import`、别名、相对导入四种写法都检） | 夹具 | `::SizeAndImportTest` | 超标或循环 |
| 不挂规格：B117 | 既有测试全部通过 | 夹具 | `bin/verify --full` | 回归 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 新模块 `events_judge.py`（运行名生成、整串匹配、挑选）与模板 `run-name`，先写耦合与挑选测试 | `engine/core/events_judge.py`、`templates/.github/workflows/auto-merge.yml`、`tests/test_events_judge_runs.py` | `python3 -W error::ResourceWarning -m unittest tests.test_events_judge_runs -v` | 验收第 1–2 行 |
| 2 | `load_ci` 加第二段查询与导入；**先**在七个既有假客户端里各加一条空 `workflows` 路由（不改断言），再补端到端、核对与降级测试 | `engine/core/events_io.py`、`tests/test_events_judge_runs.py`、白名单里的七个既有测试文件 | 同上，加 `tests.test_events_io tests.test_trace_events_cli tests.test_audit_events` | 验收第 3–6 行 |
| 3 | CHANGELOG；全量验证，整理交付证据 | `CHANGELOG.md` | `bin/verify --full` | 验收第 7 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收、G2（CI 的 `consumer-contract`）与定向变异复核，以下变异必须各自被对应断言抓住：
- 运行名匹配改成子串或 `startswith`、忽略大小写、去掉 `event == workflow_run` 或 `path` 条件、去掉 SHA 必须等于 `resolved` 的条件、PR 号只比前缀；
- 判定运行的包改用 PR 的 `head_sha`/`head_branch` 核对（应拒绝 main 的值）；去掉 API 记录形状校验；
- 第二段查询失败时整体返回失败、丢掉分支运行的结果；运行列表请求改回带 `event=workflow_run` 或其他筛选参数；改写判定包事件的 `trace_id`；`run-name` 的条件去掉或与 `judge.if` 不一致；
- 模板里改了 `run-name` 的格式或取值字段（耦合测试须失败）；
- `events_judge` 反向导入 `events_io`（`from engine.core import events_io` 写法也要抓住）；`events_io` 净增超过 20 行。

**合并后的接续（设计方，不在本任务内）**：① 另开 R3 PR 把模板的 `run-name` 同步到本仓库的 `.github/workflows/auto-merge.yml`，用户合并；② 再走一次引擎自升级 PR（R3）；③ 随后在下一个自动合并的 PR 上跑 `trace --ci`、`audit`，应能看到 `route.result` 且没有 `missing_route`。
