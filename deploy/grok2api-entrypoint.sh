#!/usr/bin/env sh
set -eu

# Compatibility wrapper for older deployments that still mount this script.
# The current grok2api image already ships its own entrypoint and runs app.main:app.
DATA_DIR="${DATA_DIR:-/app/data}"
LOG_DIR="${LOG_DIR:-/app/logs}"
SERVER_HOST="${SERVER_HOST:-0.0.0.0}"
SERVER_PORT="${SERVER_PORT:-8000}"
SERVER_WORKERS="${SERVER_WORKERS:-1}"
GROK2API_APP_KEY="${GROK2API_APP_KEY:-grok2api}"
GROK2API_API_KEY="${GROK2API_API_KEY:-}"
GROK2API_PROXY_MODE="${GROK2API_PROXY_MODE:-single_proxy}"
GROK2API_BASE_PROXY_URL="${GROK2API_BASE_PROXY_URL:-socks5://warp:1080}"
GROK2API_ASSET_PROXY_URL="${GROK2API_ASSET_PROXY_URL:-$GROK2API_BASE_PROXY_URL}"
GROK2API_CLEARANCE_MODE="${GROK2API_CLEARANCE_MODE:-flaresolverr}"
FLARESOLVERR_URL="${FLARESOLVERR_URL:-http://flaresolverr:8191}"
CF_REFRESH_INTERVAL="${CF_REFRESH_INTERVAL:-600}"
CF_TIMEOUT="${CF_TIMEOUT:-60}"
CONFIG_FILE="${DATA_DIR}/config.toml"

mkdir -p "$DATA_DIR" "$LOG_DIR"

if [ ! -s "$CONFIG_FILE" ]; then
cat >"$CONFIG_FILE" <<EOF
[app]
app_key = "${GROK2API_APP_KEY}"
api_key = "${GROK2API_API_KEY}"

[features]
temporary = true
memory = false
stream = true
thinking = true

[proxy.egress]
mode = "${GROK2API_PROXY_MODE}"
proxy_url = "${GROK2API_BASE_PROXY_URL}"
resource_proxy_url = "${GROK2API_ASSET_PROXY_URL}"

[proxy.clearance]
mode = "${GROK2API_CLEARANCE_MODE}"
flaresolverr_url = "${FLARESOLVERR_URL}"
refresh_interval = ${CF_REFRESH_INTERVAL}
timeout_sec = ${CF_TIMEOUT}
EOF
fi

exec granian --interface asgi --host "${SERVER_HOST}" --port "${SERVER_PORT}" --workers "${SERVER_WORKERS}" app.main:app
