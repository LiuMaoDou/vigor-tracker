"""Vigor: a small WSGI app with transactional SQLite storage.

Local: python3 server.py
Production: gunicorn --bind 0.0.0.0:8765 --workers 1 --threads 4 server:app
"""

import argparse
from datetime import date
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from socketserver import ThreadingMixIn
import threading
from urllib.parse import urlsplit
from uuid import uuid4
from wsgiref.simple_server import WSGIServer, make_server


ROOT = Path(__file__).resolve().parent
MAX_BODY = 5 * 1024 * 1024


class APIError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def validate_projects(projects):
    def positive_number(value):
        return type(value) in (int, float) and 0 < value <= 9e15 and math.isfinite(value)

    if not isinstance(projects, list):
        raise APIError(400, "项目数据必须是数组")
    normalized = []
    for project in projects:
        if (not isinstance(project, dict)
                or not isinstance(project.get("name"), str) or not project["name"].strip()
                or not positive_number(project.get("goal"))
                or not isinstance(project.get("unit", ""), str)
                or not isinstance(project.get("tasks"), list)
                or type(project.get("isSample", False)) is not bool):
            raise APIError(400, "项目名称、目标、单位或记录格式无效")
        tasks = []
        for task in project["tasks"]:
            if (not isinstance(task, dict) or not isinstance(task.get("date"), str)
                    or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", task["date"])
                    or not positive_number(task.get("quantity"))):
                raise APIError(400, "记录日期或完成数量无效")
            try:
                date.fromisoformat(task["date"])
            except ValueError:
                raise APIError(400, "记录日期无效") from None
            tasks.append({"date": task["date"], "quantity": task["quantity"]})
        item = {"name": project["name"], "goal": project["goal"],
                "unit": project.get("unit", ""), "tasks": tasks}
        if "isSample" in project:
            item["isSample"] = project["isSample"]
        normalized.append(item)
    return normalized


class Store:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = self.connect()
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    database_id TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY,
                    position INTEGER NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    goal REAL NOT NULL CHECK (goal > 0),
                    unit TEXT NOT NULL,
                    is_sample INTEGER
                );
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    quantity REAL NOT NULL CHECK (quantity > 0),
                    UNIQUE (project_id, position)
                );
            """)
            db.execute("INSERT OR IGNORE INTO state (id, database_id) VALUES (1, ?)", (str(uuid4()),))
            db.commit()
        finally:
            db.close()
        os.chmod(self.path, 0o600)

    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def read(self):
        db = self.connect()
        try:
            # The revision and both tables must come from one consistent snapshot.
            db.execute("BEGIN")
            state = db.execute("SELECT database_id, revision FROM state WHERE id=1").fetchone()
            result = []
            for row in db.execute("SELECT * FROM projects ORDER BY position"):
                project = {"name": row["name"], "goal": row["goal"], "unit": row["unit"],
                           "tasks": [dict(task) for task in db.execute(
                               "SELECT date, quantity FROM records WHERE project_id=? ORDER BY position", (row["id"],))]}
                if row["is_sample"] is not None:
                    project["isSample"] = bool(row["is_sample"])
                result.append(project)
            return {"projects": result, "revision": state["revision"],
                    "databaseId": state["database_id"], "initialized": state["revision"] > 0}
        finally:
            db.close()

    def write(self, projects, revision):
        projects = validate_projects(projects)
        if type(revision) is not int or revision < 0:
            raise APIError(400, "缺少有效的数据版本，请刷新页面")
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT revision FROM state WHERE id=1").fetchone()[0]
            if current != revision:
                raise APIError(409, "数据已在其他页面更新。本次修改未保存，请刷新页面后重试。")
            db.execute("DELETE FROM projects")
            for position, project in enumerate(projects):
                sample = int(project["isSample"]) if "isSample" in project else None
                cursor = db.execute("INSERT INTO projects (position, name, goal, unit, is_sample) VALUES (?, ?, ?, ?, ?)",
                                    (position, project["name"], project["goal"], project["unit"], sample))
                db.executemany("INSERT INTO records (project_id, position, date, quantity) VALUES (?, ?, ?, ?)",
                               [(cursor.lastrowid, i, task["date"], task["quantity"])
                                for i, task in enumerate(project["tasks"])])
            db.execute("UPDATE state SET revision=revision+1 WHERE id=1")
            db.commit()
            return {"projects": projects, "revision": revision + 1}
        finally:
            db.close()

    def backup(self, destination):
        destination = Path(destination).expanduser().resolve()
        if destination == self.path or destination.exists():
            raise ValueError("Choose a new backup filename; existing files are never overwritten.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = self.connect()
        target = sqlite3.connect(str(destination))
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        os.chmod(destination, 0o600)


class Application:
    def __init__(self, path):
        self.store = Store(path)

    def __call__(self, environ, start_response):
        headers = [("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff")]
        status = 200
        content_type = "application/json; charset=utf-8"
        try:
            path = environ.get("PATH_INFO", "/")
            method = environ.get("REQUEST_METHOD", "GET")
            if path == "/api/projects":
                if method == "GET":
                    body = self.store.read()
                elif method == "PUT":
                    origin = environ.get("HTTP_ORIGIN")
                    if origin and urlsplit(origin).netloc.lower() != environ.get("HTTP_HOST", "").lower():
                        raise APIError(403, "不允许跨站修改数据")
                    if environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip() != "application/json":
                        raise APIError(415, "请求必须使用 JSON 格式")
                    try:
                        length = int(environ.get("CONTENT_LENGTH", "0"))
                    except ValueError:
                        raise APIError(400, "请求长度无效") from None
                    if length <= 0 or length > MAX_BODY:
                        raise APIError(413, "请求数据为空或超过 5 MB")
                    try:
                        data = json.loads(environ["wsgi.input"].read(length))
                    except (ValueError, UnicodeDecodeError):
                        raise APIError(400, "JSON 格式无效") from None
                    if not isinstance(data, dict):
                        raise APIError(400, "请求数据格式无效")
                    body = self.store.write(data.get("projects"), data.get("revision"))
                else:
                    raise APIError(405, "不支持此请求方法")
            elif path == "/api/health" and method == "GET":
                self.store.read()
                body = {"status": "ok", "storage": "sqlite"}
            elif path in ("/", "/index.html", "/favicon.ico") and method in ("GET", "HEAD"):
                # Explicit allowlist: never expose SQLite files, source, backups, or credentials.
                filename = "favicon.ico" if path == "/favicon.ico" else "index.html"
                body = (ROOT / filename).read_bytes()
                content_type = "image/x-icon" if filename.endswith(".ico") else "text/html; charset=utf-8"
            else:
                raise APIError(404, "页面不存在")
        except APIError as error:
            status = error.status
            body = {"error": str(error)}
        except sqlite3.Error:
            status = 503
            body = {"error": "数据库暂时不可用，请稍后重试；本次修改未保存"}
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        reasons = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
                   405: "Method Not Allowed", 409: "Conflict", 413: "Content Too Large",
                   415: "Unsupported Media Type", 503: "Service Unavailable"}
        start_response(f"{status} {reasons[status]}", headers + [("Content-Type", content_type), ("Content-Length", str(len(body)))])
        return [b"" if environ.get("REQUEST_METHOD") == "HEAD" else body]


_application = None
_lock = threading.Lock()


def app(environ, start_response):
    global _application
    with _lock:
        if _application is None:
            _application = Application(os.environ.get("VIGOR_DB_PATH", ROOT / "data/vigor.sqlite"))
    return _application(environ, start_response)


class ThreadedServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vigor SQLite server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=os.environ.get("VIGOR_DB_PATH", str(ROOT / "data/vigor.sqlite")))
    parser.add_argument("--backup", metavar="FILE", help="Create a consistent SQLite backup, then exit")
    args = parser.parse_args()
    if args.backup:
        Store(args.db).backup(args.backup)
        print(f"Backup saved: {Path(args.backup).resolve()}")
    else:
        _application = Application(args.db)
        with make_server(args.host, args.port, app, server_class=ThreadedServer) as httpd:
            print(f"Vigor: http://{args.host}:{httpd.server_port} | SQLite: {_application.store.path}", flush=True)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
