"""auto-merge 判定运行与 PR 的关联（B117）：运行名的生成与解析、按工作流列运行的分页（带按 PR
创建时间的提前停止）与判定运行的挑选。

信任来自 API 返回、由默认分支上的工作流定义渲染的运行名：workflow_run 触发的运行执行的是默认
分支的这份定义，PR 作者改不了它，display_title 由该定义用 GitHub 提供的载荷渲染，事件包内容与
PR 分支代码都影响不到；判定包的 origin 仍按该运行自己的 API 记录逐项核对（在 events_io）。本
模块不导入 events_io（避免循环）：运行列表分页自带实现，工作流列表查询经参数注入的 list_all。

提前停止的前提与边界（_list_workflow_runs）：GitHub 的运行列表按创建时间从新到旧返回（实际行为，
非 API 合同），判定运行一定晚于 PR 创建，所以读到「连续两页整页运行都早于 PR 创建时间减一天」
即可停止，读取量只取决于 PR 创建以来的运行数、不随仓库运行总数增长；连续两页只容忍孤立的旧页
夹在新页之间，不保证任意乱序都不漏。页内出现 created_at 缺失或不可解析（必须带时区）的运行后，
本次分页永久禁用提前停止、读到底；PR 的 createdAt 缺失或不可解析时同样读到底。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from engine.core.events_origin import SHA_RE

RUN_NAME_FORMAT = "auto-merge PR #{0} @ {1}"  # 与模板 run-name 的 format 串逐字一致（耦合测试把关）
_NAME_RE = re.compile(r"auto-merge PR #([0-9]+) @ ([0-9a-f]{40})\Z")
_PER_PAGE = 100
_PAGE_LIMIT = 50  # 分页上限（与 events_io._list_all 同口径）
_GRACE = timedelta(days=1)  # PR 创建到第一次判定之间的余量
_STALE_PAGES = 2  # 连续两页整页都旧才停止（只容忍孤立的旧页夹在新页之间）


def _finding(code: str, detail: str) -> dict:
    return {"code": code, "detail": detail}


def judge_run_name(pr: int, head: str) -> str:
    """判定运行的运行名：模板 run-name 在 judge 真正执行时渲染出的值（耦合测试比对模板格式串）。"""
    return RUN_NAME_FORMAT.format(pr, head)


def parse_judge_run_name(title) -> tuple[int, str] | None:
    """display_title →（PR 号，40 位小写十六进制 head）；非字符串、多余前后缀、大写、短 SHA、换行都返回 None。"""
    match = _NAME_RE.fullmatch(title) if isinstance(title, str) else None
    return (int(match[1]), match[2]) if match else None


def select_judge_runs(runs: list, pr: int, resolved: str,
                      trusted: frozenset[str]) -> tuple[list[dict], set[str]]:
    """从 API 形状的运行里挑出该 PR 的判定运行：path 可信、event 为 workflow_run、display_title
    整串等于规范运行名 judge_run_name(pr, head)（只做整串相等：不做子串、宽松正则，也不做整数化比较）；运行名指向同 PR 另一个 head 的计入
    stale 集合（调用方与分支运行一样汇总成一条 head_mismatch），不导入。"""
    matched: list[dict] = []
    stale: set[str] = set()
    for run in runs:
        parsed = parse_judge_run_name(run.get("display_title"))
        path = run.get("path")
        if (parsed is None or run.get("event") != "workflow_run"
                or not isinstance(path, str) or path not in trusted):
            continue
        number, head = parsed
        if number != pr or run.get("display_title") != judge_run_name(number, head):
            continue  # 整串相等：PR 号带前导零（#0187）等非规范写法不算
        if head == resolved:
            matched.append(run)
        else:
            stale.add(head)
    return matched, stale


def run_identity(run: dict, findings: list[dict]) -> tuple[str, str] | None:
    """判定运行自己的 API 身份（head_sha, head_branch）：判定包的 origin 按它核对（不是 PR 的值）。
    API 记录形状不符记 api 发现并返回 None，跳过该运行（不猜）。"""
    head, branch = run.get("head_sha"), run.get("head_branch")
    if not (isinstance(head, str) and SHA_RE.fullmatch(head) and isinstance(branch, str) and branch):
        findings.append(_finding("api", f"判定运行 {run.get('id')} 的 head/分支信息缺失或形状不符"))
        return None
    return head, branch


def import_matched(matched: list[dict], import_run, findings: list[dict]) -> None:
    """挑出的判定运行逐个核对 API 身份（形状不符记 api 发现跳过）后交给 import_run 导入；
    import_run 由 events_io 注入（本模块不反向导入它，避免循环）。"""
    for run in sorted(matched, key=lambda item: str(item.get("id"))):
        identity = run_identity(run, findings)
        if identity is not None:
            import_run(run, identity[1], identity[0])


def _parse_time(value) -> datetime | None:
    """ISO 时间戳 → UTC：必须带时区（Z 或 ±HH:MM 偏移，小数秒均可）；缺时区、解析失败都算不可解析。"""
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed is not None and parsed.tzinfo is not None else None


def _list_workflow_runs(client, repo: str, workflow_id, created_at,
                        findings: list[dict]) -> list[dict] | None:
    """一个工作流的运行列表：不带任何筛选参数（GitHub 对带 event/branch/status/head_sha/created/
    actor/check_suite_id 参数的运行列表每次搜索最多返回 1,000 条、超出静默截断而调用方无从发现），
    按创建时间从新到旧分页，提前停止规则见模块 docstring；不收敛记 api 发现（同 _list_all 口径）。"""
    cutoff = _parse_time(created_at)
    allow_stop = cutoff is not None
    if cutoff is not None:
        cutoff -= _GRACE
    runs: list[dict] = []
    stale_pages = 0
    for page in range(1, _PAGE_LIMIT + 1):
        try:
            data = client.api(f"repos/{repo}/actions/workflows/{workflow_id}/runs"
                              f"?per_page={_PER_PAGE}&page={page}")
        except (RuntimeError, ValueError) as exc:
            findings.append(_finding("api", f"列出 workflow_runs 第 {page} 页失败：{exc}"))
            return None
        batch = data.get("workflow_runs") if isinstance(data, dict) else None
        if not isinstance(batch, list):
            findings.append(_finding("api", f"workflow_runs 响应形状不符（第 {page} 页）"))
            return None
        page_runs = [item for item in batch if isinstance(item, dict)]
        runs += page_runs
        if allow_stop and page_runs:
            moments = [_parse_time(item.get("created_at")) for item in page_runs]
            if None in moments:
                allow_stop = False  # 坏时间戳页之后永久禁用提前停止（即使之后又出现连续两页旧运行）
            elif all(moment < cutoff for moment in moments):  # 恰等于阈值不算「早于」
                stale_pages += 1
                if stale_pages >= _STALE_PAGES:
                    return runs
            else:
                stale_pages = 0
        total = data.get("total_count")
        if not batch or (isinstance(total, int) and len(runs) >= total):
            return runs
    findings.append(_finding("api", "workflow_runs 分页 50 页未收敛，已放弃"))
    return None


def collect_judge_runs(client, *, repo: str, pr: int, resolved: str, trusted: frozenset[str],
                       list_all, created_at, stale: set[str], findings: list[dict]) -> list[dict]:
    """列出 auto-merge 工作流的运行并挑出该 PR 的判定运行（load_ci 的第二段查询，B117）。

    先查工作流列表（工作流很少，一页即可），对 path 可信且为 auto-merge 的每个工作流按上面分页
    列运行、不带筛选参数，再用 select_judge_runs 挑选；运行名指向同 PR 另一个 head 的并入传入的
    stale 集合（调用方与分支运行一样汇总成一条 head_mismatch），不导入。工作流列表或任一运行列
    表失败：返回已挑到的部分（失败本身已记 api 发现），调用方保持已导入的分支运行结果。
    created_at 是 PR 的 createdAt 原样字符串（None 或不可解析时运行列表读到底）。
    """
    workflows = list_all(client, f"repos/{repo}/actions/workflows?per_page={_PER_PAGE}",
                         "workflows", findings)
    if workflows is None:
        return []
    matched: list[dict] = []
    for workflow in workflows:
        path = workflow.get("path") if isinstance(workflow, dict) else None
        if not isinstance(path, str) or path not in trusted or "auto-merge" not in path:
            continue
        runs = _list_workflow_runs(client, repo, workflow.get("id"), created_at, findings)
        part_matched, part_stale = select_judge_runs(runs or [], pr, resolved, trusted)
        matched += part_matched
        stale |= part_stale
    return matched
