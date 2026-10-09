# delivery-harness

AI 编码 Agent 的可验证交付引擎：判定器、护栏、风险判级与合并路由、派发与独立评审、可观测性（事件、trace、audit、告警）。以带锁文件的内置副本装进业务仓库（`.harness/engine/` + `.harness/engine.lock`）。对外说明见 README.md（英文）与 README.zh-CN.md，安全模型见 SECURITY.md。

## 开工先读

- 未关闭事项只记在 `docs/backlog.md`（待办清单）：开工和恢复中断时先读；新增、开始、关闭的规则见该文件「使用规则」。
- 路线与已定设计：`docs/plans/2026-09-29-roadmap.md`（阶段划分、安装形态、面向开源原则、接入无感化）。B46 可观测性已完成，设计与合同留在 `docs/plans/2026-09-29-observability-*.md` 备查；B81 自治试验设计见 `docs/plans/2026-10-03-autonomy-trial-design.md`。
- 首个使用方是 [Agent-Notification](https://github.com/SnowsonZ/Agent-Notification)（本机 `../session-manager`，主目录只留给用户）；抽离决定见其 `docs/decisions/0001-harness-extraction.md`。

## 开发与验证

- 引擎在 `engine/`，只依赖 Python 标准库；安装模板在 `templates/`；测试在 `tests/`。对外声明支持 Python ≥ 3.11，开发与 CI 实际用 3.12（本机 3.12.10）；3.14 下有已知失败（B125）。
- 验证：`bin/verify --full`；或直接 `python3 -m ruff check engine tests`（版本 0.16.8）与 `python3 -W error::ResourceWarning -m unittest discover -s tests -v`。Linux 由 `harness` 工作流的完整验证覆盖，macOS 由 `ci` 工作流三个分片覆盖。通过与否只认命令与 CI 输出，不手写。
- 消费方验证：CI 的 `consumer-contract` 用本次引擎升级 Agent-Notification 并跑其 `bin/verify --full`（不是必过项，须看结果；Linux 上跳过 Swift 等 macOS 专属检查）。改 Swift 解析、变异、质量指标或回放时，另在 Agent-Notification 的独立 worktree 中运行 `python3 <本仓库>/engine/cli.py upgrade --target <worktree> --allow-dirty`，比对测试数、质量指标、变异得分不变。不在其主目录试装。
- 一个设计拆成多个派发任务时按 `docs/task-splitting.md`：先跑通第一个任务，再拆完其余并做追溯表与独立拆分评审，一并提交。
- 升级已接入的项目（如 Agent-Notification）按 `docs/upgrading.md`；改配置项、命令接口或模板配合方式时，在 CHANGELOG 写 `**Migration:**` 条目，`upgrade` 靠它提示。
- 引擎与模板里不得出现使用者自己的值（账号、邮箱、模型、App、本机路径、项目名）：`tests/test_install.py` 的 OwnValuesTest 拦截；新增项目相关的值一律走 `.harness/config/` 配置，缺必填项明确报错。
- 引擎运行时的提示与文案目前为中文，国际化见待办 B47；代码注释、提交说明沿用中文，README.md、SECURITY.md、CHANGELOG.md 用英文。
- 告警、账本等命令会真实写 GitHub：文档验收与试跑只用假平台或 `--help`；平台设置由用户执行。

## 角色与派发

- 设计方（Claude Code 或 Codex）写设计与任务书、做设计方复核；执行方 Pi 经 `bin/dispatch` 在槽位工作树里实现；独立评审用 `bin/harness review pr <n>`，按 `rules.toml [review] chain` 选与设计方不同家的评审方。评审方只拿材料路径自行读取，不内联发送正文。
- `docs/runs/<任务书名>/<序号>.json` 记录一次派发尝试，只由 dispatcher 生成：不手改 exit，历史 stopped/clarify 保留；完成证据是成功记录加 CI、PR、合并资料。
- 续派前确认没有存活的旧执行方占用槽位，核对实际等待对象与输出；父编排进程退出不代表其 Pi 子进程结束（B77）。

## 提交、合并与发布

- 所有改动经 PR。判级以 `.harness/config/rules.toml` 为准（`.github/`、`.harness/`、`bin/` 与各 Agent 钩子目录是 R3；`engine/`、`templates/`、AGENTS.md 是 R2；README、CHANGELOG、`docs/plans/`、`docs/backlog.md` 等是 R0），放权以 `autonomy.toml` 为准：按类别由批准 App 自动合并，R2 满足合同制条件时也可自动合并（B81，仅本仓库试验），其余由用户批准合并。main 的 ruleset：非推送者批准最后一次推送、`harness` 与 `test (macos-latest)` 必过、禁止改写与删除。
- 推送、开 PR、评论用 Agent 账号：`bin/as-agent <命令>`（身份取自 `checks.toml [identity]`）；不合并、不批准 PR。推送后用 `git ls-remote origin refs/heads/<分支>` 核对与 HEAD 一致再告诉用户（本机网络会让推送静默失败）。
- 版本号与 tag 只由用户决定；发版前同步 `engine/__init__.py` 的 `__version__` 与 CHANGELOG。
- 用户可见的行为变化写进 CHANGELOG「Unreleased」；新增、迁移文档时同步 README 与本文件。

## 本机注意

- 本仓库已自举（B49）：装有自己的内置引擎副本（`.harness/engine/`，是上一次合并的引擎，引擎改动合并后要再走 `upgrade` PR 才对本仓库自己生效）、`bin/verify`、`bin/dispatch`、git 层与 Agent 层守卫。新环境先 `bin/harness guard-git install`（`pip install -r requirements-dev.txt` 装 ruff），提交前 `bin/verify`。
- 新 worktree 可能默认选到 Python 3.14，提交钩子会因缺 ruff 或 B125 失败：命令前加 `PATH=$HOME/.pyenv/shims:$PATH PYENV_VERSION=3.12.10`。
- 用户在对话中要的总结、解释直接在对话里答，不落文档（除非明确要求）。
