# 待办清单

更新：2026-09-29。本仓库未关闭事项的唯一清单。编号沿用迁出前 Agent-Notification 待办的 `B<序号>`（2026-09-29 从那边迁入，原行在那边标为「转到 delivery-harness」），新事项从 B51 顺延，不复用。

## 使用规则

- **开工前先看这里**：接手工作（含中断后恢复）时先读本清单与 `docs/plans/2026-09-29-roadmap.md`。
- **新增**：发现暂不处理的问题、用户提出但未排期的事、评审遗留的残余风险，都记成一行，写清触发条件、负责方与来源。
- **开始做**：PR 描述写上待办编号；进行中的行写上分支名。
- **关闭**：在完成它的同一个 PR 里，把这一行移到文末「已关闭」，写上日期和 PR 号。放弃同样移过去，写明原因。
- **负责方为「用户」的事项**：Agent 不代做，只提醒并准备好操作步骤。

## 1. 按顺序推进

用户 2026-09-29 定的顺序：先让第三方能走通（B48，已完成；B53 是 T001 冒烟暴露的后续阻塞，已完成），再做可观测性（B46，设计先交用户审），再做接入无感化（B45），之后装进第二个项目（Python + TypeScript）。

| 编号 | 事项 | 估计成本 | 负责 | 来源 |
|---|---|---|---|---|
| B46 | 可观测性（v0.2）：统一事件格式与贯穿全程的追踪 ID、各环节埋点、`bin/harness trace`、`audit`、即时告警；收掉 B36、B38、B40。设计已审定，P1 实现全部合并（T101–T109、T110、T106、T111、T112，并行度经 #24 修订）；T107 文档收尾 PR #33 待合并；随后 P1 期门禁 G1（自举升级）/G2（消费方等价，含契约测试修复原子合并）；P2–P6 按追溯表顺序。设计与合同见 `docs/plans/2026-09-29-observability-*.md` | 大 | 评审方 | 用户 2026-09-29 |
| B45 | 第二个项目接入前：`adopt`（探测语言、源码目录、测试与 lint 命令，渐进档位，已有配置幂等合并，AGENTS.md 受管块）与 TypeScript 语言插件 | 大 | 评审方 | 用户 2026-09-29（面向开源、接入无感） |
| B42 | harness 测试（Agent-Notification 的 `tests/test_harness*.py`）移植到本仓库：改为基于夹具项目，本仓库单独即可验证；那边只留消费方契约测试 | 中 | 评审方 | Agent-Notification T008；任务书 T118（task-118-harness-contract-port.md）|
| B44 | 本仓库 CI 加消费方契约测试：检出 Agent-Notification main，用待测引擎 upgrade 后跑其 harness 测试与 `verify --quick` | 小 | 评审方 | Agent-Notification T008；任务书 T115（task-115-consumer-contract-ci.md）|
| B43 | 删除迁移过渡回退：git 守卫与派发读旧布局 `harness/`、评审材料的旧入口（`LEGACY_RULES_REL` 等）；Agent-Notification 已迁，随 v0.1.1 | 小 | 评审方 | Agent-Notification T008 |

## 2. 可以做，暂未排期

| 编号 | 事项 | 估计成本 | 负责 | 来源 |
|---|---|---|---|---|
| B50 | 第三方走通遗留的项目专属内容：`base-tests` 只支持 unittest 与固定 `tests/` 目录（需可配置测试命令，随 B45 的 adopt）；守卫拒绝文案与周报里仍有原项目的事故编号（v0.8.0）、设计章节号和 `docs/review/weekly/` 路径（随 B47 国际化清理）；B48 的工作流未在真实 GitHub 仓库上跑过，只做了本地等价验证，首个真实仓库（第二个项目）接入时核对 | 中 | 评审方 | B48 |
| B51 | 升级时自动检测配置缺口（对照新版引擎列出缺失的必填或建议配置，替代只靠 CHANGELOG 的 Migration 条目）；与 B45 的 adopt 一起做。已完成部分：拒绝非 main 提交、打印 Migration 条目、`docs/upgrading.md`（PR #3） | 小 | 评审方 | B48 后 Agent-Notification 升级流程讨论 |
| B52 | 审计账本覆盖「被拦下」（关闭未合并）的 PR：v0.2 只做已合并（用户 2026-09-29 定），被拦下的 PR 目前只在本机库里 | 中 | 评审方 | 可观测性设计 4.1 |
| B54 | 任务拆分评审工具化：`bin/dispatch review-plan`（复用独立评审的材料包与提示词，材料为设计、追溯表与全部任务书），现按 `docs/task-splitting.md` 手工执行 | 中 | 评审方 | 用户 2026-09-29；任务书 T120（task-120-review-plan.md）|

| B59 | 独立评审的测试强度核查：T101 的 OpenCode 评审通过，但没发现设计方变异检查抓出的两个未被抓住的变异。本仓库没有评审校准集；建立校准集（含「测试恒真、变异未被抓住」类样本）并按其结果选评审方与提示词 | 中 | 评审方 | T101 复核；任务书 T119（task-119-review-calibration.md）|
| B64 | 引擎任务书校验的 DESIGNERS 枚举扩展（现仅 claude-code/codex）：支持任意设计方标识（如 zcode），免得新设计方沿用旧席位署名；现以「Zcode 代行 codex 席位」过渡（T110 头部 designer: codex）。触发：设计方席位再变更或新增执行方宿主时 | 小 | 评审方 | PR #23 第二轮评审，2026-09-29 |
| B62 | 凭据规则缺少词边界：普通长task文件名的尾部被误识别为密钥；T602文档迁移任务名在暂存后扫描触发9处误报。改进凭据模式边界并补真实凭据/普通标识符正反例；本次只缩短新任务书slug，不改护栏规则 | 小 | 评审方 | B46提交前卫生检查，2026-09-29 |
| B68 | 独立评审材料包在任务书探测未命中时写「无」（如 docs-only 的 T107 PR 实按 task-107 执行）：增加从 PR 标题/正文提取 `docs/plans/task-*.md` 的回退；既有探测路径逐字不变。任务书 T114（task-114-review-pack-taskbook.md） | 小 | 评审方 | PR #33 评审（2026-09-30） |
| B70 | `dispatch --resume` 在该分支已有 PR 时不复用而新建，`gh pr create` 报错崩溃（T203/T119 两例）：resume 应查询既有 PR 并复用（反馈注入既有 PR） | 小 | 评审方 | T203/T119 resume 实测（2026-09-30）；任务书 T121（task-121-resume-reuse-pr.md）|
| B71 | `wait_ci` 查询 Actions workflows 端点的瞬时失败（TLS/EOF）无重试（`gh run list` 有 3 次重试但 workflows 列表拉取不在覆盖内），T118/T204 两例派发进程死在等 CI 阶段（产物已推送、PR 已开，仅失去自动重跑）：把 workflows 拉取纳入同一重试 | 小 | 评审方 | T118/T204 实测（2026-09-30）；任务书 T122（task-122-wait-ci-retry.md）|
| B72 | 派发结束/失败后槽位工作树残留并占住分支，后续 resume/同名任务 checkout 被拒（T203 resume 首撞）：dispatch 结束路径应归还槽位（detach 或清理）；期间需人工 `git worktree remove --force` | 小 | 评审方 | T203/T119 resume 实测（2026-09-30） |
| B74 | `write_materials` 的评审 diff 200K 截断：大 diff PR（如 T301 的 1.8 万行夹具）在评审材料中丢失全部代码改动，评审只能按描述验收（T301 首轮评审严重项）。修法：超限时 diff 落盘为附件文件并在 pack 中引用，或分段；评审工作区按任务隔离（同 B59 发现） | 中 | 评审方 | T301 首轮评审（2026-09-30） |
| B75 | 校准样本清单 `docs/review/calibration/samples.json` 的 #51 样本 head `48ad84b` 已被第二轮推进、检出失败（errors 1）：清单条目应指向该 PR 的稳定语义状态或标注 head 可更新规则 | 小 | 评审方 | T119 校准基线运行（2026-09-30） |
| B73 | 间歇性 macOS flake：`test_pr_title_has_no_duplicate_prefix`、`test_record_contains_timeline_and_exact_anchor` 等整 dispatch 夹具测试在 macOS runner 偶发 dispatch 返回 1（main 近 14 次 ci 三红；本地与 CI=true 无法复现）。诊断断言已入 #59；下次 flake 依日志修根因，必要时给整 dispatch 夹具加确定性时钟/事件同步 | 中 | 评审方 | main ci #37/#38/#58、PR #61 实测 |
| B69 | `run_timeline._read_events` 用裸 `sqlite3.connect` 未沿用 `events_db._connect` 的 `busy_timeout`：库锁竞争窗口内读取异常后退化为空时间线（方向安全、偶发）。改用既有连接参数并补回归 | 小 | 评审方 | PR #43 评审（2026-09-30）；任务书 T116（task-116-read-events-busy-timeout.md）|
| B67 | `routing` 的 r1 检查在仓库尚无 main 提交（空仓库首个 PR）时的既有缺陷：#35（T106）独立评审发现，等价夹具覆盖到该分支；修法与回归随 T112 后的小修轮或并入 B45 adopt | 小 | 评审方 | PR #35 独立评审（2026-09-30）；任务书 T113（task-113-r1-missing-base.md）|
| B47 | 开源化其余项：界面与提示词国际化（en、zh-CN）、可配置的目录约定与默认分支、执行方宿主适配器（Pi 之外）、`platform` 一键平台设置与 App manifest | 中 | 评审方 | 用户 2026-09-29 |
| B49 | 自举：本仓库装上自己的 Agent 层与 git 层守卫（目前只有服务端 ruleset 兜底） | 中（进行中：分支 chore/self-host） | 评审方 | 2026-09-29 迁移准备 |
| B4 | 按改动行做变异测试（先只支持 Python），自动发现「测试写了但没测到东西」 | 出现一次这类逃逸再做；约 1 天，每个 PR 的 CI 多 1–4 分钟 | 评审方 | Agent-Notification 修复证据方案讨论（2026-09-26） |
| B33 | Swift 质量棘轮解析的两处已知限制：协议中的计算属性声明（`var x: T { get }`）被计为函数；字符串插值内嵌字符串（`"\("x")"`）会让解析提前结束字符串、漏算同行大括号 | 小 | 评审方 | Agent-Notification PR #62 独立评审；任务书 T117（task-117-swift-parser-limits.md）|
| B34 | `docs` 检查补两项：AGENTS.md 超过 100 行即失败；待办清单中写「进行中」的条目必须写分支名 | 小 | 评审方 | Agent-Notification 2026-09-29 现状复核 |
| B35 | 派发 Pi 时改用 `--no-extensions` 再显式加载守卫：`-na` 只忽略槽位中的项目文件，用户级扩展仍会加载 | 小 | 评审方 | 同上 |
| B36 | 派发提示词要求执行方结束时报告「缺失的上下文或工具」，写进运行记录 `missing_context`，周报汇总（随 B46） | 小 | 评审方 | 同上 |
| B37 | 独立评审结论为「不通过」时，派发的任务 PR 由执行方带评审发现修复一轮，仍不通过转用户 | 中 | 评审方 | 目标态设计 13.2 |
| B38 | 运行记录内容检查：拒绝含命令全文、本机路径、会话正文的记录（随 B46） | 小 | 评审方 | 目标态设计 10.5 |
| B40 | 本机完整事件流（git 公共目录下 `dispatch/runs/`）保留 30 天后清理（随 B46） | 小 | 评审方 | 目标态设计 10.1 |
| B41 | 用 Agent-Notification App 采集的执行方用量核对运行记录的 token 与费用（可选） | 中 | 评审方 | 目标态设计 10.1 |

B34–B41 的处理建议（B35 与 B36 先做、B41 放弃等）用户 2026-09-29 答复「先不动」，保持现状。

## 3. 暂缓

| 编号 | 事项 | 状态与条件 | 负责 |
|---|---|---|---|
| B31 | 执行方凭据隔离：方案须同时支持 Windows、Linux、macOS，全盘评估后再定（隔离手段、钩子与脚本的跨平台运行、守卫的命令解析、槽位与凭据位置、各宿主的钩子信任） | 暂缓；进入无人值守前必须完成评估 | 评审方评估，用户决定 |

## 已关闭

| 编号 | 事项 | 关闭 |
|---|---|---|
| B57 | `dispatch.pr_body` 固定写「需要人工验收的部分：见任务书验收表中的人工条目」，任务书没有人工条目时落空；只在有人工条目时才写，否则写「无」。T101 独立评审指出；随 T111 收掉 | 小 | 评审方 | T101… | 2026-09-30，delivery-harness #37（T111） |
| B60 | 命令守卫误报：`command_guard` 会扫描 heredoc 正文，把正文里出现的 `HARNESS_*=…` 字样当成「设置覆盖变量」拒绝（同样的文字放在 `echo` 引号里则放行，说明引号内已按数据处理，heredoc … | 2026-09-30，delivery-harness #36（T112） |
| B61 | 任务书步骤文件解析漏掉根目录多点文件名（如 README.zh-CN.md）：`taskbook.step_files` 的 PATH_TOKEN 只识别单点根文件名，导致这类步骤路径不参与类别/风险交叉核对。补解析与回归，不能只靠合… | 2026-09-30，delivery-harness #36（T112） |
| B63 | T109 独立评审遗留（一般级两条，处置：PR #20 先合并再跟进）：① `engine/core/events.py` `emit` 的 step 合法性检查基于 strip 后的 `step_text`，写入 payload 却… | 2026-09-30，① T110（#29）、② T107（#33） |
| B65 | 第一轮派发评审遗留（均一般级，处置：随小型修复任务收掉，最晚 T107 前）：① `engine/cli.py` `_record_dispatch` 的延迟导入与 emit 未包异常保护——引擎副本损坏时 ImportError 会… | 2026-09-30，delivery-harness #37（T111） |
| B66 | T105 验收追认与体量治理：`tests/test_events_agents.py` 超任务书 800 行约束——用户合并 PR #31 即为追认；后续瘦身随 T201/T205 的外置文件模式（执行计划既有安排），不另立任务 |… | 2026-09-30，追认随 #31 合并；瘦身随 T201/T205 外置模式 |
| B58 | 第三方没有 `docs/templates/review-checklist.md`：评审提示词与材料包引用它，每单评审重复提出。已随引擎缺口 docs PR 提供建议清单（含「每条验收说明未实现时会怎么失败」列） | 2026-09-30，本 PR |
| — | 引擎抽离与 Agent-Notification 迁移（阶段 A、B） | 2026-09-29，delivery-harness #1、Agent-Notification #66 |
| B48 | 第三方可走通：通用 CI 工作流、两套 ruleset、`[platform]` 批准方式与 App 变量名可配置、单账号模式与风险说明；README「Platform setup」 | 2026-09-29，delivery-harness #3 |
| B53 | CI 工作流名可配置（`[dispatch] ci_workflows`），dispatch 等待全部必需工作流；T001 冒烟暴露 | 2026-09-29，delivery-harness #11 |
| B55 | `dispatch` 等 CI 时对 `gh` 瞬时失败有限次重试（连续 3 次才抛出）；T001 后验证 `wait_ci` 时暴露，本机网络多次瞬断 | 2026-09-29，delivery-harness #15 |
| B56 | `bin/dispatch review` 导入旧的平铺模块名 `review`，自 v0.1 抽离起崩溃（`bin/harness review pr` 不受影响）；T101 评审时暴露 | 2026-09-29，delivery-harness #15 |
