# 0001 harness 抽成独立引擎，以带锁文件的内置副本安装

> 状态：迁自 Agent-Notification（2026-10-09 快照，正文未改；业务库不再留存 harness 抽取与设计文档，只留引用）。文中指向业务库专属文件的链接已改为该仓库的绝对地址。
状态：已采纳（2026-09-29，用户决定）。

## 背景

harness 在本仓库建成并验证（阶段一、二），用户要开发第二个项目，并希望这一层面向开源、可装到任意项目。原先的 `harness/` 目录把通用机制与本项目数据（规则、基线、回放用例、Swift 检查、账号与模型）混在一起。

## 决定

1. 引擎抽到独立公开仓库 [delivery-harness](https://github.com/SnowsonZ/delivery-harness)，用 `git subtree split` 保留历史（推送前逐个提交扫描凭据与本机路径，0 命中）。
2. **安装形态为带锁文件的内置副本**：`.harness/engine/` + `.harness/engine.lock`（版本、引擎提交、目录树哈希），`verify` 的 integrity 检查拒绝绕过 `upgrade` 的改动，`.harness/**` 判 R3。
3. 引擎不带任何使用者自己的默认值；本项目的值写在 `.harness/config/`。
4. 本仓库先迁（T008），行为不变作为验收；第二个项目等引擎 v0.2（带可观测性与 `adopt`）再装。

## 理由与放弃的方案

守卫的可信来源是「经用户批准合并到 origin/main 的代码」：git 守卫读 origin/main 的规则，派发从 origin/main 导出守卫给执行方，自动合并执行 main 上的定义。内置副本与此完全同构。

| 方案 | 放弃原因 |
|---|---|
| pip/uv 装进本机环境 | 守卫在执行方可写的环境里，且不在 origin/main 上，信任链断 |
| git submodule | `git archive` 不含子模块，派发槽位还要逐个初始化；指针变更的审查不直观 |
| 可复用 workflow（`uses: …@v1`） | CI 跑引擎仓库的代码、本机跑另一份，形成两个来源 |

代价：每个业务仓库多约 5,500 行不需自己维护的代码；lint、质量棘轮、变异测试不覆盖该目录（由引擎仓库负责）。

## 影响

- 升级引擎：在 delivery-harness 检出中运行 `python3 engine/cli.py upgrade --target <本仓库>`，经 PR 由用户批准。
- 迁移过渡期内，git 守卫与派发在 origin/main 仍为旧布局时读 `harness/`，全部使用方迁移后删除（待办 B43）。
