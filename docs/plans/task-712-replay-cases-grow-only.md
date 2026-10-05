---
task: T712
class: K5
risk: R2
designer: claude-code
size: small
architecture: true
spec_refs: []
no_spec_reason: 用户 2026-10-05 决定「缺陷修复可以自动合并，实际代码的开发合并都走验收，约束不足时再加」；回放清单只追加时不再强制人审，无产品规格验收编号
budget:
  wall_clock_min: 120
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T712：回放清单只追加新用例时按 R2 判级（缺陷修复可走合同制路径）

负责方：**派发任务**。设计方：claude-code；独立评审方：Codex（单会话）。依据：用户 2026-10-05 决定（缺陷修复也走验收自动合并）。

## 病灶（代码证据）

- 带 `Defect:` 的修复 PR 必须在 `.harness/project/replay_cases.py` 里加入回放用例（`engine/checks/evidence.py` 的回放覆盖检查），而 `.harness/**` 命中 `rules.toml [risk] r3`，所以 `engine/routing/risk.py` 的 `classify_file` 把整个修复 PR 判为 R3、类别 K7，永远转人审。K4（缺陷修复）在 D1 之后虽然是 L4，实际上走不了合同制路径。
- 不能简单改成「只增行就放行」：回放清单里除了 `CASES`，还有 `BASELINE`、`GUARDED`、`DEFERRED`。在 `DEFERRED`、`GUARDED` 里**新增一行**就能让某个缺陷免于回放，这是削弱护栏，却也是「只增行」。所以必须按结构比较。
- 判级在 CI 里由 main 上的引擎执行，PR 内容只能当数据读，不能执行（`engine/core/cases.py` 的 `load_project_cases` 用 `exec_module` 加载，判级时不能复用）。

## 目标终态

1. `rules.toml [risk]` 新增可选键 `grow_only_cases`（路径列表，缺省为空，即行为与现在完全相同）。模板 `templates/.harness/config/rules.toml` 中以注释形式说明，不默认启用：消费方要自己决定是否放宽。
2. `classify_file` 在判 R3 之前（与 `shrink_only` 同一位置、紧随其后）增加判断：路径命中 `grow_only_cases`、状态为 `M`，且下面第 3 条的结构比较成立时，返回 R2，`rule` 为 `"grow_only_cases"`，理由写明「回放清单只追加新用例」。其余任何情况都不拦截，照原有规则继续判定（对 `.harness/**` 即 R3）。
3. 结构比较（新函数，只用 `ast.parse` 读 base 与 head 两个版本的文件内容，**不执行**）同时满足才成立：
   - 两个版本都能解析；
   - 顶层语句数量相同，除 `CASES` 的赋值语句外，其余每条顶层语句的 `ast.dump` 完全相同（`BASELINE`、`GUARDED`、`DEFERRED`、import、文档字符串都不得改动）；
   - `CASES` 在两个版本里都是对列表字面量的赋值；head 列表的前 len(base) 项与 base 逐项 `ast.dump` 相同，且 head 至少多 1 项；
   - 每个新增项都是对名字 `Case` 的调用，只有关键字参数，每个参数值都能被 `ast.literal_eval` 求值。
   - 任何一步失败（含读取或解析异常）都返回「不成立」，不抛异常。
4. 函数体量与复杂度：结构比较放在独立的辅助函数里，避免抬高 `classify_file` 的复杂度（`quality-baseline.json` 的 `complex_functions` 不得上升）。
5. CHANGELOG「Unreleased」加一行说明新键（可选、缺省不启用，无需迁移）。

本仓库自己的配置（把回放清单登记进 `grow_only_cases` 和 `autonomy.toml [contract_route] allowed`）属于 R3 配置，由设计方在升级 PR 中另行提交，不在本任务内。

## 白名单

- `engine/routing/risk.py`
- `templates/.harness/config/rules.toml`（只加注释说明的可选键）
- `tests/test_risk_grow_only_cases.py`（新增）
- `CHANGELOG.md`

消费方扫描（检查单第 2 项）：

| 消费方 | 是否需要配套改动 | 理由 |
|---|---|---|
| `engine/routing/policy.py` 的 `machine_class`、`_contract_candidate` | 无需 | 读 `risk.files` 的等级与路径；回放清单判为 R2 后，带 `Defect:` 的 PR 按现有逻辑判为 K4，是否走合同制由 `[contract_route] allowed` 决定 |
| `engine/routing/run_check.py`、`engine/checks/evidence.py` | 无需 | 不读判级结果 |
| 既有判级测试（`tests/test_risk*.py`、`tests/test_events_route.py` 等） | 无需 | 新键缺省为空，原有判定逐字不变（验收第 7 行断言） |
| Agent-Notification | 无需迁移 | 新键可选、缺省不启用 |

## 非目标

- 不改回放清单本身、不改 `cases.py` 的加载方式、不改证据检查。
- 不改本仓库 `.harness/config/`（设计方另行提交）。
- 不支持新建回放清单文件（状态 `A` 照原规则判定）。
- 白名单以外的文件一律不改。
- 共用合同 C0 同样适用。

## 前置条件

- 工作区基于最新的 `origin/main`。按函数名定位，行号只作参考；只有函数、调用点或接口本身对不上时，才停下向设计方报告。
- 与 T711 白名单不交叉，可以并行。

## 验收

具名测试为本任务要新建的断言，目前还不存在；不能把这张表当成已通过的证据。夹具为匿名临时 git 仓库，`rules.toml` 在夹具内配置 `grow_only_cases = [".harness/project/replay_cases.py"]`（第 7 行除外）。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：K4 合同制 | 在 `CASES` 末尾追加一个全字面量参数的 `Case(...)`：该文件判 R2、`rule` 为 `grow_only_cases`；与 `engine/x.py`、`tests/` 一起改且提交带 `Defect:` 时，整体判 R2、`policy.machine_class` 为 K4 | 夹具 | `tests.test_risk_grow_only_cases.GrowOnlyCasesTest.test_append_case_is_r2` | 修复 PR 仍被判 R3 |
| 不挂规格：K4 合同制 | 修改或删除已有 `Case`、调换顺序，都判 R3 | 夹具 | `…test_modify_or_delete_case_stays_r3` | 削弱已有回放被放行 |
| 不挂规格：K4 合同制 | 在 `DEFERRED`、`GUARDED` 里新增条目，或改动 `BASELINE`，即使只增行也判 R3 | 夹具 | `…test_exemption_changes_stay_r3` | 借「只增」豁免回放 |
| 不挂规格：K4 合同制 | 新增的 `Case` 含非字面量参数（函数调用、f 字符串、名字引用），或新增项不是 `Case(...)` 调用，判 R3 | 夹具 | `…test_non_literal_case_stays_r3` | 可执行内容混进清单 |
| 不挂规格：K4 合同制 | head 版本在追加 `Case` 的同时加入一条有副作用的顶层语句（写一个标记文件）：判 R3，且判级过程**没有**创建该标记文件 | 夹具 | `…test_never_executes_pr_content` | 判级执行了 PR 内容 |
| 不挂规格：K4 合同制 | head 版本有语法错误、base 中没有该文件、状态为 `A`，都判 R3 且不抛异常 | 夹具 | `…test_unparsable_or_new_file_stays_r3` | 异常中断判级，或新文件被放行 |
| 不挂规格：K4 合同制 | 不配置 `grow_only_cases` 时，追加 `Case` 照旧判 R3（行为与现在一致） | 夹具 | `…test_default_off` | 消费方被静默放宽 |
| 不挂规格：K4 合同制 | 既有测试全部通过、零修改；`complex_functions` 不上升 | 夹具 | `bin/verify --full` | 回归 |

## 步骤与提交顺序

以下是实现与验证的顺序，**不是当前的提交授权**。只有在用户授权派发之后，执行方才按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 结构比较辅助函数与 `classify_file` 的接入，新增回归测试 | `engine/routing/risk.py`、`tests/test_risk_grow_only_cases.py` | `python3 -W error::ResourceWarning -m unittest tests.test_risk_grow_only_cases -v` | 验收第 1–7 行 |
| 2 | 模板注释与 CHANGELOG；全量验证，整理交付证据 | `templates/.harness/config/rules.toml`、`CHANGELOG.md` | `bin/verify --full` | 验收第 8 行 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收、G2 与定向变异复核，以下变异必须各自被对应断言抓住：
- 结构比较改为只看「没有删除行」；
- 去掉「其余顶层语句不变」的检查；
- 新增项不再要求参数是字面量；
- `grow_only_cases` 缺省时也生效；
- 结构比较改为调用 `load_project_cases` 风格的 `exec_module` 加载。

同一失败连续三轮没有新证据时，停止这条路径。不得自行扩大白名单、修改任务书、实现非目标。

## 修订记录 1（2026-10-05，设计方采纳 #146 独立评审的发现）

#146 的 Codex 评审（head `09c4108`）判「不通过」。设计方核实后，以下两处属实，都是任务书没写到：

1. **`CASES` 赋值语句本身除列表外的部分没有被要求不变**（严重）。目标终态 3 只要求「其余顶层语句不变」和「列表前缀不变」，对 `CASES` 这条语句的类型、目标、注解没有约束。评审方构造的反例：在追加合法 `Case` 的同时，把注解改成 `(BASELINE.clear() or list[Case])`，判级仍为 R2；而回放清单没有延迟注解，加载时会执行这个表达式，清空 `BASELINE`。这正是本任务要防的「借追加削弱护栏」。
2. **CHANGELOG 漏了 `**Migration:**` 标记**（一般）。按 AGENTS.md，新增配置项要写 `**Migration:**` 条目，`upgrade` 靠它提示。任务书原文写的是「无需迁移」，不准确。

裁决：

- 目标终态 3 补一条：两个版本的 `CASES` 赋值语句，**除 `value`（列表）外完全相同**，包括语句类型（`Assign` 与 `AnnAssign` 不得互换）、赋值目标、注解、`simple` 标志。实现方式：复制两条语句节点、把 `value` 置空后比较 `ast.dump`。
- 验收补一行：

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：K4 合同制 | 追加合法 `Case` 的同时：①把 `CASES` 的注解改成有副作用的表达式；②在普通赋值与带注解赋值之间互换；③改动赋值目标形式。三种都判 R3 | 夹具 | `tests.test_risk_grow_only_cases.GrowOnlyCasesTest.test_cases_statement_other_parts_unchanged` | 改注解等方式借追加执行代码、削弱护栏 |

- 变异清单补一项：「`CASES` 语句只比较列表，不比较注解」。
- CHANGELOG 的条目改为 `**Migration:**` 开头，写明：新键可选、缺省不启用，现有配置无需改动。
- 白名单、其余目标终态与验收不变；已完成的提交保留，在此基础上续跑。
