#!/usr/bin/env bash
# Upload the locally verified app to alonuniverse.com/trip.
# Does not rebuild nginx unless --nginx is passed.
#
#   ./scripts/deploy.sh
#   ./scripts/deploy.sh --nginx

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${DEPLOY_HOST:-root@REDACTED_SERVER_HOST}"
PORT="${DEPLOY_PORT:-REDACTED_SSH_PORT}"
SSH=(ssh -o BatchMode=yes -p "$PORT" "$HOST")
RSYNC=(rsync -az -e "ssh -o BatchMode=yes -p $PORT")
REMOTE_API=/www/wwwroot/trip_api
REMOTE_WEB=/www/wwwroot/trip
NGINX_LOCAL="$ROOT/../../../alonuniverse/服务器连接/alonuniverse.com.conf"
NGINX_REMOTE=/www/server/panel/vhost/nginx/alonuniverse.com.conf
WITH_NGINX=0

for arg in "$@"; do
  case "$arg" in
    --nginx) WITH_NGINX=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 1 ;;
  esac
done

echo "==> production frontend build (same-origin API)"
(
  cd "$ROOT/frontend"
  VITE_API_BASE_URL='' npm run build
)

echo "==> sync backend + data"
"${RSYNC[@]}" \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  "$ROOT/backend/" "$HOST:$REMOTE_API/backend/"
"${RSYNC[@]}" "$ROOT/backend/requirements.txt" "$HOST:$REMOTE_API/requirements.txt"
"${RSYNC[@]}" -r \
  --exclude 'poi_serpapi_cache/' \
  --exclude 'osm_fee_cache/' \
  --exclude 'lodging_serpapi_cache/' \
  "$ROOT/data/" "$HOST:$REMOTE_API/data/"

echo "==> sync frontend dist"
"${RSYNC[@]}" "$ROOT/frontend/dist/" "$HOST:$REMOTE_WEB/"

echo "==> restart API (宝塔 Python 项目 trip_api)"
"${SSH[@]}" "/etc/init.d/trip_api_pymanager restart && sleep 1 && curl -fsS http://127.0.0.1:5003/api/trip/health"

if [[ "$WITH_NGINX" -eq 1 ]]; then
  echo "==> nginx (backup, test, reload)"
  stamp="$(date +%Y%m%d%H%M%S)"
  scp -P "$PORT" "$NGINX_LOCAL" "$HOST:/tmp/alonuniverse.com.conf.trip"
  "${SSH[@]}" "cp -a $NGINX_REMOTE $NGINX_REMOTE.bak-trip-$stamp && cp /tmp/alonuniverse.com.conf.trip $NGINX_REMOTE && nginx -t && nginx -s reload && rm -f /tmp/alonuniverse.com.conf.trip"
fi

echo "==> smoke"
"${SSH[@]}" "curl -fsS http://127.0.0.1:5003/api/trip/health"
echo
echo "done. public: https://alonuniverse.com/trip/"
