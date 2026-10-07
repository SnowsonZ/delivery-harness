"""T718 gh --json 字段合约测试（B116）：真实 gh 客户端只用该命令真实存在的 JSON 字段。

病灶：`gh pr view` 没有 `baseRepository` 字段（GraphQL 有、gh 的 JSON 字段列表没有，gh 2.92.0 报
`Unknown JSON field`），`load_ci` 在第一步就失败。本文件三道防线：

1. PrQueryTest：PATH 上的假 gh（逐字段校验 `pr view` 快照，非法字段输出 `Unknown JSON field` 并
   退出 1）驱动真实的 `events_io.GhClient.pr` 与 `ledger.GhClient.pr`，不经过任何桩。
2. FieldContractTest：用 ast 遍历 engine/ 全部 `--json` 调用点，按实际命令（`--json` 之前的
   `pr|issue|run|repo` 加 `view|list|checks`）校验每个字段都在快照里；无法解析的调用点必须
   失败并列出 文件:行，只能登记 UNRESOLVED_OK 白名单（逐项理由）放行。
3. 快照正反例：对校验函数自己断言跨命令字段（`nameWithOwner` 套 `pr view`、`headSha` 套
   `pr checks`）会被检出，证明没有「别的命令接受就放行」。

字段快照来源与取法（gh 2.92.0，2026-04-28 发布）：离线命令
`GH_TOKEN=fake gh <命令> --json __nope__`（无需网络与真实凭据），stderr 的
`Unknown JSON field: "__nope__"` 后跟 `Available fields:` 列表；`issue view` 需要占位参数
（`gh issue view 1 --json __nope__`）。七个命令：pr view、pr list、pr checks、issue view、
issue list、run list、repo view。gh 升级后字段集可能变化：有新字段时先加快照再加用法，
被移除的字段会由本测试在调用点上暴露。
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine.core import events_io
from engine.reports import ledger

ENGINE_DIR = Path(__file__).resolve().parents[1] / "engine"
PR_NUMBER = 181
BRANCH, HEAD = "task/t718-ci-events", "c" * 40
REPO_URL = f"https://github.com/owner/repo/pull/{PR_NUMBER}"

def _fields(text: str) -> frozenset[str]:
    """空白分隔的字段清单 → frozenset（保持与 gh 输出一致的逐字段可读形式）。"""
    return frozenset(text.split())


# gh 2.92.0 各命令的 JSON 字段快照（取法见模块 docstring）；键是（命令, 子命令）。
AVAILABLE_FIELDS = {
    ("pr", "view"): _fields("""
        additions assignees author autoMergeRequest baseRefName baseRefOid body changedFiles
        closed closedAt closingIssuesReferences comments commits createdAt deletions files
        fullDatabaseId headRefName headRefOid headRepository headRepositoryOwner id
        isCrossRepository isDraft labels latestReviews maintainerCanModify mergeCommit
        mergeStateStatus mergeable mergedAt mergedBy milestone number potentialMergeCommit
        projectCards projectItems reactionGroups reviewDecision reviewRequests reviews state
        statusCheckRollup title updatedAt url
    """),
    ("pr", "list"): _fields("""
        additions assignees author autoMergeRequest baseRefName baseRefOid body changedFiles
        closed closedAt closingIssuesReferences comments commits createdAt deletions files
        fullDatabaseId headRefName headRefOid headRepository headRepositoryOwner id
        isCrossRepository isDraft labels latestReviews maintainerCanModify mergeCommit
        mergeStateStatus mergeable mergedAt mergedBy milestone number potentialMergeCommit
        projectCards projectItems reactionGroups reviewDecision reviewRequests reviews state
        statusCheckRollup title updatedAt url
    """),
    ("pr", "checks"): _fields("""
        bucket completedAt description event link name startedAt state workflow
    """),
    ("issue", "view"): _fields("""
        assignees author body closed closedAt closedByPullRequestsReferences comments createdAt
        id isPinned labels milestone number projectCards projectItems reactionGroups state
        stateReason title updatedAt url
    """),
    ("issue", "list"): _fields("""
        assignees author body closed closedAt closedByPullRequestsReferences comments createdAt
        id isPinned labels milestone number projectCards projectItems reactionGroups state
        stateReason title updatedAt url
    """),
    ("run", "list"): _fields("""
        attempt conclusion createdAt databaseId displayTitle event headBranch headSha name
        number startedAt status updatedAt url workflowDatabaseId workflowName
    """),
    ("repo", "view"): _fields("""
        archivedAt assignableUsers codeOfConduct contactLinks createdAt defaultBranchRef
        deleteBranchOnMerge description diskUsage forkCount fundingLinks hasDiscussionsEnabled
        hasIssuesEnabled hasProjectsEnabled hasWikiEnabled homepageUrl id isArchived
        isBlankIssuesEnabled isEmpty isFork isInOrganization isMirror isPrivate
        isSecurityPolicyEnabled isTemplate isUserConfigurationRepository issueTemplates issues
        labels languages latestRelease licenseInfo mentionableUsers mergeCommitAllowed
        milestones mirrorUrl name nameWithOwner openGraphImageUrl owner parent primaryLanguage
        projects projectsV2 pullRequestTemplates pullRequests pushedAt rebaseMergeAllowed
        repositoryTopics securityPolicyUrl squashMergeAllowed sshUrl stargazerCount
        templateRepository updatedAt url usesCustomOpenGraphImage viewerCanAdminister
        viewerDefaultCommitEmail viewerDefaultMergeMethod viewerHasStarred viewerPermission
        viewerPossibleCommitEmails viewerSubscription visibility watchers
    """),
}
COMMANDS = frozenset(AVAILABLE_FIELDS)

# 无法解析的调用点白名单：键 "相对路径:行号" → 逐项理由。当前为空——设计方实测 34 处调用点
# 全部可解析；再引入解析不了的 gh 调用点时，先消除（写成常量/显式命令），确属必要的才登记。
UNRESOLVED_OK: dict[str, str] = {}


def unknown_fields(command: tuple[str, str], field_string: str) -> list[str]:
    """字段串里不在该命令快照中的字段，按原顺序；命令本身不在快照里时整串未知。"""
    fields = AVAILABLE_FIELDS.get(command)
    if fields is None:
        return [name for name in field_string.split(",") if name]
    return [name for name in field_string.split(",") if name not in fields]


def _flatten(node: ast.expr, constants: dict[str, list[str | None]]) -> list[str | None] | None:
    """把表达式按顺序摊平为字符串常量序列；不可静态求值的部分用 None 占位，摊不开返回 None。

    支持字符串常量、列表/元组、`+` 拼接（PR_FIELDS + ",state"）、模块级字符串常量（PR_FIELDS）、
    `*` 展开、str(pr) 之类的调用与 f-string（占位）。空序列参与拼接。
    """
    if isinstance(node, ast.Constant):
        return [node.value if isinstance(node.value, str) else None]
    if isinstance(node, ast.Name):
        return constants.get(node.id, [None])  # 模块级常量展开；局部变量占位
    if isinstance(node, ast.Starred):
        return _flatten(node.value, constants)
    if isinstance(node, (ast.List, ast.Tuple)):
        items: list[str | None] = []
        for element in node.elts:
            part = _flatten(element, constants)
            if part is None:
                return None
            items += part
        return items
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _flatten(node.left, constants), _flatten(node.right, constants)
        if left is None or right is None:
            return None
        if not left or not right:
            return left + right
        if len(left) == 1 and len(right) == 1 and isinstance(left[0], str) and isinstance(right[0], str):
            return [left[0] + right[0]]
        return None
    if isinstance(node, ast.Call):
        return [None] * (len(node.args) + len(node.keywords))
    if isinstance(node, ast.IfExp):
        left, right = _flatten(node.body, constants), _flatten(node.orelse, constants)
        return None if left is None or right is None else [None] * (len(left) + len(right))
    return [None]  # f-string、dict、比较等：一个占位


def _module_constants(tree: ast.Module) -> dict[str, list[str | None]]:
    """模块级赋值里的可摊平常量（只看顶层 Assign，值本身也必须是可摊平的字符串序列）。"""
    constants: dict[str, list[str | None]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = _flatten(node.value, constants)
            if value is not None:
                constants[node.targets[0].id] = value
    return constants


def _carries_json_flag(node: ast.Call) -> bool:
    return any(isinstance(sub, ast.Constant) and sub.value == "--json" for sub in ast.walk(node))


def _is_shellish_call(node: ast.Call) -> bool:
    """排除已知非 gh 调用：argparse 的 add_argument 与 str.startswith 也会出现 "--json" 字符串。"""
    func = node.func
    return not (isinstance(func, ast.Attribute) and func.attr in {"add_argument", "startswith"})


def gh_json_call_sites(root: Path) -> tuple[list[tuple[str, int, tuple[str, str], str]],
                                            list[tuple[str, int, str]]]:
    """扫描目录下全部 .py 的 gh --json 调用点 →（(相对路径, 行号, 命令, 字段串) 列表, 无法解析列表）。"""
    call_sites: list[tuple[str, int, tuple[str, str], str]] = []
    unresolved: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = _module_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _carries_json_flag(node) or not _is_shellish_call(node):
                continue
            sequence: list[str | None] = []
            for argument in node.args:
                part = _flatten(argument, constants)
                if part is None:
                    sequence = None
                    break
                sequence += part
            if sequence is None:
                unresolved.append((path.relative_to(root).as_posix(), node.lineno, "参数摊不开"))
                continue
            try:
                index = sequence.index("--json")
            except ValueError:
                continue  # "--json" 出现在嵌套表达式里，本调用不直接带该参数
            field_string = sequence[index + 1] if index + 1 < len(sequence) else None
            if not isinstance(field_string, str) or not re.fullmatch(r"[A-Za-z0-9_,]+", field_string):
                unresolved.append((path.relative_to(root).as_posix(), node.lineno,
                                   f"字段串解析不出常量：{field_string!r}"))
                continue
            pairs = [(sequence[i], sequence[i + 1]) for i in range(index - 1)
                     if (sequence[i], sequence[i + 1]) in COMMANDS]
            if len(pairs) != 1:
                unresolved.append((path.relative_to(root).as_posix(), node.lineno,
                                   f"命令识别不出（{pairs} 个候选）"))
                continue
            call_sites.append((path.relative_to(root).as_posix(), node.lineno, pairs[0], field_string))
    return call_sites, unresolved


class _FakeGh:
    """PATH 上的假 gh：逐字段校验 `pr view` 的 --json 快照，非法字段按 gh 2.92.0 的话术拒绝。

    收到的完整参数追加到 $FAKE_GH_LOG（一行一次调用），stdout 输出 $FAKE_GH_RESPONSE 原文。
    校验列表由 $FAKE_PR_FIELDS 提供（逗号分隔），避免把快照复制进 shell。
    """

    SCRIPT = r"""#!/bin/bash
printf '%s\n' "$*" >> "$FAKE_GH_LOG"
fields=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "--json" ]; then fields="$arg"; fi
  prev="$arg"
done
if [ -n "$fields" ]; then
  IFS=',' read -r -a names <<< "$fields"
  for name in "${names[@]}"; do
    case ",$FAKE_PR_FIELDS," in
      *",$name,"*) ;;
      *) echo "Unknown JSON field: \"$name\"" >&2; exit 1 ;;
    esac
  done
fi
printf '%s' "$FAKE_GH_RESPONSE"
"""

    def __init__(self, directory: Path, *, response: str, serial: int):
        self.bin_dir = directory / f"fake-gh-bin-{serial}"
        self.bin_dir.mkdir()
        self.log = directory / f"fake-gh-calls-{serial}.log"
        script = self.bin_dir / "gh"
        script.write_text(self.SCRIPT, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.environ = {
            "FAKE_GH_LOG": str(self.log),
            "FAKE_PR_FIELDS": ",".join(sorted(AVAILABLE_FIELDS[("pr", "view")])),
            "FAKE_GH_RESPONSE": response,
        }

    def install(self):
        original = os.environ.get("PATH")
        os.environ["PATH"] = f"{self.bin_dir}{os.pathsep}{original}"
        os.environ.update(self.environ)
        return original


def pr_response(url) -> str:
    """pr view 的假响应：headRefName/headRefOid 固定，url 按用例给（None 表示键缺失）。"""
    data = {"headRefName": BRANCH, "headRefOid": HEAD}
    if url is not None:
        data["url"] = url
    return json.dumps(data)


class PrQueryTest(unittest.TestCase):
    """验收 1、2：真实的两处 GhClient.pr 在假 gh 上只用合法字段，并从 url 解析仓库名。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-gh-json-fields-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._serial = 0

    def query_with_fake_gh(self, client_factory, url) -> tuple[dict, str]:
        """在 PATH 假 gh 下运行真实客户端的 pr()：返回（结果, 记录到的调用参数行）。"""
        self._serial += 1
        fake = _FakeGh(self.tmp, response=pr_response(url), serial=self._serial)
        previous_path = fake.install()
        self.addCleanup(setattr, os, "PATH", previous_path)
        for key in fake.environ:
            self.addCleanup(os.environ.pop, key, None)
        result = client_factory().pr(PR_NUMBER)  # 不经过任何桩：走 PATH 上的假 gh 子进程
        lines = fake.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1, "pr() 恰好发起一次 gh 调用")
        return result, lines[0]

    def test_both_clients_use_valid_fields_and_derive_repository_from_url(self):
        clients = (("events_io", lambda: events_io.GhClient()), ("ledger", lambda: ledger.GhClient()))
        for label, factory in clients:
            with self.subTest(client=label):
                result, call = self.query_with_fake_gh(factory, REPO_URL)
                self.assertEqual(call,
                                 f"pr view {PR_NUMBER} --json headRefName,headRefOid,url")
                self.assertNotIn("baseRepository", call, "记录到的 --json 字段不得含 baseRepository")
                self.assertEqual(result, {"headRefName": BRANCH, "headRefOid": HEAD,
                                          "repository": "owner/repo"})

    def test_repository_parsing_forms(self):
        forms = [
            ("github.com", REPO_URL, "owner/repo"),
            ("企业版主机", "https://git.example.test/o/r/pull/7", "o/r"),
            ("缺 url", None, None),
            ("url 不含 /pull/", "https://github.com/o/r/wiki", None),
            ("非字符串", 123, None),
        ]
        clients = (("events_io", lambda: events_io.GhClient()), ("ledger", lambda: ledger.GhClient()))
        for label, url, expected in forms:
            for client_label, factory in clients:
                with self.subTest(form=label, client=client_label):
                    result, _ = self.query_with_fake_gh(factory, url)
                    self.assertEqual(result["repository"], expected)
                    self.assertEqual(result["headRefName"], BRANCH)  # 其余键不受 url 影响


class _RunsStub:
    """load_ci 的 gh 桩：pr 视图固定，runs 走分页 API，artifacts 只记录查询路线（无包 → 有发现）。"""

    def __init__(self, runs: list[dict]):
        self.runs = runs
        self.artifact_routes: list[str] = []

    def pr(self, pr: int) -> dict:
        return {"headRefName": BRANCH, "headRefOid": HEAD, "repository": "owner/repo"}

    def api(self, route: str):
        if "/artifacts?" in route:
            self.artifact_routes.append(route)
            return {"total_count": 0, "artifacts": []}
        if "/actions/runs?" in route:
            return {"total_count": len(self.runs), "workflow_runs": self.runs}
        raise AssertionError(f"未预期的 API 路线：{route}")

    def download(self, url: str) -> bytes:
        raise AssertionError(f"不应下载：{url}")


class LoadCiRestShapeTest(unittest.TestCase):
    """验收 4：load_ci 按 REST 字段名（head_sha）匹配运行；命令行写法（headSha）一个也不匹配。"""

    def test_matches_runs_by_rest_head_sha(self):
        stub = _RunsStub([
            {"id": 9100, "run_attempt": 1, "head_sha": HEAD, "path": ".github/workflows/harness.yml"},
            {"id": 9101, "run_attempt": 1, "head_sha": "e" * 40, "path": ".github/workflows/harness.yml"},
        ])
        result = events_io.load_ci(PR_NUMBER, gh=stub)
        self.assertEqual(stub.artifact_routes,
                         ["repos/owner/repo/actions/runs/9100/artifacts?per_page=100&page=1"],
                         "head 匹配的运行按 REST 形状被处理并查询 artifacts")
        mismatched = [item for item in result["findings"] if item["code"] == "head_mismatch"]
        self.assertEqual(len(mismatched), 1)
        self.assertIn("1 个其他 head", mismatched[0]["detail"])
        # 旧形状：gh 命令行写法的 headSha 在 REST 响应里不存在，所有运行都是 None head，一个也匹配不上
        legacy = _RunsStub([
            {"id": 9100, "run_attempt": 1, "headSha": HEAD, "path": ".github/workflows/harness.yml"},
            {"id": 9101, "run_attempt": 1, "headSha": "e" * 40, "path": ".github/workflows/harness.yml"},
        ])
        result = events_io.load_ci(PR_NUMBER, gh=legacy)
        self.assertEqual(legacy.artifact_routes, [], "旧形状下没有任何运行被处理")
        mismatched = [item for item in result["findings"] if item["code"] == "head_mismatch"]
        self.assertEqual(len(mismatched), 1)
        # 全部运行都落到 None head，去重后只报一次；关键是一个都导入不了
        self.assertIn("1 个其他 head", mismatched[0]["detail"])


class FieldContractTest(unittest.TestCase):
    """验收 3：engine/ 全部 --json 调用点按实际命令校验字段；无法解析的必须显式登记。"""

    def test_detector_flags_cross_command_fields(self):
        # 正反例打在校验函数自己身上：字段被别的命令接受不算数。
        self.assertEqual(unknown_fields(("pr", "view"), "nameWithOwner"), ["nameWithOwner"])
        self.assertEqual(unknown_fields(("pr", "checks"), "headSha"), ["headSha"])
        self.assertEqual(unknown_fields(("issue", "view"), "url,nameWithOwner"),
                         ["nameWithOwner"])
        # 反向：字段在自己的命令里必须放行（不是一律拒绝），含拼接后的串。
        self.assertEqual(unknown_fields(("run", "list"), "headSha,status,event"), [])
        self.assertEqual(unknown_fields(("pr", "view"), "headRefName,headRefOid,url"), [])
        self.assertEqual(unknown_fields(("repo", "view"), "nameWithOwner"), [])
        # baseRepository 不得在任何命令的快照里（病灶字段，防止被顺手加进快照放行）。
        everywhere = set().union(*AVAILABLE_FIELDS.values())
        self.assertNotIn("baseRepository", everywhere)

    def test_every_json_call_site_uses_fields_valid_for_its_own_command(self):
        call_sites, unresolved = gh_json_call_sites(ENGINE_DIR)
        listed = [f"{path}:{line}（{reason}）" for path, line, reason in unresolved
                  if f"{path}:{line}" not in UNRESOLVED_OK]
        self.assertEqual(listed, [], f"以下 gh --json 调用点无法静态解析，须改为可解析写法"
                                     f"或登记 UNRESOLVED_OK（附理由）：{listed}")
        stale = [key for key in UNRESOLVED_OK
                 if key not in {f"{path}:{line}" for path, line, _ in unresolved}]
        self.assertEqual(stale, [], f"白名单条目已不再对应任何调用点，请删除：{stale}")
        problems = []
        for path, line, command, field_string in call_sites:
            bad = unknown_fields(command, field_string)
            if bad:
                problems.append(f"{path}:{line} {command[0]} {command[1]} 非法字段 {bad}")
        self.assertEqual(problems, [], f"以下 gh --json 调用点用了该命令不存在的字段"
                                       f"（快照 gh 2.92.0）：{problems}")
        counted: dict[tuple[str, str], int] = {}
        for _, _, command, _ in call_sites:
            counted[command] = counted.get(command, 0) + 1
        self.assertEqual(sorted(counted), sorted(COMMANDS),
                         "七个命令的调用点都应被扫描到（命令集变了要同步快照）")


if __name__ == "__main__":
    unittest.main()
