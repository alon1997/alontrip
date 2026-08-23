#!/bin/bash
# 在服务器上执行：按宝塔 Python 项目管理器登记 trip_api（和染迷/学习时钟同一套可见）。
set -euo pipefail

API=/www/wwwroot/trip_api
BACKEND=$API/backend
VENV=$BACKEND/flask_env
PJ=trip_api
CONF=/www/server/panel/plugin/pythonmamager/config.json
ENVJSON=/www/server/panel/data/python_project_env.json

mkdir -p "$BACKEND/logs" /www/wwwroot/trip
chown -R www:www /www/wwwroot/trip "$API"

if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q -U pip
"$VENV/bin/pip" install -q -r "$API/requirements.txt"

cat > "$BACKEND/.env" << 'EOF'
SERPAPI_KEY=
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com/anthropic
APP_PORT=5003
CORS_ORIGINS=https://alonuniverse.com,https://www.alonuniverse.com
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=hackathontrip
MYSQL_USER=hackathontrip
MYSQL_PASSWORD=
EOF
chmod 640 "$BACKEND/.env"
chown www:www "$BACKEND/.env"

python3 << 'PY'
import json, os, pwd, sys
sys.path.insert(0, "/www/server/panel/class")
os.chdir("/www/server/panel")
from plugin.pythonmamager.pythonmamager_main import pythonmamager_main

pj = {
    "pjname": "trip_api",
    "version": "3.10.12",
    "rfile": "/www/wwwroot/trip_api/backend/app/main.py",
    "path": "/www/wwwroot/trip_api/backend",
    "vpath": "/www/wwwroot/trip_api/backend/flask_env",
    "status": "0",
    "port": "5003",
    "rtype": "gunicorn",
    "proxy": "",
    "framework": "sanic",
    "auto_start": "1",
    "user": "www",
    "parm": "",
    "log_path": "/www/wwwroot/trip_api/backend/logs",
    "is_supervisor": 0,
    "numprocs": "1",
    "supervisor_name": "",
}

conf_path = "/www/server/panel/plugin/pythonmamager/config.json"
conf = json.loads(open(conf_path).read() or "[]")
conf = [c for c in conf if c.get("pjname") != "trip_api"]
conf.append(pj)
open(conf_path, "w").write(json.dumps(conf, ensure_ascii=False, indent=2))

env_path = "/www/server/panel/data/python_project_env.json"
env = json.loads(open(env_path).read())
bin_path = "/www/wwwroot/trip_api/backend/flask_env/bin/python"
if not any(e.get("bin_path") == bin_path for e in env.get("environments", [])):
    env.setdefault("environments", []).append({
        "bin_path": bin_path,
        "version": "Python 3.10.12",
        "type": "venv",
        "conda_path": "",
        "venv_name": "flask_env",
        "activate_sh": "source /www/wwwroot/trip_api/backend/flask_env/bin/activate",
        "system_path": "/usr/bin/python3.10",
        "ps": "hackathon trip_api",
        "site_packages": "/www/wwwroot/trip_api/backend/flask_env/lib/python3.10/site-packages",
    })
    open(env_path, "w").write(json.dumps(env, ensure_ascii=False, indent=2))

m = pythonmamager_main()
psh = m.get_pj_sh(pj, change_project=True)
m.create_system_file(pj["pjname"], psh.start, psh.stop, cover=True)
m._set_sys_auto_start(pj["pjname"], "1")
ok = m._start_project(pj)
print("start_ok", ok)
print("check", os.popen(psh.check).read().strip())
PY

chown -R www:www "$API" /www/wwwroot/trip
sleep 2
curl -fsS http://127.0.0.1:5003/api/trip/health
echo
echo "宝塔：网站 → Python项目 应出现 trip_api"
