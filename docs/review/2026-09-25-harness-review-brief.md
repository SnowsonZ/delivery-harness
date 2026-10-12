# 评审交接：可验证交付 harness（PR #7）

> 状态：迁自 Agent-Notification（2026-10-09 快照，正文未改；业务库不再留存 harness 抽取与设计文档，只留引用）。文中指向业务库专属文件的链接已改为该仓库的绝对地址。
用途：交给独立评审方（另一个 Agent），对「可验证交付」方案与 PR #7 的全部改动做一次整体评审。写于 2026-09-25，评审基线为分支 `claude/gracious-bardeen-pi7ysv` 的当前 head（以 `git rev-parse HEAD` 为准）。

## 0. 原始需求与对齐结论（评审的判断基准）

评审首先要回答：这些改动是否在实现用户的原始需求，而不只是「实现得对不对」。以下内容按原文引用，不要改写。

**理论来源**：用户的调研报告《低人工干预下 AI Agent 持续高质量交付：理论与全链路最佳实践》（2026-09-23，Claude 文档，未入库；其中核心结论是「关键不在模型多聪明，而在能否把任务变成可验证的问题，并用 harness 把验证、隔离、回滚和学习做成闭环」）。

**用户原始需求（2026-09-25，原话）**：

> 按照报告给的这套理论。以我们这个项目为核，构建起报告中提到到，全流程中需要做到的将任务转为为可验证的问题。把该项目打造为我们目标的标杆项目。

**对齐时用户的决定（2026-09-25）**：

1. 自治目标 L4：R0/R1 门禁全绿自动合并，其余每个 PR 人审证据包。
2. 角色：Codex / Claude Code 做设计、评审，并作为顾问协助执行者解决问题；Zcode / OpenCode / Pi 做实现、测试、部署上线等实际工作。
3. 同意按提供的配置在仓库设置中启用 ruleset（`main` 与 `v*` tag 保护）。
4. P1–P6 连续完成后合并为一个 PR 提交。

**对齐后的目标与验收**（详见 `docs/plans/verifiable-delivery.md` §1、§2、§7、§8）：

- 目标：把每一类改动的完成判断，从「Agent 说做完了、人去复验」改为「开工前就有可执行的判定，完工后由 Agent 之外的机器重跑判定并留证据」，并用 v0.8.0 的真实事故证明有效。标杆 = 有效（有数据证明）+ 可复制（模板、脚本、文档可被其他项目照搬）。
- 「可验证」的五条操作定义：验收有编号且是命令或测试；判定在 Agent 之外；判定器本身被检验；执行者改不了判定器；证据由脚本生成。
- 标杆验收四条：事故回放、验收编号覆盖、三处同口径、真实任务试跑并与 v0.8.0 基线对比。
- 不做：全自动（UI 与 R2 以上保留人审）；改变产品行为；大团队设施（Temporal、OpenTelemetry、合并队列、金丝雀）；打 tag 或发版。

## 1. 评审范围

- **方案**：目标、「可验证」的定义、角色分工、风险等级、护栏分层是否合理，是否真的解决 v0.8.0 暴露的问题。
- **实现**：`harness/`、`.githooks/`、`.github/`（workflow、rulesets、PR 模板）、`.claude/`、`.opencode/`、`bin/verify`。
- **产品代码的两处改动**：`scripts/daily_report.py`（抽出 `cost_thresholds`）、`native/WidgetSnapshotWriter.swift` 与 `native/InboxPolicy.swift`（写入器改调策略函数）。两处都声称行为不变。
- **规格与测试**：六份 `docs/specs/*.md` 新增的验收编号表（69 条）；新增测试 `tests/test_harness*.py`、`test_properties.py`、`test_architecture.py`、`test_golden.py`，以及对已有测试文件的追加。

不在范围内：产品功能本身的正确性（v0.8.0 已评审过），除非本 PR 改动了它。

## 2. 建议阅读顺序

1. `docs/plans/verifiable-delivery.md`：目标、定义、角色、风险等级、阶段状态、待决事项（§11）、后续工作（§13）。
2. `docs/review/2026-09-25-harness-baseline.md`：v0.8.0 失败分类（§1）、基线指标（§2）、实施中的新发现（§5）、测试强度基线（§6）。
3. `docs/specs/delivery-harness.md`：现役命令、约定、三层护栏、一次性设置、验证状态（§6）与已知边界。
4. 代码：先读 `harness/common.py`、`verify.py`、`evidence.py`、`risk.py`，再读 `git_guard.py`、`command_guard.py`、`replay.py` 与 `replay_cases.py`，最后读 `acceptance.py`、`mutate.py`、`quality.py`、`metrics.py`、`review_pack.py`、`release_check.py`。
5. PR 的 harness job summary（风险等级、修复证据、交付度量）。

## 3. 复现与核对

```sh
git fetch origin && git checkout claude/gracious-bardeen-pi7ysv
python3 -m pip install -r requirements-dev.txt     # 有开发包 venv 时用 scratch/iterm-probe-venv/bin/python
python3 harness/git_guard.py install               # 评审环境也会受 git 守卫约束
bin/verify --full                                  # 非 macOS 上 Swift 三项会跳过
python3 harness/review_pack.py --base origin/main --output build/review/pack.md
python3 harness/replay.py --list                   # 基线覆盖
python3 harness/mutate.py --check                  # 约 1.5 分钟
python3 harness/acceptance.py --manual             # 人工验收清单
```

注意：本仓库的 Claude Code 项目设置会加载命令守卫。守卫按字符串匹配命令，命令文本里出现危险字样（哪怕只是 echo）也会被拒；这是已知边界，不需要绕过，换一种写法即可。

## 4. 请重点质疑的地方

以下是作者自己最没有把握、或最可能出错的部分：

0. **是否偏离原始需求**（第 0 节）：改动是否真的把「任务转为可验证的问题」做成了全流程，还是只堆了检查工具；两个角色（设计评审方、执行方）的分工是否被落实；对齐决定有没有被违背或悄悄扩大；哪些部分离「可复制的标杆」还差得远。
1. **证据是否可能「假通过」**：`evidence.py` 把修复提交改过的代码退回修复前，再跑引用该编号的测试。退回时如果别的提交也改了同一文件，测试可能因为无关原因失败，被误判为「修复前失败」。请评估这个误判面，以及「出错」与「断言失败」的区分是否足够。
2. **风险判定能否被绕过**：`risk.py` 按路径判定。执行方有没有办法让 R2/R3 的改动被判成 R0/R1？例如改名、把逻辑挪进 `docs/` 或 `tests/`、利用 `shrink_only` 规则。
3. **护栏的漏洞**：`reference-transaction` 的判断（旧值为全 0 时查当前值）、pre-push 对强推的识别、命令守卫的正则。请尝试构造能漏过的操作，只在临时仓库里试。
4. **回放与变异的有效性**：注入点是否代表真实的缺陷形态；性质测试有没有写得过松（已发现并修过一处：条目缺失时被跳过）；变异存活项里有没有被当成「等价变异」放过的真实缺口。
5. **两处产品改动是否真的行为不变**：尤其是写入器的 `runningRows` 从 `human.filter` 改为 `rows.filter` 加 `widgetRunningListed`（后者自带排除 agent）。
6. **规格编号是否忠于原文**：DR、IN、CB、ZN 四张表是「由既有条款整理、不新增需求」。请抽查有没有夹带新要求，以及覆盖列引用的测试是否真的在断言那条验收。
7. **方案层面**：单人项目承担这套流程的成本是否合理；哪些部分过度设计、可以砍；哪些 v0.8.0 失败类型其实没被结构性解决。

## 5. 已知问题（不必作为新发现上报）

- H0925-4（合并口径不一致，以 `expectedFailure` 登记）、H0925-6（Zcode 两份规格矛盾）：待用户决定。
- 执行方与用户共用 GitHub 身份，服务端分不清谁在合并，L4 自动合并尚未接上。
- ruleset 与发版审批尚未导入；OpenCode 插件、Codex 钩子未在真实环境验证；Pi、Zcode 无 Agent 层拦截。
- `replay_cases.py` 的 `DEFERRED` 中 9 项暂缓回放；DR14 登记为验收缺口（首个试跑任务）。
- 命令守卫按字符串匹配会误报（已有 5 次记录，见规范「已知边界」）。

如果评审认为上述某项的严重度被低估，可以写成发现并说明理由。

## 6. 产出

- 结论：可合并 / 修改后可合并 / 不可合并。
- 发现：写入 `docs/review/2026-09-25-harness-review.md`（日期按实际评审日），编号 `PR7-R<序号>`，严重度分阻断、严重、一般。每条附证据（命令、文件行、输出）与修复要求，包括应补的测试或回放用例。格式见 `docs/templates/review-checklist.md` 末尾。
- 对方案的整体意见（第 4 节第 7 项）单列一节。

## 7. 约束

- 只读评审：不修改实现、不推送到 PR 分支。需要改动的写成发现，由实现方修复后复审。
- 不在真实仓库里尝试破坏性操作（强推、改写历史、推 tag、删除 CI 记录）；验证护栏只在临时仓库中进行。
- 通过与否只认可复现的命令输出与 CI 运行，不采信文档中的自述，包括本交接书。

## 8. 复评（2026-09-26 起）

首轮评审报告：`docs/review/2026-09-26-harness-review.md`，结论「修改后可合并」，六条发现 PR7-R1..R6。实现方已逐条修复，处理摘要见[基线评审 §5.1](2026-09-25-harness-baseline.md)。复评只需核对：

1. 每条发现的修复证据：`python3 harness/evidence.py --base origin/main`，PR7-R1..R6 均应为「修复前失败、修复后通过」。
2. 回放：`python3 harness/replay.py --only PR7-R1`（依次换编号），注入后对应测试必须失败。
3. 修复是否真的堵住了报告中的复现步骤（按报告原命令在临时仓库重跑），以及修复本身是否引入新的绕过面。
4. PR7-R3 只修了一部分（规则文件改读 origin/main；守卫代码与解释器仍可写，写为已知边界）：这一处理是否可接受。

复评结论追加到首轮报告末尾，新发现继续按 `PR7-R<序号>` 编号。
