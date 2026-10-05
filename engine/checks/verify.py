"""统一验证入口：本机、云端、CI 用同一条命令、同一组检查。

    bin/verify              默认档：工具版本、仓库卫生、质量棘轮、文档链接、验收映射、任务书准入与项目检查（lint、测试等）
    bin/verify --quick      快速档：工具版本、仓库卫生与标为 quick 的项目检查（pre-commit 用）
    bin/verify --full       完整档：默认档 + 事故回放（注入历史缺陷，对应测试必须失败）
    bin/verify --strict     任何被跳过的检查都算失败（防止平台相关检查被静默跳过）

终端只打印每项结论；完整输出写到 build/verify/<检查名>.log，汇总写到 build/verify/summary.json。
通过与否以本命令的退出码和 CI 上当前 head 的运行为准，不以任何人的自述为准。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

from engine.core import events, events_db
from engine.core.common import CLI, ENGINE_REL, ROOT, clean_git_env, git, setting

LOG_DIR = ROOT / "build" / "verify"
MIN_PYTHON = (3, 11)
TAIL_LINES = 30


@dataclass
class Check:
    name: str
    tiers: tuple[str, ...]
    command: list[str] | None = None
    shell: str | None = None
    requires: str | None = None  # "macos"
    why_skipped: str = ""
    func: object = None  # 进程内检查：返回 (ok, 输出)
    project: bool = False  # 项目检查（checks.toml [[verify.checks]]）：子进程事件导临时库（B92）


@dataclass
class Result:
    name: str
    status: str  # pass / fail / skip
    seconds: float = 0.0
    log: str = ""
    note: str = ""
    tail: list[str] = field(default_factory=list)
    code: int | None = None  # 检查命令的退出码；未运行（--skip、strict 转换）为 None


def pinned_version(package: str) -> str | None:
    requirements = ROOT / setting("verify", "requirements", "requirements-dev.txt")
    if not requirements.exists():
        return None
    for line in requirements.read_text().splitlines():
        match = re.match(rf"\s*{re.escape(package)}==([\w.]+)", line)
        if match:
            return match.group(1)
    return None


def check_tools() -> tuple[bool, str]:
    lines = []
    ok = True
    if sys.version_info < MIN_PYTHON:
        ok = False
        lines.append(f"Python {platform.python_version()} < {'.'.join(map(str, MIN_PYTHON))}")
    else:
        lines.append(f"Python {platform.python_version()} ✓")
    # 锁定版本的 Python 工具（checks.toml [verify] pinned_tools），与 CI 同一版本。
    requirements = setting("verify", "requirements", "requirements-dev.txt")
    for tool in setting("verify", "pinned_tools", []):
        want = pinned_version(tool)
        result = subprocess.run([sys.executable, "-m", tool, "--version"], capture_output=True, text=True, check=False)
        have = result.stdout.strip().removeprefix(f"{tool} ") if result.returncode == 0 else None
        if want is None:
            ok = False
            lines.append(f"{requirements} 未锁定 {tool} 版本")
        elif have != want:
            ok = False
            lines.append(f"{tool} {have or '未安装'} ≠ 锁定的 {want}；执行 `{sys.executable} -m pip install -r {requirements}`")
        else:
            lines.append(f"{tool} {have} ✓")
    # 每个开发与 Agent 执行环境都必须装上 git 守卫；CI 是全新检出，不需要。
    if os.environ.get("CI") != "true":
        hooks = git("config", "--local", "--get", "core.hooksPath", check=False)
        if hooks != ".githooks":
            ok = False
            lines.append(
                f"git 守卫未安装（core.hooksPath = {hooks or '未设置'}）；执行 `python3 {ENGINE_REL}/cli.py guard-git install`"
            )
        else:
            lines.append("git 守卫已安装 ✓")
    return ok, "\n".join(lines)


def _is_macos() -> bool:
    return sys.platform == "darwin" and shutil.which("xcrun") is not None


ALL_TIERS = ("quick", "default", "full")


def builtin_checks(strict: bool = False) -> list[Check]:
    """引擎自带的检查。项目检查（lint、测试、平台相关的编译运行）写在 checks.toml [[verify.checks]]。"""
    py = sys.executable
    cli = [py, str(CLI)]
    return [
        Check("tools", ALL_TIERS, func=check_tools),
        # 引擎完整性：.harness/engine/ 与锁文件一致（引擎只能经 upgrade 整体替换）。
        Check("integrity", ALL_TIERS, command=[*cli, "integrity"]),
        Check("hygiene", ALL_TIERS, command=[*cli, "hygiene", "--tracked"]),
        # 熵治理棘轮（quality）：复杂度超标函数数与超长文件数只降不升。
        Check("quality", ("default", "full"), command=[*cli, "quality"]),
        # 文档熵治理（docs）：已跟踪 Markdown 的相对链接断链即失败；陈旧状态只报告。
        Check("docs", ("default", "full"), command=[*cli, "docs"]),
        # 规格验收编号 ↔ 测试映射（acceptance）：引用失效或新增无测试条目即失败。
        Check("acceptance", ALL_TIERS, command=[*cli, "acceptance"]),
        # 任务书准入（taskbook）：头部、类别与风险、验收挂规格编号、步骤交叉核对。
        Check("taskbook", ALL_TIERS, command=[*cli, "taskbook"]),
        # 事故回放：注入历史缺陷，对应测试必须失败（.harness/project/replay_cases.py）。
        Check("replay", ("full",), command=[*cli, "replay", *(["--strict"] if strict else [])]),
    ]


def project_checks() -> list[Check]:
    """checks.toml 的 [[verify.checks]]：name、tiers、command（列表，`{python}` 换成当前解释器）或 shell、
    requires = "macos"、why_skipped。"""
    checks = []
    for item in setting("verify", "checks", []):
        command = [sys.executable if part == "{python}" else part for part in item["command"]] if "command" in item else None
        checks.append(
            Check(
                item["name"],
                tuple(item.get("tiers", ("default", "full"))),
                command=command,
                shell=item.get("shell"),
                requires=item.get("requires"),
                why_skipped=item.get("why_skipped", ""),
                project=True,
            )
        )
    return checks


def build_checks(strict: bool = False) -> list[Check]:
    """顺序取 checks.toml [verify] order（引擎检查与项目检查的名字混排）；没写的检查依次排在后面，回放最后。"""
    available = {check.name: check for check in [*builtin_checks(strict), *project_checks()]}
    order = [name for name in setting("verify", "order", []) if name in available]
    rest = [name for name in available if name not in order and name != "replay"]
    names = order + rest + ([] if "replay" in order else ["replay"])
    return [available[name] for name in names]


# 最终 Result（含 --skip 与 --strict 转换）到事件状态的映射（设计 3.3）。
STATUS_BY_RESULT = {"pass": "ok", "fail": "fail", "skip": "skip"}


def _log_refs(log: str) -> dict:
    """完整日志按原始字节算哈希并存为本机产物；读取或保存失败时省略对应字段，不影响判定。"""
    if not log:
        return {}
    try:
        data = (ROOT / log).read_bytes()
    except OSError:
        return {}
    refs = {"log.sha256": hashlib.sha256(data).hexdigest(), "log.size": len(data)}
    try:
        stored = events.store_artifact(data)
    except Exception:  # noqa: BLE001  观察准备失败不影响原判定
        stored = None
    if stored:
        refs["log.ref"] = stored["ref"]
    return refs


def emit_result(result: Result, info: dict, tier: str) -> None:
    """每项检查的最终 Result 之后写 verify.<name>（D029）：head、档位、退出码、时长、失败签名与日志哈希。"""
    try:
        events.emit(stage="verify", step=f"verify.{result.name}", status=STATUS_BY_RESULT[result.status],
                    duration_ms=int(result.seconds * 1000),
                    inputs=[events.ref("head", info["head"]), events.ref("tier", tier)],
                    outputs={"exit": result.code, "signature": result.note or None, **_log_refs(result.log)})
    except Exception:  # noqa: BLE001  观察失败不影响原判定
        return


def emit_summary(info: dict, tier: str, ok: bool) -> None:
    """收尾写一条 verify.summary（D030）：档位、通过与工作区状态。"""
    try:
        events.emit(stage="verify", step="verify.summary", status="ok" if ok else "fail",
                    inputs=[events.ref("head", info["head"]), events.ref("tier", tier)],
                    outputs={"tier": tier, "ok": ok, "dirty": info["dirty"]})
    except Exception:  # noqa: BLE001  观察失败不影响原判定
        return


class _ProjectEventsSink:
    """项目检查子进程的事件临时库（B92）：懒建临时目录，verify 结束后整体删除。

    重定向变量只放进项目检查子进程的环境，由 events_db._harness_dir 按公共目录匹配生效；
    引擎检查与 verify 自身照常写真实事件库，夹具仓库等公共目录不同的子进程自行忽略。
    """

    def __init__(self) -> None:
        self._dir: str | None = None

    def env(self) -> dict[str, str]:
        common = events_db.common_dir()
        if common is None:
            return {}
        if self._dir is None:
            self._dir = tempfile.mkdtemp(prefix="dh-verify-events-")
        return {"HARNESS_EVENTS_REDIRECT": json.dumps({"dir": self._dir, "for_common_dir": str(common)})}

    def close(self) -> None:
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None


def run_check(check: Check, sink: _ProjectEventsSink | None = None) -> Result:
    if check.requires == "macos" and not _is_macos():
        return Result(check.name, "skip", note=check.why_skipped)
    target = LOG_DIR / f"{check.name}.log"
    started = time.monotonic()
    if check.func is not None:
        ok, output = check.func()
        target.write_text(output + "\n")
        code = 0 if ok else 1
    else:
        env = clean_git_env({"PYTHONDONTWRITEBYTECODE": "1"})
        if check.project and sink is not None:
            env.update(sink.env())  # 项目检查子进程的事件导进临时库（B92）
        with open(target, "w") as log:
            completed = subprocess.run(
                check.command if check.command else check.shell,
                shell=check.command is None,
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                # 钩子会注入 GIT_DIR 等变量；linked worktree 里它是指向真实仓库的绝对路径，
                # 检查里的临时 git 仓库会被它带偏（H0926-3），所以子进程不继承这些变量。
                env=env,
                check=False,
            )
        code = completed.returncode
    seconds = time.monotonic() - started
    relative = str(target.relative_to(ROOT))
    if code == 0:
        return Result(check.name, "pass", seconds, relative, code=code)
    tail = target.read_text(errors="replace").splitlines()[-TAIL_LINES:]
    return Result(check.name, "fail", seconds, relative, f"退出码 {code}", tail, code=code)


def head_info() -> dict:
    try:
        sha = git("rev-parse", "HEAD")
        dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    except RuntimeError:
        sha, dirty = "unknown", True
    return {"head": sha, "dirty": dirty}


def write_step_summary(results: list[Result], info: dict) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    icon = {"pass": "✅", "fail": "❌", "skip": "⏭️"}
    lines = [f"### verify @ `{info['head'][:12]}`", "", "| 检查 | 结果 | 用时 | 说明 |", "|---|---|---|---|"]
    for result in results:
        lines.append(f"| {result.name} | {icon[result.status]} | {result.seconds:.1f}s | {result.note} |")
    with open(target, "a") as handle:
        handle.write("\n".join(lines) + "\n\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    tier = parser.add_mutually_exclusive_group()
    tier.add_argument("--quick", action="store_const", dest="tier", const="quick")
    tier.add_argument("--full", action="store_const", dest="tier", const="full")
    parser.add_argument("--only", help="只跑这些检查（逗号分隔）")
    parser.add_argument("--skip", default="", help="跳过这些检查（逗号分隔，会记入汇总）")
    parser.add_argument("--strict", action="store_true", help="被跳过的检查算失败")
    parser.add_argument("--json", action="store_true", help="结束时打印汇总 JSON")
    args = parser.parse_args(argv)
    tier_name = args.tier or "default"

    checks = [check for check in build_checks(args.strict) if tier_name in check.tiers]
    if args.only:
        wanted = set(args.only.split(","))
        unknown = wanted - {check.name for check in build_checks(args.strict)}
        if unknown:
            parser.error(f"未知检查：{', '.join(sorted(unknown))}")
        checks = [check for check in build_checks(args.strict) if check.name in wanted]
    skipped_by_user = {name for name in args.skip.split(",") if name}

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    info = head_info()
    results = []
    sink = _ProjectEventsSink()  # 项目检查子进程的事件临时库，运行结束后删净（B92）
    try:
        for check in checks:
            if check.name in skipped_by_user:
                result = Result(check.name, "skip", note="--skip 手动跳过")
            else:
                result = run_check(check, sink)
            if result.status == "skip" and args.strict:
                result = Result(check.name, "fail", note=f"--strict 下不允许跳过（{result.note}）")
            results.append(result)
            emit_result(result, info, tier_name)  # 最终 Result（含 strict 转换）之后埋点，--skip 与 strict 都不漏
            mark = {"pass": "✓", "fail": "✗", "skip": "-"}[result.status]
            extra = f"  {result.note}" if result.note else ""
            print(f"{mark} {result.name:<15} {result.seconds:5.1f}s{extra}", flush=True)
            for line in result.tail:
                print(f"    │ {line}")
            if result.tail:
                print(f"    └ 完整输出：{result.log}")
    finally:
        sink.close()

    failed = [result.name for result in results if result.status == "fail"]
    summary = {
        **info,
        "tier": tier_name,
        "strict": args.strict,
        "platform": sys.platform,
        "python": platform.python_version(),
        "results": [
            {"name": r.name, "status": r.status, "seconds": round(r.seconds, 2), "note": r.note, "log": r.log}
            for r in results
        ],
        "ok": not failed,
    }
    (LOG_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    emit_summary(info, tier_name, not failed)
    write_step_summary(results, info)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    dirty = "（工作区有未提交改动）" if info["dirty"] else ""
    if failed:
        print(f"verify 失败：{', '.join(failed)} @ {info['head'][:12]}{dirty}")
        return 1
    print(f"verify 通过 @ {info['head'][:12]}{dirty}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
