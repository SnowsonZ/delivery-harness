"""migration_notes 的边界测试：版本段过滤、Unreleased 恒返回、`**Migration:**` 前缀识别与空白处理。

被测函数在 engine/core/install.py，读的是 ENGINE_DIR.parent / "CHANGELOG.md"；测试用临时目录里的
假 CHANGELOG 并 patch install.ENGINE_DIR，不依赖真实仓库的 CHANGELOG 内容。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENGINE_REPO = Path(__file__).resolve().parents[1]

# 覆盖全部行为的假 CHANGELOG：各版本段交错放置，含前导空白、行尾空白、无空格短横线、
# 正文提到标记但不以它开头、以及非迁移条目等干扰项。
CHANGELOG = (
    "# Changelog\n"
    "\n"
    "## Unreleased\n"
    "\n"
    "  -  **Migration:** 未发布条目   \n"
    "- **Fix:** 不是迁移条目\n"
    "\n"
    "## 1.2.0 - 2026-10-01\n"
    "\n"
    "- **Migration:** 晚于 1.1.0 的条目\n"
    "- Fix: 正文提到 **Migration:** 但不以它开头\n"
    "\n"
    "## 1.1.5\n"
    "\n"
    "-**Migration:** 紧贴标题符号也算晚于\n"
    "\n"
    "## 1.1.0\n"
    "\n"
    "- **Migration:** 等于 previous_version，忽略\n"
    "\n"
    "## 1.0.0\n"
    "\n"
    "- **Migration:** 早于 previous_version，忽略\n"
)


def migration_notes(previous_version: str, engine_dir: Path) -> list[str]:
    """在被测模块上调用 migration_notes，ENGINE_DIR 指向临时目录，不依赖当前工作目录。"""
    sys.path.insert(0, str(ENGINE_REPO))
    from engine.core import install

    with mock.patch.object(install, "ENGINE_DIR", engine_dir):
        return install.migration_notes(previous_version)


class MigrationNotesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-mn-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.engine_dir = self.tmp / "engine"
        self.changelog = self.tmp / "CHANGELOG.md"
        self.changelog.write_text(CHANGELOG, encoding="utf-8")

    def test_empty_previous_version_returns_no_notes(self):
        self.assertEqual(migration_notes("", self.engine_dir), [])

    def test_only_versions_after_previous_version_apply(self):
        # 等于（1.1.0）与早于（1.0.0）的版本段被忽略；条目按文件顺序返回。
        self.assertEqual(
            migration_notes("1.1.0", self.engine_dir),
            [
                "**Migration:** 未发布条目",
                "**Migration:** 晚于 1.1.0 的条目",
                "**Migration:** 紧贴标题符号也算晚于",
            ],
        )

    def test_unreleased_applies_to_any_previous_version(self):
        self.assertEqual(
            migration_notes("99.0.0", self.engine_dir),
            ["**Migration:** 未发布条目"],
        )

    def test_only_lines_starting_with_marker_are_collected(self):
        # 所有版本段都生效时，也只收以 **Migration:** 开头的行：**Fix:** 条目与
        # 「正文提到标记但不以它开头」的行都在上面 CHANGELOG 里，且不应出现在结果中。
        self.assertEqual(
            migration_notes("0.0.0", self.engine_dir),
            [
                "**Migration:** 未发布条目",
                "**Migration:** 晚于 1.1.0 的条目",
                "**Migration:** 紧贴标题符号也算晚于",
                "**Migration:** 等于 previous_version，忽略",
                "**Migration:** 早于 previous_version，忽略",
            ],
        )

    def test_entries_are_stripped(self):
        # 前导 “- ” 与空白、首尾空白都去掉：上面 CHANGELOG 的未发布条目源串是
        # “  -  **Migration:** 未发布条目   ”，返回值应是干净的条目文本。
        notes = migration_notes("99.0.0", self.engine_dir)
        self.assertEqual(notes, [note.strip().lstrip("- ") for note in notes])
        self.assertIn("**Migration:** 未发布条目", notes)

    def test_missing_changelog_returns_no_notes(self):
        self.changelog.unlink()
        self.assertEqual(migration_notes("1.0.0", self.engine_dir), [])


if __name__ == "__main__":
    unittest.main()
