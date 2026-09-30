"""T304 GitHub 事实同步测试：假 gh 三种合并（人批准/App 批准/无批准）的全字段事实、抽样与合并后新
escape 的不可变快照（旧事实保留、新事实另起新链）、分页与重复同步幂等（读 API 次数不影响事件哈希）、
API 失败明确报告事实缺失且只读（不推送/合并/批准，工作流步骤 continue-on-error 隔离），以及修订二轮
修复的单亲「标题 (#N)」提交判 squash（真 rebase 不误判、squash 形式 push 标题解析出 PR 号）。

夹具沿用 tests/test_trace_events_cli.py 的模式：匿名临时 git 仓库、隔离 events_db.ROOT、递增冻结时钟、
假 gh 桩（可配置失败与页容量，记录全部调用）；另经真实 cli install 安装布局与 PATH 上的假 gh 可执行
脚本调用产品入口。不碰真实库/PR/工作流，不执行 PR 代码，不调用真 gh。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.core import events, events_db, events_io
from engine.reports import github_events

ENGINE_REPO = Path(__file__).resolve().parents[1]
CLI = ENGINE_REPO / "engine" / "cli.py"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
CLEAN_PREFIXES = ("GIT_", "GITHUB_", "GH_", "HARNESS_", "CI")
REPO = "owner/repo"
TS = "2026-01-02T03:04:05Z"
TS2 = "2026-01-02T09:05:06Z"
HUMAN_HEAD, HUMAN_MERGE = "a1" * 20, "a2" * 20
APP_HEAD, APP_MERGE = "b1" * 20, "b2" * 20
NONE_HEAD, NONE_MERGE = "c1" * 20, "c2" * 20
UNK_HEAD, UNK_MERGE = "d1" * 20, "d2" * 20
PAGE_HEAD, PAGE_MERGE = "e1" * 20, "e2" * 20
OTHER_HEAD, OTHER_MERGE = "f1" * 20, "f2" * 20
# 只读路由白名单：事实同步允许出现的全部 API 路径（出现其他路径即是在做合并/批准等动作）。
READ_ROUTES = re.compile(r"repos/[^/]+/[^/]+/(pulls/\d+(?:/reviews|/commits)?|commits/[0-9a-f]+|issues)\Z")


def canonical(data: dict) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class _Clock:
    """递增冻结时钟：从 2026-01-02T03:01 起每次调用前进一分钟，事件 ts 严格递增且可比。"""

    def __init__(self):
        self.step = 0

    def __call__(self) -> str:
        self.step += 1
        hour, minute = divmod(self.step, 60)
        return f"2026-01-02T{3 + hour // 24:02d}:{(hour % 24 + 1):02d}:{minute:02d}.000Z"


class FakeGh:
    """github_events 用 gh 桩：PR/评审/提交/议题只读 API，记录全部调用序列。

    page_size 模拟更小的服务端页容量（迫使实现真实分页）；fail 里的子串命中即抛 RuntimeError
    （模拟无权限/短暂失败）。
    """

    def __init__(self, *, repo=REPO, pulls=None, reviews=None, pr_commits=None, commits=None,
                 audit=None, escape=None, page_size=None, fail=()):
        self.calls: list[str] = []
        self._repo = repo
        self.pulls = pulls or {}
        self.reviews = reviews or {}
        self.pr_commits = pr_commits or {}
        self.commits = commits or {}
        self.issues = {"audit": list(audit or []), "escape": list(escape or [])}
        self.page_size = page_size
        self.fail = tuple(fail)

    def repo(self) -> str:
        self.calls.append("repo")
        return self._repo

    def api(self, route: str):
        self.calls.append(route)
        for pattern in self.fail:
            if pattern in route:
                raise RuntimeError("HTTP 403: 权限不足（夹具）")
        path, _, query = route.partition("?")
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\d+)", path):
            return self.pulls[int(match[1])]
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\d+)/reviews", path):
            return self._page(self.reviews.get(int(match[1]), []), query)
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\d+)/commits", path):
            return self._page(self.pr_commits.get(int(match[1]), []), query)
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/commits/([0-9a-f]+)", path):
            return self.commits[match[1]]
        if path.endswith("/issues"):
            label = urllib.parse.parse_qs(query).get("labels", [""])[0]
            return self._page(self.issues.get(label, []), query)
        raise AssertionError(f"FakeGh 未配置的路由：{path}")

    def _page(self, items: list, query: str) -> list:
        params = urllib.parse.parse_qs(query)
        page = int(params.get("page", ["1"])[0])
        cap = min(self.page_size or int(params.get("per_page", ["30"])[0]),
                  int(params.get("per_page", ["30"])[0]))
        start = (page - 1) * cap
        return items[start:start + cap]


class ObservabilityTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-github-events-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.fresh_repo("app")
        root_patch = mock.patch.object(events_db, "ROOT", self.repo)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        clock = mock.patch.object(events_db, "_now", new=_Clock())
        clock.start()
        self.addCleanup(clock.stop)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in [name for name in os.environ if name.startswith(CLEAN_PREFIXES)]:
            os.environ.pop(key, None)
        events._warned = False
        self.addCleanup(setattr, events, "_warned", False)

    # ---- 夹具 ----

    def fresh_repo(self, name: str) -> Path:
        path = self.tmp / name
        path.mkdir()
        env = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, **GIT_ENV}
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True, capture_output=True, env=env)
        (path / "README.md").write_text("# fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True, capture_output=True, env=env)
        return path

    @staticmethod
    def merged_pr(number: int, branch: str, *, head: str, merge_sha: str, merged_by: dict,
                  labels: tuple[str, ...] = (), merged_at: str = TS) -> dict:
        return {"number": number, "state": "closed", "merged": True, "merged_at": merged_at,
                "merge_commit_sha": merge_sha, "head": {"ref": branch, "sha": head},
                "merged_by": merged_by, "labels": [{"name": name} for name in labels]}

    @staticmethod
    def issue(number: int, title: str, body: str, labels: tuple[str, ...], created_at: str = TS) -> dict:
        return {"number": number, "title": title, "body": body, "state": "open",
                "created_at": created_at, "labels": [{"name": name} for name in labels]}

    def run_sync(self, *args: str, gh: FakeGh) -> tuple[int, str, str]:
        """真实产品入口：github_events.main 带注入的 gh 桩，捕获 stdout/stderr。"""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(github_events, "GhClient", lambda: gh), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = github_events.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def github_rows(self, trace: str | None = None) -> list[dict]:
        return events_io.query(source="github", trace_id=trace)

    def rows_of_step(self, trace: str, step: str) -> list[dict]:
        return [row for row in self.github_rows(trace) if row["step"] == step]

    def merge_row(self, trace: str) -> dict:
        found = self.rows_of_step(trace, "github.merge")
        self.assertEqual(len(found), 1, trace)
        return found[0]

    def snapshot_map(self) -> dict[tuple[str, str, int], str]:
        return {(row["source"], row["trace_id"], row["seq"]): row["hash"] for row in self.github_rows()}

    def assert_read_only(self, gh: FakeGh) -> None:
        """假 gh 只出现过只读路由：同步不做任何合并/批准/推送动作。"""
        self.assertTrue(gh.calls, "同步应至少调用过一次 API")
        for call in gh.calls:
            if call == "repo":
                continue
            self.assertRegex(call.partition("?")[0], READ_ROUTES, call)

    # ---- 验收 1：人批准/App 批准/缺批准三种合并的全字段事实；取不到记 unknown，不伪造 ----

    def test_human_app_and_none_merge_facts(self):
        gh = FakeGh(
            pulls={
                11: self.merged_pr(11, "task/human", head=HUMAN_HEAD, merge_sha=HUMAN_MERGE,
                                   merged_by={"login": "alice", "type": "User"},
                                   labels=("class:K3", "needs-independent-review")),
                22: self.merged_pr(22, "task/app", head=APP_HEAD, merge_sha=APP_MERGE,
                                   merged_by={"login": "bob", "type": "User"}, labels=()),
                33: self.merged_pr(33, "task/none", head=NONE_HEAD, merge_sha=NONE_MERGE,
                                   merged_by={"login": "carol", "type": "User"}, labels=("class:K1",)),
                44: self.merged_pr(44, "task/partial", head=UNK_HEAD, merge_sha=UNK_MERGE,
                                   merged_by={"login": "erin", "type": "User"}, labels=()),
            },
            reviews={
                11: [{"state": "CHANGES_REQUESTED", "user": {"login": "alice", "type": "User"},
                      "commit_id": HUMAN_HEAD},
                     {"state": "APPROVED", "user": {"login": "alice", "type": "User"},
                      "commit_id": HUMAN_HEAD}],
                22: [{"state": "APPROVED", "user": {"login": "harness-approval[bot]", "type": "Bot"},
                      "commit_id": APP_HEAD}],
                33: [],
                44: [{"state": "APPROVED", "user": {"login": "dave", "type": "User"},
                      "commit_id": UNK_HEAD}],
            },
            commits={HUMAN_MERGE: {"parents": [{"sha": "p1"}, {"sha": "p2"}]},
                     APP_MERGE: {"parents": [{"sha": "p3"}, {"sha": "p4"}]},
                     NONE_MERGE: {"parents": [{"sha": "p5"}]}},
            pr_commits={33: [{"sha": "x1"}, {"sha": "x2"}]},
            fail=(f"commits/{UNK_MERGE}",),  # 合并提交取不到：合并方式只能如实记 unknown
        )
        for number, expected in ((11, 0), (22, 0), (33, 0), (44, 1)):
            code, out, err = self.run_sync("--pr", str(number), gh=gh)
            self.assertEqual(code, expected, err or out)
        self.assert_read_only(gh)

        # 人批准：批准者与合并者是不同字段；批准绑定提交等于合并 head；双亲 → merge；逐标签成事件
        row = self.merge_row("task/human")
        self.assertEqual(row["stage"], "merge")
        self.assertTrue(row["source"].startswith("github:11:"))
        self.assertEqual(row["outputs"], {"pr": 11, "approver": "alice", "approver_type": "User",
                                          "approval_commit": HUMAN_HEAD, "approval_bound": True,
                                          "merger": "alice", "merger_type": "User",
                                          "merge_method": "merge", "label_count": 2, "merged_at": TS})
        self.assertIn({"kind": "pr", "ref": f"{REPO}#11"}, row["inputs"])
        self.assertIn({"kind": "merge_commit", "ref": HUMAN_MERGE}, row["inputs"])
        labels = {item["outputs"]["label"] for item in self.rows_of_step("task/human", "github.merge_label")}
        self.assertEqual(labels, {"class:K3", "needs-independent-review"})

        # App 批准：批准者是 Bot，不把合并者当批准者
        row = self.merge_row("task/app")
        self.assertEqual((row["outputs"]["approver"], row["outputs"]["approver_type"],
                          row["outputs"]["approval_bound"]), ("harness-approval[bot]", "Bot", True))
        self.assertEqual((row["outputs"]["merger"], row["outputs"]["merge_method"]), ("bob", "merge"))

        # 缺批准（单账号 none）：记 none，不捏造 App，也不把 owner/合并者当批准者
        row = self.merge_row("task/none")
        self.assertEqual((row["outputs"]["approver"], row["outputs"]["approver_type"],
                          row["outputs"]["approval_commit"], row["outputs"]["approval_bound"]),
                         ("none", "none", None, None))
        self.assertNotEqual(row["outputs"]["approver"], row["outputs"]["merger"])
        self.assertNotEqual(row["outputs"]["approver_type"], "Bot")
        self.assertEqual(row["outputs"]["merge_method"], "rebase")  # 单亲且 PR 多于一个提交

        # 合并提交取不到：方式为 unknown 且有明确发现，其余已知字段照常记录、不缺失也不伪造
        code, _, err = self.run_sync("--pr", "44", gh=gh)
        self.assertEqual(code, 1, err)
        row = self.merge_row("task/partial")
        self.assertEqual(row["outputs"]["merge_method"], "unknown")
        self.assertEqual(row["outputs"]["approver"], "dave")
        self.assertIn("失败", err)
        self.assertEqual(events_db.verify(), [])

    # ---- 验收 2：抽样与未抽样；合并后新 escape 可见且旧快照原样保留；同快照幂等 ----

    def test_sample_and_later_escape_are_new_facts(self):
        gh = FakeGh(
            pulls={66: self.merged_pr(66, "task/sampled", head=HUMAN_HEAD, merge_sha=HUMAN_MERGE,
                                      merged_by={"login": "alice", "type": "User"}),
                   77: self.merged_pr(77, "task/plain", head=NONE_HEAD, merge_sha=NONE_MERGE,
                                      merged_by={"login": "carol", "type": "User"})},
            reviews={66: [], 77: []},
            commits={HUMAN_MERGE: {"parents": [{"sha": "p1"}, {"sha": "p2"}]},
                     NONE_MERGE: {"parents": [{"sha": "p5"}]}},
            audit=[self.issue(901, "Audit: PR #66 (K5)", "PR #66 was sampled by the audit rule.",
                              ("audit", "class:K5"))],
        )
        for number in (66, 77):
            code, _, err = self.run_sync("--pr", str(number), gh=gh)
            self.assertEqual(code, 0, err)

        # 抽中：audit 议题指认该 PR → sampled=true 且议题号入事件；未抽中：false 且无议题号
        sampled = self.rows_of_step("task/sampled", "github.audit_sample")
        self.assertEqual(len(sampled), 1)
        self.assertEqual((sampled[0]["stage"], sampled[0]["source"].startswith("github:66:")), ("ci", True))
        self.assertEqual(sampled[0]["outputs"], {"pr": 66, "sampled": True, "issue": 901})
        plain = self.rows_of_step("task/plain", "github.audit_sample")
        self.assertEqual(plain[0]["outputs"], {"pr": 77, "sampled": False, "issue": None})
        before = self.snapshot_map()

        # 合并后新登记 escape：再同步即可见；不同 PR 的 escape 互不归属
        gh.issues["escape"] += [self.issue(950, "Escape: wrong result", "introduced by PR #66.",
                                           ("escape", "class:K5"), TS2),
                                self.issue(951, "Escape: other", "introduced by PR #77.",
                                           ("escape", "class:K1"), TS2)]
        code, _, err = self.run_sync("--pr", "66", gh=gh)
        self.assertEqual(code, 0, err)
        escapes = [row for row in self.github_rows("task/sampled") if row["step"] == "github.escape"]
        self.assertEqual([row["outputs"] for row in escapes],
                         [{"pr": 66, "issue": 950, "class": "K5", "state": "open"}])
        self.assertEqual((escapes[0]["stage"], escapes[0]["seq"]), ("ci", 1))  # 新快照另起新链

        # 旧快照原样保留：已有 (source, trace, seq) 的哈希一个都没变，也没有任何行被覆盖
        after = self.snapshot_map()
        self.assertEqual(len(after), len(before) + 1)
        self.assertTrue(set(before.items()) <= set(after.items()))

        # 同快照幂等：重复同步不新增任何事实事件
        total = len(self.github_rows())
        code, _, err = self.run_sync("--pr", "66", gh=gh)
        self.assertEqual((code, len(self.github_rows())), (0, total), err)
        self.assertEqual(events_db.verify(), [])

    # ---- 验收 3：分页的多个关联 PR/议题；重复同步不新增相同事实事件；归到各自 headRefName ----

    def test_pagination_and_repeat_sync(self):
        audit = [self.issue(850 + index, f"Audit noise {index}", "unrelated",
                            ("audit", "class:K1")) for index in range(5)]
        audit.append(self.issue(801, "Audit: PR #88 (K1)", "PR #88 was sampled.", ("audit", "class:K1")))
        escape = [self.issue(830 + index, f"Escape noise {index}", "unrelated", ("escape",))
                  for index in range(5)]
        escape += [self.issue(810, "Escape: regression", "introduced by PR #88.", ("escape", "class:K1")),
                   self.issue(820, "Escape: other regression", "introduced by PR #99.",
                              ("escape", "class:K2"))]
        merge_message = {"commit": {"message": "Merge pull request #88 from task/paged\n\ndetails"}}
        gh = FakeGh(
            pulls={88: self.merged_pr(88, "task/paged", head=PAGE_HEAD, merge_sha=PAGE_MERGE,
                                      merged_by={"login": "alice", "type": "User"}),
                   99: self.merged_pr(99, "task/other", head=OTHER_HEAD, merge_sha=OTHER_MERGE,
                                      merged_by={"login": "bob", "type": "User"})},
            reviews={88: [], 99: []},
            commits={PAGE_MERGE: {**merge_message, "parents": [{"sha": "p1"}, {"sha": "p2"}]},
                     OTHER_MERGE: {"parents": [{"sha": "p3"}]}},
            pr_commits={99: [{"sha": "x1"}]},
            audit=audit, escape=escape, page_size=2,  # 服务端页容量 2：实现必须真实翻页
        )
        # 缺省路径：--head 是 push 的合并提交，从其信息解析关联 PR
        code, out, err = self.run_sync("--head", PAGE_MERGE, gh=gh)
        self.assertEqual(code, 0, err)
        self.assertIn("PR #88", out)
        code, out, err = self.run_sync("--pr", "99", gh=gh)
        self.assertEqual(code, 0, err)

        # 分页被真实读取：议题列表请求过第 2 页及以后
        paged = [call for call in gh.calls if "/issues" in call and "page=2" in call]
        self.assertTrue(paged, gh.calls)

        # 事实分别归属各自 PR 的 headRefName；#88 的同步不带上 #99 的 escape，反之亦然
        paged_rows = {(row["step"], row["outputs"].get("issue")) for row in self.github_rows("task/paged")}
        self.assertIn(("github.merge", None), paged_rows)
        self.assertIn(("github.audit_sample", 801), paged_rows)
        self.assertIn(("github.escape", 810), paged_rows)
        self.assertNotIn(("github.escape", 820), paged_rows)
        other_rows = {(row["step"], row["outputs"].get("issue")) for row in self.github_rows("task/other")}
        self.assertEqual(other_rows, {("github.merge", None), ("github.audit_sample", None),
                                      ("github.escape", 820)})
        sampled = next(row["outputs"] for row in self.github_rows("task/paged")
                       if row["step"] == "github.audit_sample")
        self.assertEqual(sampled, {"pr": 88, "sampled": True, "issue": 801})

        # 重复同步：换一个页容量（读 API 次数不同）事实相同 → 不新增事件，事件哈希逐条不变
        snapshot = self.snapshot_map()
        gh.page_size = 3
        for args in (("--head", PAGE_MERGE), ("--pr", "99")):
            code, out, err = self.run_sync(*args, gh=gh)
            self.assertEqual(code, 0, err)
        self.assertEqual(self.snapshot_map(), snapshot)
        self.assertEqual(events_db.verify(), [])

        # 反例：不指向合并提交的 head 明确报告 no_pr，不产出事件
        gh.commits["1111111111111111111111111111111111111111"] = {"commit": {"message": "direct push"},
                                                                  "parents": [{"sha": "p9"}]}
        code, out, err = self.run_sync("--head", "1" * 40, gh=gh)
        self.assertEqual(code, 1, err)
        self.assertIn("不是关联 PR 的合并提交", err)
        self.assertEqual(len(self.github_rows()), len(snapshot))

    # ---- 验收 4：API 失败明确报告事实缺失，不伪造完整事实；同步只读；模板步骤失败隔离 ----

    def test_api_failure_does_not_change_merge(self):
        # 整体无权限：明确报告、退出码 1、一个事件都不写（不可得 ≠ 完整）
        denied = FakeGh(pulls={31: self.merged_pr(31, "task/x", head=HUMAN_HEAD, merge_sha=HUMAN_MERGE,
                                                  merged_by={"login": "alice", "type": "User"})},
                        fail=("pulls/31",))
        code, _, err = self.run_sync("--pr", "31", gh=denied)
        self.assertEqual(code, 1, err)
        self.assertIn("读取 PR 31 失败", err)
        self.assertEqual(self.github_rows(), [])
        self.assert_read_only(denied)

        # 部分失败：评审列表取不到 → 批准者如实记 unknown（不捏造），其余已知字段照常成快照
        partial = FakeGh(pulls={32: self.merged_pr(32, "task/y", head=APP_HEAD, merge_sha=APP_MERGE,
                                                   merged_by={"login": "bob", "type": "User"},
                                                   labels=("class:K2",))},
                         reviews={}, commits={APP_MERGE: {"parents": [{"sha": "p1"}, {"sha": "p2"}]}},
                         fail=("pulls/32/reviews",))
        code, _, err = self.run_sync("--pr", "32", gh=partial)
        self.assertEqual(code, 1, err)
        row = self.merge_row("task/y")
        self.assertEqual((row["outputs"]["approver"], row["outputs"]["approver_type"]),
                         ("unknown", "unknown"))
        self.assertEqual((row["outputs"]["merger"], row["outputs"]["merge_method"]), ("bob", "merge"))
        self.assertIn("失败", err)
        self.assert_read_only(partial)

        # 工作流模板：同步步骤只读、失败被 continue-on-error 隔离，原 harness 判定与 job 结论不受影响
        text = (ENGINE_REPO / "templates/.github/workflows/harness.yml").read_text(encoding="utf-8")
        lines = text.splitlines()
        start = next(index for index, line in enumerate(lines) if line.strip() == "- name: Sync GitHub facts")
        end = next((index for index in range(start + 1, len(lines))
                    if lines[index].strip().startswith("- name:")), len(lines))
        step = "\n".join(lines[start:end])
        self.assertIn("continue-on-error: true", step)
        self.assertIn("always()", step)
        # 手工与 App 合并都经 push 触发，不靠 auto-merge 独占；非 push（PR 分支）在脚本内显式跳过
        self.assertIn('if [ "${{ github.event_name }}" != "push" ]', step)
        self.assertIn("python .harness/engine/reports/github_events.py --head", step)
        self.assertIn("GH_TOKEN: ${{ github.token }}", step)
        self.assertIn("GH_REPO: ${{ github.repository }}", step)
        self.assertIsNone(re.search(r"^\s+id:", step, re.MULTILINE))  # 无输出被后续步骤消费，不参与判定
        job = text.split("\n  harness:\n", 1)[1].split("\n  #", 1)[0]
        for permission in ("pull-requests: read", "issues: read", "contents: read"):
            self.assertIn(permission, job.split("- uses:", 1)[0])  # 同步所需只读权限，不升写权限

    # ---- 修订二轮：单亲「标题 (#N)」提交判 squash；真 rebase 不误判；squash 形式标题解析 PR 号 ----

    def test_squash_and_rebase_merge_methods(self):
        squash_merge, squash_head = "a7" * 20, "a8" * 20
        rebase_merge, rebase_head = "a9" * 20, "b0" * 20
        gh = FakeGh(
            pulls={55: {"number": 55, "state": "closed", "merged": True, "merged_at": TS,
                        "merge_commit_sha": squash_merge, "title": "Fix the thing",
                        "head": {"ref": "task/squash", "sha": squash_head},
                        "merged_by": {"login": "alice", "type": "User"}, "labels": []},
                   66: self.merged_pr(66, "task/rebased", head=rebase_head, merge_sha=rebase_merge,
                                      merged_by={"login": "bob", "type": "User"}),
                   77: {**self.merged_pr(77, "task/edited", head=NONE_HEAD, merge_sha=NONE_MERGE,
                                         merged_by={"login": "carol", "type": "User"}),
                        "title": "Rename things"}},
            reviews={55: [], 66: [], 77: []},
            commits={squash_merge: {"commit": {"message": "Fix the thing (#55)\n\n* one\n* two"},
                                    "parents": [{"sha": "p1"}]},   # squash：单亲，主题 = 标题 (#55)
                     rebase_merge: {"commit": {"message": "fix bug (#12)"},
                                    "parents": [{"sha": "p2"}]},   # 真 rebase：顶提交碰巧以「(#N)」结尾
                     NONE_MERGE: {"commit": {"message": "Rename things WIP (#77)"},
                                  "parents": [{"sha": "p3"}]}},    # 尾注号对但主题≠标题：不判 squash
            pr_commits={55: [{"sha": "x1"}, {"sha": "x2"}],    # squash 自多提交 PR，旧启发式会误判 rebase
                        66: [{"sha": "y1"}, {"sha": "y2"}],
                        77: [{"sha": "z1"}, {"sha": "z2"}]},
        )
        # squash 形式 push 标题解析出 PR 号：缺省路径不带 --pr 也同步（旧版报 no_pr），方式记 squash
        code, out, err = self.run_sync("--head", squash_merge, gh=gh)
        self.assertEqual(code, 0, err)
        self.assertIn("PR #55", out)
        row = self.merge_row("task/squash")
        self.assertEqual(row["outputs"]["merge_method"], "squash")
        self.assertEqual(row["outputs"]["merger"], "alice")

        # 真 rebase：单亲、PR 多于一个提交、顶提交以「(#N)」结尾——仍记 rebase，不误判成 squash
        code, _, err = self.run_sync("--pr", "66", gh=gh)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.merge_row("task/rebased")["outputs"]["merge_method"], "rebase")

        # 反例：尾注 PR 号对但提交主题不是「标题 (#PR号)」（如合并后改过标题）——不判 squash
        code, _, err = self.run_sync("--pr", "77", gh=gh)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.merge_row("task/edited")["outputs"]["merge_method"], "rebase")
        self.assert_read_only(gh)
        self.assertEqual(events_db.verify(), [])

    # ---- 步骤 2：安装布局经 PATH 上的假 gh 调用真实脚本入口；增量 --since 与幂等 ----

    def test_installed_entry_with_fake_gh(self):
        project = self.fresh_repo("installed")
        env = {**{k: v for k, v in os.environ.items() if not k.startswith(CLEAN_PREFIXES)}, **GIT_ENV,
               "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, str(CLI), "install", "--target", str(project),
                                 "--allow-dirty"], capture_output=True, text=True, env=env, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        entry = project / ".harness/engine/reports/github_events.py"
        self.assertTrue(entry.exists())
        state_path = self.tmp / "fake-gh-state.json"
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        script = bin_dir / "gh"
        script.write_text(self._FAKE_GH, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        state = {
            "repo": REPO,
            "pulls": {"7": self.merged_pr(7, "task/installed", head=HUMAN_HEAD, merge_sha=HUMAN_MERGE,
                                          merged_by={"login": "alice", "type": "User"},
                                          labels=("class:K3",))},
            "reviews": {"7": [{"state": "APPROVED", "user": {"login": "alice", "type": "User"},
                               "commit_id": HUMAN_HEAD}]},
            "pr_commits": {},
            "commits": {HUMAN_MERGE: {"parents": [{"sha": "p1"}, {"sha": "p2"}]}},
            "issues": {"audit": [], "escape": []},
        }

        def run(*args: str) -> subprocess.CompletedProcess:
            state_path.write_text(canonical(state), encoding="utf-8")
            return subprocess.run([sys.executable, ".harness/engine/reports/github_events.py", *args],
                                  cwd=project, capture_output=True, text=True, check=False, timeout=120,
                                  env={**env, "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
                                       "GITHUB_REPOSITORY": REPO, "GH_TOKEN": "fake-token",
                                       "FAKE_GH_STATE": str(state_path)})

        done = run("--pr", "7")
        self.assertEqual(done.returncode, 0, done.stderr)
        with mock.patch.object(events_db, "ROOT", project):
            rows = events_io.query(source="github")
            merge_output = next(row["outputs"] for row in rows if row["step"] == "github.merge")
            self.assertEqual(merge_output,
                             {"pr": 7, "approver": "alice", "approver_type": "User",
                              "approval_commit": HUMAN_HEAD, "approval_bound": True,
                              "merger": "alice", "merger_type": "User",
                              "merge_method": "merge", "label_count": 1, "merged_at": TS})
            total = len(rows)
        # 登记后增量读取（--since）+ 幂等：第三跑不再新增
        state["issues"]["escape"].append(self.issue(700, "Escape: late", "introduced by PR #7.",
                                                    ("escape", "class:K3"), TS2))
        again = run("--pr", "7", "--since", TS)
        self.assertEqual(again.returncode, 0, again.stderr)
        with mock.patch.object(events_db, "ROOT", project):
            rows = events_io.query(source="github")
            self.assertEqual([row["outputs"] for row in rows if row["step"] == "github.escape"],
                             [{"pr": 7, "issue": 700, "class": "K3", "state": "open"}])
            self.assertEqual(len(events_io.query(source="github")), total + 1)
        third = run("--pr", "7", "--since", TS)
        self.assertEqual(third.returncode, 0, third.stderr)
        with mock.patch.object(events_db, "ROOT", project):
            self.assertEqual(len(events_io.query(source="github")), total + 1)

    # 假 gh 可执行脚本：serve 状态文件里的只读 API 响应（页参数生效；fail 子串命中即 403）。
    _FAKE_GH = """#!/usr/bin/env python3
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs

state = json.loads(Path(os.environ["FAKE_GH_STATE"]).read_text(encoding="utf-8"))
args = sys.argv[1:]
if not args or args[0] != "api":
    print(json.dumps({"nameWithOwner": state["repo"]}))
    sys.exit(0)
route = args[1]
for pattern in state.get("fail", []):
    if pattern in route:
        sys.stderr.write("HTTP 403: 权限不足（夹具）\\n")
        sys.exit(4)
path, _, query = route.partition("?")
params = parse_qs(query)


def page(items):
    per = int(params.get("per_page", ["30"])[0])
    number = int(params.get("page", ["1"])[0])
    return items[(number - 1) * per:number * per]


if match := re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\\d+)", path):
    print(json.dumps(state["pulls"][match[1]]))
elif match := re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\\d+)/reviews", path):
    print(json.dumps(page(state["reviews"].get(match[1], []))))
elif match := re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\\d+)/commits", path):
    print(json.dumps(page(state["pr_commits"].get(match[1], []))))
elif match := re.fullmatch(r"repos/[^/]+/[^/]+/commits/([0-9a-f]+)", path):
    print(json.dumps(state["commits"][match[1]]))
elif path.endswith("/issues"):
    print(json.dumps(page(state["issues"].get(params.get("labels", [""])[0], []))))
else:
    print("{}")
"""


if __name__ == "__main__":
    unittest.main()
