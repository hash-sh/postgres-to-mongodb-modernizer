"""Start/stop the two backends (original Postgres app and generated MongoDB app) and reset their databases."""
import os
import signal
import subprocess
import time

import psycopg
import requests
from pymongo import MongoClient

from pipeline.config import settings

ORIGINAL_ENV = {
    "PORT": "3001", "JWT_KEY": settings.jwt_key, "NODE_ENV": "development",
    "DEV_DB_USERNAME": "conduit", "DEV_DB_PASSWORD": "conduit", "DEV_DB_NAME": "conduit",
    "DEV_DB_HOSTNAME": "127.0.0.1", "DEV_DB_DIALECT": "postgres",
}
MONGO_ENV = {"PORT": "3002", "JWT_KEY": settings.jwt_key, "MONGO_URI": settings.mongo_uri, "MONGO_DB": settings.mongo_db}

PG_TABLES = ["Comments", "Favorites", "Followers", "TagList", "Articles", "Tags", "Users"]


class Backend:
    def __init__(self, name, cwd, env, url, db=None):
        self.name, self.cwd, self.env, self.url, self.db = name, cwd, env, url, db
        self.proc = None
        self.log = settings.logs / f"{name}_backend.log"

    def start(self, timeout=40):
        self.stop_port()
        settings.logs.mkdir(exist_ok=True)
        env = {**os.environ, **self.env, **({"MONGO_DB": self.db} if self.db else {})}
        logf = open(self.log, "w")  # one log per start: stack traces fed to the repair agent must be from THIS run
        logf.write(f"\n===== start {time.ctime()} =====\n")
        logf.flush()
        self.proc = subprocess.Popen(["node", "index.js"], cwd=self.cwd, env=env, stdout=logf, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"{self.name} backend exited with code {self.proc.returncode}; see {self.log}")
            try:
                if requests.get(self.url + "/", timeout=1).status_code < 500:
                    time.sleep(0.3)  # let sequelize.sync / index creation settle
                    return self
            except requests.RequestException:
                pass
            time.sleep(0.3)
        self.stop()
        raise RuntimeError(f"{self.name} backend did not become ready; see {self.log}")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc = None

    def stop_port(self):
        """Kill a stray process from a previous crashed run that still holds our port."""
        port = self.env["PORT"]
        out = subprocess.run(["bash", "-c", f"ss -ltnp 'sport = :{port}' | grep -o 'pid=[0-9]*' | cut -d= -f2"],
                             capture_output=True, text=True).stdout.split()
        for pid in out:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        if out:
            time.sleep(0.5)

    def tail_log(self, n=40):
        try:
            return "".join(self.log.read_text(errors="replace").splitlines(True)[-n:])
        except FileNotFoundError:
            return ""

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()


def original():
    return Backend("original", settings.original_backend, ORIGINAL_ENV, settings.original_url)


def mongo(db=None):
    return Backend("mongo", settings.mongo_backend, MONGO_ENV, settings.mongo_url, db=db)


def reset_postgres():
    with psycopg.connect(settings.pg_dsn) as c:
        c.execute("TRUNCATE " + ",".join(f'"{t}"' for t in PG_TABLES) + " RESTART IDENTITY CASCADE")


def reset_mongo(db=None):
    MongoClient(settings.mongo_uri).drop_database(db or settings.mongo_db)
