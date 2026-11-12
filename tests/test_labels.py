import tempfile
import unittest
import json

from agentboard.store import Store, ApiError


class LabelTests(unittest.TestCase):
    def test_labels_are_persisted_and_old_edits_preserve_them(self):
        with tempfile.TemporaryDirectory() as path:
            store = Store(path, workspace=path)
            try:
                task = store.create_task({'title': 'example', 'labels': ['bug', 'needs-triage']})['task']
                self.assertIn('labels', task)
                self.assertEqual(task['labels'], ['bug', 'needs-triage'])
                changed = store.edit(task['id'], {'expected_revision': task['revision'], 'title': 'edited'})['task']
                self.assertEqual(changed['labels'], task['labels'])
            finally:
                store.close()

    def test_old_tasks_and_invalid_labels(self):
        with tempfile.TemporaryDirectory() as path:
            store = Store(path, workspace=path)
            try:
                task = store.create_task({'title': 'legacy'})['task']
                def remove_labels(conn):
                    old = store._get(conn, 'tasks', task['id'])
                    old.pop('labels')
                    store._save_task(conn, old)
                store._run(remove_labels)
                self.assertEqual(store.task(task['id'])['task']['labels'], [])
                for value in ['bad,label', 'bad，label', 'bad\nlabel', 'bad\x00label']:
                    with self.assertRaises(ApiError):
                        store.create_task({'title': 'invalid', 'labels': [value]})
            finally:
                store.close()

    def test_markdown_roundtrip(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as path:
            store = Store(path, workspace=path)
            try:
                task = store.create_task({'title': 'example', 'labels': ['bug']})['task']
                store.sync_markdown()
                document = Path(path) / 'tasks' / (task['id'] + '.md')
                front, body = document.read_text(encoding='utf-8')[4:].split('\n---\n', 1)
                metadata = json.loads(front)
                self.assertEqual(metadata['labels'], ['bug'])
                metadata['labels'] = ['enhancement']
                document.write_text('---\n' + json.dumps(metadata) + '\n---\n' + body, encoding='utf-8')
                store.sync_markdown()
                self.assertEqual(store.task(task['id'])['task']['labels'], ['enhancement'])
            finally:
                store.close()
