"""任务书准入检查：docs/plans/task-*.md 是派发给执行方的合同，不合格的不派发（设计文档第四节）。

每份任务书开头是一段 YAML 头部（只支持下面这种子集：`键: 值`、一层嵌套、`[a, b]` 行内列表、`#` 注释）：

    ---
    task: T005
    class: K4              # 任务类别：K1 说明与记录、K2 补测试、K3 行为不变重构、K4 缺陷修复、
                           # K5 功能与规格实现、K6 界面与组件、K7 护栏与流程、K8 发版
    risk: R2               # 预期风险；CI 仍按路径独立判定
    designer: claude-code  # 任务设计方（claude-code / codex），用于评审分离
    size: medium           # small / medium / large
    architecture: false    # 架构级变更：跨两个以上模块、新增或修改公共接口、数据迁移、新增依赖
    spec_refs: [DR14]      # 挂钩的规格验收编号；为空时写 no_spec_reason
    budget:
      wall_clock_min: 60
      ci_rounds: 3
      retries: 2
      tokens: null
    rollback: git revert
    ---

检查内容：
  - 头部字段齐全、取值合法；类别与风险相容（K2 只允许 R0，K3 只允许 R1……）；预算不超过上限。
  - 「验收」表非空；每行的编号是现役规格中的验收编号、`新增:<规格文件>#<编号>`（同一改动在规格里新增）
    或 `不挂规格：<原因>`；证据类型在 acceptance.py 的词表中；可自动化的行写明测试名或命令（反引号）。
  - 中、大任务有「非目标」「前置条件」「步骤与提交顺序」。
  - 与「步骤与提交顺序」中列出的文件交叉核对：预期风险不低于这些路径的风险；触及护栏路径须为 K7；
    跨两个以上模块或触及架构级路径须声明 architecture: true。只是交叉核对：实现 PR 仍按实际改动判级。
  - .harness/state/taskbook-exempt.txt 登记的历史任务只检查头部可解析、字段齐全（清单只能缩减）。

    bin/harness taskbook                    检查全部任务书（verify 各档运行）
    bin/harness taskbook docs/plans/task-005-x.md --on-main   派发前：另要求与 origin/main 上的版本一致
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path

from engine.checks import acceptance, quality
from engine.core import events
from engine.core.common import ROOT, STATE_DIR, git, load_rules, path_matches

TASK_GLOB = "task-*.md"
EXEMPT_FILE = STATE_DIR / "taskbook-exempt.txt"
# 类别 → 允许的预期风险（设计文档第十五节 15.1）。
CLASS_RISKS = {
    "K1": {"R0"},
    "K2": {"R0"},
    "K3": {"R1"},
    "K4": {"R2", "R3"},
    "K5": {"R2", "R3"},
    "K6": {"R2", "R3"},
    "K7": {"R3"},
    "K8": {"R3"},
}
# 任务书须经用户审的类别（2026-09-28 决定 1）：护栏与流程、发版；另加 architecture: true。
USER_REVIEW_CLASSES = {"K7", "K8"}
DESIGNERS = {"claude-code", "codex"}
SIZES = {"small", "medium", "large"}
BUDGET_LIMITS = {"ci_rounds": 3, "retries": 2}
REQUIRED = ["task", "class", "risk", "designer", "size", "architecture", "budget", "rollback"]
NEW_REF = re.compile(r"^新增[:：]\s*(?P<spec>[^#\s]+)#(?P<id>[A-Z]{1,3}\d+)$")
NO_SPEC = re.compile(r"^不挂规格[:：]\s*(?P<reason>\S.*)$")
# 步骤里的文件路径：多级路径、根目录单点与多点文件名（README.zh-CN.md，B61）都完整识别，
# 否则多点文件名被截断、不参与类别/风险的交叉核对。
PATH_TOKEN = re.compile(
    r"`([A-Za-z0-9_.*-]+(?:/[A-Za-z0-9_.*-]+)+"
    r"|[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.(?:py|swift|md|toml|txt|json))`"
)


class HeaderError(ValueError):
    pass


def _scalar(text: str):
    text = text.strip()
    if text in ("", "null", "~"):
        return None
    if text in ("true", "false"):
        return text == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if text.startswith("[") and text.endswith("]"):
        return [_scalar(part) for part in text[1:-1].split(",") if part.strip()]
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _strip_comment(line: str) -> str:
    # 只认「空白 + #」开头的注释；值里不含引号内的 #（本项目的头部用不到）。
    return re.sub(r"(^|\s)#.*$", "", line).rstrip()


def parse_header(text: str) -> tuple[dict, str]:
    """返回 (头部, 正文)。没有头部或格式超出子集时抛 HeaderError。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise HeaderError("缺少 YAML 头部（文件第一行应为 ---）")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        raise HeaderError("YAML 头部没有结束的 ---") from None
    header: dict = {}
    parent: str | None = None
    for number, raw in enumerate(lines[1:end], 2):
        line = _strip_comment(raw)
        if not line.strip():
            continue
        match = re.fullmatch(r"(?P<indent>\s*)(?P<key>[a-z_]+):(?P<value>.*)", line)
        if not match:
            raise HeaderError(f"头部第 {number} 行无法解析：{raw.strip()}")
        key, value, nested = match["key"], match["value"], bool(match["indent"])
        if nested:
            if parent is None or not isinstance(header.get(parent), dict):
                raise HeaderError(f"头部第 {number} 行缩进了，但上一级不是映射")
            header[parent][key] = _scalar(value)
            continue
        if key in header:
            raise HeaderError(f"头部字段 {key} 重复")
        if value.strip():
            header[key], parent = _scalar(value), None
        else:
            header[key], parent = {}, key
    return header, "\n".join(lines[end + 1 :])


def sections(body: str) -> dict[str, str]:
    """二级标题 → 内容。标题按前缀识别（「前置条件（不满足就停下报告）」记为「前置条件」）。"""
    result: dict[str, list[str]] = {}
    current = None
    for line in body.splitlines():
        if line.startswith("## "):
            current = re.split(r"[（(]", line[3:].strip(), maxsplit=1)[0].strip()
            result[current] = []
        elif current is not None:
            result[current].append(line)
    return {name: "\n".join(lines) for name, lines in result.items()}


def _section(parts: dict[str, str], prefix: str) -> str | None:
    return next((text for name, text in parts.items() if name.startswith(prefix)), None)


@dataclass
class Row:
    ref: str
    kinds: list[str]
    cover: str


@dataclass
class Report:
    path: str
    header: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # 体量提示（B119）：不参与合格判定、不改退出码

    @property
    def needs_user_review(self) -> bool:
        """护栏与流程、发版、架构级的任务书须经用户审（2026-09-28 决定 1）。"""
        return self.header.get("class") in USER_REVIEW_CLASSES or self.header.get("architecture") is True


def acceptance_rows(text: str) -> list[Row]:
    rows = []
    columns = None
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            columns = None
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if "编号" in cells and "证据类型" in cells:
            cover = next((i for i, cell in enumerate(cells) if cell.startswith("覆盖")), None)
            columns = {"id": cells.index("编号"), "kind": cells.index("证据类型"), "cover": cover}
            continue
        if columns is None or set(line.replace("|", "").strip()) <= set("-: "):
            continue
        kinds = [part.strip() for part in re.split(r"[、+＋]", _cell(cells, columns["kind"])) if part.strip()]
        rows.append(Row(_cell(cells, columns["id"]).strip("`"), kinds, _cell(cells, columns["cover"])))
    return rows


def _cell(cells: list[str], index: int | None) -> str:
    return cells[index] if index is not None and index < len(cells) else ""


def check_header(header: dict, path: str) -> list[str]:
    errors = [f"头部缺少字段 {key}" for key in REQUIRED if key not in header]
    number = re.match(r"task-(\d+)", Path(path).name)
    task = header.get("task")
    if "task" in header and (not isinstance(task, str) or not re.fullmatch(r"T\d{3,}", task)):
        errors.append(f"task 应为 T<三位编号>，实际为 {task!r}")
    elif number and isinstance(task, str) and int(task[1:]) != int(number[1]):
        errors.append(f"task {task} 与文件名编号 {number[1]} 不一致")
    klass, level = header.get("class"), header.get("risk")
    if "class" in header and klass not in CLASS_RISKS:
        errors.append(f"class 取值非法：{klass!r}（K1–K8）")
    elif "risk" in header and level not in CLASS_RISKS[klass]:
        errors.append(f"class {klass} 与 risk {level!r} 不相容（允许 {'、'.join(sorted(CLASS_RISKS[klass]))}）")
    if "designer" in header and header["designer"] not in DESIGNERS:
        errors.append(f"designer 取值非法：{header['designer']!r}（{'、'.join(sorted(DESIGNERS))}）")
    if "size" in header and header["size"] not in SIZES:
        errors.append(f"size 取值非法：{header['size']!r}（{'、'.join(sorted(SIZES))}）")
    if "architecture" in header and not isinstance(header["architecture"], bool):
        errors.append("architecture 应为 true 或 false")
    refs = header.get("spec_refs")
    if refs is not None and not (isinstance(refs, list) and all(isinstance(ref, str) for ref in refs)):
        errors.append("spec_refs 应为编号列表，如 [DR14, U4]")
    elif not refs and not str(header.get("no_spec_reason") or "").strip():
        errors.append("spec_refs 为空时须写 no_spec_reason")
    budget = header.get("budget")
    if "budget" in header and not isinstance(budget, dict):
        errors.append("budget 应为映射（wall_clock_min、ci_rounds、retries、tokens）")
    elif isinstance(budget, dict):
        for key in ("wall_clock_min", "ci_rounds", "retries"):
            value = budget.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(f"budget.{key} 应为非负整数")
            elif key in BUDGET_LIMITS and value > BUDGET_LIMITS[key]:
                errors.append(f"budget.{key} = {value} 超过上限 {BUDGET_LIMITS[key]}")
    if "rollback" in header and not str(header.get("rollback") or "").strip():
        errors.append("rollback 不能为空")
    return errors


def check_rows(rows: list[Row], header: dict, spec_ids: dict[str, str], root: Path) -> list[str]:
    if not rows:
        return ["「验收」表为空，或缺少含「编号」「证据类型」「覆盖」的表头"]
    errors = []
    linked: set[str] = set()
    vocabulary = acceptance.AUTOMATABLE | acceptance.MANUAL
    for index, row in enumerate(rows, 1):
        where = f"验收第 {index} 行"
        new = NEW_REF.match(row.ref)
        if acceptance.ID_RE.match(row.ref):
            if row.ref not in spec_ids:
                errors.append(f"{where}：{row.ref} 不是现役规格中的验收编号")
            linked.add(row.ref)
        elif new:
            target = root / new["spec"]
            ids = {item.id for item in acceptance.parse_spec(target, root)} if target.is_file() else set()
            if new["id"] not in ids:
                errors.append(f"{where}：{new['spec']} 中没有新增的编号 {new['id']}（新需求先进规格）")
            linked.add(new["id"])
        elif not NO_SPEC.match(row.ref):
            errors.append(f"{where}：编号 {row.ref!r} 须为规格验收编号、新增:<规格>#<编号> 或 不挂规格：<原因>")
        unknown = [kind for kind in row.kinds if kind not in vocabulary]
        if not row.kinds or unknown:
            errors.append(f"{where}：证据类型缺失或不在词表中（{'、'.join(unknown) or '空'}）")
        elif any(kind in acceptance.AUTOMATABLE for kind in row.kinds) and "`" not in row.cover:
            errors.append(f"{where}：可自动化条目须在「覆盖」写明测试名或命令（反引号）")
    refs = set(header.get("spec_refs") or [])
    if refs - linked:
        errors.append(f"spec_refs 中的 {'、'.join(sorted(refs - linked))} 没有出现在验收表里")
    if linked - refs:
        errors.append(f"验收表挂了 {'、'.join(sorted(linked - refs))}，spec_refs 里没有")
    return errors


def step_files(steps: str) -> list[str]:
    """「步骤与提交顺序」中列出的文件：有「涉及文件」列时只取该列（验证命令如 `bin/verify` 不算），否则取全文。"""
    column = None
    cells_text = []
    for line in steps.splitlines():
        if not line.lstrip().startswith("|"):
            column = None
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if column is None:
            column = next((index for index, cell in enumerate(cells) if cell.startswith("涉及文件")), None)
            continue
        cells_text.append(_cell(cells, column))
    source = "\n".join(cells_text) if cells_text else steps
    return sorted(set(PATH_TOKEN.findall(source)))


def check_steps(steps: str, header: dict, rules: dict) -> list[str]:
    """用「步骤与提交顺序」中列出的文件交叉核对类别、风险与架构级声明。"""
    paths = step_files(steps)
    config = rules.get("taskbook", {})
    risk_rules = rules.get("risk", {})
    errors = []
    guard = [path for path in paths if path_matches(path, config.get("guard_paths", []))]
    if guard and header.get("class") not in USER_REVIEW_CLASSES:
        errors.append(f"步骤触及护栏或流程路径（{'、'.join(guard)}），class 应为 K7")
    if header.get("architecture") is not True:
        modules = {name for path in paths for name, pattern in config.get("modules", {}).items() if path_matches(path, pattern)}
        arch = [path for path in paths if path_matches(path, config.get("architecture_paths", []))]
        if len(modules) >= 2:
            errors.append(f"步骤跨 {len(modules)} 个模块（{'、'.join(sorted(modules))}），应声明 architecture: true")
        if arch:
            errors.append(f"步骤触及架构级路径（{'、'.join(arch)}），应声明 architecture: true")
    declared = header.get("risk")
    if declared in {"R0", "R1", "R2"}:
        r3 = [path for path in paths if path_matches(path, risk_rules.get("r3", []))]
        if r3:
            errors.append(f"步骤触及 R3 路径（{'、'.join(r3)}），risk 应为 R3")
    return errors


HEADROOM_MARGIN = 100  # 白名单未声明净增时，距质量棘轮上限不足这个行数就必须声明（B119）


def _ratchet_files(root: Path) -> list[Path]:
    """质量棘轮统计范围内的文件：目录与通配取自 quality.sources()，与 quality.measure 同口径（B119）。"""
    return [path for directory, pattern in quality.sources() for path in (root / directory).glob(pattern)]


def headroom_errors(path: str, root: Path = ROOT) -> list[str]:
    """白名单里已存在 Python 文件的行数余量（B119）：声明净增会撞或距上限不足 HEADROOM_MARGIN 行即报错。

    只在派发准入（dispatch.admit）与显式传路径的 `bin/harness taskbook <路径>` 调用，不进 check_all：
    历史任务书里「当前 N 行」的陈述会随文件增长过期，verify 扫全部任务书时不能因它失败。
    行数口径与 quality.measure 相同（len(text.splitlines())），上限取 quality.LONG_FILE。
    """
    try:
        text = (root / path).read_text(encoding="utf-8")
    except OSError:
        return []
    scope = _ratchet_files(root)
    limit = quality.LONG_FILE
    errors = []
    for line in sections(text).get("白名单", "").splitlines():
        entry = line.strip()
        if not entry.startswith("- "):
            continue
        match = re.search(r"`([^`]+)`", entry)  # 同一条目里有多个路径时只看第一个，其余忽略
        if not match or not match[1].endswith(".py"):
            continue
        target = root / match[1]
        if target not in scope or not target.is_file():
            continue
        current = len(target.read_text(encoding="utf-8").splitlines())
        declared = re.search(r"净增(?:不超过|不得超过)\s*(\d+)\s*行", entry)
        if declared:
            added = int(declared[1])
            if current + added > limit:
                errors.append(f"白名单 {match[1]}：当前 {current} 行，声明净增不超过 {added} 行，"
                              f"{current}+{added}={current + added} 超过质量棘轮上限 {limit} 行："
                              "先写明把代码放进新模块或搬迁的步骤，或降低净增")
        elif current >= limit - HEADROOM_MARGIN:
            errors.append(f"白名单 {match[1]}：当前 {current} 行，距质量棘轮上限 {limit} 行不足 "
                          f"{HEADROOM_MARGIN} 行：请写明「净增不超过 N 行」且 当前+N ≤ {limit}")
    return errors


def check_text(text: str, path: str, spec_ids: dict[str, str], root: Path = ROOT, rules: dict | None = None,
               exempt: bool = False) -> Report:
    rules = rules or load_rules()
    report = Report(path)
    try:
        report.header, body = parse_header(text)
    except HeaderError as error:
        report.errors.append(str(error))
        return report
    if exempt:  # 历史任务只要求头部可解析、字段齐全（取值按当时实际填写，可能不满足现行约束）
        report.errors += [f"头部缺少字段 {key}" for key in REQUIRED if key not in report.header]
        return report
    report.errors += check_header(report.header, path)
    parts = sections(body)
    if _section(parts, "目标终态") is None:
        report.errors.append("缺少「目标终态」")
    rows = acceptance_rows(_section(parts, "验收") or "")
    report.errors += check_rows(rows, report.header, spec_ids, root)
    if len(rows) >= 10:
        report.warnings.append(f"验收表 {len(rows)} 行，T718、T719 均因体量超出一次派发预算而多轮返工，"
                               "考虑拆分（或确认已拆到无法再拆）")
    if report.header.get("size") in {"medium", "large"}:
        for name in ("非目标", "前置条件", "步骤与提交顺序"):
            if _section(parts, name) is None:
                report.errors.append(f"中、大任务缺少「{name}」")
    report.errors += check_steps(_section(parts, "步骤与提交顺序") or "", report.header, rules)
    return report


def load_exempt(path: Path = EXEMPT_FILE) -> dict[str, str]:
    exempt = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            entry, _, reason = line.partition("#")
            if entry.strip():
                exempt[entry.strip()] = reason.strip()
    return exempt


def spec_ids(root: Path = ROOT) -> dict[str, str]:
    return {item.id: item.spec for spec in sorted((root / "docs" / "specs").glob("*.md"))
            for item in acceptance.parse_spec(spec, root)}


def check_all(root: Path = ROOT, exempt: dict[str, str] | None = None, rules: dict | None = None) -> list[Report]:
    exempt = load_exempt(root / ".harness" / "state" / "taskbook-exempt.txt") if exempt is None else exempt
    ids = spec_ids(root)
    reports = []
    tasks = sorted((root / "docs" / "plans").glob(TASK_GLOB))
    for task in tasks:
        rel = task.relative_to(root).as_posix()
        reports.append(check_text(task.read_text(encoding="utf-8"), rel, ids, root, rules, exempt=rel in exempt))
    names = {task.relative_to(root).as_posix() for task in tasks}
    for entry in exempt:
        if entry not in names:
            reports.append(Report(entry, errors=["taskbook-exempt.txt 登记的任务书不存在"]))
        elif not exempt[entry]:
            reports.append(Report(entry, errors=["taskbook-exempt.txt 的登记须写明原因（# 之后）"]))
    return reports


def on_main(path: str, root: Path = ROOT) -> str | None:
    """派发前：任务书须已合并到 main（即已按类别经过审查）。"""
    main_text = git("show", f"origin/main:{path}", cwd=root, check=False)
    if not main_text:
        return f"{path} 不在 origin/main 上：先合并任务书再派发"
    if main_text != (root / path).read_text(encoding="utf-8").rstrip("\n"):
        return f"{path} 与 origin/main 上的版本不一致：先合并改动再派发"
    return None


# 问题文案前缀 → 稳定规则键（观察侧归類用，不参与判定；顺序即优先级，前缀长的在前）。
_PROBLEM_RULES = (
    ("白名单", "taskbook.headroom"),
    ("class 取值非法", "header.class"),
    ("class ", "header.class_risk"),
    ("spec_refs 中的", "acceptance.link"),
    ("spec_refs", "header.spec_refs"),
    ("步骤触及护栏", "steps.class"),
    ("步骤触及架构级路径", "steps.architecture"),
    ("步骤触及 R3 路径", "steps.risk"),
    ("步骤跨", "steps.architecture"),
    ("头部缺少字段", "header.required"),
    ("task ", "header.task"),
    ("designer", "header.designer"),
    ("size", "header.size"),
    ("architecture", "header.architecture"),
    ("budget", "header.budget"),
    ("rollback", "header.rollback"),
    ("「验收」表为空", "acceptance.table"),
    ("验收第", "acceptance.row"),
    ("验收表挂了", "acceptance.link"),
    ("缺少「目标终态」", "section.goal"),
    ("中、大任务缺少", "section.required"),
    ("不是 docs/plans", "path"),
)


def _problem_rule(error: str) -> str:
    """观察旁路：把问题文案归到稳定的规则键（只用于事件计数，不参与判定；未识别的归 other）。"""
    for prefix, rule in _PROBLEM_RULES:
        if error.startswith(prefix):
            return rule
    if "origin/main" in error:
        return "on_main"
    if "taskbook-exempt.txt" in error:
        return "exempt"
    return "other"


def _admission_inputs(path: str, root: Path, rev: str) -> list[dict]:
    """任务书引用（路径@提交 + 内容哈希）；文件读不到时只带路径，不带哈希。"""
    target = root / path
    if not target.is_file():
        return [events.ref("taskbook", path)]
    try:
        return [events.file_ref("taskbook", target, rev=rev or None)]
    except OSError:
        return [events.ref("taskbook", path)]


def _record_admission(reports: list[Report], root: Path = ROOT) -> None:
    """观察旁路：不合格的任务书各一条 taskbook.admit（问题规则计数），加一条 taskbook.summary 汇总（B94）。"""
    try:
        rev = git("rev-parse", "HEAD", cwd=root, check=False)
        for report in reports:
            if not report.errors:
                continue
            counts: dict[str, int] = {}
            for error in report.errors:
                rule = _problem_rule(error)
                counts[rule] = counts.get(rule, 0) + 1
            events.emit(stage="ci", step="taskbook.admit", status="fail",
                        inputs=_admission_inputs(report.path, root, rev),
                        outputs={"problems": len(report.errors), **counts},
                        decision={"by": "taskbook", "rule": "admit", "reason": report.errors[0]})
        failed = sum(1 for report in reports if report.errors)
        events.emit(stage="ci", step="taskbook.summary", status="fail" if failed else "ok",
                    outputs={"total": len(reports), "failed": failed},
                    decision={"by": "taskbook", "rule": "admit",
                              "reason": f"{failed} 份不合格" if failed else "合格"})
    except Exception:  # noqa: BLE001  设计要求：事件失败不得影响调用方
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", help="只检查这些任务书（默认全部）")
    parser.add_argument("--on-main", action="store_true", help="另要求与 origin/main 上的版本一致（派发前）")
    args = parser.parse_args(argv)
    reports = check_all(ROOT)
    if args.paths:
        wanted = {Path(path).as_posix().removeprefix("./") for path in args.paths}
        reports = [report for report in reports if report.path in wanted]
        missing = wanted - {report.path for report in reports}
        reports += [Report(path, errors=["不是 docs/plans/task-*.md 下的任务书"]) for path in sorted(missing)]
        for report in reports:  # 显式传路径时才查行数余量（B119），check_all 不查
            report.errors += headroom_errors(report.path, ROOT)
            if args.on_main:
                problem = on_main(report.path)
                if problem:
                    report.errors.append(problem)
    failed = [report for report in reports if report.errors]
    print(f"任务书 {len(reports)} 份：合格 {len(reports) - len(failed)}，不合格 {len(failed)}")
    for report in failed:
        for error in report.errors:
            print(f"✗ {report.path}：{error}")
    for report in reports:
        for warning in report.warnings:
            print(f"提示：{report.path}：{warning}")
    _record_admission(reports, ROOT)  # 观察：不合格逐份引用与计数，另加一条汇总
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
