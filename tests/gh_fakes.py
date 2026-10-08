"""六个 audit/ledger 测试文件共用的假 GitHub 客户端基类（B123，T721 C 部分）。

导入方式沿用仓库既有写法（仓库根在 sys.path）：`from tests.gh_fakes import FakeGhBase`，各测试文件的
`FakeGh` 是它的子类，只保留自己真正不同的行为。之后给这六个文件新增一条 API 路由只改本文件一处。
文件名不以 test_ 开头：不会被 unittest discover 或 bin/ci-shard 当成用例收集。

行为矩阵（设计评审对六个旧实现逐个 difflib 得到的已知差异下限；子类用类属性或小型覆盖保留，
执行 T721 时已逐文件对照旧代码核对）：

| 文件 | 已知差异 |
|---|---|
| test_audit_completeness | closed（批量已关闭 PR 列表）；actions/runs 按 branch 过滤、runs 与 artifacts 不分页 |
| test_audit_ledger_github | 同上，但没有 closed 路由（构造参数也没有 closed） |
| test_audit_reconstruction | closed、page_size；actions/runs 按 branch 过滤、runs 与 artifacts 分页 |
| test_audit_ledger | page_size、audit/escape 构造参数、评论 id 计数器 _ids、PATCH 评论、writes 记账方式；actions/runs 不按 branch 过滤；没有单条评论 GET；评论时间戳与其余文件不同 |
| test_ledger_equivalent_head | repo() 不记录调用；缺失提交抛 KeyError、缺失评论抛 StopIteration；actions/runs 不过滤 branch |
| test_audit_events | closed、fail_comments；任何提交查询固定返回两个父节点；API 调用固定记为 GET（基类按 (method, route) 记录，对 GET 调用两者相同） |

子类开关（类属性；默认值即 completeness / ledger_github 的行为）：
  runs_filter_by_branch   actions/runs 是否按 branch 查询参数过滤（False：equivalent_head、ledger）
  paginate_runs           actions/runs 的 workflow_runs 是否分页（True：reconstruction、ledger、equivalent_head）
  paginate_artifacts      artifacts 列表是否分页（True：reconstruction、ledger、equivalent_head）
  get_comment_by_id       是否提供 GET issues/comments/<id> 路由（False：ledger，路由按未配置断言失败）
  fail_comments           issues/<n>/comments 是否抛 403（True：test_audit_events 的故障注入）
  missing_pr              "404"（RuntimeError）或 "key"（KeyError，equivalent_head）
  missing_commit          "404"、"key"（equivalent_head）或 "fixed"（固定双父节点，test_audit_events）
  missing_comment         "404" 或 "stop"（StopIteration，equivalent_head）

不迁移的三个文件与理由：
  - test_trace_events_cli.py：FakeGh.api(route) 按页返回运行列表，协议不同；
  - test_gh_json_fields.py：PATH 上的子进程假 `gh` 与 _RunsStub；
  - test_events_judge_runs.py：按工作流分页的运行列表，协议不同。
"""

from __future__ import annotations

import re
import urllib.parse

REPO = "owner/repo"
GITHUB_URL = f"https://github.com/{REPO}.git"
# 基类 POST 评论的固定时间戳（completeness / reconstruction / ledger_github 的取值）。
TS = "2026-01-02T00:01:00.000Z"


class FakeGhBase:
    """audit/ledger/load_ci/sync 共用 gh 桩：只读 API、artifact zip 下载与锚点评论 POST。

    记录：api/pr/repo/download 全部调用进 calls（(method, route) / ("pr", n) / ("repo",) /
    ("download", url)），评论写操作另记进 writes；fail 子串命中即抛 403；未配置的路由抛
    AssertionError（新增路由时在这里加一行即可对六个子类同时生效）。
    """

    runs_filter_by_branch = True
    paginate_runs = False
    paginate_artifacts = False
    get_comment_by_id = True
    fail_comments = False
    missing_pr = "404"
    missing_commit = "404"
    missing_comment = "404"

    def __init__(self, *, repo=REPO, pulls=None, closed=None, reviews=None, pr_commits=None,
                 commits=None, comments=None, issues=None, runs=None, artifacts=None, downloads=None,
                 page_size=None, fail=()):
        self.calls: list[tuple] = []
        self.writes: list[tuple] = []
        self._repo = repo
        self.pulls = pulls or {}
        self.closed = list(closed or [])
        self.reviews = reviews or {}
        self.pr_commits = pr_commits or {}
        self.commits = commits or {}
        self.comments = list(comments or [])
        self.issues = issues or {}
        self.runs = runs or []
        self.artifacts = artifacts or {}
        self.downloads = downloads or {}
        self.page_size = page_size
        self.fail = tuple(fail)

    def repo(self) -> str:
        self.calls.append(("repo",))
        return self._repo

    def pr(self, pr: int) -> dict:
        self.calls.append(("pr", pr))
        pull = self.pulls.get(pr)
        if pull is None:
            raise RuntimeError(f"HTTP 404: Not Found（夹具无 PR {pr}）")
        return {"headRefName": pull["head"]["ref"], "headRefOid": pull["head"]["sha"],
                "repository": self._repo}

    def api(self, route: str, *, method: str = "GET", payload=None):
        self.calls.append((method, route))
        if method != "GET":
            return self._write(route, payload)
        for pattern in self.fail:
            if pattern in route:
                raise RuntimeError("HTTP 403: 权限不足（夹具）")
        path, _, query = route.partition("?")
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\d+)", path):
            return self._pull(match[1])
        if re.fullmatch(r"repos/[^/]+/[^/]+/pulls", path):
            return self._page(self.closed, query)
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\d+)/reviews", path):
            return self._page(self.reviews.get(int(match[1]), []), query)
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\d+)/commits", path):
            return self._page(self.pr_commits.get(int(match[1]), []), query)
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/commits/([0-9a-f]+)", path):
            return self._commit(match[1])
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/issues/(\d+)/comments", path):
            if self.fail_comments:
                raise RuntimeError("HTTP 403: 权限不足（夹具）")
            return self._page(self.comments, query)
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/issues/comments/(\d+)", path):
            if not self.get_comment_by_id:
                raise AssertionError(f"FakeGh 未配置的路由：{path}")
            return self._single_comment(int(match[1]))
        if re.fullmatch(r"repos/[^/]+/[^/]+/issues", path):
            label = urllib.parse.parse_qs(query).get("labels", [""])[0]
            return self._page(self.issues.get(label, []), query)
        if re.fullmatch(r"repos/[^/]+/[^/]+/actions/runs", path):
            return self._runs(query)
        if match := re.fullmatch(r"repos/[^/]+/[^/]+/actions/runs/(\d+)/artifacts", path):
            return self._artifacts(int(match[1]), query)
        if re.fullmatch(r"repos/[^/]+/[^/]+/actions/workflows", path):
            return {"total_count": 0, "workflows": []}  # load_ci 的第二段查询（夹具无 auto-merge 工作流）
        raise AssertionError(f"FakeGh 未配置的路由：{path}")

    def _pull(self, number: str) -> dict:
        pull = self.pulls.get(int(number))
        if pull is None:
            if self.missing_pr == "key":
                return self.pulls[int(number)]  # 保留 equivalent_head 的 KeyError 语义
            raise RuntimeError(f"HTTP 404: Not Found（夹具无 PR {number}）")
        return pull

    def _commit(self, sha: str) -> dict:
        if self.missing_commit == "fixed":
            return {"parents": [{"sha": "p1"}, {"sha": "p2"}]}  # test_audit_events：任何提交都是双父节点
        commit = self.commits.get(sha)
        if commit is None:
            if self.missing_commit == "key":
                return self.commits[sha]  # KeyError
            raise RuntimeError(f"HTTP 404: Not Found（夹具无提交 {sha[:12]}）")
        return commit

    def _single_comment(self, comment_id: int) -> dict:
        comment = next((item for item in self.comments if item.get("id") == comment_id), None)
        if comment is None:
            if self.missing_comment == "stop":
                return next(item for item in self.comments if item["id"] == comment_id)  # StopIteration
            raise RuntimeError(f"HTTP 404: Not Found（夹具无评论 {comment_id}）")
        return comment

    def _runs(self, query: str) -> dict:
        items = self.runs
        if self.runs_filter_by_branch:
            branch = urllib.parse.unquote(urllib.parse.parse_qs(query).get("branch", [""])[0])
            items = [item for item in items if not branch or item.get("head_branch") == branch]
        if self.paginate_runs:
            return {"total_count": len(items), "workflow_runs": self._page(items, query)}
        return {"total_count": len(items), "workflow_runs": items}

    def _artifacts(self, run_id: int, query: str) -> dict:
        items = self.artifacts.get(run_id, [])
        if self.paginate_artifacts:
            return {"total_count": len(items), "artifacts": self._page(items, query)}
        return {"total_count": len(items), "artifacts": items}

    def _write(self, route: str, payload) -> dict:
        """锚点评论 POST：记录进 writes 并追加到 comments；需要 PATCH 的子类自行覆盖本方法。"""
        path = route.partition("?")[0]
        if re.fullmatch(r"repos/[^/]+/[^/]+/issues/\d+/comments", path):
            self.writes.append(("POST", route, payload))
            comment = {"id": 9000 + len(self.comments), "body": payload["body"], "created_at": TS,
                       "html_url": f"{GITHUB_URL}/issues/comments/{9000 + len(self.comments)}"}
            self.comments.append(comment)
            return comment
        raise AssertionError(f"FakeGh 不允许的写路由：{route}")

    def download(self, url: str) -> bytes:
        self.calls.append(("download", url))
        return self.downloads[url]

    def _page(self, items: list, query: str) -> list:
        params = urllib.parse.parse_qs(query)
        page = int(params.get("page", ["1"])[0])
        requested = int(params.get("per_page", ["30"])[0])
        cap = min(self.page_size or requested, requested)
        return items[(page - 1) * cap:page * cap]
