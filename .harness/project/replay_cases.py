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
    Case(
        defect="E128-R1",
        title="评审入口不对共用评审工作区加锁，并发评审互相 checkout 并覆盖材料（#119 补审与 #120 评审）",
        file="engine/agents/review.py",
        find='    with review_lock.workspace_lock(root, workspace, timeout, purpose=f"review_pr #{number}"):',
        replace="    with open(os.devnull):",
        tests=("test_review_lock.ReviewLockTest.test_concurrent_reviews_serialized",),
    ),
]
GUARDED: dict[str, tuple[str, ...]] = {}
DEFERRED: dict[str, str] = {}
