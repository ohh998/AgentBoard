"""Loopback HTTP API. No execution of user-provided commands or Markdown."""
from __future__ import annotations

from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import mimetypes
from pathlib import Path
import re
import secrets
import threading
import time
import sys
from urllib.parse import unquote, urlsplit

from .store import ApiError


WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
MAX_BODY = 256 * 1024


def is_loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


class BoardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, address, store):
        self.store = store
        self.sessions = {}
        self.session_lock = threading.Lock()
        self.worker_slots = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        self.worker_slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.worker_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.worker_slots.release()


def make_server(host, port, store):
    if not is_loopback(host):
        raise ValueError("服务仅支持本地环回地址（127.0.0.1 / localhost）")
    if ":" in host:
        import socket
        class IPv6Server(BoardServer):
            address_family = socket.AF_INET6
        return IPv6Server((host, port), store)
    return BoardServer((host, port), store)


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentBoard/1"

    def log_message(self, fmt, *args):
        # Do not log URL query parameters, bearer tokens, cookies, or bodies.
        pass

    def _send(self, status, payload=None, headers=None, raw=None, content_type=None):
        body = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type or "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _guard(self):
        host = self.headers.get("Host", "")
        try:
            parsed = urlsplit("http://" + host)
            port = parsed.port or 80
            hostname = parsed.hostname
            if parsed.username or parsed.password or parsed.path or not hostname or not is_loopback(hostname) or port != self.server.server_port:
                raise ValueError()
        except ValueError:
            raise ApiError(403, "invalid_host", "Host 必须是本机服务地址")
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{host}":
            raise ApiError(403, "cross_origin", "不允许跨站请求，请直接访问本地协作台")
        if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            raise ApiError(403, "cross_origin", "不允许跨站请求")

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(400, "invalid_body", "不支持 Transfer-Encoding")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "invalid_body", "无效的 Content-Length")
        if not 0 <= length <= MAX_BODY:
            raise ApiError(413, "body_too_large", "请求正文超过 256KB")
        if self.headers.get_content_type() != "application/json":
            raise ApiError(415, "json_required", "请使用 application/json")
        try:
            result = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            raise ApiError(400, "invalid_json", "JSON 格式不正确")
        if not isinstance(result, dict):
            raise ApiError(400, "invalid_json", "JSON 正文必须是对象")
        return result

    def _auth(self):
        authorization = self.headers.get("Authorization", "")
        if authorization:
            if not authorization.startswith("Bearer "):
                raise ApiError(401, "unauthorized", "请使用 Bearer 认证")
            return self.server.store.authenticate(authorization[7:])
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            session = cookie.get("agentboard_session")
        except Exception:
            session = None
        if session:
            with self.server.session_lock:
                expires = self.server.sessions.get(session.value, 0)
                if expires > time.time():
                    # Cookie mutations require a genuine same-origin browser Origin.
                    if self.command not in ("GET", "HEAD") and self.headers.get("Origin") != f"http://{self.headers.get('Host')}":
                        raise ApiError(403, "origin_required", "网页写入需要同源 Origin")
                    return {"kind": "admin", "session": session.value}
        raise ApiError(401, "unauthorized", "请登录本地协作台或提供 agent 令牌")

    def _require(self, auth, kinds):
        if auth["kind"] not in kinds:
            raise ApiError(403, "forbidden", "该令牌没有此操作权限")

    def _static(self, path):
        path = unquote(path)
        if path == "/":
            path = "/index.html"
        # Static file allowlist prevents runtime data/source disclosure.
        if path not in ("/index.html", "/app.js", "/styles.css", "/favicon.svg"):
            raise ApiError(404, "not_found", "页面不存在")
        target = WEB_ROOT / path[1:]
        if not target.is_file():
            raise ApiError(404, "not_found", "文件不存在")
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, raw=target.read_bytes(), content_type=mime + ("; charset=utf-8" if mime.startswith("text/") or mime.endswith("javascript") else ""))

    def _dispatch(self):
        self._guard()
        path = urlsplit(self.path).path
        method = self.command
        store = self.server.store
        if method == "GET" and path == "/api/health":
            return self._send(200, {"ok": True, "protocol_version": "1"})
        if method == "GET" and not path.startswith("/api/"):
            return self._static(path)
        if method == "POST" and path == "/api/session":
            body = self._body()
            auth = store.authenticate(body.get("token", ""))
            self._require(auth, ("admin",))
            session = secrets.token_urlsafe(32)
            with self.server.session_lock:
                self.server.sessions = {k: v for k, v in self.server.sessions.items() if v > time.time()}
                self.server.sessions[session] = time.time() + 12 * 3600
            return self._send(200, {"ok": True}, {"Set-Cookie": f"agentboard_session={session}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200"})
        auth = self._auth()
        if method == "DELETE" and path == "/api/session":
            if auth.get("session"):
                with self.server.session_lock:
                    self.server.sessions.pop(auth["session"], None)
            return self._send(200, {"ok": True}, {"Set-Cookie": "agentboard_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
        if method == "GET" and path == "/api/state":
            etag, body = store.snapshot()
            if self.headers.get("If-None-Match") == etag:
                return self._send(304, raw=b"", headers={"ETag": etag})
            return self._send(200, raw=body, headers={"ETag": etag})
        matched = re.fullmatch(r"/api/tasks/([a-zA-Z0-9_-]+)", path)
        if method == "GET" and matched:
            return self._send(200, store.task(matched[1]))
        if method != "POST":
            raise ApiError(404, "not_found", "API 不存在")
        body = self._body()
        if path == "/api/agents/register":
            self._require(auth, ("join", "admin"))
            return self._send(201, store.register(body))
        if path == "/api/tasks":
            self._require(auth, ("admin", "agent"))
            return self._send(201, store.create_task(body, auth.get("agent_id")))
        if path == "/api/markdown/sync":
            self._require(auth, ("admin",))
            return self._send(200, store.sync_markdown())
        matched = re.fullmatch(r"/api/agents/([a-zA-Z0-9_-]+)/heartbeat", path)
        if matched:
            if auth["kind"] != "agent" or auth["agent_id"] != matched[1]:
                raise ApiError(403, "forbidden", "只能更新自己的 agent 状态")
            return self._send(200, store.heartbeat(matched[1], body))
        matched = re.fullmatch(r"/api/tasks/([a-zA-Z0-9_-]+)/(claim|progress|release|block|submit|edit|reopen|approve|cancel)", path)
        if matched:
            ident, action = matched.groups()
            if action == "claim":
                self._require(auth, ("agent",))
                result = store.claim(ident, auth["agent_id"], body)
            elif action == "edit":
                self._require(auth, ("admin",))
                result = store.edit(ident, body)
            else:
                if action in ("progress", "release", "block", "submit"):
                    self._require(auth, ("agent",))
                else:
                    self._require(auth, ("admin",))
                result = store.transition(ident, action, body, auth.get("agent_id"), auth["kind"] == "admin")
            return self._send(200, result)
        raise ApiError(404, "not_found", "API 不存在")

    def _handle(self):
        self.connection.settimeout(15)
        try:
            self._dispatch()
        except ApiError as exc:
            error = {"code": exc.code, "message": exc.message}
            if exc.details is not None:
                error["details"] = exc.details
            self._send(exc.status, {"error": error})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as exc:
            print("Agent Board internal_error type=" + type(exc).__name__, file=sys.stderr, flush=True)
            self._send(500, {"error": {"code": "internal_error", "message": "服务处理失败，请查看本地数据目录与维护文档"}})

    do_GET = _handle
    do_POST = _handle
    do_DELETE = _handle
