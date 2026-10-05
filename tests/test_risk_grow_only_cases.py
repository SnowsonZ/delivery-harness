"""T712 回放清单只追加判级测试：[risk] grow_only_cases 命中的回放清单在只向 CASES 末尾追加全字面量
Case(...) 时按 R2（grow_only_cases 规则，可与产品代码、测试同 PR 判 K4）；其余任何改动——修改/删除/调换
已有用例、改 BASELINE、GUARDED、DEFERRED、非字面量参数、夹带副作用顶层语句、CASES 赋值语句本身
（类型、目标、注解、simple 标志，修订 1）、语法错误、新增文件、缺省不配置——照旧按 R3，且判级只读
PR 内容、从不执行它。

夹具全部使用匿名临时 git 仓库，rules.toml 在夹具内配置 grow_only_cases（test_default_off 除外）；
不碰真实库、PR 或工作流。既有测试零修改。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.core import common
from engine.routing import policy, risk

REPLAY_PATH = ".harness/project/replay_cases.py"
EXTRA_PATH = ".harness/project/replay_extra.py"
RULES_TOML = """\
[risk]
r3 = [".harness/**"]
r2 = ["engine/**"]
r0 = ["docs/plans/**", "README*.md"]
tests = ["tests/**"]
contracts = []
taskbooks = []
shrink_only = []
golden = []
grow_only_cases = [".harness/project/replay_cases.py", ".harness/project/replay_extra.py"]
"""
# 同一份规则、仅去掉 grow_only_cases：缺省行为与现在完全一致（追加也判 R3）。
RULES_DEFAULT_OFF = RULES_TOML.replace(
    'grow_only_cases = [".harness/project/replay_cases.py", ".harness/project/replay_extra.py"]\n', "")
CASE_ONE = '''    Case(
        defect="X1-1",
        title="首个缺陷",
        file="engine/x.py",
        find="old1",
        replace="new1",
        tests=("test_x.TestX.test_one",),
    )'''
CASE_TWO = '''    Case(
        defect="X1-2",
        title="第二个缺陷",
        file="engine/y.py",
        find="old2",
        replace="new2",
        tests=("test_y.TestY.test_two",),
    )'''
NEW_CASE = '''    Case(
        defect="X1-3",
        title="新缺陷",
        file="engine/z.py",
        find="old3",
        replace="new3",
        tests=("test_z.TestZ.test_three",),
    )'''
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}


def replay(cases: list[str], *, baseline: str = "[]", guarded: str = "{}", deferred: str = "{}") -> str:
    """拼一份回放清单夹具；BASELINE、GUARDED、DEFERRED 的值由参数替换。"""
    body = ",\n".join(cases)
    return (
        '"""夹具回放清单。"""\n\nfrom engine.core.cases import Case\n\n'
        f"BASELINE: list[str] = {baseline}\n"
        f"CASES: list[Case] = [\n{body},\n]\n"
        f"GUARDED: dict[str, tuple[str, ...]] = {guarded}\n"
        f"DEFERRED: dict[str, str] = {deferred}\n"
    )


class GrowOnlyCasesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-risk-grow-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "app"
        (self.repo / ".harness/config").mkdir(parents=True)
        (self.repo / ".harness/project").mkdir(parents=True)
        (self.repo / "engine").mkdir()
        (self.repo / ".harness/config/rules.toml").write_text(RULES_TOML, encoding="utf-8")
        (self.repo / ".harness/config/rules-default-off.toml").write_text(RULES_DEFAULT_OFF, encoding="utf-8")
        (self.repo / REPLAY_PATH).write_text(replay([CASE_ONE, CASE_TWO]), encoding="utf-8")
        (self.repo / "engine/x.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        self.rules = common.load_rules(self.repo / ".harness/config/rules.toml")
        self.rules_off = common.load_rules(self.repo / ".harness/config/rules-default-off.toml")
        events = mock.patch.dict(os.environ, {"HARNESS_EVENTS": "off"})  # 不向真实事件库写测试事件
        events.start()
        self.addCleanup(events.stop)

    # ---- 夹具操作 ----

    def git(self, *args, check=True):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "HARNESS_"))}
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True,
                              env={**env, **GIT_ENV}, check=check)

    def fresh_head(self, files: dict[str, str], message: str, ref: str | None = None) -> str:
        """从 base（或指定 ref）起独立提交（子例之间互不叠加），返回 head 提交。"""
        self.git("checkout", "-q", "-B", "fixture-work", ref or self.base)
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def classify(self, head: str, rules: dict | None = None) -> risk.RiskReport:
        return risk.classify(self.base, head, cwd=self.repo, rules=rules or self.rules)

    def file_risk(self, report: risk.RiskReport, path: str = REPLAY_PATH) -> risk.FileRisk:
        return next(item for item in report.files if item.path == path)

    def assert_stays_r3(self, head: str, path: str = REPLAY_PATH, *, base: str | None = None) -> None:
        report = risk.classify(base or self.base, head, cwd=self.repo, rules=self.rules)
        item = self.file_risk(report, path)
        self.assertEqual(item.level, 3, item.reason)
        self.assertEqual(item.reason, "命中 R3 规则 `.harness/**`")
        self.assertEqual(report.level, 3)

    # ---- 验收 1：只追加全字面量 Case → R2；与产品代码、测试同改且带 Defect → 整体 R2、K4 ----

    def test_append_case_is_r2(self):
        head = self.fresh_head({
            REPLAY_PATH: replay([CASE_ONE, CASE_TWO, NEW_CASE]),
            "engine/x.py": "VALUE = 2\n",
            "tests/test_new.py": "def test_new():\n    assert True\n",
        }, "修复缺陷\n\nDefect: X1-3")
        with mock.patch.object(risk.events, "emit") as emit:
            report = self.classify(head)
        self.assertEqual(report.level, 2)
        item = self.file_risk(report)
        self.assertEqual((item.status, item.level, item.reason), ("M", 2, "回放清单只追加新用例"))
        decisions = {call.kwargs["outputs"]["path"]: call.kwargs["decision"]
                     for call in emit.call_args_list if call.args[1] == "risk.file"}
        self.assertEqual(decisions[REPLAY_PATH],
                         {"by": "risk", "rule": "grow_only_cases", "reason": "回放清单只追加新用例"})

        shas = common.git("rev-list", f"{self.base}..{head}", cwd=self.repo).split()
        defects = any(common.commit_field(sha, "Defect", cwd=self.repo) for sha in shas)
        self.assertTrue(defects)
        self.assertEqual(policy.machine_class(report, defects), "K4")

    # ---- 验收 2：修改、删除已有 Case 或调换顺序 → R3 ----

    def test_modify_or_delete_case_stays_r3(self):
        heads = {
            "modify": replay([CASE_ONE.replace("首个缺陷", "改过的标题"), CASE_TWO, NEW_CASE]),
            "delete": replay([CASE_ONE]),
            "reorder": replay([CASE_TWO, CASE_ONE, NEW_CASE]),
        }
        for label, text in heads.items():
            with self.subTest(label=label):
                self.assert_stays_r3(self.fresh_head({REPLAY_PATH: text}, f"改动 {label}"))

    # ---- 验收 3：DEFERRED、GUARDED 新增条目或改动 BASELINE（只增行）→ R3 ----

    def test_exemption_changes_stay_r3(self):
        variants = {
            "deferred": {"deferred": '{"X9-9": "暂缓原因"}'},
            "guarded": {"guarded": '{"X9-8": ("test_g.TestG.test_g",)}'},
            "baseline": {"baseline": '["X1-1"]'},
        }
        for label, kw in variants.items():
            with self.subTest(label=label):
                text = replay([CASE_ONE, CASE_TWO, NEW_CASE], **kw)
                self.assert_stays_r3(self.fresh_head({REPLAY_PATH: text}, f"豁免 {label}"))

    # ---- 验收 4：新增项非字面量或不是 Case(...) 调用 → R3 ----

    def test_non_literal_case_stays_r3(self):
        entries = {
            "call": NEW_CASE.replace('find="old3",', 'find=str("old3"),'),
            "fstring": NEW_CASE.replace('find="old3",', 'find=f"old{3}",'),
            "name": NEW_CASE.replace('tests=("test_z.TestZ.test_three",),', "tests=CASE_IDS,"),
            "not_case": '    "只是一条字符串"',
        }
        for label, entry in entries.items():
            with self.subTest(label=label):
                text = replay([CASE_ONE, CASE_TWO, entry])
                self.assert_stays_r3(self.fresh_head({REPLAY_PATH: text}, f"非字面量 {label}"))

    # ---- 验收 5：夹带副作用顶层语句 → R3，且判级过程不执行 PR 内容 ----

    def test_never_executes_pr_content(self):
        marker = self.repo / ".harness/project/touched-by-pr"
        side_effect = (
            'import pathlib as _p\n'
            '_p.Path(__file__).with_name("touched-by-pr").write_text("executed")\n\n'
            + replay([CASE_ONE, CASE_TWO, NEW_CASE])
        )
        self.assert_stays_r3(self.fresh_head({REPLAY_PATH: side_effect}, "夹带副作用"))
        self.assertFalse(marker.exists(), "判级过程执行了 PR 内容")

    # ---- 验收 6：语法错误、base 中没有该文件（状态 A）→ R3 且不抛异常 ----

    def test_unparsable_or_new_file_stays_r3(self):
        with self.subTest(label="syntax_error"):
            broken = replay([CASE_ONE, CASE_TWO, NEW_CASE]) + "def broken(:\n"
            self.assert_stays_r3(self.fresh_head({REPLAY_PATH: broken}, "语法错误"))
        with self.subTest(label="added_file"):
            head = self.fresh_head({EXTRA_PATH: replay([NEW_CASE])}, "新增清单文件")
            report = self.classify(head)
            item = self.file_risk(report, EXTRA_PATH)
            self.assertEqual((item.status, item.level), ("A", 3))
            self.assertEqual(report.level, 3)

    # ---- 验收（修订 1）：CASES 赋值语句除列表外的部分被改动 → R3 ----

    def test_cases_statement_other_parts_unchanged(self):
        appended = [CASE_ONE, CASE_TWO, NEW_CASE]
        variants = {
            # ① 注解换成有副作用的表达式：回放清单加载时会执行 BASELINE.clear()，清空基线（#146 评审反例）
            "annotation_side_effect": replay(appended).replace(
                "CASES: list[Case] = [", "CASES: (BASELINE.clear() or list[Case]) = ["),
            # ② 带注解赋值换成普通赋值：语句类型与注解都变了
            "ann_to_plain": replay(appended).replace("CASES: list[Case] = [", "CASES = ["),
            # ③ 目标加括号：simple 标志从 1 变 0，赋值目标形式被改动
            "parenthesized_target": replay(appended).replace(
                "CASES: list[Case] = [", "(CASES): list[Case] = ["),
        }
        for label, text in variants.items():
            with self.subTest(label=label):
                self.assert_stays_r3(self.fresh_head({REPLAY_PATH: text}, f"CASES 语句改动 {label}"))
        # ② 反向：base 是普通赋值，head 换成带注解赋值（head 须落在该 base 之上，三点 diff 才含互换）
        with self.subTest(label="plain_to_ann"):
            plain_base = self.fresh_head(
                {REPLAY_PATH: replay([CASE_ONE, CASE_TWO]).replace("CASES: list[Case] = [", "CASES = [")},
                "普通赋值基线")
            head = self.fresh_head({REPLAY_PATH: replay(appended)}, "换成带注解赋值", ref=plain_base)
            self.assert_stays_r3(head, base=plain_base)

    # ---- 验收 7：不配置 grow_only_cases 时行为与现在一致（追加也判 R3） ----

    def test_default_off(self):
        head = self.fresh_head({REPLAY_PATH: replay([CASE_ONE, CASE_TWO, NEW_CASE])}, "缺省关闭")
        report = self.classify(head, rules=self.rules_off)
        self.assertEqual(self.file_risk(report).level, 3)
        self.assertEqual(report.level, 3)


if __name__ == "__main__":
    unittest.main()
