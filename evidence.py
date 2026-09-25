"""修复证据：由脚本从提交与测试运行结果生成，取代手写的「已修复」。

约定：修复某个缺陷的提交在说明末尾带 trailer

    Defect: V080-R17          # 缺陷编号，全局唯一：<来源>-<序号>
    Defect: V080-R3 doc       # 纯规格或文档类修复，不要求代码与测试

对每个编号检查：
  1. 有带该编号的提交；非 doc 类修复必须改了代码（只改文档或测试 = 声称已修但代码未变，v0.8.0 X1）
  2. tests/ 下有引用该编号的测试（Python 按方法定位，Swift 列出位置）
  3. 在临时工作树里：head 上这些 Python 测试通过；把该编号提交改过的代码文件退回修复前，测试必须失败
     （修复前失败才能证明测试真的在检查这个缺陷，v0.8.0 X4）

    python3 harness/evidence.py --base origin/main [--head HEAD] [--ids A,B] [--markdown out.md]
"""

from __future__ import annotations

import argparse
import ast
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import ROOT, clean_git_env, commit_field, git, path_matches

ID_PATTERN = r"[A-Z][A-Z0-9]*-[A-Z]*\d+"
TRAILER_RE = re.compile(rf"^({ID_PATTERN})(?:\s+(doc))?\s*$")
DOC_PATTERNS = ["docs/**", "**/*.md"]
TEST_PATTERNS = ["tests/**"]


@dataclass
class DefectEvidence:
    defect: str
    doc_only: bool = False
    commits: list[tuple[str, str]] = field(default_factory=list)  # (sha, 标题)
    code_files: dict[str, tuple[int, int]] = field(default_factory=dict)  # 路径 -> (+, -)
    test_files: list[str] = field(default_factory=list)
    python_tests: list[str] = field(default_factory=list)
    swift_refs: list[str] = field(default_factory=list)
    before: str = ""  # 修复前测试结果
    after: str = ""  # 修复后测试结果
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def id_token(defect: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-]){re.escape(defect)}(?![\w-])")


def defect_commits(base: str, head: str, cwd: Path = ROOT) -> dict[str, DefectEvidence]:
    """从 base..head 的提交 trailer 收集缺陷编号（按提交先后）。"""
    found: dict[str, DefectEvidence] = {}
    shas = git("rev-list", "--reverse", f"{base}..{head}", cwd=cwd).split()
    for sha in shas:
        subject = git("log", "-1", "--format=%s", sha, cwd=cwd)
        for value in commit_field(sha, "Defect", cwd):
            match = TRAILER_RE.match(value)
            if not match:
                found.setdefault(value, DefectEvidence(value)).problems.append(
                    f"`{sha[:7]}` 的 Defect trailer 格式不对：`{value}`（应为 `<来源>-<序号>`，可加 ` doc`）"
                )
                continue
            evidence = found.setdefault(match.group(1), DefectEvidence(match.group(1)))
            evidence.doc_only = evidence.doc_only or bool(match.group(2))
            evidence.commits.append((sha, subject))
    return found


def collect_files(evidence: DefectEvidence, cwd: Path = ROOT) -> None:
    for sha, _ in evidence.commits:
        numstat = git("show", "--numstat", "--format=", "--no-renames", sha, cwd=cwd)
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, removed, path = parts
            if path_matches(path, TEST_PATTERNS):
                if path not in evidence.test_files:
                    evidence.test_files.append(path)
            elif not path_matches(path, DOC_PATTERNS):
                old = evidence.code_files.get(path, (0, 0))
                plus = int(added) if added.isdigit() else 0
                minus = int(removed) if removed.isdigit() else 0
                evidence.code_files[path] = (old[0] + plus, old[1] + minus)


def _python_test_ids(path: Path, pattern: re.Pattern[str]) -> list[str]:
    """引用编号的 Python 测试：按所在行归到测试方法；落在类里方法外则取该类全部测试。"""
    source = path.read_text(encoding="utf-8")
    hit_lines = [number for number, line in enumerate(source.splitlines(), 1) if pattern.search(line)]
    if not hit_lines:
        return []
    tree = ast.parse(source)
    module = path.stem
    selected: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        methods = [item for item in node.body if isinstance(item, ast.FunctionDef) and item.name.startswith("test")]
        class_hit = any(node.lineno <= line <= node.end_lineno for line in hit_lines)
        if not class_hit:
            continue
        in_method = [
            method
            for method in methods
            if any(method.lineno - len(method.decorator_list) <= line <= method.end_lineno for line in hit_lines)
        ]
        for method in in_method or methods:
            selected.append(f"{module}.{node.name}.{method.name}")
    if not selected and hit_lines:  # 只在模块级（文件说明）引用：取整个模块
        selected.append(module)
    return selected


def find_tests(evidence: DefectEvidence, tests_dir: Path) -> None:
    pattern = id_token(evidence.defect)
    for path in sorted(tests_dir.glob("test_*.py")):
        evidence.python_tests.extend(_python_test_ids(path, pattern))
    for path in sorted(tests_dir.glob("*.swift")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                evidence.swift_refs.append(f"{path.relative_to(tests_dir.parent)}:{number}")


def _run_python_tests(worktree: Path, tests: list[str]) -> tuple[str, str]:
    """返回 (结果, 输出摘要)，结果为 pass / fail / error。"""
    completed = subprocess.run(
        [sys.executable, "-W", "error::ResourceWarning", "-m", "unittest", *tests],
        cwd=worktree / "tests",
        capture_output=True,
        text=True,
        env=clean_git_env({"PYTHONDONTWRITEBYTECODE": "1"}),
        check=False,
    )
    output = completed.stderr.strip().splitlines()
    summary = output[-1] if output else ""
    if completed.returncode == 0:
        return "pass", summary
    failures = re.search(r"failures=(\d+)", summary)
    return ("fail" if failures else "error"), summary


def _exists_at(rev: str, path: str, cwd: Path) -> bool:
    completed = subprocess.run(
        ["git", "cat-file", "-e", f"{rev}:{path}"], cwd=cwd, env=clean_git_env(), capture_output=True, check=False
    )
    return completed.returncode == 0


def verify_fail_before_fix(evidence: DefectEvidence, head: str, cwd: Path = ROOT) -> None:
    if not evidence.python_tests:
        return
    first_sha = evidence.commits[0][0]
    before_fix = git("rev-parse", f"{first_sha}^", cwd=cwd, check=False) or None
    temp = Path(tempfile.mkdtemp(prefix="evidence-"))
    worktree = temp / "wt"
    try:
        git("worktree", "add", "--detach", str(worktree), head, cwd=cwd, isolate=True)
        after, after_summary = _run_python_tests(worktree, evidence.python_tests)
        evidence.after = after
        if after != "pass":
            evidence.problems.append(f"head 上引用该编号的测试未通过（{after_summary}）")
        for path in evidence.code_files:
            target = worktree / path
            if before_fix and _exists_at(before_fix, path, cwd):
                git("checkout", before_fix, "--", path, cwd=worktree, isolate=True)
            elif target.exists():
                target.unlink()
        before, before_summary = _run_python_tests(worktree, evidence.python_tests)
        evidence.before = before
        if before == "pass":
            evidence.problems.append("把修复退回后测试仍然通过：测试没有检查到这个缺陷")
        elif before == "error":
            evidence.warnings.append(f"修复退回后测试以错误（而非断言失败）结束，需人工确认：{before_summary}")
    finally:
        git("worktree", "remove", "--force", str(worktree), cwd=cwd, check=False, isolate=True)
        shutil.rmtree(temp, ignore_errors=True)


def analyse(base: str, head: str, ids: list[str] | None = None, run_tests: bool = True, cwd: Path = ROOT):
    evidences = defect_commits(base, head, cwd)
    if ids:
        for defect in ids:
            evidences.setdefault(defect, DefectEvidence(defect))
        evidences = {key: value for key, value in evidences.items() if key in ids}
    tests_dir = cwd / "tests"
    for evidence in evidences.values():
        if evidence.problems and not evidence.commits:
            continue
        if not evidence.commits:
            evidence.problems.append(f"{base}..{head} 中没有带 `Defect: {evidence.defect}` 的提交")
            continue
        collect_files(evidence, cwd)
        if evidence.doc_only:
            continue
        if not evidence.code_files:
            evidence.problems.append("提交只改了文档或测试，没有代码改动（声称已修但代码未变）")
            continue
        find_tests(evidence, tests_dir)
        if not evidence.python_tests and not evidence.swift_refs:
            evidence.problems.append(f"tests/ 中没有引用 {evidence.defect} 的测试")
            continue
        if evidence.swift_refs and not evidence.python_tests:
            evidence.warnings.append("只有 Swift 测试引用该编号，修复前失败检查需在 macOS 上进行")
        if run_tests:
            verify_fail_before_fix(evidence, head, cwd)
    return evidences


def render_markdown(evidences: dict[str, DefectEvidence], base: str, head: str, cwd: Path = ROOT) -> str:
    base_sha = git("rev-parse", "--short", base, cwd=cwd)
    head_sha = git("rev-parse", "--short", head, cwd=cwd)
    label = {"pass": "✓ 通过", "fail": "✗ 失败", "error": "✗ 出错", "": "—"}
    lines = [
        f"### 修复证据（`{base_sha}` → `{head_sha}`，由 `harness/evidence.py` 生成）",
        "",
    ]
    if not evidences:
        lines.append("本范围内没有带 `Defect:` trailer 的提交。")
        return "\n".join(lines) + "\n"
    lines += ["| 编号 | 提交 | 代码改动 | 测试 | 修复前 | 修复后 | 结论 |", "|---|---|---|---|---|---|---|"]
    for evidence in evidences.values():
        commits = " ".join(f"`{sha[:7]}`" for sha, _ in evidence.commits) or "—"
        if evidence.doc_only:
            files = "文档类修复"
        else:
            files = "<br>".join(f"`{p}` +{a} −{r}" for p, (a, r) in evidence.code_files.items()) or "—"
        tests = "<br>".join(f"`{t}`" for t in evidence.python_tests + evidence.swift_refs) or "—"
        verdict = "✅" if evidence.ok else "❌"
        lines.append(
            f"| {evidence.defect} | {commits} | {files} | {tests} | {label[evidence.before]} "
            f"| {label[evidence.after]} | {verdict} |"
        )
    notes = [(e.defect, "❌", p) for e in evidences.values() for p in e.problems]
    notes += [(e.defect, "⚠️", w) for e in evidences.values() for w in e.warnings]
    if notes:
        lines += [""] + [f"- {icon} {defect}：{text}" for defect, icon, text in notes]
    lines += ["", "「修复前」应为失败：把该编号提交改过的代码退回修复前，引用该编号的测试必须失败。"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--ids", help="只检查这些编号（逗号分隔）；列出的编号没有提交也算失败")
    parser.add_argument("--no-run", action="store_true", help="不运行测试，只检查提交与测试引用")
    parser.add_argument("--markdown", help="把证据表写入该文件（CI 写入 job summary）")
    args = parser.parse_args(argv)

    ids = [item for item in (args.ids or "").split(",") if item]
    evidences = analyse(args.base, args.head, ids or None, run_tests=not args.no_run)
    report = render_markdown(evidences, args.base, args.head)
    print(report)
    if args.markdown:
        with open(args.markdown, "a") as handle:
            handle.write(report + "\n")
    return 0 if all(evidence.ok for evidence in evidences.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
