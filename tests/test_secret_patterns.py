"""凭据模式（rules.toml [hygiene] secret_patterns）：真实形状的密钥照拦，普通长名字不误报（待办 B62）。

误报的来源：`sk-[A-Za-z0-9_-]{32,}` 没有前置边界，`task-` 的结尾恰好是 `sk-`，任务名在 `task-` 之后
有 32 个以上的字母数字与连字符时整条派发运行记录都被拦（T718 的任务名就是这样）。注意：本文件里长任务名一律用拼接构造，
否则提交本测试时会被尚未修好的旧规则拦下。
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEY = "sk-" + "A1b2C3d4" * 5  # 40 个字符的密钥形状（占位，不是真实密钥）
LONG_SLUG = "task-" + "718-ci-events-" + "import-real-github"  # 与 T718 同名；拼接构造，源码里不出现连续的长名字


def patterns(rel: str) -> list[re.Pattern[str]]:
    data = tomllib.loads((ROOT / rel).read_text(encoding="utf-8"))
    return [re.compile(item) for item in data["hygiene"]["secret_patterns"]]


def hit(rx_list: list[re.Pattern[str]], text: str) -> bool:
    return any(rx.search(text) for rx in rx_list)


class SecretPatternTest(unittest.TestCase):
    CONFIGS = (".harness/config/rules.toml", "templates/.harness/config/rules.toml")

    def test_real_shaped_keys_are_still_caught_wherever_they_appear(self):
        for rel in self.CONFIGS:
            rx = patterns(rel)
            for text in (KEY, f"key = {KEY}", f'"{KEY}"', f"OPENAI_API_KEY={KEY}", f"Bearer {KEY}", f"({KEY})",
                         f"\n{KEY}"):
                with self.subTest(config=rel, text=text[:24]):
                    self.assertTrue(hit(rx, text))

    def test_long_task_names_are_not_mistaken_for_keys(self):
        for rel in self.CONFIGS:
            rx = patterns(rel)
            for text in (LONG_SLUG, "task/" + LONG_SLUG[5:], "docs/runs/" + LONG_SLUG + "/1.json",
                         "task-" + "x" * 60, "des" + "k-" + "a" * 40,
                         "ris" + "k-" + "b" * 40, "mas" + "k-" + "c" * 40, "dis" + "k-" + "d" * 40):
                with self.subTest(config=rel, text=text[:30]):
                    self.assertFalse(hit(rx, text))

    def test_other_patterns_are_untouched(self):
        for rel in self.CONFIGS:
            rx = patterns(rel)
            for text in ("ghp_" + "a" * 36, "AKIA" + "A" * 16, "xoxb-" + "1" * 12,
                         "-----BEGIN RSA " + "PRIVATE KEY-----", "github_pat_" + "a" * 45):
                with self.subTest(config=rel, text=text[:20]):
                    self.assertTrue(hit(rx, text))

    def test_template_and_repo_config_agree_on_the_patterns(self):
        self.assertEqual([rx.pattern for rx in patterns(self.CONFIGS[0])],
                         [rx.pattern for rx in patterns(self.CONFIGS[1])])


if __name__ == "__main__":
    unittest.main()
