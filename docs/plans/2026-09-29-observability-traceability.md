# B46 需求追溯表与实施顺序

状态：**OpenCode第二轮可提交，待用户审定共用合同C8**。设计方Codex；独立评审方OpenCode；任务书均写designer: codex（已合并的历史T101不改）。本轮只留工作区。

依据：[设计](2026-09-29-observability-design.md)、[共用合同](2026-09-29-observability-task-contracts.md)、[拆分流程](../task-splitting.md)。设计条款→需求行→任务验收为正向；后面的任务表为反向。具名新测试尚未实现，本文不声称验收通过。T101已合并，其存量验收不足由T109补强。

## 正向追溯

| ID | 设计位置 | 需求与数据/边界 | 承担任务 | 验收断言（测试或具体步骤） | 负责方 |
|---|---|---|---|---|---|
| D001 | 1、2.1 | 从任务书/派发到合并后复原环节、输入输出、角色/模型、决定与规则；设计方PR无需派发 | [T601](task-601-observability-e2e.md) | `python3 -W error::ResourceWarning -m unittest tests.test_observability_e2e.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D002 | 1、2.5 | 应有环节缺失、内部删改/尾删、固定锚点不符可发现 | [T402](task-402-audit-completeness.md)、[T601](task-601-observability-e2e.md) | `python3 -W error::ResourceWarning -m unittest tests.test_audit_completeness.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_observability_e2e.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D003 | 1、4.2 | 卡点、耗时、首个失败、守卫拒绝；关联任务/PR/分支 | [T302](task-302-trace-events-cli.md) | `python3 -W error::ResourceWarning -m unittest tests.test_trace_events_cli.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D004 | 2.2 | 标准库SQLite、git公共目录/多worktree共库、WAL/连接关闭/版本保护、四表及所有列 | [T101](task-101-events-core.md)、[T109](task-109-events-hardening.md) | 已合并 `test_events.EventsTest` 对应验收；新增不足由T109补强；`python3 -W error::ResourceWarning -m unittest tests.test_events_hardening.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D005 | 2.2 | 完整事件结构、stage/status、actor role/host/model、decision/error、engine_version、redacted | [T101](task-101-events-core.md)、[T301](task-301-events-io.md) | 已合并 `test_events.EventsTest` 对应验收；新增不足由T109补强；`python3 -W error::ResourceWarning -m unittest tests.test_events_io.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D006 | 2.2 | refs输入与输出引用、kind/ref/sha256/size；输出视图从扁平outputs三元字段复原（C1） | [T301](task-301-events-io.md)、[T401](task-401-audit-reconstruction.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_io.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_audit_reconstruction.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D007 | 2.2 | artifacts内容寻址索引/原子文件；anchors来源、trace、stage、head_hash、fixed_in、ts | [T101](task-101-events-core.md)、[T109](task-109-events-hardening.md)、[T201](task-201-run-timeline.md) | 已合并 `test_events.EventsTest` 对应验收；新增不足由T109补强；`python3 -W error::ResourceWarning -m unittest tests.test_events_hardening.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_run_timeline.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D008 | 2.2、2.5 | seq/prev_hash/hash规范化链、并发事务、source/trace独立；独立哈希断言抓住遗漏prev_hash与seq变异 | [T109](task-109-events-hardening.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_hardening.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D009 | 2.3 | 分支trace/main@短提交/detached/CI取值，PR正文不作ID权威，attempt/head区别同名重用 | [T101](task-101-events-core.md)、[T302](task-302-trace-events-cli.md) | 已合并 `test_events.EventsTest` 对应验收；新增不足由T109补强；`python3 -W error::ResourceWarning -m unittest tests.test_trace_events_cli.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D010 | 2.3、3.4 | workflow_run显式PR分支贯穿risk/route；Actions多run/attempt/job的seq冲突隔离（C2补充） | [T103](task-103-events-route.md)、[T109](task-109-events-hardening.md)、[T301](task-301-events-io.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_route.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_events_hardening.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_events_io.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D011 | 2.4 | git与GitHub内容引用+哈希，可按固定提交/URL复取，原始字节含换行 | [T401](task-401-audit-reconstruction.md) | `python3 -W error::ResourceWarning -m unittest tests.test_audit_reconstruction.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D012 | 2.4 | 完整Pi流/verify日志本机保存、不入库内容、不上传；CI只发布安全结构化JSON文本 | [T105](task-105-events-agents.md)、[T301](task-301-events-io.md)、[T303](task-303-ci-events.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_agents.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_events_io.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_ci_events_workflows.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D013 | 2.4 | 每类长串/路径/换行/嵌套禁项、step与空引用过滤、redacted计数；永不截断改写 | [T101](task-101-events-core.md)、[T109](task-109-events-hardening.md)、[T301](task-301-events-io.md) | 已合并 `test_events.EventsTest` 对应验收；新增不足由T109补强；`python3 -W error::ResourceWarning -m unittest tests.test_events_hardening.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_events_io.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D014 | 2.4、3.6 | 放行不逐条记录；拒绝逐条；执行环节allowed/denied计数且双拒绝理由不双算 | [T104](task-104-events-guard.md)、[T105](task-105-events-agents.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_guard.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_events_agents.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D015 | 2.5、3 | 事件开/关/写入失败不进原判定；引用准备失败/span未知参数也不改变业务异常 | [T106](task-106-events-equivalence.md)、[T109](task-109-events-hardening.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_equivalence.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_events_hardening.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D016 | 2.5 | 环节前缀锚点固定在记录/CI artifact/PR评论，早期锚点与后续合法追加不误报 | [T201](task-201-run-timeline.md)、[T303](task-303-ci-events.md)、[T305](task-305-audit-ledger.md)、[T402](task-402-audit-completeness.md) | `python3 -W error::ResourceWarning -m unittest tests.test_run_timeline.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_ci_events_workflows.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_audit_ledger.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_audit_completeness.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D017 | 2.5 | 来源可信度分别标local/ci/github；核对默认分支workflow/run/head，不能信任包自报source | [T301](task-301-events-io.md)、[T303](task-303-ci-events.md)、[T402](task-402-audit-completeness.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_io.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_ci_events_workflows.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_audit_completeness.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D018 | 3.1 | taskbook.admit引用路径@提交/sha、ok、问题规则与计数；任务书合并批准事实 | [T108](task-108-events-checks.md)、[T304](task-304-github-events.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_checks.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_github_events.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D019 | 3.2/admit | 任务书sha、origin/main、结果/任务编号/类别/时长/重试/CI轮次预算 | [T105](task-105-events-agents.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_agents.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D020 | 3.2/claim | 分支、认领成功/已存在的结果 | [T105](task-105-events-agents.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_agents.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D021 | 3.2/slot | 起点origin/main、槽位序号/提交 | [T105](task-105-events-agents.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_agents.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D022 | 3.2/guard_preflight | guard_ref、必拒探针实际结果 | [T105](task-105-events-agents.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_agents.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D023 | 3.2/executor_round | 每轮提示词/起点、host/version/model/时长/exit、token/cost/守卫放拒/完整流hash | [T105](task-105-events-agents.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_agents.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D024 | 3.2/local_verify | 每轮提交范围、每项结果/失败签名/打转与本机日志hash | [T105](task-105-events-agents.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_agents.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D025 | 3.2/clarify | 执行方备注sha、澄清是否请求、missing_context条目数/类别 | [T105](task-105-events-agents.md)、[T202](task-202-missing-context.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_agents.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_missing_context.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D026 | 3.2/push_pr | 记录/PR正文sha、提交SHA/PR号 | [T105](task-105-events-agents.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_agents.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D027 | 3.2/ci_wait | 每轮PR head、轮次/结论/Actions run ID/失败摘要sha | [T105](task-105-events-agents.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_agents.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D028 | 3.2/escalate | 安全状态摘要sha、原因/PR或议题/标签 | [T105](task-105-events-agents.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_agents.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D029 | 3.3/verify.item | head/档位、每项状态/时长/退出码/失败签名/完整日志hash；skip/strict含在内 | [T102](task-102-events-verify.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_verify.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D030 | 3.3/verify.summary | head/档位、通过与否/dirty | [T102](task-102-events-verify.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_verify.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D031 | 3.3/integrity | lock/目录hash、一致性/被改文件数/失败kind | [T102](task-102-events-verify.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_verify.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D032 | 3.3/hygiene | 范围、违规数/规则名，不保存匹配内容 | [T108](task-108-events-checks.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_checks.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D033 | 3.3/base_tests | base/head、结果/forced/安全摘要 | [T108](task-108-events-checks.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_checks.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D034 | 3.3/risk | base/head、最终/逐文件等级/规则、R1声明/降级、已有测试被改数 | [T103](task-103-events-route.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_route.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D035 | 3.3/mutation | 目标、得分与基线 | [T108](task-108-events-checks.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_checks.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D036 | 3.3/evidence | 每缺陷编号/提交/改代码/前败后过 | [T108](task-108-events-checks.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_checks.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D037 | 3.3/run_check | 记录引用、任务归属/每Finding/trailer/格式/CI预算；T203新增内容判定 | [T108](task-108-events-checks.md)、[T203](task-203-run-record-privacy.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_checks.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_run_record_privacy.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D038 | 3.3/quality | 基线路径@提交/哈希、每个指标当前值/基线 | [T108](task-108-events-checks.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_checks.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D039 | 3.3/metrics | base/head、全部数值交付度量 | [T108](task-108-events-checks.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_checks.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D040 | 3.4/route.facts | main autonomy/rules提交与sha、风险/机器声明类别/行数/窗口逃逸数 | [T103](task-103-events-route.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_route.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D041 | 3.4/route.rule | 每Rule ok/理由/decision.by=policy与固定规则键 | [T103](task-103-events-route.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_route.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D042 | 3.4/route.result | auto_merge/audit/approval方式与main配置引用 | [T103](task-103-events-route.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_route.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D043 | 3.4/review | 材料任务书/diff/CI sha、PRhead、reviewer/model/时长/结论/严重度计数/评论URL/失败kind/身份分离 | [T105](task-105-events-agents.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_agents.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D044 | 3.5/merge | PR/merge引用、批准者login/type/绑定head、合并者/方式/标签，手工/App/none均覆盖 | [T304](task-304-github-events.md) | `python3 -W error::ResourceWarning -m unittest tests.test_github_events.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D045 | 3.5/audit_sample | 抽样与否、audit议题号/PR，stage用ci不新增枚举 | [T304](task-304-github-events.md) | `python3 -W error::ResourceWarning -m unittest tests.test_github_events.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D046 | 3.5/escape | 后登记escape议题/引入PR/类别、新快照取回不覆写合并账本 | [T304](task-304-github-events.md) | `python3 -W error::ResourceWarning -m unittest tests.test_github_events.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D047 | 3.5/alert | 触发事件/安全状态、条件/评论位置/发布结果 | [T403](task-403-alert-cli-workflows.md) | `python3 -W error::ResourceWarning -m unittest tests.test_alert_cli.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D048 | 3.6/command | deny规则/角色/工具类别/安全相对目标；无命令全文；放行不记 | [T104](task-104-events-guard.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_guard.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D049 | 3.6/git | 三个钩子、分支/规则、deny；拒绝结果与退出码原样 | [T104](task-104-events-guard.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_guard.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D050 | 3.7/record | 持久摘要trace/stages/链头、每轮attempt、旧字段不变/不额外推送补未来事件 | [T201](task-201-run-timeline.md) | `python3 -W error::ResourceWarning -m unittest tests.test_run_timeline.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D051 | 3.7/B36 | missing_context尾报告/类别/安全摘要与unknown-invalid；结束时明确无缺失 | [T202](task-202-missing-context.md) | `python3 -W error::ResourceWarning -m unittest tests.test_missing_context.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D052 | 3.7/B38 | 旧/新记录递归禁命令/路径/会话，Finding失败且不回显；policy原run_findings接入 | [T203](task-203-run-record-privacy.md) | `python3 -W error::ResourceWarning -m unittest tests.test_run_record_privacy.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D053 | 4/local | 元数据默认不清理，产物默认30天、UTC边界/每日首emit/跨进程一次；B40原始终止流也清理 | [T204](task-204-artifact-retention.md) | `python3 -W error::ResourceWarning -m unittest tests.test_artifact_retention.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D054 | 4/local | 清理不越范围/符号链接、不删活动流；无守护进程/失败隔离 | [T204](task-204-artifact-retention.md) | `python3 -W error::ResourceWarning -m unittest tests.test_artifact_retention.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D055 | 4/CI | 导出JSON非SQLite、本机幂等按hash导入、完整链前缀/冲突坏包原子拒绝 | [T301](task-301-events-io.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_io.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D056 | 4/CI | 每次run/attempt/job的harness-events安全manifest、90天、失败后always上传/事件summary | [T303](task-303-ci-events.md) | `python3 -W error::ResourceWarning -m unittest tests.test_ci_events_workflows.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D057 | 4.1 | 专用harness-audit长期JSON、每已合并PR一文件、引用/决定者/链头锚点与本机摘要；无库亦可写 | [T305](task-305-audit-ledger.md) | `python3 -W error::ResourceWarning -m unittest tests.test_audit_ledger.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D058 | 4.1 | 正常追加、竞争/重复幂等/冲突不覆盖；PR固定锚点评论；禁删禁强推ruleset模板 | [T305](task-305-audit-ledger.md) | `python3 -W error::ResourceWarning -m unittest tests.test_audit_ledger.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D059 | 4.2/events | since/stage/status过滤、计数/JSON，参数错误与导出导入模式 | [T302](task-302-trace-events-cli.md) | `python3 -W error::ResourceWarning -m unittest tests.test_trace_events_cli.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D060 | 4.2/trace | 任务/PR/分支解析与歧义、时间线输入输出决定/最长耗时/首失败、来源 | [T302](task-302-trace-events-cli.md) | `python3 -W error::ResourceWarning -m unittest tests.test_trace_events_cli.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D061 | 4.2/trace-ci | --ci分页多workflow/job/attempt、匹配PR head、过期/不可得诊断、幂等导入 | [T302](task-302-trace-events-cli.md) | `python3 -W error::ResourceWarning -m unittest tests.test_trace_events_cli.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D062 | 4.2/audit-rebuild | PR或all-merged/since批量分页，逐引用原字节/规范化快照复取与哈希核对 | [T401](task-401-audit-reconstruction.md) | `python3 -W error::ResourceWarning -m unittest tests.test_audit_reconstruction.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D063 | 4.2/audit-complete | R2+评审且身份不同、auto有route、task有记录锚点、app低风险批准者/绑定head、none模式 | [T402](task-402-audit-completeness.md) | `python3 -W error::ResourceWarning -m unittest tests.test_audit_completeness.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D064 | 4.2/audit-tamper | 链/前缀锚点/账本与运行层对head核对；中删改/尾删/引用变动/到期均如实报告 | [T402](task-402-audit-completeness.md)、[T401](task-401-audit-reconstruction.md) | `python3 -W error::ResourceWarning -m unittest tests.test_audit_completeness.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_audit_reconstruction.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D065 | 4.2、6/P5 | 周报附事件小节：阶段耗时、守卫拒绝、升级原因、审计发现及缺上下文；旧指标不变且对账 | [T501](task-501-weekly-events.md) | `python3 -W error::ResourceWarning -m unittest tests.test_weekly_events.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D066 | 5 | 完整性integrity失败即时告警 | [T403](task-403-alert-cli-workflows.md) | `python3 -W error::ResourceWarning -m unittest tests.test_alert_cli.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D067 | 5 | 独立评审否决或评审方自身失败，现有评论补标签并调用共用发布 | [T403](task-403-alert-cli-workflows.md) | `python3 -W error::ResourceWarning -m unittest tests.test_alert_cli.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D068 | 5 | 派发stall/timeout/打转/重试预算升级补trace与阶段链接 | [T205](task-205-dispatch-alerts.md) | `python3 -W error::ResourceWarning -m unittest tests.test_dispatch_alerts.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D069 | 5 | 只剩最后一轮CI预警在wait前，预算1也覆盖 | [T205](task-205-dispatch-alerts.md) | `python3 -W error::ResourceWarning -m unittest tests.test_dispatch_alerts.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D070 | 5 | 槽位每轮被拒工具达到rules阈值预警，理由数不等于调用数 | [T205](task-205-dispatch-alerts.md) | `python3 -W error::ResourceWarning -m unittest tests.test_dispatch_alerts.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D071 | 5 | route误差预算耗尽拒自动合并告警 | [T403](task-403-alert-cli-workflows.md) | `python3 -W error::ResourceWarning -m unittest tests.test_alert_cli.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D072 | 5 | audit环节缺失/链头锚点不符告警 | [T403](task-403-alert-cli-workflows.md) | `python3 -W error::ResourceWarning -m unittest tests.test_alert_cli.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D073 | 5 | 只用PR/议题评论+escalation，trace/reason远端标记幂等、失败不改原结果 | [T205](task-205-dispatch-alerts.md)、[T403](task-403-alert-cli-workflows.md) | `python3 -W error::ResourceWarning -m unittest tests.test_dispatch_alerts.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_alert_cli.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D074 | 5 | harness末尾/auto判定后always，失败CI也接通；可信main代码持最小write权限 | [T403](task-403-alert-cli-workflows.md) | `python3 -W error::ResourceWarning -m unittest tests.test_alert_cli.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D075 | 6/P1 | 事件三态逐字等价，禁项断言，链删改负例；消费方等价另见G2 | [T106](task-106-events-equivalence.md)、[T109](task-109-events-hardening.md) | `python3 -W error::ResourceWarning -m unittest tests.test_events_equivalence.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_events_hardening.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D076 | 6/P2 | 含禁项运行记录失败、旧格式仍可读；保留期检查 | [T203](task-203-run-record-privacy.md)、[T204](task-204-artifact-retention.md) | `python3 -W error::ResourceWarning -m unittest tests.test_run_record_privacy.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_artifact_retention.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D077 | 6/P3 | 真实任务从派发到合并与GitHub/账本一致（真实平台另见G3） | [T601](task-601-observability-e2e.md)、[T305](task-305-audit-ledger.md) | `python3 -W error::ResourceWarning -m unittest tests.test_observability_e2e.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests）；`python3 -W error::ResourceWarning -m unittest tests.test_audit_ledger.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D078 | 6/P4 | 人工制造缺评审/路由/记录、中删/尾删/锚点错与hash错，恢复后通过、告警幂等 | [T601](task-601-observability-e2e.md) | `python3 -W error::ResourceWarning -m unittest tests.test_observability_e2e.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D079 | 6/P5 | 事件小节与原周报口径对账，无库/不可得不报虚构0 | [T501](task-501-weekly-events.md) | `python3 -W error::ResourceWarning -m unittest tests.test_weekly_events.ObservabilityTaskTest -v`（任务书命令逐个点名方法，不接受0 tests） | 派发任务 |
| D080 | 6、7 | P1安全文档/事件配置，最终README命令/账本/平台、SECURITY、Migration、升级入口 | [T107](task-107-events-docs.md)、[T602](task-602-docs-migration.md) | T107人工对照 `p1_config`；T602人工对照 `final_migration_platform` | 设计方 |
| D081 | 1、7/8 | OTLP不在v0.2；无常驻进程；只用既有GitHub通知，不接入收件箱 | [T602](task-602-docs-migration.md) | T602人工对照 `final_security_limits` | 设计方 |
| D082 | 2.5、3.7 | 同用户不能阻止同时改库与未固定锚点；B31不实现；不监控槽位外操作 | [T602](task-602-docs-migration.md) | T602人工对照 `final_security_limits` | 设计方 |
| D083 | 3.7 | 需求/设计推理/会话、设计方放行与命令、平台设置变更不可采集 | [T602](task-602-docs-migration.md) | T602人工对照 `final_security_limits` | 设计方 |
| D084 | 4.2 | 设计方本机守卫拒绝不进账本，无原始流上传通道 | [T602](task-602-docs-migration.md) | T602人工对照 `final_security_limits` | 设计方 |
| D085 | 7/6、B52 | 已合并PR账本长期；被拦下PR仍仅本机，B52不扩范围 | [T602](task-602-docs-migration.md) | T602人工对照 `final_security_limits` | 设计方 |

## 反向追溯与依赖

剩余**24份任务书：22份可派发，T107/T602两份设计方文档任务**。K2/R0为T106/T601；其余K7/R3。所有接口以共用合同为准。依赖列表是最低合并前提；同文件必须沿下节次序串行。各期结束G1/G2未完成，不进入下一期派发。任务拆分文档PR不等于实现已经获准。

| 任务 | 内容 | 依赖已合并 | 对应需求 | 负责方 | 预算分钟 |
|---|---|---|---|---|---|
| [T102](task-102-events-verify.md) | 命令与 verify、integrity 埋点 | T101、T109 | D029、D030、D031 | 派发任务 | 120 |
| [T103](task-103-events-route.md) | 风险判级与路由埋点 | T109 | D010、D034、D040、D041、D042 | 派发任务 | 120 |
| [T104](task-104-events-guard.md) | 命令守卫与 git 守卫拒绝埋点 | T109 | D014、D048、D049 | 派发任务 | 120 |
| [T105](task-105-events-agents.md) | 派发全链与独立评审埋点 | T103、T104 | D012、D014、D019、D020、D021、D022、D023、D024、D025、D026、D027、D028、D043 | 派发任务 | 180 |
| [T106](task-106-events-equivalence.md) | 事件三态判定等价验收 | T105、T108 | D015、D075 | 派发任务 | 120 |
| [T107](task-107-events-docs.md) | P1 安全模型与事件配置文档收尾 | T106 | D080 | 设计方 | 60 |
| [T108](task-108-events-checks.md) | 其余检查的结构化输出埋点 | T109 | D018、D032、D033、D035、D036、D037、D038、D039 | 派发任务 | 180 |
| [T109](task-109-events-hardening.md) | 事件库加固与来源链隔离 | T101 |
| [T110](task-110-step-clean.md) | emit 写入值清洗口径修正（B63①） | T109 | T109 评审发现①（B63） | 派发任务 | 45 | D004、D007、D008、D010、D013、D015、D075 | 派发任务 | 150 |
| [T201](task-201-run-timeline.md) | 运行记录时间线与固定链头 | T107、T108 | D007、D016、D050 | 派发任务 | 120 |
| [T202](task-202-missing-context.md) | 执行方缺失上下文摘要 | T201 | D025、D051 | 派发任务 | 120 |
| [T203](task-203-run-record-privacy.md) | 运行记录内容检查（B38） | T201 | D037、D052、D076 | 派发任务 | 150 |
| [T204](task-204-artifact-retention.md) | 本机产物保留期与每日清理（B40） | T203 | D053、D054、D076 | 派发任务 | 120 |
| [T205](task-205-dispatch-alerts.md) | 派发预警与升级时间线 | T203 | D068、D069、D070、D073 | 派发任务 | 150 |
| [T301](task-301-events-io.md) | 事件导出与幂等导入 | T205 | D005、D006、D010、D012、D013、D017、D055 | 派发任务 | 180 |
| [T302](task-302-trace-events-cli.md) | events 与 trace 命令 | T301 | D003、D009、D059、D060、D061 | 派发任务 | 150 |
| [T303](task-303-ci-events.md) | 工作流事件 artifact 与 summary | T302 | D012、D016、D017、D056 | 派发任务 | 150 |
| [T304](task-304-github-events.md) | 合并、抽审与逃逸事实同步 | T303 | D018、D044、D045、D046 | 派发任务 | 120 |
| [T305](task-305-audit-ledger.md) | 合并账本与 PR 锚点 | T304 | D016、D057、D058、D077 | 派发任务 | 180 |
| [T401](task-401-audit-reconstruction.md) | 审计引用复原与哈希核对 | T305 | D006、D011、D062、D064 | 派发任务 | 150 |
| [T402](task-402-audit-completeness.md) | 审计完整性与锚点防篡改 | T401 | D002、D016、D017、D063、D064 | 派发任务 | 150 |
| [T403](task-403-alert-cli-workflows.md) | 告警汇总命令与工作流末尾步骤 | T402 | D047、D066、D067、D071、D072、D073、D074 | 派发任务 | 180 |
| [T501](task-501-weekly-events.md) | 周报事件汇总与原指标对账 | T403 | D065、D079 | 派发任务 | 120 |
| [T601](task-601-observability-e2e.md) | 可观测性全链与缺陷注入验收 | T501 | D001、D002、D077、D078 | 派发任务 | 180 |
| [T602](task-602-docs-migration.md) | 最终文档、平台步骤与迁移收尾 | T601 | D080、D081、D082、D083、D084、D085 | 设计方 | 90 |

## 合并顺序与共用接口拥有者

P1：T109 →（T102、T103、T104 三槽并行，T103/T104 已放宽为依赖 T109）→（T105、T108、T110 并行）→ T106 → T107 → G1/G2。
P2：T201 →（T202、T203 并行，T203 已放宽为依赖 T201）→（T204、T205 并行，T205 已放宽为依赖 T203）→ G1/G2。
P3：T301 → T302 → T303 → T304 → T305 → G1/G2/G4。
P4：T401 → T402 → T403 → G1/G2。
P5：T501 → G1/G2。P6：T601 → G3 → T602 → G1/G2（如本期引擎未改则G1核对无需重复upgrade）→ G5（发版时）。

最多三个槽位；并行对经设计方 2026-09-29 修订核定（白名单两两不交叉、不落在共用文件链上），其余链仍按依赖串行。每次合并后剩余分支更新main并重新满足ruleset；不为了用满槽位增加合并冲突。

| 共用文件/接口 | 定义方→后续修改/消费 | 串行与约束 |
|---|---|---|
| events.py/events_db.py公共API、v1存储、source | T101→T109→T204→T301（events_db） | 先加固再埋点；C2隔离不重写hash/seq |
| cli.py入口与注册 | T102→T302→T401→T403 | 守卫放行与只读查询按C1例外；新增入口必须支持安装布局 |
| dispatch.py记录/埋点/告警 | T105→T201→T202→T205 | 阶段摘要、旧字段与CI轮次不相互破坏 |
| dispatch_observation.py/run_timeline.py | T105定义观察适配，T201定义记录摘要组装 | dispatch.py仅接线，避免F1体量/复杂度棘轮，不上调质量基线 |
| dispatch_host.py流解析 | T105→T202→T205 | parse旧三元接口保留；parse_observability的字段一致 |
| run_check.py | T108→T203 | 原观察埋点与新B38数据判定分开，不能读事件判记录 |
| review.py | T105→T403 | 安全审计评论摘要供账本取回；后补告警不重跑评审 |
| alerts.py/publish、rules [alerts] | T205→T403 | C4标记/返回/阈值唯一，不各任务自建不同去重 |
| events_io.py/bundle/query/import | T301→T302（load_ci） | C5形状唯一，T303/304/305/401消费 |
| harness工作流模板 | T303→T304→T305→T403 | 仅模板；真实副本G1由设计方同步 |
| auto-merge工作流模板 | T303→T403 | 持写令牌的观察job只执行main代码 |
| checks.toml模板 | T204→T402 | events/artifact_days与audit键；T101已有enabled |
| ledger.py/JSON/ruleset模板 | T305→T401/T402消费 | C6只追加；用户G4应用平台ruleset |
| audit.py/report与audit配置 | T401→T402 | C7退出码/finding规则不与周报或告警另定义 |
| SECURITY/README/CHANGELOG | T107→T602（设计方） | 文档随实际行为发布，版本不由任务定 |

## 非派发工作与阶段门禁

| ID | 设计/计划需求 | 时机与负责方 | 可执行断言与完成证据 |
|---|---|---|---|
| G1 | 本仓库内置副本/配置/工作流同步 | 每期合并后由设计方；提交/推送/开PR先等用户授权 | 干净main起点按docs/upgrading.md做upgrade PR；engine.lock固定已合并提交，bin/harness integrity与bin/verify --full命令输出通过；模板对应真实.harness配置/.github工作流逐项diff，工作流job名/可信检出/90天/always一致。不在评审期间替换副本。P1只核引擎/配置，P3/P4才同步新工作流 |
| G2 | 消费方等价（设计6/P1） | 每个涉及判定/守卫/派发/配置的PR与每期升级，由设计方在Agent-Notification独立worktree做 | 从同一消费方基线采集升级前bin/verify --full的测试数/质量/变异；python3 <引擎仓库>/engine/cli.py upgrade --target <独立worktree> --allow-dirty，复跑bin/verify --full并对比相同项目指标；链校验与产生事件正例。历史556是上次基线，不在测试里硬编码来掩盖变化；不动其用户主目录 |
| G3 | 真实全链与GitHub一致（设计6/P3） | P5内置副本已同步后，使用用户授权派发的T601实际PR；设计方复核、用户批准合并 | 保存实际dispatch/CI/OpenCode评审/合并URL；trace <PR> --ci导入幂等、audit <PR>无适用error；从harness-audit取真实JSON逐字段对API的head/merge/approve/review与记录锚点，周报口径对账。在临时副本删评审/尾事件、改锚点后audit/alert能发现。夹具T601不能替代真实命令与API输出 |
| G4 | harness-audit分支与ruleset/权限平台应用 | P3模板与本仓库同步结果已可审后由用户设置；设计方只准备操作步骤 | 用户明确确认分支保护禁删/禁强推、写令牌权限/默认分支代码；只读平台核对实际设置与模板。真实账本writer成功且PR锚点评论可取回；普通覆盖不能靠ruleset防止的边界写SECURITY |
| G5 | 版本号与发布 | 用户在准备发版时决定，设计方仅按获准版本同步文档与version文件 | 未收到版本决定不改__version__/tag；发版前engine/__init__.py与CHANGELOG版本相同，正常项目检查与CI通过。此项为发布门禁，不因尚未发版延长B46功能实现完成状态 |

G1/G2/G3证据应是实际命令输出或平台资料，不能手写“通过”。本次整包拆分PR只准备这些合同，尚未执行任何G项。平台与发布分别由用户做，不转交Pi；G5不是无版本决定时关闭功能待办的阻塞项。

## 拆分审查状态

OpenCode v1评审已经完成，结论为**需修订**：无阻断、1严重、5一般、4建议。[评审原文及逐项处理](../review/2026-09-29-observability-split-review.md)保留v1结论；v2草稿已处理F1–F9，F10语义细化列在共用合同C8等待用户明确审定。第二轮已独立复核，结论可提交（待用户审定C8）；原v1结论不改写，第二轮原文与4条建议处理在同一评审文件保留。

v1的32份输入均按sha256冻结并保留在本机build快照；原始JSONL/报告/错误日志也保留。本地准入、链接、依赖及文件列直接核对见[结构核对证据](../review/2026-09-29-observability-split-structure-check.md)，包括B61补核对。第二轮复核已完成；整包与两轮原文/处理结果一并组成文档PR供用户审，C8仍未获审定。提交/推送/开PR各次授权仍分别等待用户。
