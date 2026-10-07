"""T717（B110）：disable_auto_merge 的文档字符串与行为一致。

四个断言：_run 成功（含返回空串，即 PR 本来就没开自动合并）时返回真，并按契约调用 _run 一次；
_run 抛 RuntimeError/OSError 时返回假、不向外抛、向标准错误打印含 PR 号与重试命令的诊断；
文档字符串与任务书审定原文逐字相同（规范化空白后）；除文档字符串外，函数定义与基线提交
6bee972 完全一致（AST 比较，证明签名、装饰器与方法体都没被顺手改动）。
桩用 GitHub 子类覆盖 _run，不调用真实 gh，不启动子进程。
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.agents.github import GitHub

# 任务书（docs/plans/task-717-disable-auto-merge-contract.md 目标终态第 1 条）审定的文档原文，
# 规范化空白后逐字比较。
APPROVED_DOC = """关闭 PR 已开启的自动合并（T715 否决信号）。

返回值只表示 `gh pr merge --disable-auto` 这条命令是否成功：成功返回真（PR 本来没有开启自动合并时 \
`gh` 也退出 0，同样返回真）；命令失败（RuntimeError）或 `gh` 无法执行（OSError）时只向标准错误打印\
诊断并返回假，不抛异常。它不告诉调用方是否真有请求被关闭——否决本体已经完成，这里的失败不改变调用\
方的结论与退出码。"""

# 基线源码（来源提交 6bee972）：git show 6bee972:engine/agents/github.py 中 disable_auto_merge
# 的完整源码，作字符串字面量逐字固定在这里（T717 验收第 4 行）。
BASELINE_SOURCE = '''def disable_auto_merge(self, pr: int) -> bool:
        """关闭 PR 已开启的自动合并（T715 否决信号）：成功返回真；PR 本来就没开或调用失败时只打印、
        返回假，不抛异常——否决本体已经完成，这里的失败不改变调用方的结论与退出码。"""
        try:
            self._run(["gh", "pr", "merge", str(pr), "--disable-auto"], agent=True)
            return True
        except (RuntimeError, OSError) as error:
            print(f"关闭 PR #{pr} 的自动合并未成功（{error}）；若它已开启自动合并，"
                  f"请手动 gh pr merge {pr} --disable-auto", file=sys.stderr)
            return False
'''


def _named_function(source: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "disable_auto_merge":
            return node
    raise AssertionError("源码里找不到 disable_auto_merge")


class StubGitHub(GitHub):
    """覆盖 _run 的桩：记录调用、返回给定值或抛给定异常，不启动子进程。"""

    def __init__(self, result_or_error: str | Exception):
        super().__init__()
        self.behavior = result_or_error
        self.calls: list[tuple[list[str], dict]] = []

    def _run(self, argv: list[str], cwd: Path | None = None, agent: bool = False,
             stdin: str | None = None) -> str:
        self.calls.append((argv, {"agent": agent, "cwd": cwd, "stdin": stdin}))
        if isinstance(self.behavior, Exception):
            raise self.behavior
        return self.behavior


class DisableAutoMergeContractTest(unittest.TestCase):
    def test_returns_true_when_the_command_succeeds_even_without_a_request(self):
        for stdout in ("", "已为 pull request #14 关闭自动合并。"):  # 空串 = PR 本来就没开自动合并
            with self.subTest(stdout=stdout):
                gh = StubGitHub(stdout)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertTrue(gh.disable_auto_merge(14))
                self.assertEqual(len(gh.calls), 1)  # 只调用一次 _run
                argv, kwargs = gh.calls[0]
                self.assertEqual(argv, ["gh", "pr", "merge", "14", "--disable-auto"])
                self.assertTrue(kwargs["agent"])

    def test_returns_false_and_prints_on_command_failure_or_missing_gh(self):
        for error in (RuntimeError("gh pr merge 失败：网络断开"), OSError("找不到 gh")):
            with self.subTest(error=error):
                gh = StubGitHub(error)
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertFalse(gh.disable_auto_merge(27))  # 返回假且不向外抛
                self.assertIn("#27", stderr.getvalue())
                self.assertIn("gh pr merge 27 --disable-auto", stderr.getvalue())

    def test_docstring_is_exactly_the_approved_text(self):
        normalized = lambda s: " ".join(s.split())
        self.assertEqual(normalized(inspect.getdoc(GitHub.disable_auto_merge)), normalized(APPROVED_DOC))

    def test_function_is_unchanged_apart_from_the_docstring(self):
        source = Path(__file__).resolve().parents[1] / "engine" / "agents" / "github.py"
        dumps = {}
        for name, text in (("基线 6bee972", BASELINE_SOURCE), ("当前源码", source.read_text(encoding="utf-8"))):
            fn = _named_function(text)
            first = fn.body[0]
            self.assertTrue(isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                            and isinstance(first.value.value, str), f"{name}：函数体第一条不是文档字符串")
            fn.body = fn.body[1:]  # 只删去第一个文档字符串节点；整个 FunctionDef（名称、参数与注解、装饰器、返回注解）照常比较
            dumps[name] = ast.dump(fn, include_attributes=False)
        self.assertEqual(dumps["基线 6bee972"], dumps["当前源码"])


if __name__ == "__main__":
    unittest.main()
