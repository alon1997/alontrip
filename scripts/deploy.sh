#!/usr/bin/env bash
# Push local 04app to production after a local pass.
# Usage (from 04app/):
#   bash scripts/deploy.sh
#   bash scripts/deploy.sh --no-sync-db
#
# Writes only /www/wwwroot/trip/ and /www/wwwroot/trip_api/.
# Never copies .env / flask_env / .venv. Never touches Nginx or other sites.
# Never starts trip_api_pymanager.
set -euo pipefail

APP="$(cd "$(dirname "$0")/.." && pwd)"

# T-050: deployment target is private — load it from the local (gitignored)
# .env as DEPLOY_HOST / DEPLOY_SSH_PORT. The public repo never carries it.
ENV_FILE="$APP/.env"
if [ -f "$ENV_FILE" ]; then
  DEPLOY_HOST="${DEPLOY_HOST:-$(grep '^DEPLOY_HOST=' "$ENV_FILE" | cut -d= -f2-)}"
  DEPLOY_SSH_PORT="${DEPLOY_SSH_PORT:-$(grep '^DEPLOY_SSH_PORT=' "$ENV_FILE" | cut -d= -f2-)}"
fi
: "${DEPLOY_HOST:?set DEPLOY_HOST in .env (local only, never committed)}"
: "${DEPLOY_SSH_PORT:?set DEPLOY_SSH_PORT in .env (local only, never committed)}"
HOST="$DEPLOY_HOST"
PORT="$DEPLOY_SSH_PORT"
REMOTE_FRONT=/www/wwwroot/trip
REMOTE_API=/www/wwwroot/trip_api
SSH=(ssh -p "$PORT" -o BatchMode=yes "$HOST")
RSYNC=(rsync -az --exclude .DS_Store -e "ssh -p $PORT -o BatchMode=yes")
SYNC_DB=1
for arg in "$@"; do
  case "$arg" in
    --no-sync-db) SYNC_DB=0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

cd "$APP"

echo "== build frontend =="
(
  cd frontend
  VITE_API_BASE_URL='' npm run build
)

echo "== rsync frontend =="
"${RSYNC[@]}" --delete frontend/dist/ "$HOST:$REMOTE_FRONT/"

echo "== rsync backend app =="
"${RSYNC[@]}" --delete \
  --exclude '__pycache__/' --exclude '*.pyc' \
  backend/app/ "$HOST:$REMOTE_API/backend/app/"
"${RSYNC[@]}" backend/gunicorn_conf.py backend/requirements.txt \
  "$HOST:$REMOTE_API/backend/"

echo "== rsync data + scripts =="
"${SSH[@]}" "mkdir -p $REMOTE_API/data/pois $REMOTE_API/data/lodgings $REMOTE_API/data/transport_hubs $REMOTE_API/data/transit_cache $REMOTE_API/scripts $REMOTE_API/backend/logs"
"${RSYNC[@]}" data/pois/ "$HOST:$REMOTE_API/data/pois/"
"${RSYNC[@]}" data/lodgings/ "$HOST:$REMOTE_API/data/lodgings/"
"${RSYNC[@]}" data/transport_hubs/ "$HOST:$REMOTE_API/data/transport_hubs/"
"${RSYNC[@]}" data/transit_cache/ "$HOST:$REMOTE_API/data/transit_cache/"
"${RSYNC[@]}" --exclude 'deploy.sh' scripts/ "$HOST:$REMOTE_API/scripts/"

echo "== merge API keys (not MYSQL/CORS) =="
python3 - "$APP" <<'PY' | "${SSH[@]}" "python3 $REMOTE_API/scripts/merge_prod_env.py $REMOTE_API/backend/.env"
import json, sys
from pathlib import Path

app = Path(sys.argv[1])
env_path = app / ".env"
if not env_path.is_file():
    env_path = app / "backend" / ".env"
wanted = ("SERPAPI_KEY", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "AMAP_KEY")
vals = {}
for line in env_path.read_text(encoding="utf-8").splitlines():
    raw = line.strip()
    if not raw or raw.startswith("#") or "=" not in raw:
        continue
    key, value = raw.split("=", 1)
    key = key.strip()
    if key in wanted and value.strip():
        vals[key] = value.strip().strip('"').strip("'")
json.dump(vals, sys.stdout)
PY

echo "== chown www =="
"${SSH[@]}" "chown -R www:www $REMOTE_FRONT $REMOTE_API/backend/app $REMOTE_API/data $REMOTE_API/scripts $REMOTE_API/backend/gunicorn_conf.py $REMOTE_API/backend/requirements.txt $REMOTE_API/backend/logs $REMOTE_API/backend/.env
chmod 640 $REMOTE_API/backend/.env"

if [ "$SYNC_DB" = 1 ]; then
  echo "== upsert pois into hackathontrip =="
  "${SSH[@]}" "cd $REMOTE_API && backend/flask_env/bin/python scripts/sync_pois_from_json.py"
fi

echo "== restart uvicorn as www =="
"${SSH[@]}" -n "bash -c '
set -e
PIDS=\$(ps -eo pid,user,cmd | awk \"/www/ && /uvicorn app.main:app/ && !/awk/ {print \\\$1}\")
if [ -n \"\${PIDS:-}\" ]; then
  kill \$PIDS || true
  sleep 1
fi
install -d -o www -g www /www/wwwroot/trip_api/backend/logs
setsid sudo -u www bash -c \"cd /www/wwwroot/trip_api/backend && exec nohup flask_env/bin/uvicorn app.main:app --host 127.0.0.1 --port 5003 >> logs/uvicorn.out 2>&1\" </dev/null >/dev/null 2>&1 &
sleep 1
ss -tlnp | grep 5003 || { echo \"5003 not listening\" >&2; exit 1; }
ps -eo user,pid,cmd | awk \"/www/ && /uvicorn app.main:app/ && !/awk/\"
'"

echo "== health =="
curl -sS https://alonuniverse.com/api/trip/health
echo
echo "deploy ok"
