# delivery-harness

AI 编码 Agent 的可验证交付引擎：判定器、护栏、风险判级与合并路由、派发与独立评审。以带锁文件的内置副本装进业务仓库（`.harness/engine/` + `.harness/engine.lock`）。对外说明见 README.md（英文）与 README.zh-CN.md，安全模型见 SECURITY.md。

## 开工先读

- 未关闭事项只记在 `docs/backlog.md`（待办清单）：开工和恢复中断时先读；新增、开始、关闭的规则见该文件「使用规则」。
- 路线与已定设计：`docs/plans/2026-09-29-roadmap.md`（阶段划分、安装形态、面向开源原则、接入无感化、可观测性设计要点）。
- 首个使用方是 [Agent-Notification](https://github.com/SnowsonZ/Agent-Notification)（本机 `../session-manager`，主目录只留给用户）；抽离决定见其 `docs/decisions/0001-harness-extraction.md`。

## 开发与验证

- 引擎在 `engine/`，只依赖 Python 标准库（≥ 3.11）；安装模板在 `templates/`；测试在 `tests/`。
- 验证：`python3 -m ruff check engine tests`（版本 0.16.8）与 `python3 -W error::ResourceWarning -m unittest discover -s tests -v`，与 CI（Ubuntu、macOS）同口径。通过与否只认命令与 CI 输出，不手写。
- 在真实项目上验证（改判定逻辑、守卫、派发、配置读取时必做）：在 Agent-Notification 的独立 worktree 中运行 `python3 <本仓库>/engine/cli.py upgrade --target <worktree> --allow-dirty`，再跑它的 `bin/verify --full`（harness 契约测试 `tests/test_harness*.py` 暂在那边，待办 B42）。不在其主目录试装。
- 引擎与模板里不得出现使用者自己的值（账号、邮箱、模型、App、本机路径、项目名）：`tests/test_install.py` 的 OwnValuesTest 拦截；新增项目相关的值一律走 `.harness/config/` 配置，缺必填项明确报错。
- 引擎运行时的提示与文案目前为中文，国际化见待办；代码注释、提交说明沿用中文，README.md、SECURITY.md、CHANGELOG.md 用英文。

## 提交与发布

- 所有改动经 PR，由用户批准合并（main 受 ruleset 保护：非推送者批准最后一次推送、两项 CI 必过、禁止改写与删除）。整个仓库都是护栏本身，按 R3 对待。
- 推送、开 PR、评论用 Agent 账号 `Snowson`：`GH_TOKEN=$(gh auth token --user Snowson)` 且提交者设为该账号（参考 Agent-Notification 的 `bin/as-agent`）；不合并、不批准 PR。推送后用 `git ls-remote origin refs/heads/<分支>` 核对与 HEAD 一致再告诉用户（本机网络会让推送静默失败）。
- 版本号与 tag 只由用户决定；发版前同步 `engine/__init__.py` 的 `__version__` 与 CHANGELOG。
- 用户可见的行为变化写进 CHANGELOG「Unreleased」；新增、迁移文档时同步 README 与本文件。

## 本机注意

- 本仓库还没有装自己的 Agent 层与 git 层守卫（自举在待办），推 main、改写历史等本机不拦，只有服务端 ruleset 兜底：不要依赖本机拦截，按上面的流程操作。
- 用户在对话中要的总结、解释直接在对话里答，不落文档（除非明确要求）。
