"""文档熵治理：已跟踪 Markdown 的相对链接必须指向存在的文件；报告状态陈旧的文档。

范围：仓库根的 README*.md 与 AGENTS.md、docs/ 下全部 .md（只看 git 已跟踪的文件）。

  断链（失败）  行内链接 `[文字](目标)`、图片与引用式定义 `[名字]: 目标` 中的相对路径，按所在文件的目录解析
                （以 / 开头的按仓库根解析），目标文件或目录不存在即失败。http(s)、mailto 等带协议的链接与纯锚点
                `#…` 不检查；`文件#锚点` 只核对文件存在，不核对锚点。代码块与行内代码里的内容不算链接。
  陈旧状态（只报告）  「状态：」一行写着待执行、进行中、草案等未完成字样，且该文件最近一次 git 提交早于 30 天。
                     它提示文档可能已与实际脱节，由评审方收尾时核对（待办清单「定期核对」），不阻塞合并。

    bin/harness docs
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path
from urllib.parse import unquote

from engine.core.common import ROOT, git

PATTERNS = ("README*.md", "AGENTS.md", "docs/*.md")  # git 路径通配里 * 可跨目录
STALE_DAYS = 30
UNFINISHED = ("待执行", "进行中", "草案", "待实施", "实施中", "待派发", "未开始", "待评审")

_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1")
_INLINE_LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*(<[^>]*>|[^\s)]+)(?:\s+[\"'(][^)]*)?\)")
_REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
# 状态行：行首（可带引用号、加粗），或跟在同一行的「日期：… · 」之后；正文与列表里顺带提到的「状态：」不算。
_STATUS = re.compile(r"^(?:[^\n]*·\s*)?(?:>\s*)?\**(?:当前)?状态\**\s*[:：]\s*(.+)")


def tracked_markdown(root: Path = ROOT) -> list[str]:
    output = git("ls-files", "--", *PATTERNS, cwd=root, isolate=True)
    return sorted(line for line in output.splitlines() if line.endswith(".md"))


def _content_lines(text: str):
    """逐行给出（行号，去掉行内代码后的内容），跳过围栏代码块。"""
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        opener = _FENCE.match(line)
        if fence:
            if opener and opener.group(1) == fence:
                fence = None
            continue
        if opener:
            fence = opener.group(1)
            continue
        yield number, _INLINE_CODE.sub("", line)


def link_targets(text: str) -> list[tuple[int, str]]:
    targets = []
    for number, line in _content_lines(text):
        found = [match.group(1) for match in _INLINE_LINK.finditer(line)]
        reference = _REFERENCE.match(line)
        if reference:
            found.append(reference.group(1))
        targets.extend((number, target.strip("<>")) for target in found)
    return targets


def broken_links(root: Path, relative: str) -> list[str]:
    source = root / relative
    problems = []
    for number, target in link_targets(source.read_text(encoding="utf-8")):
        if not target or target.startswith("#") or _SCHEME.match(target):
            continue
        path = unquote(target.split("#", 1)[0].split("?", 1)[0])
        if not path:
            continue
        resolved = root / path.lstrip("/") if path.startswith("/") else source.parent / path
        if not resolved.exists():
            problems.append(f"{relative}:{number}: 断链 {target}")
    return problems


def unfinished_status(text: str) -> str | None:
    for _number, line in _content_lines(text):
        match = _STATUS.search(line)
        if not match:
            continue
        # 只看状态值的第一句，免得把后文提到的词当成状态。
        value = re.split(r"[。；;]", match.group(1), maxsplit=1)[0]
        if any(word in value for word in UNFINISHED):
            return value.strip("* ")
    return None


def stale_documents(root: Path, files: list[str], now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    stale = []
    for relative in files:
        status = unfinished_status((root / relative).read_text(encoding="utf-8"))
        if not status:
            continue
        stamp = git("log", "-1", "--format=%ct", "--", relative, cwd=root, isolate=True, check=False)
        if not stamp:
            continue
        days = int((now - int(stamp)) // 86400)
        if days > STALE_DAYS:
            stale.append(f"{relative}：状态「{status}」，最近一次提交在 {days} 天前")
    return stale


def main(argv: list[str] | None = None, root: Path = ROOT, now: float | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args(argv)
    files = tracked_markdown(root)
    broken = [problem for relative in files for problem in broken_links(root, relative)]
    stale = stale_documents(root, files, now)
    print(f"检查了 {len(files)} 份 Markdown")
    for problem in broken:
        print(f"✗ {problem}")
    if stale:
        print(f"状态可能陈旧（超过 {STALE_DAYS} 天未提交，只报告）：")
        for item in stale:
            print(f"  - {item}")
    else:
        print("状态陈旧：无")
    if broken:
        print(f"✗ {len(broken)} 处断链：修正链接或补回目标文件")
        return 1
    print("✓ 相对链接均指向存在的文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
