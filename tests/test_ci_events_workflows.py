"""T303 工作流事件 artifact 与 summary 测试：模板结构解析与假命令步骤重放、verify 失败后仍
export/upload/summary 且原 job 仍失败、上传集合精确等于安全 manifest、90 天保留与 run/attempt/job
物理名唯一、summary 从导出事件确定性渲染、可信路由与安装布局。

夹具沿用 tests/test_trace_events_cli.py 的模式：匿名临时 git 仓库、隔离 events_db.ROOT、递增冻结
时钟；引擎判定步骤用可配置退出的桩命令替换（记录调用、向安装布局的事件库写对应检查事件），
upload-artifact 由重放器记账，export/summary 走安装布局里真实的 ci_events.py；不碰真实库/PR/
工作流，不执行 PR 代码，不调用真 gh。
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import cli
from engine.core import events, events_db, events_io

ENGINE_REPO = Path(__file__).resolve().parents[1]
CLI = ENGINE_REPO / "engine" / "cli.py"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
GITHUB_KEYS = ("CI", "GITHUB_HEAD_REF", "GITHUB_REF_NAME", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
               "GITHUB_JOB", "GITHUB_WORKFLOW_REF", "GITHUB_EVENT_NAME", "GITHUB_WORKSPACE")
BRANCH = "task/303-fixture"
EXPORT_TARGET = "$RUNNER_TEMP/harness-events"  # export --target 与 upload path 的同一目录
_INSTANCE = itertools.count(1)


def canonical(data: dict) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class _Clock:
    """递增冻结时钟：从 2026-01-02T03:01 起每次调用前进一分钟，事件 ts 严格递增且可比。"""

    def __init__(self):
        self.step = 0

    def __call__(self) -> str:
        self.step += 1
        hour, minute = divmod(self.step, 60)
        return f"2026-01-02T{3 + hour // 24:02d}:{(hour % 24 + 1):02d}:{minute:02d}.000Z"


# ---- 最小 YAML 子集解析器：只需完整解析 templates/.github/workflows 的结构 ----

def _prep(text: str) -> list[tuple[int, str]]:
    """行 → (缩进, 内容)；空白行 (-1, "") 保留（块标量内部有意义），纯注释行删除。"""
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip():
            lines.append((-1, ""))
            continue
        body = raw.lstrip(" ")
        if body.startswith("#"):
            continue
        stripped = raw.rstrip()
        indent = len(stripped) - len(stripped.lstrip(" "))
        lines.append((indent, stripped[indent:]))
    return lines


def _skip_blank(lines: list[tuple[int, str]], index: int) -> int:
    while index < len(lines) and lines[index][0] < 0:
        index += 1
    return index


def _scalar(value: str):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    if value in ("true", "True"):
        return True
    if value in ("false", "False"):
        return False
    if value.replace("_", "").isdigit():
        return int(value)
    return value


def _strip_comment(value: str) -> str:
    """去掉普通标量后的 ` #…` 注释（块标量内容不经过这里）。"""
    marker = value.find(" #")
    return value[:marker].strip() if marker >= 0 else value.strip()


def _split_flow(text: str) -> list[str]:
    parts, depth, current = [], 0, ""
    for char in text:
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    if current.strip():
        parts.append(current)
    return parts


def _flow(value: str):
    """流式列表 [a, b]；其余按标量处理（${{ }} 表达式原样保留为字符串）。"""
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [_scalar(part) for part in _split_flow(inner)] if inner else []
    return _scalar(value)


def _parse_block(lines: list[tuple[int, str]], index: int, indent: int):
    index = _skip_blank(lines, index)
    if index < len(lines) and lines[index][0] == indent and lines[index][1].startswith("- "):
        return _parse_list(lines, index, indent)
    return _parse_map(lines, index, indent)


def _parse_list(lines: list[tuple[int, str]], index: int, indent: int):
    items: list = []
    while True:
        index = _skip_blank(lines, index)
        if index >= len(lines) or lines[index][0] != indent or not lines[index][1].startswith("- "):
            break
        virtual = [(indent + 2, lines[index][1][2:])]
        index += 1
        while index < len(lines) and lines[index][0] > indent:
            virtual.append(lines[index])
            index += 1
        value, _ = _parse_block(virtual, 0, indent + 2)
        items.append(value)
    return items, index


def _parse_map(lines: list[tuple[int, str]], index: int, indent: int):
    out: dict = {}
    while True:
        index = _skip_blank(lines, index)
        if index >= len(lines) or lines[index][0] != indent or lines[index][1].startswith("- "):
            break
        key, _, rest = lines[index][1].partition(":")
        key, rest = key.strip(), rest.strip()
        index += 1
        if rest in ("|", "|-", "|+", ">", ">-", ">+"):
            out[key], index = _block_scalar(lines, index, indent, rest)
        elif rest:
            out[key] = _flow(_strip_comment(rest))
        elif index < len(lines) and lines[index][0] > indent:
            out[key], index = _parse_block(lines, index, lines[index][0])
        else:
            out[key] = None
    return out, index


def _block_scalar(lines: list[tuple[int, str]], index: int, key_indent: int, marker: str):
    body: list[str] = []
    content_indent = None
    while index < len(lines):
        if lines[index][0] < 0:  # 块内空白行
            body.append("")
            index += 1
            continue
        if lines[index][0] <= key_indent:
            break
        if content_indent is None:
            content_indent = lines[index][0]
        body.append(" " * max(lines[index][0] - content_indent, 0) + lines[index][1])
        index += 1
    while body and not body[-1]:
        body.pop()
    if marker.startswith(">"):
        folded: list[str] = []
        buffer = ""
        for line in body:
            if line:
                buffer = f"{buffer} {line.strip()}" if buffer else line.strip()
            else:
                folded.append(buffer)
                buffer = ""
        if buffer:
            folded.append(buffer)
        return "\n".join(folded), index
    return "\n".join(body) + "\n", index


def parse_workflow(text: str) -> dict:
    return _parse_block(_prep(text), 0, 0)[0]


_EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def _literal(text: str) -> str:
    text = text.strip()
    return text[1:-1] if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"" else text


def _output_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value
    return out


class _Replay:
    """harness job 步骤重放：run 脚本真跑（引擎判定换桩命令）、uses 动作记账、if/continue-on-error 语义。

    条件子集：无 if（等价 success()）、always()、!cancelled()（重放不模拟取消）、success()、
    failure()、`A == 'b'`、`&&` 连接与 `!` 前缀；查值支持 github.* 上下文与 steps.<id>.outputs.<k>。
    """

    def __init__(self, project: Path, env: dict, context: dict[str, str]):
        self.project = project
        self.env = env
        # github.* 取值缺省来自运行环境（与 Actions 的表达式取值口径一致）
        self.context = {**context, "github.run_id": env.get("GITHUB_RUN_ID", ""),
                        "github.run_attempt": env.get("GITHUB_RUN_ATTEMPT", ""),
                        "github.job": env.get("GITHUB_JOB", ""),
                        "github.event_name": env.get("GITHUB_EVENT_NAME", "")}
        self.uploads: list[dict] = []
        self.ran: list[str] = []
        self.failed: list[str] = []
        self.trace: list[str] = []
        self.swallowed: list[str] = []
        self.step_outputs: dict[str, dict[str, str]] = {}
        self.conclusion = "success"
        self.summary_path = project.parent / f"job-summary-{next(_INSTANCE)}.md"
        self.summary_path.unlink(missing_ok=True)  # 由步骤以追加方式创建
        self.env["GITHUB_STEP_SUMMARY"] = str(self.summary_path)

    def lookup(self, expression: str) -> str | None:
        expression = expression.strip()
        if expression in self.context:
            return self.context[expression]
        parts = expression.split(".")
        if len(parts) == 4 and parts[0] == "steps" and parts[2] == "outputs":
            return self.step_outputs.get(parts[1], {}).get(parts[3])
        return None

    def substitute(self, text: str) -> str:
        def replace(match):
            value = self.lookup(match[1])
            if value is None and match[1].strip() not in self.context:
                raise AssertionError(f"重放器缺表达式取值：{match[1]!r}")
            return value or ""

        return _EXPR.sub(replace, text)

    def condition(self, expression) -> bool:
        if expression is None:
            return self.conclusion == "success"
        return self._evaluate(expression.strip())

    def _evaluate(self, text: str) -> bool:
        if text.startswith("${{") and text.endswith("}}"):
            text = text[3:-2].strip()
        if " && " in text:
            return all(self._evaluate(part) for part in text.split(" && "))
        text = text.strip()
        if text.startswith("!"):
            return not self._evaluate(text[1:])
        if text in ("always()", "!cancelled()"):
            return True
        if text == "cancelled()":
            return False  # 重放不模拟取消
        if text == "success()":
            return self.conclusion == "success"
        if text == "failure()":
            return self.conclusion == "failure"
        if "==" in text:
            left, right = text.split("==", 1)
            return self.lookup(left) == _literal(right)
        if "!=" in text:
            left, right = text.split("!=", 1)
            return self.lookup(left) != _literal(right)
        raise AssertionError(f"重放器不支持的条件：{text!r}")

    def run(self, steps: list[dict], *, stub: Path) -> None:
        for step in steps:
            name = step.get("name") or step.get("uses", "?")
            if not self.condition(step.get("if")):
                continue
            if "uses" in step:
                self._action(step)
                self.ran.append(name)
                continue
            output = self.project.parent / f"github-output-{next(_INSTANCE)}.txt"
            result = subprocess.run(["bash", "-ec", self._command(self.substitute(step["run"]), stub)],
                                    cwd=self.project, check=False, capture_output=True, text=True, timeout=120,
                                    env={**self.env, "GITHUB_OUTPUT": str(output)})
            if step_id := step.get("id"):
                self.step_outputs[step_id] = _output_file(output)
            self.trace.append(f"{name}: code={result.returncode} {result.stderr.strip()[-400:]}")
            self.ran.append(name)
            if result.returncode != 0:
                if step.get("continue-on-error"):
                    self.swallowed.append(name)  # 观察自身失败被隔离，不改变 job 结论
                else:
                    self.failed.append(name)
                    self.conclusion = "failure"

    def _command(self, script: str, stub: Path) -> str:
        """引擎判定换成桩命令；ci_events 用安装布局里的真实模块；其余 python 换当前解释器。"""
        script = script.replace("python .harness/engine/cli.py", f'"{sys.executable}" "{stub}"')
        script = script.replace("python .harness/engine/reports/ci_events.py",
                                f'"{sys.executable}" "{self.project / ".harness/engine/reports/ci_events.py"}"')
        return script.replace("python -m pip", f'"{sys.executable}" -m pip')

    def _action(self, step: dict) -> None:
        if "upload-artifact" in str(step.get("uses")):
            with_ = step.get("with") or {}
            self.uploads.append({"name": self.substitute(str(with_["name"])),
                                 "path": self.substitute(str(with_["path"])),
                                 "retention-days": with_.get("retention-days")})


_STUB = '''"""重放夹具：可配置退出的引擎判定桩——记录调用、写对应检查事件、--markdown 时追加一行原机器输出。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, {syspath!r})
from engine.core import events  # noqa: E402  依赖上面的 sys.path 准备

name = sys.argv[1] if len(sys.argv) > 1 else ""
fail = name in set(os.environ.get("FAKE_FAIL", "").split(",")) - {{""}}
with open({log!r}, "a", encoding="utf-8") as fh:
    fh.write(name + "\\n")
stage = {{"verify": "verify", "integrity": "verify", "risk": "route", "policy": "route"}}.get(name, "ci")
events.emit(stage, f"cli.{{name}}", "fail" if fail else "ok", outputs={{"code": 1 if fail else 0}})
summary = os.environ.get("GITHUB_STEP_SUMMARY")
if "--markdown" in sys.argv and summary:
    with open(summary, "a", encoding="utf-8") as fh:
        fh.write(f"（fake {{name}} summary output）\\n")
sys.exit(1 if fail else 0)
'''


class ObservabilityTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-ci-events-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        root_patch = mock.patch.object(events_db, "ROOT", self.tmp / "unused")
        root_patch.start()
        self.addCleanup(root_patch.stop)
        clock = mock.patch.object(events_db, "_now", new=_Clock())
        clock.start()
        self.addCleanup(clock.stop)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in (*GITHUB_KEYS, "HARNESS_EVENTS"):
            os.environ.pop(key, None)
        events._warned = False
        self.addCleanup(setattr, events, "_warned", False)

    # ---- 夹具 ----

    def fresh_project(self, name: str) -> Path:
        path = self.tmp / name
        path.mkdir()
        env = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, **GIT_ENV}
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True, capture_output=True, env=env)
        (path / "README.md").write_text("# fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True, capture_output=True, env=env)
        return path

    def install(self, project: Path) -> None:
        """真实产品入口：把当前引擎（含 ci_events.py）安装进临时项目。"""
        env = {**{k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "HARNESS_"))}, **GIT_ENV,
               "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, str(CLI), "install", "--target", str(project), "--allow-dirty"],
                                capture_output=True, text=True, env=env, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((project / ".harness/engine/reports/ci_events.py").exists())

    def template(self, name: str) -> dict:
        return parse_workflow((ENGINE_REPO / "templates/.github/workflows" / name).read_text(encoding="utf-8"))

    def ci_keys(self, job: str, attempt: int, run_id: str) -> dict[str, str]:
        """进程内 emit 与子进程共用的 Actions 键（与工作流运行时的取值口径一致）。"""
        return {"CI": "true", "GITHUB_JOB": job, "GITHUB_RUN_ID": run_id, "GITHUB_RUN_ATTEMPT": str(attempt),
                "GITHUB_HEAD_REF": BRANCH, "GITHUB_REF_NAME": BRANCH, "GITHUB_EVENT_NAME": "pull_request",
                "GITHUB_REPOSITORY": "owner/repo",
                "GITHUB_WORKFLOW_REF": f"owner/repo/.github/workflows/harness.yml@{'a' * 40}"}

    def subprocess_env(self, project: Path, job: str, attempt: int, run_id: str) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GIT_", "GITHUB_", "HARNESS_")) and key != "CI"}
        env.update(self.ci_keys(job, attempt, run_id))
        env.update(GIT_ENV)
        env["GITHUB_WORKSPACE"] = str(project)
        env["RUNNER_TEMP"] = str(self.tmp / "runner-temp")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def run_ci_events(self, project: Path, args: list[str], env: dict):
        """真实产品入口（安装布局，直接脚本模式）：捕获结果并断言退出码。"""
        return subprocess.run([sys.executable, ".harness/engine/reports/ci_events.py", *args], cwd=project,
                              capture_output=True, text=True, env=env, check=False, timeout=120)

    def write_stub(self, project: Path) -> Path:
        path = self.tmp / "fake" / "fake_engine_cli.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_STUB.format(syspath=str(project / ".harness"), log=str(path.parent / "calls.log")),
                        encoding="utf-8")
        return path

    def upload_steps(self) -> dict[str, dict]:
        steps = {}
        for name, job_name in (("harness.yml", "harness"), ("auto-merge.yml", "judge")):
            found = [step for step in self.template(name)["jobs"][job_name]["steps"]
                     if "upload-artifact" in str(step.get("uses", ""))]
            self.assertEqual(len(found), 1, name)
            steps[name] = found[0]
        return steps

    @staticmethod
    def resolve_artifact_name(step: dict, job: str, run_id: str, attempt: int) -> str:
        context = {"github.run_id": run_id, "github.run_attempt": str(attempt), "github.job": job}
        return _EXPR.sub(lambda match: context[match[1].strip()], str(step["with"]["name"]))

    @staticmethod
    def export_target(step: dict) -> str:
        """export 步骤 --target 的参数（与 upload path 同一目录引用）。"""
        return str(step["run"]).split("--target", 1)[1].split('"')[1]

    def replay_context(self) -> dict[str, str]:
        return {"github.event.pull_request.base.sha": "", "github.event.before": "",
                "github.token": "fake-token", "runner.temp": "$RUNNER_TEMP"}

    # ---- 验收 1：解析模板结构并重放——verify 非零仍 export/upload/summary，原 job 仍失败 ----

    def test_export_runs_after_check_failure(self):
        project = self.fresh_project("replay-fail")
        self.install(project)
        template = self.template("harness.yml")
        self.assertEqual(list(template["jobs"]), ["harness"])  # 状态检查名保持
        steps = template["jobs"]["harness"]["steps"]
        names = [step.get("name") or step.get("uses") for step in steps]
        verify = steps[names.index("Verify")]
        self.assertIsNone(verify.get("continue-on-error"))  # 原失败不许被抹平
        for label in ("Export harness events", "Upload harness events", "Harness event summary"):
            step = steps[names.index(label)]
            self.assertEqual(step.get("if"), "${{ always() }}", label)
            self.assertTrue(step.get("continue-on-error"), label)  # 观察自身失败不改判定
            self.assertGreater(names.index(label), names.index("Verify"), label)
        stub = self.write_stub(project)

        # 失败重放：verify 退出 1，观察三步骤仍执行，job 结论仍是 failure
        env = self.subprocess_env(project, "harness", 1, "5100")
        replay = _Replay(project, env, self.replay_context())
        replay.env["FAKE_FAIL"] = "verify"
        replay.run(steps, stub=stub)
        self.assertEqual(replay.conclusion, "failure", replay.trace)
        self.assertEqual(replay.failed, ["Verify"], replay.trace)
        self.assertEqual(replay.swallowed, [])  # 失败重放里观察步骤自身全部成功
        for label in ("Export harness events", "Upload harness events", "Harness event summary"):
            self.assertIn(label, replay.ran, label)
        self.assertEqual(replay.uploads, [{"name": "harness-events-5100-1-harness",
                                           "path": EXPORT_TARGET, "retention-days": 90}])
        export_dir = self.tmp / "runner-temp" / "harness-events"
        bundle = json.loads((export_dir / "harness-events.json").read_text(encoding="utf-8"))
        self.assertTrue([event for event in bundle["events"]
                         if event["step"] == "cli.verify" and event["status"] == "fail"])
        self.assertTrue(bundle["chains"][0]["source"].startswith("ci:5100:1:harness"))
        text = replay.summary_path.read_text(encoding="utf-8")
        self.assertIn("harness 事件 summary", text)
        self.assertIn("verify/cli.verify fail", text)  # 从导出事件渲染
        self.assertIn("（fake evidence summary output）", text)  # 原有机器输出保留
        self.assertIn("（fake metrics summary output）", text)

        # 成功重放（attempt 2）：全部判定步骤照跑，job success，物理名带新 attempt
        env = self.subprocess_env(project, "harness", 2, "5100")
        replay = _Replay(project, env, self.replay_context())
        replay.run(steps, stub=stub)
        self.assertEqual((replay.conclusion, replay.failed), ("success", []))
        # 未声明 R1：只有变异步骤按条件跳过，其余全部照跑
        self.assertEqual(set(names) - set(replay.ran), {"Mutation score of changed targets (R1 claims)"})
        self.assertEqual(replay.uploads[0]["name"], "harness-events-5100-2-harness")
        bundle = json.loads((export_dir / "harness-events.json").read_text(encoding="utf-8"))
        self.assertEqual(len(bundle["events"]), 7)  # 七个引擎判定步骤各写一条检查事件

    # ---- 验收 2：上传集合精确等于安全 manifest；90 天；run/attempt/job 物理名唯一 ----

    def test_artifact_allowlist_retention_and_run_identity(self):
        project = self.fresh_project("allowlist")
        self.install(project)
        with mock.patch.dict(os.environ, self.ci_keys("harness", 1, "5200")), \
                mock.patch.object(events_db, "ROOT", project):
            safe_bytes = canonical({"checks": ["lint", "tests"]}).encode("utf-8")
            safe = events.store_artifact(safe_bytes)
            self.assertIsNotNone(events.emit("verify", "verify.summary", "ok",
                                             outputs={"log.sha256": safe["sha256"], "exit": 0}))
            raw = events.store_artifact(b"raw verify log\nnot json\n")
            self.assertIsNotNone(events.emit("verify", "verify.item", "fail",
                                             outputs={"raw.sha256": raw["sha256"]}))
            self.assertIsNotNone(events.emit("ci", "harness.export", "ok", outputs={"ghost.sha256": "e" * 64}))
        target = self.tmp / "export-5200"
        result = self.run_ci_events(project, ["export", "--target", str(target)],
                                    self.subprocess_env(project, "harness", 1, "5200"))
        self.assertEqual(result.returncode, 0, result.stderr)
        files = sorted(path.name for path in target.iterdir())
        bundle = json.loads((target / "harness-events.json").read_text(encoding="utf-8"))
        manifest = {entry["file"]: entry for entry in bundle["artifacts"]}
        self.assertEqual(set(files), {"harness-events.json", *manifest})  # 上传集合精确等于白名单
        self.assertEqual(list(manifest), [safe["sha256"]])
        self.assertEqual(manifest[safe["sha256"]],
                         {"sha256": safe["sha256"], "size": len(safe_bytes), "file": safe["sha256"]})
        self.assertEqual(sorted(finding["code"] for finding in bundle["findings"]),
                         ["artifact_missing", "artifact_unsafe"])  # 原始日志与悬空哈希被排除
        self.assertFalse([name for name in files if name.endswith((".db", ".db-wal", ".db-shm"))])
        for path in target.iterdir():
            self.assertNotIn(b"raw verify log", path.read_bytes())
        db = project / ".git/harness/harness.db"  # 临时库在 git 公共目录，不在上传集合
        self.assertTrue(db.exists())
        self.assertNotIn("harness.db", files)

        # 物理 artifact 名：两个模板各自的 job × run/attempt 任一不同都不同，形状与下载侧 _PACKAGE_RE 一致
        uploads = self.upload_steps()
        resolved = []
        for name, step in uploads.items():
            self.assertEqual(step["with"]["retention-days"], 90, name)
            job_name = "harness" if name == "harness.yml" else "judge"
            job_steps = self.template(name)["jobs"][job_name]["steps"]
            export = next(item for item in job_steps if item.get("name") == "Export harness events")
            # runner.temp 与 $RUNNER_TEMP 是同一目录：解析后导出目录与上传 path 必须同引用
            resolved_path = _EXPR.sub(lambda match: "$RUNNER_TEMP", str(step["with"]["path"]))
            self.assertEqual(resolved_path, EXPORT_TARGET, name)
            self.assertEqual(self.export_target(export), EXPORT_TARGET, name)
            for run_id, attempt in (("5100", 1), ("5100", 2), ("5101", 1)):
                resolved.append(self.resolve_artifact_name(step, job_name, run_id, attempt))
        self.assertEqual(len(set(resolved)), len(resolved))
        for name in resolved:
            self.assertIsNotNone(events_io._PACKAGE_RE.fullmatch(name), name)
            self.assertTrue(name.startswith("harness-events-"))

    # ---- 验收 3：失败/跳过/路由拒绝事件的确定性 summary，与原机器输出相符 ----

    def test_summary_renders_event_results(self):
        project = self.fresh_project("summary")
        self.install(project)
        with mock.patch.dict(os.environ, self.ci_keys("harness", 1, "5300")), \
                mock.patch.object(events_db, "ROOT", project):
            emitted = [
                events.emit("verify", "verify.lint", "ok", duration_ms=12,
                            decision={"by": "verify", "rule": "lint", "reason": "lint 通过"}),
                events.emit("verify", "verify.tests", "fail", duration_ms=40,
                            decision={"by": "verify", "rule": "tests", "reason": "2 个用例失败"}),
                events.emit("verify", "mutation", "skip",
                            decision={"by": "verify", "rule": "mutation", "reason": "未配置变异目标"}),
                events.emit("route", "route.result", "deny", source="ci:5300:1:judge",
                            decision={"by": "policy", "rule": "auto_merge", "reason": "误差预算耗尽"}),
            ]
        self.assertEqual([bool(event) for event in emitted], [True] * 4)
        harness_dir, judge_dir = self.tmp / "export-h", self.tmp / "export-j"
        for directory, job in ((harness_dir, "harness"), (judge_dir, "judge")):
            result = self.run_ci_events(project, ["export", "--target", str(directory)],
                                        self.subprocess_env(project, job, 1, "5300"))
            self.assertEqual(result.returncode, 0, result.stderr)
        for suffix in ("", "-wal", "-shm"):  # 摘掉库再渲染：summary 只读导出的 bundle 文件
            (project / ".git/harness" / f"harness.db{suffix}").unlink(missing_ok=True)
        env = self.subprocess_env(project, "harness", 1, "5300")
        first = self.run_ci_events(project, ["summary", "--bundle", str(harness_dir / "harness-events.json")], env)
        again = self.run_ci_events(project, ["summary", "--bundle", str(harness_dir / "harness-events.json")], env)
        judge = self.run_ci_events(project, ["summary", "--bundle", str(judge_dir / "harness-events.json")], env)
        self.assertEqual((first.returncode, judge.returncode), (0, 0), first.stderr + judge.stderr)
        self.assertEqual(first.stdout, again.stdout)  # 确定性：同一 bundle 逐字节相同
        text = first.stdout
        self.assertIn("harness 事件 summary", text)
        self.assertIn("- verify/verify.lint ok（12ms） · 依据 verify/lint：lint 通过", text)
        self.assertIn("- verify/verify.tests fail（40ms） · 依据 verify/tests：2 个用例失败", text)
        self.assertIn("- verify/mutation skip · 依据 verify/mutation：未配置变异目标", text)  # 跳过不漏
        self.assertIn("verify/route 事件 3 条", text)
        self.assertIn(f"链：ci:5300:1:harness / {BRANCH}", text)
        self.assertIn("- route/route.result deny · 依据 policy/auto_merge：误差预算耗尽", judge.stdout)
        # 与原机器输出相符：渲染的检查/状态与导出事件逐字一致
        bundle = json.loads((harness_dir / "harness-events.json").read_text(encoding="utf-8"))
        lines = [line[2:] for line in text.splitlines() if line.startswith("- ")]
        rendered = bundle["events"]
        for event in rendered:
            if event["stage"] in ("verify", "route"):
                self.assertTrue([line for line in lines
                                 if line.startswith(f"{event['stage']}/{event['step']} {event['status']}")],
                                event["step"])
        self.assertEqual(len(lines), len([event for event in rendered if event["stage"] in ("verify", "route")]))
        # 反例：bundle 内容变了 summary 就变（证明由导出事件驱动，不是独立假汇总）
        edited = json.loads((harness_dir / "harness-events.json").read_text(encoding="utf-8"))
        edited["events"] = [event for event in edited["events"] if event["status"] != "fail"]
        mutated = self.tmp / "mutated.json"
        mutated.write_text(json.dumps(edited, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                           encoding="utf-8")
        result = self.run_ci_events(project, ["summary", "--bundle", str(mutated)], env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("verify.tests", result.stdout)
        self.assertIn("verify/route 事件 2 条", result.stdout)
        result = self.run_ci_events(project, ["summary", "--bundle", str(mutated.parent / "nope.json")], env)
        self.assertEqual((result.returncode, result.stderr != ""), (2, True))

    # ---- 验收 4：临时 install 后 ci_events 可运行；judge 只用默认分支引擎，head 只当数据 ----

    def test_trusted_route_and_installed_layout(self):
        project = self.fresh_project("layout")
        self.install(project)
        env = self.subprocess_env(project, "harness", 1, "5400")
        # 直接脚本模式（安装布局）：python .harness/engine/reports/ci_events.py export
        target = self.tmp / "layout-export"
        result = self.run_ci_events(project, ["export", "--target", str(target)], env)
        self.assertEqual((result.returncode, (target / "harness-events.json").exists()), (0, True), result.stderr)
        # python -m 模式（cwd=.harness，安装布局）
        module = subprocess.run([sys.executable, "-m", "engine.reports.ci_events",
                                 "export", "--target", str(self.tmp / "layout-m")],
                                cwd=project / ".harness", capture_output=True, text=True, env=env,
                                check=False, timeout=120)
        self.assertEqual(module.returncode, 0, module.stderr)
        self.assertTrue((self.tmp / "layout-m" / "harness-events.json").exists())
        self.assertNotIn("ci_events", cli.COMMANDS)  # 不新增 CLI 注册

        # 可信路由：judge 检出的是默认分支；head 只被 fetch 当数据；python 一律来自默认分支的 .harness/engine
        judge = self.template("auto-merge.yml")["jobs"]["judge"]
        steps = judge["steps"]
        self.assertEqual(steps[0]["with"]["ref"], "${{ github.event.repository.default_branch }}")
        fetch = next(step for step in steps if step.get("name", "").startswith("Fetch evaluated head"))
        self.assertEqual(fetch["run"].strip(), 'git fetch --no-tags origin "$HEAD_SHA"')
        for step in steps:
            run = str(step.get("run", ""))
            self.assertNotIn("git checkout", run)  # 不检出、更不执行 PR 代码
            self.assertNotIn("git switch", run)
            for line in run.splitlines():
                if line.strip().startswith("python"):
                    self.assertTrue(line.strip().startswith("python .harness/engine/"), line)
        # head_sha 只作为 env 数据输入进入 fetch 与 policy，不进入任何 with（检出 ref）
        appearances = [(step.get("name"), key) for step in steps
                       for key, value in (step.get("env") or {}).items()
                       if "workflow_run.head_sha" in str(value)]
        self.assertEqual(len(appearances), 2, appearances)
        self.assertEqual([key for _, key in appearances], ["HEAD_SHA", "HEAD_SHA"])
        for step in steps:
            self.assertFalse([value for value in (step.get("with") or {}).values()
                              if "head_sha" in str(value).lower()], step.get("name"))
        # trace 对齐：harness 的事件 trace（GITHUB_HEAD_REF）与 route 的 --branch 同源
        policy = next(step for step in steps if step.get("id") == "policy")
        self.assertEqual(policy["env"]["HEAD_BRANCH"], "${{ github.event.workflow_run.head_branch }}")
        self.assertIn('--branch "$HEAD_BRANCH"', policy["run"])
        harness_steps = self.template("harness.yml")["jobs"]["harness"]["steps"]
        self.assertFalse([step for step in harness_steps if "GITHUB_HEAD_REF" in (step.get("env") or {})])

    # ---- 步骤 2：失败隔离与范围——export 保持只读；事件关闭与导出自身失败不改 job 结论 ----

    def test_export_scope_and_failure_isolation(self):
        project = self.fresh_project("scope")
        self.install(project)
        env = self.subprocess_env(project, "harness", 1, "5500")
        # 无事件：导出空包成功，且不创建事件库（导出保持只读，读不建库）
        target = self.tmp / "scope-export"
        result = self.run_ci_events(project, ["export", "--target", str(target)], env)
        self.assertEqual(result.returncode, 0, result.stderr)
        bundle = json.loads((target / "harness-events.json").read_text(encoding="utf-8"))
        self.assertEqual((bundle["events"], bundle["anchors"], bundle["chains"]), ([], [], []))
        self.assertFalse((project / ".git/harness/harness.db").exists())
        # 事件关闭：整条观察链路静默，导出仍成功且无库
        checks = project / ".harness/config/checks.toml"
        checks.write_text(checks.read_text(encoding="utf-8") + "\n[events]\nenabled = false\n", encoding="utf-8")
        target = self.tmp / "scope-off"
        result = self.run_ci_events(project, ["export", "--target", str(target)], env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((target / "harness-events.json").read_text(
            encoding="utf-8"))["events"], [])
        self.assertFalse((project / ".git/harness/harness.db").exists())
        # summary 对坏输入明确返回 2：文件缺失、非法 JSON 与非对象形状都不当成功
        for content in (None, "不是 JSON", '[{"events": []}]'):
            bad = self.tmp / "bad.json"
            if content is not None:
                bad.write_text(content, encoding="utf-8")
            result = self.run_ci_events(project, ["summary", "--bundle", str(bad)], env)
            self.assertEqual((result.returncode, result.stderr != ""), (2, True), content)

        # 重放：导出自身失败（RUNNER_TEMP 指向文件之下，目录建不出来）不改 job 结论，
        # upload/summary 照常执行——观察失败被 continue-on-error 隔离（共用合同 C0）
        blocked = self.tmp / "blocked-file"
        blocked.write_text("not a dir\n", encoding="utf-8")
        env = self.subprocess_env(project, "harness", 2, "5600")
        env["RUNNER_TEMP"] = str(blocked / "runner-temp")
        replay = _Replay(project, env, self.replay_context())
        replay.run(self.template("harness.yml")["jobs"]["harness"]["steps"], stub=self.write_stub(project))
        self.assertEqual((replay.conclusion, replay.failed), ("success", []), replay.trace)
        self.assertEqual(replay.swallowed, ["Export harness events", "Harness event summary"], replay.trace)
        self.assertIn("Upload harness events", replay.ran)
        self.assertEqual(replay.uploads[0]["name"], "harness-events-5600-2-harness")


if __name__ == "__main__":
    unittest.main()
