# delivery-harness

AI 编码 Agent 的可验证交付引擎：判定器、护栏、风险判级与合并路由、派发与独立评审、可观测性（事件、trace、audit、告警）。以带锁文件的内置副本装进业务仓库（`.harness/engine/` + `.harness/engine.lock`）。对外说明见 README.md（英文）与 README.zh-CN.md，安全模型见 SECURITY.md。

本文件适用于所有在本仓库工作的人与 Agent。**维护者**（负责派发、合并路由与发版的人）及其 Agent 另读 [docs/maintainers.md](docs/maintainers.md)。

## 开工先读

- 未关闭事项只记在 `docs/backlog.md`（待办清单）：开工和恢复中断时先读；新增、开始、关闭的规则见该文件「使用规则」。
- 路线与已定设计：`docs/plans/2026-09-29-roadmap.md`（阶段划分、安装形态、面向开源原则、接入无感化）。已完成阶段的设计与合同留在 `docs/plans/` 备查。
- 系统设计正源：`docs/plans/2026-09-27-target-state-design.md`（目标态设计：八环节 + 安全/人的角色/度量/自治四横切、三阶段路线），理论调研快照在 `docs/research/2026-09-23-agent-delivery-theory.md`，抽取决策在 `docs/decisions/`，建设期评审记录在 `docs/review/`。2026-10-09 起 harness 设计、调研与评审文档归本仓库，Agent-Notification 只留引用。

## 结构

- 产品是 `engine/`（只依赖 Python 标准库）与 `templates/`（安装时写入业务仓库，不覆盖已有文件）；测试在 `tests/`。
- 本仓库用自己的引擎开发自己（自举）：`.harness/`、`bin/`、git 与 Agent 钩子、`.github/workflows/` 是本仓库的实例，不是产品。`.harness/engine/` 是上一次合并的引擎副本，不手改（`integrity` 检查会失败）；引擎改动合并后经 `upgrade` PR 才对本仓库生效。
- 对外声明支持 Python ≥ 3.11，CI 实际用 3.12；3.14 下有已知失败（B125）。

## 开发与验证

- 新环境：`pip install -r requirements-dev.txt`（ruff 0.16.8），`bin/harness guard-git install`。
- 验证：`bin/verify --full`；或直接 `python3 -m ruff check engine tests` 与 `python3 -W error::ResourceWarning -m unittest discover -s tests -v`。Linux 由 `harness` 工作流覆盖，macOS 由 `ci` 工作流三个分片覆盖。通过与否只认命令与 CI 输出，不手写。
- 告警、账本等命令会真实写 GitHub：文档验收与试跑只用假平台或 `--help`。

## 硬约束

- 引擎与模板里不得出现使用者自己的值（账号、邮箱、模型、App、本机路径、项目名）：`tests/test_install.py` 的 OwnValuesTest 拦截；项目相关的值一律走 `.harness/config/` 配置，缺必填项明确报错。
- 不为通过检查而削弱判定、已有测试、质量棘轮、守卫或隐私过滤；检查本身有误时单独修正并说明理由。
- 改配置项、命令接口或模板配合方式时，在 CHANGELOG 写 `**Migration:**` 条目（`upgrade` 靠它提示，流程见 `docs/upgrading.md`）；其他用户可见的行为变化写进 CHANGELOG「Unreleased」。
- 语言：引擎运行时文案、代码注释、提交说明、开发文档用中文（国际化见待办 B47）；README.md、SECURITY.md、CHANGELOG.md 用英文。新增、迁移文档时同步 README 与本文件。

## 提交与合并

- 所有改动经 PR，`harness` 与 `test (macos-latest)` 两项 CI 必过；main 禁止改写与删除。
- 风险等级以 `.harness/config/rules.toml` 为准：`.github/`、`.harness/`、`bin/` 与各 Agent 钩子目录是 R3（护栏），`engine/`、`templates/`、AGENTS.md 是 R2，`docs/specs/`、`docs/templates/` 与 `docs/plans/` 下日期前缀的设计与计划文档是合同（R2），任务书 `docs/plans/task-*.md` 另按其类别判级，README、CHANGELOG、`docs/backlog.md` 等是 R0。护栏改动必须由维护者审。
