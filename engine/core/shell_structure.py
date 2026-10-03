"""按命令结构判断 shell 命令是否越权（方案 §13 E3）：只检查真正会执行的命令及其参数。

字符串规则会把 echo、grep 的模式、提交说明、Agent 提示词、heredoc 正文里的文字当成命令而误拒；
这里先把命令拆成一条条简单命令，只对会执行的程序（git、rm、gh、curl 与覆盖变量的赋值）按参数判断。

会执行代码、但无法按结构判断的地方退回字符串规则（由调用方传入）：
- `python -c`、`node -e` 等解释器代码；
- 从 stdin 读取并执行的 shell 或解释器（`… | sh`、`bash <<EOF`、`python3 - <<EOF`）。
`bash -c`、`eval`、`$(...)`、反引号里的命令递归按结构判断。无法解析（引号不配对）时抛 ValueError，
调用方整段退回字符串规则。
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Callable

from engine.core.common import setting

FILTER = "改写历史（v0.8.0 曾因此改写 main 与 18 个 tag）"
FORCE = "强制推送：已推送的历史不改写"
FORCE_REFSPEC = "强制推送（+refspec）：已推送的历史不改写"
BATCH = "批量推送（--mirror/--all/--tags）"
PUSH_MAIN = "直接推送 main：请推功能分支并开 PR"
PUSH_TAG = "推送 tag：推 tag 会触发发布，只能由用户执行"
TAG = "创建、移动或删除 tag：发版由用户执行"
UPDATE_REF = "直接改写 main 或 tag 引用"
NO_VERIFY = "--no-verify 跳过 git 守卫"
NO_VERIFY_SHORT = "-n（--no-verify）跳过 git 守卫"
HOOKS_PATH = "修改 core.hooksPath 会绕过 git 守卫"
OVERRIDE = "覆盖变量只供人使用，Agent 不能自行放开守卫"
RESET_HARD = "git reset --hard 会丢弃未提交的改动（v0.8.0 X1 的修复就这样丢失）；先提交或 stash"
CLEAN_X = "git clean -x 会删除项目登记要保留的本地运行时（checks.toml [runtime] preserve）；用 -e 逐个排除：{paths}"
RM_OUTSIDE = "递归删除工作区以外的路径（临时目录 /tmp 除外）"
RELEASE = "gh release（list、view 以外）：发版由用户执行"
ISSUE_DELETE = "删除议题会销毁逃逸、抽审与升级登记；已关闭的议题照样计数"
LABEL_ERASE = "撤下或删改 escape、audit、escalation、budget-exceeded、class:K* 标签会让误差预算与升级登记失真"
ISSUE_IMPLEMENTER = "执行者不能关闭、重开或改动议题（含标签）：逃逸、抽审与升级登记由设计评审方处理"
REVIEW_SIGNAL = "执行者不能发表 PR 或议题评论、提交评审、运行独立评审或设计方复核：这些是自动合并读取的结论，由评审方与设计方写"
# 登记用的标签（设计 14.3）：任何 Agent 都不能撤下或删改。
PROTECTED_LABELS = re.compile(r"(^|,)\s*(escape|audit|escalation|budget-exceeded|class:K\d)\s*(,|$)")
READ_ONLY_RELEASE = {"list", "view"}
IMPLEMENTER_ISSUE_ACTIONS = {"close", "reopen", "edit", "transfer", "lock", "unlock", "pin", "unpin", "delete"}
# 派发脚本里写评审与复核结论的子命令（自治试验设计 6）：执行者与评审账号同体，不能自己运行。
IMPLEMENTER_DISPATCH_ACTIONS = {"review", "review-calibrate", "signoff"}
# 执行者与评审账号同体：这些 gh 子命令写的是自动合并读取的结论（自治试验设计 6），执行者不能写。
IMPLEMENTER_REVIEW_ACTIONS = {("issue", "comment"), ("pr", "comment"), ("pr", "review")}
DELETE_REMOTE = "删除 CI 记录或远端资源会销毁证据"
API_WRITE = "GitHub API 写操作（改 ref、写文件、删资源）会绕过分支保护与评审；需要时交给用户"
MERGE = "合并 PR：R0/R1 由仓库 auto-merge 合并，R2 及以上由用户合并，Agent 不自行合并（方案 §13 D4）"
APPROVE = "批准 PR：合并前须由非推送者批准；R0/R1 由 auto-merge 的 App 批准，R2 及以上由用户批准，Agent 不批准（方案 §13 D3）"

SEPARATOR_CHARS = set(";&|()\n")
KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "for", "case", "esac", "!", "{", "}", "time"}
WRAPPERS = {"sudo", "command", "exec", "nohup", "nice", "builtin"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
INTERPRETERS = {"python", "python3", "node", "ruby", "perl", "php", "bun", "deno", "osascript"}
DECLARERS = {"export", "declare", "typeset", "readonly", "local"}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
ASSIGNMENT = re.compile(r"^([^=\s]+)=")
TEMP_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/")
HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][\w.-]*)\2")

TextRules = Callable[[str], list[str]]


def check(command: str, text_rules: TextRules, role: str = "designer") -> list[str]:
    body, substitutions, heredocs = _strip_heredocs_and_substitutions(command)
    reasons: list[str] = []
    for inner in substitutions:
        reasons += check(inner, text_rules, role)
    for words, stdin in _simple_commands(body):
        found, executes_stdin = _check_simple(words, text_rules, role)
        reasons += found
        if not executes_stdin:
            continue
        # 交给 shell 或解释器执行的 stdin 看不到结构，按字符串规则判断：heredoc 与 here-string 只看被执行的那段，
        # 管道与来源不明时整段判断（上游是什么看不清）。
        if stdin[0] == "heredoc" and stdin[1] < len(heredocs):
            reasons += text_rules(heredocs[stdin[1]])
        elif stdin[0] == "herestring":
            reasons += text_rules(stdin[1])
        else:
            reasons += text_rules(command)
    return reasons


def _strip_heredocs_and_substitutions(command: str) -> tuple[str, list[str], list[str]]:
    """去掉 heredoc 正文（按出现顺序另存，交给 shell 或解释器执行时单独判断），取出 $(...) 与反引号里的命令。"""
    lines, kept, pending, heredocs, current = command.split("\n"), [], [], [], []
    for line in lines:
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
                heredocs.append("\n".join(current))
                current = []
            else:
                current.append(line)
            continue
        kept.append(line)
        pending = [match.group(3) for match in HEREDOC.finditer(line)]
    body = "\n".join(kept)
    substitutions = []
    index = 0
    while (start := body.find("$(", index)) != -1:
        if body.startswith("$((", start):  # 算术展开
            index = start + 3
            continue
        depth, end = 0, start + 1
        while end < len(body):
            if body[end] == "(":
                depth += 1
            elif body[end] == ")":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        substitutions.append(body[start + 2 : end])
        index = start + 2
    substitutions += re.findall(r"`([^`]*)`", body)
    return body, substitutions, heredocs


def _simple_commands(text: str) -> list[tuple[list[str], tuple]]:
    """拆成简单命令，并记下各自 stdin 的来源：("heredoc", 序号)、("herestring", 文本)、("pipe",) 或 (None,)。"""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    commands, current, stdin, heredoc_index = [], [], (None,), 0
    tokens = list(lexer)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token and set(token) <= SEPARATOR_CHARS:
            if current:
                commands.append((current, stdin))
            current = []
            stdin = ("pipe",) if token in ("|", "|&") else (None,)
        elif token in ("<<", "<<-"):  # heredoc：下一个词是分隔符，正文按出现顺序对应
            stdin = ("heredoc", heredoc_index)
            heredoc_index += 1
            index += 1
        elif token == "<<<":  # here-string：下一个词就是 stdin 的内容
            stdin = ("herestring", tokens[index + 1] if index + 1 < len(tokens) else "")
            index += 1
        elif token and set(token) <= set("<>&"):  # 其他重定向：下一个词是文件名
            index += 1
        else:
            current.append(token)
        index += 1
    if current:
        commands.append((current, stdin))
    return commands


def _override_name(name: str) -> bool:
    return (
        name.startswith("HARNESS_")
        or re.search(r"_ALLOW_(MAIN|TAG|REWRITE)$|_SKIP_VERIFY$", name) is not None
        or "$" in name  # 变量拼出来的名字（${P}_ALLOW_TAG）看不出指向谁，按覆盖变量处理
        or "`" in name
    )


def _check_assignments(words: list[str]) -> list[str]:
    reasons = []
    for word in words:
        match = ASSIGNMENT.match(word)
        name = match.group(1) if match else word
        if _override_name(name):
            reasons.append(OVERRIDE)
    return reasons


def _check_simple(words: list[str], text_rules: TextRules, role: str = "designer") -> tuple[list[str], bool]:
    reasons: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        program = os.path.basename(word)
        if word in KEYWORDS:
            index += 1
        elif ASSIGNMENT.match(word):
            reasons += _check_assignments([word])
            index += 1
        elif program == "env":
            index += 1
            while index < len(words) and (words[index].startswith("-") or ASSIGNMENT.match(words[index])):
                if words[index] in ("-u", "--unset", "-C", "--chdir"):
                    index += 1
                else:
                    reasons += _check_assignments([words[index]]) if ASSIGNMENT.match(words[index]) else []
                index += 1
        elif program in WRAPPERS or program == "xargs":
            index += 1
            while index < len(words) and words[index].startswith("-"):
                index += 2 if words[index] in ("-n", "-u", "-g", "-I", "-P") else 1
        elif program == "timeout":
            index += 1
            while index < len(words) and words[index].startswith("-"):
                index += 1
            index += 1  # 时长
        else:
            break
    if index >= len(words):
        return reasons, False
    program, args = os.path.basename(words[index]), words[index + 1 :]
    if program == "dispatch" and role == "implementer":
        positional = [arg for arg in args if not arg.startswith("-")]
        if positional and positional[0] in IMPLEMENTER_DISPATCH_ACTIONS:
            return reasons + [REVIEW_SIGNAL], False
    if program in ("harness", "cli.py") and role == "implementer" and _review_entrypoint(args):
        return reasons + [REVIEW_SIGNAL], False
    if program in DECLARERS:
        return reasons + _check_assignments([arg for arg in args if not arg.startswith("-")]), False
    if program == "eval":
        return reasons + check(" ".join(args), text_rules, role), False
    if program in SHELLS:
        code = _option_value(args, "c")
        if code is not None:
            return reasons + check(code, text_rules, role), False
        return reasons, _reads_stdin(args)
    if program in INTERPRETERS or re.fullmatch(r"python3(\.\d+)?", program):
        code = _option_value(args, "c") if program.startswith("python") else _option_value(args, "e")
        if code is not None:
            return reasons + text_rules(code), False
        if program.startswith("python") and role == "implementer":
            target, rest = _python_target(args)  # cli.py 脚本或 -m engine.cli 模块，同 harness 入口
            if target in ("cli.py", "engine.cli") and _review_entrypoint(rest):
                reasons.append(REVIEW_SIGNAL)
        return reasons, _reads_stdin(args)
    if program == "git":
        return reasons + _git(args), False
    if program == "git-filter-repo":
        return reasons + [FILTER], False
    if program == "rm":
        return reasons + _rm(args), False
    if program == "gh":
        return reasons + _gh(args, role), False
    if program == "curl":
        return reasons + _curl(args), False
    return reasons, False


# Python 解释器带值的选项：分开写（-W ignore、-X dev）时下一个参数是值，不是脚本；-c 由调用方先处理。
PYTHON_VALUE_OPTIONS = ("-W", "-X")


def _python_target(args: list[str]) -> tuple[str | None, list[str]]:
    """(脚本文件名或 -m 的模块名, 之后属于脚本的参数)：按 Python 的参数语法先消费解释器选项及其值。"""
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-m" or (arg.startswith("-m") and not arg.startswith("--")):
            module = arg[2:] or (args[index + 1] if index + 1 < len(args) else "")
            return module, args[index + (1 if arg[2:] else 2):]
        if arg in PYTHON_VALUE_OPTIONS:
            index += 2
        elif arg.startswith("-"):
            index += 1  # 无值开关，或连写的 -Wignore、-Xdev
        else:
            return os.path.basename(arg), args[index + 1:]
    return None, []


def _review_entrypoint(args: list[str]) -> str | None:
    """harness / cli.py 的评审与复核入口（自治试验设计 6，T706）：dispatch 的 review|review-calibrate|signoff
    子命令，或顶层 review 命令。review-pack、review-plan 只写本地文件，不是入口。"""
    positional = [arg for arg in args if not arg.startswith("-")]
    if not positional:
        return None
    if positional[0] == "review" or (
        positional[0] == "dispatch" and len(positional) > 1 and positional[1] in IMPLEMENTER_DISPATCH_ACTIONS
    ):
        return REVIEW_SIGNAL
    return None


def _option_value(args: list[str], letter: str) -> str | None:
    """`-c CODE`、`-lc CODE` 这类短选项的值；没有则返回 None。"""
    for index, arg in enumerate(args):
        if arg.startswith("-") and not arg.startswith("--") and letter in arg[1:]:
            return args[index + 1] if index + 1 < len(args) else ""
        if not arg.startswith("-"):
            return None  # 脚本文件：之后的参数属于脚本
    return None


def _reads_stdin(args: list[str]) -> bool:
    positional = [arg for arg in args if not arg.startswith("-") or arg == "-"]
    return not positional or positional[0] == "-"


def _short_flags(arg: str) -> str:
    return arg[1:] if arg.startswith("-") and not arg.startswith("--") else ""


def _git(args: list[str]) -> list[str]:
    reasons, index = [], 0
    while index < len(args) and args[index].startswith("-"):
        arg = args[index]
        if arg in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            value = args[index + 1] if index + 1 < len(args) else ""
            if arg == "-c" and value.lower().startswith("core.hookspath="):
                reasons.append(HOOKS_PATH)
            index += 2
        else:
            if arg.lower().startswith("-ccore.hookspath="):
                reasons.append(HOOKS_PATH)
            index += 1
    if index >= len(args):
        return reasons
    sub, rest = args[index], args[index + 1 :]
    if sub in ("filter-repo", "filter-branch"):
        reasons.append(FILTER)
    elif sub == "push":
        reasons += _git_push(rest)
    elif sub == "tag":
        listing = ("-l", "--list", "-v", "--verify", "--contains", "--no-contains", "--points-at", "--merged",
                   "--no-merged", "--sort", "--format", "--column")
        if rest and not rest[0].startswith(listing) and not re.fullmatch(r"-n\d*", rest[0]):
            reasons.append(TAG)
    elif sub == "update-ref":
        if any(arg == "refs/heads/main" or arg.startswith("refs/tags/") for arg in rest):
            reasons.append(UPDATE_REF)
    elif sub == "config":
        reasons += _git_config(rest)
    elif sub == "reset":
        if "--hard" in rest:
            reasons.append(RESET_HARD)
    elif sub == "clean":
        reasons += _git_clean(rest)
    if sub in ("commit", "merge", "rebase", "am") and "--no-verify" in rest:
        reasons.append(NO_VERIFY)
    if sub == "commit":
        reasons += _git_commit_short(rest)
    return reasons


def _git_config(rest: list[str]) -> list[str]:
    """只拦设置与取消 core.hooksPath；`git config core.hooksPath`（不带值）与 --get 等是读取。"""
    keys = [index for index, arg in enumerate(rest) if arg.lower() == "core.hookspath"]
    if not keys:
        return []
    modifying = ("--unset", "--unset-all", "--add", "--replace-all", "--rename-section", "--remove-section", "--edit", "-e")
    value_follows = any(not arg.startswith("-") for arg in rest[keys[0] + 1 :])
    if any(arg in modifying for arg in rest) or value_follows:
        return [HOOKS_PATH]
    return []


def _git_push(rest: list[str]) -> list[str]:
    reasons, positional, index = [], [], 0
    while index < len(rest):
        arg = rest[index]
        flags = _short_flags(arg)
        if arg in ("-o", "--push-option", "--repo", "--receive-pack", "--exec"):
            index += 2
            continue
        if arg == "--force" or arg.startswith("--force-with-lease") or "f" in flags:
            reasons.append(FORCE)
        elif arg in ("--mirror", "--all", "--tags"):
            reasons.append(BATCH)
        elif arg == "--no-verify":
            reasons.append(NO_VERIFY)
        elif not arg.startswith("-"):
            positional.append(arg)
        index += 1
    for refspec in positional[1:]:  # 第一个是远端
        if refspec.startswith("+"):
            reasons.append(FORCE_REFSPEC)
        target = refspec.split(":", 1)[-1].lstrip("+")
        if target in ("main", "refs/heads/main"):
            reasons.append(PUSH_MAIN)
        if target.startswith("refs/tags/") or re.fullmatch(r"v\d[\w.-]*", target):
            reasons.append(PUSH_TAG)
    return reasons


def _git_clean(rest: list[str]) -> list[str]:
    removes_ignored = any(set(_short_flags(arg)) & {"x", "X"} for arg in rest)
    excluded = []
    for index, arg in enumerate(rest):
        if arg == "-e" and index + 1 < len(rest):
            excluded.append(rest[index + 1])
        elif arg.startswith("--exclude="):
            excluded.append(arg.split("=", 1)[1])
    kept = {path.rstrip("/") for path in excluded}
    missing = [path for path in setting("runtime", "preserve", []) if path.rstrip("/") not in kept]
    if removes_ignored and missing:
        return [CLEAN_X.format(paths="、".join(f"-e {path}" for path in missing))]
    return []


def _git_commit_short(rest: list[str]) -> list[str]:
    index = 0
    while index < len(rest):
        flags = _short_flags(rest[index])
        if "n" in flags.split("m")[0].split("F")[0]:  # -n 在带值选项（-m/-F）之前才是选项本身
            return [NO_VERIFY_SHORT]
        index += 2 if flags and flags[-1] in "mFcCt" else 1  # 跳过选项的值（提交说明是数据）
    return []


def _rm(args: list[str]) -> list[str]:
    recursive = any(arg in ("-r", "-R", "--recursive") or set(_short_flags(arg)) & {"r", "R"} for arg in args)
    if not recursive:
        return []
    paths, options_done = [], False
    for arg in args:
        if arg == "--" and not options_done:
            options_done = True
        elif options_done or not arg.startswith("-"):
            paths.append(arg)
    for path in paths:
        outside = path.startswith("/") and not path.startswith(TEMP_PREFIXES)
        if outside or path.startswith(("~", "$HOME", "${HOME}", "..")) or "/.." in path:
            return [RM_OUTSIDE]
    return []


def _label_values(args: list[str], option: str) -> list[str]:
    values = []
    for index, arg in enumerate(args):
        if arg == option and index + 1 < len(args):
            values.append(args[index + 1])
        elif arg.startswith(option + "="):
            values.append(arg[len(option) + 1 :])
    return values


def _gh_issue_and_label(sub: str, action: str, positional: list[str], args: list[str], role: str) -> list[str]:
    """议题与标签是逃逸、抽审、升级的登记（设计 14.3）：任何 Agent 不删议题、不撤登记标签；执行者不改议题，
    也不写评论与评审（自治试验设计 6）。"""
    reasons = []
    if sub == "issue" and action == "delete":
        reasons.append(ISSUE_DELETE)
    if sub in ("issue", "pr") and action == "edit" and any(
        PROTECTED_LABELS.search(value) for value in _label_values(args, "--remove-label")
    ):
        reasons.append(LABEL_ERASE)
    if sub == "label" and action in ("delete", "edit") and any(PROTECTED_LABELS.search(word) for word in positional[2:]):
        reasons.append(LABEL_ERASE)
    if role == "implementer":
        touches_labels = any(arg.startswith(("--add-label", "--remove-label")) for arg in args)
        if (sub == "issue" and action in IMPLEMENTER_ISSUE_ACTIONS) or (sub == "pr" and action == "edit" and touches_labels) \
                or (sub == "label" and action != "list"):
            reasons.append(ISSUE_IMPLEMENTER)
        if (sub, action) in IMPLEMENTER_REVIEW_ACTIONS:
            # 评论与评审标记是自动合并读取的结论（自治试验设计 6）：执行者与评审账号同体，不能自己写。
            reasons.append(REVIEW_SIGNAL)
    return reasons


# gh 的带值全局选项（T706）：分开写（-R o/r、--repo o/r）与连写（--repo=o/r、-Ro/r）都要连值一起先消费，
# 否则选项的值会被当成子命令，带这些选项的写法绕过全部子命令规则。
GH_VALUE_OPTIONS = ("-R", "--repo", "--hostname")


def _gh_value_options_consumed(args: list[str]) -> list[str]:
    rest: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in GH_VALUE_OPTIONS:
            index += 2  # 选项与其值
        elif arg.startswith(("--repo=", "--hostname=")) or re.fullmatch(r"-R\S+", arg):
            index += 1
        else:
            rest.append(arg)
            index += 1
    return rest


def _gh(args: list[str], role: str = "designer") -> list[str]:
    """按原参数与剥掉 -R/--repo/--hostname 后的参数各判一次，取拒绝理由的并集：剥离只为识别被选项值
    挤偏的子命令，不能放宽原解析已拒绝的写法（如 --description -R 中的 -R 是描述内容，T706 二轮评审）。"""
    reasons = _gh_rules(args, role)
    consumed = _gh_value_options_consumed(args)
    if consumed != args:
        reasons += [reason for reason in _gh_rules(consumed, role) if reason not in reasons]
    return reasons


def _gh_rules(args: list[str], role: str = "designer") -> list[str]:
    positional = [arg for arg in args if not arg.startswith("-")]
    sub = positional[0] if positional else ""
    action = positional[1] if len(positional) > 1 else ""
    if sub == "release":
        return [] if action in READ_ONLY_RELEASE else [RELEASE]
    if sub in ("issue", "pr", "label"):
        found = _gh_issue_and_label(sub, action, positional, args, role)
        if found:
            return found
    if sub in ("run", "repo") and action == "delete":
        return [DELETE_REMOTE]
    if sub == "pr" and action == "merge":
        return [MERGE]
    if sub == "pr" and action == "review" and any(arg in ("-a", "--approve") or arg.startswith("--approve=") for arg in args):
        return [APPROVE]
    if sub == "api":
        method = _method(args, ("-X", "--method"), ("-X", "--method="))
        fields = any(
            arg in ("-f", "-F", "--field", "--raw-field", "--input")
            or arg.startswith(("--field=", "--raw-field=", "--input="))
            or re.match(r"-[fF].", arg)
            for arg in args
        )
        if action == "graphql" and _graphql_read_only(args):
            return []  # GraphQL 查询本身就用 POST 与 -f 传参，只有 mutation 才写
        if method in WRITE_METHODS or fields:
            return [API_WRITE]
    return []


def _graphql_read_only(args: list[str]) -> bool:
    """`gh api graphql` 只在参数都内联、且没有 mutation 时算只读；--input 与 -F x=@文件 看不到内容，按写处理。"""
    if any(arg == "--input" or arg.startswith("--input=") or "=@" in arg for arg in args):
        return False
    return not any(re.search(r"(?i)\bmutation\b", arg) for arg in args)


def _curl(args: list[str]) -> list[str]:
    if not any("api.github.com" in arg for arg in args):
        return []
    method = _method(args, ("-X", "--request"), ("-X", "--request="))
    data = any(re.match(r"-[dFT]", arg) or arg.startswith(("--data", "--form", "--upload-file", "--json")) for arg in args)
    return [API_WRITE] if method in WRITE_METHODS or data else []


def _method(args: list[str], separate: tuple[str, ...], attached: tuple[str, ...]) -> str:
    for index, arg in enumerate(args):
        if arg in separate and index + 1 < len(args):
            return args[index + 1].upper()
        for prefix in attached:
            if arg.startswith(prefix) and len(arg) > len(prefix):
                return arg[len(prefix) :].upper()
    return ""
