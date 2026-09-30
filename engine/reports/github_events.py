"""GitHub 事实同步（B46 T304，设计 3.5）：合并后从 GitHub API 取回可核对的事实写成事件。

    python .harness/engine/reports/github_events.py [--pr <号>] [--head <提交>] [--since <UTC 时间>]

--pr 同步指定 PR；缺省从 push 合并提交的信息解析关联 PR（merge 形式以「Merge pull request #N from」
开头，squash 形式主题为「标题 (#N)」；head 缺省取 GITHUB_SHA，本地为 HEAD）。手工合并与 App 合并都经
push 到 main 到达这里，不靠 auto-merge job 独占触发。--since 只增量读取该
时间后更新的 escape 议题（登记后顺带增量同步，不引入 watcher）；省略则全量读。只被合并后 main 上的
harness 工作流模板调用，不注册进 cli.py（观察旁路；查询与渲染不追加自身事件，共用合同 C1）。

merge 事件记录设计 3.5 全字段：批准者 login/type、批准绑定提交是否等于合并 head、合并者、合并方式
（merge / squash / rebase）与合并时标签；缺批准（单账号 none）记 "none"，不捏造 App；无法确定合并方式
记 "unknown"。
audit_sample 与 escape 按任务书用 stage=ci，不新增 STAGES 枚举。

每类事实各成不可变快照：source = github:<PR号>:<规范化事实 sha256>（键排序紧凑 JSON 的 sha256），
读取时间不进事实摘要，同输入事实同 source（同快照幂等，重复同步不新增事件），事实变化另起新链、
不覆盖旧快照；事件按 PR 的 headRefName 归属（trace_id）。只读查询：不做任何合并、批准、推送动作；
API 失败逐项记 findings 明确报告事实缺失（退出码 1），不伪造完整事实，也不影响原有判定（工作流步骤
continue-on-error 隔离）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

# 直接脚本运行时 sys.path[0] 是 reports 目录，先把包根插进去才能导入 engine 包；
# -m 方式下包根已在 sys.path，此行幂等。
_PACKAGE_ROOT = str(Path(__file__).resolve().parents[2])
if _PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, _PACKAGE_ROOT)

from engine.core import events, events_db, events_io  # 导入须在上面的 sys.path 准备之后
from engine.core.common import git

# 议题标签约定与 engine/routing/policy.py 的登记一致（escape/class:<类别>；audit 由 auto-merge 创建）。
ESCAPE_LABEL = "escape"
AUDIT_LABEL = "audit"
_CLASS_PREFIX = "class:"
_MERGE_PR_RE = re.compile(r"Merge pull request #(\d+) from ")
_SQUASH_PR_RE = re.compile(r"\(#(\d+)\)$")
_REF_RE = re.compile(r"#(\d+)")
_PAGE_LIMIT = 50
_NONE = "none"
_UNKNOWN = "unknown"


def _finding(code: str, detail: str) -> dict:
    return {"code": code, "detail": detail}


def _text(value, limit: int = 120) -> str | None:
    """规范化字符串（超长、本机路径、换行丢弃为 None）：进入事实摘要前先过隐私口径，保证摘要可复算。"""
    return events._clean_str(str(value), limit) if value is not None else None


class GhClient:
    """缺省 gh 客户端：只读 API 查询，失败抛 RuntimeError；响应只解析、不执行。"""

    def _run(self, argv: list[str]) -> bytes:
        result = subprocess.run(["gh", *argv], capture_output=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode("utf-8", "replace").strip() or f"gh 退出码 {result.returncode}")
        return result.stdout

    def api(self, route: str):
        return json.loads(self._run(["api", route]))

    def repo(self) -> str:
        """仓库身份 owner/repo：优先 Actions/gh 环境变量，否则问 gh。"""
        value = os.environ.get("GITHUB_REPOSITORY") or os.environ.get("GH_REPO")
        if value and value.strip():
            return value.strip()
        return self._run(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]).decode("utf-8").strip()


# ---- 事实读取（只读 API，全部记录 findings、不中断同步）----

def _collect_pages(client, route: str, what: str, findings: list[dict]) -> list[dict] | None:
    """数组型列表端点分页取全（reviews/commits/issues 返回裸数组）：空页即收，50 页不收敛放弃。"""
    separator = "&" if "?" in route else "?"
    items: list[dict] = []
    for page in range(1, _PAGE_LIMIT + 1):
        try:
            batch = client.api(f"{route}{separator}per_page=100&page={page}")
        except (RuntimeError, ValueError, AttributeError) as exc:
            findings.append(_finding("api", f"读取{what}第 {page} 页失败：{exc}"))
            return None
        if not isinstance(batch, list):
            findings.append(_finding("api", f"{what}响应形状不符（第 {page} 页不是列表）"))
            return None
        items += [item for item in batch if isinstance(item, dict)]
        if not batch:
            return items
    findings.append(_finding("api", f"{what}分页 {_PAGE_LIMIT} 页未收敛，已放弃"))
    return None


def _squash_pr(message: str) -> int | None:
    """squash 合并提交的主题是「<PR 标题> (#N)」：从首行尾注解析 PR 号，非该形式返回 None。"""
    match = _SQUASH_PR_RE.search(message.split("\n", 1)[0].strip())
    return int(match[1]) if match else None


def _resolve_pr(client, repo: str, head: str, findings: list[dict]) -> int | None:
    """从 push 合并提交的信息解析关联 PR：merge 形式以「Merge pull request #N from」开头，
    squash 形式（手工 squash 合并也经 push 到 main 到达）主题以「标题 (#N)」结尾。"""
    try:
        commit = client.api(f"repos/{repo}/commits/{head}")
    except (RuntimeError, ValueError, AttributeError) as exc:
        findings.append(_finding("api", f"读取提交 {head[:12]}… 失败：{exc}"))
        return None
    message = commit.get("commit", {}).get("message") if isinstance(commit, dict) else None
    if isinstance(message, str):
        merge = _MERGE_PR_RE.match(message)
        if merge is not None:
            return int(merge[1])
        squash = _squash_pr(message)
        if squash is not None:
            return squash
    findings.append(_finding("no_pr", f"提交 {head[:12]}… 不是关联 PR 的合并提交，未同步"))
    return None


def _pull(client, repo: str, pr: int, findings: list[dict]) -> dict | None:
    """PR 核心事实：未合并或读取失败都不是事实，返回 None（不产出半个 PR 的快照）。"""
    try:
        pull = client.api(f"repos/{repo}/pulls/{pr}")
    except (RuntimeError, ValueError, AttributeError) as exc:
        findings.append(_finding("api", f"读取 PR {pr} 失败：{exc}"))
        return None
    if not isinstance(pull, dict) or pull.get("merged") is not True:
        findings.append(_finding("not_merged", f"PR {pr} 未合并或响应形状不符，跳过（事实同步只覆盖已合并 PR）"))
        return None
    return pull


def _head_ref(pull: dict) -> str | None:
    head = pull.get("head")
    return _text(head.get("ref")) if isinstance(head, dict) else None


def _approval_fact(reviews: list[dict], head_sha: str | None) -> dict:
    """最近一次有效批准：批准者 login/type、绑定提交与是否等于合并的 head（被合并的 PR head 提交，
    即 auto-merge --match-head-commit 的对象）。

    无批准（单账号 none）记 none——合并者不是批准者，不把 owner/merged_by 当批准者，更不捏造 App。
    """
    approved = [review for review in reviews if review.get("state") == "APPROVED"]
    if not approved:
        return {"approver": _NONE, "approver_type": _NONE, "approval_commit": None, "approval_bound": None}
    last = approved[-1]  # API 按时间升序返回，最后一条即最近一次批准
    user = last.get("user") if isinstance(last.get("user"), dict) else {}
    commit = _text(last.get("commit_id"), 40)
    return {"approver": _text(user.get("login")), "approver_type": _text(user.get("type")),
            "approval_commit": commit,
            "approval_bound": (commit == head_sha) if commit and head_sha else None}


def _is_squash_subject(message, title: str | None, pr: int) -> bool:
    """单亲提交判 squash 的核对：提交首行恰为「<PR 标题> (#PR号)」，标题与尾注都核对上才算，
    碰巧以「(#N)」结尾的普通提交或真 rebase 的顶提交不会误判。"""
    if not isinstance(message, str) or not isinstance(title, str) or not title.strip():
        return False
    return message.split("\n", 1)[0].strip() == f"{title.strip()} (#{pr})"


def _merge_method(client, repo: str, pr: int, title: str | None,
                  merge_sha: str | None, findings: list[dict]) -> str:
    """合并方式：双亲为 merge；单亲且主题恰为「PR 标题 (#PR号)」判 squash；单亲且 PR 多于一个
    提交为 rebase；其余（含无法取回）如实记 unknown。"""
    if not merge_sha:
        return _UNKNOWN
    try:
        commit = client.api(f"repos/{repo}/commits/{merge_sha}")
        parents = commit.get("parents") if isinstance(commit, dict) else None
        message = commit.get("commit", {}).get("message") if isinstance(commit, dict) else None
    except (RuntimeError, ValueError, AttributeError) as exc:
        findings.append(_finding("api", f"读取合并提交 {merge_sha[:12]}… 失败：{exc}（合并方式记 unknown）"))
        return _UNKNOWN
    if isinstance(parents, list) and len(parents) == 2:
        return "merge"
    if isinstance(parents, list) and len(parents) == 1:
        if _is_squash_subject(message, title, pr):
            return "squash"
        commits = _collect_pages(client, f"repos/{repo}/pulls/{pr}/commits", f"PR {pr} 提交列表", findings)
        if commits is not None and len(commits) > 1:
            return "rebase"
    return _UNKNOWN


def _label_names(item: dict) -> list[str]:
    return [_text(label.get("name")) for label in item.get("labels") or []
            if isinstance(label, dict) and _text(label.get("name"))]


def _mentions(item: dict, pr: int) -> bool:
    """议题是否指认该 PR（标题或正文里的 #N 精确匹配；PR 本身不算 escape/audit 议题）。"""
    if item.get("pull_request"):
        return False
    text = " ".join(item[key] for key in ("title", "body") if isinstance(item.get(key), str))
    return str(pr) in set(_REF_RE.findall(text))


# ---- 快照（不可变事实，source=github:<PR>:<规范化事实 sha256>，同快照幂等）----

def _snapshot_source(pr: int, facts: dict) -> str:
    """source = github:<PR号>:<规范化事实摘要>：键排序紧凑 JSON 的 sha256，读取时间不进摘要。"""
    canonical = json.dumps(facts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"github:{pr}:{digest}"


def _fresh(source: str, trace: str) -> bool:
    """同快照幂等：该 (source, trace) 已有事件即重复同步，不再新增相同事实事件。"""
    return not events_db.read_events(source=source, trace_id=trace)


def _emit(stage: str, step: str, trace: str, source: str, inputs: list[dict], outputs: dict) -> bool:
    return events.emit(stage, step, "ok", trace_id=trace, source=source,
                       actor={"role": "engine", "host": "github"}, inputs=inputs, outputs=outputs) is not None


def _pr_inputs(repo: str, pr: int, extra: list[tuple[str, str | None]]) -> list[dict]:
    refs = [{"kind": "pr", "ref": f"{repo}#{pr}"}]
    refs += [{"kind": kind, "ref": value} for kind, value in extra if value]
    return refs


def _sync_merge(client, repo: str, pr: int, pull: dict, trace: str,
                findings: list[dict], snapshots: list[dict]) -> int:
    """合并事实快照：merge 事件（设计 3.5 全字段）加逐标签事件；子事实取不到记 unknown/none。"""
    head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
    head_sha = _text(head.get("sha"), 40)
    merge_sha = _text(pull.get("merge_commit_sha"), 40)
    merged_by = pull.get("merged_by") if isinstance(pull.get("merged_by"), dict) else {}
    labels = sorted(set(_label_names(pull)))
    reviews = _collect_pages(client, f"repos/{repo}/pulls/{pr}/reviews", f"PR {pr} 评审列表", findings)
    if reviews is None:
        approval = {"approver": _UNKNOWN, "approver_type": _UNKNOWN,
                    "approval_commit": None, "approval_bound": None}
    else:
        approval = _approval_fact(reviews, head_sha)
    method = _merge_method(client, repo, pr, pull.get("title"), merge_sha, findings)
    facts = {"kind": "merge", "pr": pr, "repository": events_io._repo_identity(repo),
             "head_ref": _head_ref(pull), "head_sha": head_sha,
             "merge_sha": merge_sha, "merged_at": _text(pull.get("merged_at")),
             "merger": _text(merged_by.get("login")), "merger_type": _text(merged_by.get("type")),
             **approval, "merge_method": method, "labels": labels}
    source = _snapshot_source(pr, facts)
    if not _fresh(source, trace):
        return 0
    inputs = _pr_inputs(repo, pr, [("merge_commit", merge_sha),
                                   ("head", facts["head_sha"])])
    outputs = {"pr": pr, **approval, "merger": facts["merger"], "merger_type": facts["merger_type"],
               "merge_method": method, "label_count": len(labels), "merged_at": facts["merged_at"]}
    emitted = int(_emit("merge", "github.merge", trace, source, inputs, outputs))
    for label in labels:
        emitted += int(_emit("merge", "github.merge_label", trace, source, inputs, {"pr": pr, "label": label}))
    snapshots.append({"kind": "merge", "source": source})
    return emitted


def _sync_audit(client, repo: str, pr: int, trace: str,
                findings: list[dict], snapshots: list[dict]) -> int:
    """抽审事实快照：audit 议题是否指认该 PR；读不到时如实记 unknown，不伪造抽样结果。"""
    issues = _collect_pages(client, f"repos/{repo}/issues?state=all&labels={AUDIT_LABEL}",
                            "audit 议题列表", findings)
    sampled, issue_number = _UNKNOWN, None
    if issues is not None:
        sampled, issue_number = False, None
        for item in issues:
            if isinstance(item.get("number"), int) and _mentions(item, pr):
                sampled, issue_number = True, item["number"]
                break
    source = _snapshot_source(pr, {"kind": "audit_sample", "pr": pr,
                                   "sampled": sampled, "issue": issue_number})
    if not _fresh(source, trace):
        return 0
    outputs = {"pr": pr, "sampled": sampled, "issue": issue_number}
    inputs = _pr_inputs(repo, pr, [("issue", str(issue_number) if issue_number else None)])
    emitted = int(_emit("ci", "github.audit_sample", trace, source, inputs, outputs))
    snapshots.append({"kind": "audit_sample", "source": source})
    return emitted


def _sync_escapes(client, repo: str, pr: int, trace: str, since: str | None,
                  findings: list[dict], snapshots: list[dict]) -> int:
    """escape 事实快照：每个指认该 PR 的 escape 议题一条，类别取 class:<类别> 标签。

    后登记的 escape 是新事实、另起新链，不覆盖旧快照；since 只减少读取量（增量），不影响去重。
    """
    route = f"repos/{repo}/issues?state=all&labels={ESCAPE_LABEL}"
    if since:
        route += f"&since={urllib.parse.quote(str(since), safe='')}"
    issues = _collect_pages(client, route, "escape 议题列表", findings)
    if issues is None:
        return 0
    emitted = 0
    seen = 0
    for item in issues:
        if not isinstance(item.get("number"), int) or not _mentions(item, pr):
            continue
        classes = sorted(name for name in _label_names(item) if name.startswith(_CLASS_PREFIX))
        klass = classes[0][len(_CLASS_PREFIX):] if classes else _UNKNOWN
        facts = {"kind": "escape", "pr": pr, "issue": item["number"], "class": klass,
                 "state": _text(item.get("state")), "created_at": _text(item.get("created_at"))}
        source = _snapshot_source(pr, facts)
        if not _fresh(source, trace):
            continue
        outputs = {"pr": pr, "issue": item["number"], "class": klass, "state": facts["state"]}
        emitted += int(_emit("ci", "github.escape", trace, source,
                             _pr_inputs(repo, pr, [("issue", str(item["number"]))]), outputs))
        seen += 1
    snapshots.append({"kind": "escape", "issues": seen})
    return emitted


# ---- 入口 ----

def _default_head() -> str | None:
    """缺省 head：Actions 的 GITHUB_SHA，本地为当前 HEAD；都没有返回 None。"""
    sha = os.environ.get("GITHUB_SHA")
    return sha or git("rev-parse", "HEAD", cwd=events_db.ROOT, check=False, isolate=True) or None


def sync(pr: int | None = None, *, head: str | None = None, since: str | None = None, gh=None) -> dict:
    """同步一个已合并 PR 的 GitHub 事实，返回 {pr, trace, snapshots, findings, events}。

    pr 缺省时从 head（缺省 GITHUB_SHA/HEAD）的合并提交信息解析；gh 供测试注入桩客户端。只读查询：
    任何 API 失败都进 findings（退出码 1 由 main 报告），已取得的事实照常成快照，缺失的不伪造。
    """
    findings: list[dict] = []
    snapshots: list[dict] = []
    result = {"pr": None, "trace": None, "snapshots": snapshots, "findings": findings, "events": 0}
    client = GhClient() if gh is None else gh
    try:
        repo = events_io._repo_identity(str(client.repo()))
    except (RuntimeError, ValueError, AttributeError) as exc:
        findings.append(_finding("api", f"确定仓库失败：{exc}"))
        return result
    if pr is None:
        pr = _resolve_pr(client, repo, head or _default_head() or "", findings)
    if pr is None:
        return result
    pull = _pull(client, repo, pr, findings)
    if pull is None:
        return result
    trace = _head_ref(pull)
    if not trace:
        findings.append(_finding("shape", f"PR {pr} 的 headRefName 缺失，事件无法归属 PR 分支"))
        return result
    result["pr"] = pr
    result["trace"] = trace
    result["events"] += _sync_merge(client, repo, pr, pull, trace, findings, snapshots)
    result["events"] += _sync_audit(client, repo, pr, trace, findings, snapshots)
    result["events"] += _sync_escapes(client, repo, pr, trace, since, findings, snapshots)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="github_events", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pr", type=int, help="同步指定 PR（缺省从 push 合并提交信息解析）")
    parser.add_argument("--head", metavar="提交", help="用该提交的信息解析关联 PR（缺省 GITHUB_SHA 或本地 HEAD）")
    parser.add_argument("--since", metavar="UTC 时间", help="只增量读取该时间后更新的 escape 议题")
    args = parser.parse_args(argv)
    result = sync(args.pr, head=args.head, since=args.since)
    for finding in result["findings"]:
        print(f"github_events：{finding['detail']}", file=sys.stderr)
    if result["pr"] is not None:
        print(f"github_events：PR #{result['pr']}（{result['trace']}）："
              f"快照 {len(result['snapshots'])} 个、事件 {result['events']} 条")
    return 1 if result["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
