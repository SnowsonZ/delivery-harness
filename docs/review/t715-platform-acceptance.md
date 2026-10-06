# T715 平台验收记录（待办 B106）

日期 2026-10-07（时间戳为本地时间 UTC+8，GitHub 运行时间为 UTC，差 8 小时）。引擎：本仓库内置 a8b78e6（含 T715，#163 合并后）。仓库设置 Allow auto-merge 已开启；同步 App 与批准 App 均已配置；ruleset `main` 开启 strict。

测试 PR：#164、#165（误选未归类路径 `docs/acceptance/`，按 R2 转人审，已关闭，改用 R0 路径 `docs/review/`）；#166、#167（P1）；#168、#169、#170（P2b）；#172（P3、P4，故意放在 R2 路径，手动开启自动合并，不合并，已关闭）。

## 结论

| 项 | 结果 | 要点 |
|---|---|---|
| P0 `push` 触发的首次运行 | 通过 | `platform` 解析出 environment 与同步 App 变量，`sync-waiting` 取到同步令牌、完整列出打开 PR 并输出「没有等待中的自动合并 PR」；`judge`、`merge-app` 等 job 在 push 下被跳过 |
| P1 等待路径 | 通过 | #166：必需检查还在等时由同步 App 开启（`BLOCKED`），检查全过变 `CLEAN` 后约 35 秒由同步 App 合并；合并触发了 main 的 push（`auto-merge`、`harness`、`ci`） |
| P2 新推送后重判、重新批准与开启 | 通过 | #168 被 `sync-waiting` 更新后 head 变化（553c2d3 → 22dadc8），新 head 上出现第二条批准（reviews 里两条 APPROVED），新一轮 CI 与判定后重新开启；重复 `--auto` 返回成功（见 P2b）。批准是否被 GitHub 显式作废未单独核对（ruleset 要求最后一次推送由推送者以外的人批准，新 head 在重新批准前为 `BLOCKED`） |
| P2b 等待中同步链 | 通过 | #169 合并后触发的 push 运行里 `sync-waiting` 输出「PR #168 落后 2 个提交，已用同步 App 更新分支」；`update-branch` 之后 #168 的 `autoMergeRequest` **保留**（开启者不变）；新 head 重新判定时再次 `--auto` 返回 0 且请求不变；最终由同步 App 合并 |
| P3 否决后关闭 | 通过 | 真实 `GitHub.disable_auto_merge(172)` 返回 True，`autoMergeRequest` 清空；请求本就不存在时再调用也返回 True（`gh pr merge --disable-auto` 此时退出码为 0，与方法注释「没开返回假」不符，记入 B110） |
| P4 `automerge-off` | 通过 | 列出在途运行（REST 字段 `id`、`head_branch` 正确显示）与存量请求；关闭后复查为零，退出码 0；再跑一次为「无运行、无请求」。**没有在真实环境碰到「有未完成运行时非零退出」**（那次运行恰好在复查前结束），这一条由单元测试覆盖 |

**异常（B109）**：#168 的合并提交 e150220 **没有触发任何 push 工作流**（`auto-merge`、`harness`、`ci` 都没有），而 #166、#167、#169 的合并都触发了；四次里一次。提交作者、提交者、签名与其他合并一致，原因未明。后果：当时已开启自动合并但落后的 #170 无人同步，直到手动 `gh pr update-branch`。

## P1（#166、#167）：观察脚本原始记录

```
02:59:56 #166 OPEN head=e178dd7 mss=BLOCKED auto=off mergedBy=-
02:59:57 #167 OPEN head=a2b9bf7 mss=BLOCKED auto=off mergedBy=-
03:00:50 #166 OPEN head=e178dd7 mss=BLOCKED auto=on by syncApp MERGE mergedBy=-
03:03:30 #166 OPEN head=e178dd7 mss=CLEAN auto=on by syncApp MERGE mergedBy=-
03:04:05 #166 MERGED head=e178dd7 mss=UNKNOWN auto=on by syncApp MERGE mergedBy=syncApp
03:04:05 #167 OPEN head=a2b9bf7 mss=UNKNOWN auto=off mergedBy=-
03:04:22 #167 OPEN head=a2b9bf7 mss=BEHIND auto=off mergedBy=-
03:07:51 #167 OPEN head=f863945 mss=BLOCKED auto=off mergedBy=-
03:13:41 #167 MERGED head=f863945 mss=UNKNOWN auto=off mergedBy=syncApp
watch end 03:13:41
```

#166 在 03:00:50 观察到 `auto=on by syncApp`（开启时必需检查还在等，`mss=BLOCKED`），03:03:30 `CLEAN`，03:04:05 `MERGED`。#167 在 #166 合并时 `auto=off` 且已落后，由判定前的「批准前同步」处理，之后 03:13:30 开启（检查已全过）、11 秒后合并。

## P2b（#168、#169、#170）：观察脚本原始记录

```
03:30:08 #168 OPEN head=553c2d3 mss=BLOCKED auto=off mergedBy=-
03:30:09 #169 OPEN head=f79ae49 mss=BLOCKED auto=off mergedBy=-
03:30:10 #170 OPEN head=92040d3 mss=BLOCKED auto=off mergedBy=-
03:30:27 #169 OPEN head=f79ae49 mss=BLOCKED auto=on by syncApp MERGE mergedBy=-
03:31:59 #168 OPEN head=553c2d3 mss=BLOCKED auto=on by syncApp MERGE mergedBy=-
03:33:13 #168 OPEN head=553c2d3 mss=CLEAN auto=on by syncApp MERGE mergedBy=-
03:33:32 #169 OPEN head=f79ae49 mss=CLEAN auto=on by syncApp MERGE mergedBy=-
03:34:29 #170 OPEN head=92040d3 mss=BEHIND auto=off mergedBy=-
03:34:45 #168 OPEN head=553c2d3 mss=UNKNOWN auto=on by syncApp MERGE mergedBy=-
03:34:46 #169 MERGED head=f79ae49 mss=UNKNOWN auto=on by syncApp MERGE mergedBy=syncApp
03:34:47 #170 OPEN head=92040d3 mss=UNKNOWN auto=off mergedBy=-
03:35:03 #168 OPEN head=22dadc8 mss=BLOCKED auto=on by syncApp MERGE mergedBy=-
03:35:43 #170 OPEN head=4950ba8 mss=BLOCKED auto=off mergedBy=-
03:40:35 #168 OPEN head=22dadc8 mss=BLOCKED auto=on by syncApp MERGE mergedBy=-
03:42:10 #170 OPEN head=4950ba8 mss=BLOCKED auto=on by syncApp MERGE mergedBy=-
03:44:16 #168 OPEN head=22dadc8 mss=CLEAN auto=on by syncApp MERGE mergedBy=-
03:45:10 #168 MERGED head=22dadc8 mss=UNKNOWN auto=on by syncApp MERGE mergedBy=syncApp
03:45:13 #170 OPEN head=4950ba8 mss=BEHIND auto=on by syncApp MERGE mergedBy=-
03:55:30 #170 OPEN head=741a86d mss=BLOCKED auto=on by syncApp MERGE mergedBy=-
03:58:11 #170 OPEN head=741a86d mss=UNKNOWN auto=on by syncApp MERGE mergedBy=-
04:01:12 #170 OPEN head=741a86d mss=BLOCKED auto=on by syncApp MERGE mergedBy=-
04:04:48 #170 OPEN head=741a86d mss=CLEAN auto=on by syncApp MERGE mergedBy=-
04:05:59 #170 MERGED head=741a86d mss=UNKNOWN auto=on by syncApp MERGE mergedBy=syncApp
watch end 04:05:59
```

关键运行（`gh run list --workflow auto-merge.yml`，UTC）：

```
37520796252 19:40:54 workflow_run completed/success
37520674975 19:39:57 workflow_run completed/success
37520379723 19:37:35 workflow_run completed/success
37520269793 19:36:43 workflow_run completed/success
37520046310 19:34:58 workflow_run completed/success
37519991261 19:34:31 push completed/success
37519575301 19:31:10 workflow_run completed/success
37519389805 19:29:45 workflow_run completed/success
37518824752 19:25:13 workflow_run completed/success
37517722493 19:16:29 workflow_run completed/success
37517340941 19:13:32 push completed/success
37517247547 19:12:48 workflow_run completed/success
37516567212 19:07:23 workflow_run completed/success
37516538713 19:07:09 workflow_run completed/success
```

- `37519991261`（push，19:34:31，#169 合并触发）：`sync-waiting` 输出「PR #168 落后 2 个提交，已用同步 App 更新分支，等新一轮 CI 后重判」。
- `37520674975`（workflow_run，19:40:28）：`merge-app` 的 Enable 步骤输出 `auto-merge enabled:`（#168 此时请求已存在，重复 `--auto` 成功）。
- `37520796252`（workflow_run，19:42:03）：同一步骤输出 `auto-merge enabled:`（#170 首次开启）。
- #168 合并后（19:44:55 UTC，e150220）没有 push 运行；03:45:13 观察到 #170 `auto=on` 且 `BEHIND`，03:58 手动 `update-branch` 后（head 741a86d）重新判定，04:05:59 由同步 App 合并。

## P3 原始输出（`build/dispatch/p3.log`）

```
## 初始
autoMergeRequest=null state=OPEN mss=BLOCKED
## 开启：gh pr merge 172 --auto --merge
exit 0
autoMergeRequest=on by Snowson MERGE state=OPEN mss=BLOCKED
## 重复开启（等待状态下再次 --auto）
exit 0
autoMergeRequest=on by Snowson MERGE state=OPEN mss=BLOCKED
## P3 真实 GitHub.disable_auto_merge(172)（引擎代码，agent 身份）
返回值 True
autoMergeRequest=null state=OPEN mss=BLOCKED
## 请求本就不存在时再关闭一次
返回值 True
autoMergeRequest=null state=OPEN mss=BLOCKED
```

## P4 原始输出（`build/dispatch/p4.log`）

```
## 重新开启
autoMergeRequest=on by Snowson
## 在途的 auto-merge.yml 运行
37524495833 workflow_run queued/
37524164837 workflow_run completed/success
37523854402 push completed/success
37523458874 workflow_run completed/success
## automerge-off --dry-run
未完成的 auto-merge.yml 运行：37524495833（in_progress，main）
已开启自动合并的打开 PR：#172
exit 0
autoMergeRequest=on by Snowson
## automerge-off（真实，仓库当前引擎）
未完成的 auto-merge.yml 运行：37524495833（in_progress，main）
已开启自动合并的打开 PR：#172
PR #172 的自动合并已关闭
复查通过：未完成运行为零，自动合并请求为零
exit 0
autoMergeRequest=null
## 再运行一次（应为无请求、无在途运行、0 退出）
未完成的 auto-merge.yml 运行：无
已开启自动合并的打开 PR：无
复查通过：未完成运行为零，自动合并请求为零
exit 0
```

## 其他实测

- 平台上 `gh pr merge --auto` 在已开启时重复调用：退出码 0、请求不变（P3、P2b 均观察到）。
- `gh pr list` 没有 `--paginate`：评审第 1 轮发现，`automerge-off` 与 `sync-waiting` 已改用 REST 分页；本次 P4 真实运行验证了 REST 查询可用。
- 测试 PR 留下的五个文件 `docs/review/t715-r0*-*.md` 已在本 PR 删除；测试分支（`test/t715-*`）保留，可在仓库设置里打开「合并后自动删除分支」或由用户清理（守卫禁止经 API 删分支）。
