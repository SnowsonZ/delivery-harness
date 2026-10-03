"""T701 合同制合并路由测试：R2 候选判定、PR 评论里评审/复核标记的读取与防混入、head 内容相同判定、
同家评审降级强制审计、pending 输出与模板工作流的等待分支、合同模式改从配置读取。

夹具全部使用匿名临时 git 仓库与假 gh（参考 tests/test_events_route.py 的 fake_gh 写法），经 policy.main
（与命令行同一入口）驱动；事件固定关闭，不碰真实库、PR 或工作流。既有测试零修改。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.checks import r1_checks
from engine.core import common
from engine.routing import policy
from tests.test_ci_events_workflows import parse_workflow

ENGINE_REPO = Path(__file__).resolve().parents[1]
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
LOGIN = "agent-bot"
HEAD_REF = "task/901-contract"
RULES_TOML = """\
[hygiene]
forbidden = ["build/**", "**/.DS_Store"]
allowed = []
max_file_kb = 1024
secret_patterns = ['XSECRETKEY-[A-Za-z0-9]{20,}']
home_path_pattern = '/Users/(?!placeholder/)'

[risk]
r3 = [".github/**", "critical/**"]
r2 = ["engine/**", "AGENTS.md", "docs/specs/**"]
r0 = ["docs/plans/**", "docs/runs/**", "README*.md", "CHANGELOG.md"]
tests = ["tests/**"]
contracts = ["docs/backlog.md"]
taskbooks = ["docs/plans/task-*.md"]
shrink_only = []
golden = []

[taskbook]
guard_paths = ["critical/**"]
architecture_paths = []

[taskbook.modules]

[guard]
protected_branches = ["main"]
implementer_protected = []
"""
# 同一份 rules，仅 [risk] contracts 移除 docs/backlog.md：证明 K0 判定来自配置而不是写死的路径。
RULES_NO_CONTRACTS = RULES_TOML.replace('contracts = ["docs/backlog.md"]\n', "contracts = []\n")
CHECKS_TOML = """\
[sources]
python_dirs = ["engine"]
python_glob = "*.py"
code = ["engine/**"]
ui = []

[identity]
agent_login = "agent-bot"
"""
TASKBOOK = """\
---
task: T901
class: K5
risk: R2
designer: claude-code
size: small
architecture: false
spec_refs: []
no_spec_reason: 测试夹具
budget:
  wall_clock_min: 10
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert
---

## 目标终态

夹具任务书（T701 合同制路由测试）。

## 验收

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B81 | 夹具 | 单测 | `tests.test_contract_route` | 断言失败 |
"""
AUTONOMY = {
    "size": {"max_lines": 400, "exclude": ["docs/runs/**"]},
    "classes": {"K5": {"name": "功能与规格实现", "level": "L4", "window": 20, "max_escapes": 1}},
    "contract_route": {"allowed": ["engine/**", "tests/**", "docs/runs/**", "CHANGELOG.md"]},
}
ALLOWED = AUTONOMY["contract_route"]["allowed"]
PROMPT = "为 T901 准备的提示词\n"


def fake_gh(comments=None, labels=(), issues=()):
    """gh 桩：labels/escapes/CI 运行按固定形状应答；候选 PR 的 comments 按参数应答。

    comments 为 None 时，任何 comments 调用都是断言失败——非候选 PR 不得多出这次调用。
    """

    def gh(*args):
        joined = " ".join(args)
        if "pr view" in joined and "comments" in joined:
            if comments is None:
                raise AssertionError(f"未预期的 comments 调用（非候选 PR）：{joined}")
            return json.dumps({"comments": comments})
        if joined.startswith("pr view") and "labels" in joined:
            return json.dumps({"labels": [{"name": name} for name in labels]})
        if joined.startswith("pr list"):
            return json.dumps([{"number": 101}])
        if joined.startswith("issue list"):
            return json.dumps(list(issues))
        if joined.startswith("run list"):
            return json.dumps([])
        raise AssertionError(f"未预期的 gh 调用：{joined}")

    return gh


def review_marker(head: str, *, verdict="通过", flagged=False) -> str:
    data = {"verdict": verdict, "reviewer": "opencode", "model": "codex 默认模型 max", "head": head,
            "findings": 0, "flagged": flagged, "parsed": True}
    return f"<!-- independent-review {json.dumps(data, ensure_ascii=False)} -->"


def signoff_marker(head: str, *, verdict="通过", mutations=2, caught=2) -> str:
    data = {"verdict": verdict, "head": head, "designer": "claude-code", "mutations": mutations, "caught": caught}
    return f"<!-- designer-signoff {json.dumps(data, ensure_ascii=False)} -->"


def review_comment(head: str, *, audit=None, embed=None, login=LOGIN, **kw) -> dict:
    """评审评论：audit 为 C6 审计标记（dict）；embed 为夹在正文里的额外标记（防混入用例）。"""
    lines = ["### 独立评审（试行）：通过", "", "**结论**：没有发现。", ""]
    if embed:
        lines += [embed, ""]
    lines.append(review_marker(head, **kw))
    if audit is not None:
        lines.append(f"<!-- harness-review-audit {json.dumps(audit, ensure_ascii=False)} -->")
    return {"author": {"login": login}, "body": "\n".join(lines) + "\n"}


def signoff_comment(head: str, *, trailing=None, login=LOGIN, **kw) -> dict:
    """复核评论：trailing 非空时标记后面还有内容（破坏「最后一个非空行」规则）。"""
    lines = ["### 设计方复核", "", signoff_marker(head, **kw)]
    if trailing:
        lines.append(trailing)
    return {"author": {"login": login}, "body": "\n".join(lines) + "\n"}


class ContractRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-contract-route-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        for rel in (".harness/config", "docs/plans", "docs/runs/task-901-contract", "engine/routing", "tests"):
            (self.repo / rel).mkdir(parents=True)
        (self.repo / ".harness/config/rules.toml").write_text(RULES_TOML, encoding="utf-8")
        (self.repo / ".harness/config/rules-no-contracts.toml").write_text(RULES_NO_CONTRACTS, encoding="utf-8")
        (self.repo / ".harness/config/checks.toml").write_text(CHECKS_TOML, encoding="utf-8")
        (self.repo / "docs/plans/task-901-contract.md").write_text(TASKBOOK, encoding="utf-8")
        (self.repo / "engine/routing/app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "CHANGELOG.md").write_text("# log\n", encoding="utf-8")
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        self._seq = 0

    # ---- 夹具操作 ----

    def git(self, *args, check=True):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "HARNESS_"))}
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=check)

    def commit(self, files: dict[str, str], message: str) -> str:
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def record(self, model) -> str:
        """任务 PR 的实现提交 + 运行记录，返回 head；model 为 gen_ai.request.model。"""
        self._pr = getattr(self, "_pr", 0) + 1
        record = {
            "task": "T901", "class": "K5", "attempt": 1, "branch": HEAD_REF,
            "gen_ai.agent.name": "pi", "host_version": "0.85.1", "gen_ai.request.model": model,
            "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
            "prompt_path": "docs/runs/task-901-contract/1.prompt.md",
            "guard_ref": "main@" + self.base[:7],
            "started_at": "2026-01-02T03:04:05Z", "ended_at": "2026-01-02T03:14:05Z",
            "exit": "ok", "retries": 0, "failure_signatures": [], "guard_denials": {},
        }
        return self.commit({
            "engine/routing/app.py": f"VALUE = {1 + self._pr}\n",
            "tests/test_feature.py": "def test_feature():\n    assert True\n",
            "CHANGELOG.md": "# log\n\n- feature\n",
            "docs/runs/task-901-contract/1.prompt.md": PROMPT,
            "docs/runs/task-901-contract/1.json": json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        }, "实现\n\nTask: T901")

    def run_policy(self, head: str, gh, *, autonomy=None, rules=".harness/config/rules.toml",
                   pr=9, branch=HEAD_REF, base=None) -> dict:
        """驱动 policy.main（与命令行同一入口）：注入夹具配置、cwd 与 gh 桩，捕获输出文件与规则快照。"""
        self._seq += 1
        captured = []
        orig_gather, orig_decide = policy.gather, policy.decide

        def gather(*args, **kwargs):
            kwargs.setdefault("cwd", self.repo)
            kwargs.setdefault("gh", gh)
            return orig_gather(*args, **kwargs)

        def decide(facts, auto):
            rules_ = orig_decide(facts, auto)
            captured.append([(rule.name, rule.ok, rule.reason) for rule in rules_])
            return rules_

        output = self.tmp / f"github-output-{self._seq}.txt"
        with mock.patch.object(r1_checks, "load_autonomy", return_value=autonomy or AUTONOMY), \
                mock.patch.object(common, "RULES_PATH", self.repo / rules), \
                mock.patch.object(common, "CONFIG_DIR", self.repo / ".harness/config"), \
                mock.patch.object(policy, "gather", gather), \
                mock.patch.object(policy, "decide", decide), \
                mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output), "HARNESS_EVENTS": "off"}), \
                contextlib.redirect_stdout(io.StringIO()) as fake_out, \
                contextlib.redirect_stderr(io.StringIO()) as fake_err:
            code = policy.main(["--base", base or self.base, "--head", head, "--pr", str(pr),
                                "--branch", branch, "--github"])
        self.assertEqual(code, 0)
        self.assertTrue(captured, "decide 未被调用")
        return {
            "outputs": dict(line.split("=", 1) for line in output.read_text().splitlines()),
            "rules": captured[0],
            "risk_reason": captured[0][0][2],
            "stdout": fake_out.getvalue(),
            "stderr": fake_err.getvalue(),
        }

    # ---- 验收 1：R2 候选带齐两个标记 → 自动合并 ----

    def test_r2_contract_route_merges(self):
        head = self.record("glm-5.3")
        comments = [
            review_comment(head, audit={"model": "gpt-6.1-sol", "model_basis": "reported"}),
            signoff_comment(head),
        ]
        result = self.run_policy(head, fake_gh(comments=comments))
        self.assertEqual(result["outputs"]["auto_merge"], "true")
        self.assertEqual(result["outputs"]["pending"], "")
        self.assertIn("合同制路径", result["risk_reason"])
        self.assertIn("独立评审 ok，设计方复核 ok", result["risk_reason"])
        self.assertEqual([name for name, _, _ in result["rules"]],
                         ["风险", "类别", "预算", "规模", "运行记录"])
        self.assertNotIn("同家评审", result["risk_reason"])

    # ---- 验收 2：pending 只在「其余规则全过、风险仅缺标记」时输出 ----

    def test_pending_signals(self):
        head = self.record("glm-5.3")
        review = review_comment(head, audit={"model": "gpt-6.1-sol", "model_basis": "reported"})

        only_review = self.run_policy(head, fake_gh(comments=[review]))
        self.assertEqual(only_review["outputs"]["auto_merge"], "false")
        self.assertEqual(only_review["outputs"]["pending"], "signoff")
        self.assertIn("等待：设计方复核", only_review["stdout"])

        neither = self.run_policy(head, fake_gh(comments=[]))
        self.assertEqual(neither["outputs"]["pending"], "review")
        self.assertIn("等待：独立评审", neither["stdout"])

        # 缺标记的同时超出规模 → pending 为空，照常请求所有者评审
        small = {**AUTONOMY, "size": {"max_lines": 1, "exclude": ["docs/runs/**"]}}
        over_size = self.run_policy(head, fake_gh(comments=[review]), autonomy=small)
        self.assertEqual(over_size["outputs"]["pending"], "")
        self.assertNotIn("等待：", over_size["stdout"])

        # 缺标记的同时带上 budget-exceeded 标签 → pending 为空
        budget = self.run_policy(head, fake_gh(comments=[review], labels=("budget-exceeded",)))
        self.assertEqual(budget["outputs"]["pending"], "")

        # 评审 fail → pending 为空（例外由人决定，不能被「等待中」遮住）
        failed = self.run_policy(head, fake_gh(comments=[
            review_comment(head, verdict="不通过"),
            signoff_comment(head),
        ]))
        self.assertEqual(failed["outputs"]["pending"], "")
        self.assertNotIn("等待：", failed["stdout"])

    # ---- 验收 3：其他账号写的标记一律忽略 ----

    def test_foreign_author_ignored(self):
        head = self.record("glm-5.3")
        comments = [
            review_comment(head, login="someone-else", audit={"model": "gpt-6.1-sol"}),
            signoff_comment(head, login="someone-else"),
        ]
        result = self.run_policy(head, fake_gh(comments=comments))
        self.assertEqual(result["outputs"]["auto_merge"], "false")
        self.assertIn("独立评审 missing", result["risk_reason"])
        self.assertIn("设计方复核 missing", result["risk_reason"])
        self.assertEqual(result["outputs"]["pending"], "review")

    # ---- 验收 4：防混入——夹带标记的评论整条作废 ----

    def test_embedded_marker_rejected(self):
        head = self.record("glm-5.3")
        # 评审正文里夹带复核标记：两类标记合计出现两次 → 整条评论忽略，复核仍是 missing
        embedded = self.run_policy(head, fake_gh(comments=[
            review_comment(head, embed=signoff_marker(head), audit={"model": "gpt-6.1-sol"}),
        ]))
        self.assertIn("独立评审 missing", embedded["risk_reason"])
        self.assertIn("设计方复核 missing", embedded["risk_reason"])
        self.assertEqual(embedded["outputs"]["auto_merge"], "false")

        # 评审正文里夹带第二个评审标记 → 同样整条忽略
        twice = self.run_policy(head, fake_gh(comments=[
            review_comment(head, embed=review_marker(head), audit={"model": "gpt-6.1-sol"}),
        ]))
        self.assertIn("独立评审 missing", twice["risk_reason"])

        # 复核标记不在最后一个非空行 → 整条忽略
        trailing = self.run_policy(head, fake_gh(comments=[
            review_comment(head, audit={"model": "gpt-6.1-sol"}),
            signoff_comment(head, trailing="（补充说明）"),
        ]))
        self.assertIn("设计方复核 missing", trailing["risk_reason"])
        self.assertEqual(trailing["outputs"]["pending"], "signoff")

    # ---- 验收 5：评审不通过 / flagged / 复核变异未全抓住 → 一律不放行 ----

    def test_failed_signals_block(self):
        head = self.record("glm-5.3")
        cases = {
            "verdict": [review_comment(head, verdict="不通过"), signoff_comment(head)],
            "flagged": [review_comment(head, flagged=True), signoff_comment(head)],
            "caught": [review_comment(head, audit={"model": "gpt-6.1-sol"}),
                       signoff_comment(head, mutations=2, caught=1)],
            "zero": [review_comment(head, audit={"model": "gpt-6.1-sol"}),
                     signoff_comment(head, mutations=0, caught=0)],
            "signoff_verdict": [review_comment(head, audit={"model": "gpt-6.1-sol"}),
                                signoff_comment(head, verdict="不通过")],
        }
        for label, comments in cases.items():
            with self.subTest(label=label):
                result = self.run_policy(head, fake_gh(comments=comments))
                self.assertEqual(result["outputs"]["auto_merge"], "false", label)
                self.assertEqual(result["outputs"]["pending"], "", label)
                self.assertFalse(result["rules"][0][1], label)

    # ---- 验收 6：head 内容相同判定（同步 main 有效；内容或缩进变化失效） ----

    def test_same_content_after_main_sync(self):
        self.git("checkout", "-q", "-b", HEAD_REF)
        reviewed = self.record("glm-5.3")
        self.git("checkout", "-q", "main")
        self.commit({"docs/other.md": "# other\n"}, "main 前进")
        main_tip = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("checkout", "-q", HEAD_REF)
        self.git("merge", "-q", "--no-edit", "main")
        synced = self.git("rev-parse", "HEAD").stdout.strip()

        comments = [
            review_comment(reviewed, audit={"model": "gpt-6.1-sol", "model_basis": "reported"}),
            signoff_comment(reviewed),
        ]
        # 只多了合并 main 的提交，diff 字节相同 → 旧标记仍有效
        merged = self.run_policy(synced, fake_gh(comments=comments), base=main_tip)
        self.assertEqual(merged["outputs"]["auto_merge"], "true")

        # 多了内容提交 → 旧标记失效
        content_head = self.commit({"engine/routing/app.py": "VALUE = 3\n"}, "内容变化\n\nTask: T901")
        stale = self.run_policy(content_head, fake_gh(comments=comments), base=main_tip)
        self.assertEqual(stale["outputs"]["auto_merge"], "false")

        # 只改了缩进的提交同样必须失效（patch-id 会忽略空白，这里字节比较不忽略）
        indent_head = self.commit(
            {"tests/test_feature.py": "def test_feature():\n        assert True\n"},
            "缩进变化\n\nTask: T901")
        indented = self.run_policy(indent_head, fake_gh(comments=comments), base=main_tip)
        self.assertEqual(indented["outputs"]["auto_merge"], "false")

    # ---- 验收 7：同家评审降级——合并照常，audit 强制开启 ----

    def test_same_family_review_degrades_to_audit(self):
        head = self.record("glm-5.3")
        cases = [
            # 审计标记 model 与执行模型同家（glm）→ 降级：audit=true，理由注明
            ({"model": "zai-coding-plan/glm-5.3", "model_basis": "reported"}, "true", True),
            # 不同家（gpt vs glm）→ 不降级：audit 按原来的抽样（audit_every 未命中）
            ({"model": "gpt-6.1-sol", "model_basis": "reported"}, "false", False),
            # 缺审计标记 → 评审模型未知 → 降级
            (None, "true", True),
            # 审计标记 model 为 null 或 model_basis=unknown → 降级（不取展示串「codex 默认模型 max」）
            ({"model": None, "model_basis": "explicit_request"}, "true", True),
            ({"model": "gpt-6.1-sol", "model_basis": "unknown"}, "true", True),
        ]
        for audit, expected_audit, degraded in cases:
            with self.subTest(audit=audit):
                comments = [review_comment(head, audit=audit), signoff_comment(head)]
                result = self.run_policy(head, fake_gh(comments=comments))
                self.assertEqual(result["outputs"]["auto_merge"], "true")
                self.assertEqual(result["outputs"]["audit"], expected_audit)
                self.assertEqual("同家评审（降级）" in result["risk_reason"], degraded)

        # 执行侧读不到模型（没有运行记录可读）→ 任一方为 None 即降级
        headless = [review_comment(self.base, audit={"model": "gpt-6.1-sol", "model_basis": "reported"})]
        self.assertTrue(policy._same_family_review(headless, LOGIN, self.base, self.base, self.repo))

    # ---- 验收 8：白名单外的路径不是候选；缺省配置零候选 ----

    def test_paths_outside_allowlist_not_candidate(self):
        additions = {
            "taskbook": {"docs/plans/task-902-other.md": TASKBOOK.replace("T901", "T902")},
            "agents": {"AGENTS.md": "# 指令\n"},
            "spec": {"docs/specs/x.md": "# 规格\n"},
        }
        for label, extra in additions.items():
            with self.subTest(label=label):
                head = self.record("glm-5.3")
                head = self.commit(extra, "越界\n\nTask: T901")
                comments = [
                    review_comment(head, audit={"model": "gpt-6.1-sol", "model_basis": "reported"}),
                    signoff_comment(head),
                ]
                result = self.run_policy(head, fake_gh(comments=comments))
                self.assertEqual(result["outputs"]["auto_merge"], "false", label)
                self.assertEqual(result["outputs"]["pending"], "", label)
                self.assertNotIn("合同制路径", result["risk_reason"], label)

        # [contract_route] 缺失：纯 engine/ 的 PR 也不是候选（安全缺省），且不多读评论
        head = self.record("glm-5.3")
        no_route = {key: value for key, value in AUTONOMY.items() if key != "contract_route"}
        result = self.run_policy(head, fake_gh(comments=None), autonomy=no_route)
        self.assertEqual(result["outputs"]["auto_merge"], "false")
        self.assertEqual(result["outputs"]["pending"], "")

    # ---- 验收 9：R3 与非候选的判定逐字不变、gh 调用序列不变 ----

    def test_non_candidates_unchanged(self):
        # 非候选 R2（allowed 为空）不调用评论接口：桩遇到 comments 调用即抛错，判定与理由逐字不变
        r2_head = self.record("glm-5.3")
        empty_allowed = {**AUTONOMY, "contract_route": {"allowed": []}}
        quiet = self.run_policy(r2_head, fake_gh(comments=None), autonomy=empty_allowed)
        self.assertEqual(quiet["outputs"]["auto_merge"], "false")
        self.assertEqual(quiet["risk_reason"], "R2：评审方评审 + 用户看证据包后合并")
        self.assertEqual([name for name, _, _ in quiet["rules"]],
                         ["风险", "类别", "预算", "规模", "运行记录"])

        # 任务书随本 PR 修改（in_pr）→ 「合同已批」不成立：即使白名单放行了任务书路径也不入候选
        in_pr_head = self.commit(
            {"docs/plans/task-901-contract.md": TASKBOOK + "## 追记\n\n随 PR 修改。\n"},
            "改任务书\n\nTask: T901")
        wide = {**AUTONOMY, "contract_route": {"allowed": [*ALLOWED, "docs/plans/**"]}}
        in_pr = self.run_policy(in_pr_head, fake_gh(comments=[
            review_comment(in_pr_head, audit={"model": "gpt-6.1-sol", "model_basis": "reported"}),
            signoff_comment(in_pr_head),
        ]), autonomy=wide)
        self.assertEqual(in_pr["outputs"]["auto_merge"], "false")
        self.assertEqual(in_pr["outputs"]["pending"], "")
        self.assertNotIn("合同制路径", in_pr["risk_reason"])

        # 类别不是 L4（K5 降为 L3）→ 不是候选：不读评论（桩遇 comments 调用即抛错），转用户评审
        l3 = {**AUTONOMY, "classes": {"K5": {**AUTONOMY["classes"]["K5"], "level": "L3"}}}
        l3_result = self.run_policy(r2_head, fake_gh(comments=None), autonomy=l3)
        self.assertEqual(l3_result["outputs"]["auto_merge"], "false")
        self.assertEqual(l3_result["outputs"]["pending"], "")

        # R3 PR：即使带齐两个标记也必须转用户评审，理由文字与现状相同
        head = self.commit({".github/workflows/x.yml": "name: x\n"}, "护栏\n\nTask: T901")
        comments = [
            review_comment(head, audit={"model": "gpt-6.1-sol", "model_basis": "reported"}),
            signoff_comment(head),
        ]
        result = self.run_policy(head, fake_gh(comments=comments))
        self.assertEqual(result["outputs"]["auto_merge"], "false")
        self.assertEqual(result["outputs"]["risk"], "R3")
        self.assertEqual(result["risk_reason"], "R3：必须由用户批准后合并")
        self.assertEqual([name for name, _, _ in result["rules"]],
                         ["风险", "类别", "预算", "规模", "运行记录"])

    # ---- 验收 10：合同模式（K0）来自 rules.toml 配置 ----

    def test_contract_patterns_from_config(self):
        head = self.commit({"docs/backlog.md": "- [ ] 新待办\n"}, "待办")
        with_contract = self.run_policy(head, fake_gh(), branch="chore/backlog")
        self.assertEqual(with_contract["outputs"]["class"], "K0")

        without = self.run_policy(head, fake_gh(), branch="chore/backlog",
                                  rules=".harness/config/rules-no-contracts.toml")
        self.assertEqual(without["outputs"]["class"], "K5")

    # ---- 验收 11：模板 request-review 在 pending 期间不请求所有者评审 ----

    def test_workflow_skips_owner_request_while_pending(self):
        workflow = parse_workflow((ENGINE_REPO / "templates/.github/workflows/auto-merge.yml")
                                  .read_text(encoding="utf-8"))
        judge = workflow["jobs"]["judge"]
        self.assertEqual(judge["outputs"]["pending"], "${{ steps.policy.outputs.pending }}")
        review = workflow["jobs"]["request-review"]
        self.assertEqual(review["steps"][-1]["env"]["PENDING"], "${{ needs.judge.outputs.pending }}")
        script = review["steps"][-1]["run"]
        # 打标签不受 pending 影响；请求所有者评审只在 pending 为空时执行
        self.assertLess(script.index('--add-label "class:$CLASS"'), script.index('if [ -z "$PENDING" ]'))
        self.assertLess(script.index('if [ -z "$PENDING" ]'), script.index('--add-reviewer "$OWNER"'))


if __name__ == "__main__":
    unittest.main()
