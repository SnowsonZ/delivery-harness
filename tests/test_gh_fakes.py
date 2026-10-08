"""tests/gh_fakes.py 共用假 GitHub 客户端基类的测试（B123，T721 C 部分）。

覆盖：FakeGhBase 单独实例化后十一类路由（含批量 pulls 与 actions/workflows）各有返回、
未配置路由抛 AssertionError、fail 子串命中抛 403、评论 POST 记入 writes、_page 按 page/per_page
切片（page_size 取更小值）；六个子类都是基类子类且不覆盖 api（基类加路由子类立即可见，逐个断言）；
六个文件除 FakeGh 类与 import 外与 main 上的版本 AST 逐节点相同（ast.dump 比对）、FakeGh 类体不再
含完整路由分发且总行数比重构前至少少 250 行、给基类加的路由对每个子类立即可用；三个协议不同的
测试文件逐字节不变；bin/ci-shard 的模块集合恰为原有集合加三个新测试模块、不含 tests.gh_fakes、
分片无重复无遗漏。
"""

from __future__ import annotations

import ast
import subprocess
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
NON_MIGRATED = ("tests/test_trace_events_cli.py", "tests/test_gh_json_fields.py",
                "tests/test_events_judge_runs.py")
NEW_MODULES = {"tests.test_taskbook_headroom", "tests.test_dispatch_salvage", "tests.test_gh_fakes"}


def origin_text(rel: str) -> str:
    done = subprocess.run(["git", "show", f"origin/main:{rel}"], cwd=ENGINE_REPO,
                          capture_output=True, text=True, check=True)
    return done.stdout


def frozen_dump(text: str) -> str:
    """剔除 FakeGh 类定义与全部 import 后的模块 AST（C0 例外只允许改这两处）。"""
    tree = ast.parse(text)
    tree.body = [node for node in tree.body
                 if not (isinstance(node, ast.ClassDef) and node.name == "FakeGh")
                 and not isinstance(node, (ast.Import, ast.ImportFrom))]
    return ast.dump(tree)


def fakegh_span(text: str) -> int:
    for node in ast.parse(text).body:
        if isinstance(node, ast.ClassDef) and node.name == "FakeGh":
            return node.end_lineno - node.lineno + 1
    return 0


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

    def test_files_frozen_except_fakegh_and_imports(self):
        for module in SUBCLASS_MODULES:
            rel = module.__file__.replace(str(ENGINE_REPO) + "/", "")
            with self.subTest(file=rel):
                self.assertEqual(frozen_dump(Path(module.__file__).read_text(encoding="utf-8")),
                                 frozen_dump(origin_text(rel)),
                                 "除 FakeGh 类与 import 外，测试文件与 main 上的版本必须逐节点相同")

    def test_fakegh_total_lines_shrink_by_250(self):
        old_total = sum(fakegh_span(origin_text(module.__file__.replace(str(ENGINE_REPO) + "/", "")))
                        for module in SUBCLASS_MODULES)
        new_total = sum(fakegh_span(Path(module.__file__).read_text(encoding="utf-8"))
                        for module in SUBCLASS_MODULES)
        self.assertGreaterEqual(old_total - new_total, 250, (old_total, new_total))

    def test_non_migrated_files_byte_identical(self):
        for rel in NON_MIGRATED:
            with self.subTest(file=rel):
                self.assertEqual((ENGINE_REPO / rel).read_bytes(), origin_text(rel).encode("utf-8"))


class ShardSetTest(unittest.TestCase):
    def setUp(self):
        self.script = runpy_load_ci_shard()

    def test_module_set_is_old_plus_three_new_without_gh_fakes(self):
        done = subprocess.run(["git", "ls-tree", "--name-only", "origin/main", "tests/"],
                              cwd=ENGINE_REPO, capture_output=True, text=True, check=True)
        old = {f"tests.{Path(line).stem}" for line in done.stdout.splitlines()
               if Path(line).name.startswith("test_")}
        names = [name for name, _weight in self.script["modules"]()]
        self.assertEqual(set(names), old | NEW_MODULES)
        self.assertNotIn("tests.gh_fakes", names)  # 共用基类不是用例
        self.assertEqual(len(names), len(set(names)))  # 无重复

    def test_shards_cover_every_module_exactly_once(self):
        names = sorted(name for name, _weight in self.script["modules"]())
        flat = [name for shard in self.script["shards"](3) for name in shard]
        self.assertEqual(sorted(flat), names)


def runpy_load_ci_shard() -> dict:
    import runpy
    return runpy.run_path(str(ENGINE_REPO / "bin" / "ci-shard"))


if __name__ == "__main__":
    unittest.main()
