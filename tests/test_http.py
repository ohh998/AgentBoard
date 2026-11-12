"""真实环回 HTTP 集成测试：认证、权限、浏览器来源及静态路径边界。"""

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from agentboard.server import make_server
from agentboard.store import Store


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentboard-http-tests-")
        self.workspace = Path(self.temp.name)
        self.data_dir = self.workspace / ".agentboard"
        self.store = Store(self.data_dir, workspace=self.workspace, lease_seconds=120)
        self.admin = (self.data_dir / "admin.token").read_text(encoding="utf-8").strip()
        self.join = (self.data_dir / "join.token").read_text(encoding="utf-8").strip()
        self.server = make_server("127.0.0.1", 0, self.store)
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.store.close()
        self.temp.cleanup()

    def request(self, method, path, payload=None, token=None, headers=None, raw=None):
        supplied = dict(headers or {})
        if token is not None:
            supplied["Authorization"] = f"Bearer {token}"
        if payload is not None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            supplied.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=raw, headers=supplied)
            response = connection.getresponse()
            body = response.read()
            response_headers = dict(response.getheaders())
            if body and response_headers.get("Content-Type", "").startswith("application/json"):
                content = json.loads(body)
            else:
                content = body.decode("utf-8", errors="replace")
            return response.status, content, response_headers
        finally:
            connection.close()

    def register(self, name="测试客户端"):
        status, result, _ = self.request("POST", "/api/agents/register", {
            "name": name, "client": "generic-http", "model": "any-model",
        }, token=self.join)
        self.assertIn(status, (200, 201))
        return result

    def create_task(self, token=None):
        status, result, _ = self.request("POST", "/api/tasks", {"title": "HTTP 任务"}, token=token or self.admin)
        self.assertIn(status, (200, 201))
        return result["task"]

    def assert_api_error(self, result, expected_status):
        status, content, _ = result
        self.assertEqual(status, expected_status)
        self.assertIsInstance(content, dict)
        self.assertIn("code", content["error"])
        self.assertIn("message", content["error"])

    def test_health_is_public_but_state_requires_authentication(self):
        status, content, _ = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(content["ok"])
        self.assertEqual(content["protocol_version"], "1")
        self.assert_api_error(self.request("GET", "/api/state"), 401)
        self.assert_api_error(self.request("GET", "/api/state", token="invalid-token"), 401)
        for token in (self.join, self.admin):
            status, content, _ = self.request("GET", "/api/state", token=token)
            self.assertEqual(status, 200)
            self.assertEqual(content["protocol_version"], "1")

    def test_unchanged_state_uses_etag_and_returns_empty_304(self):
        self.create_task()
        status, state, headers = self.request("GET", "/api/state", token=self.admin)
        self.assertEqual(status, 200)
        self.assertTrue(state["tasks"])
        etag = headers.get("ETag")
        self.assertTrue(etag)
        for _ in range(3):
            status, content, headers = self.request("GET", "/api/state", token=self.admin,
                                                    headers={"If-None-Match": etag})
            self.assertEqual(status, 304)
            self.assertEqual(content, "")
            self.assertEqual(headers.get("ETag"), etag)
        # A cache hit must still cross the normal authentication boundary.
        self.assert_api_error(self.request("GET", "/api/state", headers={"If-None-Match": etag}), 401)

    def test_task_and_heartbeat_changes_invalidate_state_etag(self):
        registration = self.register()
        status, _, headers = self.request("GET", "/api/state", token=self.admin)
        self.assertEqual(status, 200)
        initial_etag = headers.get("ETag")
        self.assertTrue(initial_etag)
        task = self.create_task()
        status, state, headers = self.request("GET", "/api/state", token=self.admin,
                                              headers={"If-None-Match": initial_etag})
        self.assertEqual(status, 200)
        self.assertIn(task["id"], [item["id"] for item in state["tasks"]])
        created_etag = headers.get("ETag")
        self.assertTrue(created_etag)
        self.assertNotEqual(created_etag, initial_etag)
        status, claim, _ = self.request("POST", f"/api/tasks/{task['id']}/claim", {
            "expected_revision": task["revision"],
        }, token=registration["token"])
        self.assertEqual(status, 200)
        status, state, headers = self.request("GET", "/api/state", token=self.admin,
                                              headers={"If-None-Match": created_etag})
        self.assertEqual(status, 200)
        self.assertEqual(next(item for item in state["tasks"] if item["id"] == task["id"])["status"], "in_progress")
        claimed_etag = headers.get("ETag")
        self.assertTrue(claimed_etag)
        self.assertNotEqual(claimed_etag, created_etag)
        status, _, _ = self.request("POST", f"/api/agents/{registration['agent']['id']}/heartbeat", {
            "task_id": task["id"], "claim_token": claim["claim_token"], "summary": "缓存更新后的中文工作摘要",
            "context": {"used_tokens": 512, "max_tokens": 8192, "source": "reported"},
        }, token=registration["token"])
        self.assertEqual(status, 200)
        status, state, headers = self.request("GET", "/api/state", token=self.admin,
                                              headers={"If-None-Match": claimed_etag})
        self.assertEqual(status, 200)
        self.assertTrue(headers.get("ETag"))
        self.assertNotEqual(headers["ETag"], claimed_etag)
        current_agent = next(item for item in state["agents"] if item["id"] == registration["agent"]["id"])
        self.assertEqual(current_agent["summary"], "缓存更新后的中文工作摘要")
        self.assertEqual(current_agent["context"]["used_tokens"], 512)
        current_task = next(item for item in state["tasks"] if item["id"] == task["id"])
        self.assertEqual(current_task["revision"], claim["task"]["revision"])

    def test_unicode_login_token_is_an_authentication_error(self):
        self.assert_api_error(self.request("POST", "/api/session", {"token": "错误令牌🔑"}), 401)

    def test_join_token_registers_but_cannot_modify_tasks(self):
        registration = self.register()
        self.assertIn("token", registration)
        self.assert_api_error(self.request("POST", "/api/tasks", {"title": "越权创建"}, token=self.join), 403)
        task = self.create_task()
        self.assert_api_error(self.request("POST", f"/api/tasks/{task['id']}/claim", {
            "expected_revision": task["revision"],
        }, token=self.admin), 403)
        self.assert_api_error(self.request("POST", "/api/markdown/sync", {}, token=self.join), 403)

    def test_agent_tokens_are_bound_to_identity_and_owner_actions(self):
        first = self.register("一号客户端")
        second = self.register("二号客户端")
        task = self.create_task(token=first["token"])
        status, claim, _ = self.request("POST", f"/api/tasks/{task['id']}/claim", {
            "expected_revision": task["revision"],
        }, token=first["token"])
        self.assertEqual(status, 200)
        self.assert_api_error(self.request("POST", f"/api/agents/{first['agent']['id']}/heartbeat", {
            "summary": "冒充其他客户端",
        }, token=second["token"]), 403)
        self.assert_api_error(self.request("POST", f"/api/tasks/{task['id']}/progress", {
            "expected_revision": claim["task"]["revision"], "claim_token": claim["claim_token"], "summary": "冒充所有者",
        }, token=second["token"]), 409)
        self.assert_api_error(self.request("POST", f"/api/tasks/{task['id']}/edit", {
            "expected_revision": claim["task"]["revision"], "title": "越权管理",
        }, token=first["token"]), 403)
        status, heartbeat, _ = self.request("POST", f"/api/agents/{first['agent']['id']}/heartbeat", {
            "task_id": task["id"], "claim_token": claim["claim_token"], "summary": "正常续租",
        }, token=first["token"])
        self.assertEqual(status, 200)
        self.assertEqual(heartbeat["agent"]["id"], first["agent"]["id"])

    def test_browser_session_cookie_is_private_and_logout_revokes_access(self):
        self.assert_api_error(self.request("POST", "/api/session", {"token": self.join}), 403)
        status, content, headers = self.request("POST", "/api/session", {"token": self.admin}, headers={"Origin": self.origin})
        self.assertEqual(status, 200)
        self.assertTrue(content["ok"])
        cookie = headers.get("Set-Cookie", "")
        self.assertIn("httponly", cookie.lower())
        self.assertIn("samesite=strict", cookie.lower())
        self.assertNotIn(self.admin, cookie)
        cookie_header = cookie.split(";", 1)[0]
        status, _, _ = self.request("GET", "/api/state", headers={"Cookie": cookie_header})
        self.assertEqual(status, 200)
        status, _, headers = self.request("DELETE", "/api/session", headers={"Cookie": cookie_header, "Origin": self.origin})
        self.assertEqual(status, 200)
        self.assert_api_error(self.request("GET", "/api/state", headers={"Cookie": cookie_header}), 401)

    def test_cross_origin_requests_are_rejected_before_mutation(self):
        for path, payload in (("/api/tasks", {"title": "跨站写入"}), ("/api/session", {"token": self.admin})):
            with self.subTest(path=path):
                self.assert_api_error(self.request("POST", path, payload, token=self.admin,
                                                   headers={"Origin": "http://evil.example"}), 403)
        status, content, _ = self.request("GET", "/api/state", token=self.admin)
        self.assertEqual(status, 200)
        self.assertFalse(content["tasks"])
        self.assert_api_error(self.request("GET", "/api/state", token=self.admin,
                                           headers={"Origin": "null"}), 403)

    def test_static_paths_cannot_expose_workspace_or_credentials(self):
        secret = self.workspace / "private-file.txt"
        secret.write_text("workspace-private-sentinel", encoding="utf-8")
        for path in ("/../private-file.txt", "/%2e%2e/private-file.txt", "/.agentboard/admin.token",
                     "/%2e%2e/.agentboard/admin.token", "/web/../../.agentboard/admin.token",
                     "/%2e%2e%5cprivate-file.txt"):
            with self.subTest(path=path):
                status, content, _ = self.request("GET", path)
                self.assertIn(status, (400, 403, 404))
                serialized = json.dumps(content)
                self.assertNotIn(self.admin, serialized)
                self.assertNotIn("workspace-private-sentinel", serialized)

    def test_invalid_json_and_content_type_do_not_create_tasks(self):
        self.assert_api_error(self.request("POST", "/api/tasks", token=self.admin,
                                           headers={"Content-Type": "application/json"}, raw=b'{"title":'), 400)
        status, content, _ = self.request("POST", "/api/tasks", token=self.admin,
                                         headers={"Content-Type": "text/plain"}, raw=b'{"title":"not-json-content-type"}')
        self.assertIn(status, (400, 415))
        self.assertIn("error", content)
        status, content, _ = self.request("GET", "/api/state", token=self.admin)
        self.assertEqual(status, 200)
        self.assertFalse(content["tasks"])


if __name__ == "__main__":
    unittest.main()
