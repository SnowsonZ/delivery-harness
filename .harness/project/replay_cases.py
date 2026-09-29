"""本项目的事故回放用例：把历史缺陷重新注入代码，对应测试必须失败（格式见引擎 engine/core/cases.py）。"""

from engine.core.cases import Case

BASELINE: list[str] = []
CASES: list[Case] = []
GUARDED: dict[str, tuple[str, ...]] = {}
DEFERRED: dict[str, str] = {}
