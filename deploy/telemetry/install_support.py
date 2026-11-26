"""Atomic configuration writes and rollback of deployment-owned files and units."""
import base64
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import tempfile
import time
from urllib.parse import urlsplit


def atomic_write(path, content, mode=0o644, owner=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content if isinstance(content, bytes) else content.encode("utf-8"))
            stream.flush()
            os.chmod(temporary, mode)
            if owner and os.name != "nt":
                os.chown(temporary, *owner)
            os.fsync(stream.fileno())
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def switch_link(path, target):
    with tempfile.TemporaryDirectory(dir=path.parent) as directory:
        link = Path(directory) / "current"
        link.symlink_to(target, target_is_directory=True)
        link.replace(path)


class ReleaseTransaction:
    def __init__(self, paths, units, service, command=subprocess.run):
        self.paths, self.units, self.service, self.command = paths, units, service, command
        self.changed_files = {}
        self.changed_units = set()
        self.old_files = {}
        self.old_units = {}
        self.nginx_changed = False
        self.daemon_reloaded = False
        self.rollback_database = None

    def systemctl(self, *args, **options):
        return self.command(["systemctl", *args], **options)

    def __enter__(self):
        for path in self.paths:
            if path.is_symlink():
                self.old_files[path] = path.readlink()
            elif path.exists():
                modes = path.stat()
                self.old_files[path] = (path.read_bytes(), stat.S_IMODE(modes.st_mode), (modes.st_uid, modes.st_gid))
            else:
                self.old_files[path] = None
        for unit in self.units:
            active = self.systemctl("is-active", "--quiet", unit).returncode == 0
            enabled = self.systemctl("is-enabled", unit, capture_output=True, text=True).stdout.strip()
            self.old_units[unit] = (active, enabled)
        return self

    def write(self, path, text, mode=0o644, owner=None):
        self.changed_files[path] = self.old_files[path]
        atomic_write(path, text, mode, owner)

    def switch(self, path, target):
        self.changed_files[path] = self.old_files[path]
        switch_link(path, target)

    def unit(self, action, unit):
        self.changed_units.add(unit)
        self.systemctl(action, unit, check=True)

    def reload_nginx(self):
        self.nginx_changed = True
        self.systemctl("reload", "nginx", check=True)

    def reload_units(self):
        self.daemon_reloaded = True
        self.systemctl("daemon-reload", check=True)

    def __exit__(self, exc_type, exc, traceback):
        if exc is None:
            return False

        def recover(method, *args):
            try:
                method(*args)
                return True
            except Exception as error:
                exc.add_note(f"Rollback failed: {error}")
                return False

        def systemctl(*args):
            self.systemctl(*args, check=True)

        collector_stopped = self.service not in self.changed_units or recover(systemctl, "stop", self.service)
        timers_stopped = True
        for unit in self.units:
            if unit.endswith(".timer") and unit in self.changed_units:
                timers_stopped = recover(systemctl, "stop", unit) and timers_stopped
        if self.rollback_database and collector_stopped and timers_stopped:
            recover(self.rollback_database)
        for path, old in reversed(self.changed_files.items()):
            if old is None:
                recover(lambda p: p.unlink(missing_ok=True), path)
            elif isinstance(old, Path):
                recover(switch_link, path, old)
            else:
                recover(atomic_write, path, *old)
        if self.daemon_reloaded:
            recover(systemctl, "daemon-reload")
        if self.nginx_changed:
            recover(systemctl, "reload", "nginx")

        for unit in self.units:
            if unit not in self.changed_units:
                continue
            active, enabled = self.old_units[unit]
            current = self.systemctl("is-enabled", unit, capture_output=True, text=True).stdout.strip()
            if current != enabled:
                if enabled == "enabled":
                    recover(systemctl, "enable", unit)
                elif enabled == "enabled-runtime":
                    recover(systemctl, "enable", "--runtime", unit)
                elif current in ("enabled", "enabled-runtime"):
                    recover(systemctl, "disable", unit)
            if active:
                recover(systemctl, "start", unit)
            elif unit != self.service:
                recover(systemctl, "stop", unit)
        return False


def database_backup(source, target):
    """Pre-migration backup without opening the database via a newer application."""
    if not source.exists():
        return
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as current, \
            closing(sqlite3.connect(target)) as backup:
        current.backup(backup)
        if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise OSError("pre-release database integrity check failed")


def rollback_database(database, before, failed):
    # Candidate writes were paused and mutating maintenance jobs are stopped.
    # Preserve the failed state before using SQLite's transactional backup API
    # to copy the pre-migration data into the existing, correctly owned file.
    if before.is_file():
        database_backup(database, failed)
        database_backup(before, database)


def verify_endpoints(access, origin=False):
    if origin:
        url = urlsplit(access["page_url"])
        connection = http.client.HTTPSConnection(url.hostname, url.port or 443, timeout=12)
        connection._create_connection = lambda address, timeout, source_address: socket.create_connection(
            ("127.0.0.1", address[1]), timeout, source_address)
        auth = {}
        if access["page_access"] == "password":
            credentials = base64.b64encode((access["username"] + ":" + access["password"]).encode()).decode()
            auth["Authorization"] = "Basic " + credentials
        prefix = url.path.rstrip("/")
    else:
        connection = http.client.HTTPConnection("127.0.0.1", 42767, timeout=12)
        auth = {"X-Observatory-Access": access["read_key"]}
        prefix = ""
    with closing(connection):
        for path, required in (("/healthz", "status"), ("/", None),
                               ("/api/errors?compact=1", "items"), ("/api/errors?inventory=1", "revision")):
            connection.request("GET", prefix + path, headers=auth)
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError(f"Readiness endpoint {path} returned {response.status}")
            if required is None:
                response.read()
            elif required not in json.loads(response.read()):
                raise RuntimeError(f"Readiness endpoint {path} did not return {required}")


def wait_for_collector():
    # Poll only liveness during startup. Heavy readiness reads run once after it.
    for _ in range(120):
        try:
            with closing(http.client.HTTPConnection("127.0.0.1", 42767, timeout=2)) as connection:
                connection.request("GET", "/healthz")
                response = connection.getresponse()
                if response.status == 200:
                    return
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(0.5)
    raise RuntimeError("Collector did not start before readiness deadline")
