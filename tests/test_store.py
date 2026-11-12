"""协调账本的行为测试：重点覆盖竞争、租约隔离与 Markdown 恢复。

运行：python -m unittest discover -s tests -v
每个测试拥有独立临时工作区，不读取或修改用户的运行数据。
"""

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from agentboard.store import ApiError, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentboard-tests-")
        self.workspace = Path(self.temp.name)
        self.data_dir = self.workspace / ".agentboard"
        self.store = Store(self.data_dir, workspace=self.workspace, lease_seconds=120)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def agent(self, name="test-agent", **extra):
        payload = {"name": name, "client": "unittest", "model": "test-model"}
        payload.update(extra)
        return self.store.register(payload)

    def task(self, title="测试任务", **extra):
        payload = {"title": title}
        payload.update(extra)
        return self.store.create_task(payload)["task"]

    def current(self, task_id):
        return next(task for task in self.store.state()["tasks"] if task["id"] == task_id)

    def claim(self, task, registration, **extra):
        payload = {"expected_revision": task["revision"]}
        payload.update(extra)
        return self.store.claim(task["id"], registration["agent"]["id"], payload)

    def transition(self, claim, registration, action, **extra):
        task = claim["task"]
        payload = {"expected_revision": task["revision"], "claim_token": claim["claim_token"]}
        payload.update(extra)
        return self.store.transition(task["id"], action, payload, agent_id=registration["agent"]["id"])

    def assert_error(self, status, operation, *args, **kwargs):
        with self.assertRaises(ApiError) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.status, status)
        return caught.exception

    def markdown_path(self, task_id):
        return self.data_dir / "tasks" / f"{task_id}.md"

    def read_markdown(self, task_id):
        path = self.markdown_path(task_id)
        self.store.sync_markdown()
        text = path.read_text(encoding="utf-8")
        # JSON front matter is also valid YAML; no dependency on a YAML parser.
        self.assertTrue(text.startswith("---\n"))
        metadata, description = text[4:].split("\n---\n", 1)
        return json.loads(metadata), description

    def write_markdown(self, path, metadata, description):
        path.write_text("---\n" + json.dumps(metadata, ensure_ascii=False, indent=2) + "\n---\n" + description,
                        encoding="utf-8")

    def test_simultaneous_claim_has_exactly_one_owner(self):
        task = self.task(resources=["src/store.py"])
        registrations = [self.agent(f"竞争者-{index}") for index in range(12)]
        barrier = threading.Barrier(len(registrations))

        def attempt(registration):
            barrier.wait(timeout=10)
            try:
                return ("ok", self.claim(task, registration))
            except ApiError as exc:
                return ("error", exc.status)

        with ThreadPoolExecutor(max_workers=len(registrations)) as pool:
            outcomes = list(pool.map(attempt, registrations))
        winners = [value for kind, value in outcomes if kind == "ok"]
        errors = [value for kind, value in outcomes if kind == "error"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(errors, [409] * (len(registrations) - 1))
        final = self.current(task["id"])
        self.assertEqual(final["owner_id"], winners[0]["task"]["owner_id"])
        self.assertEqual(final["claim_generation"], 1)

    def test_distinct_tasks_cannot_simultaneously_acquire_overlapping_resources(self):
        tasks = [self.task("编辑目录", resources=["SRC/"]),
                 self.task("编辑文件", resources=["src/nested/file.py"])]
        registrations = [self.agent("目录工作者"), self.agent("文件工作者")]
        barrier = threading.Barrier(2)

        def attempt(index):
            barrier.wait(timeout=10)
            try:
                return self.claim(tasks[index], registrations[index])
            except ApiError as exc:
                self.assertEqual(exc.status, 409)
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(sum(task["owner_id"] is not None for task in self.store.state()["tasks"]), 1)

    def test_resource_boundaries_and_release(self):
        first = self.agent("一号")
        second = self.agent("二号")
        third = self.agent("三号")
        parent = self.task(resources=["src/"])
        child = self.task(resources=["src/worker.py"])
        separate = self.task(resources=["src-extra/worker.py"])
        parent_claim = self.claim(parent, first)
        self.assert_error(409, self.claim, child, second)
        self.claim(separate, third)
        self.transition(parent_claim, first, "release", summary="释放目录")
        child_claim = self.claim(child, second)
        self.assertEqual(child_claim["task"]["owner_id"], second["agent"]["id"])

    def test_agent_can_only_hold_one_task(self):
        registration = self.agent()
        first = self.task("第一项")
        second = self.task("第二项")
        claim = self.claim(first, registration)
        self.assert_error(409, self.claim, second, registration)
        self.transition(claim, registration, "release")
        self.claim(second, registration)

    def test_expired_claim_token_cannot_change_reclaimed_task(self):
        # A short lease avoids editing private schema and exercises normal recovery.
        self.store.close()
        self.store = Store(self.data_dir, workspace=self.workspace, lease_seconds=0.25)
        old_agent = self.agent("旧租约")
        new_agent = self.agent("新租约")
        task = self.task()
        old_claim = self.claim(task, old_agent)
        # Populate the response cache before expiry; its TTL must not mask the lease.
        self.assertEqual(self.current(task["id"])["owner_id"], old_agent["agent"]["id"])
        time.sleep(0.35)
        recovered = self.current(task["id"])
        self.assertEqual(recovered["status"], "todo")
        self.assertIsNone(recovered["owner_id"])
        self.assertGreater(recovered["revision"], old_claim["task"]["revision"])
        new_claim = self.claim(recovered, new_agent)
        self.assertGreater(new_claim["task"]["claim_generation"], old_claim["task"]["claim_generation"])
        self.assertNotEqual(new_claim["claim_token"], old_claim["claim_token"])
        # Use the current revision: rejection must also enforce ownership/token fencing.
        self.assert_error(409, self.store.transition, task["id"], "progress", {
            "expected_revision": new_claim["task"]["revision"],
            "claim_token": old_claim["claim_token"], "summary": "迟到写入", "progress": 99,
        }, agent_id=old_agent["agent"]["id"])
        final = self.current(task["id"])
        self.assertEqual(final["owner_id"], new_agent["agent"]["id"])
        self.assertNotEqual(final["summary"], "迟到写入")

    def test_stale_revision_is_rejected_without_losing_update(self):
        task = self.task()
        edited = self.store.edit(task["id"], {"expected_revision": task["revision"], "title": "先到修改"})["task"]
        self.assert_error(409, self.store.edit, task["id"], {
            "expected_revision": task["revision"], "title": "迟到覆盖",
        })
        final = self.current(task["id"])
        self.assertEqual(final["title"], "先到修改")
        self.assertEqual(final["revision"], edited["revision"])

    def test_concurrent_edits_have_one_revision_winner(self):
        task = self.task()
        barrier = threading.Barrier(2)

        def attempt(title):
            barrier.wait(timeout=10)
            try:
                return self.store.edit(task["id"], {"expected_revision": task["revision"], "title": title})["task"]
            except ApiError as exc:
                self.assertEqual(exc.status, 409)
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ("并发修改 A", "并发修改 B")))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        current = self.current(task["id"])
        self.assertEqual(current["title"], winners[0]["title"])
        self.assertEqual(current["revision"], task["revision"] + 1)

    def test_dependencies_require_approval_and_capabilities(self):
        capable = self.agent("具备测试能力", capabilities=["python", "tests"])
        incapable = self.agent("仅设计能力", capabilities=["design"])
        dependency = self.task("前置任务")
        dependent = self.task("依赖任务", depends_on=[dependency["id"]], required_capabilities=["tests"])
        self.assert_error(409, self.claim, dependent, capable)
        claim = self.claim(dependency, capable)
        submitted = self.transition(claim, capable, "submit", result="验证通过")["task"]
        self.assertEqual(submitted["status"], "review")
        self.assert_error(409, self.claim, dependent, capable)
        self.store.transition(dependency["id"], "approve", {"expected_revision": submitted["revision"]}, admin=True)
        self.assert_error(409, self.claim, dependent, incapable)
        self.claim(dependent, capable)

    def test_heartbeat_preserves_revision_and_requires_both_lease_fields(self):
        registration = self.agent()
        task = self.task()
        claim = self.claim(task, registration)
        original = claim["task"]
        heartbeat = self.store.heartbeat(registration["agent"]["id"], {
            "task_id": task["id"], "claim_token": claim["claim_token"],
            "summary": "正在实现", "context": {"used_tokens": 1200, "max_tokens": 16000, "source": "reported"},
        })
        self.assertEqual(heartbeat["agent"]["context"]["used_tokens"], 1200)
        after = self.current(task["id"])
        self.assertEqual(after["revision"], original["revision"])
        self.assertGreaterEqual(after["lease_expires_at"], original["lease_expires_at"])
        self.assert_error(400, self.store.heartbeat, registration["agent"]["id"], {"task_id": task["id"]})

    def test_invalid_heartbeat_identity_and_unicode_claim_token_are_rejected(self):
        registration = self.agent()
        task = self.task()
        claim = self.claim(task, registration)
        agent_id = registration["agent"]["id"]
        self.assert_error(400, self.store.heartbeat, agent_id, {
            "task_id": [task["id"]], "claim_token": claim["claim_token"],
        })
        self.assert_error(409, self.store.heartbeat, agent_id, {
            "task_id": task["id"], "claim_token": "伪造令牌🔑",
        })
        self.assert_error(409, self.store.transition, task["id"], "progress", {
            "expected_revision": claim["task"]["revision"], "claim_token": "伪造令牌🔑", "summary": "应被拒绝",
        }, agent_id=agent_id)
        current = self.current(task["id"])
        self.assertEqual(current["revision"], claim["task"]["revision"])
        self.assertEqual(current["owner_id"], agent_id)
        self.assertNotEqual(current["summary"], "应被拒绝")

    def test_context_unknown_remains_null_and_private_tokens_are_not_exported(self):
        registration = self.agent()
        context = registration["agent"]["context"]
        self.assertIsNone(context["used_tokens"])
        self.assertIsNone(context["max_tokens"])
        self.assertEqual(context["source"], "unknown")
        task = self.task()
        claim = self.claim(task, registration)
        self.store.sync_markdown()
        public = json.dumps(self.store.state())
        snapshots = "\n".join(path.read_text(encoding="utf-8") for path in self.data_dir.rglob("*.md"))
        for secret in (registration["token"], claim["claim_token"]):
            self.assertNotIn(secret, public)
            self.assertNotIn(secret, snapshots)

    def test_invalid_context_and_resources_are_rejected(self):
        for context in (
            {"used_tokens": -1, "max_tokens": 100, "source": "reported"},
            {"used_tokens": 1, "max_tokens": 0, "source": "reported"},
            {"used_tokens": "12", "max_tokens": 100, "source": "reported"},
            {"used_tokens": 1, "max_tokens": 100, "source": "made-up"},
        ):
            with self.subTest(context=context):
                self.assert_error(400, self.agent, context=context)
        for resource in ("../outside", "src/../../outside", "/etc/passwd", "C:\\Windows\\system.ini", "src/*.py"):
            with self.subTest(resource=resource):
                self.assert_error(400, self.task, resources=[resource])

    def test_offline_markdown_edit_imports_with_new_revision(self):
        task = self.task(description="原始描述")
        metadata, _ = self.read_markdown(task["id"])
        metadata["title"] = "离线修改标题"
        self.write_markdown(self.markdown_path(task["id"]), metadata, "新的描述\n\n包括中文。\n")
        result = self.store.sync_markdown()
        self.assertTrue(result["imported"])
        self.assertFalse(result["conflicts"])
        current = self.current(task["id"])
        self.assertEqual(current["title"], "离线修改标题")
        self.assertEqual(current["description"].strip(), "新的描述\n\n包括中文。")
        self.assertGreater(current["revision"], task["revision"])

    def test_markdown_edit_collision_preserves_external_file_and_server_update(self):
        task = self.task(description="原始描述")
        metadata, _ = self.read_markdown(task["id"])
        metadata["title"] = "外部编辑"
        path = self.markdown_path(task["id"])
        self.write_markdown(path, metadata, "外部尚未导入的内容")
        external = path.read_bytes()
        self.store.edit(task["id"], {"expected_revision": task["revision"], "title": "服务已更新"})
        result = self.store.sync_markdown()
        self.assertTrue(result["conflicts"])
        self.assertEqual(path.read_bytes(), external)
        self.assertEqual(self.current(task["id"])["title"], "服务已更新")
        self.assertTrue(self.store.state()["markdown"]["conflicts"])

    def test_markdown_cannot_bypass_task_ownership_or_status(self):
        task = self.task()
        metadata, description = self.read_markdown(task["id"])
        metadata.update({"status": "done", "owner_id": "forged-agent", "claim_generation": 99})
        self.write_markdown(self.markdown_path(task["id"]), metadata, description)
        self.store.sync_markdown()
        current = self.current(task["id"])
        self.assertEqual(current["status"], "todo")
        self.assertIsNone(current["owner_id"])
        self.assertEqual(current["claim_generation"], task["claim_generation"])

    def test_new_markdown_task_is_imported(self):
        self.store.sync_markdown()
        directory = self.data_dir / "tasks"
        directory.mkdir(parents=True, exist_ok=True)
        self.write_markdown(directory / "offline-task.md", {
            "id": "offline-task", "title": "离线创建", "revision": 0, "status": "todo",
            "priority": 2, "resources": ["docs/"], "depends_on": [], "required_capabilities": [],
        }, "等待其他客户端认领。\n")
        result = self.store.sync_markdown()
        self.assertTrue(result["imported"])
        self.assertFalse(result["conflicts"])
        task = self.current("offline-task")
        self.assertEqual(task["title"], "离线创建")
        self.assertEqual(task["status"], "todo")
        self.assertIsNone(task["owner_id"])

    def test_restart_preserves_lease_authentication_and_resource_lock(self):
        registration = self.agent()
        task = self.task(resources=["src/"])
        claim = self.claim(task, registration)
        self.store.close()
        self.store = Store(self.data_dir, workspace=self.workspace, lease_seconds=120)
        authenticated = self.store.authenticate(registration["token"])
        self.assertEqual(authenticated["kind"], "agent")
        self.assertEqual(authenticated["agent_id"], registration["agent"]["id"])
        competitor = self.agent("重启后的竞争者")
        conflicting = self.task(resources=["src/worker.py"])
        self.assert_error(409, self.claim, conflicting, competitor)
        self.transition(claim, registration, "progress", summary="恢复后继续", progress=50)
        self.assertEqual(self.current(task["id"])["progress"], 50)
        self.store.sync_markdown()
        self.assertTrue(self.markdown_path(task["id"]).is_file())

    def test_retried_claim_request_does_not_create_a_second_lease(self):
        registration = self.agent()
        task = self.task()
        first = self.claim(task, registration, request_id="network-retry-1")
        second = self.claim(task, registration, request_id="network-retry-1")
        self.assertEqual(second["claim_token"], first["claim_token"])
        self.assertEqual(second["task"]["revision"], first["task"]["revision"])
        self.assertEqual(second["task"]["claim_generation"], first["task"]["claim_generation"])
        self.transition(first, registration, "release")
        reopened = self.current(task["id"])
        self.assert_error(409, self.claim, reopened, registration, request_id="network-retry-1")
        third = self.claim(reopened, registration, request_id="network-retry-2")
        self.assertNotEqual(third["claim_token"], first["claim_token"])

    def test_failed_markdown_projection_recovers_from_ledger_after_restart(self):
        # SQLite commit must survive a projection failure; the filesystem is a view.
        with mock.patch("agentboard.store.os.replace", side_effect=OSError("simulated disk failure")):
            task = self.task(description="已提交到事务账本的内容")
            state = self.store.state()
            self.assertGreater(state["markdown"]["pending"], 0)
            self.assertEqual(self.current(task["id"])["description"], "已提交到事务账本的内容")
        self.store.close()
        self.store = Store(self.data_dir, workspace=self.workspace)
        self.assertEqual(self.store.state()["markdown"]["pending"], 0)
        self.assertIn("已提交到事务账本的内容", self.markdown_path(task["id"]).read_text(encoding="utf-8"))
        self.assertFalse(list(self.data_dir.rglob("*.tmp")))

    def test_rejected_markdown_import_conflict_remains_visible_on_refresh(self):
        task = self.task()
        metadata, description = self.read_markdown(task["id"])
        metadata["status"] = "done"
        path = self.markdown_path(task["id"])
        self.write_markdown(path, metadata, description)
        external = path.read_bytes()
        result = self.store.sync_markdown()
        self.assertTrue(result["conflicts"])
        self.assertTrue(self.store.state()["markdown"]["conflicts"])
        self.assertTrue(self.store.state()["markdown"]["conflicts"])
        self.assertEqual(path.read_bytes(), external)

    def test_repeated_unchanged_state_does_not_reread_markdown_contents(self):
        for index in range(20):
            self.task(f"稳定任务 {index}", description="稳定的 Markdown 内容。" * 200)
        warm_state = self.store.state()
        self.assertEqual(warm_state["markdown"]["pending"], 0)
        original_read_bytes = Path.read_bytes
        markdown_reads = []

        def track_read(path):
            if path.suffix == ".md" and path.is_relative_to(self.data_dir):
                markdown_reads.append(path)
            return original_read_bytes(path)

        baseline = time.time()
        with (mock.patch.object(Path, "read_bytes", autospec=True, side_effect=track_read),
              mock.patch("agentboard.store.time.time", return_value=baseline) as clock):
            for index in range(8):
                # Cross both cache and stat-scan windows without real delays.
                clock.return_value = baseline + (index + 1) * 60
                state = self.store.state()
                self.assertEqual(len(state["tasks"]), 20)
                self.assertEqual(state["markdown"]["pending"], 0)
        self.assertEqual(markdown_reads, [], "Unchanged Markdown should be checked by metadata rather than reread and hashed")

    def test_two_store_instances_cannot_export_to_same_directory(self):
        second = None
        try:
            with self.assertRaises((ApiError, RuntimeError, OSError)):
                second = Store(self.data_dir, workspace=self.workspace)
        finally:
            if second is not None:
                second.close()


if __name__ == "__main__":
    unittest.main()
