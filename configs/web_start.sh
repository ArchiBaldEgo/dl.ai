# configs/start_web.sh
#  Used in Dockerfile to start web service.

set -euo pipefail

# Local-mode bootstrap: migrations + collectstatic + puppeteer host-cache copy
if [ "${AI_BOOTSTRAP_ON_START:-0}" = "1" ] \
   || { [ -z "${HTTP_PROXY:-}" ] && [ -z "${HTTPS_PROXY:-}" ]; }; then
    tries=0
    until python manage.py migrate --noinput; do
        tries=$((tries + 1))
        if [ "$tries" -ge 30 ]; then
            echo "migrate failed (db not ready?)" >&2
            exit 1
        fi
        echo "db not ready, retry migrate in 2s ($tries/30)"
        sleep 2
    done
    python manage.py collectstatic --noinput
    if [ ! -e /opt/puppeteer-runtime/chrome ] && [ -d /host-puppeteer-cache/chrome ]; then
        echo "chrome-host-cache-copy"
        cp -r /host-puppeteer-cache/. /opt/puppeteer-runtime/
    fi
fi

# Update log (best-effort; reads git log, needs .git to be present via bind mount)
python manage.py sync_update_log || true

# Bot pools and web server
node /app/src/web/WebPools/api/index.js --site=deepseek &
node /app/src/web/WebPools/api/index.js --site=kimi &
daphne -b 0.0.0.0 -p 8000 DjangoTest.asgi:application &
wait -n