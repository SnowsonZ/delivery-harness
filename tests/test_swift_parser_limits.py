"""待办 B33 的两处 Swift 解析限制回归：协议计算属性不计为函数；插值内嵌字符串不再提前终止。

只断言 engine.lang.swift 的解析结果（quality 指标 swift_* 的直接数据源），夹具为最小 Swift 片段。
既有判定行为的等价性由 tests.test_events_checks 全类与 bin/verify --full 覆盖，不在此重复。
"""

from __future__ import annotations

import textwrap
import unittest

from engine.lang import swift


class SwiftParserLimitsTest(unittest.TestCase):
    def test_protocol_computed_property_not_function(self):
        """协议里的计算属性与成员声明只有要求、没有实现体，不计入函数集合（含体量统计）。"""
        source = textwrap.dedent(
            """\
            protocol Repository {
                var count: Int { get }
                var loaded: Bool { get set }
                func fetch() -> Int
            }
            struct Repo: Repository {
                var count: Int { 0 }
                func fetch() -> Int { 0 }
            }
            """
        )
        self.assertEqual(swift.functions(source), [("count", 7, 1, 1), ("fetch", 8, 1, 1)])
        # 协议独占一个文件时函数集合为空；类型内计算属性仍计入（不过度排除）
        self.assertEqual(swift.functions("protocol P {\n    var x: Int { get }\n    func f()\n}\n"), [])
        self.assertEqual(swift.functions("struct S {\n    var body: Int { 0 }\n}\n"), [("body", 2, 1, 1)])

    def test_interpolated_string_braces(self):
        """插值内嵌字符串不提前终止外层字符串：字面量整体抹平，同行大括号计数不受影响。"""
        literal = r'"a\(b("c"))d"'
        self.assertEqual(swift.strip(literal), " " * len(literal))

        # 插值含闭包大括号与内嵌字符串：抹平后只剩函数体自身的大括号，体量与深度正确
        source = 'func f() {\n    let a = ' + r'"\(items.map { "\($0)" }.count)"' + "\n}"
        stripped = swift.strip(source)
        self.assertNotIn('"', stripped)
        self.assertEqual(stripped.count("{"), 1)
        self.assertEqual(swift.functions(source), [("f", 1, 3, 1)])


if __name__ == "__main__":
    unittest.main()
