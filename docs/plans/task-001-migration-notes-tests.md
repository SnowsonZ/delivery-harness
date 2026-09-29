---
task: T001
class: K2
risk: R0
designer: claude-code
size: small
architecture: false
spec_refs: []
no_spec_reason: 冒烟任务（B49 自举后首次派发），只补测试，没有规格验收编号
budget:
  wall_clock_min: 30
  ci_rounds: 3
  retries: 2
  tokens: null
rollback: git revert
---

# 任务：为 `install.migration_notes` 补边界测试

来源：可观测性执行计划（docs/plans/2026-09-29-observability-execution-plan.md）阶段 0.8，用一个真实但很小的任务走通「派发 → 本地判定 → 开 PR → CI → 评审」全链。

## 目标终态

`engine/core/install.py` 的 `migration_notes(previous_version)` 有覆盖下列行为的单元测试，全部通过，产品代码不变：

1. `previous_version` 为空字符串时返回空列表。
2. 只返回**晚于** `previous_version` 的版本段里的条目；等于或早于它的版本段被忽略。
3. `Unreleased` 段的条目无论 `previous_version` 是多少都返回。
4. 只收以 `**Migration:**` 开头（允许前导 `- ` 与空格）的行；正文里提到该标记但不以它开头的行被忽略。
5. 返回的条目去掉前导 `- ` 与首尾空白。
6. CHANGELOG.md 不存在时返回空列表。

测试用临时目录里的假 CHANGELOG：`unittest.mock.patch.object(install, "ENGINE_DIR", <临时目录>/"engine")`，`migration_notes` 读的是 `ENGINE_DIR.parent / "CHANGELOG.md"`。

## 非目标与禁止动作

- 本任务不做：改 `migration_notes` 或任何 `engine/`、`templates/`、`.harness/` 下的文件；改已有测试；修改 CHANGELOG、README。
- 禁止：改动已有测试或判定器、编辑任务书与规格；改写已推送历史；推 tag。若发现 `migration_notes` 有缺陷，不要修，写进升级包交给评审方。

## 前置条件（不满足就停下报告，不要硬做）

- `engine/core/install.py` 里存在 `migration_notes`，参数为 `previous_version: str`，返回 `list[str]`。

## 验收

| 编号 | 验收内容 | 证据类型 | 覆盖（测试名或验证步骤） |
|---|---|---|---|
| 不挂规格：冒烟任务，只补测试 | 上面第 1–6 条行为各有断言，测试通过 | 单测 | `test_migration_notes.MigrationNotesTest` |

## 步骤与提交顺序

| # | 改动 | 涉及文件 | 验证方式（命令或测试） | 对应验收 |
|---|---|---|---|---|
| 1 | 新增测试文件，覆盖第 1–6 条 | `tests/test_migration_notes.py` | `python3 -m unittest tests.test_migration_notes -v` | 上表 |

## 参考

- 被测函数：`engine/core/install.py` 的 `migration_notes`
- 同类测试写法：`tests/test_install.py` 的 `UpgradeSourceTest`（同样用 `mock.patch.object(install, "ENGINE_DIR", ...)`）

## 交付要求

- 提交前 `bin/verify` 通过；改动测试或产品逻辑后跑 `bin/verify --full`
- PR 正文、评论先写进 `build/` 下的文件，再用 `--body-file` 传入
- PR 附当前 head 的 CI 链接；「已修复」「测试通过」由 CI 的 harness job 生成，不手写
- 卡住时按下方格式升级，不要无限重试

## 升级包（卡住或需要澄清时填写）

验收无法判定、前置条件不满足、存在多个合理方案时停下提交，不自行扩大或缩小范围。

- 当前状态与目标差距：
- 已尝试的方案与结果：
- 证据（失败日志、测试输出、截图）：
- 可选方案与推荐：
- 需要评审方或用户决定的具体问题：
