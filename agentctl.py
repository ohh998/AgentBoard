#!/usr/bin/env python3
"""Agent Board 的跨客户端 CLI；仅依赖 Python 3.11+ 标准库。"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any
from urllib import error, parse, request
import uuid


DEFAULT_URL = "http://127.0.0.1:8765"
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)
}


class CliError(Exception):
    def __init__(self, message: str, *, code: str = "cli_error", status: int = 0,
                 details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextmanager
def profile_lock(path: Path):
    """OS advisory lock 会随崩溃释放，避免多个 CLI 覆盖同一 profile。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + 20
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise CliError("同一 profile 正被其他 CLI 使用，请稍后重试。", code="profile_busy")
                time.sleep(0.1)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def load_profile(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"claims": {}, "revisions": {}, "pending_claims": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CliError(f"无法读取 profile：{path}；请检查文件，勿直接删除仍在用的凭据。") from exc
    if not isinstance(data, dict) or any(
        not isinstance(data.setdefault(key, {}), dict)
        for key in ("claims", "revisions", "pending_claims")
    ):
        raise CliError(f"profile 格式无效：{path}")
    if data.get("token") and (not isinstance(data.get("token"), str)
            or not isinstance(data.get("agent"), dict)
            or not isinstance(data["agent"].get("id"), str)):
        raise CliError(f"profile 身份格式无效：{path}")
    if any(not isinstance(claim, dict) or not isinstance(claim.get("claim_token"), str)
           for claim in data["claims"].values()):
        raise CliError(f"profile 认领格式无效：{path}")
    return data


def normalize_url(value: str) -> str:
    url = parse.urlsplit(value)
    local = url.hostname == "localhost"
    if not local:
        try:
            local = ipaddress.ip_address(url.hostname or "").is_loopback
        except ValueError:
            local = False
    if (url.scheme not in ("http", "https") or not local or url.username or url.password
            or url.query or url.fragment or url.path not in ("", "/")):
        raise CliError("--url 必须是环回 HTTP(S) 地址，例如 http://127.0.0.1:8765。")
    try:
        url.port
    except ValueError as exc:
        raise CliError("--url 端口无效。") from exc
    return value.rstrip("/")


def token_from_file(path: Path, environment: str) -> str:
    token = os.environ.get(environment)
    if token:
        return token.strip()
    try:
        token = path.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        raise CliError(f"无法读取令牌文件 {path}；先启动服务，或设置 {environment}。") from exc
    if not token:
        raise CliError(f"令牌文件为空：{path}")
    return token


class Client:
    def __init__(self, url: str, token: str, timeout: float) -> None:
        self.url, self.token, self.timeout = url, token, timeout

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.token}"}
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(self.url + path, data=payload, headers=headers, method=method)
        # 本地调用不经过 HTTP_PROXY，且不跟随重定向，避免转发 Bearer 凭据。
        class NoRedirect(request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(req, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            try:
                data = json.loads(exc.read().decode("utf-8"))
                problem = data.get("error", {})
                if not isinstance(problem, dict):
                    problem = {}
            except (ValueError, UnicodeError):
                problem = {}
            raise CliError(problem.get("message", f"HTTP {exc.code}"),
                           code=problem.get("code", "http_error"), status=exc.code,
                           details=problem.get("details")) from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise CliError("连接服务失败或请求超时；确认服务地址及运行状态。写请求可能已生效，先查看任务状态再决定重试。",
                           code="connection_error") from exc
        except (ValueError, UnicodeError) as exc:
            raise CliError("服务返回了无效 JSON。", code="invalid_response") from exc
        if not isinstance(data, dict):
            raise CliError("服务返回了无效对象。", code="invalid_response")
        return data


def context_value(args: argparse.Namespace, *, default: bool = False) -> dict[str, Any] | None:
    used, maximum, source, note = (
        args.context_used, args.context_max, args.context_source, args.context_note
    )
    if all(value is None for value in (used, maximum, source, note)):
        return {"used_tokens": None, "max_tokens": None, "source": "unknown"} if default else None
    if source is None:
        raise CliError("提供上下文指标时请用 --context-source 明确标注 unknown、reported 或 estimated。")
    if used is not None and used < 0:
        raise CliError("--context-used 必须是非负整数。")
    if maximum is not None and maximum <= 0:
        raise CliError("--context-max 必须是正整数。")
    if source == "unknown" and used is not None:
        raise CliError("unknown 不应携带已用 token 数；已知或估计数值请明确来源。")
    result: dict[str, Any] = {"used_tokens": used, "max_tokens": maximum, "source": source}
    if note is not None:
        result["note"] = note
    return result


def read_markdown(path: str) -> tuple[dict[str, Any], str]:
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise CliError(f"无法读取 Markdown 文件：{path}") from exc
    lines = raw.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, raw
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
        metadata = json.loads("".join(lines[1:end]))
    except (StopIteration, ValueError) as exc:
        raise CliError("Markdown front matter 必须是 --- 包围的 JSON 对象；参见 examples/task-template.md。") from exc
    if not isinstance(metadata, dict):
        raise CliError("Markdown front matter 必须是 JSON 对象。")
    return metadata, "".join(lines[end + 1:]).lstrip("\r\n")


def task_body(args: argparse.Namespace) -> dict[str, Any]:
    metadata, description = read_markdown(args.file) if args.file else ({}, args.description or "")
    if metadata.get("id") or metadata.get("revision", 0) != 0 or metadata.get("status", "todo") != "todo":
        raise CliError("create 用于新任务；模板应不带 id、revision 为 0、status 为 todo。现有任务的 Markdown 修改请按维护流程 sync。")
    title = args.title or metadata.get("title")
    if not title:
        title = next((line[2:].strip() for line in description.splitlines() if line.startswith("# ")), None)
    if not title:
        raise CliError("请指定 --title，或在 Markdown front matter / 一级标题中提供任务标题。")
    result = {"title": title, "description": description}
    for field, override, default in (
        ("priority", args.priority, 1), ("resources", args.resource, []),
        ("depends_on", args.depends_on, []), ("required_capabilities", args.capability, []),
    ):
        result[field] = override if override is not None else metadata.get(field, default)
    return result


def safe_output(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: safe_output(item) for key, item in value.items()
                if key.lower() not in {"token", "claim_token", "authorization", "join_token", "admin_token"}}
    if isinstance(value, list):
        return [safe_output(item) for item in value]
    return value


def emit(value: Any, as_json: bool, command: str) -> None:
    value = safe_output(value)
    if as_json or command not in ("tasks", "status", "register"):
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif command == "register":
        agent = value["agent"]
        print(f"已登记 {agent['name']} ({agent['id']})；凭据已保存到本地 profile。")
    elif command == "status":
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        tasks = value["tasks"]
        if not tasks:
            print("暂无匹配任务。")
        for task in tasks:
            print(f"{task['id']}  {task['status']:10}  P{task['priority']}  r{task['revision']}  "
                  f"{task['title']}  owner={task.get('owner_id') or '-'}")


def remember(profile: dict[str, Any], task: dict[str, Any]) -> None:
    profile["revisions"][task["id"]] = task["revision"]
    if (task.get("status") != "in_progress"
            or task.get("owner_id") != profile.get("agent", {}).get("id")):
        profile["claims"].pop(task["id"], None)
    elif task["id"] in profile["claims"]:
        profile["claims"][task["id"]]["revision"] = task["revision"]


def expected_revision(args: argparse.Namespace, profile: dict[str, Any], client: Client,
                      path: Path) -> int:
    if args.expected_revision is not None:
        if args.expected_revision < 0:
            raise CliError("--expected-revision 必须是非负整数。")
        return args.expected_revision
    if args.task_id not in profile["revisions"]:
        remember(profile, client.call("GET", "/api/tasks/" + parse.quote(args.task_id, safe=""))["task"])
        atomic_json(path, profile)
    return profile["revisions"][args.task_id]


def execute(args: argparse.Namespace, profile_path: Path) -> dict[str, Any]:
    with profile_lock(profile_path):
        profile = load_profile(profile_path)
        url = normalize_url(args.url or profile.get("url") or DEFAULT_URL)
        if args.command != "register" and profile.get("url") and url != profile["url"]:
            raise CliError("指定地址与 profile 不一致；请为另一服务使用独立 --profile，防止误用凭据。")
        if args.command == "register":
            if profile.get("token"):
                raise CliError("此 profile 已登记；请复用它，或使用新的 --profile。勿覆盖仍有任务的身份。")
            token = token_from_file(Path(args.join_token_file) if args.join_token_file
                                   else Path(args.data_dir) / "join.token", "AGENTBOARD_JOIN_TOKEN")
        elif args.command == "sync":
            token = token_from_file(Path(args.admin_token_file) if args.admin_token_file
                                   else Path(args.data_dir) / "admin.token", "AGENTBOARD_ADMIN_TOKEN")
        else:
            token = profile.get("token")
            if not token:
                raise CliError("此 profile 尚未登记，请先运行 register --name ... --client ... --model ...。")
        client = Client(url, token, args.timeout)

        if args.command == "register":
            response = client.call("POST", "/api/agents/register", {
                "name": args.name, "client": args.client, "model": args.model,
                "capabilities": args.capability or [], "context": context_value(args, default=True),
            })
            profile.update({"protocol_version": "1", "url": url,
                            "agent": response["agent"], "token": response["token"]})
        elif args.command in ("status", "tasks"):
            state = client.call("GET", "/api/state")
            for task in state["tasks"]:
                remember(profile, task)
            if args.command == "status":
                agent_id = profile.get("agent", {}).get("id")
                response = {"url": url, "server_time": state["server_time"],
                            "lease_seconds": state["lease_seconds"], "stats": state["stats"],
                            "agent": next((agent for agent in state["agents"] if agent["id"] == agent_id), None),
                            "local_claims": list(profile["claims"]), "markdown": state["markdown"]}
            else:
                tasks = state["tasks"]
                if args.status:
                    tasks = [task for task in tasks if task["status"] == args.status]
                if args.mine:
                    agent_id = profile.get("agent", {}).get("id")
                    tasks = [task for task in tasks if task.get("owner_id") == agent_id]
                response = {"tasks": tasks}
        elif args.command == "show":
            response = client.call("GET", "/api/tasks/" + parse.quote(args.task_id, safe=""))
            remember(profile, response["task"])
        elif args.command == "create":
            response = client.call("POST", "/api/tasks", task_body(args))
            remember(profile, response["task"])
        elif args.command == "sync":
            response = client.call("POST", "/api/markdown/sync", {})
        elif args.command == "heartbeat":
            body: dict[str, Any] = {}
            for key in ("summary", "status"):
                if getattr(args, key) is not None:
                    body[key] = getattr(args, key)
            context = context_value(args)
            if context is not None:
                body["context"] = context
            task_id = args.task_id
            if not task_id and len(profile["claims"]) == 1:
                task_id = next(iter(profile["claims"]))
            if task_id:
                claim = profile["claims"].get(task_id)
                if not claim:
                    raise CliError("本 profile 没有该任务的认领凭据；不要复制别人的 claim token。")
                body.update(task_id=task_id, claim_token=claim["claim_token"])
            agent_id = profile.get("agent", {}).get("id")
            try:
                response = client.call("POST", f"/api/agents/{parse.quote(agent_id, safe='')}/heartbeat", body)
            except CliError as exc:
                if task_id and exc.code == "ownership_lost":
                    profile["claims"].pop(task_id, None)
                    atomic_json(profile_path, profile)
                raise
            profile["agent"] = response["agent"]
            if response.get("task"):
                remember(profile, response["task"])
        else:
            revision = expected_revision(args, profile, client, profile_path)
            task_path = "/api/tasks/" + parse.quote(args.task_id, safe="") + "/" + args.command
            body = {"expected_revision": revision}
            if args.command == "claim":
                pending = profile["pending_claims"].get(args.task_id)
                if pending and args.request_id is None and args.expected_revision is None:
                    body.update(pending)
                else:
                    body["request_id"] = args.request_id or str(uuid.uuid4())
                profile["pending_claims"][args.task_id] = dict(body)
                atomic_json(profile_path, profile)
                try:
                    response = client.call("POST", task_path, body)
                except CliError as exc:
                    if 400 <= exc.status < 500:
                        profile["pending_claims"].pop(args.task_id, None)
                        atomic_json(profile_path, profile)
                    raise
                # 服务保证同 agent 只有一份活跃认领；成功后移除历史失效项。
                profile["claims"] = {args.task_id: {"claim_token": response["claim_token"],
                                                   "revision": response["task"]["revision"]}}
                profile["pending_claims"].pop(args.task_id, None)
            else:
                claim = profile["claims"].get(args.task_id)
                if not claim:
                    raise CliError("本 profile 没有该任务的认领凭据；先 claim，并保留同一 profile。")
                body["claim_token"] = claim["claim_token"]
                if args.command == "progress":
                    body["summary"] = args.summary
                    if args.progress is not None:
                        if not 0 <= args.progress <= 100:
                            raise CliError("--progress 必须在 0..100 之间。")
                        body["progress"] = args.progress
                    context = context_value(args)
                    if context is not None:
                        body["context"] = context
                elif args.command == "release":
                    if args.summary is not None:
                        body["summary"] = args.summary
                elif args.command == "block":
                    body["reason"] = args.reason
                elif args.command == "submit":
                    body["result"] = read_markdown(args.file)[1] if args.file else args.result
                try:
                    response = client.call("POST", task_path, body)
                except CliError as exc:
                    if exc.code == "ownership_lost":
                        profile["claims"].pop(args.task_id, None)
                        atomic_json(profile_path, profile)
                    raise
                if args.command in ("release", "block", "submit"):
                    profile["claims"].pop(args.task_id, None)
            remember(profile, response["task"])
        atomic_json(profile_path, profile)
        return response


def shared_flags(parser: argparse.ArgumentParser, *, root: bool = False) -> None:
    default = None if root else argparse.SUPPRESS
    parser.add_argument("--url", default=default, help="服务地址，默认 profile 中的地址或 http://127.0.0.1:8765")
    parser.add_argument("--profile", default="default" if root else argparse.SUPPRESS,
                        help="独立 agent 身份名，默认 default；每个并行 agent 必须不同")
    parser.add_argument("--data-dir", default=".agentboard" if root else argparse.SUPPRESS,
                        help="本地凭据及默认令牌目录，默认 .agentboard")
    parser.add_argument("--json", action="store_true", default=False if root else argparse.SUPPRESS,
                        help="输出 JSON，不输出身份/认领令牌")
    parser.add_argument("--timeout", type=float, default=15.0 if root else argparse.SUPPRESS,
                        help="HTTP 请求超时秒数，默认 15")


def context_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--context-used", type=int, help="当前上下文已用 token 数；未知则省略")
    parser.add_argument("--context-max", type=int, help="本次模型/客户端实际上下文上限；未知则省略")
    parser.add_argument("--context-source", choices=("unknown", "reported", "estimated"))
    parser.add_argument("--context-note", help="指标来源或估算方法的简短说明，勿填完整对话")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent Board：跨客户端的本地 Markdown 协作 CLI")
    shared_flags(parser, root=True)
    commands = parser.add_subparsers(dest="command", required=True)
    def sub(name: str, help_text: str) -> argparse.ArgumentParser:
        child = commands.add_parser(name, help=help_text)
        shared_flags(child)
        return child
    register = sub("register", "登记独立 agent，安全保存访问凭据")
    register.add_argument("--name", required=True)
    register.add_argument("--client", required=True, help="客户端名称或版本，例如 Codex、Claude Code、脚本")
    register.add_argument("--model", required=True, help="实际模型；无法确认可填 unknown")
    register.add_argument("--capability", action="append", help="可重复的能力标签，与任务要求精确匹配")
    register.add_argument("--join-token-file", help="注册令牌文件；默认读取 data-dir/join.token")
    context_flags(register)
    sub("status", "查看自己的状态、租约配置、看板统计及 Markdown 冲突")
    tasks = sub("tasks", "列出任务并刷新本地 revision")
    tasks.add_argument("--status", choices=("todo", "in_progress", "blocked", "review", "done", "cancelled"))
    tasks.add_argument("--mine", action="store_true")
    show = sub("show", "查看任务详情并刷新本地 revision")
    show.add_argument("task_id")
    create = sub("create", "用 Markdown 文件或参数创建任务")
    create.add_argument("--title")
    contents = create.add_mutually_exclusive_group()
    contents.add_argument("--description")
    contents.add_argument("--file", help="UTF-8 Markdown 文件，- 表示 stdin")
    create.add_argument("--priority", type=int, choices=range(4))
    create.add_argument("--resource", action="append", help="工作区相对文件或以 / 结尾的目录，可重复")
    create.add_argument("--depends-on", action="append", help="依赖任务 ID，可重复")
    create.add_argument("--capability", action="append", help="要求的能力标签，可重复")
    claim = sub("claim", "原子认领任务；冲突时不自动抢占")
    claim.add_argument("task_id")
    claim.add_argument("--expected-revision", type=int)
    claim.add_argument("--request-id", help="高级用法：认领请求幂等键；默认自动生成和持久化")
    heartbeat = sub("heartbeat", "报告状态、上下文，并为本 profile 的任务续租")
    heartbeat.add_argument("--task", dest="task_id", help="任务 ID；默认自动使用唯一的本地认领")
    heartbeat.add_argument("--summary")
    heartbeat.add_argument("--status", choices=("idle", "working", "blocked"))
    heartbeat.add_argument("--watch", action="store_true", help="持续发送心跳，Ctrl+C 退出；停止后租约会过期")
    heartbeat.add_argument("--interval", type=float, default=30, help="watch 间隔秒数，默认 30，应显著小于服务租约")
    context_flags(heartbeat)
    for name, description in (("progress", "更新进度、摘要及上下文并续租"),
                              ("release", "主动释放任务并回到 todo"),
                              ("block", "报告阻塞并释放任务资源"),
                              ("submit", "提交成果进入 review，等待管理员审核")):
        action = sub(name, description)
        action.add_argument("task_id")
        action.add_argument("--expected-revision", type=int)
        if name == "progress":
            action.add_argument("--summary", required=True)
            action.add_argument("--progress", type=int)
            context_flags(action)
        elif name == "release":
            action.add_argument("--summary")
        elif name == "block":
            action.add_argument("--reason", required=True)
        else:
            result = action.add_mutually_exclusive_group(required=True)
            result.add_argument("--result", help="成果、验证方式及交接信息")
            result.add_argument("--file", help="UTF-8 成果说明 Markdown 文件，- 表示 stdin")
    sync = sub("sync", "管理员同步离线 Markdown；请先阅读维护窗口规则")
    sync.add_argument("--admin-token-file", help="默认读取 data-dir/admin.token")
    return parser


def main(argv: list[str] | None = None) -> int:
    # 管道与 Windows 控制台统一 UTF-8，避免中文 JSON 被按系统代码页编码。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = make_parser().parse_args(argv)
    try:
        if not PROFILE_RE.fullmatch(args.profile) or args.profile.upper() in RESERVED_NAMES:
            raise CliError("--profile 仅允许 1..64 位英文字母、数字、下划线、连字符，且不能为系统保留名称。")
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise CliError("--timeout 必须大于 0。")
        if args.command == "heartbeat" and (not math.isfinite(args.interval)
                                             or args.interval <= 0 or args.interval > 3600):
            raise CliError("--interval 必须在 0..3600 秒之间，且应显著小于服务租约。")
        profile_path = Path(args.data_dir).resolve() / "credentials" / f"{args.profile}.json"
        while True:
            response = execute(args, profile_path)
            emit(response, args.json, args.command)
            if args.command != "heartbeat" or not args.watch:
                return 0
            sys.stdout.flush()
            time.sleep(args.interval)
    except CliError as exc:
        problem: dict[str, Any] = {"code": exc.code, "message": str(exc)}
        if exc.status:
            problem["http_status"] = exc.status
        if exc.details is not None:
            problem["details"] = safe_output(exc.details)
        if exc.status == 409:
            problem["hint"] = "先 show 任务核对所有者、租约和 revision；确认仍获授权后再重试。不要直接改 Markdown 状态。"
        if args.json:
            print(json.dumps({"error": problem}, ensure_ascii=False, indent=2), file=sys.stderr)
        else:
            print(f"错误 [{exc.code}]：{exc}", file=sys.stderr)
            if "hint" in problem:
                print(problem["hint"], file=sys.stderr)
        return 3 if exc.status == 409 else 1
    except (OSError, UnicodeError) as exc:
        print(f"本地文件或控制台操作失败：{type(exc).__name__}；请检查路径、权限与 UTF-8 编码。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已停止；仍持有的任务请继续心跳、主动 release，或等待租约到期回收。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
