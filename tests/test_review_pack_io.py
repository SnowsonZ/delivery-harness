"""T124 回归：评审 diff 材料落盘与 pack 材料清单（待办 B74：200K 字符静默截断的修复）。

夹具在匿名临时 git 仓库里走真实产品入口：小 diff 经 review.write_materials 组装，断言
diff.patch 与旧口径逐字一致（全量、单文件）；大 diff 经 review_pack_io.write_diff 分片，
断言文件边界对齐、每片 ≤ 2MB、按序拼接与原始 diff 逐字节等价；pack 经 review_pack.build
生成，断言材料清单列出全部 diff 文件与行数、分片时顶部含「分 N 片，无截断」说明。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.agents import review, review_pack, review_pack_io

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}


def line_count(data: bytes) -> int:
    return len(data.decode("utf-8").splitlines())


class ReviewPackIoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-review-pack-io-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("# app\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        # 观察旁路在夹具里关闭：不写事件库，也不依赖宿主环境
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        os.environ.pop("HARNESS_EVENTS", None)

    # ---------- 夹具 ----------

    def git(self, *args, check=True):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GIT_", "HARNESS_", "GITHUB_"))}
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=check)

    def commit(self, files: dict[str, str], message: str) -> str:
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def bulk_files(self, count: int) -> dict[str, str]:
        """count 个互不相干的新文件，总 diff 稳定超过 2MB（每文件约 185KB）。"""
        return {f"src/gen-{index:03d}.py": "".join(f"line {index:03d}-{row:05d} payload padding text\n"
                                                   for row in range(5000)) for index in range(count)}

    def raw_diff(self, base: str) -> bytes:
        """完整 diff 的原始字节（与生产 git() 同口径：--no-color、去结尾换行）。"""
        return self.git("diff", "--no-color", f"{base}...HEAD").stdout.rstrip("\n").encode("utf-8")

    def shard_files(self) -> list[Path]:
        return sorted((self.repo / "build" / "review").glob("diff*.patch"))

    # ---------- 验收 1：小 diff 单文件，内容与现状逐字一致 ----------

    def test_small_diff_unchanged(self):
        base = self.commit({"docs/note.md": "整理前\n"}, "before")
        self.commit({"docs/note.md": "整理后\n", "src/app.py": "print('hi')\n"}, "after")
        review.write_materials(self.repo, base, "# 夹具\n\n小改动。\n", "")
        folder = self.repo / "build" / "review"
        self.assertEqual([path.name for path in self.shard_files()], ["diff.patch"])  # 仍是单个文件
        self.assertEqual((folder / "diff.patch").read_bytes(), self.raw_diff(base))  # 全量，不再截断
        self.assertEqual((folder / "pr.md").read_text(encoding="utf-8"), "# 夹具\n\n小改动。\n")
        self.assertEqual((folder / "task.md").read_text(encoding="utf-8"), "无")
        data = (folder / "diff.patch").read_bytes()
        pack = review_pack.build(base, "HEAD", run_tests=False, cwd=self.repo)
        self.assertIn(f"- `diff.patch`：{line_count(data)} 行", pack)  # 材料清单列出 diff 文件与行数

    # ---------- 验收 2：超 2MB 按文件分片，无丢失、边界对齐 ----------

    def test_large_diff_sharded_no_loss(self):
        base = self.commit({"docs/note.md": "占位\n"}, "before")
        self.commit(self.bulk_files(45), "bulk change")
        expected = self.raw_diff(base)
        self.assertGreater(len(expected), review_pack_io.SHARD_LIMIT)  # 夹具确实超过单片上限
        files = review_pack_io.write_diff(self.repo, base)
        self.assertEqual(files, self.shard_files())  # 磁盘上没有多余的旧 diff 文件
        self.assertGreaterEqual(len(files), 2)
        self.assertEqual([path.name for path in files],
                         [f"diff-{index:02d}.patch" for index in range(1, len(files) + 1)])
        shards = [path.read_bytes() for path in files]
        self.assertEqual(b"".join(shards), expected)  # 按序拼接与完整 diff 逐字节等价：无丢失
        for shard in shards:
            self.assertLessEqual(len(shard), review_pack_io.SHARD_LIMIT)  # 每片 ≤ 2MB
        for shard in shards[1:]:
            self.assertTrue(shard.startswith(b"diff --git "))  # 分片只落在文件边界：不拆半个文件

    # ---------- 验收 3：分片时 pack 材料清单列全量片文件与行数、顶部声明无截断 ----------

    def test_pack_lists_shards(self):
        base = self.commit({"docs/note.md": "占位\n"}, "before")
        self.commit(self.bulk_files(45), "bulk change")
        files = review_pack_io.write_diff(self.repo, base)
        pack = review_pack.build(base, "HEAD", run_tests=False, cwd=self.repo)
        lines = pack.splitlines()
        self.assertTrue(lines[0].startswith("# 评审证据包"))
        self.assertEqual(lines[2], f"diff 分 {len(files)} 片，无截断。")  # 顶部的显式说明
        for path in files:  # 全部片文件与各自行数
            self.assertIn(f"- `{path.name}`：{line_count(path.read_bytes())} 行", pack)


if __name__ == "__main__":
    unittest.main()
