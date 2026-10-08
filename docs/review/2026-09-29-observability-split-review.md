# B46 拆分评审原文与设计方处理

状态：**OpenCode第二轮可提交（待用户审定C8的U1–U5）；第二轮4条非阻断建议已补充**。设计方Codex，独立评审方OpenCode，模型zai-coding-plan/glm-5.3。日期2026-09-29。未提交、未推送、未开PR、未派发实现。

提交前的暂存卫生检查暴露B62：凭据模式会误认T602原稿的长文件名。本包仅将其slug缩短为task-602-docs-migration.md，编号/正文/目录不变；下文评审路径按现名展示，原始报告和输入快照仍保留原名。旧材料sha256仍固定原稿字节，不代表现文件名取得过独立评审。

## 运行与证据

OpenCode以pure/plan只读运行，命令/写文件/patch/子代理/webfetch工具关闭；进程退出0，169条事件，无error事件或失败工具，stderr为空，最终step_finish为stop，完整报告形成。事件起止1093秒（18分13秒），44个模型步骤。这里只记录过程结果，不把工具读取完成当评审通过。

v1输入32份材料先按sha256冻结；原始JSONL/result.md/stderr.log及input-v1快照保留于本机忽略目录build/observability-split-review，材料修订前全部哈希再次一致。下文评审原文未按设计方意见修改；其中T502-2→T501-2是评审方自身笔误，按T501-2理解。原文评审96行，修订后T102增加1行，当前97行。

## 首轮后的设计方处理记录（后续结论见第二轮）

| 发现 | 处理 | 位置与验证/待办 |
|---|---|---|
| F1 严重：派发体量与复杂度棘轮 | 采纳，不上调基线。T105预留dispatch_observation.py，T201预留run_timeline.py；T205条件/详情下沉alerts.py，dispatch.py仅接线 | T105/T201/T205目标终态与白名单已补；质量指标不升仍由实际实现PR的bin/verify --full验收；当前没有代码改动，不宣称已满足未来指标 |
| F2 一般：评审摘要格式漂移 | 采纳。固定harness-review-audit单行JSON v1字段、旧标记兼容、材料字节哈希/recipe与后置评论URL；T105生产、T305按同形状消费 | 共用合同C6、T105验收3、T305验收1；真实集成仍需G3，格式不能由两任务各自选择 |
| F3 一般：同仓库PR也可获写令牌 | 采纳。所有pull_request job/step保持只读；PR成功/失败告警在默认分支workflow_run独立job，harness自身仅push main承载末尾告警 | T403目标/验收3，明确同仓库PR与fork均无write；模板实施后由G1/G3核对平台 |
| F4 一般：活动/终止流判断未定 | 采纳。明确dispatch公共目录、槽位锁字段与原始流路径，存活PID保护整个task的全部attempt；未知/坏锁保守跳过、mtime与锁重检 | T204目标/验收3；不改dispatch状态/锁、不额外要求白名单外完成标记 |
| F5 一般：B38命令/会话边界不清 | 采纳。增加C3字段类型与C01–C07禁例/A01–A05合法例，精确可信guard理由例外，未知自由文本拒绝 | C3、T203验收1/2；明确这是最低可执行边界，不宣称理解任意自然语言；列入U3待用户审定 |
| F6 建议：入口映射漏测 | 采纳。T102新增逐个COMMANDS/C1映射验收，明确install/upgrade/identity/weekly与guard/query例外 | T102新增第5行，点名test_all_registered_entrypoint_mappings_and_guard_exceptions；总验收97行 |
| F7 建议：设计方任务保留派发前置句 | 采纳。T107/T602改为设计方核对任务书已合并/一致，不调用派发 | 两任务前置条件已更新 |
| F8 建议：白名单补核对无证据、文档自证 | 采纳。单独留24份文件列/白名单/类别风险核对清单，记录B61不识别根多点文件名时直接读表格的补核对 | 见结构核对证据；文档人工验收仍须用户审查，不能由脚本代替内容判断 |
| F9 建议：预算诱发减弱断言 | 采纳其保留验收/先升级要求；不授权执行方自行缩范围。任何样本缩减先报告、确认不损失正负覆盖后经原流程修订 | 所有任务交付与升级段已补，不自动延长预算、删除断言或放宽基线 |
| F10 一般：CI子链语义需明确用户决策 | 接受决策点，尚未获得审定。共用合同C8单列U1及U2–U5格式/边界/阈值细化 | 用户确认或改选后再请OpenCode复核；不把原设计批准视为这些细化已经批准 |

首轮处理时没有不采纳发现。F1–F9当时只完成草稿修订、尚未复核；第二轮结果在后文独立保留。F10仍待用户决定，结构检查不改写原v1“需修订”结论。提交/推送/开PR各次授权仍分别等待用户。

## v1评审原文

# B46 整包任务拆分独立评审报告（OpenCode，只读）

评审范围：`docs/task-splitting.md` 流程第 4 步。对照已审定设计（2026-09-29-observability-design.md v0.2）、执行计划、共用合同 C0–C7、追溯表、历史 T101 与全部 24 份新任务书，并核对 engine/templates/配置现状代码。未执行任何命令、未改动任何文件。

## 一、文件覆盖清单（全部新任务书均已审）

| # | 任务书 | 类别/风险 | 负责方 | 白名单核对结果 |
|---|---|---|---|---|
| 1 | task-109-events-hardening.md | K7/R3 arch:true | 派发 | 与涉及文件一致；均为可编辑路径 |
| 2 | task-102-events-verify.md | K7/R3 arch:true | 派发 | 一致 |
| 3 | task-103-events-route.md | K7/R3 arch:true | 派发 | 一致；与 T104 白名单不交叉（可并行成立） |
| 4 | task-104-events-guard.md | K7/R3 arch:true | 派发 | 一致 |
| 5 | task-105-events-agents.md | K7/R3 arch:true | 派发 | 一致 |
| 6 | task-106-events-equivalence.md | K2/R0 arch:false | 派发 | 仅新增测试；K2→R0 与准入一致 |
| 7 | task-107-events-docs.md | K7/R3 arch:true | 设计方 | 一致（见 F7/F8） |
| 8 | task-108-events-checks.md | K7/R3 arch:true | 派发 | 一致；8 模块均存在 |
| 9 | task-201-run-timeline.md | K7/R3 arch:true | 派发 | 一致（见 F1） |
| 10 | task-202-missing-context.md | K7/R3 arch:true | 派发 | 一致；engine/prompts/dispatch_prompt.md 存在且 install 整目录复制 |
| 11 | task-203-run-record-privacy.md | K7/R3 arch:true | 派发 | 一致（见 F5） |
| 12 | task-204-artifact-retention.md | K7/R3 arch:true | 派发 | 一致（见 F4） |
| 13 | task-205-dispatch-alerts.md | K7/R3 arch:true | 派发 | 一致（见 F1） |
| 14 | task-301-events-io.md | K7/R3 arch:true | 派发 | 一致；含 events_db.py（导入需新增按原 hash 插入接口，白名单足够） |
| 15 | task-302-trace-events-cli.md | K7/R3 arch:true | 派发 | 一致 |
| 16 | task-303-ci-events.md | K7/R3 arch:true | 派发 | 仅模板工作流，不含真实 .github/ ✓ |
| 17 | task-304-github-events.md | K7/R3 arch:true | 派发 | 一致 |
| 18 | task-305-audit-ledger.md | K7/R3 arch:true | 派发 | 含 templates/.github/rulesets/harness-audit.json（新增）✓ |
| 19 | task-401-audit-reconstruction.md | K7/R3 arch:true | 派发 | 一致 |
| 20 | task-402-audit-completeness.md | K7/R3 arch:true | 派发 | 一致 |
| 21 | task-403-alert-cli-workflows.md | K7/R3 arch:true | 派发 | 一致（见 F3） |
| 22 | task-501-weekly-events.md | K7/R3 arch:true | 派发 | 一致 |
| 23 | task-601-observability-e2e.md | K2/R0 arch:false | 派发 | 仅新增测试 |
| 24 | task-602-docs-migration.md | K7/R3 arch:true | 设计方 | 一致（docs/upgrading.md、AGENTS.md 均存在） |

统计核对：**24 份（非 23）＝ 22 可派发 + 2 设计方（T107/T602）**，与执行计划、追溯表一致；追溯表正向 D001–D085 共 85 行、反向 24 行、非派发 G1–G5，均已逐行核对。

## 二、总体核对结论（按检查维度）

1. **完整性：通过（附 F6/F8 小缺口）。** 设计 §1–§7 每项数据、环节（2.1 十环节表逐项落到 D 行）、事件、命令、配置、文档、迁移、告警与非目标（含「不做」清单、3.7「记不了」、B31/B52 边界）均有正向归属；反向 24 任务均对应 D 行。非派发 G1–G5 有时机、负责方与证据要求；真实平台全链归 G3（T601 明确「假平台不声称已通过」）。追溯表验收全部逐方法点名 unittest 命令并注明「不接受 0 tests」，未见用宽泛模块命令掩盖断言缺口；唯一非具名测试的是 D080–D085 文档人工核对（文档任务固有属性，见 F8）。
2. **正确性：通过。** 依赖链 T109→T102→(T103∥T104)→T105→T108→T106→T107→T201→…→T602 无环，任务书前置条件与追溯表依赖列逐条一致；同文件（cli.py、dispatch.py、events_db.py、run_check.py、harness.yml 模板、alerts.py、audit.py）均按追溯表串行。类别/风险/architecture 与准入检查一致（K7→R3、K2→R0；除 T106/T601 外 architecture:true，模块交叉核对 engine/templates/tests ≥2 模块成立；PATH_TOKEN 对 `templates/.harness/config/checks.toml`、`templates/.github/rulesets/harness-audit.json` 等带点目录段可匹配，唯 `README.zh-CN.md` 受 B61 已知限制）。白名单不含真实 `.github/`、`.harness/`、Agent 配置、5 个已有测试、implementer_protected（requirements-dev.txt、ruff.toml）；`templates/.github/**` 不命中 `.github/**` 保护模式。模板目录与安装副本（.harness/config、.github/workflows 4 个真实工作流）分别核对，任务书只动模板、真实副本归 G1 同步。T107/T602 设计方不派发、头部与执行计划声明一致。
3. **可验证：通过（附 F2/F5 缺口）。** 96 行验收均有具名测试与「未实现时怎样失败」列，逐行反事实核对见第四节；定向变异（prev_hash 独立摘要、seq 有效哈希缺口、尾删由固定锚点发现）、三态等价 + stderr 归一化有界负例、mock 伪证明禁令（C0）、原始数据不外发扫描（T301/T303/T305/T601）均有归属。
4. **可完成：存在 F1 严重风险，其余通过。** 公共接口签名（C5 events_io、C4 alerts、C6 ledger、C7 inspect_pr、C3 记录形状）定义方与消费方一致；安装布局入口（ci_events 直跑、新 CLI 注册经 bin/harness）可用。
5. **一致性：通过（附 F10 决策点提示）。** source 隔离（C2）、输出引用视图（C1）、artifact 安全 JSON（C5）、audit 配置/阈值/legacy 规则（C7）均为待用户审定的细化，材料在执行计划 §3.2、合同状态栏、T109 正文三处如实标注「不称已审定」，未发现冒称已审定内容；但其语义影响见 F10。

## 三、发现表

| 编号 | 严重度 | 位置 | 理由与具体失败方式 | 修复要求 |
|---|---|---|---|---|
| F1 | **严重** | T105/T201/T205 白名单 × `.harness/config/checks.toml`（python_glob=`**/*.py`）× quality 棘轮 | `engine/agents/dispatch.py` 现状 656 行。T105（10 个步骤埋点 + guard_allowed/parse_observability 接线，估 +100–150 行）、T201（stages/anchors 摘要组装，估 +50–80 行）、T205（预警触发接线，估 +30 行）累计后大概率超 800 行使 `files_over_800` 由 0→1，且 `_loop`（已约 10 个分支）加预警判断可能超 C901 阈值 10 使 `complex_functions` 上升——任一上升 `bin/verify --full` 的 quality 即失败。而 `quality-baseline.json` 不在任何白名单、三份任务书均明令「不修改……质量基线来换通过」，T201 白名单更是只有 dispatch.py+测试（无处置新逻辑的文件），执行方在白名单内无法自救，只能中断升级，需改任务书或走 R3 基线 PR（用户审） | 在 T105/T201/T205 任务书明示体量约束：可外置逻辑（时间线摘要组装、告警触发条件）放独立模块并相应扩白名单（如 T201 允许新增 `engine/agents/run_timeline.py`、T205 逻辑下沉 alerts.py），或由设计方在整包中预先声明基线配套调整方案（R3、随 G 门禁由用户批准） |
| F2 | 一般 | T105 目标终态 ↔ C6 ↔ T305 验收行 1 | T105 要求评审「评论内附安全的审计摘要与材料哈希供 T305 取回」，T305 无本机库时从「实际评论安全审计摘要」取材料哈希/角色/模型/发现/结论/head；但两侧均未定义摘要的确切形状。现状 review.py 标记 `<!-- independent-review {json} -->` 的 data 仅含 verdict/reviewer/model/head/findings/flagged/parsed，不含材料哈希、时长、按严重度计数、身份分离。两个任务隔两期由不同执行方实现、各自用自造夹具评论，格式漂移要等 G3 真实链路才暴露，修复成本高 | 在 C6 或 T105 任务书写明摘要标记与 JSON 字段清单（扩展或并列于 REVIEW_MARK），并要求 T305 测试解析 T105 形状的样例评论 |
| F3 | 一般 | T403 目标终态「harness末尾…if:always()」/ 验收行 3 | harness.yml 由 pull_request 触发时（同仓库 PR）job 定义与代码来自 PR head。若执行方按字面在 harness.yml 的 PR job 内加 if:always() 告警步骤并授写权限，同仓库 PR 代码即可获得 write token（T403 只点了 fork PR，未排除同仓库 PR）。正确承载应为：PR 失败告警走 auto-merge.yml（workflow_run、默认分支代码）的 if:always() job；harness.yml 仅 push main 运行末尾告警。任务书未写明分工，验收只断言「默认分支代码、最小 write 权限」，未断言「pull_request 触发的 job 无写权限」 | T403 写明两模板各自告警承载；验收行 3 增加断言：pull_request 触发的 job（含其 steps）permissions 无 write |
| F4 | 一般 | T204 目标终态「终止运行的 jsonl…保留运行状态/仍活动流」 | 「活动/终止」判定依据未定义。dispatch 的运行状态格式（槽位锁 `slots/<n>.json` 的 pid、`runs/<task>/<number>/round-N.jsonl` 目录约定）在 dispatch.py，而 dispatch.py 不在 T204 白名单；执行方需自行读源码推断，猜错则误删活动流（破坏审计）或漏删 | T204 写明判定依据（如：存在存活 pid 槽位锁对应的 attempt 目录视为活动；其余按时间超期清理），并引用 dispatch.py 相应常量/路径 |
| F5 | 一般 | C3「命令/对话形态」× T203 验收行 1/2 | 「命令正文」「会话正文」无可执行判定定义：负例夹具只列单行 git/python、管道/多行、User:/Assistant:，白名单侧只说「安全规则摘要可通过」。两可情形（`git status` 是命令；`engine/**`、含 `&&` 字样的规则理由算什么）无仲裁规则，执行方自选启发式后夹具通过不代表真实记录不误报（拒绝合法 guard_denials 摘要→run-check 误失败）或不漏报 | C3 增补最小判定集（命令前缀/结构规则、会话标记规则、明确放行示例清单），T203 夹具覆盖示例正反例各至少一条 |
| F6 | 建议 | T102 验收行 1 × C1「install/upgrade/identity/weekly→ci」 | C1 把这些命令的入口事件归 T102 的 cli.main 统一埋点，但验收「四种入口」只抽样未点名；若执行方逐命令特判会漏且测试不失败 | 验收加一行：install/upgrade/identity/weekly 各至少产生一条入口事件 |
| F7 | 建议 | T107/T602「前置条件」 | 两任务为设计方任务，却保留模板句「实现方先用已合并任务书通过 bin/harness taskbook…--on-main」 | 改为「设计方核对任务书已合并且与 origin/main 一致」 |
| F8 | 建议 | 追溯表「拆分审查状态」× B61 × T107/T602 | B61（PATH_TOKEN 不识别多点根文件名）已如实登记，且 T107/T602 声明 K7/R3 为最高档、漏核对不降档，风险有限；但「直接读取涉及文件列补核对」的结果目前只是自述承诺，无产出物可审；文档任务（D080–D085）的人工验收也全部是设计方自证 | 整包 PR 附上 24 份任务书白名单×涉及文件×类别/风险对照清单与 B61 补核对结果；用户审 PR 时重点抽查 D080–D085 |
| F9 | 建议 | T105/T108/T305/T403 预算 | 180 分钟内完成多文件实现 + 4 组大夹具 + `bin/verify --full` 偏紧（升级路径已有，但易诱发压缩断言） | 任务书明示「预算不足时优先缩夹具规模并保留每行验收至少一个断言，先报告再动手」 |
| F10 | 一般（决策点确认） | C2 × 设计 2.5 | C2 把 CI 事件按 `ci:<run>:<attempt>:<job>` 分链，改写了设计「同一来源同一 trace 单链」的直觉语义（防多临时库 seq=1 冲突的实质取舍）；audit 完整性、trace --ci 合并、账本 chains 都按「多子链各自完整」理解。材料已诚实标注「细化草案，不称已审定」，未冒称 | 评审确认该标注；建议整包 PR 描述将其列为显式用户决策项（与 C3/C4/C5/C7 的格式细化一并），避免随整包默认通过 |

**压力测试项核对结果**：不同 run/job 从 seq=1、原 hash 不改写 → T109 行5 + T301 行2/3 + C5 去重/冲突规则，覆盖；已合并 API 失效不改原判定 → C0 + 各任务失败隔离行 + T106，覆盖；本机链不外发而账本需全部摘要、review 后置资料复取 → T201 记录摘要 + T105 评论摘要 + T305 无库组装，路径成立但评论格式有 F2 缺口；前缀锚点与删尾 → T402 行3，覆盖；30/90 天到期核验 → T401 行3，覆盖；PR 代码与可信默认分支权限 → T303/T305 已覆盖、T403 有 F3 缺口；合并/账本竞态幂等 → T305 行3，覆盖；B38 边界 → F5；B61 → F8。

## 四、各任务验收反事实核对（96 行）

说明：每行给出「未实现时的失败点」核对；✓＝任务书的失败描述具体且经我核对确实会失败；标注缺口的引用发现编号。

| 任务·行 | 验收要点 | 未实现时如何失败（核对） |
|---|---|---|
| T109-1 | prev_hash 进规范化摘要 | ✓ 测试独立算 sha256 与库值比对；实现漏 prev_hash 则两侧摘要不等，具名断言失败 |
| T109-2 | seq 缺口被独立发现 | ✓ 夹具手工构造 seq 1/3 且 hash/prev_hash 合法；若实现只靠 hash 报错兜底，构造的合法链不触发任何报告，「应有发现」断言失败——真定向负例 |
| T109-3 | step/空引用/span/IO 失败隔离 | ✓ 逐项注入；span TypeError 遮盖业务异常时行为断言失败 |
| T109-4 | 产物原子替换 + 新版库保护 | ✓ 注入写失败/双进程同产物；`target.write_bytes` 直写或新版库被当完好时失败。现状 save_artifact 确为直写（events_db.py:282）、verify 不查版本（events_db.py:225），负例针对真实缺口 |
| T109-5 | run/attempt/job 链隔离 | ✓ 环境变量模拟；source 仍为 "ci" 时同 trace 两 job 的 seq=1 撞 UNIQUE(source,trace,seq) 或断链 |
| T102-1 | 四种入口行为保持+有事件 | ✓ 去包装→事件存在断言失败；吞异常/改码→行为比对失败。小缺口见 F6 |
| T102-2 | verify 逐项+汇总+日志哈希 | ✓ 漏项/只记汇总→事件集合断言失败；`--skip`/strict 转换后记录（现状 Result.status pass/fail/skip，verify.py:44） |
| T102-3 | integrity 失败元数据 | ✓ 现状 check() 返回 (bool,str)（integrity.py:45），只报入口事件或缺 lock 哈希/文件计数时字段断言失败 |
| T102-4 | 引用准备失败隔离 | ✓ OSError 注入下 stdout/返回值/异常逐字比对 |
| T103-1 | risk 逐文件/汇总 | ✓ R0/R2/R3、R1 降级、改测试计数夹具；漏文件或计数错→字段断言失败 |
| T103-2 | route 全规则+facts+配置提交 | ✓ 只记最终布尔→每 Rule 事件缺失；用 PR 配置替代 main 配置→引用哈希断言失败 |
| T103-3 | workflow_run 用 PR 分支 | ✓ 现状 current_trace 在 GITHUB_REF_NAME=main 时返回 main@sha（events.py:74-76）；沿用则 trace 断言失败。--branch 优先与 auto-merge.yml 传参一致 |
| T103-4 | 事件失败不改 policy | ✓ 库/API 失败注入下 Rule 列表、summary 文本、GITHUB_OUTPUT 逐字比较 |
| T104-1 | 拒绝元数据无载荷 | ✓ 扫库断言无命令/正文/绝对路径；复制原拒绝载荷（现含完整命令文本）时失败 |
| T104-2 | 三钩子拒绝 | ✓ 现状三钩子入口均存在（git_guard.py、.githooks/ 三脚本）；任一未埋→事件缺失 |
| T104-3 | 放行零事件 | ✓ 逐条记放行则行数非零 |
| T104-4 | sink 失败保持拒绝 | ✓ 拒绝 JSON/文本与退出码 2（command_guard.py:230-251）逐字比对 |
| T105-1 | 全链成功+失败分支事件 | ✓ 假 host/gh 跑 admit→escalate 失败分支；漏路径/事件先于操作/错 trace 时失败 |
| T105-2 | allowed/denied 计数+流哈希 | ✓ 现状 parse 按理由计数（dispatch_host.py:151-154），夹具含双理由拒绝——理由数冒充调用数时计数断言失败；流按原始字节哈希 |
| T105-3 | review 元数据/失败 | ✓ 通过/不通过/评审方非零三态；漏 error.kind 或未在评论附审计摘要（F2 格式缺口由后续任务暴露）时失败 |
| T105-4 | 观察不改派发/评审 | ✓ 调用序列与退出码比对 |
| T106-1 | 检查入口三态 | ✓ 缺埋点命令开启态「有事件」断言失败；行为受 sink 影响→逐字比对失败。事件下限未定义（F6） |
| T106-2 | 判级/路由/run-check 三态 | ✓ 无失败态写入证据时失败（要求三态真实进入写入点） |
| T106-3 | 守卫 JSON/文本/三钩子三态 | ✓ 改拒绝/退出码或全量过滤 stderr 时失败 |
| T106-4 | 归一化有界 | ✓ 注入额外 stderr/不同返回值必须检出——防过宽归一化的负例；白名单仅剔除 T101 固定提示与耗时字段并另断言 |
| T107-1~3 | 安全边界/配置/CHANGELOG 人工核对 | 人工：无自动化失败信号；依赖设计方逐条对照留痕 + 用户审 PR（F8） |
| T108-1 | taskbook/hygiene 元数据 | ✓ 入口事件替代内容事件或泄漏违规正文时失败 |
| T108-2 | base/mutation/evidence 字段 | ✓ 目标/缺陷只记汇总或丢「前败后过」字段时失败 |
| T108-3 | Finding/指标/度量逐项 | ✓ 漏项或 list/dict 交 emit 被过滤器吞掉（events.py:130-136 确会丢弃嵌套）时失败 |
| T108-4 | 关闭/坏库保持原输出 | ✓ 八模块逐字比对退出码 |
| T201-1 | 时间线+精确锚点 | ✓ 锚点≠落盘前 chain_head（时点抓错）时失败 |
| T201-2 | 轮次/尝试不混 | ✓ 只写最后一轮或计数混淆时失败 |
| T201-3 | 旧字段保留 | ✓ RECORD_FIELDS（run_check.py:33-37）仍被验证；删旧字段/改 prompt 字节失败 |
| T201-4 | 不补推/sink 容错 | ✓ 假 gh 断言推送次数与 CI 轮次不变；为补锚点再推或 sink 阻断时失败 |
| T202-1 | 提示词含尾标记 | ✓ render_prompt 产物断言（现状模板无此段则失败） |
| T202-2 | 只取最终 assistant 报告 | ✓ 工具消息/首条消息伪造标记不采信；旧 parse 三元返回不变 |
| T202-3 | 无标记/坏 JSON/隐私诊断 | ✓ 缺标记误当 [] 或禁项入库时失败 |
| T202-4 | 记录与 clarify 一致、不改返回码 | ✓ 只改提示词不写记录或强制失败时失败 |
| T203-1 | 禁项全位置被拒 | ✓ 顶层/嵌套/列表/键注入；只查新字段或长文则短命令负例漏（判定规则缺口 F5） |
| T203-2 | 新旧合法记录通过 | ✓ 一刀切拒绝旧字段时失败 |
| T203-3 | Finding 不回显禁值 | ✓ 报告拼接原值时失败 |
| T203-4 | policy 拒绝与事件无关 | ✓ 现状 Facts.run_findings 接入 policy（policy.py:75）；只警告或依赖事件时失败 |
| T204-1 | 边界天数/自定义/非法配置 | ✓ 29/30/31 冻结时钟断言删除集合；错删边界或连元数据清理时失败 |
| T204-2 | 每日一次/跨进程/时钟回退 | ✓ 每次 emit 清理或重复清理/跨日漏清理时失败 |
| T204-3 | 范围/符号链接/活动流 | ✓ 删到 artifacts 目录外或仍写入的流时失败；活动判定依据未定义（F4） |
| T204-4 | 清理失败隔离 | ✓ 异常传播或 stderr 刷屏（现 _warn_once 每进程一次，events.py:37）时失败 |
| T205-1 | 末轮预警在 wait 前 | ✓ 预算 3/1 两组；超限后预警或漏预算 1 时失败 |
| T205-2 | 阈值按被拒工具数 | ✓ 阈值-1/阈值/阈值+1；用理由计数（现状口径）时失败；非法配置禁用不误启 |
| T205-3 | 预警与升级共用一条 | ✓ 各新建评论/议题或缺 escalation 标签时失败 |
| T205-4 | 发布失败保持原退出 | ✓ 异常遮盖原 Stop/失败时失败 |
| T301-1 | 往返一致+安全 manifest | ✓ 重新 emit 或把原始 artifact 一并上传时失败 |
| T301-2 | 幂等+多来源隔离 | ✓ 统一 source=ci 或未按 hash 去重→冲突/重复行断言失败 |
| T301-3 | 坏包原子拒绝 | ✓ 坏哈希/缺中间/冲突 seq/未来 schema/隐私禁项各拒绝且原库不变；重算哈希掩盖篡改时失败 |
| T301-4 | 筛选不冒充导出 | ✓ 断链子集当可导入包时失败 |
| T302-1 | events 过滤/计数/IO/参数 | ✓ 过滤只改标题或总计不符时失败；非法枚举/时间须退出 2 |
| T302-2 | 标识解析+歧义 | ✓ 把 PR 号当 trace 或随意选第一条时失败 |
| T302-3 | 时间线内容 | ✓ 只打印 step 名/排序/来源错时失败 |
| T302-4 | --ci 分页/幂等/诊断 | ✓ 错 head、漏 route job、坏包静默跳过时失败；须核对 workflow/仓库/run，不信任包自报 |
| T303-1 | 失败仍导出 | ✓ 现状 harness.yml 无 upload 步骤；用 continue-on-error 抹平原失败时失败 |
| T303-2 | 物理名唯一+90 天+白名单 | ✓ 撞名、上传 *.db/原始日志、缺 retention 时失败 |
| T303-3 | summary 从事件渲染 | ✓ 与原机器输出不符或漏失败/skip 时失败 |
| T303-4 | 安装布局+可信检出 | ✓ 模块 import 失败或执行 PR 引擎时失败 |
| T304-1 | 三种合并事实 | ✓ 只覆盖 App 或把 owner 当批准者时失败；缺字段须 unknown 不伪造 |
| T304-2 | 抽样+后登记 escape | ✓ 只读合并当时或覆盖旧快照时失败 |
| T304-3 | 分页+重复同步幂等 | ✓ 未分页或读 API 次数影响事件哈希时失败（获取时间不进快照） |
| T304-4 | API 失败不改判定 | ✓ 把不可得当完整或执行合并动作时失败 |
| T305-1 | 无本机库组装账本 | ✓ writer 依赖本机库或漏评审/route/记录摘要时失败（评审摘要格式风险 F2） |
| T305-2 | 只写已合并+匹配 head | ✓ 覆盖 B52 或选最新非合并 head 时失败 |
| T305-3 | 竞争/幂等/冲突 | ✓ bare remote 双 writer：force push、覆盖旧文件、丢另一 PR 文件时失败 |
| T305-4 | 锚点评论+ruleset+隐私 | ✓ 只写分支无评论、假称 append-only、泄漏库/原始流时失败 |
| T401-1 | 按来源复原 | ✓ 只显示事件名或来源混合时失败 |
| T401-2 | 各类引用原字节核对 | ✓ 现状 common.git 去结尾换行（run_check.py:124 注明），截断换行或规范化不一致时失败 |
| T401-3 | mismatch/expired/unavailable 分立 | ✓ 一律跳过当成功时失败；30/90 天边界夹具覆盖 |
| T401-4 | 批量分页+安全解析+退出码 | ✓ 漏页、path 越范围、执行引用内容、退出码不清时失败 |
| T402-1 | 按风险/种类应有环节 | ✓ 只跑链校验未查应有环节时失败；设计方 PR 不要求派发记录 |
| T402-2 | 批准绑定+none+独立性 | ✓ 只看 APPROVED 状态、把人当 App、误报 none、评审同设计方未发现时失败 |
| T402-3 | 前缀锚点+尾删+账本不一致 | ✓ 比最终 head 误报合法追加、或仅 verify_chain 漏尾删时失败 |
| T402-4 | 配置缺省/合法/非法+独立 | ✓ 非法值静默关规则或 audit 影响 policy 时失败 |
| T403-1 | 七类触发全断言 | ✓ 任一触发漏接或历史误触发时失败 |
| T403-2 | 远端标记跨机去重 | ✓ 删本机缓存重跑仍只更新一条；只靠本机 DB 去重时失败 |
| T403-3 | 失败也告警+可信代码 | ✓ judge success 门槛漏失败告警、fork/PR 代码持 write token 时失败；承载分工歧义 F3 |
| T403-4 | 发布失败保持原结论 | ✓ 评论异常遮盖 review/verify/route 结果时失败 |
| T501-1 | 周窗口+去重 | ✓ 时间过滤或 hash 去重漏时双算，断言失败 |
| T502-2→T501-2 | 拒绝计数对账 | ✓ executor 计数与 guard 细条相加翻倍时失败（现状记录 guard_denials 按理由，run_check RECORD_FIELDS 有该字段可对账） |
| T501-3 | 审计发现按 head 去重 | ✓ 每次 audit 运行累加历史发现时失败 |
| T501-4 | 无库/坏库旧小节逐字不变 | ✓ 改原来源或把不可得写成 0 时失败 |
| T601-1 | 真实入口全链 | ✓ 任何入口未接或预写假事件替代产品产物时失败 |
| T601-2 | 设计方 PR+冷机复原 | ✓ 审计强依赖本机库或强制所有 PR 有派发记录时失败 |
| T601-3 | 注入+恢复 | ✓ 恒真 finding 或删尾仅靠内部链校验时失败 |
| T601-4 | 告警幂等+不外发 | ✓ 同 reason 重复评论或 db/WAL/SHM/原始日志/会话出现在导出时失败 |
| T602-1~4 | 命令/迁移/边界/门禁人工核对 | 人工：同 T107，依赖留痕与用户审（F8）；第 4 行要求 G1–G5 证据为实际命令输出，不得手写通过 |

## 五、结论

**需修订。**

阻断级问题为零：追溯完整、依赖无环、白名单与准入一致、96 行验收均可说出失败方式。但 F1（dispatch.py 体量棘轮与 T105/T201/T205 白名单冲突，执行方在白名单内无法自救）是可预见且高概率的执行期死锁，须先修订任务书或预先声明基线配套方案；F2–F5、F10 应一并处理（F10 随整包向用户显式列为决策项）。F6–F9 建议采纳。处理完发现并经用户审定后，整包方可作为文档 PR 提交；本评审不替代用户对任务书 PR 的批准。
## v1材料哈希清单

该清单固定评审输入，不代表当前v2文档哈希。原内容本机快照可复取。

| 文件 | sha256 |
|---|---|
| `AGENTS.md` | `35d6fdcb49faf7f562a01e9c079ec879550283a2dab2f2dfcacdd47603fb583b` |
| `docs/backlog.md` | `e07bff26366f387b43d71e46f178b045d9227ad82c83124bdf049e0181e8fed2` |
| `docs/task-splitting.md` | `62d0a74abd3d4f381f8ea7fb38111c5ff02fef95ac27136eedda964dfa99ea21` |
| `docs/plans/2026-09-29-observability-design.md` | `b6e43ad66d275bf721084e4b3e5115e77574227d54eb99f28cfb870058f6e106` |
| `docs/plans/2026-09-29-observability-execution-plan.md` | `0bc646683285c0260c3cfcf97dcdff9804c7c8fd1284ff1b5fe758bf70caa542` |
| `docs/plans/2026-09-29-observability-task-contracts.md` | `18998233b6ebfbd0872dcb93a8057b35894da46cbf23e448a22c8a55114b6ba9` |
| `docs/plans/2026-09-29-observability-traceability.md` | `4f8fab3a9b8ab497477d5fb813472a7a6d441d1ca2281ab2f60a3ccdfcbbd617` |
| `docs/plans/task-101-events-core.md` | `c0f3702c5c53e13b9af0d998635962a3ed418c0f5ab32e4dbfc2013dd921b56c` |
| `docs/plans/task-102-events-verify.md` | `567ad6adca7fcd9ef589e79805d93bdb4b3aa218238c8391e2c837b4c62b5110` |
| `docs/plans/task-103-events-route.md` | `8d9abf51992de19b6a8aa993e6c6c8d26f7e3a9923268c0d48a9ffbab915e273` |
| `docs/plans/task-104-events-guard.md` | `878a8bbe2af5ebf43c86a4cc5fb1d150748244fe056209acfdb1f57a12f24030` |
| `docs/plans/task-105-events-agents.md` | `275eeb7892053b478e8c44137b6ecf61f4a6584a768e048e46b5313b9bcf9215` |
| `docs/plans/task-106-events-equivalence.md` | `6e4316e08c1dd542073d07e34c0f53b70c64b86dbba4a9a78084cac0845b8197` |
| `docs/plans/task-107-events-docs.md` | `c491c9f3f972ccb426d336fb9dfe83574d2fcc737b2ec140a21aefc6ab553e67` |
| `docs/plans/task-108-events-checks.md` | `cd94618569b58cb7e367bca1e32df48d6ce2baf8b29d98290acb842daa477bbf` |
| `docs/plans/task-109-events-hardening.md` | `b3a92ecdab9e55b563f254621e70a7a467c0c239a2ab17ae7c26b27be3f1fe85` |
| `docs/plans/task-201-run-timeline.md` | `040fcf2671cf1bddb44d994c1bfc62b3a10647f547bc302575e2fe639f640932` |
| `docs/plans/task-202-missing-context.md` | `26c00b86aa4eaa3986db6d3ccb6dffe4a3d80951152b4dda3570cfb561f7510d` |
| `docs/plans/task-203-run-record-privacy.md` | `b851c0c81678f0ede0279d523e5eb4eb26cf45868d648d128e024918347a869b` |
| `docs/plans/task-204-artifact-retention.md` | `53bfd4a4ee7b64868dc84c9bdfca06b869c6682f4841631e85327495135bd226` |
| `docs/plans/task-205-dispatch-alerts.md` | `9bbe4e55e1ec12afd80e25868e76fb602d720ff2aad6a7d23ab067354212b653` |
| `docs/plans/task-301-events-io.md` | `bea372abc7707f9c81549c29a4523039b3dcb1c797280d0f76dae34a86f2c41c` |
| `docs/plans/task-302-trace-events-cli.md` | `ebbc2f2b9f0418f996e7a1aa08158b5734b2dddfa8c6c9a954b9e15ece2c537b` |
| `docs/plans/task-303-ci-events.md` | `176f894bb352959a952cff163f938296733d4a2ec655b18ff1afa667e07a7e59` |
| `docs/plans/task-304-github-events.md` | `8aeaae43b0e6c0b00bdce386808de8a7ddc9ac6fe5f74db132d53c78ca48a6f4` |
| `docs/plans/task-305-audit-ledger.md` | `925a70f120d9e63f591129eda7934de444296b196098a01f7b51d4a73c6ab5b4` |
| `docs/plans/task-401-audit-reconstruction.md` | `c6dffe0369f005e4617c8a33cd140e2d34a2df361b2d07a51943762c1ce6ddac` |
| `docs/plans/task-402-audit-completeness.md` | `8b8b88fa33153f7ca8a84f0b80f1b5d01755f0c47cda346d5c10e3d70d80800f` |
| `docs/plans/task-403-alert-cli-workflows.md` | `cd8b42ec982c7b27f7acd0db96cfa2c60eaea8698935e99cff3a94d4aa88a7dd` |
| `docs/plans/task-501-weekly-events.md` | `66df830006bdc0f97ab5a709e052c126208c145b463f7b7953d194db2e4867e2` |
| `docs/plans/task-601-observability-e2e.md` | `7d43432f3b2abc3725eaf8a37a6e4a6580726b81f8da4c833d665bf8f7c83ea9` |
| `docs/plans/task-602-docs-migration.md` | `876439229507776f55c443d8b875d85c7d3af5bd9fdeb076d16d52443d5997c6` |

## 第二轮复核与后续澄清

用户明确继续后，以v1→v2差异和F1–F10处理为入口，OpenCode仍以pure/plan只读运行。输入34份文件已固定SHA与input-v2快照；过程/输出保存在本机build/observability-split-review-v2。第二轮正常退出0，无error事件、stderr为空，最终stop。独立结论为**可提交（待用户审定C8的U1–U5）**，不是提交/推送/派发授权。

| 第二轮建议 | 设计方处理（建议级、不阻断） | 位置 |
|---|---|---|
| S1 review-pack材料覆盖 | 增加pack_v1原字节哈希与本机产物引用，不上传原文、不执行PR代码复算；30天到期明确reference_expired | C6、T105/T305目标终态；属于U5格式细化 |
| S2 decision.reason词表 | 明确C0状态、既有Attempt退出与C4稳定reason键组成固定去重词表，生产/检查同表，不自造同义短token | C3 |
| S3 expired存储语义 | 明确为报告/查询层的reference_expired，不向refs表加列 | T204验收3 |
| S4 告警措辞 | 明确alerts.py由T205新建；删去T403含混旧句，仅保留F3的push main/workflow_run承载分工 | T205/T403目标终态 |

上述澄清在第二轮输出后补充，未再启动评审；第二轮建议不阻断提交，原文结论与覆盖边界保留如下。结构/准入/链接由设计方重新核对，用户仍须审定U1–U5。所有改动仅工作区，版本/平台/实现未执行。

### 第二轮原文

# B46 第二轮独立拆分复核（OpenCode，只读）

## 一、F1–F10 逐项复核

| 发现 | 结论 | 依据 |
|---|---|---|
| F1 严重：体量/复杂度棘轮 | **解决** | T105 白名单加 `engine/agents/dispatch_observation.py`、T201 加 `run_timeline.py`（两文件步骤1同步）；T105/T201/T205 各加「体量约束（F1）」段：dispatch.py 只接线、≤800行、棘轮不升、不上调基线、保不住质量就升级；追溯表串行表加 `dispatch_observation.py/run_timeline.py` 行，执行计划 §3.4 同步。现状核实：dispatch.py 656 行；`python_glob="**/*.py"` 递归计入新模块（.harness/config/checks.toml:36），quality.py 的 `files_over_800`/`complex_functions` 只降不升、基线不在白名单——外置模块给执行方白名单内自救路径，即 v1 修复要求本身 |
| F2 一般：评审摘要格式漂移 | **解决** | C6 新增固定标记 `<!-- harness-review-audit <单行JSON> -->`、JSON v1 全字段清单（schema_version…severity_counts/materials）、recipe（task_v1/diff_v1/ci_v1/pr_v1）、model_basis、none 哨兵、自引用规避。T105 验收3「按C6新标记精确断言字段/材料哈希…标记或形状不同也必须失败」；T305 验收1「评审样例必须符合T105/C6精确形状，缺标记/字段、错误head、未来版本各报missing；生产者/消费者自造不同JSON格式也失败」。现状核实：REVIEW_MARK/reviewed_heads 独立（review.py:371-381），并列标记可行；`common.git` rstrip("\n")（common.py:91）+ `diff[:200_000]`（review.py:291），C6 diff_v1 描述与现状一致；task 哨兵「无」吻合（review.py:288） |
| F3 一般：同仓库PR写令牌 | **解决** | T403 新增「告警承载明确分工（F3）」段：harness.yml 仅 push main 末尾告警；所有 pull_request job/step 只读、同仓库/fork 一视同仁；PR 成败由 auto-merge.yml workflow_run 独立 job 承载、if 不继承 judge success 门槛；并禁止「默认分支checkout使PR可改job持写权限」。验收3 增「分别重放同仓库PR/forkPR，所有pull_request的job及继承权限均无write」+「同仓库PR取得write也必须失败」 |
| F4 一般：活动/终止判定 | **解决** | T204 新增「活动流规则（F4）」段。现状逐项吻合：`state_dir(root)`（dispatch.py:135）、锁 `slots/<n>.json` 字段 pid/task/branch/started_at+可选 attempt（176-177、406）、`_alive` 语义 ProcessLookupError→死/PermissionError→存活（151-158）、原始流 `runs/<task.id>/<number>/round-N.jsonl`（307、400、407，number 即记录 attempt）。存活PID保护整个task全部attempt、坏锁保守跳过、mtime+锁+lstat 复核均写入；验收3 增活/死PID/权限不明/坏锁/旧attempt/续跑子用例 |
| F5 一般：B38 边界 | **解决** | C3 新增最小判定集与 C01–C07/A01–A05 表；guard_denials 仅精确可信集合例外（现状 COMMAND_RULES/TOOL_RULES/IMPLEMENTER_* 常量名吻合，command_guard.py:29-151）。T202 生产侧统一5个 summary 短ID、未知ID invalid；T203 验收1/2 增 C01-C07/A01-A05 具名子用例与「可信规则提及git命令仍通过、追加正文不行」；旧记录兼容由验收2覆盖。已如实列 U3 待审 |
| F6 建议：入口映射 | **解决** | T102 新增第5行 `test_all_registered_entrypoint_mappings_and_guard_exceptions`，步骤1命令已含。现状 COMMANDS 26项存在（cli.py:15-48）；逐个遍历+点名 install/upgrade/identity/weekly，特判遗漏/阶段错/守卫放行产生日志均点名断言失败——**能失败**。「events/trace为零、audit/alert自记」在 T102 时刻命令尚未注册，属合同性前瞻说明，与 C1 一致且不与动态遍历冲突 |
| F7 建议：设计方任务派发句 | **解决** | T107/T602 前置条件改为「设计方核对本任务书已经合并且与origin/main一致…不调用派发」；T105 等派发任务保留原句正确 |
| F8 建议：结构核对证据 | **解决** | `docs/review/2026-09-29-observability-split-structure-check.md`：24份表含类别/风险/架构、白名单=步骤文件列（全True）、路径清单、准入错误0、材料sha256，及 B61 直接读涉及文件列的补核对声明。抽验 T102/T105/T201/T204/T205/T403 行与实际任务书白名单逐字一致 |
| F9 建议：预算诱发减断言 | **解决** | 24份「交付与升级」段统一替换为「正负断言必须保留…夹具样本需缩小先报告设计方，确认不损失覆盖后按原流程修订，不自行缩范围」；抽验11份均落地 |
| F10 一般：CI子链决策点 | **已列为待决策** | C8 新增 U1–U5（含影响与改选条件），合同/执行计划/追溯表/backlog 状态栏均标「待用户明确审定」，无冒称已批准。该项只能由用户决定，非文档缺陷 |

## 二、新发现（本轮修订引入或暴露）

| 严重度 | 位置 | 问题与可复现方式 | 修复要求 |
|---|---|---|---|
| 建议 | C6 materials × 现状 `write_materials` | 现状生成5个材料文件（pr/task/ci/diff/**pack.md**），C6 kind 只定 task/diff/ci(+pr_v1)；评审实际读取的 review-pack 产物无 recipe/kind，审计上「评审读了什么」覆盖不完整。两侧按合同最小集仍自洽，不会卡执行 | 设计方明确 pack 是否入 materials；入则补 pack_v1 recipe，不入则在 C6 声明仅四类为审计材料 |
| 建议 | C3 × T201/T203 | C0 只定 stage/status 枚举，`stages[].decision.reason` 的「稳定结果短ID」词表未定义；T201 生产与 T203 校验两侧只能按「短token」各自理解，语义约束弱（不会互相卡死，但审计语义可漂移） | C3 补 decision.reason 建议词表（如沿用 exit 枚举与 C4 稳定 reason 键） |
| 建议 | T204 验收3 × 目标终态「不改 v1 四表」 | 「已转存引用可标 expired」若被读作 refs 表落状态列则与同任务书「不改 v1 四表」冲突 | 明确「标 expired」为报告/查询层语义（C7 reference_expired 已覆盖到期判定） |
| 建议 | T205「下沉已有白名单alerts.py的内部纯函数」；T403 保留 v1 旧句 | engine/core/alerts.py 现状不存在（由 T205 新建，C4 已明确），措辞易误读为文件已有函数；T403 目标终态 v1 的「harness末尾…if:always()」句与 F3 段并存，虽被验收3以失败方式拦截，双重表述仍有误读空间 | 措辞改写；删除或改写 T403 旧句 |

无阻断、严重、一般级新发现。未发现新的接口/验收矛盾：C6↔T105↔T305、C3↔T202↔T203、C4↔T205↔T403、白名单与依赖串行（dispatch.py 链 T105→T201→T202→T205、新模块归属 T105/T201 各自定义）均自洽可完成。

## 三、本次实际覆盖材料

- 评审与delta：`docs/review/2026-09-29-observability-split-review.md`（处理表+F1–F10原文，未重读96行旧表）、`build/observability-split-review-v2/delta.md`（全文535行）
- 跨任务材料全文：共用合同、追溯表、执行计划、结构核对证据
- 任务书11份全文：T102/T105/T107/T201/T202/T203/T204/T205/T305/T403/T602
- 现状代码（有限）：`engine/cli.py`、`engine/agents/dispatch.py`（含确认656行）、`engine/agents/review.py`、`engine/core/common.py`、`engine/guards/command_guard.py`、`engine/checks/quality.py`、`.harness/config/checks.toml`
- 未逐份重读：其余13份冻结任务书（T103/104/106/108/109/301/302/303/304/401/402/501/601）。依据：delta 显示其改动仅为「交付与升级」段统一替换（F9，已抽验11份含该句）、结构核对表覆盖其白名单/文件列，且这些文件不在本轮修订重点路径上。此为本结论的覆盖边界。

## 四、结论

**可提交（待用户审定 C8 的 U1–U5）。**

- F1–F9 已在 v2 文档实际落地且与现状代码吻合，无冒称；F10/U1–U5 未获用户批准，材料如实标注，不因本次复核视为已批准。
- 本结论仅为「任务书文档可交用户审」，不是实现、提交、推送、派发的授权；上述4条建议级发现不阻断提交，可随用户审定 U1–U5 时一并处理或由设计方修订。

## 2026-10-08 P5/P6 任务书与 G3 修订复核

设计方 Codex 修订 T501/T502 席位、T601/T602 任务书、G3 与相关计划后，OpenCode（plan，glm-5.3）从隔离工作树自行读取文件及相对 `2b00c63` 的 diff，只读核对拆分流程、现有代码与配置。结论为 **可提交**：无阻断、严重发现；三条一般发现及一条建议如下。此结论针对复核当时的 diff，下面的处理是在结论之后完成，由设计方本地核对，未冒充第二次独立评审。

| 发现 | 处理 |
|---|---|
| 追溯表仍写 Zcode 旧席位，且「剩余 25 份」漏掉 T502、忽略 P1–P4 已完成 | 状态改为当前 Codex 设计方、非 Codex 独立评审；反向追溯只列 P5/P6 待完成四份，预算与各任务书一致 |
| 执行计划分工仍写 OpenCode 固定评审方 | 改为当前非 Codex 评审链，说明 T601 R0 与 T502 R2 两条实际链；原 Zcode 分工保留为历史 |
| `docs/task-splitting.md` 把已有测试列为通常不入白名单，但 T502 需要改旧形状断言 | 不改变全局拆分规则；在已合并的 T502 任务书写明 #208 批准的唯一窄例外及限定行段，其他已有测试仍不开放 |
| `.harness/config/rules.toml` 评审方注释与配置不符（建议） | 配置文件不属本次文档合同修订，未改；不影响任务书已写明的实际链顺序 |

独立评审核对的关键事实：`designer: codex` 合法；现行评审链先跳过同席位 Codex，再用 Claude Code、OpenCode；`FakeGhBase` 缺少 T601 所需的 `createdAt` 与工作流运行路由，允许只在新测试内派生；T601 仅新增测试为 K2/R0，T502 引擎改动为 K5/R2 合同制候选；T602 依赖无环。设计方随后重新运行任务书准入、文档链接与完整验证，结果以实际命令输出为准。
