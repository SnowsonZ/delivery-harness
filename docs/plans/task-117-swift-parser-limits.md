---
task: T117
class: K7
risk: R3
designer: codex
size: small
architecture: true
spec_refs: []
no_spec_reason: 待办 B33 的 Swift 解析器两处已知限制修复，无产品规格验收编号
budget:
  wall_clock_min: 90
  ci_rounds: 2
  retries: 1
  tokens: null
rollback: git revert（仅在用户授权后）
---

# T117：Swift 质量棘轮解析两处已知限制

负责方：**派发任务**。设计方：codex 席位（现由 Zcode 代行）；独立评审方 OpenCode。依据：待办 B33（Agent-Notification PR #62 独立评审确认的两处限制）；[共用合同](2026-09-29-observability-task-contracts.md)C0。

## 目标终态

`engine/lang/swift.py` 的解析修复两处已知限制，既有判定行为在非受限输入上逐字不变：

1. 协议中的计算属性声明（`var x: T { get }`）不再被计为函数（当前误计入函数集合/函数体量）。
2. 字符串插值内嵌字符串（如 `"\("x")"`）不再使字符串解析提前结束：解析器识别插值内的引号配对，同行大括号计数不因此错乱。

修复方式由实现方按现有解析结构选择（状态机加插值深度、或预处理），但须满足：两条具名回归 + 既有 Swift 测试全类逐字通过；不新增第三方依赖。

实现前先在实际代码核对两处限制的现行表现，与上述不符时停下报告设计方，不自行补实现。

## 白名单

- `engine/lang/swift.py`
- `tests/test_swift_parser_limits.py`（新增）

## 非目标

不实现其他任务；不放宽原判定、预算、准入或守卫；不引入第三方依赖、不改版本/tag。除白名单外不改文件，不编辑已有测试、真实`.github/`、`.harness/`、Agent配置、依赖锁与ruff配置。共用合同C0同样适用。

## 前置条件

- 工作区基于含依赖的最新main。逐项核对实际接口，差异停下报告设计方，不自行补实现。
- 与同轮其他任务白名单不交叉。
- 改语言判定：设计方将做等价验证（G2，556 计数对照 + 消费方 Swift 检查输出对照）。
- 实现方先用已合并任务书通过 `bin/harness taskbook <本任务书> --on-main`；差异先修订任务书并经原审批。

## 验收

具名测试为本任务要新建的断言，当前尚未存在；不可把这张表当已通过证据。

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或步骤） | 未实现时怎样失败 |
|---|---|---|---|---|
| 不挂规格：B33 | 协议计算属性 `var x: T { get }` 不计入函数集合（含函数体量统计） | 夹具 | `tests.test_swift_parser_limits.SwiftParserLimitsTest.test_protocol_computed_property_not_function` | 仍计为函数时失败 |
| 不挂规格：B33 | 含插值内嵌字符串的行：字符串边界与大括号计数正确（`"a\(b("c"))d"` 类样例） | 夹具 | `…test_interpolated_string_braces` | 解析提前结束、计数错乱时失败 |
| 不挂规格：B33 | 既有 Swift 判定行为不变（既有测试全类逐字通过） | 夹具 | 既有 Swift 测试全类 | 行为被改动时失败 |

## 步骤与提交顺序

以下是实现与验证顺序，**不是当前提交授权**；仅在用户授权派发后，执行方按派发提示词提交。

| # | 改动 | 涉及文件 | 验证方式 | 对应验收 |
|---|---|---|---|---|
| 1 | 两处解析修复与回归断言 | `engine/lang/swift.py`、`tests/test_swift_parser_limits.py` | `python3 -W error::ResourceWarning -m unittest tests.test_swift_parser_limits.SwiftParserLimitsTest -v` 及既有 Swift 测试全类 | 本任务验收全部行 |
| 2 | 验证范围，整理交付证据 | `tests/` | `bin/verify --full` | C0与本任务验收 |

## 交付与升级

完成项目检查 `bin/verify --full`。设计方另做逐行验收与定向变异复核（回退两处修复，各自必须被对应断言抓住）；设计方做 G2（556 计数对照）。同一失败连续三轮无新证据时停止该路径；不得自行扩大白名单、改原任务书、实现非目标。
