"""通过真实 HTTP 服务与子进程验证通用 CLI 的协作和凭据边界。"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from agentboard.server import make_server
from agentboard.store import Store


CLI_PATH = Path(__file__).resolve().parents[1] / "agentctl.py"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentboard-cli-tests-")
        self.workspace = Path(self.temp.name)
        self.data_dir = self.workspace / ".agentboard"
        self.profile_path = self.data_dir / "credentials" / "integration.json"
        self.store = Store(self.data_dir, workspace=self.workspace, lease_seconds=120)
        self.server = make_server("127.0.0.1", 0, self.store)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.secrets = {
            (self.data_dir / "admin.token").read_text(encoding="utf-8").strip(),
            (self.data_dir / "join.token").read_text(encoding="utf-8").strip(),
        }
        self.environment = os.environ.copy()
        self.environment.pop("AGENTBOARD_JOIN_TOKEN", None)
        self.environment.pop("AGENTBOARD_ADMIN_TOKEN", None)
        # Exercise the CLI's explicit UTF-8 setup, even when inherited encoding is ASCII.
        self.environment["PYTHONIOENCODING"] = "ascii"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.store.close()
        self.temp.cleanup()

    def profile(self):
        return json.loads(self.profile_path.read_text(encoding="utf-8"))

    def cli(self, *arguments, expected=0):
        result = subprocess.run([
            sys.executable, str(CLI_PATH), "--url", self.url,
            "--data-dir", str(self.data_dir), "--profile", "integration", "--json",
            *arguments,
        ], cwd=self.workspace, env=self.environment, capture_output=True, timeout=20)
        stdout = result.stdout.decode("utf-8")
        stderr = result.stderr.decode("utf-8")
        if self.profile_path.exists():
            profile = self.profile()
            if profile.get("token"):
                self.secrets.add(profile["token"])
            self.secrets.update(claim["claim_token"] for claim in profile["claims"].values())
        for secret in self.secrets:
            self.assertFalse(secret in stdout + stderr, "CLI output exposed a private credential")
        self.assertEqual(result.returncode, expected, f"CLI failed: {stdout} {stderr}")
        content = json.loads(stderr if expected else stdout)

        def assert_public(value):
            if isinstance(value, dict):
                self.assertFalse({"token", "claim_token", "admin_token", "join_token"}.intersection(value))
                for item in value.values():
                    assert_public(item)
            elif isinstance(value, list):
                for item in value:
                    assert_public(item)

        assert_public(content)
        if expected == 0:
            self.assertEqual(stderr, "")
        return content

    def register(self):
        return self.cli("register", "--name", "跨客户端协作者 🧩", "--client", "任意客户端",
                        "--model", "未知型号", "--capability", "python")

    def create(self, title="中文协作任务"):
        return self.cli("create", "--title", title)["task"]

    def claim(self, task):
        return self.cli("claim", task["id"])["task"]

    def cancel(self, task):
        self.store.transition(task["id"], "cancel", {"expected_revision": task["revision"], "reason": "管理员停止旧工作"}, admin=True)

    def assert_can_claim_and_heartbeat(self, title):
        next_task = self.create(title)
        claimed = self.claim(next_task)
        heartbeat = self.cli("heartbeat", "--summary", "新任务继续工作")
        self.assertEqual(heartbeat["task"]["id"], next_task["id"])
        self.assertEqual(set(self.profile()["claims"]), {next_task["id"]})
        self.assertEqual(heartbeat["task"]["owner_id"], self.profile()["agent"]["id"])
        return claimed

    def test_unicode_json_full_workflow_keeps_credentials_private(self):
        registration = self.register()
        agent = registration["agent"]
        self.assertEqual(agent["name"], "跨客户端协作者 🧩")
        self.assertEqual(agent["context"]["source"], "unknown")
        self.assertIsNone(agent["context"]["used_tokens"])
        self.assertIsNone(agent["context"]["max_tokens"])
        task_file = self.workspace / "中文任务.md"
        task_file.write_text('---\n' + json.dumps({
            "title": "修复并发协作 🧩", "revision": 0, "status": "todo",
            "resources": ["src/"], "required_capabilities": ["python"],
        }, ensure_ascii=False) + '\n---\n## 工作说明\n实现并验证中文协作流程。\n', encoding="utf-8")
        task = self.cli("create", "--file", str(task_file))["task"]
        self.assertEqual(task["title"], "修复并发协作 🧩")
        self.assertIn("实现并验证中文", task["description"])
        claimed = self.claim(task)
        self.assertEqual(claimed["status"], "in_progress")
        self.assertIn(task["id"], self.profile()["claims"])
        heartbeat = self.cli("heartbeat", "--summary", "正在验证未知上下文")
        self.assertEqual(heartbeat["task"]["revision"], claimed["revision"])
        self.assertIsNone(heartbeat["agent"]["context"]["used_tokens"])
        progress = self.cli("progress", task["id"], "--summary", "中文进度 ✅", "--progress", "60",
                            "--context-source", "estimated", "--context-used", "321", "--context-max", "4096",
                            "--context-note", "按客户端摘要估算")["task"]
        self.assertEqual(progress["progress"], 60)
        self.assertEqual(progress["summary"], "中文进度 ✅")
        status = self.cli("status")
        self.assertEqual(status["agent"]["context"]["used_tokens"], 321)
        self.assertEqual(status["agent"]["context"]["source"], "estimated")
        submitted = self.cli("submit", task["id"], "--result", "已验证流程，等待人工审核。 🧪")["task"]
        self.assertEqual(submitted["status"], "review")
        self.assertIn("等待人工审核", submitted["result"])
        self.assertFalse(self.profile()["claims"])
        self.assertFalse(self.profile()["pending_claims"])

    def test_status_and_show_remove_cancelled_expired_or_other_owner_claims(self):
        self.register()
        original = self.claim(self.create("管理员即将取消"))
        self.cancel(original)
        status = self.cli("status")
        self.assertEqual(status["local_claims"], [])
        self.assertFalse(self.profile()["claims"])
        next_claim = self.assert_can_claim_and_heartbeat("取消后接取新任务")
        self.cli("release", next_claim["id"])

        # Only this claim is short-lived; subsequent subprocesses use ordinary leases.
        expired_task = self.create("即将过期并转交他人")
        self.store.lease_seconds = 0.25
        self.claim(expired_task)
        time.sleep(0.35)
        self.store.lease_seconds = 120
        recovered = self.store.task(expired_task["id"])["task"]
        self.assertEqual(recovered["status"], "todo")
        competitor = self.store.register({"name": "另一客户端", "client": "http", "model": "generic"})
        self.store.claim(recovered["id"], competitor["agent"]["id"], {"expected_revision": recovered["revision"]})
        shown = self.cli("show", expired_task["id"])["task"]
        self.assertEqual(shown["status"], "in_progress")
        self.assertEqual(shown["owner_id"], competitor["agent"]["id"])
        self.assertFalse(self.profile()["claims"])
        self.assert_can_claim_and_heartbeat("转交后继续接取新任务")

    def test_heartbeat_ownership_loss_cleans_local_claim_before_next_task(self):
        self.register()
        original = self.claim(self.create("旧任务失去所有权"))
        self.cancel(original)
        error = self.cli("heartbeat", "--summary", "这次续租必须失败", expected=3)
        self.assertEqual(error["error"]["code"], "ownership_lost")
        self.assertEqual(error["error"]["http_status"], 409)
        self.assertFalse(self.profile()["claims"])
        claimed = self.assert_can_claim_and_heartbeat("失去所有权后接取另一任务")
        self.assertEqual(self.cli("status")["local_claims"], [claimed["id"]])


if __name__ == "__main__":
    unittest.main()
