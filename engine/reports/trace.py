"""events 与 trace 查询命令（可观测性 P3，设计 4.2）：只读观察入口，查询不追加自身事件（共用合同 C1）。

    python3 cli.py events [--since 1d] [--stage verify] [--status deny] [--json]
                          [--export 路径 | --import 路径]
        过滤展示与计数；--export/--import 走 C5 的整库导出与幂等导入（展示过滤不进导出包，筛选后
        的断链子集不是可导入 bundle）。非法枚举/时间/组合返回 2；导入有发现返回 1。

    python3 cli.py trace <任务编号 | PR 号 | 分支> [--ci] [--json]
        任务号经运行记录（docs/runs，task 字段须相符）与任务书文件名映射到分支；PR 号经 API
        headRefName 换分支（不读检出分支，也不读 PR 正文）；完整分支名即 trace。多分支同任务、或
        分支关联多个 PR 时列出候选（attempt/链头/时间），不随意选第一条。时间线标注输入/输出引用、
        决定、角色、时长、来源与尝试分段（运行记录窗口优先，CI 链按来源各成一段），汇总最长阶段与
        首个失败（deny/fail/error 区分）。--ci 经 C5 load_ci 下载 PR 关联的 CI 事件包幂等导入后再
        展示；缺本机库或下载有发现时如实报告，不当成功。

退出码：0 正常（含歧义候选）；1 运行受阻（gh 失败、--ci 未完成或导入有发现）；2 参数与解析错误。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from engine.core import events, events_db, events_io

_TASK_RE = re.compile(r"[Tt](\d+)\Z")
_PR_RE = re.compile(r"#?(\d+)\Z")
_DURATION_RE = re.compile(r"(\d+)([dhms])\Z")
_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_FAILURES = ("fail", "deny", "error")


def _parse_since(text: str) -> datetime:
    """--since 的取值：<n><d|h|m|s> 相对时长或 ISO 时间戳；不合法抛 ValueError（命令转退出码 2）。"""
    match = _DURATION_RE.fullmatch(text.strip())
    if match:
        return datetime.now(UTC) - timedelta(seconds=int(match[1]) * _UNITS[match[2]])
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(f"--since 无法解析为时长或 UTC 时间：{text}") from None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _parse_ts(value) -> datetime | None:
    """记录/事件里的时间戳 → aware UTC；缺失或损坏返回 None（按无法分段处理）。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


# ---- events：过滤、计数与 C5 导出/导入 ----


def _counts(rows: list[dict]) -> dict:
    """过滤集的 stage/status 计数（跟随筛选结果，不跟总计）。"""
    return {"stage": dict(sorted(Counter(row["stage"] for row in rows).items())),
            "status": dict(sorted(Counter(row["status"] for row in rows).items()))}


def _export(target: Path) -> int:
    try:
        bundle = events_io.export_bundle()
        events_io.write_bundle(bundle, target)
    except OSError as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        return 1
    print(f"已导出 {len(bundle['events'])} 个事件、{len(bundle['chains'])} 条链、"
          f"{len(bundle['anchors'])} 个锚点 → {target}")
    return 0


def _import(source: Path) -> int:
    try:
        bundle = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        print(f"读取失败：{exc}", file=sys.stderr)
        return 2
    except ValueError:
        print(f"{source} 不是有效的 JSON", file=sys.stderr)
        return 2
    result = events_io.import_bundle(bundle if isinstance(bundle, dict) else {})
    for finding in result["findings"]:
        print(f"发现 {finding['code']}：{finding['detail']}", file=sys.stderr)
    print(f"导入完成：新增 {result['imported']}，跳过 {result['skipped']}，发现 {len(result['findings'])} 项")
    return 1 if result["findings"] else 0


def events_main(argv: list[str] | None = None) -> int:
    """events 子命令：过滤展示与计数；--export/--import 整库模式供工作流使用。"""
    parser = argparse.ArgumentParser(prog="events", description="事件过滤展示、计数与 C5 导出/导入")
    parser.add_argument("--since", metavar="时长|UTC", help="只看此后的事件：如 1d、12h 或 ISO 时间戳")
    parser.add_argument("--stage", choices=events.STAGES, help="按环节过滤")
    parser.add_argument("--status", choices=events.STATUSES, help="按状态过滤")
    parser.add_argument("--json", action="store_true", help="输出 JSON（过滤集、计数与总计）")
    parser.add_argument("--export", metavar="路径", help="导出整库 EventBundle 到该文件（不含展示过滤）")
    parser.add_argument("--import", dest="import_path", metavar="路径", help="从该文件幂等导入 EventBundle")
    args = parser.parse_args(argv)
    if (args.export or args.import_path) and (args.since or args.stage or args.status):
        print("--export/--import 是整库模式，不与 --since/--stage/--status 同用", file=sys.stderr)
        return 2
    if args.export and args.import_path:
        print("--export 与 --import 只能二选一", file=sys.stderr)
        return 2
    if args.export:
        return _export(Path(args.export))
    if args.import_path:
        return _import(Path(args.import_path))
    try:
        since = _parse_since(args.since) if args.since else None
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    filtered = args.since is not None or args.stage is not None or args.status is not None
    rows = sorted(events_io.query(since=since, stage=args.stage, status=args.status),
                  key=lambda row: (row["ts"], row["source"], row["seq"]))
    total = len(events_io.query()) if filtered else len(rows)
    if args.json:
        print(json.dumps({"count": len(rows), "total": total, "counts": _counts(rows),
                          "events": rows}, ensure_ascii=False, indent=2))
        return 0
    print(f"事件 {len(rows)} 条 / 总计 {total} 条")
    for name, counted in _counts(rows).items():
        print(f"按 {name}：" + ("、".join(f"{key} {value}" for key, value in counted.items()) or "—"))
    for row in rows:
        duration = f" {row['duration_ms']}ms" if row["duration_ms"] is not None else ""
        print(f"  #{row['seq']} {row['ts']} [{row['source']}] {row['stage']}/{row['step']} "
              f"{row['status']}{duration}")
    return 0


# ---- trace：标识解析 ----


@dataclass
class Candidate:
    """一个候选：分支（run_record/taskbook 来源）或 PR（push_pr 来源），带 attempt/链头/时间。"""

    branch: str
    origin: str
    attempt: int | None = None
    ts: str | None = None
    end: str | None = None
    head: str | None = None
    exit_status: str | None = None
    pr: int | None = None

    def row(self) -> dict:
        return {"branch": self.branch, "origin": self.origin, "attempt": self.attempt, "ts": self.ts,
                "head": self.head, "exit": self.exit_status, "pr": self.pr}


def _read_record(path: Path) -> dict | None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _record_candidate(record: dict, origin: str) -> Candidate | None:
    """运行记录 → 候选：branch 必填；attempt/时间/退出与链头（锚点或 stages 末项）尽力补齐。"""
    branch = record.get("branch")
    if not isinstance(branch, str) or not branch:
        return None
    head = None
    if isinstance(record.get("anchors"), list) and record["anchors"]:
        last = record["anchors"][-1]
        head = last.get("head_hash") if isinstance(last, dict) else None
    stages = record.get("stages")
    if head is None and isinstance(stages, list) and stages and isinstance(stages[-1], dict):
        head = stages[-1].get("head_hash")
    attempt = record.get("attempt")
    return Candidate(branch, origin, attempt=attempt if isinstance(attempt, int) else None,
                     ts=record.get("started_at") if isinstance(record.get("started_at"), str) else None,
                     end=record.get("ended_at") if isinstance(record.get("ended_at"), str) else None,
                     head=head if isinstance(head, str) else None,
                     exit_status=record.get("exit") if isinstance(record.get("exit"), str) else None)


def _scan_records(match) -> list[Candidate]:
    """扫描 docs/runs 下所有运行记录，branch/trace_id 命中的转为候选；损坏或不相符的跳过。"""
    candidates: list[Candidate] = []
    runs = events_db.ROOT / "docs" / "runs"
    for directory in sorted(runs.glob("*")) if runs.is_dir() else []:
        for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
            record = _read_record(path)
            if record and match(record):
                candidate = _record_candidate(record, "run_record")
                if candidate is not None:
                    candidates.append(candidate)
    return candidates


def _task_candidates(number: int) -> list[Candidate]:
    """任务号 → 候选分支：运行记录（task 字段须为 T<编号>）加任务书文件名映射（task/<stem>）。"""
    padded = f"{number:03d}"
    candidates = [item for item in _scan_records(
        lambda record: record.get("task") == f"T{number}") if item.branch.startswith(f"task/{padded}-")]
    plans = events_db.ROOT / "docs" / "plans"
    known = {item.branch for item in candidates}
    for path in sorted(plans.glob(f"task-{padded}-*.md")) if plans.is_dir() else []:
        branch = f"task/{path.stem.removeprefix('task-')}"
        if branch not in known:
            candidates.append(Candidate(branch, "taskbook"))
            known.add(branch)
    return candidates


def _pr_candidates(trace_id: str) -> list[Candidate]:
    """本机 push_pr 事件记录的 PR 候选（分支重用可能先后关联多个 PR）。"""
    candidates: list[Candidate] = []
    for row in events_io.query(trace_id=trace_id, stage="dispatch"):
        outputs = row["outputs"] if isinstance(row["outputs"], dict) else {}
        pr = outputs.get("pr")
        if row["step"] == "push_pr" and isinstance(pr, int):
            head = outputs.get("head")
            candidates.append(Candidate(trace_id, "push_pr", ts=row["ts"],
                                        head=head if isinstance(head, str) else None, pr=pr))
    return candidates


def _resolve(identifier: str) -> dict:
    """标识 → 解析视图（不触网）：任务号查映射，PR 号留待 API，分支即 trace。"""
    if match := _TASK_RE.fullmatch(identifier):
        number = int(match[1])
        candidates = _task_candidates(number)
        branches = {item.branch for item in candidates}
        ambiguous = len(branches) > 1
        return {"kind": "task", "identifier": identifier, "number": number, "candidates": candidates,
                "ambiguous": ambiguous,
                "trace_id": None if ambiguous else (candidates[0].branch if candidates else None)}
    if match := _PR_RE.fullmatch(identifier):
        return {"kind": "pr", "identifier": identifier, "number": int(match[1])}
    if identifier and not identifier.startswith("-"):
        return {"kind": "branch", "identifier": identifier, "trace_id": identifier}
    return {"kind": "unknown", "identifier": identifier}


# ---- trace：时间线组装与展示 ----


def _ci_attempt(source: str) -> int | None:
    """ci:<run>:<attempt>:<job> 来源链里的 attempt；其余来源为 None。"""
    parts = source.split(":")
    return int(parts[2]) if len(parts) == 4 and parts[0] == "ci" and parts[2].isdigit() else None


def _segment_events(rows: list[dict], records: list[Candidate]) -> tuple[list[dict], dict[int, dict]]:
    """时间线分段：本机运行记录窗口（attempt/链头/起止）优先，CI 来源链各成一段，其余未分段。"""
    windows = [(item, _parse_ts(item.ts), _parse_ts(item.end)) for item in records
               if item.origin == "run_record"]
    windows = [(item, start, end) for item, start, end in windows if start and end]
    buckets: dict[str, dict] = {}
    labels: dict[int, dict] = {}
    for index, row in enumerate(rows):
        if (source := row["source"]).startswith("ci:"):
            key, attempt, head = source, _ci_attempt(source), None
        else:
            moment = _parse_ts(row["ts"])
            hit = next((item for item, start, end in windows
                        if moment is not None and start <= moment <= end), None)
            key = f"attempt {hit.attempt}" if hit is not None else "本机（窗口外）"
            attempt = hit.attempt if hit is not None else None
            head = hit.head if hit is not None else None
        labels[index] = {"attempt": attempt, "segment": key}
        bucket = buckets.setdefault(key, {"segment": key, "attempt": attempt, "head": head,
                                          "start": row["ts"], "end": row["ts"], "count": 0})
        bucket["end"], bucket["count"] = row["ts"], bucket["count"] + 1
    return sorted(buckets.values(), key=lambda item: item["start"]), labels


def _longest_stage(rows: list[dict]) -> dict | None:
    """各环节累计时长最长的阶段（并列取字典序首个，保证确定性）；无时长数据为 None。"""
    totals: dict[str, int] = {}
    for row in rows:
        if isinstance(row["duration_ms"], int):
            totals[row["stage"]] = totals.get(row["stage"], 0) + row["duration_ms"]
    if not totals:
        return None
    stage = max(sorted(totals), key=lambda name: totals[name])
    return {"stage": stage, "duration_ms": totals[stage]}


def _first_failure(rows: list[dict]) -> dict | None:
    """首个失败事件（rows 已按时间排序）；deny/fail/error 按原状态区分，不合并。"""
    for row in rows:
        if row["status"] in _FAILURES:
            return {"ts": row["ts"], "source": row["source"], "seq": row["seq"],
                    "stage": row["stage"], "step": row["step"], "status": row["status"]}
    return None


def _timeline(trace_id: str, records: list[Candidate]) -> dict:
    rows = sorted(events_io.query(trace_id=trace_id), key=lambda row: (row["ts"], row["source"], row["seq"]))
    segments, labels = _segment_events(rows, records)
    return {"trace_id": trace_id, "chains": sorted({row["source"] for row in rows}),
            "segments": segments, "counts": _counts(rows),
            "events": [dict(row, **labels[index]) for index, row in enumerate(rows)],
            "longest_stage": _longest_stage(rows), "first_failure": _first_failure(rows)}


def _short(digest) -> str:
    return digest[:7] if isinstance(digest, str) and digest else "—"


def _ref_text(item: dict) -> str:
    text = f"{item.get('kind')}={item.get('ref')}"
    if item.get("sha256"):
        text += f"#{_short(item['sha256'])}"
    if item.get("size") is not None:
        text += f"({item['size']}B)"
    return text


def _event_lines(row: dict) -> list[str]:
    lines = [f"  #{row['seq']} {row['ts']} [{row['source']}] {row['stage']}/{row['step']} {row['status']}"
             + (f" {row['duration_ms']}ms" if row["duration_ms"] is not None else "")]
    if row["inputs"]:
        lines.append("      输入 " + "；".join(_ref_text(item) for item in row["inputs"]))
    if row["outputs"]:
        lines.append("      输出 " + "；".join(f"{key}={row['outputs'][key]}"
                                             for key in sorted(row["outputs"], key=str)))
    if row["decision"]:
        lines.append(f"      决定 by={row['decision'].get('by')} rule={row['decision'].get('rule')} "
                     f"{row['decision'].get('reason')}")
    if row["error"]:
        lines.append(f"      错误 kind={row['error'].get('kind')} signature={row['error'].get('signature')}")
    if row["actor"]:
        model = f" 模型 {row['actor']['model']}" if row["actor"].get("model") else ""
        lines.append(f"      角色 {row['actor'].get('role')}@{row['actor'].get('host')}{model}")
    return lines


def _candidate_text(item: dict) -> str:
    parts = [item["branch"]]
    if item.get("pr") is not None:
        parts.append(f"PR {item['pr']}")
    if item.get("attempt") is not None:
        parts.append(f"attempt {item['attempt']}")
    if item.get("head"):
        parts.append(f"链头 {_short(item['head'])}")
    if item.get("ts"):
        parts.append(item["ts"])
    return "  " + "，".join(parts) + f"（来源 {item['origin']}）"


def _render(view: dict) -> str:
    if "candidates" in view:
        lines = [f"标识 {view['identifier']} 对应多个分支，未选择（用完整分支重试）："]
        lines += [_candidate_text(item) for item in view["candidates"]]
        return "\n".join(lines)
    lines = [f"trace {view['trace_id']}（标识 {view['identifier']}，{view['kind']}）"]
    for segment in view["segments"]:
        head = f"链头 {_short(segment['head'])}，" if segment.get("head") else ""
        attempt = f"attempt {segment['attempt']}，" if segment.get("attempt") is not None else ""
        lines.append(f"  {segment['segment']}：{attempt}{head}{segment['start']}→{segment['end']}"
                     f"（{segment['count']} 个事件）")
    for row in view["events"]:
        lines += _event_lines(row)
    longest = view["longest_stage"]
    lines.append("  最长阶段 " + (f"{longest['stage']}（{longest['duration_ms']}ms）" if longest else "—"))
    failure = view["first_failure"]
    lines.append("  首个失败 " + (f"#{failure['seq']} {failure['ts']} [{failure['source']}] "
                                   f"{failure['stage']}/{failure['step']} {failure['status']}"
                                   if failure else "—"))
    return "\n".join(lines)


def _emit(view: dict, as_json: bool) -> None:
    print(json.dumps(view, ensure_ascii=False, indent=2) if as_json else _render(view))


def _pr_branch(number: int) -> str | None:
    """PR → API headRefName（trace 权威来源）；失败打印诊断返回 None。"""
    try:
        branch = events_io.GhClient().pr(number).get("headRefName")
    except (RuntimeError, ValueError, AttributeError) as exc:
        print(f"查询 PR {number} 失败：{exc}", file=sys.stderr)
        return None
    if not isinstance(branch, str) or not branch:
        print(f"PR {number} 的 headRefName 缺失", file=sys.stderr)
        return None
    return branch


def _with_ci(view: dict, trace_id: str, pr_number: int | None) -> int:
    """--ci：确定 PR（标识给定或本机 push_pr 事件）、下载导入并写进 view；返回退出码贡献。"""
    known = _pr_candidates(trace_id)
    prs = [pr_number] if pr_number is not None else sorted({item.pr for item in known})
    if len(prs) > 1:
        # 分支重用关联了多个 PR：列候选（pr/链头/时间），不随意选一条下载
        view["pr_candidates"] = [item.row() for item in known]
        print("该分支关联多个 PR，--ci 未下载；请指定 PR 号", file=sys.stderr)
        return 1
    if not prs:
        print("本机没有该 trace 的 push_pr 事件，无法确定 PR；请直接给 PR 号", file=sys.stderr)
        return 1
    view["pr"] = prs[0]
    try:
        result = events_io.load_ci(prs[0], gh=events_io.GhClient())
    except Exception as exc:  # noqa: BLE001  下载失败转诊断，不替代时间线
        result = {"imported": 0, "skipped": 0, "findings": [{"code": "api", "detail": str(exc)}]}
    view["ci"] = result
    for finding in result["findings"]:
        print(f"CI 发现 {finding['code']}：{finding['detail']}", file=sys.stderr)
    blocking = [item for item in result["findings"]
                if item.get("code") not in events_io.INFORMATIONAL_FINDINGS]  # B124：旧 head 运行不算受阻
    return 1 if blocking else 0


def trace_main(argv: list[str] | None = None) -> int:
    """trace 子命令：解析任务/PR/分支 → 时间线；--ci 先下载导入该 PR 的 CI 事件包。"""
    parser = argparse.ArgumentParser(prog="trace", description="按任务编号、PR 号或分支复原交付时间线")
    parser.add_argument("identifier", help="任务编号（T402）、PR 号（77 或 #77）或完整分支名")
    parser.add_argument("--ci", action="store_true", help="先下载并导入该 PR 的 CI 事件包（C5 load_ci）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)
    resolved = _resolve(args.identifier)
    if resolved["kind"] == "unknown":
        print(f"无法识别的标识：{args.identifier!r}（任务编号 T<n> / PR 号 / 完整分支）", file=sys.stderr)
        return 2
    if resolved["kind"] == "task" and resolved["ambiguous"]:
        _emit({"identifier": resolved["identifier"], "kind": "task", "ambiguous": True,
               "trace_id": None, "candidates": [item.row() for item in resolved["candidates"]]},
              args.json)
        return 0
    if resolved["kind"] == "task" and resolved["trace_id"] is None:
        print(f"找不到任务 T{resolved['number']:03d} 的运行记录或任务书", file=sys.stderr)
        return 1
    pr_number = None
    if resolved["kind"] == "pr":
        branch = _pr_branch(resolved["number"])
        if branch is None:
            return 1
        resolved["trace_id"], pr_number = branch, resolved["number"]
    trace_id = resolved["trace_id"]
    view: dict = {"identifier": args.identifier, "kind": resolved["kind"], "trace_id": trace_id,
                  "pr": pr_number, "ambiguous": resolved.get("ambiguous", False)}
    code = _with_ci(view, trace_id, pr_number) if args.ci else 0
    # 时间线在导入之后组装：--ci 新导入的事件同屏展示
    records = _scan_records(lambda record: trace_id in (record.get("branch"), record.get("trace_id")))
    view.update({"records": [item.row() for item in records], **_timeline(trace_id, records)})
    _emit(view, args.json)
    return code


