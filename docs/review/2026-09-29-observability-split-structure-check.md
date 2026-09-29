# B46 v2任务书结构核对证据

日期2026-09-29。设计方Codex执行；这是准入/结构核对，不是OpenCode独立评审结论，也不代替用户对文档内容的批准。输入为当前24份任务书，使用仓库taskbook.check_text、实际rules.toml和command_guard保护路径。

B61补核对：现有PATH_TOKEN不识别README.zh-CN.md根多点文件名。本表额外直接读取「步骤与提交顺序」表的「涉及文件」列中的全部反引号路径，与白名单逐字比较，未用PATH_TOKEN的结果冒充完整清单。根多点路径仍列在下面。头部分类/风险和架构声明另由真实准入代码检查。

| 任务 | 类别/风险/架构 | 负责 | 白名单＝步骤文件列 | 路径清单 | 准入错误 | 执行方受保护/已有测试命中 | 材料sha256 |
|---|---|---|---|---|---|---|---|
| T102 | K7/R3/True | 派发任务 | True | `engine/cli.py`、`engine/checks/verify.py`、`engine/checks/integrity.py`、`tests/test_events_verify.py` | 0 | 无 | `6474d3bd62847ad0c179051aed6b0fd35257ae9bba047e8b6cb9449b4f55bc4d` |
| T103 | K7/R3/True | 派发任务 | True | `engine/routing/policy.py`、`engine/routing/risk.py`、`tests/test_events_route.py` | 0 | 无 | `a7a682882e0f03a402a29384d6e8be78f8daeab00d6f07e9e308891e85b953a0` |
| T104 | K7/R3/True | 派发任务 | True | `engine/guards/command_guard.py`、`engine/guards/git_guard.py`、`tests/test_events_guard.py` | 0 | 无 | `b2d517c70fa094a691665527676f023c9d3c599a4d91384097f29322358be0a7` |
| T105 | K7/R3/True | 派发任务 | True | `engine/agents/review.py`、`engine/agents/dispatch.py`、`engine/agents/dispatch_host.py`、`engine/agents/dispatch_observation.py`、`tests/test_events_agents.py` | 0 | 无 | `ed860672b60f5320e2e38853065ef5ba66b85992560bf3a8415c92549c8bcbae` |
| T106 | K2/R0/False | 派发任务 | True | `tests/test_events_equivalence.py` | 0 | 无 | `21852c8b8b69fc024eaadf1bd8258f4777121070193b314e270a6b8db3dcda07` |
| T107 | K7/R3/True | 设计方 | True | `SECURITY.md`、`CHANGELOG.md`、`README.md`、`README.zh-CN.md` | 0 | 无 | `c427a19388c27e350d95a02c7c2d95f2ead71df2808f89143c333418a1f8d107` |
| T108 | K7/R3/True | 派发任务 | True | `engine/checks/hygiene.py`、`engine/checks/base_tests.py`、`engine/checks/mutate.py`、`engine/checks/evidence.py`、`engine/checks/quality.py`、`engine/checks/taskbook.py`、`engine/routing/run_check.py`、`engine/reports/metrics.py`、`tests/test_events_checks.py` | 0 | 无 | `f77a22c990a2202dd113c8a8f5637c40b026aace5490440a3f146da64ef65417` |
| T109 | K7/R3/True | 派发任务 | True | `engine/core/events.py`、`engine/core/events_db.py`、`tests/test_events_hardening.py` | 0 | 无 | `53398fb02ff4e45b736f6816fd298b70140778da0badd538eeb17599675cab9d` |
| T201 | K7/R3/True | 派发任务 | True | `engine/agents/dispatch.py`、`engine/agents/run_timeline.py`、`tests/test_run_timeline.py` | 0 | 无 | `987d3bbd65762ca011c0d0e26a27df8ce3075532ed0d0d84394dfbc95d9500a6` |
| T202 | K7/R3/True | 派发任务 | True | `engine/prompts/dispatch_prompt.md`、`engine/agents/dispatch.py`、`engine/agents/dispatch_host.py`、`tests/test_missing_context.py` | 0 | 无 | `c1f913d6e7427e6733248a92138cb07da406dfe0b8794ed12e68054b7794f29f` |
| T203 | K7/R3/True | 派发任务 | True | `engine/routing/run_check.py`、`tests/test_run_record_privacy.py` | 0 | 无 | `72986f695e7d0dc86799f46f3d28059ec0d9776710cc68c12d55071280b869a1` |
| T204 | K7/R3/True | 派发任务 | True | `engine/core/events.py`、`engine/core/events_db.py`、`templates/.harness/config/checks.toml`、`tests/test_artifact_retention.py` | 0 | 无 | `fb1cc06bb7ea399dd27242c60c353422afa42bfe6a3d0619bc1c960e4af3b097` |
| T205 | K7/R3/True | 派发任务 | True | `engine/agents/dispatch.py`、`engine/agents/dispatch_host.py`、`engine/core/alerts.py`、`templates/.harness/config/rules.toml`、`tests/test_dispatch_alerts.py` | 0 | 无 | `f10bed63283478e71d515b99fb94b0b21c10393c383e7b6338ebb883371b7eaa` |
| T301 | K7/R3/True | 派发任务 | True | `engine/core/events_io.py`、`engine/core/events_db.py`、`tests/test_events_io.py` | 0 | 无 | `cfd54b00c4204cd6e43df0dc9a720d17d64c2685cce49685adb337327bb859cf` |
| T302 | K7/R3/True | 派发任务 | True | `engine/cli.py`、`engine/core/events_io.py`、`engine/reports/trace.py`、`tests/test_trace_events_cli.py` | 0 | 无 | `fb14b0c599edf1350e1231932a59f6b6375a13a2d4125ef00e82c62214eccbe7` |
| T303 | K7/R3/True | 派发任务 | True | `engine/reports/ci_events.py`、`templates/.github/workflows/harness.yml`、`templates/.github/workflows/auto-merge.yml`、`tests/test_ci_events_workflows.py` | 0 | 无 | `89f5ebd2c9638642dc4693af369928fcf770547e7f80a700644029ba31cc9a4b` |
| T304 | K7/R3/True | 派发任务 | True | `engine/reports/github_events.py`、`templates/.github/workflows/harness.yml`、`tests/test_github_events.py` | 0 | 无 | `d5c8c6515c29d5b3810716efd68d51f6c111368ad80d6767a9bc46d6f7bfc2ac` |
| T305 | K7/R3/True | 派发任务 | True | `engine/reports/ledger.py`、`templates/.github/workflows/harness.yml`、`templates/.github/rulesets/harness-audit.json`、`tests/test_audit_ledger.py` | 0 | 无 | `5fb47f9f97be4fa0615d269ef3ad7def4f88bedff52aeb73cfb9db67e1993c76` |
| T401 | K7/R3/True | 派发任务 | True | `engine/cli.py`、`engine/reports/audit.py`、`tests/test_audit_reconstruction.py` | 0 | 无 | `3c45042965eef2e4363834fea6e302ea997820e15673ce6155ff4c87817b23d0` |
| T402 | K7/R3/True | 派发任务 | True | `engine/reports/audit.py`、`templates/.harness/config/checks.toml`、`tests/test_audit_completeness.py` | 0 | 无 | `172e979cf8a3ad1161bc7095432024dcf68c3ae0607015965cb64b6cb6192fc2` |
| T403 | K7/R3/True | 派发任务 | True | `engine/cli.py`、`engine/core/alerts.py`、`engine/agents/review.py`、`templates/.github/workflows/harness.yml`、`templates/.github/workflows/auto-merge.yml`、`tests/test_alert_cli.py` | 0 | 无 | `ac98a1d9ce031e0e2069289577c77fcc55bba520aa50d846e3b66bb278db185f` |
| T501 | K7/R3/True | 派发任务 | True | `engine/reports/weekly.py`、`tests/test_weekly_events.py` | 0 | 无 | `4bb4490dc94babfc5ef2345f05a4bdf023984074d912aca1bf710007d7333964` |
| T601 | K2/R0/False | 派发任务 | True | `tests/test_observability_e2e.py` | 0 | 无 | `18788a5077f52acf55bac90643cf05e802101d7b30fb38bb3f1b85b4ed4d7e54` |
| T602 | K7/R3/True | 设计方 | True | `README.md`、`README.zh-CN.md`、`SECURITY.md`、`CHANGELOG.md`、`docs/upgrading.md`、`AGENTS.md` | 0 | 无 | `e305265aa71a8568cc50079cc0e2e3c8716df8f8a1af0d785768809b412aeddc` |

结果：24份，22可派发、2设计方；文件列/白名单/头部交叉核对异常 0。设计方任务按独立文档合同执行，不通过派发器授予写权限。新增测试文件尚未实现，测试不存在不能当行为验收通过。
