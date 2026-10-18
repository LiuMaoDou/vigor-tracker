# Vigor Tracker

A Chinese goal and activity tracker with Morandi light/dark themes. The HTML UI talks to a Python server, which stores projects and records in **SQLite**. Different browsers connecting to the same server use the same database.

## Run locally

Python 3.9+ is sufficient; local use needs no third-party packages.

```sh
python3 server.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). The server creates `data/vigor.sqlite`. Do not use `python3 -m http.server` or open `index.html` directly: saving requires the API server.

```sh
python3 server.py --port 8765 --db /absolute/path/vigor.sqlite
```

Only the local machine can connect by default. The SQLite file, its WAL files, backups, and credentials are excluded from Git and Docker build contexts.

## Existing browser data

Open the updated app at the **same address and port, in the same browser profile** as before. If SQLite has never been written to, it reads the old `vigor-tracker` IndexedDB database:

- If SQLite has never been written to, it copies the browser projects and records into SQLite automatically. An intentionally empty browser dataset is also preserved.
- If SQLite has already been initialized, the app uses it directly, without an old-browser-data notice or automatic replacement.
- The original IndexedDB copy is left intact. Subsequent project and record writes go only to SQLite; theme preferences remain browser-local.
- A fresh browser does not seed or initialize the server database merely by visiting. Add projects using the plus button beside **我的项目**.

When moving to another hostname, export JSON from the old address and import it at the new one, or transfer a SQLite backup. Browsers cannot read another hostname's IndexedDB data.

## Storage and API

`projects` and `records` are separate tables, linked by a foreign key. A save replaces the dataset in one transaction. `state` stores the database identity and revision.

- `GET /api/projects`: projects, revision, database identity, initialization status.
- `PUT /api/projects`: `{ "projects": [...], "revision": 3 }`; validates and saves atomically.
- A stale revision returns HTTP 409. The page retains its current state and asks you to refresh, rather than silently overwriting another browser's changes.
- `GET /api/health`: confirms the database is readable.

JSON import replaces the entire dataset after confirmation. Server-side validation rejects invalid dates, quantities, and malformed data. Only the HTML page and favicon are served as files; database and source files are not web-accessible.

This is one shared tracker, not a multi-user account system. Data is refreshed on page reload; it does not currently stream live changes to other tabs. Saving requires the server to be running.

## Back up and restore

Click the icon beside **浅色 / 深色** to open **备份** (download JSON) or **恢复** (restore JSON after confirmation). The control is available on both project and record pages.

Alternatively, make a consistent SQLite backup, including committed data still in the WAL:

```sh
python3 server.py --backup backups/vigor-backup.sqlite
```

Choose a new backup filename each time. The command never overwrites an existing file. To inspect or restore a backup, stop the existing server, then start it using the backup as the database:

```sh
python3 server.py --db /absolute/path/backups/vigor-backup.sqlite
```

Keep a copy of the backup before using it as the live database. Avoid copying only `vigor.sqlite` while the service is running; use the backup command instead.

## Deploy on a cloud server

The app needs a server process and persistent disk; static-only hosting is insufficient. The included container runs Gunicorn as a non-root user and stores SQLite in a persistent named volume. The Compose port is bound to loopback for use behind an HTTPS reverse proxy.

```sh
cp .env.example .env
# Fill in VIGOR_USERNAME and VIGOR_PASSWORD in .env.
docker compose up -d --build
```

Point your HTTPS reverse proxy at `127.0.0.1:8765` and preserve the original `Host` header. The browser's native sign-in prompt protects both the page and API with the configured credentials. Keep one app instance attached to the SQLite volume; do not share the file across unrelated hosts or network filesystems. Container recreation keeps the named volume; `docker compose down -v` deletes it.

To move the local database into a new cloud deployment:

1. Create a SQLite backup using the command above and transfer that backup to the cloud server.
2. Before starting the app, build the image and copy the backup into the named volume:

   ```sh
   docker compose build
   docker compose run --rm --no-deps --user root \
     -v "$PWD/backups:/transfer:ro" vigor sh -c \
     'test ! -e /data/vigor.sqlite && cp /transfer/vigor-backup.sqlite /data/vigor.sqlite && chown 10001:10001 /data/vigor.sqlite && chmod 600 /data/vigor.sqlite'
   docker compose up -d
   ```

   This deliberately fails if a target database already exists. For an existing deployment, use JSON import after reviewing a backup.

Without Docker, create a virtual environment, install `requirements.txt`, set `VIGOR_DB_PATH`, `VIGOR_USERNAME`, and `VIGOR_PASSWORD`, and run Gunicorn behind your HTTPS proxy:

```sh
gunicorn --bind 127.0.0.1:8765 --workers 1 --threads 4 server:app
```

Use a process manager such as systemd to restart it after a reboot. `python3 server.py` is the local development entry point; Gunicorn is the deployment entry point.

## Checks

```sh
python3 -m unittest discover -s tests -v
```

The tests cover real SQLite persistence, backup integrity, concurrent writes, stale revisions, invalid input, delete-all behavior, asset isolation, request origin checks, and optional authentication.
