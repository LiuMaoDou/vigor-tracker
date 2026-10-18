import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from server import APIError, Application, Store


PROJECTS = [{"name": "晨间跑步", "goal": 60, "unit": "km", "isSample": True,
             "tasks": [{"date": "2026-10-18", "quantity": 5}, {"date": "2026-10-16", "quantity": 3}]}]


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "vigor.sqlite"
        self.app = Application(self.path)

    def request(self, path="/api/projects", method="GET", data=None, headers=None, application=None):
        payload = json.dumps(data).encode() if data is not None else b""
        environ = {"PATH_INFO": path, "REQUEST_METHOD": method, "HTTP_HOST": "127.0.0.1:8765",
                   "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": str(len(payload)),
                   "wsgi.input": io.BytesIO(payload)}
        environ.update(headers or {})
        response = {}
        def start(status, result_headers):
            response.update(status=int(status.split()[0]), headers=dict(result_headers))
        raw = b"".join((application or self.app)(environ, start))
        response["body"] = json.loads(raw) if response["headers"]["Content-Type"].startswith("application/json") else raw
        return response

    def test_fresh_database_is_uninitialized_until_a_real_save(self):
        state = self.request()["body"]
        self.assertEqual(state["projects"], [])
        self.assertEqual(state["revision"], 0)
        self.assertFalse(state["initialized"])
        self.assertEqual(self.request()["body"], state)

    def test_save_creates_real_tables_and_survives_reopening(self):
        response = self.request(method="PUT", data={"projects": PROJECTS, "revision": 0})
        self.assertEqual(response["status"], 200)
        reopened = Store(self.path).read()
        self.assertEqual(reopened["projects"], PROJECTS)
        self.assertEqual(reopened["revision"], 1)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM records").fetchone()[0], 2)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_old_revision_cannot_overwrite_newer_data(self):
        self.app.store.write(PROJECTS, 0)
        response = self.request(method="PUT", data={"projects": [], "revision": 0})
        self.assertEqual(response["status"], 409)
        self.assertEqual(self.app.store.read()["projects"], PROJECTS)

    def test_concurrent_writers_only_one_commits(self):
        def write(_):
            try:
                self.app.store.write(PROJECTS, 0)
                return 200
            except APIError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(write, range(2))), [200, 409])
        self.assertEqual(self.app.store.read()["revision"], 1)

    def test_invalid_data_cannot_destroy_existing_records(self):
        self.app.store.write(PROJECTS, 0)
        for value in (None, [{}], [{"name": "bad", "goal": True, "tasks": []}],
                      [{"name": "bad", "goal": 10**400, "tasks": []}],
                      [{"name": "bad", "goal": float("nan"), "tasks": []}],
                      [{"name": "bad", "goal": 1, "tasks": [{"date": "2026-02-30", "quantity": 1}]}]):
            with self.subTest(value=value):
                self.assertEqual(self.request(method="PUT", data={"projects": value, "revision": 1})["status"], 400)
                self.assertEqual(self.app.store.read()["projects"], PROJECTS)

    def test_delete_all_is_persistent_and_does_not_reseed(self):
        self.app.store.write(PROJECTS, 0)
        self.app.store.write([], 1)
        reopened = Store(self.path).read()
        self.assertEqual(reopened["projects"], [])
        self.assertTrue(reopened["initialized"])

    def test_backup_is_consistent_and_never_overwrites(self):
        self.app.store.write(PROJECTS, 0)
        destination = Path(self.directory.name) / "backup.sqlite"
        self.app.store.backup(destination)
        self.app.store.write([], 1)
        self.assertEqual(Store(destination).read()["projects"], PROJECTS)
        with self.assertRaises(ValueError):
            self.app.store.backup(destination)

    def test_database_and_source_are_not_public_static_assets(self):
        for path in ("/data/vigor.sqlite", "/server.py", "/.env", "/../server.py", "/backups/backup.sqlite"):
            self.assertEqual(self.request(path)["status"], 404)
        response = self.request("/")
        self.assertEqual(response["status"], 200)
        self.assertIn(b"/api/projects", response["body"])
        self.assertEqual(response["headers"]["Cache-Control"], "no-store")

    def test_cross_origin_and_non_json_writes_are_rejected(self):
        body = {"projects": PROJECTS, "revision": 0}
        self.assertEqual(self.request(method="PUT", data=body, headers={"HTTP_ORIGIN": "https://elsewhere.example"})["status"], 403)
        self.assertEqual(self.request(method="PUT", data=body, headers={"CONTENT_TYPE": "text/plain"})["status"], 415)
        self.assertEqual(self.request(method="PUT", data=body, headers={"HTTP_ORIGIN": "http://127.0.0.1:8765"})["status"], 200)

    def test_cloud_auth_covers_api_and_page(self):
        secured = Application(self.path, "test-user", "test-password")
        for path in ("/", "/api/projects", "/api/health"):
            self.assertEqual(self.request(path, application=secured)["status"], 401)
        auth = "Basic " + base64.b64encode(b"test-user:test-password").decode()
        self.assertEqual(self.request(headers={"HTTP_AUTHORIZATION": auth}, application=secured)["status"], 200)
        self.assertEqual(self.request(headers={"HTTP_AUTHORIZATION": "Basic !!!"}, application=secured)["status"], 401)


if __name__ == "__main__":
    unittest.main()
