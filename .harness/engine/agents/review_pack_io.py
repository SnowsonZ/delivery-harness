"""评审 diff 材料的落盘（T124，待办 B74）：去掉 200K 字符静默截断，大 diff 按文件边界分片。

总量 ≤ SHARD_LIMIT 时维持旧形态：单个 `build/review/diff.patch`（`git diff --no-color` 加
git() 的去结尾换行口径不变，只是不再截断）。超过时按文件边界分片为 `diff-01.patch`、
`diff-02.patch`…：各片按序拼接与完整 diff 逐字节等价，分片只落在文件边界（不拆半个文件）。
单个文件的 diff 本身超过 SHARD_LIMIT 时独占一片（此时该片必然超上限：文件边界优先于片大小）。
分片清单由 review_pack 作为消费方在 pack.md 里列出（文件名与行数、分 N 片声明）。
"""

from __future__ import annotations

import re
from pathlib import Path

from engine.core.common import git

SHARD_LIMIT = 2 * 1024 * 1024  # 每片上限 2MB；总量以内维持单个 diff.patch
_FILE_HEADER = re.compile(rb"(?m)^diff --git ")


def write_diff(workspace: Path, base: str) -> list[Path]:
    """把 `base...HEAD` 的评审 diff 写入 workspace 的 build/review/，返回写入的文件（按片序）。

    替代 write_materials 原来按 200_000 字符静默截断的 diff 段；写入前清掉目录里不再属于本次
    结果的旧 diff 文件，保证 pack 的材料清单与磁盘一致（评审工作区跨 PR 复用时尤其如此）。
    """
    diff = git("diff", "--no-color", f"{base}...HEAD", cwd=workspace).encode("utf-8")
    folder = workspace / "build" / "review"
    folder.mkdir(parents=True, exist_ok=True)
    shards = _shards(_file_chunks(diff))
    files = [folder / ("diff.patch" if len(shards) == 1 else f"diff-{index:02d}.patch")
             for index in range(1, len(shards) + 1)]
    for stale in folder.glob("diff*.patch"):
        if stale not in files:
            stale.unlink()
    for path, shard in zip(files, shards):
        path.write_bytes(shard)
    return files


def _file_chunks(diff: bytes) -> list[bytes]:
    """按文件边界切块：每个行首 `diff --git ` 开启一个文件段（diff 的正文行都有 +/空格/- 前缀，
    不会误配）；没有 diff 或只有单个文件段时原样返回一段（空 diff 也写一个空的 diff.patch，与
    旧行为一致）。"""
    starts = [match.start() for match in _FILE_HEADER.finditer(diff)]
    if len(starts) <= 1:
        return [diff]
    if starts[0] > 0:  # 理论上不出现的前导内容并入第一段
        starts.insert(0, 0)
    return [diff[start:end] for start, end in zip(starts, [*starts[1:], len(diff)])]


def _shards(chunks: list[bytes]) -> list[bytes]:
    """贪心装片：整段装入，再放下一段会超上限时先封片；单个文件段本身超 SHARD_LIMIT 时独占一片。"""
    shards: list[bytes] = []
    current: list[bytes] = []
    size = 0
    for chunk in chunks:
        if current and size + len(chunk) > SHARD_LIMIT:
            shards.append(b"".join(current))
            current, size = [], 0
        current.append(chunk)
        size += len(chunk)
    if current:
        shards.append(b"".join(current))
    return shards
