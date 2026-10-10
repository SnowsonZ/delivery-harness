# PR #7 独立评审：可验证交付 harness（2026-09-26）

> 状态：迁自 Agent-Notification（2026-10-09 快照，正文未改；业务库不再留存 harness 抽取与设计文档，只留引用）。文中指向业务库专属文件的链接已改为该仓库的绝对地址。
评审对象：PR #7（分支 `claude/gracious-bardeen-pi7ysv`，head `a109782`，base `origin/main` `d8bde8d`）。评审依据 [评审交接书](2026-09-25-harness-review-brief.md) 第 0 节的原始需求与对齐决定。

方法：在独立 worktree（`/tmp/pr7-review`）与独立 venv（ruff 0.16.8，与 `requirements-dev.txt` 锁定一致）中复现全部命令；破坏性操作只在 `/tmp` 临时仓库中尝试；结论只基于本人命令输出与 GitHub 上 PR #7 head（run 36157693749）的 CI 运行。未修改实现、未推送。

## 结论：修改后可合并

核心机制真实、可运转、方向符合原始需求。四条标杆验收中前三条（事故回放、验收编号覆盖、三处同口径）经本机 + CI 复现成立；第四条（真实任务试跑）尚未执行，PR 自己也如实标注 P6 为 ⏳。

两个「严重」发现（PR7-R1、PR7-R2）都是 `risk.py` / `evidence.py` 的小改动（各约 20 行内 + 负例测试），建议合并前修掉——本 PR 是 R3 基础设施，合并后这些就是现役判定器，带病上线再改又需一轮 R3 审批；且 E1（L4 自动合并）接通前它们必须先堵。其余发现可随后续 PR。上线顺序上，A4（ruleset 导入）的优先级应高于 A3（本机装守卫）：PR7-R3 表明 git 层对能写工作区的执行者不构成硬边界，服务端是唯一不可绕过的一层。

## 是否偏离原始需求

**判定：未偏离，无悄悄扩大；但「标杆」尚缺最后一环（有效性未经真实任务验证），且角色分工尚未实际运转。**

对照交接书第 0 节逐条核对：

- **「把任务转化为可验证的问题、全流程」**：方案 §2 的五条操作定义与报告核心结论一一对应；全流程落点表（方案 §6）覆盖需求→度量八个环节。抽查证实映射是实质的而非纸面的：`acceptance.py` 真的会因覆盖引用失效而失败（实验：从 `acceptance-gaps.txt` 删掉 DR14 行，检查器报「可自动化条目没有测试，也不在 acceptance-gaps.txt 中」）；`verify --full` 在本机 macOS 真的编译并运行三项 Swift 检查 + 19 个注入回放。
- **对齐决定未违背**：未打 tag、未发版、未导 ruleset（只提交了导入文件，等用户操作）；P1–P6 合并为一个 PR（对齐决定 4）；两处产品代码改动在方案 §8「只为可测而必需的重构」授权内，且实测行为等价（见 PR7-R5 之外的核对记录）。六份规格的验收表抽查（DR12/DR13/DR14 与 main 原文逐字一致、DR1/DR6 语义保留）未见夹带新要求。
- **观察 1（角色分工是纸面状态）**：方案 §3 规定设计与评审方「不替执行者提交实现代码」，但本 PR 全部 12 个提交的作者即设计评审方（Claude）。建设期由谁实现用户未限定，A1 独立评审也正为此设置，因此不算违规；但要如实记录：**「执行方实现、评审方评审」的分工尚未运转过一次，trial-001 是第一次检验**。在此之前，「标杆 = 有效（有数据证明）」只有注入侵炎式的证据，没有真实交付流的证据。
- **观察 2（有效性证据的成色）**：19 个注入全部拦住是「测试集能抓住已知缺陷形态」的证据，不等于「能抓住下一类未知缺陷」。变异存活清单里 `_merge_day_tasks` 的 `LtE→Lt`（daily_report.py:1410）在「合计相等但明细不同」时可观察，恰与 H0925-4 同域——作者已在 §13 E5 登记为待区分项，不算新发现，但它说明 76% 的得分里有真实缺口，不只是等价变异。

## 发现

| 编号 | 严重度 | 问题 | 证据 | 修复要求 |
|---|---|---|---|---|
| PR7-R1 | 严重 | `Defect: <编号> doc` 标记可让任意产品代码改动免检：`evidence.py` 对 `doc_only` 只列提交，不核对改动范围；证据表渲染为「文档类修复」，`code_files` 被收集但不展示。规格约定 doc 类是「纯规格或文档修复」，机器未核对「确实只改了文档」 | 复现（`/tmp` 临时仓库，`evidence.analyse`）：提交改 `scripts/app.py`（产品代码）+ message 带 `Defect: FAKE-8 doc` → `ok=True`、`doc_only=True`、problems/warnings 均空、`code_files=['scripts/app.py']`；`tests/test_harness.py::test_doc_only_defect_needs_no_test` 只测了真 doc 场景，无负例 | `analyse` 中 `doc_only` 且 `code_files` 非空时记 problem；`render_markdown` 对 doc 类如常列出代码改动列；补「doc 标记 + 产品代码改动」负例测试 |
| PR7-R2 | 严重 | 已有测试文件「纯追加」判 R0 且不产生任何 flag（`risk.py:76-78`），但纯追加可以禁用任意已有测试（模块级 monkeypatch，如 `T.test_x = _noop`），配合 `Risk: R1` 声明可整单降为 R1（自动合并档）。击穿「可验证」定义第 4 条「执行者改不了判定器」 | 复现（`risk.classify` + unittest）：改产品行为（value 1→2）+ 测试文件末尾追加 3 行禁用 `test_value` + 提交带 `Risk: R1` → `level=R1`、`flags=[]`、note 为「只改产品代码、已有测试与黄金快照零改动 → R1」，且测试套件退出码 0（`OK`）。`tests/test_harness.py::test_appending_to_existing_test_is_r0` 只测了无害追加 | 二选一：(a) 已有测试文件的追加同样产生 flag（保守）；(b) `verify` 增加测试完整性检查（对 tests/ 做模块级对测试属性重赋值的 AST 检测，或记录并比对测试方法数只增不减）。无论哪种，E1 接通前必须落地；补回放用例 |
| PR7-R3 | 严重 | git 层守卫的规则与解释器都在执行者可写的工作区：篡改 `harness/rules.toml` 后本地护栏立即空转；钩子解释器优先用不入库的 `scratch/iterm-probe-venv`（同样可替换）。规格 §7 已知边界只列了 `--no-verify` 与改 `core.hooksPath` 两条绕过，低估了绕过面；对无 Agent 层的 Zcode/Pi，在 A4（ruleset 导入）完成前这是 X3 重演的完整通路 | 复现（`/tmp` 临时仓库，真实钩子）：对照实验——非快进 `git branch -f main <孤儿提交>` 被拒（「改写保护分支 refs/heads/main……不是快进」）；先提交清空 `protected_branches` 的 rules.toml 后同样操作**放行**（main 被改写为孤儿提交，无任何拦截）。`git_guard.py:120` `load_rules()` 读工作区文件；`.githooks/_runtime` 解释器路径优先 `scratch/` | (1) 规格 §7 已知边界补记此条，并注明「服务端 ruleset 是唯一硬边界」；(2) 把 A4 的优先级提到 A3 之前（上线顺序）；(3) 可选加固：pre-commit 对 `harness/`、`.githooks/` 的改动增加提示性 flag（自知可被绕过，仅提高可见度） |
| PR7-R4 | 一般 | 「修复前失败」对新增文件类修复退化为 import error：修复实现在新文件时退回 = 删除文件，引用测试必然以 error（非断言失败）结束，只产生 warning，不影响 evidence 结论。此时不能证明测试在检查缺陷行为——缺陷可以原样未修而证据全绿 | 复现：修复提交只新增 `scripts/newmod.py`（缺陷所在 `app.py` 原样不动），测试 `import newmod` 并断言其输出 → `before='error'`、`after='pass'`、`ok=True`，仅 warning「需人工确认」；CI 的 evidence 步骤不会因此失败（exit 0） | `error` 时要求更强的人工确认痕迹（如 review_pack 把 error 类逐条列成「评审必须确认」清单），或规定新文件修复的测试必须经由既有模块路径引用；至少在 delivery-harness.md §2 写明此边界 |
| PR7-R5 | 一般 | 证据退回以整文件为粒度：修复提交之前的其他提交若改过同一文件，退回会一并撤销，引用测试可能因无关原因 fail，被计为有效的「修复前失败」（无法归因） | 复现：提交 C1 改 `value()`、修复提交只修 `broken()`，测试同时断言两者 → 退回后测试因 `value()` 失败 → `before='fail'`、`ok=True`，FAKE-7 的修复本身未被检验 | 交接书已自查到该风险且方向正确；建议：`verify_fail_before_fix` 记录退回文件在 `base..first_sha^` 区间是否被其他提交改过，是则加 warning 暴露归因风险；中期改为按 hunk 退回（`git diff | patch -R`） |
| PR7-R6 | 一般 | `command_guard.py` 的 `gh api` 规则只拦 `-X DELETE`：`-X PATCH` 改远端 ref（服务端强推）、`--method PUT` 经 contents API 直写 main 文件均放行，本地三层全部无感。同族绕过（引号 tag、变量拼接、解释器包装）属 E3 已登记的字符串匹配局限，但 gh api 的 PATCH/PUT 未列入任何已知边界 | 复现（`check_command` 直接调用）：`gh api -X PATCH repos/x/y/git/refs/heads/main -f sha=abc -F force=true` → 放行；`gh api --method PUT repos/x/y/contents/scripts/app.py` → 放行；`gh api -X DELETE .../actions/runs/1` → 拦截。另：`git push origin 'v0.9.0'`（带引号）→ 放行（git 层 pre-push 兜底）；`A=HARNESS_ALLOW_M; B=AIN; env "${A}${B}=1" git push origin main` → command_guard 拦到 push main 但**漏掉变量拼接**，git 层因环境变量真实生效而放行 | (1) gh api 按动词收紧（PATCH/PUT 到 refs、contents、workflows 等路径列入拦截）；(2) 把「拼接变量可绕过 HARNESS_* 检查」补进 §7 已知边界（git 层信任环境变量）；(3) E3（结构化解析）提优先级 |

未构成新发现、但复核过的项：交接书第 5 节已知问题的严重度评估基本准确；其中「命令守卫字符串匹配误报」（4 次记录）在评审过程中再次出现（本评审的实验命令含 `git filter-repo` 字样被拦 2 次，换写法后通过），维持原严重度即可。

## 对方案的整体意见

**这套 harness 与「单人 + 多 Agent 并行」的实际病灶是对症的，不是过度设计。** v0.8.0 的 19 项缺陷 + 6 类过程失败里，代价最大的是过程失败（X1 三次、X3 两次、5 轮评审），本 PR 的火力（证据脚本、三层护栏、机器风险分级）正好集中在那里。结构上也克制：不上 Temporal/OTel/合并队列，人工验收明确列清单不假装自动化，都符合单人项目的裁剪。

四个具体意见：

1. **R0/R1 通道的可靠性是当前短板，也是 L4 的前提。** PR7-R1、PR7-R2 都打在这条通道上。另外要意识到 R1 的机器核对（已有测试零改动 + 快照零差异）只能证明「判定器没被动过」，不能证明「行为真的没变」——黄金快照只覆盖 usage 两个子命令。建议在 delivery-harness.md §3 的 R1 行如实标注这个边界（「行为不变的机器证据限于现有测试与快照覆盖面」），避免 R1 被读成比实际更强的保证。
2. **优先级建议调整：A4（ruleset 导入）应先于一切。** PR7-R3 表明本机两层对「能写工作区的执行者」都不是硬边界，服务端是唯一一层。ruleset 不导入，整套三层护栏的第一层就不存在，且 tag 直推即可触发真实发布。这也和发版流程记忆一致（tag push 即远端发布）。
3. **可裁剪项**：`metrics.py` 的 GitHub API 轮次统计（信息价值中等、PR 期间 per_page=100 截断）、review_pack 对验收编号的列举（E8 已列）、quality 棘轮对 20 个 C901 的缓慢治理——保留无妨，但不应再投入。相反，evidence 的归因精度（PR7-R4/R5）和 command_guard 的结构化解析（E3/PR7-R6）值得提前。
4. **「标杆」的最后一块拼图是 trial-001，且应该按原计划只做 DR14 补测试这件小事。** 试跑的价值在于度量真实交付流（复验轮数、声称已修、CI 轮次 vs 32 次基线），不在于任务本身的大小。建议用户尽快指定执行方跑完，再决定 E1 是否接通。

## 复现记录（摘要）

- `bin/verify --full`（本机 macOS，独立 venv，ruff 0.16.8）：10 项全过（tools/lint/hygiene/quality/acceptance/python-tests 365 项/swift-policy/swift-runner/zcode-selftest/replay 19 注入），`ok=true` @ `a109782`。
- `python3 harness/replay.py --list`：25 条基线全覆盖（15 注入 + 5 守卫 + 9 暂缓带原因），退出码 0。
- `python3 harness/acceptance.py`：69 条编号，可自动化 54（53 有测试 + 1 登记缺口 DR14），人工 19，退出码 0。
- `python3 harness/mutate.py --check`：四目标 96%/90%/76%/100%，与 `mutation-baseline.json` 一致，退出码 0。
- `python3 harness/review_pack.py --base origin/main`：风险 R3（34 个护栏路径）；三个 H0925 修复均「修复前失败、修复后通过」（脚本生成）。
- CI（PR #7 head，run 36157693749）：`harness`（12s）与 `build`（3m7s，macos-26 `--strict --full`）均 SUCCESS；PR 正文按模板填写、未手写通过状态，CI 链接指向当前 head 的运行。
- 攻击实验：evidence 三个假通过场景、risk 追加禁用 + R1 组合、git 守卫对照实验（含 rules.toml 篡改、非快进改写、真实强推、tag 移动/删除、filter-repo）、command_guard 30 条正负例，全部在 `/tmp` 临时仓库/纯函数调用中完成，脚本存于评审环境（`/tmp/pr7-attacks/`），未触及真实仓库。
- 两处产品改动等价性：`widgetRunningListed` = `origin != "agent" && inboxActiveListed`，与 main 版 `human.filter { inboxActiveListed }`（`human` 已是非 agent 过滤）逐条件等价；`widgetEntryTitle` 与被替换的内联 `hideTitles ? "" : title` 等价、project 不经隐藏（W6/R15 口径不变）；`cost_thresholds` 与原内联逻辑在 `len<8` 边界与分位索引上一致，`amounts` 在原位排序后未再使用。

---

# 复评（2026-09-26，head `1861f76`）

复评对象：同分支新 head `1861f76`（base 仍为 `d8bde8d`）。方法同首轮：独立 worktree（`/tmp/pr7-rereview`）与独立 venv 复现，破坏性操作只在 `/tmp` 临时仓库，结论只认本人命令输出与 CI 运行（run 36162444057，`build` 与 `harness` 均 SUCCESS）。

## 复评结论：可合并

六条发现中五条已修复且机器证据完整；PR7-R3 为符合其声明定位的部分修复。修复引入的新绕过面有两条（PR7-R7 严重、PR7-R9 一般）与一条已知边界表述缺口（PR7-R8 一般），其中 **PR7-R7 是原 PR7-R2 的换位绕过，必须在 E1（L4 自动合并）接通前修复**；它与 R8/R9 都是小改动，若实现方还能在合并前再推一版，建议一并处理，否则登记进方案 §13 作为 E1 前置条件后即可合并。本轮不再有「合并前必须先修」的阻断项：R7 在当前形态下（L4 未接通、R1 合并仍经人工）有人审兜底，与首轮 R1/R2 所处的「判定器承诺本身被击穿」不同——base_tests 的机器防线在，只是又有了需人审补位的盲区。

## 首轮六条发现的复核

机器证据（全部本人复现）：`bin/verify --full` 在本机 macOS 10 项全过 @ `1861f76`（回放扩到 26 个注入用例）；`harness/evidence.py --base origin/main` 九条 `Defect:`（H0925-1/2/5、PR7-R1..R6）全部「✗ 失败 → ✓ 通过」；`harness/replay.py --only PR7-R1` 至 R6 逐条注入全部「拦住」（R6 两条用例）；`harness/base_tests.py --base origin/main` 通过；CI harness job 中 `Existing tests (base version) on head` 步骤存在且 success。

| 编号 | 状态 | 证据与复核 |
|---|---|---|
| PR7-R1 | 已修复 | `analyse` 对 `doc_only` 且 `code_files` 非空记 problem（evidence.py `analyse`）；回放「doc 类修复改了代码也不查」注入后 `FAILED (failures=1)`；单测负例 `test_doc_defect_that_changes_code_is_rejected`。交接书问的「代码改动与 Defect 提交分离」不构成绕过：不带 `Defect:` 的提交本来就不进证据表，其行为变化由 risk（产品代码 R2）与 base_tests 兜底（对照实验 C：纯行为变化被 base_tests 拦住，exit 1） |
| PR7-R2 | 已修复，但修复引入新绕过面（PR7-R7） | `harness/base_tests.py` 落地并由 CI harness job 执行（workflow 有 `Existing tests (base version) on head` 步骤，运行 success）；回放注入「追加的 monkeypatch 生效」后 `FAILED (failures=2)`；单测覆盖追加 monkeypatch、新测试文件篡改、有意改动只报告、干净改动通过四场景。交接书问的三条：仓库根 `sitecustomize.py` **不会**被 `python -m unittest` 加载（cwd 注入晚于 site 初始化，实验 B 中断言真实执行、base_tests 报失败）；`load_tests` 协议随 base 版本测试文件还原，head 无法注入；追加测试**仍判 R0** 但 risk 加了说明性 note，安全性改由 base_tests 机器兜底——而这条兜底被 PR7-R7 打穿 |
| PR7-R3 | 部分修复，与声明的「防误操作」定位一致 | 工作区篡改 `rules.toml` 不再解除保护：临时仓库对照实验，非快进 `branch -f main` 在工作区规则被改后仍被拒（钩子读 origin/main 版本）。残留缺口见 PR7-R8（伪造 origin/main 引用可再次解除）。定位调整（git 层「防误操作，不防有意绕过」）、ruleset 提前到本机安装之前（A4 先于 A3）均已写入 specs 与方案 §13 ✓ |
| PR7-R4 | 已修复 | `before == "error"` 从 warning 改为 problem（evidence.py `verify_fail_before_fix`）；回放注入后 `FAILED (failures=1)`；单测 `test_error_before_fix_is_not_proof`。交接书问的「断言失败却不检查缺陷」仍可构造（断言修复引入的标记而非缺陷行为），但这属测试质量的机器上限，由 review-checklist X4（「测试是否真的调用产品代码」）人审兜底，不算修复缺陷。「首次运行拦下实现方自己三条证据」的改法（39a6101）核对为合理且更强：R1/R4 的测试从 `problems[0]` 改为对全部 problems 断言（空列表从 IndexError 变 assertIn 失败）；R2 的测试从 import 模块改为经 CI 实际调用的命令行（`base_tests.py --base … --repo …`，断言退出码与输出），并新增一条断言 CI workflow 里确实接线了这道检查——不是把测试改弱 |
| PR7-R5 | 已修复 | `_reverse_apply_fix` 按 `-U0` 零上下文反向应用该编号提交自身的 diff（多提交按 LIFO 逆序，`--unidiff-zero` 冲突即失败）；冲突时退整文件并加 warning，且退整文件后若测试 error 会被 PR7-R4 新规则判 problem，形成联动。双提交场景（PR7-R2 的 `0f60e67`+`39a6101`）证据表正确产出「失败 → 通过」，归因于修复本身。warning 在证据表 notes 中对评审方可见 |
| PR7-R6 | 部分修复，curl 侧有顺序盲区（PR7-R9） | `gh api` 侧覆盖良好：`-X/--method PATCH|POST|PUT|DELETE`、`-f/-F/--field/--raw-field/--input`（隐含 POST）、前缀 env 赋值均拦；GET 放行。拼接变量后半截（`_ALLOW_TAG`）、带引号 tag、`ori"gin" main` 拆写经去引号复查均拦（实验 19 条正负例）。curl 只拦「url 在前、动词在后」一种顺序，见 PR7-R9。交接书问的变量间接展开、eval、`$(printf …)`、base64、wget、python requests：**同意不在此轮处理**——Agent 层定位是锦上添花，恶意执行者的完整解法是服务端 ruleset，字符串规则继续堆叠边际收益递减，E3（shlex 结构化解析）是正道且已标注「评审建议优先」 |

## 新发现

| 编号 | 严重度 | 问题 | 证据 | 修复要求 |
|---|---|---|---|---|
| PR7-R7 | 严重 | base_tests 的信任根包含 head 版本的产品代码：base 版本的测试文件被还原了，但测试 import 的是 head 的产品模块，产品模块在 import 时即可污染 unittest。与原 PR7-R2 同级的换位绕过（把禁用代码从 tests/ 挪到被 import 的产品代码顶部） | 复现（`/tmp` 临时仓库，按 CI 同样方式调 `base_tests.py --base … --repo …`）：在 `scripts/mod.py` 顶部加三行（`unittest.TestCase.assertEqual = lambda self, *a, **k: None`）+ 行为变化（value 1→2）+ 提交带 `Risk: R1` → base_tests 报「✓ base 版本的已有测试在 head 代码上全部通过（OK）」、`risk.classify` 判 **R1**、flags 为空——R1 自动合并档全绿。对照：同场景去掉钩子则 base_tests exit 1 正确拦截 | E1 接通前必修（可与本 PR 合并前再推一版）。方向（至少一项 + 回放用例）：(a) base_tests 在运行后校验 `unittest.TestCase` 关键方法未被替换（运行前后快照比对，能抓大多数「改了未改回」）；(b) 源码检查：产品代码（scripts/、native/ 等非 tests 路径）新增行出现对 `unittest`/`TestCase`/`pytest` 属性赋值的模式即 flag（hygiene 或架构测试）；(c) 方案 §13 登记为 E1 前置条件 |
| PR7-R8 | 一般 | `trusted_rules` 读 `origin/main:harness/rules.toml`，但本地 `refs/remotes/origin/main` 引用本身不受任何保护：`git update-ref refs/remotes/origin/main <伪造提交>`（reference-transaction 只拦 refs/heads/main 与 refs/tags/）或把 remote 指向伪造源后 fetch，即可让守卫读到伪造规则。specs §7 已知边界的现表述「规则文件已改为读 origin/main」会让读者以为工作区篡改已被堵住，实际只在不改引用时成立 | 复现（`/tmp` 临时仓库）：构造 `protected_branches = []` 的 rules.toml 提交 → `git update-ref refs/remotes/origin/main <该提交>` 无拦截 → 此后非快进 `branch -f main` **放行**（main 被改写为孤儿提交），并打印「按 origin/main 的规则执行」的告警——告警读的正是伪造规则。另注：本 PR 合并前 origin/main 上没有 harness/rules.toml，`git show` 失败退回工作区版本（自举过渡），合并后首次 fetch 才切换到受保护版本 | 在「防误操作」定位下可接受，但 specs §7 已知边界须补两句：①本地 origin/main 引用可改写/可被伪造 remote 污染，有意绕过者可再次解除本机层，唯一硬边界仍是服务端 ruleset；②PR 合并前该机制退回工作区版本。不改代码也可，文档诚实优先 |
| PR7-R9 | 一般 | curl 写请求的拦截存在参数顺序盲区：`curl -X PATCH https://api.github.com/…`（动词在前）、`curl --request PATCH …`（长选项）、`curl -d @body https://api.github.com/…`（-d 在前）、`-XPOST` 连写均放行；只有「url 在前、动词在后」被拦。另 `gh pr merge` 未拦（R2 以上「由用户合并」目前仅靠约定，Agent 层本可拦） | 复现（`check_command` 直接调用，19 条用例）：`curl -X PATCH https://api.github.com/x`、`curl --request PATCH https://api.github.com/x`、`curl -d @body https://api.github.com/x`、`curl -XPOST https://api.github.com/x`、`wget --post-data=@f …`、`python3 -c "requests.patch(…)"` 均放行；`curl https://api.github.com/x -X PATCH` 拦截 | curl 规则去掉顺序依赖（对 `curl` 与 `api.github.com` 共现 + 任一写动词/数据选项即拦，含 `--request`、`-X` 连写）；补上述变体的负例测试与回放用例。wget/requests/间接展开归 E3，不在本轮。`gh pr merge` 是否由 Agent 层一律拒绝，交用户决定（影响 L4 下 R0/R1 的合法合并路径） |

## 复评补充

- 两点方案风险（分工未运转 E11、标杆有效待 trial-001）按交接书要求不重复上报。
- 本轮评审过程中命令守卫未误报（实验命令换行拆写后均正常执行）；上一轮记录的字符串匹配误报面无恶化。
- 复评环境：worktree `/tmp/pr7-rereview`、venv `/tmp/pr7-venv`（ruff 0.16.8），实验脚本 `/tmp/pr7-attacks/rereview_attacks.py` 及临时仓库（`/tmp/pr7-rere-r3-*`、`/tmp/rere-r2-*`），评审后已清理，未触及真实仓库与远端。

