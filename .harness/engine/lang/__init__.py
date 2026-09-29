"""语言插件：每种语言提供签名比对（R1）、新增依赖识别（R1），可选提供函数体量与嵌套深度（质量棘轮）。

插件接口（模块级）：
    SUFFIXES: tuple[str, ...]
    signatures(source) -> dict[str, object]            函数 → 签名（base 中已有函数的签名不能变）
    new_dependencies(path, lines, local) -> list[str]   新增行里引入的外部依赖（理由文本）
    functions(source) -> [(名称, 行号, 函数体行数, 嵌套深度)]   可选
"""

from __future__ import annotations

from types import ModuleType

from engine.lang import python, swift

PLUGINS: tuple[ModuleType, ...] = (python, swift)


def for_path(path: str) -> ModuleType | None:
    return next((plugin for plugin in PLUGINS if path.endswith(plugin.SUFFIXES)), None)
