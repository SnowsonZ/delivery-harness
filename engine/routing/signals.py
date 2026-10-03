"""合同制合并路由的评论信号（只读）：独立评审、设计方复核与审计标记的解析、「内容相同」判定。

policy 的合同制路径（设计 docs/plans/2026-10-03-autonomy-trial-design.md 第 4 节）从这里读 PR 评论里的
机器可读标记，只认 checks.toml [identity] agent_login 写的：

  评审标记  <!-- independent-review {"verdict","reviewer","model","head","findings","flagged","parsed"} -->
            （写入方 engine/agents/review.py 的 render_comment；本模块只读，不改它的格式）
  复核标记  <!-- designer-signoff {"verdict","head","designer","mutations","caught"} -->
            （写入方 bin/dispatch signoff，T702 按这个合同写）
  审计标记  <!-- harness-review-audit {…} -->（共用合同 C6；同家评审降级取其中的 model/model_basis，
            不取评审标记里的 model——那是展示串，未配置模型时会是「codex 默认模型」之类的占位）

防混入（设计第 6 节）：一条评论只有同时满足三条才算标记——两类标记（评审、复核）合计恰好出现一次、
标记独占一行、位置正确（复核标记是最后一个非空行；评审标记之后只允许 harness-review-audit 行）。
不满足的评论整条忽略：评审正文里嵌着的模型输出可能夹带伪造的标记。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from engine.core.common import git

REVIEW_MARK = "<!-- independent-review "
SIGNOFF_MARK = "<!-- designer-signoff "
AUDIT_MARK = "<!-- harness-review-audit "


def markers(comments: list[dict], mark: str, login: str) -> list[dict]:
    """作者为 login 的合法标记 JSON，按评论出现顺序返回；解析失败的条目跳过，其他作者一律忽略。"""
    return [data for _comment, data in _accepted(comments, mark, login)]


def same_content(reviewed: str, head: str, base: str, cwd: Path) -> bool:
    """评审/复核时的 head（reviewed）与当前 head 是否同一份内容（设计第 4 节）。

    reviewed == head 时为真；否则要求 reviewed 是 head 的祖先，并且两段
    `git diff --binary --full-index --no-ext-diff --no-renames` 的输出字节完全相同：一段是
    <merge-base(base, reviewed)>..<reviewed>，一段是 <merge-base(base, head)>..<head>。不做任何空白
    规范化：patch-id 会忽略空白，而 Python 的缩进变化能改变执行路径。提交不存在或 git 出错时为假。
    """
    if not reviewed:
        return False
    if reviewed == head:
        return True
    if git("merge-base", reviewed, head, cwd=cwd, check=False) != reviewed:
        return False
    old, new = _range_diff(base, reviewed, cwd), _range_diff(base, head, cwd)
    return old is not None and old == new


def review_status(comments: list[dict], login: str, head: str, base: str, cwd: Path) -> tuple[str, str]:
    """("ok"|"fail"|"missing", 理由)：取最后一个内容与当前 head 相同的评审标记。

    verdict 为「通过」且 flagged 为假时是 ok，其余情况是 fail；没有这样的标记时是 missing。
    """
    pair = _last_matched(comments, REVIEW_MARK, login, head, base, cwd)
    if pair is None:
        return "missing", "没有指向当前 head 的评审标记"
    data = pair[1]
    if data.get("verdict") != "通过":
        return "fail", f"评审结论为「{data.get('verdict')}」"
    if data.get("flagged"):
        return "fail", "评审标记了严重级发现（flagged）"
    return "ok", "评审通过且未标记严重"


def signoff_status(comments: list[dict], login: str, head: str, base: str, cwd: Path) -> tuple[str, str]:
    """同 review_status 的规则；ok 的条件是 verdict 为「通过」、mutations >= 1 且 caught == mutations。"""
    pair = _last_matched(comments, SIGNOFF_MARK, login, head, base, cwd)
    if pair is None:
        return "missing", "没有指向当前 head 的复核标记"
    data = pair[1]
    if data.get("verdict") != "通过":
        return "fail", f"复核结论为「{data.get('verdict')}」"
    mutations, caught = data.get("mutations"), data.get("caught")
    if not isinstance(mutations, int) or not isinstance(caught, int):
        return "fail", "mutations/caught 缺失或不是整数"
    if mutations < 1:
        return "fail", f"mutations={mutations}（没有做定向变异）"
    if caught != mutations:
        return "fail", f"caught {caught} ≠ mutations {mutations}（有变异未被抓住）"
    return "ok", f"复核通过，{mutations} 项定向变异全部抓住"


def audit_model(comments: list[dict], login: str, head: str, base: str, cwd: Path) -> str | None:
    """最后一条有效评审标记所在评论里 harness-review-audit 的 model（同家评审降级用它，不用展示串）。

    缺审计标记、model 为 null 或 model_basis 为 unknown 时为 None（按模型未知处理）。
    """
    pair = _last_matched(comments, REVIEW_MARK, login, head, base, cwd)
    body = pair[0].get("body") if pair else None
    if not isinstance(body, str):
        return None
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith(AUDIT_MARK) or not stripped.endswith("-->"):
            continue
        try:
            data = json.loads(stripped.removeprefix(AUDIT_MARK).removesuffix("-->").strip())
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or data.get("model") is None or data.get("model_basis") == "unknown":
            return None
        return str(data["model"])
    return None


def model_family(model: str | None) -> str | None:
    """模型家族：最后一个 / 之后、第一个 - 之前的部分，转小写（provider/abc-x 与 abc-y 都得 abc，
    即同一家族）；空值得到 None。"""
    if not model:
        return None
    family = model.rsplit("/", 1)[-1].split("-", 1)[0].lower()
    return family or None


def _accepted(comments: list[dict] | None, mark: str, login: str) -> list[tuple[dict, dict]]:
    """[(评论, 标记 JSON)]：作者为 login 且通过防混入检查的评论里的 mark 标记，按出现顺序。"""
    found = []
    for comment in comments or []:
        if not isinstance(comment, dict):
            continue
        author = comment.get("author")
        if not isinstance(author, dict) or author.get("login") != login:
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        if body.count(REVIEW_MARK) + body.count(SIGNOFF_MARK) != 1 or mark not in body:
            continue
        data = _marker_data(body, mark)
        if data is not None:
            found.append((comment, data))
    return found


def _marker_data(body: str, mark: str) -> dict | None:
    """标记 JSON：独占一行、位置正确、可解析成对象；任一不满足返回 None（整条评论作废）。"""
    lines = body.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith(mark):
            continue
        payload = stripped.removeprefix(mark)
        if not payload.endswith("-->"):
            return None
        after = lines[index + 1:]
        if mark == SIGNOFF_MARK:
            if any(rest.strip() for rest in after):
                return None  # 复核标记必须是最后一个非空行
        elif any(rest.strip() and not rest.strip().startswith(AUDIT_MARK) for rest in after):
            return None  # 评审标记之后只允许审计标记行
        try:
            data = json.loads(payload.removesuffix("-->").strip())
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None
    return None


def _last_matched(comments: list[dict] | None, mark: str, login: str, head: str, base: str,
                  cwd: Path) -> tuple[dict, dict] | None:
    """最后一个内容与当前 head 相同的 (评论, 标记 JSON)；没有则为 None。"""
    for pair in reversed(_accepted(comments, mark, login)):
        if same_content(str(pair[1].get("head") or ""), head, base, cwd):
            return pair
    return None


def _range_diff(base: str, tip: str, cwd: Path) -> bytes | None:
    """<merge-base(base, tip)>..<tip> 的字节 diff；提交不存在或 git 出错返回 None。"""
    root = git("merge-base", base, tip, cwd=cwd, check=False)
    if not root:
        return None
    result = subprocess.run(
        ["git", "diff", "--binary", "--full-index", "--no-ext-diff", "--no-renames", f"{root}..{tip}"],
        cwd=cwd, capture_output=True, check=False)
    return result.stdout if result.returncode == 0 else None
