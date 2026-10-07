"""T720 判定运行关联（B117）：模板 run-name 与 events_judge 的耦合、判定运行的挑选、经 load_ci
的端到端导入、消费者（audit 完整性与合并账本）效果、分页提前停止与请求形状、核对严格性、隔离与
降级、体量与结构。判定运行由 workflow_run 触发、在 API 里归在默认分支，按 PR 分支查询永远查不
到：信任来自默认分支上的工作流定义渲染的运行名（REST display_title），不信任事件包自述；判定包
origin 按该运行自己的 API 记录逐项核对。夹具的判定包经真实 emit + ci_events.export 生成（含
cli.policy 的 main@<短 SHA> 与 route.facts/route.result 的 PR 分支两种 trace），假客户端按 REST
形状提供 PR/运行/工作流/artifact；不碰真实库/PR/工作流，不调用真 gh。
"""

from __future__ import annotations

import io
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine.core import common, events, events_db, events_io, events_judge
from engine.reports import audit_completeness, ci_events, ledger
from tests import test_audit_ledger as ledger_fixture
from tests.test_ci_events_workflows import parse_workflow
from tests.test_gh_json_fields import imports_events_io

ENGINE_DIR = Path(__file__).resolve().parents[1]
TEMPLATE = ENGINE_DIR / "templates" / ".github" / "workflows" / "auto-merge.yml"
TRUSTED = frozenset({".github/workflows/auto-merge.yml", ".github/workflows/auto-merge.yaml",
                     ".github/workflows/harness.yml"})
PR = 187
HEAD = "8f42c2b" + "0" * 33
MAIN_HEAD = "9" * 40
JUDGE_TITLE = events_judge.judge_run_name(PR, HEAD)


def _literal(value: str) -> str:
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"" else value


class RunNameTest(unittest.TestCase):
    """验收第 1 行：模板顶层 run-name 与 events_judge 生成/解析的名字耦合；条件与 judge.if 逐字相同。"""

    @classmethod
    def setUpClass(cls):
        cls.workflow = parse_workflow(TEMPLATE.read_text(encoding="utf-8"))

    def expression(self) -> str:
        body = str(self.workflow["run-name"]).strip()
        self.assertTrue(body.startswith("${{") and body.endswith("}}"), body)
        return body[3:-2].strip()

    def evaluate(self, context: dict[str, str]) -> str:
        """按 GitHub 表达式的与或短路语义求值 run-name（本模板只含 ==、&&、||、format 与字符串）。"""
        left, separator, fallback = self.expression().partition(" || ")
        self.assertTrue(separator, "run-name 缺少「其余情形保持普通名」的兜底分支")
        terms = left.split(" && ")
        for term in terms[:-1]:
            if not self._comparison(term, context):
                return _literal(fallback.strip())
        return self._rendered(terms[-1], context)

    def _comparison(self, term: str, context: dict[str, str]) -> bool:
        field, marker, expected = term.partition("==")
        self.assertTrue(marker, f"run-name 条件项不是比较式：{term}")
        expected = expected.strip()
        value = _literal(expected) if expected[:1] in ("'", '"') else context.get(expected)
        return context.get(field.strip()) == value

    def _rendered(self, term: str, context: dict[str, str]) -> str:
        match = re.fullmatch(r"format\('([^']+)',\s*([^,]+),\s*(.+?)\)", term.strip())
        self.assertIsNotNone(match, f"run-name 的取值不是 format 串：{term}")
        fmt, pr_field, head_field = match[1], match[2].strip(), match[3].strip()
        # 取值字段恰为 workflow_run 载荷里的 PR 号与被评估的 head（与 judge 等 job 在用的变量一致）
        self.assertEqual(pr_field, "github.event.workflow_run.pull_requests[0].number")
        self.assertEqual(head_field, "github.event.workflow_run.head_sha")
        return fmt.format(context[pr_field], context[head_field])

    def test_format_string_matches_events_judge(self):
        # 格式串经示例值（PR 号、40 位小写 SHA）代入后等于 events_judge 生成的名字
        context = {"github.event.workflow_run.event": "pull_request",
                   "github.event.workflow_run.conclusion": "success",
                   "github.event.workflow_run.head_repository.full_name": "owner/repo",
                   "github.repository": "owner/repo",
                   "github.event.workflow_run.pull_requests[0].number": str(PR),
                   "github.event.workflow_run.head_sha": HEAD}
        self.assertEqual(self.evaluate(context), JUDGE_TITLE)
        self.assertEqual(JUDGE_TITLE, f"auto-merge PR #{PR} @ {HEAD}")
        self.assertEqual(events_judge.parse_judge_run_name(JUDGE_TITLE), (PR, HEAD))

    def test_condition_is_judge_if_verbatim(self):
        # run-name 里的条件与 judge.if 逐字相同（从模板文本里取出两处比较，忽略空白）
        condition = self.expression().split(" && format(")[0]
        judge_if = self.workflow["jobs"]["judge"]["if"]
        self.assertEqual(" ".join(condition.split()), " ".join(str(judge_if).split()))

    def test_other_cases_keep_the_plain_name(self):
        # push 触发、上游 CI 失败（judge 被跳过）、来自 fork 的运行：run-name 求值为普通名 auto-merge
        base = {"github.event.workflow_run.pull_requests[0].number": str(PR),
                "github.event.workflow_run.head_sha": HEAD,
                "github.repository": "owner/repo"}
        cases = {
            "push 触发": {**base, "github.event.workflow_run.event": "push",
                        "github.event.workflow_run.conclusion": "success",
                        "github.event.workflow_run.head_repository.full_name": "owner/repo"},
            "上游 CI 失败因而 judge 被跳过": {**base, "github.event.workflow_run.event": "pull_request",
                                          "github.event.workflow_run.conclusion": "failure",
                                          "github.event.workflow_run.head_repository.full_name": "owner/repo"},
            "来自 fork 的运行": {**base, "github.event.workflow_run.event": "pull_request",
                             "github.event.workflow_run.conclusion": "success",
                             "github.event.workflow_run.head_repository.full_name": "forker/repo"},
        }
        for label, context in cases.items():
            with self.subTest(case=label):
                self.assertEqual(self.evaluate(context), "auto-merge")


def api_run(run_id: int, *, title=None, event="workflow_run", path=".github/workflows/auto-merge.yml",
            head_branch="main", head_sha=MAIN_HEAD, attempt=1) -> dict:
    """REST 形状的运行记录（display_title 缺省为该 PR 关联名之外的普通名）。"""
    run = {"id": run_id, "run_attempt": attempt, "event": event, "path": path,
           "head_branch": head_branch, "head_sha": head_sha, "name": "auto-merge"}
    if title is not None:
        run["display_title"] = title
    return run


class SelectTest(unittest.TestCase):
    """验收第 2 行：只有 path 可信、event 为 workflow_run、display_title 整串等于目标名的被挑出。"""

    def collect(self, runs: list[dict]) -> tuple[list[dict], set[str]]:
        return events_judge.select_judge_runs(runs, PR, HEAD, TRUSTED)

    def test_only_exact_matches_are_selected(self):
        matched, stale = self.collect([api_run(1, title=JUDGE_TITLE)])
        self.assertEqual(([run["id"] for run in matched], stale), ([1], set()))

    def test_loose_or_malformed_titles_are_rejected(self):
        other_pr = events_judge.judge_run_name(18, HEAD)
        runs = [api_run(2, title=f"auto-merge PR #18 @ {HEAD}"),           # PR 号前缀相同（#18 对 #187）
                api_run(3, title=other_pr),                                # 另一个 PR 号
                api_run(4, title=events_judge.judge_run_name(1870, HEAD)),  # PR 号前缀相同（#1870 对 #187）
                api_run(4, title=f"auto-merge PR #{PR} @ {HEAD.upper()}"),  # 大写十六进制
                api_run(5, title=f"auto-merge PR #{PR} @ {HEAD[:7]}"),      # 短 SHA
                api_run(6, title=f"  {JUDGE_TITLE}  "),                     # 前后多空白
                api_run(7, title=f"{JUDGE_TITLE}\n"),                       # 尾随换行
                api_run(8),                                                 # display_title 缺失
                api_run(9, title=187),                                      # display_title 非字符串
                api_run(10, title=JUDGE_TITLE, event="push"),               # event 不是 workflow_run
                api_run(12, title=JUDGE_TITLE, path=".github/workflows/evil.yml"),  # path 不可信
                api_run(13, title=JUDGE_TITLE, head_sha=HEAD, event=None),
                api_run(14, title=f"auto-merge PR #0{PR} @ {HEAD}"),         # 评审 201：PR 号前导零（整数化后相等）
                api_run(15, title=f"auto-merge PR #00{PR} @ {HEAD}")]
        matched, stale = self.collect(runs)
        self.assertEqual((matched, stale), ([], set()))

    def test_parse_rejects_prefix_and_suffix_text(self):
        # 变异 S1：解析用子串搜索会放过前缀；select 的整串相等检查另有一层，所以解析本身要直接断言
        self.assertEqual(events_judge.parse_judge_run_name(JUDGE_TITLE), (PR, HEAD))
        for title in (f"x{JUDGE_TITLE}", f" {JUDGE_TITLE}", f"{JUDGE_TITLE} ", f"{JUDGE_TITLE}\n", f"{JUDGE_TITLE}x"):
            with self.subTest(title=title):
                self.assertIsNone(events_judge.parse_judge_run_name(title))

    def test_same_pr_other_head_counts_as_stale(self):
        other = "d" * 40
        # 非规范写法（前导零）的「其他 head」同样不计入 stale，更不会被选中
        matched, stale = self.collect([api_run(20, title=f"auto-merge PR #0{PR} @ {other}", head_sha=other)])
        self.assertEqual((matched, stale), ([], set()))
        matched, stale = self.collect([api_run(21, title=events_judge.judge_run_name(PR, other), head_sha=other),
                                       api_run(22, title=events_judge.judge_run_name(999, other), head_sha=other)])
        self.assertEqual(([run["id"] for run in matched], stale), ([], {other}))


# ---- 夹具：真实 emit + ci_events.export 生成的事件包与 load_ci 的 gh 桩 ----

REPO = "owner/repo"
GITHUB_URL = f"https://github.com/{REPO}.git"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
PR_BRANCH = "task/720-judge-runs"
PR_NUMBER = 187
JUDGE_RUN_ID = 8000
CREATED_AT = "2026-03-10T00:00:00Z"  # PR 创建时间（提前停止的基准，cutoff 为其前一天）
TS = "2026-03-04T05:06:07Z"
_SERIAL = itertools.count()


def canonical(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def zip_bytes(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def git(*args: str, cwd: Path) -> str:
    env = {**{key: value for key, value in os.environ.items() if not key.startswith("GIT_")}, **GIT_ENV}
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False, env=env)
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败：{done.stderr}")
    return done.stdout.strip()


class _Clock:
    """递增冻结时钟：从 2026-01-02T00:01Z 起每次调用前进一分钟，事件 ts 严格递增且可解析。"""

    def __init__(self):
        self.step = 0

    def __call__(self) -> str:
        self.step += 1
        moment = datetime(2026, 1, 2, tzinfo=UTC) + timedelta(minutes=self.step)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def builder_repo(root: Path, branch: str = PR_BRANCH) -> tuple[Path, str, str]:
    """新建 builder 仓库：main 一个提交（判定运行的 head）+ 任务分支一个提交（被评估的 PR head）。"""
    repo = root / f"builder-{next(_SERIAL)}"
    repo.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "README.md").write_text("# builder\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "main", cwd=repo)
    git("remote", "add", "origin", GITHUB_URL, cwd=repo)
    git("checkout", "-q", "-b", branch, cwd=repo)
    (repo / "change.txt").write_text("change\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "branch", cwd=repo)
    return repo, git("rev-parse", branch, cwd=repo), git("rev-parse", "main", cwd=repo)


def _export_package(repo: Path, checkout: str, env: dict[str, str], emits: list[dict]) -> tuple[str, bytes]:
    """检出 checkout 后以给定 Actions 身份 emit 事件并经真实 ci_events.export 生成事件包 zip。"""
    git("checkout", "-q", "--detach", checkout, cwd=repo)
    head = git("rev-parse", "HEAD", cwd=repo)
    target = repo.parent / f"export-{next(_SERIAL)}"
    with mock.patch.object(events_db, "ROOT", repo):
        for key, value in env.items():
            os.environ[key] = value
        try:
            for item in emits:
                assert events.emit(**item) is not None, item
            assert ci_events.export_main(["--target", str(target)]) == 0
        finally:
            for key in env:
                os.environ.pop(key, None)
    name = f"harness-events-{env['GITHUB_RUN_ID']}-{env['GITHUB_RUN_ATTEMPT']}-{env['GITHUB_JOB']}"
    content = (target / "harness-events.json").read_text(encoding="utf-8")
    return head, zip_bytes({f"{name}/harness-events.json": content})


def branch_package(root: Path, *, run_id: int = 9001, branch: str = PR_BRANCH) -> tuple[str, bytes, str]:
    """harness 的分支运行事件包：真实 emit + export，trace 为 PR 分支。返回（PR head, zip, job）。"""
    repo, pr_head, _ = builder_repo(root, branch)
    head, package = _export_package(
        repo, pr_head,
        {"CI": "true", "GITHUB_HEAD_REF": branch, "GITHUB_REF_NAME": branch,
         "GITHUB_RUN_ID": str(run_id), "GITHUB_RUN_ATTEMPT": "1", "GITHUB_JOB": "verify"},
        [{"stage": "verify", "step": "tests", "status": "ok", "duration_ms": 7}])
    assert head == pr_head
    return pr_head, package, "verify"


def judge_package(root: Path, *, run_id: int = JUDGE_RUN_ID, branch: str = PR_BRANCH,
                  auto_merge: bool = True) -> tuple[str, bytes]:
    """judge 判定包：检出 main（判定运行的 head）、cli.policy 落在 main@<短 SHA>、route.facts/
    route.result 落在 PR 分支——真实形状的两种 trace。返回（判定 head, zip）。"""
    repo, _pr_head, judge_head = builder_repo(root, branch)
    _, package = _export_package(
        repo, "main",
        {"CI": "true", "GITHUB_REF_NAME": "main",
         "GITHUB_RUN_ID": str(run_id), "GITHUB_RUN_ATTEMPT": "1", "GITHUB_JOB": "judge"},
        [{"stage": "route", "step": "cli.policy", "status": "ok", "duration_ms": 5,
          "outputs": {"code": 0}},
         {"stage": "route", "step": "facts", "status": "ok", "trace_id": branch,
          "outputs": {"risk": "R1", "machine_class": "K1", "declared_class": None,
                      "changed_lines": 3, "escapes": 0, "window": 20}},
         {"stage": "route", "step": "result", "status": "ok" if auto_merge else "deny",
          "trace_id": branch, "outputs": {"auto_merge": auto_merge, "audit": False, "approval": "app"},
          "decision": {"by": "policy", "rule": "result",
                       "reason": "自动合并" if auto_merge else "转用户评审"}}])
    return judge_head, package


def judge_workflow(path: str = ".github/workflows/auto-merge.yml", workflow_id: int = 501) -> dict:
    return {"id": workflow_id, "name": "auto-merge", "path": path}


def judge_run_record(run_id: int, pr: int, resolved: str, judge_head: str, *, title: str | None = None,
                     created_at: str | None = None, **extra) -> dict:
    """REST 形状的判定运行记录：display_title 缺省为该 PR 的关联名，head 在默认分支上。"""
    record = {"id": run_id, "run_attempt": 1, "event": "workflow_run",
              "display_title": title if title is not None else events_judge.judge_run_name(pr, resolved),
              "head_branch": "main", "head_sha": judge_head,
              "path": ".github/workflows/auto-merge.yml", "name": "auto-merge"}
    if created_at is not None:
        record["created_at"] = created_at
    record.update(extra)
    return record


def artifact_entry(run_id: int, attempt: int, job: str, *, expired: bool = False,
                   name: str | None = None) -> dict:
    label = name or f"harness-events-{run_id}-{attempt}-{job}"
    return {"id": run_id * 10 + attempt, "name": label, "expired": expired,
            "archive_download_url": f"https://dl/{label}"}


class FakeGh:
    """load_ci 的 gh 桩：PR 视图（可带 createdAt）、分支运行、工作流与其运行列表、artifact 下载；
    calls 记录每次 api 路由供请求形状断言，fail 子串命中即抛错（第二段查询失败用）。"""

    def __init__(self, *, head: str, branch: str = PR_BRANCH, created_at: str | None = CREATED_AT,
                 runs: list | None = None, workflows: list | None = None,
                 workflow_runs: dict | None = None, artifacts: dict | None = None,
                 downloads: dict | None = None, fail: tuple = ()):
        self.calls: list[str] = []
        self.head, self.branch, self.created_at = head, branch, created_at
        self.runs, self.workflows, self.fail = list(runs or []), list(workflows or []), tuple(fail)
        self.workflow_runs = dict(workflow_runs or {})
        self.artifacts = dict(artifacts or {})
        self.downloads = dict(downloads or {})

    def pr(self, pr: int) -> dict:
        return {"headRefName": self.branch, "headRefOid": self.head,
                "repository": REPO, "createdAt": self.created_at}

    def api(self, route: str):
        self.calls.append(route)
        if any(pattern in route for pattern in self.fail):
            raise RuntimeError("HTTP 403: 权限不足（夹具）")
        page = int(urllib.parse.parse_qs(route.split("?", 1)[1])["page"][0])
        items = self._items(route)
        return {"total_count": len(items), self._key(route): items[(page - 1) * 100:page * 100]}

    def _items(self, route: str) -> list:
        if "/artifacts" in route:
            return self.artifacts.get(int(route.split("/actions/runs/")[1].split("/")[0]), [])
        if "/actions/workflows?" in route:
            return self.workflows
        if "/actions/workflows/" in route:
            return self.workflow_runs.get(int(route.split("/actions/workflows/")[1].split("/")[0]), [])
        return self.runs

    @staticmethod
    def _key(route: str) -> str:
        if "/artifacts" in route:
            return "artifacts"
        return "workflows" if "/actions/workflows?" in route else "workflow_runs"

    def download(self, url: str) -> bytes:
        return self.downloads[url]


class JudgeFixture(unittest.TestCase):
    """隔离 events_db.ROOT 的夹具基类：临时仓库、递增冻结时钟、GIT_/GITHUB_/CI 环境清理。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-judge-runs-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        clock = mock.patch.object(events_db, "_now", new=_Clock())
        clock.start()
        self.addCleanup(clock.stop)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in [name for name in os.environ if name.startswith(("GIT_", "GITHUB_", "CI"))]:
            os.environ.pop(key, None)
        for key, value in GIT_ENV.items():
            os.environ[key] = value
        events._warned = False
        self.addCleanup(setattr, events, "_warned", False)

    def use_root(self, name: str = "app") -> Path:
        """新建隔离仓库并把它设为 events_db.ROOT（多次调用叠加，恢复按后进先出）。"""
        repo = self.tmp / f"{name}-{next(_SERIAL)}"
        repo.mkdir()
        git("init", "-q", "-b", "main", cwd=repo)
        (repo / "README.md").write_text("# app\n", encoding="utf-8")
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", "init", cwd=repo)
        patch = mock.patch.object(events_db, "ROOT", repo)
        patch.start()
        self.addCleanup(patch.stop)
        return repo

    def load(self, stub: FakeGh, pr: int = PR_NUMBER, head: str | None = None) -> dict:
        return events_io.load_ci(pr, head=head, gh=stub)

    def world(self, *, pr: int = PR_NUMBER, branch: str = PR_BRANCH, auto_merge: bool = True,
              with_branch_run: bool = True, branch_run_id: int = 9001, judge_run_id: int = JUDGE_RUN_ID,
              workflows: list | None = None, workflow_runs: dict | None = None,
              artifacts: dict | None = None, downloads: dict | None = None,
              fail: tuple = (), created_at: str | None = CREATED_AT, title: str | None = None) -> SimpleNamespace:
        """一整套可用世界：builder 的分支包与判定包 + 配好两级列表与 artifact 的假客户端。"""
        runs, artifacts_map, downloads_map = [], {}, {}
        if with_branch_run:
            resolved, package, job = branch_package(self.tmp, run_id=branch_run_id, branch=branch)
            runs.append({"id": branch_run_id, "run_attempt": 1, "event": "pull_request",
                         "head_branch": branch, "head_sha": resolved,
                         "path": ".github/workflows/harness.yml"})
            artifacts_map[branch_run_id] = [artifact_entry(branch_run_id, 1, job)]
            downloads_map[f"https://dl/harness-events-{branch_run_id}-1-{job}"] = package
        else:
            resolved = builder_repo(self.tmp, branch)[1]
        judge_head, judge_zip = judge_package(self.tmp, run_id=judge_run_id, branch=branch,
                                              auto_merge=auto_merge)
        runs_listing = [judge_run_record(judge_run_id, pr, resolved, judge_head, title=title)]
        stub = FakeGh(head=resolved, branch=branch, created_at=created_at, runs=runs,
                      workflows=[judge_workflow()] if workflows is None else workflows,
                      workflow_runs={501: runs_listing} if workflow_runs is None else workflow_runs,
                      artifacts=artifacts_map, downloads=downloads_map, fail=fail)
        stub.artifacts.setdefault(judge_run_id, [artifact_entry(judge_run_id, 1, "judge")])
        stub.downloads.setdefault(f"https://dl/harness-events-{judge_run_id}-1-judge", judge_zip)
        stub.artifacts.update(artifacts or {})
        stub.downloads.update(downloads or {})
        return SimpleNamespace(stub=stub, resolved=resolved, judge_head=judge_head,
                               branch=branch, pr=pr, judge_run_id=judge_run_id)


class LoadJudgeRunsTest(JudgeFixture):
    """验收第 3 行：判定运行经 load_ci 端到端导入，两种 trace 原样保留，重跑全部跳过。"""

    def test_judge_runs_import_with_both_traces_and_idempotence(self):
        self.use_root()
        world = self.world()
        result = self.load(world.stub)
        self.assertEqual(result["findings"], [])
        self.assertEqual((result["imported"], result["skipped"]), (4, 0))  # 分支包 1 + 判定包 3
        judge_rows = events_io.query(source=f"ci:{JUDGE_RUN_ID}:1:judge")
        # 两种 trace 原样保留：判定主体事件在 PR 分支、cli.policy 在 main@<短 SHA>
        self.assertEqual({row["trace_id"] for row in judge_rows},
                         {PR_BRANCH, f"main@{world.judge_head[:7]}"})
        branch_rows = sorted((row for row in judge_rows if row["trace_id"] == PR_BRANCH),
                             key=lambda row: row["seq"])
        self.assertEqual([row["step"] for row in branch_rows], ["facts", "result"])
        self.assertEqual(branch_rows[-1]["outputs"]["auto_merge"], True)
        self.assertTrue(all(row["source"] == f"ci:{JUDGE_RUN_ID}:1:judge" for row in judge_rows))
        self.assertTrue(events_io.query(trace_id=PR_BRANCH, source="ci:9001:1:verify"))
        self.assertEqual(events_db.verify(), [])
        # 重跑：全部「跳过」、新增为 0
        again = self.load(world.stub)
        self.assertEqual((again["imported"], again["skipped"], again["findings"]), (0, 4, []))


class ConsumerEffectTest(JudgeFixture):
    """验收第 4 行：导入的判定事件装进最小的假审计对象，check 认出受信任 CI 的 route.result。"""

    def findings_for(self, *, auto_merge: bool, with_judge: bool) -> list[str]:
        self.use_root()
        world = self.world(auto_merge=auto_merge, with_branch_run=False)
        if with_judge:
            self.assertEqual(self.load(world.stub)["findings"], [])
        events.emit("merge", "github.merge", "ok", trace_id=world.branch, source=f"github:{world.pr}",
                    outputs={"merger": "harness-merge-app", "merger_type": "Bot", "merged_at": TS,
                             "approver": "harness-merge-app", "approver_type": "Bot",
                             "approval_commit": "d" * 40, "approval_bound": True})
        auditor = SimpleNamespace(event_rows=events_io.query(), stages=[], anchors=[], chains=[],
                                  repo=REPO, pr=world.pr, merge_sha="d" * 40, trace=world.branch)
        config_dir = self.tmp / f"config-{next(_SERIAL)}"
        config_dir.mkdir()
        with mock.patch.object(common, "CONFIG_DIR", config_dir):
            return [finding["rule"] for finding in audit_completeness.check(auditor)]

    def test_route_result_satisfies_missing_route(self):
        # 自动合并案例（合并事件 merger_type=Bot）+ 判定包已导入：不再报 missing_route
        self.assertEqual(self.findings_for(auto_merge=True, with_judge=True), [])
        # route.result 的 auto_merge 为 false：仍报 missing_route
        self.assertEqual(self.findings_for(auto_merge=False, with_judge=True), ["missing_route"])
        # 没导入判定包：仍报 missing_route
        self.assertEqual(self.findings_for(auto_merge=True, with_judge=False), ["missing_route"])


class LedgerEffectTest(ledger_fixture.ObservabilityTaskTest):
    """验收第 5 行：复用判定导入夹具与本文件的账本夹具，真实 build_ledger 收进判定链。"""

    def import_judge(self, project_head: str, branch: str) -> None:
        judge_head, judge_zip = judge_package(self.tmp, branch=branch)
        stub = FakeGh(head=project_head, branch=branch, workflows=[judge_workflow()],
                      workflow_runs={501: [judge_run_record(JUDGE_RUN_ID, 305, project_head, judge_head)]},
                      artifacts={JUDGE_RUN_ID: [artifact_entry(JUDGE_RUN_ID, 1, "judge")]},
                      downloads={f"https://dl/harness-events-{JUDGE_RUN_ID}-1-judge": judge_zip})
        result = events_io.load_ci(305, head=project_head, gh=stub)
        self.assertEqual(result["findings"], [])
        self.assertGreaterEqual(result["imported"], 3)

    def test_judge_events_reach_the_ledger_and_other_trace_does_not(self):
        project, _origin, branch_head, merge_sha, _base = self.build_project()
        branch = ledger_fixture.BRANCH
        self.import_judge(branch_head, branch)
        built = ledger.build_ledger(305, gh=self.platform_gh(head=branch_head, merge_sha=merge_sha),
                                    cwd=project)
        # 判定链与 route.facts/route.result 进入账本
        self.assertIn(f"ci:{JUDGE_RUN_ID}:1:judge",
                      [item["source"] for item in built["sources"] if item["class"] == "ci"])
        steps = {(item["stage"], item["step"]) for item in built["stages"]
                 if item.get("evidence_kind") == "event"}
        self.assertIn(("route", "facts"), steps)
        self.assertIn(("route", "result"), steps)
        # main@<短 SHA> trace 的事件不混入该 PR 的账本
        self.assertEqual({chain["trace_id"] for chain in built["chains"]}, {branch})

    def test_ledger_has_no_route_result_without_judge_package(self):
        project, _origin, branch_head, merge_sha, _base = self.build_project()
        built = ledger.build_ledger(305, gh=self.platform_gh(head=branch_head, merge_sha=merge_sha),
                                    cwd=project)
        steps = {(item["stage"], item["step"]) for item in built["stages"]
                 if item.get("evidence_kind") == "event"}
        self.assertNotIn(("route", "result"), steps)
        self.assertNotIn(f"ci:{JUDGE_RUN_ID}:1:judge", [item["source"] for item in built["sources"]])


class _PagedGh:
    """提前停止测试的分页桩：workflows 一页 + 指定工作流的运行页序列；记录请求路由。"""

    def __init__(self, pages: list[list[dict]], *, created_at: str | None = CREATED_AT,
                 pr_head: str = "c" * 40):
        self.pages = pages
        self.created_at = created_at
        self.pr_head = pr_head
        self.routes: list[str] = []
        self.workflows = [judge_workflow()]

    def pr(self, pr: int) -> dict:
        return {"headRefName": PR_BRANCH, "headRefOid": self.pr_head,
                "repository": REPO, "createdAt": self.created_at}

    def api(self, route: str):
        self.routes.append(route)
        page = int(urllib.parse.parse_qs(route.split("?", 1)[1])["page"][0])
        if "/actions/workflows?" in route:
            return {"total_count": len(self.workflows), "workflows": self.workflows}
        runs = self.pages[page - 1] if page <= len(self.pages) else []
        return {"total_count": sum(len(items) for items in self.pages), "workflow_runs": runs}


class EarlyStopTest(unittest.TestCase):
    """验收第 6 行（追溯第 3a 条）：按 PR 创建时间提前停止——页数有界、坏页禁用、孤立旧页不漏。"""

    def collect(self, stub: _PagedGh, *, pr: int = PR_NUMBER, resolved: str = "c" * 40):
        findings: list[dict] = []
        stale: set[str] = set()
        matched = events_judge.collect_judge_runs(
            stub, repo=REPO, pr=pr, resolved=resolved, trusted=TRUSTED,
            list_all=events_io._list_all, created_at=stub.created_at, stale=stale, findings=findings)
        return matched, stale, findings

    def run_routes(self, stub: _PagedGh) -> list[str]:
        return [route for route in stub.routes if "/actions/workflows/" in route]

    @staticmethod
    def filler(run_id: int, created_at: str | None) -> dict:
        record = {"id": run_id, "run_attempt": 1, "event": "workflow_run", "display_title": "auto-merge",
                  "head_branch": "main", "head_sha": "9" * 40,
                  "path": ".github/workflows/auto-merge.yml"}
        if created_at is not None:
            record["created_at"] = created_at
        return record

    def pages(self, target_page: int, *, fresh_pages: tuple | None = None, grace_pages: tuple = (),
              target_created_at: str | None = "2026-03-10T00:00:00Z",
              bad_pages: tuple = (), total: int = 30) -> list[list[dict]]:
        """total 页运行：fresh_pages 内的页填充运行全为新，其余页填充运行全旧（created_at 早于 cutoff
        一天以上）；缺省 fresh_pages 为目标页之前的所有页；target_page 另含一条目标运行（时间由
        target_created_at 定）；bad_pages 内含坏时间戳（第一页 naive、其后不可解析）。"""
        fresh, stale = "2026-03-10T00:00:00Z", "2026-03-08T00:00:00Z"
        fresh_pages = tuple(range(1, target_page)) if fresh_pages is None else fresh_pages
        pages = []
        run_id = itertools.count(1)
        for page in range(1, total + 1):
            created = fresh if page in fresh_pages else stale
            if page in grace_pages:  # 早于 PR 创建时间但落在一天余量窗口内：不算「旧」
                created = "2026-03-09T12:00:00Z"
            items = [self.filler(next(run_id), created) for _ in range(99)]
            if page == target_page:
                items.insert(0, judge_run_record(JUDGE_RUN_ID, PR_NUMBER, "c" * 40, "9" * 40,
                                                 created_at=target_created_at))
            elif page in bad_pages:
                items[0]["created_at"] = "2026-03-08T00:00:00" if page == bad_pages[0] else "not-a-time"
            pages.append(items)
        return pages

    def test_stops_after_two_stale_pages_and_finds_target(self):
        # 30 页、目标在第 2 页、其后全部早于 PR 创建一天以上：找到目标，且目标页之后恰好再读连续两页旧页即停
        stub = _PagedGh(self.pages(2))
        matched, stale, findings = self.collect(stub)
        self.assertEqual(([run["id"] for run in matched], stale, findings),
                         ([JUDGE_RUN_ID], set(), []))
        self.assertLessEqual(len(self.run_routes(stub)), 4)

    def test_reads_to_the_end_without_created_at(self):
        # pr() 不返回 createdAt：读到底（30 页）、结果相同
        stub = _PagedGh(self.pages(2), created_at=None)
        matched, _stale, findings = self.collect(stub)
        self.assertEqual([run["id"] for run in matched], [JUDGE_RUN_ID])
        self.assertEqual(findings, [])
        self.assertEqual(len(self.run_routes(stub)), 30)

    def test_reads_to_the_end_with_unparseable_created_at(self):
        # pr() 返回的 createdAt 不可解析：同样读到底（不提前停止）、目标仍被找到
        stub = _PagedGh(self.pages(2), created_at="not-a-timestamp")
        matched, _stale, findings = self.collect(stub)
        self.assertEqual([run["id"] for run in matched], [JUDGE_RUN_ID])
        self.assertEqual(findings, [])
        self.assertEqual(len(self.run_routes(stub)), 30)

    def test_bad_timestamp_page_disables_early_stop_permanently(self):
        # 坏时间页之后接连续两页旧运行：仍读到底（永久禁用）、目标仍被找到
        stub = _PagedGh(self.pages(2, bad_pages=(3, 4)))
        matched, _stale, findings = self.collect(stub)
        self.assertEqual([run["id"] for run in matched], [JUDGE_RUN_ID])
        self.assertEqual(findings, [])
        self.assertEqual(len(self.run_routes(stub)), 30)

    def test_isolated_stale_page_does_not_stop(self):
        # 评审 201：新、旧、新、旧、目标页、其后连续旧页。孤立旧页（第 2、4 页）各自被新页隔开：
        # 单页旧就停（第 2 页）会漏目标；不在新页处清零旧页计数（第 2、4 页累计到 2）也会在第 4 页误停。
        pages = self.pages(5, fresh_pages=(1, 3))
        for number in (2, 4):  # 夹具自检：这两页整页都旧
            self.assertTrue(all(item["created_at"] == "2026-03-08T00:00:00Z" for item in pages[number - 1]))
        self.assertTrue(all(item["created_at"] == "2026-03-10T00:00:00Z" for item in pages[2]))  # 第 3 页整页新
        stub = _PagedGh(pages)
        matched, _stale, findings = self.collect(stub)
        self.assertEqual([run["id"] for run in matched], [JUDGE_RUN_ID])
        self.assertEqual(findings, [])
        # 目标页（5）含新运行 → 计数清零；其后第 6、7 页连续旧才停
        self.assertEqual(len(self.run_routes(stub)), 7)

    def test_one_day_grace_window_is_not_stale(self):
        # 变异 E7：去掉一天余量后，窗口内（早于 createdAt、晚于 createdAt − 1 天）的页被当成旧页而提前停止
        pages = self.pages(5, fresh_pages=(1,), grace_pages=(2, 3, 4))
        stub = _PagedGh(pages)
        matched, _stale, findings = self.collect(stub)
        self.assertEqual(([run["id"] for run in matched], findings), ([JUDGE_RUN_ID], []))
        self.assertGreaterEqual(len(self.run_routes(stub)), 5)

    def test_boundary_and_timestamp_forms(self):
        # 页内运行时间恰等于 createdAt − 1 天：不算「早于」、不停止；小数秒与 +00:00 均可解析
        # 第 2 页填充运行全旧，只有目标运行恰在阈值（createdAt − 1 天）上：它不算「早于」，该页不计旧页
        boundary = self.pages(2, target_created_at="2026-03-09T00:00:00Z", fresh_pages=(1,))
        stub = _PagedGh(boundary)
        matched, _stale, findings = self.collect(stub)
        self.assertEqual([run["id"] for run in matched], [JUDGE_RUN_ID])
        self.assertEqual((findings, len(self.run_routes(stub))), ([], 4))  # 第 3、4 页旧才停
        decimal_pages = self.pages(2)
        for record in decimal_pages[2]:
            record["created_at"] = "2026-03-08T00:00:00.123Z"
        for record in decimal_pages[3]:
            record["created_at"] = "2026-03-08T00:00:00+00:00"
        stub = _PagedGh(decimal_pages)
        matched, _stale, findings = self.collect(stub)
        self.assertEqual(([run["id"] for run in matched], findings, len(self.run_routes(stub))),
                         ([JUDGE_RUN_ID], [], 4))


class PaginationAndShapeTest(JudgeFixture):
    """验收第 7 行：1,001 条运行、目标在第 11 页仍被找到并导入；请求不带筛选参数；老仓库与 .yaml。"""

    BANNED = ("event=", "branch=", "status=", "head_sha=", "created=", "actor=", "check_suite_id=")

    def test_1001_runs_target_on_page_11_is_found_and_imported(self):
        self.use_root()
        resolved = builder_repo(self.tmp)[1]
        judge_head, judge_zip = judge_package(self.tmp)
        runs = [{"id": index, "run_attempt": 1, "event": "workflow_run", "display_title": "auto-merge",
                 "head_branch": "main", "head_sha": "9" * 40,
                 "path": ".github/workflows/auto-merge.yml"} for index in range(1000)]
        runs.append(judge_run_record(JUDGE_RUN_ID, PR_NUMBER, resolved, judge_head))  # 第 11 页第 1 条
        stub = FakeGh(head=resolved, created_at=None, workflows=[judge_workflow()],
                      workflow_runs={501: runs},
                      artifacts={JUDGE_RUN_ID: [artifact_entry(JUDGE_RUN_ID, 1, "judge")]},
                      downloads={f"https://dl/harness-events-{JUDGE_RUN_ID}-1-judge": judge_zip})
        result = self.load(stub)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["imported"], 3)
        listing = [route for route in stub.calls if "/actions/workflows/" in route]
        self.assertEqual(len(listing), 11)  # 读到底：11 页（100×10 + 1）
        for route in listing:
            self.assertTrue(route.endswith("per_page=100") or "&page=" in route, route)
            for banned in self.BANNED:
                self.assertNotIn(banned, route, route)

    def test_no_auto_merge_workflow_queries_no_runs(self):
        self.use_root()
        world = self.world(with_branch_run=False,
                           workflows=[{"id": 7, "name": "harness", "path": ".github/workflows/harness.yml"}])
        result = self.load(world.stub)
        self.assertEqual((result["imported"], result["findings"]), (0, []))
        self.assertEqual([route for route in world.stub.calls if "/actions/workflows?" in route],
                         ["repos/owner/repo/actions/workflows?per_page=100&page=1"])
        self.assertFalse([route for route in world.stub.calls if "/actions/workflows/" in route])

    def test_yaml_extension_is_recognized(self):
        self.use_root()
        resolved = builder_repo(self.tmp)[1]
        judge_head, judge_zip = judge_package(self.tmp)
        stub = FakeGh(head=resolved, workflows=[judge_workflow(path=".github/workflows/auto-merge.yaml",
                                                               workflow_id=502)],
                      workflow_runs={502: [judge_run_record(JUDGE_RUN_ID, PR_NUMBER, resolved, judge_head)]},
                      artifacts={JUDGE_RUN_ID: [artifact_entry(JUDGE_RUN_ID, 1, "judge")]},
                      downloads={f"https://dl/harness-events-{JUDGE_RUN_ID}-1-judge": judge_zip})
        result = self.load(stub)
        self.assertEqual((result["imported"], result["findings"]), (3, []))
        self.assertTrue(any("/actions/workflows/502/runs" in route for route in stub.calls))


class StrictVerificationTest(JudgeFixture):
    """验收第 8 行：判定包的 origin/来源/物理名核对仍严格（拿 PR 的 head 核对必然被拒）。"""

    def mutated_world(self, *, mutate_origin: dict | None = None, mutate_event: dict | None = None,
                      artifacts: list | None = None, downloads: dict | None = None,
                      run_extra: dict | None = None) -> SimpleNamespace:
        world = self.world(with_branch_run=False)
        name = f"harness-events-{JUDGE_RUN_ID}-1-judge"
        with zipfile.ZipFile(io.BytesIO(world.stub.downloads[f"https://dl/{name}"])) as archive:
            bundle = json.loads(archive.read(f"{name}/harness-events.json"))
        if mutate_origin:
            bundle["origin"].update(mutate_origin)
        if mutate_event:
            bundle["events"][0].update(mutate_event)
        world.stub.downloads[f"https://dl/{name}"] = zip_bytes({f"{name}/harness-events.json": canonical(bundle)})
        if artifacts is not None:
            world.stub.artifacts[JUDGE_RUN_ID] = artifacts
        if downloads is not None:
            world.stub.downloads.update(downloads)
        for run in world.stub.workflow_runs[501]:
            run.update(run_extra or {})
        return world

    def codes(self, world: SimpleNamespace) -> tuple[list[str], int]:
        self.use_root()
        result = self.load(world.stub)
        return [finding["code"] for finding in result["findings"]], result["imported"]

    def test_origin_checked_against_the_runs_own_api_record(self):
        pr_head = "c" * 40  # 与夹具无关的 PR head：拿它核对判定包必然被拒
        cases = {
            "origin.head_sha 写成 PR 的 head": self.mutated_world(mutate_origin={"head_sha": pr_head}),
            "origin.head_branch 写成 PR 分支": self.mutated_world(mutate_origin={"head_branch": PR_BRANCH}),
            "origin.run_id 不符": self.mutated_world(mutate_origin={"run_id": "7999"}),
            "repository 不符": self.mutated_world(mutate_origin={"repository": "https://github.com/other/repo.git"}),
            "事件 source 不是 ci:<run>:<attempt>:<job>": self.mutated_world(
                mutate_event={"source": f"ci:{JUDGE_RUN_ID}:1:other"}),
            "artifact 物理名与运行不符": self.mutated_world(
                artifacts=[artifact_entry(JUDGE_RUN_ID, 1, "judge", name="harness-events-7999-1-judge")]),
            "artifact 过期": self.mutated_world(artifacts=[artifact_entry(JUDGE_RUN_ID, 1, "judge", expired=True)]),
            "zip 损坏": self.mutated_world(downloads={f"https://dl/harness-events-{JUDGE_RUN_ID}-1-judge": b"not a zip"}),
            "运行 head_sha 不是 40 位十六进制": self.mutated_world(run_extra={"head_sha": "short"}),
            "运行 head_branch 为空": self.mutated_world(run_extra={"head_branch": ""}),
        }
        expected_code = {"origin.head_sha 写成 PR 的 head": "origin_mismatch",
                         "origin.head_branch 写成 PR 分支": "origin_mismatch",
                         "origin.run_id 不符": "origin_mismatch",
                         "repository 不符": "origin_mismatch",
                         "事件 source 不是 ci:<run>:<attempt>:<job>": "source_mismatch",
                         "artifact 物理名与运行不符": "artifact_name",
                         "artifact 过期": "artifact_expired",
                         "zip 损坏": "package_corrupt",
                         "运行 head_sha 不是 40 位十六进制": "api",
                         "运行 head_branch 为空": "api"}
        for label, world in cases.items():
            with self.subTest(case=label):
                codes, imported = self.codes(world)
                self.assertIn(expected_code[label], codes, (label, codes))
                self.assertEqual(imported, 0, label)
                self.assertEqual(events_io.query(source=f"ci:{JUDGE_RUN_ID}:1:judge"), [], label)

    def test_shape_check_skips_before_artifacts_query(self):
        # 运行 API 记录形状不符：记 api 发现并跳过该运行（不再查询 artifacts）
        world = self.mutated_world(run_extra={"head_sha": "short"})
        codes, imported = self.codes(world)
        self.assertEqual((codes, imported), (["api"], 0))
        self.assertFalse([route for route in world.stub.calls if "/artifacts" in route])


class IsolationAndDegradationTest(JudgeFixture):
    """验收第 9 行：隔离（别的 PR、不可信 path、过期 head）与降级（第二段查询失败、无判定运行、
    judge 被跳过、judge 已执行但包缺失）。"""

    def test_other_pr_untrusted_path_and_stale_head_are_isolated(self):
        self.use_root()
        world = self.world(with_branch_run=False)
        other_head = "d" * 40
        world.stub.workflow_runs = {501: [
            judge_run_record(31, 999, "e" * 40, world.judge_head,
                             title=events_judge.judge_run_name(999, "e" * 40)),   # 别的 PR 号：不导入、无发现
            judge_run_record(32, world.pr, "f" * 40, other_head,
                             title=events_judge.judge_run_name(world.pr, "f" * 40)),  # 同 PR 过期 head：一条 head_mismatch
            judge_run_record(33, world.pr, world.resolved, world.judge_head,
                             path=".github/workflows/evil.yml"),                  # path 不可信：不导入
            judge_run_record(34, world.pr, world.resolved, world.judge_head,
                             title="auto-merge"),                                  # 普通名（judge 未执行）：不导入
        ]}
        result = self.load(world.stub)
        self.assertEqual(result["imported"], 0)
        self.assertEqual([finding["code"] for finding in result["findings"]], ["head_mismatch"])
        self.assertIn("1 个其他 head", result["findings"][0]["detail"])
        self.assertEqual(events_io.query(source=f"ci:{JUDGE_RUN_ID}:1:judge"), [])

    def test_second_stage_failure_keeps_branch_results(self):
        for label, fail in (("工作流列表失败", ("/actions/workflows?",)),
                            ("运行列表失败", ("/actions/workflows/501/",))):
            with self.subTest(case=label):
                self.use_root()
                world = self.world(fail=fail)
                result = self.load(world.stub)
                codes = [finding["code"] for finding in result["findings"]]
                # 分支运行已导入的结果保持，只多一条 api 发现，不整体失败
                self.assertEqual(codes.count("api"), 1, (label, result["findings"]))
                self.assertNotIn("head_mismatch", codes)
                self.assertGreaterEqual(result["imported"], 1)
                self.assertTrue(events_io.query(source="ci:9001:1:verify"))
                self.assertEqual(events_db.verify(), [])

    def test_no_judge_runs_and_skipped_judge_produce_nothing(self):
        # 没有任何判定运行：不报错、不新增发现
        self.use_root()
        world = self.world(with_branch_run=False, workflow_runs={501: []},
                           artifacts={JUDGE_RUN_ID: []}, downloads={})
        result = self.load(world.stub)
        self.assertEqual((result["imported"], result["findings"]), (0, []))
        # 同一 PR/head 上游 CI 失败、judge 被跳过的运行（普通名 auto-merge、无事件包）：不被挑出、无发现
        world = self.world(with_branch_run=False,
                           workflow_runs={501: [judge_run_record(41, world.pr, world.resolved,
                                                                 world.judge_head, title="auto-merge")]},
                           artifacts={JUDGE_RUN_ID: []}, downloads={})
        result = self.load(world.stub)
        self.assertEqual((result["imported"], result["findings"]), (0, []))

    def test_missing_package_for_judged_run_is_artifact_missing(self):
        # judge 已执行（关联名）但包缺失：一条 artifact_missing（上游失败而被跳过的运行是普通名，不会到这）
        self.use_root()
        world = self.world(with_branch_run=False, artifacts={JUDGE_RUN_ID: []}, downloads={})
        result = self.load(world.stub)
        self.assertEqual([finding["code"] for finding in result["findings"]], ["artifact_missing"])
        self.assertEqual(result["imported"], 0)


class SizeAndImportTest(unittest.TestCase):
    """验收第 10 行：两个模块的体量棘轮与 events_judge 的无循环导入。"""

    BASELINE_EVENTS_IO = 773  # 本任务派发时 events_io.py 的物理行数（任务书给定的基线）

    def test_line_budgets_and_no_events_io_import(self):
        source = (ENGINE_DIR / "engine" / "core" / "events_io.py").read_text(encoding="utf-8")
        self.assertLessEqual(len(source.splitlines()), 800)
        self.assertLessEqual(len(source.splitlines()) - self.BASELINE_EVENTS_IO, 20)
        judge_source = (ENGINE_DIR / "engine" / "core" / "events_judge.py").read_text(encoding="utf-8")
        self.assertLessEqual(len(judge_source.splitlines()), 800)
        self.assertEqual(imports_events_io(judge_source), [])


if __name__ == "__main__":
    unittest.main()
