"""事故回放用例的数据结构与项目用例的加载。

项目在 `.harness/project/replay_cases.py` 中登记自己的历史缺陷（引擎不带任何项目的用例）：

    from engine.core.cases import Case

    BASELINE = ["D1", ...]              # 必须有覆盖的缺陷编号（注入、守卫测试或写明原因的暂缓）
    CASES = [Case("D1", "标题", "src/mod.py", "原文（恰好出现一次）", "注入后的文字", ("test_mod",)), ...]
    GUARDED = {"D2": ("test_guard.GuardTest.test_x",), ...}   # 过程类失败由守卫测试覆盖
    DEFERRED = {"D3": "暂缓原因", ...}

Case.tests 里是在 tests/ 下运行的 Python 测试 id，或 checks.toml [replay.suites.<名字>] 登记的测试套件名。
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from types import SimpleNamespace

from engine.core.common import PROJECT_DIR, setting


def suites() -> dict[str, dict]:
    """checks.toml [replay.suites.<名字>]：shell 命令与 requires（如 "macos"）。"""
    return dict(setting("replay", "suites", {}))


@dataclass(frozen=True)
class Case:
    defect: str
    title: str
    file: str
    find: str
    replace: str
    tests: tuple[str, ...]

    @property
    def platform(self) -> str:
        registered = suites()
        needs = {registered[name].get("requires", "any") for name in self.tests if name in registered}
        return "macos" if "macos" in needs else "any"


def load_project_cases() -> SimpleNamespace:
    """读取项目的回放用例；项目没有登记时返回空集合。"""
    path = PROJECT_DIR / "replay_cases.py"
    empty = SimpleNamespace(BASELINE=[], CASES=[], GUARDED={}, DEFERRED={})
    if not path.exists():
        return empty
    spec = importlib.util.spec_from_file_location("harness_project_replay_cases", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return SimpleNamespace(
        BASELINE=list(getattr(module, "BASELINE", [])),
        CASES=list(getattr(module, "CASES", [])),
        GUARDED=dict(getattr(module, "GUARDED", {})),
        DEFERRED=dict(getattr(module, "DEFERRED", {})),
    )
