"""base_tests 的测试进程入口：跑 tests/，并核对 unittest 在运行中没有被篡改（评审 PR7-R7）。

base 版本的测试文件还原了，但它们 import 的是 head 的产品代码；产品模块可以在 import 时替换
`TestCase.assertEqual` 之类，让测试「全绿」。这里在加载测试前给 unittest 各模块及其类的属性拍快照，
运行后比对；再跑一组必然失败的哨兵用例，框架被改成不会失败时即可发现。

同一进程里的检查挡不住专门针对本文件的篡改（例如识别哨兵用例名），这一残余由 risk.py 对产品代码
引用测试框架的标记与评审兜底，见 docs/specs/delivery-harness.md「已知边界」。

退出码：0 通过；1 测试失败；3 测试框架被篡改。最后一行 stderr 是摘要。
"""

from __future__ import annotations

import os
import sys
import unittest
import unittest.case
import unittest.loader
import unittest.result
import unittest.runner
import unittest.suite

TAMPERED = 3
MODULES = (unittest, unittest.case, unittest.loader, unittest.result, unittest.runner, unittest.suite)


def snapshot() -> dict[tuple[str, str, str], object]:
    snap: dict[tuple[str, str, str], object] = {}
    for module in MODULES:
        for name, value in list(vars(module).items()):
            snap[(module.__name__, "", name)] = value
            if isinstance(value, type) and value.__module__.startswith("unittest"):
                for attr, member in list(vars(value).items()):
                    snap[(module.__name__, name, attr)] = member
    return snap


class _Sentinel(unittest.TestCase):
    """每一条都必须失败或出错；框架被改成「不会失败」时这里会通过。"""

    def test_assert_equal(self):
        self.assertEqual(1, 2)

    def test_assert_true(self):
        self.assertTrue(False)

    def test_raises(self):
        raise RuntimeError("sentinel")


def main() -> int:
    sys.path[0] = os.getcwd()  # 与 `python -m unittest` 一致：以仓库根为导入起点，而不是 harness/
    before = snapshot()
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner().run(suite)

    changed = [".".join(part for part in key if part) for key, value in snapshot().items() if key in before and before[key] is not value]
    sentinel = unittest.TestResult()
    unittest.defaultTestLoader.loadTestsFromTestCase(_Sentinel).run(sentinel)
    caught = len(sentinel.failures) + len(sentinel.errors)
    if changed or caught != 3:
        detail = "、".join(changed[:5]) if changed else f"哨兵用例只有 {caught}/3 条失败"
        print(f"TAMPERED：测试框架在运行中被篡改（{detail}）", file=sys.stderr)
        return TAMPERED
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
