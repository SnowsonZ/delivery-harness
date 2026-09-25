"""事故回放集：把 v0.8.0 的缺陷重新注入产品代码，对应检查必须失败。

每条注入用例：在 file 中把恰好出现一次的 find 换成 replace（模拟缺陷回来了），然后运行 tests，
期望失败。find 找不到或不唯一时用例算「过期」，必须随代码一起维护。

过程类失败（历史改写、声称已修等）不适合源码注入，由 GUARDED 中列出的守卫测试覆盖；
确实无法自动回放的列在 DEFERRED 并写明原因，保持可见。
基线编号见 docs/review/2026-09-25-harness-baseline.md。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    defect: str
    title: str
    file: str
    find: str
    replace: str
    tests: tuple[str, ...]  # Python 测试 id（在 tests/ 下运行）；"swift-policy" 表示 Swift 策略测试

    @property
    def platform(self) -> str:
        return "macos" if "swift-policy" in self.tests else "any"


CASES: list[Case] = [
    Case(
        "V080-R2",
        "重扫合计变小时直接采用重扫结果（过去日被降级）",
        "scripts/daily_report.py",
        "        if old is None or _task_total(old) <= _task_total(record):",
        "        if True:",
        ("test_properties.MergeNoDowngradeProperties", "test_daily_report"),
    ),
    Case(
        "V080-R2",
        "只在现有报告中的任务不再恢复",
        "scripts/daily_report.py",
        "    for key, old in base_map.items():\n        if key in rescan_map:",
        "    for key, old in []:\n        if key in rescan_map:",
        ("test_properties.MergeNoDowngradeProperties", "test_daily_report"),
    ),
    Case(
        "V080-R3",
        "判定为 agent 的会话被恢复",
        "scripts/daily_report.py",
        "        if key in excluded_sessions:\n            continue  # 判定为 agent：正当排除，不恢复。",
        "        pass",
        ("test_properties.MergeNoDowngradeProperties", "test_daily_report"),
    ),
    Case(
        "V080-R4",
        "单价不变时丢掉 history",
        "scripts/pricing_fetch.py",
        '                    prices["history"] = old["history"]',
        "                    pass",
        ("test_properties.PricingTransformProperties", "test_pricing_fetch"),
    ),
    Case(
        "V080-R4",
        "10 倍跳变防护变成删除条目",
        "scripts/pricing_fetch.py",
        "            if isinstance(old, dict):\n                entries[name] = json.loads(json.dumps(old))",
        "            if False:\n                entries[name] = json.loads(json.dumps(old))",
        ("test_properties.PricingTransformProperties", "test_pricing_fetch"),
    ),
    Case(
        "V080-R19",
        "非法单价时删除条目而不是保留上一版",
        "scripts/pricing_fetch.py",
        "                entries[key_name] = json.loads(json.dumps(old))",
        "                pass",
        ("test_properties.PricingTransformProperties", "test_pricing_fetch"),
    ),
    Case(
        "V080-R13",
        "价格不一致的别名不再丢弃（命中取决于顺序）",
        "scripts/pricing_fetch.py",
        "        if consistent:\n            continue",
        "        if True:\n            continue",
        ("test_properties.PricingTransformProperties", "test_pricing_fetch"),
    ),
    Case(
        "V080-R11",
        "维度金额用锚点日期计价",
        "scripts/usage_report.py",
        "                single, _, _ = cost_for_models({key: entry}, day, tables)",
        "                single, _, _ = cost_for_models({key: entry}, anchor, tables)",
        ("test_properties.PeriodCostProperties", "test_usage_report", "test_architecture"),
    ),
    Case(
        "V080-R11",
        "合计与逐日金额用锚点日期计价",
        "scripts/usage_report.py",
        "        day_cost, day_unpriced, _notes = cost_for_models(public_day, day, tables)",
        "        day_cost, day_unpriced, _notes = cost_for_models(public_day, anchor, tables)",
        ("test_properties.PeriodCostProperties", "test_usage_report", "test_architecture"),
    ),
    Case(
        "V080-R11",
        "上期金额用锚点日期计价",
        "scripts/usage_report.py",
        "        day_cost, _, _ = cost_for_models(_public_models(day_models), day, tables)",
        "        day_cost, _, _ = cost_for_models(_public_models(day_models), anchor, tables)",
        ("test_architecture",),
    ),
    Case(
        "V080-R12",
        "补录过去日时不带 agent 统计",
        "scripts/usage_report.py",
        "                store, home, min(missing), max(missing), agent_stats=agent_stats",
        "                store, home, min(missing), max(missing)",
        ("test_usage_report",),
    ),
    Case(
        "V080-R17",
        "分位数前没有排序",
        "scripts/daily_report.py",
        "    ordered = sorted(amounts)",
        "    ordered = list(amounts)",
        ("test_properties.CostThresholdProperties", "test_daily_report"),
    ),
    Case(
        "H0925-1",
        "generate_overview 绕过 cost_thresholds 自行取分位（R17 的回归测试曾守不住）",
        "scripts/daily_report.py",
        "    thresholds = cost_thresholds(amounts)",
        "    thresholds = [amounts[int(len(amounts) * q)] for q in (0.5, 0.75, 0.9)] if len(amounts) >= 8 else [0.0] * 3",
        ("test_architecture",),
    ),
    Case(
        "V080-R8",
        "进行中条目要求未读（恒为 0）",
        "native/InboxPolicy.swift",
        '    origin != "agent" && inboxActiveListed(state: state, openAvailable: openAvailable)',
        '    origin != "agent" && inboxActiveListed(state: state, openAvailable: openAvailable) && false',
        ("swift-policy",),
    ),
    Case(
        "H0925-2",
        "写入器绕过 widgetRunningListed 自行过滤（V080-R8）",
        "native/WidgetSnapshotWriter.swift",
        "            widgetRunningListed(origin: $0.origin, state: $0.state, openAvailable: $0.openAvailable)",
        "            inboxNotifyEligible(origin: $0.origin, unread: $0.unread)",
        ("test_architecture",),
    ),
    Case(
        "V080-R15",
        "hide_titles 同时清掉项目名",
        "native/InboxPolicy.swift",
        "    WidgetEntryText(title: widgetEntryTitle(hideTitles: hideTitles, title: title), project: project)",
        '    WidgetEntryText(title: widgetEntryTitle(hideTitles: hideTitles, title: title), project: hideTitles ? "" : project)',
        ("swift-policy",),
    ),
    Case(
        "H0925-2",
        "写入器内联 hideTitles 判断（V080-R15）",
        "native/WidgetSnapshotWriter.swift",
        "                project: text.project,\n                state: row.state, at: max(row.activityAt ?? 0, row.eventAt)\n            )\n        }",
        '                project: hideTitles ? "" : row.project,\n                state: row.state, at: max(row.activityAt ?? 0, row.eventAt)\n            )\n        }',
        ("test_architecture",),
    ),
    Case(
        "H0925-5",
        "证据检查只认 git trailer（Defect 与 Co-Authored-By 隔空行即被忽略）",
        "harness/common.py",
        '    message = git("log", "-1", "--format=%B", sha, cwd=cwd)',
        '    message = git("log", "-1", "--format=%(trailers:only,unfold)", sha, cwd=cwd)',
        (
            "test_harness.EvidenceTest.test_defect_before_co_author_paragraph_is_recognized",
            "test_harness.RiskTest.test_r1_claim_before_co_author_paragraph_is_recognized",
        ),
    ),
    # ---- 评审 PR7（docs/review/2026-09-26-harness-review.md）发现的 harness 缺陷 ----
    Case(
        "PR7-R1",
        "doc 类修复改了代码也不查",
        "harness/evidence.py",
        "            if evidence.code_files:\n                # doc 类修复免于测试核对",
        "            if False:\n                # doc 类修复免于测试核对",
        ("test_harness.EvidenceTest.test_doc_defect_that_changes_code_is_rejected",),
    ),
    Case(
        "PR7-R2",
        "已有测试不按 base 版本运行（追加的 monkeypatch 生效）",
        "harness/base_tests.py",
        '            git("checkout", base, "--", "tests", cwd=worktree, isolate=True)',
        "            pass",
        ("test_harness.BaseTestsTest",),
    ),
    Case(
        "PR7-R3",
        "git 守卫读工作区的规则文件",
        "harness/git_guard.py",
        "    if shown.returncode != 0:\n        return load_rules()",
        "    if True:\n        return load_rules()",
        ("test_harness_guard.TamperedRulesTest",),
    ),
    Case(
        "PR7-R4",
        "修复前以出错结束只记警告",
        "harness/evidence.py",
        '            evidence.problems.append(\n                f"修复退回后测试以出错而非断言失败结束',
        '            evidence.warnings.append(\n                f"修复退回后测试以出错而非断言失败结束',
        ("test_harness.EvidenceTest.test_error_before_fix_is_not_proof",),
    ),
    Case(
        "PR7-R5",
        "整文件退回，连带撤掉后续无关提交",
        "harness/evidence.py",
        "        if not _reverse_apply_fix(evidence, worktree, cwd):",
        "        if True:",
        ("test_harness.EvidenceTest.test_revert_only_the_fix_not_later_commits",),
    ),
    Case(
        "PR7-R6",
        "不检查去掉引号的命令形式（拆写的覆盖变量）",
        "harness/command_guard.py",
        '    unquoted = re.sub(r"[\\"\'\\\\]", "", normalized)',
        "    unquoted = normalized",
        ("test_harness_guard.CommandGuardTest.test_remote_writes_and_split_override_variables",),
    ),
    Case(
        "PR7-R6",
        "只拦 GitHub API 的 DELETE",
        "harness/command_guard.py",
        'r"(?i)\\bgh\\s+api\\b[^;&|]*\\s(-X\\s*|--method[\\s=]+)(POST|PUT|PATCH|DELETE)\\b"',
        'r"(?i)\\bgh\\s+api\\b[^;&|]*\\s(-X\\s*|--method[\\s=]+)(DELETE)\\b"',
        ("test_harness_guard.CommandGuardTest.test_remote_writes_and_split_override_variables",),
    ),
    Case(
        "PR7-R7",
        "base_tests 不核对测试框架是否被产品代码篡改",
        "harness/base_tests_runner.py",
        "    if changed or caught != 3:",
        "    if False:",
        ("test_harness.BaseTestsTest.test_product_code_cannot_disable_assertions_on_import",),
    ),
    Case(
        "PR7-R7",
        "有意改动测试时，框架被篡改也只报告",
        "harness/base_tests.py",
        "    return completed.returncode == 0, not intended or completed.returncode == TAMPERED, summary",
        "    return completed.returncode == 0, not intended, summary",
        ("test_harness.BaseTestsTest.test_tampering_is_enforced_even_when_tests_changed_on_purpose",),
    ),
    Case(
        "PR7-R7",
        "产品代码引用测试框架不产生标记，仍可声明 R1",
        "harness/risk.py",
        "    if touching:",
        "    if False:",
        ("test_harness.RiskTest.test_product_code_touching_test_framework_is_flagged_and_not_r1",),
    ),
    Case(
        "PR7-R9",
        "curl 写请求只在 URL 之后出现写方法时被拦",
        "harness/command_guard.py",
        'r"\\bcurl\\b(?=[^;&|]*api\\.github\\.com)"',
        'r"\\bcurl\\b[^;&|]*api\\.github\\.com[^;&|]*"',
        ("test_harness_guard.CommandGuardTest.test_curl_writes_in_any_argument_order",),
    ),
    Case(
        "H0926-3",
        "verify 的检查子进程继承钩子注入的 GIT_DIR（linked worktree 中指向真实仓库）",
        "harness/verify.py",
        '                env=clean_git_env({"PYTHONDONTWRITEBYTECODE": "1"}),',
        '                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},',
        ("test_harness.VerifyTest.test_checks_do_not_inherit_hook_git_dir",),
    ),
    Case(
        "H0926-2",
        "执行方守卫把缺口清单也一并禁改，规范要求的删行做不了",
        "harness/command_guard.py",
        "    if pattern and relative not in IMPLEMENTER_EDITABLE:",
        "    if pattern:",
        ("test_harness_guard.CommandGuardTest.test_implementer_may_shrink_gap_list_but_not_edit_other_verifiers",),
    ),
    Case(
        "V080-R10",
        "通知路径处理后 URL 仍留在队列（被重放）",
        "native/InboxPolicy.swift",
        "        pending.removeAll { $0 == url }",
        "        _ = url",
        ("swift-policy",),
    ),
]

# 过程类失败：由守卫测试覆盖（测试必须存在，回放时逐个运行并要求通过）。
GUARDED: dict[str, tuple[str, ...]] = {
    "V080-R1": (
        "test_harness.HygieneTest.test_staged_snapshot_is_rejected",
        "test_harness_guard.GitGuardTest.test_staged_snapshot_is_rejected_by_pre_commit",
    ),
    "V080-X1": ("test_harness.EvidenceTest.test_claimed_fix_without_code_change",),
    "V080-X3": (
        "test_harness_guard.GitGuardTest.test_filter_repo_cannot_rewrite_main_or_tags",
        "test_harness_guard.GitGuardTest.test_amend_on_main_is_rejected",
        "test_harness_guard.GitGuardTest.test_moving_or_deleting_tag_is_rejected",
        "test_harness_guard.PrePushTest.test_push_tag_is_rejected",
    ),
    "V080-X4": ("test_harness.EvidenceTest.test_test_that_does_not_detect_the_bug",),
    "V080-X6": ("test_harness_guard.PrePushTest.test_force_push_is_rejected",),
}

# 暂不能自动回放的基线条目：原因必须写明，回放报告中始终列出。
DEFERRED: dict[str, str] = {
    "V080-R5": "CI 编译命令缺文件：已由结构消除（verify 与 CI 共用同一条 Swift 编译命令）",
    "V080-R6": "Zcode 逐请求对账：需要本机真实数据回放（unknown 占比阈值），数据不入库",
    "V080-R7": "热力金额阈值来源：需要经 generate_overview 的夹具（依赖当天日期与来源扫描），待补",
    "V080-R9": "今日 token 未传入写入器：需要快照端到端测试（macOS），待补",
    "V080-R14": "组件条目逐条 Link：界面行为，真机 UI 验收",
    "V080-R16": "plist 值与命名：由 macOS CI「Assert widget extension」每次运行覆盖",
    "V080-R18": "CI 断言写错：断言本身随主 workflow 每次运行",
    "V080-X2": "自述与事实不符：流程约定（PR 模板、harness job summary 生成证据），无法由测试证明",
    "V080-X5": "无预算的试错：预算写在 docs/templates/task.md，超限升级；暂无自动计数",
}

BASELINE = [f"V080-R{number}" for number in range(1, 20)] + [f"V080-X{number}" for number in range(1, 7)]
