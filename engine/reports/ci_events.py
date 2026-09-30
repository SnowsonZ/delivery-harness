"""CI 工作流的事件导出与 summary（B46 T303）：只被 harness/auto-merge 工作流模板调用，不注册进
cli.py（无 CLI 注册冲突；导出与 summary 是观察旁路，查询与渲染不追加自身事件，共用合同 C1）。

    python .harness/engine/reports/ci_events.py export --target <目录>
        先把当前链（default_source()）各 trace 的链头固定为 ci_artifact 锚点（设计 2.5：锚点随
        artifact 上传即被固定），再把该链的 EventBundle v1 写成 <目录>/harness-events.json，并把
        manifest 里的安全 JSON 内容产物复制进同一目录；目录先清空再写，上传集合因此精确等于
        「bundle + 安全 manifest」，临时库 db/WAL/SHM 与本机原始日志不进入。缺库/无事件导出空包；
        被排除的产物以 findings 记进包内并向 stderr 提示，不中断导出。

    python .harness/engine/reports/ci_events.py summary --bundle <文件>
        从导出的 bundle 渲染 verify/route 事件的 Markdown summary（检查/规则、状态、依据与理由、
        错误）到 stdout，供工作流追加进 $GITHUB_STEP_SUMMARY。只读展示层：与原机器输出同源（同一
        事件数据），保留既有步骤自己的 summary 输出与状态检查名，不替换、不改判定。

两种调用方式都须支持安装布局：直接脚本把包根（安装布局的 .harness、引擎仓库的仓库根）插入
sys.path；`python -m engine.reports.ci_events` 在 .harness 目录（或 PYTHONPATH 含它）下运行。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

# 直接脚本运行时 sys.path[0] 是 reports 目录，先把包根插进去才能导入 engine 包；
# -m 方式下包根已在 sys.path，此行幂等。
_PACKAGE_ROOT = str(Path(__file__).resolve().parents[2])
if _PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, _PACKAGE_ROOT)

from engine.core import events, events_db, events_io  # 导入须在上面的 sys.path 准备之后

BUNDLE_NAME = "harness-events.json"  # 逻辑名 harness-events 的包文件（artifact 内唯一 JSON 成员）
ANCHOR_STAGE = "ci"  # 锚点在 CI 作业导出时固定，记 ci 阶段
ANCHOR_FIXED_IN = "ci_artifact"  # 链头固定处：CI 事件 artifact（events.set_anchor 的枚举值）
SUMMARY_STAGES = ("verify", "route")  # summary 只渲染判定与路由事件（T303 目标终态）


# ---- export：链头锚点、EventBundle 与安全内容产物 ----

def _chains(source: str) -> list[tuple[str, str]]:
    """当前来源参与导出的链（按链首次出现序）；查询只读。"""
    chains: list[tuple[str, str]] = []
    for row in events_io.query(source=source):
        key = (row["source"], row["trace_id"])
        if key not in chains:
            chains.append(key)
    return chains


def _fix_anchors(source: str) -> int:
    """导出前把各链链头固定为 ci_artifact 锚点（set_anchor 永不抛异常）；返回固定的锚点数。"""
    fixed = 0
    for chain_source, trace in _chains(source):
        head = events.chain_head(chain_source, trace)
        if head:
            events.set_anchor(trace, ANCHOR_STAGE, head, ANCHOR_FIXED_IN, chain_source)
            fixed += 1
    return fixed


def _reset_dir(target: Path) -> None:
    """导出目录先清空再建：上传集合精确等于本次的 bundle + manifest，不混入上次残留。"""
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)


def _copy_manifest(bundle: dict, target: Path) -> int:
    """把 manifest 里的安全内容产物复制进导出目录（文件名即 sha256）；失败抛 OSError 不当成功。"""
    directory = events_db.artifacts_dir()
    copied = 0
    for entry in bundle["artifacts"]:
        if directory is None:
            raise OSError("无本地产物目录，manifest 内容产物无法复制")
        shutil.copyfile(directory / entry["file"], target / entry["file"])
        copied += 1
    return copied


def export_main(argv: list[str]) -> int:
    """export 子命令：写 bundle 与安全内容产物；导出失败返回 1，不当成功。"""
    parser = argparse.ArgumentParser(prog="ci_events export", description="导出当前链的 EventBundle 与安全内容产物")
    parser.add_argument("--target", required=True, metavar="目录",
                        help="导出目录（存在则先清空；即 upload-artifact 的 path）")
    args = parser.parse_args(argv)
    source = events.default_source()
    anchored = _fix_anchors(source)
    bundle = events_io.export_bundle(source=source)
    target = Path(args.target)
    try:
        _reset_dir(target)
        events_io.write_bundle(bundle, target / BUNDLE_NAME)
        copied = _copy_manifest(bundle, target)
    except OSError as exc:
        print(f"ci_events：导出失败：{exc}", file=sys.stderr)
        return 1
    for finding in bundle["findings"]:
        print(f"ci_events：已排除 {finding['code']}：{finding['detail']}", file=sys.stderr)
    print(f"ci_events：导出 {len(bundle['events'])} 个事件、{len(bundle['anchors'])} 个锚点"
          f"（本次固定 {anchored} 个）、{copied} 个内容产物 → {target / BUNDLE_NAME}")
    return 0


# ---- summary：从导出的 bundle 渲染 verify/route 事件 ----

def _sort_key(event: dict) -> tuple:
    """确定性排序：时间、来源、seq（seq 缺失排前，保证任意 bundle 输出稳定）。"""
    seq = event.get("seq")
    return str(event.get("ts") or ""), str(event.get("source") or ""), seq if isinstance(seq, int) else 0


def _decision_suffix(decision) -> str:
    """依据后缀：by/rule 与理由引用；字段缺失只写有的部分。"""
    if not isinstance(decision, dict):
        return ""
    basis = "/".join(str(decision[key]) for key in ("by", "rule") if decision.get(key) is not None)
    reason = decision.get("reason")
    if basis and reason is not None:
        return f" · 依据 {basis}：{reason}"
    if basis:
        return f" · 依据 {basis}"
    return f" · 依据：{reason}" if reason is not None else ""


def _event_line(event: dict) -> str:
    """一行一个事件：检查/规则、状态、耗时、依据与理由、错误——与事件数据逐字同源。"""
    duration = f"（{event['duration_ms']}ms）" if isinstance(event.get("duration_ms"), int) else ""
    line = f"- {event.get('stage')}/{event.get('step')} {event.get('status')}{duration}"
    line += _decision_suffix(event.get("decision"))
    error = event.get("error")
    if isinstance(error, dict) and error.get("kind") is not None:
        line += f" · 错误 {error.get('kind')}"
    return line


def render_summary(bundle: dict) -> str:
    """verify/route 事件的确定性 Markdown：同一 bundle 输出逐字节相同；只读，不改数据。"""
    rows = [event for event in bundle["events"]
            if isinstance(event, dict) and event.get("stage") in SUMMARY_STAGES]
    rows.sort(key=_sort_key)
    chains = sorted({f"{event.get('source')} / {event.get('trace_id')}" for event in rows})
    lines = ["## harness 事件 summary", "",
             f"链：{'；'.join(chains) if chains else '—'}｜verify/route 事件 {len(rows)} 条"]
    lines += [_event_line(event) for event in rows]
    if bundle.get("findings"):
        lines.append(f"导出时排除 {len(bundle['findings'])} 项产物（见包内 findings）")
    lines.append("")
    return "\n".join(lines)


def summary_main(argv: list[str]) -> int:
    """summary 子命令：只读 bundle 文件（不依赖本机库），渲染失败/形状不符返回 2。"""
    parser = argparse.ArgumentParser(prog="ci_events summary",
                                     description="从导出的 bundle 渲染 verify/route 事件 summary")
    parser.add_argument("--bundle", required=True, metavar="文件", help="export 写出的 JSON 包")
    args = parser.parse_args(argv)
    try:
        bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        print(f"ci_events：读取 bundle 失败：{exc}", file=sys.stderr)
        return 2
    except ValueError:
        print(f"ci_events：{args.bundle} 不是有效的 JSON", file=sys.stderr)
        return 2
    if not isinstance(bundle, dict) or not isinstance(bundle.get("events"), list):
        print("ci_events：bundle 形状不符（应有 events 列表）", file=sys.stderr)
        return 2
    print(render_summary(bundle), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("用法：ci_events.py export --target <目录> | summary --bundle <文件>", file=sys.stderr)
        return 2
    name, rest = argv[0], argv[1:]
    if name == "export":
        return export_main(rest)
    if name == "summary":
        return summary_main(rest)
    print(f"未知子命令：{name}（export | summary）", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
