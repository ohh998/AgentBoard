"""Transactional coordinator and recoverable Markdown projection (stdlib only)."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone


class ApiError(Exception):
    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def epoch(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else 0


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def fail(code, message, status=409, details=None):
    raise ApiError(status, code, message, details)


def string(value, name, maximum=10000, required=False):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        fail("invalid_input", f"{name} 必须是{'非空' if required else ''}字符串，长度不超过 {maximum}", 400)
    return value.strip() if name != "description" else value


def strings(value, name, maximum=50):
    if not isinstance(value, list) or len(value) > maximum:
        fail("invalid_input", f"{name} 必须是最多 {maximum} 项的数组", 400)
    return list(dict.fromkeys(string(v, name, 200, True) for v in value))


def integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        fail("invalid_input", f"{name} 必须是 {low} 到 {high} 的整数", 400)
    return value


def context(value):
    if not isinstance(value, dict):
        fail("invalid_input", "context 必须是对象", 400)
    used, capacity = value.get("used_tokens"), value.get("max_tokens")
    if used is not None:
        integer(used, "used_tokens", 0, 10**10)
    if capacity is not None:
        integer(capacity, "max_tokens", 1, 10**10)
    source = value.get("source", "unknown")
    if source not in ("unknown", "reported", "estimated"):
        fail("invalid_input", "无效的上下文来源", 400)
    if source == "unknown":
        used = None
    return {"used_tokens": used, "max_tokens": capacity, "source": source,
            "note": string(value.get("note", ""), "note", 500), "updated_at": now_iso()}


def resource(value):
    value = string(value, "resource", 200, True).replace("\\", "/")
    if value.startswith("/") or re.search(r"[:*?\x00-\x1f]", value) or any(p in ("", ".", "..") for p in value.rstrip("/").split("/")):
        fail("invalid_resource", "资源必须是工作区相对路径，不能含 ..、绝对路径或通配符", 400)
    return str(PurePosixPath(value)).casefold()


def overlap(left, right):
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


class Store:
    def __init__(self, data_dir, workspace=None, lease_seconds=120):
        if not isinstance(lease_seconds, (int, float)) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.data_dir = Path(data_dir).resolve()
        self.workspace = Path(workspace or ".").resolve()
        self.lease_seconds = lease_seconds
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conflicts = []
        self.import_conflicts = []
        self.file_conflicts = {}
        self.file_cache = {}
        self.next_markdown_scan = 0
        self.next_expiry = 0
        self.state_bytes = None
        self.state_etag = None
        self.state_deadline = 0
        self.last_export_error = None
        self.instance_file = None
        self._instance_lock()
        self.db_path = self.data_dir / "board.sqlite3"
        try:
            with self._connect() as conn:
                conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, data TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, type TEXT NOT NULL, task_id TEXT, agent_id TEXT, message TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS documents (path TEXT PRIMARY KEY, kind TEXT NOT NULL, entity_id TEXT, hash TEXT, pending INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS requests (agent_id TEXT NOT NULL, request_id TEXT NOT NULL, task_id TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(agent_id,request_id));
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """)
                version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                if version and version[0] != "1":
                    raise RuntimeError("不支持的数据版本，请先迁移")
                conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version','1')")
            self.admin_token = self._load_token("admin.token")
            self.join_token = self._load_token("join.token")
            self._run(lambda conn: None)
        except Exception:
            self.close()
            raise

    def _instance_lock(self):
        handle = open(self.data_dir / "instance.lock", "a+b")
        if handle.seek(0, 2) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError("该数据目录已有服务运行；请连接现有服务，或使用不同数据目录")
        self.instance_file = handle

    def close(self):
        handle = self.instance_file
        if handle:
            self.instance_file = None
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _load_token(self, name):
        path = self.data_dir / name
        if not path.exists():
            with open(path, "x", encoding="utf-8") as handle:
                handle.write(secrets.token_urlsafe(32))
            try:
                path.chmod(0o600)
            except OSError:
                pass
        return path.read_text(encoding="utf-8").strip()

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            yield conn
        finally:
            conn.close()

    def _run(self, callback):
        with self.lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    self._expire(conn)
                    result = callback(conn)
                    conn.commit()
                except Exception:
                    # Expiration is retried on the next request if this transaction fails.
                    conn.rollback()
                    self.next_expiry = 0
                    self._invalidate()
                    raise
            self._flush()
            return result

    def _get(self, conn, table, ident):
        row = conn.execute(f"SELECT data FROM {table} WHERE id=?", (ident,)).fetchone()
        if not row:
            fail("not_found", "任务或 agent 不存在", 404)
        return json.loads(row["data"])

    def _all(self, conn, table):
        return [json.loads(r["data"]) for r in conn.execute(f"SELECT data FROM {table}")]

    def _save_task(self, conn, task):
        if task["status"] == "in_progress":
            self.next_expiry = min(self.next_expiry, epoch(task["lease_expires_at"]))
        conn.execute("INSERT OR REPLACE INTO tasks VALUES (?,?)", (task["id"], dumps(task)))
        self._dirty(conn, f"tasks/{task['id']}.md", "task", task["id"])

    def _save_agent(self, conn, agent):
        conn.execute("UPDATE agents SET data=? WHERE id=?", (dumps(agent), agent["id"]))
        self._dirty(conn, f"agents/{agent['id']}.md", "agent", agent["id"])

    def _invalidate(self):
        self.state_bytes = None
        self.state_etag = None

    def _dirty(self, conn, path, kind, ident=None):
        self._invalidate()
        conn.execute("INSERT INTO documents(path,kind,entity_id,pending) VALUES (?,?,?,1) ON CONFLICT(path) DO UPDATE SET pending=1", (path, kind, ident))

    def _event(self, conn, kind, task_id=None, agent_id=None, message=""):
        conn.execute("INSERT INTO events(at,type,task_id,agent_id,message) VALUES (?,?,?,?,?)", (now_iso(), kind, task_id, agent_id, message))
        self._dirty(conn, "events.md", "events")

    def _public_task(self, task):
        return {key: val for key, val in task.items() if key != "claim_token"}

    def _public_agent(self, agent):
        agent = dict(agent)
        if time.time() - epoch(agent["last_seen_at"]) > self.lease_seconds:
            agent["status"] = "offline"
        return agent

    def _expire(self, conn):
        if time.time() < self.next_expiry:
            return
        self.next_expiry = float("inf")
        for task in self._all(conn, "tasks"):
            if task["status"] == "in_progress" and epoch(task["lease_expires_at"]) > time.time():
                self.next_expiry = min(self.next_expiry, epoch(task["lease_expires_at"]))
            elif task["status"] == "in_progress":
                previous = task["owner_id"]
                self._detach(conn, task)
                task.update(status="todo", revision=task["revision"] + 1, updated_at=now_iso(), summary="租约过期，已重新开放认领", progress=0)
                self._save_task(conn, task)
                self._event(conn, "lease_expired", task["id"], previous, "心跳超时，旧认领凭据失效，任务已回待认领")

    def _detach(self, conn, task):
        if task["owner_id"]:
            agent = self._get(conn, "agents", task["owner_id"])
            if agent["current_task_id"] == task["id"]:
                agent.update(current_task_id=None, status="idle")
                self._save_agent(conn, agent)
        task.update(owner_id=None, claim_token=None, lease_expires_at=None)

    def _lease(self):
        return datetime.fromtimestamp(time.time() + self.lease_seconds, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def authenticate(self, token):
        if not isinstance(token, str) or not token:
            fail("unauthorized", "需要认证令牌", 401)
        if secrets.compare_digest(digest(token), digest(self.admin_token)):
            return {"kind": "admin"}
        if secrets.compare_digest(digest(token), digest(self.join_token)):
            return {"kind": "join"}
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM agents WHERE token_hash=?", (digest(token),)).fetchone()
        if row:
            return {"kind": "agent", "agent_id": row["id"]}
        fail("unauthorized", "令牌无效", 401)

    def snapshot(self):
        """One immutable UTF-8 response cache; cache hits do no SQLite/Markdown IO."""
        with self.lock:
            timestamp = time.time()
            if self.state_bytes is not None and timestamp < self.state_deadline:
                return self.state_etag, self.state_bytes
            def read(conn):
                tasks = sorted([self._public_task(t) for t in self._all(conn, "tasks")], key=lambda t: (t["priority"], t["created_at"]))
                agents = sorted([self._public_agent(a) for a in self._all(conn, "agents")], key=lambda a: a["last_seen_at"], reverse=True)
                events = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100")]
                return {"protocol_version": "1", "lease_seconds": self.lease_seconds,
                        "tasks": tasks, "agents": agents, "events": events,
                        "stats": {status: sum(t["status"] == status for t in tasks) for status in ("todo", "in_progress", "blocked", "review", "done", "cancelled")}}
            result = self._run(read)
            with self._connect() as conn:
                pending = conn.execute("SELECT count(*) FROM documents WHERE pending=1").fetchone()[0]
            result["markdown"] = {"pending": pending, "conflicts": list(self.conflicts)}
            if self.last_export_error:
                result["markdown"]["error"] = self.last_export_error
            # server_time does not affect the ETag: a clock tick is not a state change.
            self.state_etag = '"' + digest(dumps(result))[:32] + '"'
            result["server_time"] = now_iso()
            self.state_bytes = dumps(result).encode("utf-8")
            deadlines = [timestamp + 15, self.next_expiry, self.next_markdown_scan]
            deadlines.extend(epoch(a["last_seen_at"]) + self.lease_seconds for a in result["agents"] if a["status"] != "offline")
            self.state_deadline = min(deadlines)
            return self.state_etag, self.state_bytes

    def state(self):
        # Direct library callers receive a copy, so they cannot mutate cached state.
        return json.loads(self.snapshot()[1])

    def task(self, ident):
        return self._run(lambda conn: {"task": self._public_task(self._get(conn, "tasks", ident))})

    def register(self, payload):
        agent = {"id": "a-" + uuid.uuid4().hex[:12], "name": string(payload.get("name"), "name", 100, True),
                 "client": string(payload.get("client", "unknown"), "client", 100, True),
                 "model": string(payload.get("model", "unknown"), "model", 100, True),
                 "capabilities": strings(payload.get("capabilities", []), "capabilities"), "status": "idle",
                 "context": context(payload.get("context", {})), "summary": "已注册，等待认领任务",
                 "last_seen_at": now_iso(), "current_task_id": None}
        token = secrets.token_urlsafe(32)
        def write(conn):
            conn.execute("INSERT INTO agents VALUES (?,?,?)", (agent["id"], dumps(agent), digest(token)))
            self._dirty(conn, f"agents/{agent['id']}.md", "agent", agent["id"])
            self._event(conn, "agent_registered", agent_id=agent["id"], message=f"{agent['name']} · {agent['client']} · {agent['model']} 已接入")
            return {"agent": agent, "token": token}
        return self._run(write)

    def _fields(self, payload):
        return {"title": string(payload.get("title"), "title", 200, True),
                "description": string(payload.get("description", ""), "description", 100000),
                "priority": integer(payload.get("priority", 1), "priority", 0, 3),
                "resources": list(dict.fromkeys(resource(v) for v in strings(payload.get("resources", []), "resources"))),
                "depends_on": strings(payload.get("depends_on", []), "depends_on"),
                "required_capabilities": strings(payload.get("required_capabilities", []), "required_capabilities")}

    def _dependencies(self, conn, ident, dependencies):
        if ident in dependencies:
            fail("dependency_cycle", "任务不能依赖自己", 400)
        def visit(current, seen):
            if current == ident:
                fail("dependency_cycle", "任务依赖不能形成环", 400)
            if current in seen:
                return
            seen.add(current)
            task = self._get(conn, "tasks", current)
            for next_id in task["depends_on"]:
                visit(next_id, seen)
        for dependency in dependencies:
            visit(dependency, set())

    def _create(self, conn, payload, actor=None, ident=None):
        fields = self._fields(payload)
        ident = ident or "t-" + uuid.uuid4().hex[:12]
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", ident):
            fail("invalid_id", "任务 id 仅允许字母数字、下划线、连字符，最多80字符", 400)
        if conn.execute("SELECT 1 FROM tasks WHERE id=?", (ident,)).fetchone():
            fail("duplicate_id", "任务 id 已存在")
        self._dependencies(conn, ident, fields["depends_on"])
        timestamp = now_iso()
        task = {"id": ident, **fields, "status": "todo", "revision": 1, "owner_id": None,
                "lease_expires_at": None, "claim_generation": 0, "claim_token": None,
                "created_at": timestamp, "updated_at": timestamp, "result": "", "blocker": "",
                "progress": 0, "summary": ""}
        self._save_task(conn, task)
        self._event(conn, "task_created", ident, actor, f"创建任务：{task['title']}")
        return {"task": self._public_task(task)}

    def create_task(self, payload, actor=None):
        return self._run(lambda conn: self._create(conn, payload, actor))

    def _revision(self, task, payload):
        supplied = payload.get("expected_revision")
        if isinstance(supplied, bool) or not isinstance(supplied, int):
            fail("revision_required", "必须提供整数 expected_revision", 400)
        if supplied != task["revision"]:
            fail("revision_conflict", "任务已变化，请重新读取任务后重试", details={"expected": supplied, "actual": task["revision"]})

    def claim(self, ident, agent_id, payload):
        request_id = payload.get("request_id")
        if request_id is not None:
            string(request_id, "request_id", 100, True)
        def write(conn):
            if request_id:
                row = conn.execute("SELECT task_id,response FROM requests WHERE agent_id=? AND request_id=?", (agent_id, request_id)).fetchone()
                if row:
                    if row["task_id"] != ident:
                        fail("request_id_conflict", "该 request_id 已用于另一任务")
                    saved = json.loads(row["response"])
                    current = self._get(conn, "tasks", ident)
                    if current["status"] != "in_progress" or current["claim_token"] != saved["claim_token"]:
                        fail("claim_expired", "该认领请求的所有权已失效，请使用新 request_id")
                    return {"task": self._public_task(current), "claim_token": saved["claim_token"]}
            task = self._get(conn, "tasks", ident)
            agent = self._get(conn, "agents", agent_id)
            self._revision(task, payload)
            if task["status"] != "todo":
                fail("task_unavailable", "只有待认领任务可以认领")
            if agent["current_task_id"]:
                fail("agent_busy", "一个 agent 同时只能认领一个任务")
            if not set(task["required_capabilities"]).issubset(agent["capabilities"]):
                fail("capability_mismatch", "agent 缺少任务所需能力")
            unfinished = [d for d in task["depends_on"] if self._get(conn, "tasks", d)["status"] != "done"]
            if unfinished:
                fail("dependencies_pending", "必须先完成全部依赖任务", details={"tasks": unfinished})
            for active in self._all(conn, "tasks"):
                if active["status"] == "in_progress":
                    conflicts = [(r, other) for r in task["resources"] for other in active["resources"] if overlap(r, other)]
                    if conflicts:
                        fail("resource_conflict", "资源已被其他 agent 锁定", details={"task_id": active["id"], "resources": conflicts})
            task.update(status="in_progress", revision=task["revision"] + 1, owner_id=agent_id,
                        claim_generation=task["claim_generation"] + 1, claim_token=secrets.token_urlsafe(32),
                        lease_expires_at=self._lease(), updated_at=now_iso(), blocker="", progress=0, summary="开始工作")
            agent.update(status="working", current_task_id=ident, last_seen_at=now_iso(), summary=f"正在处理：{task['title']}")
            self._save_task(conn, task)
            self._save_agent(conn, agent)
            self._event(conn, "task_claimed", ident, agent_id, f"{agent['name']} 认领任务（代次 {task['claim_generation']}）")
            result = {"task": self._public_task(task), "claim_token": task["claim_token"]}
            if request_id:
                conn.execute("INSERT INTO requests VALUES (?,?,?,?)", (agent_id, request_id, ident, dumps(result)))
            return result
        return self._run(write)

    def _ownership(self, task, agent_id, payload):
        supplied = payload.get("claim_token")
        if task["status"] != "in_progress" or task["owner_id"] != agent_id or not isinstance(supplied, str) or not task["claim_token"] or not secrets.compare_digest(digest(supplied), digest(task["claim_token"])):
            fail("ownership_lost", "所有权已失效：停止修改共享文件，读取最新任务后重新认领")

    def heartbeat(self, agent_id, payload):
        def write(conn):
            agent = self._get(conn, "agents", agent_id)
            task = None
            if payload.get("task_id") is not None or payload.get("claim_token") is not None:
                if not payload.get("task_id") or not payload.get("claim_token"):
                    fail("invalid_input", "续租需要 task_id 和 claim_token", 400)
                task_id = string(payload["task_id"], "task_id", 80, True)
                task = self._get(conn, "tasks", task_id)
                self._ownership(task, agent_id, payload)
                task["lease_expires_at"] = self._lease()
                self._save_task(conn, task)
            if "context" in payload:
                agent["context"] = context(payload["context"])
            if "summary" in payload:
                agent["summary"] = string(payload["summary"], "summary", 2000)
            if "status" in payload:
                if payload["status"] not in ("idle", "working", "blocked"):
                    fail("invalid_input", "无效的 agent 状态", 400)
                agent["status"] = payload["status"]
            if agent["current_task_id"] and agent["status"] == "idle":
                agent["status"] = "working"
            agent["last_seen_at"] = now_iso()
            self._save_agent(conn, agent)
            result = {"agent": self._public_agent(agent)}
            if task:
                result["task"] = self._public_task(task)
            return result
        return self._run(write)

    def transition(self, ident, action, payload, agent_id=None, admin=False):
        def write(conn):
            task = self._get(conn, "tasks", ident)
            if action in ("progress", "release", "block", "submit"):
                self._ownership(task, agent_id, payload)
            else:
                if not admin:
                    fail("forbidden", "此操作需要管理权限", 403)
            self._revision(task, payload)
            old_owner = task["owner_id"]
            if action == "progress":
                task["summary"] = string(payload.get("summary"), "summary", 2000, True)
                if "progress" in payload:
                    task["progress"] = integer(payload["progress"], "progress", 0, 100)
                task["lease_expires_at"] = self._lease()
                agent = self._get(conn, "agents", agent_id)
                agent.update(summary=task["summary"], last_seen_at=now_iso(), status="working")
                if "context" in payload:
                    agent["context"] = context(payload["context"])
                self._save_agent(conn, agent)
            elif action == "release":
                task.update(status="todo", summary=string(payload.get("summary", "主动释放任务"), "summary", 2000), progress=0)
                self._detach(conn, task)
            elif action == "block":
                task.update(status="blocked", blocker=string(payload.get("reason"), "reason", 2000, True))
                self._detach(conn, task)
            elif action == "submit":
                task.update(status="review", result=string(payload.get("result"), "result", 100000, True), progress=100, summary="已提交，等待审核")
                self._detach(conn, task)
            elif action == "approve":
                if task["status"] != "review":
                    fail("invalid_transition", "只有待审核任务可以完成")
                task["status"] = "done"
            elif action == "reopen":
                if task["status"] not in ("blocked", "review", "done", "cancelled"):
                    fail("invalid_transition", "该状态不能重新开放")
                task.update(status="todo", blocker="", progress=0, summary=string(payload.get("reason", "重新开放任务"), "reason", 2000))
                self._detach(conn, task)
            elif action == "cancel":
                if task["status"] in ("done", "cancelled"):
                    fail("invalid_transition", "已完成或取消的任务不能取消")
                task.update(status="cancelled", summary=string(payload.get("reason", "管理员取消任务"), "reason", 2000))
                self._detach(conn, task)
            else:
                fail("not_found", "未知操作", 404)
            task.update(revision=task["revision"] + 1, updated_at=now_iso())
            self._save_task(conn, task)
            self._event(conn, "task_" + action, ident, agent_id or old_owner, task["summary"] if action != "block" else task["blocker"])
            return {"task": self._public_task(task)}
        return self._run(write)

    def _edit(self, conn, ident, payload):
        task = self._get(conn, "tasks", ident)
        self._revision(task, payload)
        if task["status"] not in ("todo", "blocked"):
            fail("task_active", "只能编辑待认领或阻塞任务，请先停止或释放工作")
        fields = self._fields({**task, **payload})
        self._dependencies(conn, ident, fields["depends_on"])
        task.update(**fields, revision=task["revision"] + 1, updated_at=now_iso())
        self._save_task(conn, task)
        self._event(conn, "task_edited", ident, message=f"更新任务：{task['title']}")
        return {"task": self._public_task(task)}

    def edit(self, ident, payload):
        return self._run(lambda conn: self._edit(conn, ident, payload))

    def _document_content(self, conn, row):
        if row["kind"] == "task":
            task = self._public_task(self._get(conn, "tasks", row["entity_id"]))
            description = task.pop("description")
            return ("---\n" + json.dumps(task, ensure_ascii=False, indent=2) + "\n---\n" + description + "\n").encode("utf-8")
        if row["kind"] == "agent":
            agent = self._public_agent(self._get(conn, "agents", row["entity_id"]))
            return ("# Agent 状态快照（只读）\n\n```json\n" + json.dumps(agent, ensure_ascii=False, indent=2) + "\n```\n").encode("utf-8")
        rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 500").fetchall()
        lines = ["# 协作日志（最新 500 条，只读；完整历史在 SQLite）", ""]
        for event in rows:
            message = event["message"].replace("\n", " ")
            lines.append(f"- {event['at']} · {event['type']} · {event['task_id'] or '-'} · {message}")
        return ("\n".join(lines) + "\n").encode("utf-8")

    def _safe_path(self, relative):
        # Refuse symlinks/junctions leading outside the runtime directory.
        path = self.data_dir / relative
        if not path.resolve().is_relative_to(self.data_dir):
            fail("unsafe_path", "Markdown 路径越出数据目录", 400)
        return path

    def _atomic_write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with open(temporary, "xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _flush(self):
        exported = 0
        previous = (dumps(self.conflicts), self.last_export_error)
        self.last_export_error = None
        try:
            with self._connect() as conn:
                scan = time.time() >= self.next_markdown_scan
                query = "SELECT * FROM documents" if scan else "SELECT * FROM documents WHERE pending=1"
                rows = conn.execute(query).fetchall()
                if scan:
                    self.next_markdown_scan = time.time() + 15
                for row in rows:
                    path = self._safe_path(row["path"])
                    try:
                        stat = path.stat()
                        signature = (stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns, stat.st_ino)
                    except FileNotFoundError:
                        signature = None
                    cached = self.file_cache.get(row["path"])
                    if cached and cached[0] == signature:
                        actual_hash = cached[1]
                    else:
                        actual_hash = digest(path.read_bytes()) if signature is not None else None
                        self.file_cache[row["path"]] = (signature, actual_hash)
                    if not row["pending"] and actual_hash == row["hash"]:
                        self.file_conflicts.pop(row["path"], None)
                        continue
                    desired = self._document_content(conn, row)
                    wanted_hash = digest(desired)
                    if actual_hash is not None and actual_hash not in (row["hash"], wanted_hash):
                        self.file_conflicts[row["path"]] = {"path": row["path"], "code": "external_edit", "message": "文件在外部被修改，已保留；请在维护窗口同步并解决 revision 冲突"}
                        continue
                    if actual_hash != wanted_hash:
                        self._atomic_write(path, desired)
                    stat = path.stat()
                    self.file_cache[row["path"]] = ((stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns, stat.st_ino), wanted_hash)
                    self.file_conflicts.pop(row["path"], None)
                    conn.execute("UPDATE documents SET hash=?,pending=0 WHERE path=?", (wanted_hash, row["path"]))
                    exported += 1
        except (OSError, sqlite3.Error, ApiError) as exc:
            # Exception messages can include paths; do not include file contents/tokens.
            self.last_export_error = type(exc).__name__
            self.file_conflicts[""] = {"path": "", "code": "export_failed", "message": "Markdown 导出未完成，请检查磁盘空间、路径与权限"}
        else:
            self.file_conflicts.pop("", None)
        self.conflicts = list({c["path"]: c for c in [*self.file_conflicts.values(), *self.import_conflicts]}.values())
        if previous != (dumps(self.conflicts), self.last_export_error):
            self._invalidate()
        return exported

    @staticmethod
    def _parse_markdown(raw):
        try:
            text = raw.decode("utf-8-sig").replace("\r\n", "\n")
            match = re.match(r"\A---\n(.*?)\n---(?:\n|$)(.*)\Z", text, re.S)
            if not match:
                fail("invalid_markdown", "需要 --- 包围的 JSON 元数据与 Markdown 正文", 400)
            metadata = json.loads(match[1])
            if not isinstance(metadata, dict):
                fail("invalid_markdown", "元数据必须是 JSON 对象", 400)
            return metadata, match[2].removesuffix("\n")
        except (UnicodeError, json.JSONDecodeError) as exc:
            fail("invalid_markdown", f"无法解析 Markdown：{exc}", 400)

    def sync_markdown(self):
        # Imports are explicit; filesystem edits never acquire leases or change states.
        imported, conflicts = [], []
        with self.lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    self._expire(conn)
                    directory = self._safe_path("tasks")
                    for path in sorted(directory.glob("*.md")) if directory.exists() else []:
                        relative = f"tasks/{path.name}"
                        conn.execute("SAVEPOINT import_one")
                        try:
                            self._safe_path(relative)
                            raw = path.read_bytes()
                            if len(raw) > 200000:
                                fail("invalid_markdown", "Markdown 文件超过 200KB", 400)
                            current_hash = digest(raw)
                            document = conn.execute("SELECT * FROM documents WHERE path=?", (relative,)).fetchone()
                            if document and document["hash"] == current_hash:
                                conn.execute("RELEASE import_one")
                                continue
                            metadata, description = self._parse_markdown(raw)
                            ident = metadata.get("id", path.stem)
                            if ident != path.stem:
                                fail("filename_mismatch", "任务 id 必须等于 Markdown 文件名", 400)
                            existing = conn.execute("SELECT data FROM tasks WHERE id=?", (ident,)).fetchone()
                            payload = {k: metadata[k] for k in ("title", "priority", "resources", "depends_on", "required_capabilities") if k in metadata}
                            payload["description"] = description
                            if existing:
                                task = json.loads(existing["data"])
                                # Read-only fields cannot be forged via an offline file.
                                for key in ("status", "owner_id", "claim_generation", "lease_expires_at"):
                                    if key in metadata and metadata[key] != task[key]:
                                        fail("readonly_field", f"{key} 是只读字段，必须通过 API 修改")
                                payload["expected_revision"] = metadata.get("revision")
                                self._edit(conn, ident, payload)
                            else:
                                if metadata.get("revision", 0) != 0 or metadata.get("status", "todo") != "todo" or metadata.get("owner_id") is not None:
                                    fail("invalid_new_task", "新任务必须 revision=0、status=todo、无 owner", 400)
                                self._create(conn, payload, ident=ident)
                            conn.execute("UPDATE documents SET hash=?,pending=1 WHERE path=?", (current_hash, relative))
                            imported.append(ident)
                            conn.execute("RELEASE import_one")
                        except (ApiError, OSError) as exc:
                            conn.execute("ROLLBACK TO import_one")
                            conn.execute("RELEASE import_one")
                            conflicts.append({"path": relative, "code": getattr(exc, "code", "read_failed"), "message": str(exc)})
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            self.import_conflicts = conflicts
            self.next_markdown_scan = 0
            self._invalidate()
            exported = self._flush()
            unique = {c["path"]: c for c in [*self.conflicts, *conflicts]}
            self.conflicts = list(unique.values())
        return {"imported": imported, "exported": exported, "conflicts": self.conflicts}
