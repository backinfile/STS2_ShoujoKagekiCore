"""Run as root on the deployment host after copying this release; owns only its service and nginx includes."""
import argparse
import grp
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import subprocess
import time
from domain_config import resolve_origins
from install_support import ReleaseTransaction, database_backup, rollback_database, verify_endpoints, wait_for_collector
from urllib.parse import urlsplit

ROOT = Path("/opt/karen-observatory")
CONFIG = Path("/etc/karen-observatory")
DATA = Path("/var/lib/karen-observatory")
LOGS = Path("/var/log/karen-observatory")
USER = "karen-observatory"
parser = argparse.ArgumentParser()
parser.add_argument("--public-origin", "--page-origin", default=os.environ.get("TELEMETRY_PUBLIC_ORIGIN"))
parser.add_argument("--upload-origin", default=os.environ.get("TELEMETRY_UPLOAD_ORIGIN"))
parser.add_argument("--error-max-groups", type=int)
parser.add_argument("--error-max-reports", type=int)
parser.add_argument("--error-retention-days", type=int)
parser.add_argument("--error-ignored-retention-days", type=int)
parser.add_argument("--page-access", choices=("link", "password"))
args = parser.parse_args()
if os.geteuid() != 0 or not Path("/etc/nginx/karen-services.d").is_dir():
    raise SystemExit("Root and the shared Karen nginx includes are required")
release = Path(__file__).resolve().parent
if ROOT not in release.parents or release.parent.name != "releases":
    raise SystemExit("Expected an isolated /opt/karen-observatory/releases/<release> directory")
try:
    account = pwd.getpwnam(USER)
except KeyError:
    subprocess.run(["useradd", "--system", "--home", str(DATA), "--shell", "/usr/sbin/nologin", USER], check=True)
    account = pwd.getpwnam(USER)
CONFIG.mkdir(mode=0o750, parents=True, exist_ok=True)
os.chown(CONFIG, 0, grp.getgrnam("www-data").gr_gid)
DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chown(DATA, account.pw_uid, account.pw_gid)
LOGS.mkdir(mode=0o750, parents=True, exist_ok=True)
os.chown(LOGS, account.pw_uid, account.pw_gid)
log_file = LOGS / "service.log"
log_file.touch(exist_ok=True)
log_file.chmod(0o640)
os.chown(log_file, account.pw_uid, account.pw_gid)
access_file = CONFIG / "access.json"
if access_file.exists():
    access = json.loads(access_file.read_text())
else:
    access = {"page_path": "/observe-" + secrets.token_urlsafe(24) + "/",
              "upload_path": "/ingest-" + secrets.token_urlsafe(24),
              "username": "karen", "password": secrets.token_urlsafe(24),
              "read_key": secrets.token_urlsafe(32)}
access.pop("upload_key", None)  # A distributed game client cannot keep a service credential secret.
access["page_access"] = args.page_access or access.get("page_access", "password")
for key in ("page_path", "upload_path"):
    if not re.fullmatch(r"/[A-Za-z0-9_-]{25,90}/?", access[key]):
        raise SystemExit("Invalid persistent route")
page_origin, upload_origin = resolve_origins(access, args.public_origin, args.upload_origin)
page_host = urlsplit(page_origin).hostname
upload_host = urlsplit(upload_origin).hostname
upload_certificate = Path("/etc/letsencrypt/live/karen-telemetry-upload")
if not all((upload_certificate / name).is_file() for name in ("fullchain.pem", "privkey.pem")):
    raise SystemExit("Provision the upload hostname certificate as karen-telemetry-upload before installing")
access["page_url"] = page_origin + access["page_path"]
access["upload_url"] = upload_origin + access["upload_path"]
hashed = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=access["password"] + "\n",
                        text=True, capture_output=True, check=True).stdout.strip()
password_file = CONFIG / "htpasswd"
environment = CONFIG / "service.env"
limits = {"RAW_SUBMISSIONS_LIMIT_BYTES": 4 * 1024**3, "ERROR_MAX_GROUPS": 20000, "ERROR_MAX_REPORTS": 250000, "ERROR_RETENTION_DAYS": 7, "ERROR_IGNORED_RETENTION_DAYS": 3}
if environment.exists():
    for line in environment.read_text().splitlines():
        key, _, value = line.partition("=")
        if key in limits:
            limits[key] = int(value)
for key, value in (("ERROR_MAX_GROUPS", args.error_max_groups), ("ERROR_MAX_REPORTS", args.error_max_reports),
                   ("ERROR_RETENTION_DAYS", args.error_retention_days),
                   ("ERROR_IGNORED_RETENTION_DAYS", args.error_ignored_retention_days)):
    if value is not None:
        limits[key] = value
if not 1 <= limits["ERROR_MAX_GROUPS"] <= 100000 or not 1 <= limits["ERROR_MAX_REPORTS"] <= 1000000:
    raise SystemExit("Error limits out of bounds: groups 1..100000, reports 1..1000000")
if any(not 1 <= limits[key] <= 3650 for key in ("ERROR_RETENTION_DAYS", "ERROR_IGNORED_RETENTION_DAYS")):
    raise SystemExit("Error retention days must be 1..3650")

environment_text = (f"DATABASE_PATH={DATA}/history.sqlite3\nPORT=42767\n"
                       f"RAW_SUBMISSIONS_PATH={DATA}/raw-submissions\n"
                       f"TELEMETRY_INGEST_PAUSE_FILE={DATA}/ingest-paused\n"
                       f"READ_KEY={access['read_key']}\n"
                       f"TELEMETRY_WEB_ROOT={ROOT}/current/web\n"
                       + "".join(f"{key}={value}\n" for key, value in limits.items()))

current = ROOT / "current"
service = f"""[Unit]
Description=Karen run history collector and observatory
After=network.target
[Service]
Type=simple
User={USER}
Group={USER}
WorkingDirectory={ROOT}/current
EnvironmentFile={CONFIG}/service.env
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 {ROOT}/current/app.py
StandardOutput=append:{LOGS}/service.log
StandardError=append:{LOGS}/service.log
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={DATA}
ReadWritePaths={LOGS}
RestrictAddressFamilies=AF_INET AF_UNIX
MemoryHigh=192M
MemoryMax=256M
CPUQuota=50%
TasksMax=32
LimitNOFILE=256
[Install]
WantedBy=multi-user.target
"""
headers = """add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer" always;
    add_header X-Robots-Tag "noindex, nofollow" always;
    add_header Content-Security-Policy "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; connect-src 'self'; worker-src 'self'; manifest-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'" always;"""
page_auth = "auth_basic off;" if access["page_access"] == "link" else f'auth_basic "Karen Observatory";\n    auth_basic_user_file {password_file};'
locations = f"""# Generated by the independent observatory installer. No public root route.
location ^~ {access['page_path']} {{
    if ($host != {page_host}) {{ return 404; }}
    if ($request_method !~ ^(GET|HEAD)$) {{ return 405; }}
    {page_auth}
    limit_req zone=karen_observatory_cf_reads burst=50 nodelay;
    limit_req zone=karen_observatory_cf_reads_global burst=80 nodelay;
    limit_req_status 429;
    client_max_body_size 4k;
    proxy_pass http://127.0.0.1:42767/;
    proxy_set_header X-Observatory-Access "{access['read_key']}";
    proxy_set_header X-Upload-Key "";
    proxy_set_header Authorization "";
    proxy_set_header X-Forwarded-For "";
    proxy_set_header Forwarded "";
    proxy_set_header X-Karen-Client-IP $karen_client_ip;
    proxy_http_version 1.1;
    proxy_pass_header Cache-Control;
    proxy_set_header X-Observatory-Client $http_x_observatory_client;
    proxy_intercept_errors off;
    proxy_read_timeout 30s;
    access_log off;
    {headers}
}}
location = {access['upload_path']} {{
    if ($request_method != POST) {{ return 405; }}
    auth_basic off;
    limit_req zone=karen_observatory_cf_uploads burst=10 nodelay;
    limit_req zone=karen_observatory_cf_uploads_global burst=10 nodelay;
    limit_conn karen_observatory_upload_concurrency 2;
    limit_req_status 429;
    limit_conn_status 429;
    client_max_body_size 8m;
    client_body_timeout 15s;
    proxy_pass http://127.0.0.1:42767/api/upload;
    proxy_set_header X-Observatory-Access "";
    proxy_set_header X-Upload-Key "";
    proxy_set_header Authorization $http_authorization;
    proxy_set_header X-Forwarded-For "";
    proxy_set_header Forwarded "";
    proxy_set_header X-Karen-Client-IP $karen_client_ip;
    proxy_read_timeout 30s;
    access_log off;
    {headers}
}}
location = {access['upload_path']}/errors {{
    if ($request_method != POST) {{ return 405; }}
    auth_basic off;
    limit_req zone=karen_observatory_errors burst=5 nodelay;
    limit_req zone=karen_observatory_errors_global burst=10 nodelay;
    limit_conn karen_observatory_error_concurrency 2;
    limit_req_status 429;
    limit_conn_status 429;
    client_max_body_size 64k;
    client_body_timeout 10s;
    proxy_pass http://127.0.0.1:42767/api/errors;
    proxy_set_header X-Observatory-Access "";
    proxy_set_header Authorization "";
    proxy_set_header X-Forwarded-For "";
    proxy_set_header Forwarded "";
    proxy_set_header X-Karen-Client-IP $karen_client_ip;
    proxy_read_timeout 20s;
    access_log off;
    {headers}
}}
location = {access['upload_path']}/session {{
    if ($request_method != POST) {{ return 405; }}
    auth_basic off;
    limit_req zone=karen_observatory_cf_uploads burst=10 nodelay;
    limit_req zone=karen_observatory_cf_uploads_global burst=10 nodelay;
    limit_conn karen_observatory_upload_concurrency 2;
    limit_req_status 429;
    limit_conn_status 429;
    client_max_body_size 1k;
    client_body_timeout 10s;
    proxy_pass http://127.0.0.1:42767/api/upload-session;
    proxy_set_header X-Observatory-Access "";
    proxy_set_header Authorization "";
    proxy_set_header X-Forwarded-For "";
    proxy_set_header Forwarded "";
    proxy_set_header X-Karen-Client-IP $karen_client_ip;
    proxy_read_timeout 10s;
    access_log off;
    {headers}
}}
"""
zones = """limit_req_zone $karen_client_ip zone=karen_observatory_cf_reads:1m rate=20r/s;
limit_req_zone $server_name zone=karen_observatory_cf_reads_global:1m rate=40r/s;
limit_req_zone $karen_client_ip zone=karen_observatory_cf_uploads:1m rate=30r/m;
limit_req_zone $server_name zone=karen_observatory_cf_uploads_global:1m rate=30r/m;
limit_conn_zone $server_name zone=karen_observatory_upload_concurrency:1m;
limit_req_zone $karen_client_ip zone=karen_observatory_errors:1m rate=6r/m;
limit_req_zone $server_name zone=karen_observatory_errors_global:1m rate=60r/m;
limit_conn_zone $server_name zone=karen_observatory_error_concurrency:1m;
"""
upload_server = f"""# Dedicated ingest host. Read routes are gated to the page host.
server {{
    listen 80;
    listen [::]:80;
    server_name {upload_host};
    return 301 https://$host$request_uri;
}}
server {{
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name {upload_host};
    ssl_certificate {upload_certificate}/fullchain.pem;
    ssl_certificate_key {upload_certificate}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    server_tokens off;
    include /etc/nginx/karen-services.d/karen-observatory.conf;
    location / {{ return 404; }}
}}
"""
files = {Path("/etc/nginx/conf.d/karen-observatory-upload.conf"): upload_server,
         Path("/etc/nginx/karen-services.d/karen-observatory.conf"): locations,
         Path("/etc/nginx/karen-services-http.d/karen-observatory.conf"): zones}
nginx_files = files
files = {Path("/etc/systemd/system/karen-observatory.service"): service}

backup_script = ROOT / "backup.py"
files[backup_script] = '''from datetime import datetime, timezone
from pathlib import Path
import re, shutil, subprocess
root=Path('/var/lib/karen-observatory/backups')
root.mkdir(mode=0o700,exist_ok=True)
target=root/('history-set-'+datetime.now(timezone.utc).strftime('%Y%m%d'))
if not target.exists():
    subprocess.run(['/usr/bin/python3','/opt/karen-observatory/current/app.py','--backup-set',str(target)],check=True)
backups=[p for p in root.iterdir() if re.fullmatch('history-set-[0-9]{8}',p.name)
         and (p/'manifest.json').is_file() and not p.is_symlink()]
backups.extend(p for p in root.iterdir() if re.fullmatch('history-[0-9]{8}[.]sqlite3',p.name)
               and p.is_file() and not p.is_symlink())
for old in sorted(backups,key=lambda p:(p.stem[-8:],p.is_dir()))[:-7]:
    if old.is_dir() and old.resolve().parent==root.resolve():
        shutil.rmtree(old)
    elif old.is_file():
        old.unlink()
'''
files[Path("/etc/systemd/system/karen-observatory-backup.service")] = f"""[Unit]
Description=Consistent SQLite and raw submissions backup for Karen Observatory
[Service]
Type=oneshot
User={USER}
Group={USER}
UMask=0077
EnvironmentFile={CONFIG}/service.env
ExecStart=/usr/bin/python3 {backup_script}
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths={DATA}
ProtectHome=true
"""
files[Path("/etc/systemd/system/karen-observatory-backup.timer")] = """[Unit]
Description=Daily Karen history backup
[Timer]
OnCalendar=*-*-* 04:30:00 UTC
Persistent=true
RandomizedDelaySec=15m
[Install]
WantedBy=timers.target
"""
files[Path("/etc/systemd/system/karen-observatory-raw-archive.service")] = f"""[Unit]
Description=Verify and compress closed UTC days of original Karen JSON submissions
[Service]
Type=oneshot
User={USER}
Group={USER}
WorkingDirectory={ROOT}/current
EnvironmentFile={CONFIG}/service.env
Environment=PYTHONDONTWRITEBYTECODE=1
UMask=0077
ExecStart=/usr/bin/python3 {ROOT}/current/app.py --archive-raw
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={DATA}
MemoryMax=128M
CPUQuota=25%
"""
files[Path("/etc/systemd/system/karen-observatory-raw-archive.timer")] = """[Unit]
Description=Daily compression of original Karen JSON submissions
[Timer]
OnCalendar=*-*-* 00:15:00 UTC
Persistent=true
RandomizedDelaySec=10m
[Install]
WantedBy=timers.target
"""
files[Path("/etc/logrotate.d/karen-observatory")] = f"""{LOGS}/service.log {{
    daily
    rotate 7
    maxsize 1M
    missingok
    notifempty
    compress
    copytruncate
    su {USER} {USER}
}}
"""
monitor = ROOT / "monitor.py"
files[monitor] = '''import json,os,shutil,time
from pathlib import Path
from urllib.request import urlopen
data=Path('/var/lib/karen-observatory')
with urlopen('http://127.0.0.1:42767/healthz?metrics=1',timeout=5) as response:
    metrics=json.load(response)
    if metrics.get('status')!='ok': raise SystemExit('health endpoint failed')
    print(json.dumps(metrics,separators=(',',':')))
if shutil.disk_usage(data).free<2*1024**3: raise SystemExit('disk free below 2GiB')
if (data/'history.sqlite3').stat().st_size>900*1024**2: raise SystemExit('database above 900MiB warning threshold')
backups=list((data/'backups').glob('history-set-????????/manifest.json'))
if not backups or time.time()-max(p.stat().st_mtime for p in backups)>48*3600: raise SystemExit('backup missing or older than 48 hours')
pending=list((data/'raw-submissions'/'pending').glob('*/*.json'))
try:
    sizes=[p.stat() for p in pending]
except FileNotFoundError:
    sizes=[]  # a concurrent archive pass removed a loose file
if sizes and time.time()-min(p.st_mtime for p in sizes)>48*3600: raise SystemExit('raw JSON archive backlog older than 48 hours')
total=sum(p.st_size for p in sizes)
for bundle in (data/'raw-submissions'/'bundles').glob('*.tar.gz'):
    try: total+=bundle.stat().st_size
    except FileNotFoundError: pass
groups=metrics['capacity']['active_error_groups']
reports=metrics['capacity']['error_reports']
print(json.dumps({'archive_bytes':total,'active_error_groups':groups,'error_reports':reports}))
if (total>=int(os.environ['RAW_SUBMISSIONS_LIMIT_BYTES'])*.85 or
        groups>=int(os.environ['ERROR_MAX_GROUPS'])*.85 or
        reports>=int(os.environ['ERROR_MAX_REPORTS'])*.85):
    print('Observatory storage nearing configured capacity',flush=True)
'''
files[Path("/etc/systemd/system/karen-observatory-monitor.service")] = f"""[Unit]
Description=Karen collector health, disk and backup monitor
[Service]
Type=oneshot
User={USER}
Group={USER}
EnvironmentFile={CONFIG}/service.env
ExecStart=/usr/bin/python3 {monitor}
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
MemoryMax=48M
CPUQuota=10%
"""
files[Path("/etc/systemd/system/karen-observatory-monitor.timer")] = """[Unit]
Description=Check Karen collector every minute
[Timer]
OnBootSec=2m
OnUnitActiveSec=1m
[Install]
WantedBy=timers.target
"""

files[Path("/etc/systemd/system/karen-observatory-error-retention.service")] = f"""[Unit]
Description=Retire inactive error bodies and unreferenced environments
After=karen-observatory.service
[Service]
Type=oneshot
User={USER}
Group={USER}
WorkingDirectory={ROOT}/current
EnvironmentFile={CONFIG}/service.env
Environment=PYTHONDONTWRITEBYTECODE=1
UMask=0077
ExecStart=/usr/bin/python3 {ROOT}/current/app.py --prune-errors
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={DATA}
MemoryMax=256M
CPUQuota=25%
TimeoutStartSec=10min
"""
files[Path("/etc/systemd/system/karen-observatory-error-retention.timer")] = """[Unit]
Description=Daily error retention after the morning review
[Timer]
OnCalendar=*-*-* 02:30:00 UTC
Persistent=true
RandomizedDelaySec=10m
[Install]
WantedBy=timers.target
"""

if limits["RAW_SUBMISSIONS_LIMIT_BYTES"] <= 0:
    raise SystemExit("Raw submissions limit must be positive")

collector = "karen-observatory.service"
timer_names = ("backup", "raw-archive", "monitor", "error-retention")
timers = [f"karen-observatory-{name}.timer" for name in timer_names]
pause = DATA / "ingest-paused"
if pause.exists():
    raise SystemExit("An existing ingest pause must be resolved before installing")
with ReleaseTransaction([*nginx_files, *files, access_file, password_file, environment, current, pause],
                        [collector, *timers], collector) as deploy:
    for path, content in nginx_files.items():
        deploy.write(path, content, mode=0o600)
    subprocess.run(["nginx", "-t"], check=True)

    for name, timer in zip(timer_names, timers):
        if deploy.old_units[timer][0]:
            deploy.unit("stop", timer)
        job = f"karen-observatory-{name}.service"
        state = deploy.systemctl("show", "-p", "ActiveState", "--value", job, capture_output=True, text=True)
        if state.stdout.strip() in ("active", "activating", "deactivating"):
            raise RuntimeError(f"Wait for the current {name} maintenance job to finish")

    if deploy.old_units[collector][0]:
        deploy.unit("stop", collector)
    backups = CONFIG / "pre-release"
    backups.mkdir(mode=0o700, exist_ok=True)
    database = DATA / "history.sqlite3"
    release_time = time.time_ns()
    saved = backups / f"history-{release_time}.sqlite3"
    database_backup(database, saved)

    deploy.write(pause, "Candidate release readiness check\n", mode=0o644)
    deploy.write(access_file, json.dumps(access, indent=2) + "\n", mode=0o600)
    deploy.write(password_file, access["username"] + ":" + hashed + "\n", mode=0o640,
                 owner=(0, grp.getgrnam("www-data").gr_gid))
    deploy.write(environment, environment_text, mode=0o640, owner=(0, account.pw_gid))
    for path, content in files.items():
        deploy.write(path, content)
    deploy.switch(current, release)
    deploy.reload_units()
    deploy.unit("enable", collector)
    deploy.rollback_database = lambda: rollback_database(database, saved, backups / f"failed-{release_time}.sqlite3")
    # Apply data migrations as the service identity before starting the
    # request-serving unit with its steady-state cgroup resource limits.
    migration_environment = os.environ | dict(line.split("=", 1) for line in environment_text.splitlines())
    migration_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    print("Applying collector database migrations before service start", flush=True)
    subprocess.run(["runuser", "-u", USER, "--", "/usr/bin/python3", str(release / "app.py"), "--migrate-only"],
                   check=True, env=migration_environment)
    deploy.unit("start", collector)
    wait_for_collector()
    verify_endpoints(access)
    deploy.reload_nginx()
    verify_endpoints(access, origin=True)
    deploy.systemctl("start", "karen-observatory-backup.service", check=True)
    deploy.systemctl("start", "karen-observatory-monitor.service", check=True)
    for timer in timers[:-1]:
        deploy.unit("enable", timer)
    # Once timers can mutate the active database and archive, preserve all writes.
    deploy.rollback_database = None
    for timer in timers[:-1]:
        deploy.unit("start", timer)
    if deploy.old_units[timers[-1]][0]:
        deploy.unit("start", timers[-1])
    pause.unlink()
print("Installed and verified collector, protected routes and backups. Error retention uses its existing timer state.")
