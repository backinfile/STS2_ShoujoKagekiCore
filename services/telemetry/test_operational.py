import copy
from contextlib import closing
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from app import Server, Store, restore_backup_set
from test_app import fixture
from test_errors import fixture as error_fixture


class OperationalTests(unittest.TestCase):
    def setUp(self):
        self.scratch = self.enterContext(tempfile.TemporaryDirectory())
        self.directory = Path(self.scratch)
        self.store = Store(self.directory / "history.sqlite3")
        self.addCleanup(self.store.errors.snapshots.close)

    def test_review_saturation_and_stable_inventory(self):
        data = error_fixture(count=1)
        _, received = self.store.errors.upload(data)
        with self.store.connect() as db:
            db.executemany("""INSERT INTO error_groups (id,build,mod_version,platform,source,message,stack,
                first_seen,last_seen,occurrences,environment,last_activity) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                ((f"{i:064x}", "test", "test", "test", "test", "test", "", 1, 1, 1, "{}", 1)
                 for i in range(1, 20000)))
        server = self.enterContext(Server(("127.0.0.1", 0), self.store, "test-key"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)

        def request(path, data=None):
            with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=8)) as client:
                headers = {"X-Observatory-Access": "test-key"}
                if data:
                    headers["Content-Type"] = "application/json"
                client.request("POST" if data else "GET", path, json.dumps(data) if data else None, headers)
                response = client.getresponse()
                return response.status, response.getheader("Retry-After"), json.loads(response.read() or "null")

        slot = server.routes["review"]
        self.assertTrue(slot.acquire(blocking=False))
        try:
            self.assertEqual(request("/api/errors?inventory=1")[:2], (503, "2"))
            self.assertEqual(request("/api/errors?compact=1")[0], 200)
            self.assertEqual(request("/healthz")[0], 200)
            self.assertEqual(request("/api/errors", data | {"count": 2})[0], 200)
        finally:
            slot.release()
        server.ingest_pause_file = self.directory / "pause"
        server.ingest_pause_file.touch()
        self.assertEqual(request("/api/errors", data | {"count": 3})[:2], (503, "2"))
        server.ingest_pause_file.unlink()

        path = "/api/errors?inventory=1"
        status, _, page = request(path + "&snapshot=start")
        self.assertEqual(status, 200)
        self.assertEqual(page["total"], 20000)
        revision, token = page["revision"], page["snapshot"]
        self.assertEqual(request("/api/errors", data | {"count": 3})[0], 200)
        stale = request(path + f"&after={page['next']}&revision={revision}")
        self.assertEqual((stale[0], stale[2]["error"]), (400, "inventory_changed"))
        pages = 1
        groups = {item["id"]: item for item in page["items"]}
        while page["next"]:
            status, _, page = request(path + f"&snapshot={token}&after={page['next']}&revision={revision}")
            self.assertEqual((status, page["revision"]), (200, revision))
            for item in page["items"]:
                self.assertNotIn(item["id"], groups)
                groups[item["id"]] = item
            pages += 1
        self.assertEqual((pages, len(groups)), (40, 20000))
        self.assertEqual(groups[received["id"]]["occurrences"], 2)
        self.assertEqual(request(path + "&snapshot=expired")[0], 409)
        health = request("/healthz?metrics=1")[2]
        self.assertEqual(health["capacity"], {"active_error_groups": 20000, "error_reports": 1})
        metrics = health["routes"]
        self.assertEqual(metrics["review"]["status"]["503"], 1)
        self.assertEqual(metrics["ingest"]["status"]["503"], 1)
        for _ in range(16):
            self.assertTrue(server.slots.acquire(timeout=2))
        try:
            self.assertEqual(request("/healthz")[:2], (503, "2"))
        finally:
            for _ in range(16):
                server.slots.release()

    def test_restore_before_and_after_archive_and_free_physical_budget(self):
        history = fixture()
        payload = json.dumps(history).encode() + b" " * 5000
        self.store.raw_limit = len(payload)
        result = self.store.upload(history, payload)[1]
        legacy = self.directory / "legacy.sqlite3"
        self.store.backup(legacy)
        before, after = self.directory / "before", self.directory / "after"
        self.store.backup_set(before)
        self.assertEqual(self.store.archive_submissions(before=int(time.time()) + 1), 1)
        self.store.backup_set(after)
        old = Store(legacy, self.store.raw.root)
        self.assertEqual(old.repair_raw_index(), 1)
        self.assertEqual(old.original(result["run_id"]), payload)

        for index, checkpoint in enumerate((before, after)):
            restored = self.directory / f"restored-{index}"
            restore_backup_set(checkpoint, restored)
            store = Store(restored / "history.sqlite3")
            self.assertEqual(store.original(result["run_id"]), payload)
            self.assertEqual(store.upload(history, payload)[0], 200)
        self.assertLess(self.store.archive_status()["raw_stored_bytes"], len(payload))
        changed = copy.deepcopy(history)
        changed["start_time"] += 1
        self.assertEqual(self.store.upload(changed)[0], 201)


if __name__ == "__main__":
    unittest.main()
