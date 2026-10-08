"""tests/gh_fakes.py 共用假 GitHub 客户端基类的测试（B123，T721 C 部分）。

覆盖：FakeGhBase 单独实例化后十一类路由（含批量 pulls 与 actions/workflows）各有返回、未配置路由抛
AssertionError、fail 子串命中抛 403、评论 POST 记入 writes、_page 按 page/per_page 切片（page_size 取
更小值）；六个子类都是基类子类且不覆盖 api（基类加路由，子类立即可见，逐个断言）；bin/ci-shard 不把
tests.gh_fakes 当用例、会收集本任务新增的三个测试模块、分片无重复无遗漏。

不在这里的：六个文件「除 FakeGh 与 import 外与重构前逐节点相同」「FakeGh 行数合计少 250」「三个不迁移
的文件不变」「模块集合恰为旧集合加三个」都是对 main 的一次性比对，由设计方在验收时执行，不写成永久测试
（合并后 main 就是重构后的版本，比对会自指；以后新增测试文件也会让精确集合失败）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import (
    test_audit_completeness,
    test_audit_events,
    test_audit_ledger,
    test_audit_ledger_github,
    test_audit_reconstruction,
    test_ledger_equivalent_head,
)
from tests.gh_fakes import REPO, FakeGhBase

ENGINE_REPO = Path(__file__).resolve().parents[1]
SUBCLASS_MODULES = (
    test_audit_completeness,
    test_audit_ledger,
    test_audit_reconstruction,
    test_audit_ledger_github,
    test_audit_events,
    test_ledger_equivalent_head,
)


def make_base(**overrides) -> FakeGhBase:
    data = {
        "pulls": {7: {"number": 7, "head": {"ref": "task/1", "sha": "abc"}}},
        "closed": [{"number": 1}, {"number": 2}, {"number": 3}],
        "reviews": {7: [{"state": "APPROVED"}]},
        "pr_commits": {7: [{"sha": "c1"}]},
        "commits": {"aabbcc": {"sha": "aabbcc", "parents": [{"sha": "p"}]}},
        "comments": [{"id": 5, "body": "hi", "created_at": "t", "html_url": "u"}],
        "issues": {"bug": [{"number": 9}]},
        "runs": [{"head_branch": "task/1", "id": 1}, {"head_branch": "main", "id": 2}],
        "artifacts": {3: [{"id": 30}]},
        "downloads": {"https://example.invalid/x.zip": b"zip"},
    }
    data.update(overrides)
    return FakeGhBase(**data)


class BaseRoutesTest(unittest.TestCase):
    def test_all_eleven_route_classes_answer(self):
        gh = make_base()
        self.assertEqual(gh.api(f"repos/{REPO}/pulls?per_page=2&page=2"), [{"number": 3}])
        self.assertEqual(gh.api(f"repos/{REPO}/pulls/7"), {"number": 7, "head": {"ref": "task/1", "sha": "abc"}})
        self.assertEqual(gh.api(f"repos/{REPO}/pulls/7/reviews"), [{"state": "APPROVED"}])
        self.assertEqual(gh.api(f"repos/{REPO}/pulls/7/commits"), [{"sha": "c1"}])
        self.assertEqual(gh.api(f"repos/{REPO}/commits/aabbcc"), {"sha": "aabbcc", "parents": [{"sha": "p"}]})
        self.assertEqual(gh.api(f"repos/{REPO}/issues/7/comments"),
                         [{"id": 5, "body": "hi", "created_at": "t", "html_url": "u"}])
        self.assertEqual(gh.api(f"repos/{REPO}/issues/comments/5")["id"], 5)
        self.assertEqual(gh.api(f"repos/{REPO}/issues?labels=bug"), [{"number": 9}])
        self.assertEqual(gh.api(f"repos/{REPO}/actions/runs?branch=task%2F1"),
                         {"total_count": 1, "workflow_runs": [{"head_branch": "task/1", "id": 1}]})
        self.assertEqual(gh.api(f"repos/{REPO}/actions/runs/3/artifacts"),
                         {"total_count": 1, "artifacts": [{"id": 30}]})
        self.assertEqual(gh.api(f"repos/{REPO}/actions/workflows"), {"total_count": 0, "workflows": []})

    def test_repo_pr_download_record_calls(self):
        gh = make_base()
        self.assertEqual(gh.repo(), REPO)
        self.assertEqual(gh.pr(7)["headRefName"], "task/1")
        self.assertEqual(gh.download("https://example.invalid/x.zip"), b"zip")
        self.assertIn(("repo",), gh.calls)
        self.assertIn(("pr", 7), gh.calls)
        self.assertIn(("download", "https://example.invalid/x.zip"), gh.calls)

    def test_unconfigured_route_raises_assertion(self):
        with self.assertRaises(AssertionError):
            make_base().api(f"repos/{REPO}/labels/x")

    def test_fail_substring_raises_403(self):
        gh = make_base(fail=("issues/9",))
        with self.assertRaises(RuntimeError) as caught:
            gh.api(f"repos/{REPO}/issues/9/comments")
        self.assertEqual(str(caught.exception), "HTTP 403: 权限不足（夹具）")

    def test_comment_post_is_recorded_and_appended(self):
        gh = make_base()
        expected_id = 9000 + len(gh.comments)
        comment = gh.api(f"repos/{REPO}/issues/7/comments", method="POST", payload={"body": "anchor"})
        self.assertEqual(comment["id"], expected_id)
        self.assertEqual(comment["html_url"], f"https://github.com/{REPO}.git/issues/comments/{expected_id}")
        self.assertEqual(gh.writes, [("POST", f"repos/{REPO}/issues/7/comments", {"body": "anchor"})])
        self.assertEqual(gh.comments[-1]["body"], "anchor")

    def test_write_route_outside_comments_is_rejected(self):
        with self.assertRaises(AssertionError):
            make_base().api(f"repos/{REPO}/pulls/7/merge", method="PUT", payload={})

    def test_page_slices_by_page_and_per_page(self):
        gh = make_base()
        self.assertEqual(gh._page([1, 2, 3, 4, 5], "page=2&per_page=2"), [3, 4])
        self.assertEqual(gh._page([1, 2, 3], "page=3&per_page=30"), [])
        self.assertEqual(make_base(page_size=2)._page([1, 2, 3], "page=1&per_page=30"), [1, 2])

    def test_missing_pr_commit_comment_defaults_are_404(self):
        gh = make_base()
        with self.assertRaisesRegex(RuntimeError, "夹具无 PR 8"):
            gh.api(f"repos/{REPO}/pulls/8")
        with self.assertRaisesRegex(RuntimeError, "夹具无提交 dd"):
            gh.api(f"repos/{REPO}/commits/dddd")
        with self.assertRaisesRegex(RuntimeError, "夹具无评论 6"):
            gh.api(f"repos/{REPO}/issues/comments/6")


class SubclassesTest(unittest.TestCase):
    def test_all_six_are_base_subclasses_without_own_api(self):
        for module in SUBCLASS_MODULES:
            klass = module.FakeGh
            self.assertTrue(issubclass(klass, FakeGhBase), module.__name__)
            self.assertNotIn("api", vars(klass), module.__name__)  # 不再保留旧的完整路由分发
            self.assertIs(klass.api, FakeGhBase.api, module.__name__)  # 基类加路由子类立即可见

    def test_base_routes_reachable_from_every_subclass(self):
        """八个不抛错的默认路由对每个子类实例可用（差异行为由各自测试守着）。"""
        for module in SUBCLASS_MODULES:
            with self.subTest(module=module.__name__):
                gh = module.FakeGh()
                self.assertEqual(gh.api(f"repos/{REPO}/pulls"), [])
                self.assertEqual(gh.api(f"repos/{REPO}/pulls/7/reviews"), [])
                self.assertEqual(gh.api(f"repos/{REPO}/pulls/7/commits"), [])
                self.assertEqual(gh.api(f"repos/{REPO}/issues?labels=x"), [])
                self.assertEqual(gh.api(f"repos/{REPO}/issues/7/comments"), [])
                self.assertEqual(gh.api(f"repos/{REPO}/actions/runs?branch=x"),
                                 {"total_count": 0, "workflow_runs": []})
                self.assertEqual(gh.api(f"repos/{REPO}/actions/runs/3/artifacts"),
                                 {"total_count": 0, "artifacts": []})
                self.assertEqual(gh.api(f"repos/{REPO}/actions/workflows"),
                                 {"total_count": 0, "workflows": []})

    def test_new_base_route_is_immediately_visible_to_subclasses(self):
        """模拟「给基类加一条新路由」：补丁掉 api 后六个子类同一方法生效。"""
        original = FakeGhBase.api

        def api_with_new_route(self, route, *, method="GET", payload=None):
            if route == f"repos/{REPO}/brand/new":
                return {"new": True}
            return original(self, route, method=method, payload=payload)

        with mock.patch.object(FakeGhBase, "api", api_with_new_route):
            for module in SUBCLASS_MODULES:
                with self.subTest(module=module.__name__):
                    self.assertEqual(module.FakeGh().api(f"repos/{REPO}/brand/new"), {"new": True})

class ShardSetTest(unittest.TestCase):
    def setUp(self):
        self.script = runpy_load_ci_shard()

    def test_gh_fakes_is_not_collected_and_new_modules_are(self):
        # 共用基类不是用例（文件名不以 test_ 开头）；本任务新增的三个测试模块会被分片收集；模块名无重复。
        # 「与重构前集合比对」只是一次性验收（设计方做），不写成永久测试：合并后 main 就是重构后的版本，
        # 且以后任何人新增测试文件都会让「恰好 68 个」失败。
        names = [name for name, _weight in self.script["modules"]()]
        self.assertNotIn("tests.gh_fakes", names)
        for module in ("tests.test_taskbook_headroom", "tests.test_dispatch_salvage", "tests.test_gh_fakes"):
            self.assertIn(module, names)
        self.assertEqual(len(names), len(set(names)))

    def test_shards_cover_every_module_exactly_once(self):
        names = sorted(name for name, _weight in self.script["modules"]())
        flat = [name for shard in self.script["shards"](3) for name in shard]
        self.assertEqual(sorted(flat), names)


def runpy_load_ci_shard() -> dict:
    import runpy
    return runpy.run_path(str(ENGINE_REPO / "bin" / "ci-shard"))


if __name__ == "__main__":
    unittest.main()
