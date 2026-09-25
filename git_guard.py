"""git 层守卫：与使用哪家 Agent 无关，所有经 git 的操作都会经过这里。

    python3 harness/git_guard.py install               把 core.hooksPath 指向 .githooks（幂等）
    python3 harness/git_guard.py status                查看安装状态
    .githooks/pre-commit            → pre-commit       保护分支上禁止提交；暂存区卫生；快速 verify
    .githooks/pre-push              → pre-push         禁推保护分支与 tag、禁强制推送；推送前 verify
    .githooks/reference-transaction → reference-transaction
                                                       禁止本地改写/删除保护分支、移动/删除 tag

对应 v0.8.0 X3：filter-repo 改写了本地 main 与 18 个 tag，并删除了 origin。
覆盖变量见 harness/rules.toml [guard]；只供人使用。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hygiene
from common import ROOT, ZERO_SHA, git, load_rules

HOOKS_DIR = ".githooks"


def _allowed(name: str) -> bool:
    return os.environ.get(name) == "1"


def _protected(ref: str, rules: dict) -> bool:
    return ref in {f"refs/heads/{name}" for name in rules["guard"]["protected_branches"]}


def _is_ancestor(old: str, new: str, cwd: Path) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", old, new], cwd=cwd, capture_output=True, check=False
    )
    return result.returncode == 0


def _exists(sha: str, cwd: Path) -> bool:
    return subprocess.run(["git", "cat-file", "-e", sha], cwd=cwd, capture_output=True, check=False).returncode == 0


def ref_transaction_violations(lines: list[str], cwd: Path, rules: dict) -> list[str]:
    """reference-transaction 的 prepared 阶段：返回应拒绝的更新。"""
    if _allowed("HARNESS_ALLOW_REWRITE"):
        return []
    problems = []
    for line in lines:
        parts = line.split()
        if len(parts) != 3:
            continue
        old, new, ref = parts
        if old == ZERO_SHA and (ref.startswith("refs/tags/") or _protected(ref, rules)):
            # 删除 tag、不带旧值的 update-ref 等操作传来的旧值是全 0；prepared 阶段引用尚未改动，查当前值。
            old = git("rev-parse", "--verify", "--quiet", ref, cwd=cwd, check=False) or ZERO_SHA
        if ref.startswith("refs/tags/") and old != ZERO_SHA and old != new:
            action = "删除" if new == ZERO_SHA else "移动"
            problems.append(f"{action}已有 tag {ref}（已发布的 tag 不能改；推 tag 会触发发布）")
        elif _protected(ref, rules) and old != ZERO_SHA and old != new:
            if new == ZERO_SHA:
                problems.append(f"删除保护分支 {ref}")
            elif not _is_ancestor(old, new, cwd):
                problems.append(f"改写保护分支 {ref}（{old[:7]} → {new[:7]} 不是快进）")
    return problems


def pre_push_violations(lines: list[str], cwd: Path, rules: dict) -> list[str]:
    problems = []
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        _local_ref, local_sha, remote_ref, remote_sha = parts
        if remote_ref.startswith("refs/tags/"):
            if not _allowed("HARNESS_ALLOW_TAG"):
                problems.append(f"推送 tag {remote_ref}：推 tag 会触发发布，只能由用户执行")
            continue
        if _protected(remote_ref, rules) and not _allowed("HARNESS_ALLOW_MAIN"):
            problems.append(f"直接推送保护分支 {remote_ref}：请推到功能分支并开 PR")
            continue
        if local_sha == ZERO_SHA or remote_sha == ZERO_SHA or _allowed("HARNESS_ALLOW_REWRITE"):
            continue  # 删除分支、新建分支
        if not _exists(remote_sha, cwd):
            problems.append(f"{remote_ref} 远端有本地没有的提交 {remote_sha[:7]}：先 fetch，确认不会覆盖别人的提交")
        elif not _is_ancestor(remote_sha, local_sha, cwd):
            problems.append(f"强制推送 {remote_ref}（{remote_sha[:7]} → {local_sha[:7]} 不是快进）：已推送的历史不改写")
    return problems


def current_branch(cwd: Path) -> str:
    return git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=cwd, check=False)


def _report(title: str, problems: list[str]) -> int:
    if not problems:
        return 0
    print(f"harness：{title}被拒绝：", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    print("  规则与覆盖方式见 harness/rules.toml [guard]（覆盖只供人使用）。", file=sys.stderr)
    return 1


def _run_verify(repo: Path, *args: str) -> int:
    """跑钩子所在仓库自己的 verify；仓库里没有 harness（如测试用的临时仓库）则跳过。"""
    if _allowed("HARNESS_SKIP_VERIFY"):
        print("harness：HARNESS_SKIP_VERIFY=1，跳过 verify。", file=sys.stderr)
        return 0
    entry = repo / "harness" / "verify.py"
    if not entry.is_file():
        return 0
    return subprocess.run([sys.executable, str(entry), *args], cwd=repo, check=False).returncode


def cmd_pre_commit(repo: Path) -> int:
    rules = load_rules()
    problems = []
    branch = current_branch(repo)
    if branch in rules["guard"]["protected_branches"] and not _allowed("HARNESS_ALLOW_MAIN"):
        problems.append(f"在保护分支 {branch} 上提交：请在功能分支上工作")
    problems += [violation.render() for violation in hygiene.scan_staged(repo, rules)]
    if _report("提交", problems):
        return 1
    return _run_verify(repo, "--quick")


def push_hygiene_violations(lines: list[str], repo: Path, rules: dict) -> list[str]:
    """本次推送带出的改动做卫生检查（与 CI 的 --range 同口径，提前在本机发现）。"""
    problems = []
    for line in lines:
        parts = line.split()
        if len(parts) != 4 or parts[1] == ZERO_SHA or parts[2].startswith("refs/tags/"):
            continue
        local_sha, remote_sha = parts[1], parts[3]
        if remote_sha != ZERO_SHA and _exists(remote_sha, repo):
            base = remote_sha
        else:
            base = git("merge-base", local_sha, "origin/main", cwd=repo, check=False)
        if not base:
            continue
        problems += [violation.render() for violation in hygiene.scan_range(base, local_sha, repo, rules)]
    return problems


def cmd_pre_push(repo: Path, stdin: str) -> int:
    rules = load_rules()
    lines = stdin.splitlines()
    if _report("推送", pre_push_violations(lines, repo, rules) or push_hygiene_violations(lines, repo, rules)):
        return 1
    return _run_verify(repo)


def cmd_reference_transaction(repo: Path, state: str, stdin: str) -> int:
    if state != "prepared":
        return 0
    return _report("引用更新", ref_transaction_violations(stdin.splitlines(), repo, load_rules()))


def hooks_path(cwd: Path = ROOT) -> str:
    return git("config", "--local", "--get", "core.hooksPath", cwd=cwd, check=False)


def cmd_install(cwd: Path = ROOT) -> int:
    current = hooks_path(cwd)
    if current == HOOKS_DIR:
        print("harness git 钩子已安装（core.hooksPath = .githooks）。")
        return 0
    if current:
        print(
            f"core.hooksPath 已被设为 {current!r}，不覆盖既有配置。"
            f"如确认要改用本仓库钩子：git config core.hooksPath {HOOKS_DIR}",
            file=sys.stderr,
        )
        return 1
    git_dir = Path(git("rev-parse", "--git-common-dir", cwd=cwd))
    legacy = [
        path.name
        for path in (cwd / git_dir / "hooks").glob("*")
        if path.is_file() and not path.name.endswith(".sample") and os.access(path, os.X_OK)
    ]
    git("config", "--local", "core.hooksPath", HOOKS_DIR, cwd=cwd)
    print("已安装：core.hooksPath = .githooks（pre-commit、pre-push、reference-transaction）。")
    if legacy:
        print(f"注意：.git/hooks 下的 {', '.join(legacy)} 将不再执行。", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else "status"
    if command == "install":
        return cmd_install()
    if command == "status":
        installed = hooks_path() == HOOKS_DIR
        print("已安装" if installed else "未安装：python3 harness/git_guard.py install")
        return 0 if installed else 1
    repo = Path.cwd()  # git 在仓库（工作树）根目录执行钩子
    if command == "pre-commit":
        return cmd_pre_commit(repo)
    if command == "pre-push":
        return cmd_pre_push(repo, sys.stdin.read())
    if command == "reference-transaction":
        return cmd_reference_transaction(repo, args[1] if len(args) > 1 else "", sys.stdin.read())
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
