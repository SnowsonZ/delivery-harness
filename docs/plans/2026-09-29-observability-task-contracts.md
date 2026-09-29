# B46 拆分共用合同

状态：**OpenCode第二轮可提交（待用户明确审定C8）；4条非阻断建议已澄清**。设计方：Codex。依据：[已审定设计](2026-09-29-observability-design.md)、[执行计划](2026-09-29-observability-execution-plan.md)、[任务拆分流程](../task-splitting.md)。本合同细化实现接口，未修改已审定设计；来源链隔离、配置默认值、格式细化随整包任务书请求审定，当前不得实施或派发。

## C0 通用边界与完成标准

- Python ≥3.11，仅标准库。事件与引用准备是观察旁路：emit、span、file_ref、store_artifact、锚点准备失败不能改原有判定、业务异常、返回码或 GitHub 调用序列。span 重抛块内原异常，观察异常不得遮盖它。
- T101 公共接口按已合并代码使用：emit 支持 source 并返回事件 id 或 None，store_artifact、set_anchor、chain_head、verify_chain 均已提供；SQLite v1、四表与列、busy_timeout 为 5000。连接须关闭，不照旧执行计划的 2000ms 重写。
- T109 在前，公共签名与四表不变。stage 为 guard/verify/dispatch/ci/route/review/merge/alert，status 为 ok/fail/skip/deny/error，不新增枚举。
- 元数据仅引用、哈希、枚举、计数、布尔、时长；字符串沿用 T101 长度/路径/换行过滤，丢弃而非截断，记录 redacted。outputs 扁平，详细列表拆事件。原始日志/Pi 流仅存本机；CI/账本只发布安全结构化 JSON。
- 三态等价分别比较 stdout、业务 stderr、返回码。只排除 T101 固定的一次写入失败提示和指定耗时字段，另断言提示/时长，不能过滤所有 stderr 或任意差异。
- 执行方只改任务白名单，不改真实 .github/、.harness/、Agent 配置、已有测试或依赖/ruff配置。templates 下的配置与工作流是可编辑安装模板。任务书只读，运行记录由 dispatcher 写。
- 新测试在隔离项目中调用真实产品入口并核对事件/产物；mock emit 返回后只检查调用不算验收。定向变异由设计方在源码副本中做，不动已有测试。
- 每份实现完成 bin/verify --full；涉及判定/守卫/派发/配置的改动还由设计方做 G2 消费方等价验证。T107/T602 为设计方文档任务，T106/T601 为 K2/R0，其余 K7/R3。
- 不做常驻进程、OTLP、新通知渠道、平台变更采集、槽位外监控、B31 凭据隔离、B52 被拦下PR账本、B47 默认分支/宿主适配、B37 自动修复评审、B41 用量采集对账；不顺手处理其他待办。
- 本轮只写工作区；提交、推送、开PR、派发、合并无隐含授权。独立评审由 OpenCode。任务书合并、依赖完成、用户下令才可派发。

## C1 埋点目录与引用约定

拥有者为 T102（入口/verify/integrity）、T103（risk/route）、T104（guard）、T105（agents）、T108（其余检查）。目录按设计3.1–3.6逐项覆盖，追溯表列出每行承担任务与测试。

入口阶段：verify/integrity→verify；risk/policy→route；dispatch→dispatch；review/review-pack→review；其余已有检查/metrics/weekly/install/upgrade/identity→ci。guard-command/guard-git 排除通用 cli 事件，由 T104 仅拒绝记录，遵守设计3.6放行不逐条记。events/trace 查询不追加自身事件；audit/alert 自己记汇总或发布结果，避免递归。

decision 对有判定结果的事件填 by/rule/reason，理由不合法时留规则与安全结构化原因引用、记录过滤，不能因此缺失判定依据。列表逐文件/规则/目标/缺陷/指标记录，不能把嵌套结构交给过滤器吞掉。

inputs 为原始引用列表；输出引用放在扁平 outputs 中用 log.sha256/log.size/log.ref 等三元字段。查询/账本由此派生输出引用视图，不虚构 T101 没有的 output_refs 参数，也不把派生视图加进哈希。refs 的 out 存储方向暂不成为调用方新接口，规范化哈希仍沿用 v1。

## C2 trace 与来源链（T109 定义）

分支名是 trace，attempt/head 是关联字段；设计方 PR 无派发也有同类视图。workflow_run 从 --branch/可信 PR API 取分支，不用检出的 main，也不读 PR 正文的 ID。

T101 的 source 是自由字符串，链键为 (source, trace_id)。Actions 三键齐全时 default_source 为 ci:<GITHUB_RUN_ID>:<GITHUB_RUN_ATTEMPT>:<GITHUB_JOB>；缺键的旧 CI 保留 ci，local 保留 local。显式 source 不改，actor.host 与显示分类仍为 ci。可导入真实CI包必须带三键与匹配source；旧未隔离包冲突时报告，不重算hash/改source补救。

GitHub事实source为 github:<PR号>:<规范化事实sha256>，获取时间不进入事实摘要；同快照幂等，更新快照另起链。source标签不代替仓库/run/head/工作流真实性核对，PR任意artifact不能冒充默认分支可信CI。本补充不改API、四表、hash算法或trace定义；避免多个临时库同trace从seq=1开始发生冲突。另一台机器只导入CI/账本，本机原始流不建上传通道。

## C3 运行记录（T201/T202 定义，T203 检查）

旧字段全部保留；新增字段可选。trace_id为task.branch；stages为已发生事件的安全摘要列表，每项含stage/step/status/ts/duration_ms/attempt/round/inputs/outputs/decision/actor/source/head_hash；anchors项含source/stage/head_hash/fixed_in（run_record）。链头为各阶段结束的前缀，不能伪造尚未发生的push/CI步骤；引用补path@固定提交，不自引用尚不存在的记录commit。

missing_context是列表，每项category为context/tool/spec/other，含安全summary/ref；summary限定context_unavailable/required_tool_unavailable/spec_unavailable/spec_ambiguous/other_missing，与对应category一致。任意正文/未知ID标invalid且不入记录，不由T202放行、再由T203误拒。missing_context_status为reported/unknown/invalid。合法空报告为reported与空列表，无标记/坏JSON不能冒充无缺失。PiHost.parse_observability只读取最后assistant完成消息的固定尾标记，不读取工具输出伪造报告；旧parse三元返回不变。

T203检查记录而非提示词正文。prompt_path合法相对引用与sha256可入记录，提示词文件不作为事件内容发布。允许的顶层键是现有write_record字段与本节字段/诊断；数字/枚举/哈希/相对引用/安全短规则摘要按类型校验，所有字符串和键递归检查禁项，未知自由文本字段拒绝。旧guard_denials安全规则摘要仍可通过。

命令/会话正负夹具至少覆盖单行git/python命令、管道/多行脚本、User:/Assistant:及长会话；Finding只写字段位置与规则、不回显禁值。短文本也可能是命令，不能以长度判安全。旧记录缺新字段仍通过原判定；历史审计标legacy/覆盖不足，不能豁免新版本真实缺环节。

T203的最小可执行判定集（F5，作为待用户审定的B38边界）：先对所有键/值拒绝本机路径、换行、超长与非法类型，再按已知字段校验。数字/布尔/枚举按类型；hash为64位hex，SHA为合法git对象名，ID/模型/版本/分支允许其字段规定的短token，不接受任意自然语言；ref只允许仓库相对路径/带提交路径、受支持GitHub URL或规定ID。guard_denials键只有与可信默认分支内置guard规则理由集合完全相等的旧摘要例外（从已批准代码的COMMAND_RULES/TOOL_RULES及implementer规则静态取得，不执行PR代码）；添加一个字符或追加命令不再是例外。该例外仍不能含路径/换行。

| ID | 值/位置 | 明确判定 |
|---|---|---|
| C01 | 自由摘要或未知字段里的 git status、python -m unittest、gh pr view 1 | 命令前缀命中则拒绝，不以短文本放行 |
| C02 | 自由文本以sh/bash/zsh/python/python3/node/npm/npx/pip/curl/wget/ssh/scp/make/rm/ls/cat开头并带参数，或裸shell可执行命令放在摘要字段 | 拒绝；模型/工具ID字段按字段语法判断，不能把合法tool=python当命令正文 |
| C03 | 自由文本带管道、双连接符、分号、重定向、命令替换或反引号 | 拒绝shell结构；已验证URL与精确内置规则理由按自己的字段策略处理，不给自由文本例外 |
| C04 | User:/Assistant:/System:/Human:或用户:/助手:标记，不论顶层/嵌套/键 | 拒绝会话正文；英文标记不区分大小写 |
| C05 | 多行脚本/会话、本机Unix或Windows路径 | 先按通用禁项拒绝，任何字段例外都不得越过 |
| C06 | 看似guard理由但不在可信集合，或在可信理由后追加正文 | 拒绝未知自由文本，不采用starts-with/contains例外 |
| C07 | 新字段或summary塞入无标记的任意自然语言正文 | 默认拒绝；summary用规定的类别/原因短ID，不由启发式猜“是否会话” |
| A01 | branch=task/201-run-timeline、model=provider/model、host_version=0.85.1 | 按对应短ID语法通过，不把它们当自由正文 |
| A02 | prompt_path=docs/runs/task-x/1.prompt.md、合法sha256与GitHub引用 | 按引用/哈希语法通过，拒绝绝对路径与路径穿越 |
| A03 | 输入引用kind=pattern、ref=engine/**，或规则ID中合法通配模式 | 仅规定pattern字段可通过；不是任意summary自由文本 |
| A04 | 内置规则中提及git命令或连接符的原理由，作为guard_denials键 | 精确可信集合通过；计数仍必须非负整数 |
| A05 | missing_context.category=tool、summary=required_tool_unavailable、ref=python | 按规定类别/原因/工具ID通过；不保存执行方原会话句子 |

这是字段类型与最低禁例合同，不宣称能理解任意文本的真实含义。未知摘要需设计方定义新短ID并走原审批，不能执行方自选宽泛正则。旧合法结构的兼容与上述正负例同时验收，不能只用T101空列表记录证明兼容。

运行记录stages[].decision.reason的固定词表（第二轮建议S2）：C0状态ok/fail/skip/deny/error、既有Attempt退出ok/timeout/stall/stopped/error/loop/retries/clarify，以及C4的稳定reason键。词表去重，生产方T201和消费方T203均按此表；普通成功/失败直接用ok/fail，具体依据由decision.rule、outputs数值和原理由引用表达，不自行造同义短token。新增结果词必须先经设计方修订本表。

## C4 告警 API（T205 定义，T403 扩展）

engine/core/alerts.py 的 publish(trace_id, reason, *, pr=None, task=None, details=None, gh=None) 返回 {ok, target, updated, error_kind}。只消费结构化既有状态，发布异常不传播到原判定，不实现合并/批准。

稳定reason键为 integrity_failure/review_rejected/review_error/dispatch_stall/dispatch_timeout/dispatch_loop/dispatch_budget/ci_last_round/guard_denials/budget_exhausted/audit_missing_stage/audit_anchor_mismatch。重试用尽/澄清保留原升级原因。远端标记取trace_id、NUL、reason的sha256；同键查分页评论后更新，无PR查/建一条升级议题，带escalation标签。details只含安全阶段、trace查询方式与PR/CI/账本链接。

rules.toml可选[alerts] guard_denials_threshold=3，为每轮被拒工具数，达到触发；非法/非正配置提示并禁用这项预警，不改原预算。最后CI轮次预警在wait前，预算1首轮即预警；升级与预警复用API。同键换机后仍靠远端标记去重，本机缓存不是权威。

## C5 查询、导入与 CI JSON（T301 定义，T302 加下载）

engine/core/events_io.py 提供以下接口，调用方不能自行换名：

```python
def query(*, trace_id=None, source=None, since=None, stage=None, status=None) -> list[dict]: ...
def export_bundle(*, trace_id=None, source=None) -> dict: ...
def import_bundle(bundle: dict) -> dict: ...
# {imported: int, skipped: int, findings: list[dict]}；坏包整体不写。
def write_bundle(bundle: dict, target: Path) -> None: ...
# T302在同模块实现下列下载适配，T301只定义合同、不留成功的假实现。
def load_ci(pr: int, *, head=None, gh=None) -> dict: ...
# {imported, skipped, findings}；分页、来源核对后调用import_bundle。
```

EventBundle v1含 schema_version、origin、events、anchors、artifacts、chains、findings。origin含repository/run_id/run_attempt/job/workflow_ref/head_sha/head_branch；local导出不伪造Actions键。chains各项为source/trace_id/head_hash；artifacts各项为sha256/size/file，file为安全相对文件名。

每event含source/trace_id/seq/prev_hash/hash/ts、设计JSON的inputs/outputs/decision/error/actor/engine_version/redacted。id是本机定位号，不入hash，导入可重映射。duration_ms以及所有T101列必须完整恢复，actor/decision/error恢复列，outputs恢复原紧凑JSON，optional缺省与None沿用原normalize规则。派生输出引用视图不加入canonical内容。

导出完整链前缀；筛选展示不冒充可导入包。按hash去重；同source/trace/seq不同hash为冲突，不能覆盖或重新emit。事务插入events/refs/anchors；坏hash/断链/隐私禁项/未来schema拒绝、整体不写。保持本机SQLite v1四表，已有schema测试不改。

安全内容产物是过滤后的规范化UTF-8 JSON，文件名为sha256；manifest列可发布文件及哈希/大小，逐字段扫描事件/manifest/内容。可含安全规则/数值/枚举，不含原始命令输出、verify日志片段或Pi流；不上传SQLite/WAL/SHM。逻辑artifact名harness-events，物理名harness-events-<run_id>-<run_attempt>-<job>，保留90天。下载必须核对API的仓库、workflow路径/固定提交、run/head与包origin，不能只信包自报ci。

query source参数可为精确链或local/ci/github类型（匹配前缀），时间统一UTC。不存在库为空且上层明确无数据；查询故障交观察命令诊断，不能影响原判定。import/write_bundle产物错误要报告、不当成功。load_ci取harness与route的多个job/attempt，过期/缺失/坏包明确findings。

## C6 合并账本（T305 定义，T401/T402 消费）

engine/reports/ledger.py提供build_ledger(pr:int, *, gh=None, cwd=ROOT)->dict、publish_ledger(ledger:dict, *, gh=None)->dict。受信任main工作流调用，代码入口无需cli新注册。

T105生产、T305消费的评审摘要固定标记为 `<!-- harness-review-audit <单行JSON> -->`，并列放在现有independent-review标记旁。旧标记及reviewed_heads判定不改。JSON v1必含schema_version=1、trace_id、head（完整SHA）、base（实际材料比较基点）、reviewer、model、model_basis、designer、implementers（宿主ID列表）、independent（角色可判定时布尔、否则null）、same_host、parsed、verdict、duration_ms、severity_counts（阻断/严重/一般/建议/unknown计数）、materials。每个materials项含kind/ref/sha256/size/encoding/recipe，kind至少task/diff/ci。无任务书材料明确使用none哨兵，不伪造路径；model_basis为reported/explicit_request/unknown：模型明确报告优先，实际命令显式指定的请求模型可记录但标explicit_request，两者皆无则model=null，不把未使用的配置默认值当已调用模型。材料哈希是评审实际读取文件的UTF-8字节哈希，不能按将来复取对象任意重算。

recipe采用固定短ID与参数，不存命令正文：task_v1从路径@head读取（无任务时为现有“无”哨兵字节）；diff_v1复用现有write_materials生成规则（base...head、no-color、去结尾换行后最多200000字符再UTF-8编码）；ci_v1为当时按API顺序的name/state/link列表渲染成现有ci_summary字节。CI列表是安全枚举/URL快照，可随摘要保存，不含检查日志。额外pr_v1材料按当时标题/描述渲染的原文件哈希，后续正文变化报snapshot_changed而不假装历史内容可复取。已有材料截断行为不由T105改变，摘要声明recipe与截断界限；T401复取使用同一规则，原始git文件/评论则仍按真正raw_bytes比对，不能全局去换行。

duration不含发布后未知时长；摘要不包含自己整条评论的哈希或尚未知的评论URL，避免自引用。评论真正发布后，review事件再保存实际URL与完整评论字节hash。T305按此标记/schema/head/材料字段解析，未知版本、缺字段、错误head列missing，不猜正文。T105与T305的夹具都用同形状，字段变更只能由设计方修订合同。

review-pack也属于实际评审输入（第二轮建议S1），materials必须包含kind=pack，不能只记录其余四类。pack_v1记录实际pack.md原字节的sha256/size，encoding=raw_bytes、ref为本机内容寻址产物sha256；recipe含id=pack_v1、engine_ref（生成时引擎固定提交）、artifact_sha256、retention_days=30。T105转存该原始产物；不执行PR引用的代码来“复算”pack，不上传原文。T305长期账本保留引用/哈希和本机来源；T401能取回时核对，清理到期后明确reference_expired，不能据此声称原产物已核验。此留存边界与其他本机原始产物一致，不新增外发渠道。

账本含schema_version/repository/pr/trace_id/head_sha/merged_at/merge_sha/class/risk/approval、stages/chains/anchors/references/sources/missing。stage为C3摘要或C5原始CI/GitHub事件；本机摘要标evidence_kind=run_record_summary，没有原始链不能宣称原始链已核验。review从实际评论安全审计摘要取材料哈希、角色/模型/发现/结论与head，不上传原始评审流。

references各项kind/ref/sha256/size/encoding。git文件/评论/产物按raw_bytes原字节哈希（包括结尾换行）；GitHub事实按canonical_json规范化选定字段（键排序、紧凑JSON、UTF-8），另带字段清单。复取用同编码，不拿PR正文哈希比全API对象。可变评论/PR字段变动标snapshot_changed，与固定绑定提交的事实区分。

从记录、CI/route artifact、评审评论与API收集各attempt/head；采用合并head的最终可信判定，之前失败尝试保留历史而非作为当前失败。等待关联route产物完成后固定账本；不足写missing不捏造。写harness-audit/<合并UTC年份>/<PR号>.json，仅已合并PR（未合并关闭为B52）。不能依赖本机库，不能上传原始流；后登记escape以新的API快照查询，不覆写既存账本。

writer正常fast-forward追加，禁强推/删除；竞争fetch后有限重试，同字节幂等、不同字节冲突报告。PR锚点稳定标记harness-audit:<PR>，含分支/path/commit/文件sha256及source/trace链头。ruleset禁删除/强推只保护历史，不阻止普通修改旧文件；writer自查与锚点核对发现覆盖，不能宣称ruleset本身保证只追加。

## C7 审计、配置与周报（T401/T402/T501）

inspect_pr(pr:int, *, gh=None, cwd=ROOT)->dict报告pr/trace_id/head_sha/stages/references/findings/coverage/ok。finding有rule/severity/source/stage/ref/reason，severity为error/warning，reason不回显私密内容。可执行audit报告退出0（适用项可核对且通过）/1（发现）/2（参数/整体API故障/配置错误）。缺数据不当完好；legacy范围明确、warning不等于完成新版本验收。

稳定rule包括missing_review/missing_route/missing_run_record/missing_anchor/reviewer_not_independent/approval_actor/approval_head/chain_invalid/anchor_mismatch/ledger_mismatch/hash_mismatch/reference_unavailable/reference_expired/snapshot_changed/configuration_error。历史v0.1缺可观测性标legacy/覆盖不足，但真实R2缺独立评审仍是发现；用记录/引擎版本定适用范围，不硬编码PR号。none批准模式沿现有平台配置，不凭空要求App。

checks.toml可选[audit] require_review_risk=2（0..3）、require_route_for_auto=true、require_run_record_for_task=true、verify_anchors=true。缺省按设计，非法配置明确configuration_error/退出2，不默默放宽；只影响audit报告，不进入原policy/guard/verify判定。

每个锚点核对对应前缀而非最终链头；合法追加不误报，删尾虽内部链合法仍能由固定锚点发现。账本/运行层按head/attempt匹配，来源标local/ci/github并附资料与真实workflow/run，不能只信actor。审计写安全ci/audit.summary及audit.finding观察事件供周报，不新增audit阶段枚举。

周报UTC周窗口/hash去重，仅附新小节，现有来源与小节不变。执行方拒绝总数取executor计数、设计方拒绝另列，guard细条与executor不重复相加；同PR/head审计取最新结果/唯一规则计数。无库、读取失败、覆盖不足写明，不伪造0。

## C8 评审后明确的用户决策项

OpenCode v1结论为需修订，发现F10要求将语义细化单独列出。以下未获得用户审定，不因这份修订或现有设计批准而默认生效：

| ID | 推荐草案 | 影响与改选条件 |
|---|---|---|
| U1 | CI按run/attempt/job各成一条source子链，同trace由查询关联，各链与前缀锚点分别核验 | 解决临时库seq冲突，不能宣称“整个trace只有一条连续链”；若必须跨job单链，需要跨job状态传递/存储设计，需重拆P3，不能由执行方自行拼链 |
| U2 | 输出引用从扁平outputs三元字段派生，保留T101四表/API/hash | 无schema迁移；若必须物理refs.direction=out为公共契约，需另审输出引用API/迁移与既有测试保护 |
| U3 | C3结构化短ID及最小命令/会话禁例表，未知自由文本拒绝 | 命令正文识别有明确边界，不能宣传理解所有自然语言；如希望保留自由会话摘要，必须重新审隐私规则，不能放宽本合同 |
| U4 | 每轮guard_denials_threshold缺省3，最后CI轮次在wait前预警；去重由远端标记 | “突增”定义为轮内绝对阈值，不隐含统计历史基线；用户可选其他正整数，需同步任务书/配置说明 |
| U5 | C5/C6导出与评审/账本格式、C7audit配置/legacy范围按本稿 | 长期摘要与原始链可信度分开；用户非默认audit规则在报告中明确显示覆盖范围，不能默默宣称按默认完整规则通过 |

本稿只准备决策，代码/配置尚未实现。用户确认后如有改选，由设计方更新追溯/任务书并请OpenCode复核；提交、推送、开PR、派发仍逐次等待指令。
