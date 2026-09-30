"""GitHub 封装：推送与写操作以 Agent 身份经 bin/as-agent（T123 自 dispatch.py 逐字抽离，行为不变）。"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from engine.core.common import ROOT, ci_workflows

CI_QUERY_ATTEMPTS = 3  # 等 CI 时对 gh 查询的最多尝试次数
CI_QUERY_RETRY_SECONDS = 5
# B71：gh run list 列出运行前要先拉取仓库的 Actions workflows 列表，该步 TLS/EOF 等网络瞬断
# 会以 "couldn't fetch workflows" 报错文本退出（T118/T204 两例实测派发进程正死在此步）。
# 这类失败与普通 gh 瞬断同一级别，一并纳入 _ci_runs 的重试；文本样本供测试与排障对照。
CI_RETRIABLE_ERROR_MARKERS = ("couldn't fetch workflows",)


def _ci_runs_retriable(error: BaseException) -> bool:
    """gh 查询失败是否按瞬断重试。gh 包装层失败（_run 统一转成 RuntimeError，stderr 文本可能含
    CI_RETRIABLE_ERROR_MARKERS 之一——如 couldn't fetch workflows——或 EOF/TLS 等底层网络错误）
    与返回体解析失败都算瞬断；其余异常类型不是本查询的失败形态，不重试。"""
    return isinstance(error, (RuntimeError, json.JSONDecodeError))


# ---- GitHub（推送与写操作以 Agent 身份经 bin/as-agent） ----

class GitHub:
    def __init__(self, root: Path = ROOT):
        self.root = root
        self.as_agent = [str(root / "bin" / "as-agent")]

    def _run(self, argv: list[str], cwd: Path | None = None, agent: bool = False, stdin: str | None = None) -> str:
        result = subprocess.run((self.as_agent if agent else []) + argv, cwd=cwd or self.root, input=stdin,
                                capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"{' '.join(argv[:3])} 失败：{result.stderr.strip()[-500:]}")
        return result.stdout.strip()

    def remote_branch_exists(self, branch: str) -> bool:
        return bool(self._run(["git", "ls-remote", "--heads", "origin", branch]))

    def push(self, slot: Path, branch: str) -> None:
        self._run(["git", "push", "--quiet", "-u", "origin", branch], cwd=slot, agent=True)

    # 正文一律经 stdin（--body-file -），不进命令行（规范 §2：不在引号里写 Markdown）。
    def open_pr(self, slot: Path, branch: str, title: str, body: str) -> int:
        url = self._run(["gh", "pr", "create", "--base", "main", "--head", branch, "--title", title,
                         "--body-file", "-"], cwd=slot, agent=True, stdin=body)
        return int(url.rstrip("/").rsplit("/", 1)[-1])

    def existing_pr(self, branch: str) -> int | None:
        """分支已有的开放 PR 编号，没有则 None（B70）：开新 PR 前查询，已有则复用编号不再新建。
        查询失败（如网络断开）按「无既有 PR」处理，保留原新建路径。"""
        try:
            raw = self._run(["gh", "pr", "list", "--head", branch, "--state", "open",
                             "--json", "number", "--limit", "1"])
            return int(json.loads(raw or "[]")[0]["number"])
        except (RuntimeError, TypeError, ValueError, KeyError, IndexError):
            return None

    def comment(self, pr: int, body: str, label: str | None = None) -> str:
        """评论 PR 并返回评论 URL（供观察事件记录；标签逻辑不变）。"""
        url = self._run(["gh", "pr", "comment", str(pr), "--body-file", "-"], agent=True, stdin=body)
        if label:
            self.add_label(pr, label)
        return url

    def _ensure_label(self, label: str) -> None:
        """不存在才创建；不用 --force，已有标签（含登记用标签）不被改写。"""
        try:
            self._run(["gh", "label", "create", label, "--color", "d93f0b"], agent=True)
        except RuntimeError:
            pass  # 已存在

    def add_label(self, pr: int, label: str) -> None:
        self._ensure_label(label)
        self._run(["gh", "pr", "edit", str(pr), "--add-label", label], agent=True)

    def create_issue(self, title: str, body: str, labels: list[str]) -> None:
        for label in labels:
            self._ensure_label(label)
        argv = ["gh", "issue", "create", "--title", title, "--body-file", "-"]
        for label in labels:
            argv += ["--label", label]
        self._run(argv, agent=True, stdin=body)

    def list_comments(self, pr: int) -> list[dict]:
        """分页列出 PR 评论（id 与正文），供告警远端标记去重（B46 T205）。"""
        raw = self._run(["gh", "api", f"repos/{{owner}}/{{repo}}/issues/{pr}/comments", "--paginate", "--slurp"])
        return [item for page in json.loads(raw or "[]") for item in page]

    def edit_comment(self, comment_id: int, body: str) -> None:
        self._run(["gh", "api", "-X", "PATCH", f"repos/{{owner}}/{{repo}}/issues/comments/{comment_id}",
                   "--input", "-"], agent=True, stdin=json.dumps({"body": body}))

    def list_issues(self, label: str) -> list[dict]:
        """带标签的全部议题（编号与正文，最多 200 条）：告警无 PR 时按远端标记查升级议题。"""
        raw = self._run(["gh", "issue", "list", "--state", "all", "--label", label, "--limit", "200",
                         "--json", "number,body"])
        return json.loads(raw or "[]")

    def edit_issue(self, number: int, body: str) -> None:
        self._run(["gh", "issue", "edit", str(number), "--body-file", "-"], agent=True, stdin=body)

    def _ci_runs(self, workflow: str, branch: str) -> list[dict]:
        """查一个工作流在该分支上的运行。gh 瞬时失败（如网络断开，含 workflows 列表拉取失败）连续
        CI_QUERY_ATTEMPTS 次才抛出，避免一次瞬断中止整个派发。"""
        for attempt in range(1, CI_QUERY_ATTEMPTS + 1):
            try:
                return json.loads(self._run(["gh", "run", "list", "--workflow", workflow, "--branch", branch,
                                             "--json", "headSha,status,conclusion,databaseId,url", "--limit", "10"]))
            except Exception as error:  # 瞬断分类收敛在 _ci_runs_retriable，非瞬断原样抛出
                if not _ci_runs_retriable(error):
                    raise
                if attempt == CI_QUERY_ATTEMPTS:
                    raise
                time.sleep(CI_QUERY_RETRY_SECONDS)
        return []

    def wait_ci(self, branch: str, sha: str, timeout: float, detail: dict | None = None) -> tuple[bool, str]:
        """等 rules.toml [dispatch] ci_workflows 列出的每个工作流在该提交上跑完；全部成功才算通过。

        detail 非 None 时写入 run_ids（本次结论对应的 Actions 运行 ID），供派发的 ci_wait 观察事件
        使用；返回值与 gh 调用序列保持不变。
        """
        workflows = ci_workflows()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = {}
            for workflow in workflows:
                runs = self._ci_runs(workflow, branch)
                run = next((item for item in runs if item["headSha"] == sha), None)
                if run and run["status"] == "completed":
                    found[workflow] = run
            failed = next((run for run in found.values() if run["conclusion"] != "success"), None)
            if failed:
                log = subprocess.run(["gh", "run", "view", str(failed["databaseId"]), "--log-failed"], cwd=self.root,
                                     capture_output=True, text=True, check=False).stdout
                if detail is not None:
                    detail["run_ids"] = [failed["databaseId"]]
                return False, f"CI 未通过：{failed['url']}\n\n```\n{log[-3000:]}\n```"
            if len(found) == len(workflows):
                if detail is not None:
                    detail["run_ids"] = sorted(run["databaseId"] for run in found.values())
                return True, ""
            time.sleep(30)
        if detail is not None:
            detail["run_ids"] = []
        return False, f"等待 CI 超过 {int(timeout / 60)} 分钟"
