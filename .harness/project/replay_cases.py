"""本项目的事故回放用例：把历史缺陷重新注入代码，对应测试必须失败（格式见引擎 engine/core/cases.py）。"""

from engine.core.cases import Case

BASELINE: list[str] = []
CASES: list[Case] = [
    Case(
        defect="E123-R1",
        title="守卫解析 Python 合写短选项时只看第一个字母（-uX dev、-uW ignore 的值被当成脚本）",
        file="engine/core/shell_structure.py",
        find="                    continue  # 无值开关（-u、-B、-I……），继续看同一参数里的下一个字母",
        replace="                    break",
        tests=("test_guard_python_options.GuardPythonOptionsTest.test_escape_123_bypasses_denied",),
    ),
]
GUARDED: dict[str, tuple[str, ...]] = {}
DEFERRED: dict[str, str] = {}
