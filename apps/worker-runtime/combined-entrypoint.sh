#!/usr/bin/env bash
set -Eeuo pipefail

hm_pid=""
proxy_pid=""
console_pid=""
cleanup() {
  for pid in "$console_pid" "$hm_pid" "$proxy_pid"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  wait || true
}
trap cleanup EXIT INT TERM

# Preserve the old grokcli-warp-local-1081 behavior inside this combined container.
# `warp` is the existing WARP service on the shared external Docker network.
socat TCP-LISTEN:1081,fork,reuseaddr TCP:warp:1080 &
proxy_pid=$!

# HM backend remains internal-only; the custom console proxies its admin/API routes via loopback.
export GROK2API_HOST="127.0.0.1"
export GROK2API_PORT="3000"
/app/entrypoint.sh python app.py &
hm_pid=$!

for _ in $(seq 1 90); do
  if curl -fsS -m 1 http://127.0.0.1:3000/admin/api/status >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$hm_pid" 2>/dev/null; then
    wait "$hm_pid"
    exit $?
  fi
  sleep 1
done
if ! curl -fsS -m 2 http://127.0.0.1:3000/admin/api/status >/dev/null 2>&1; then
  echo "HM backend did not become ready" >&2
  exit 1
fi

python /workspace/apps/console/app.py &
console_pid=$!

# Fail the container when either primary service exits; cleanup stops the other.
wait -n "$hm_pid" "$console_pid"
exit $?
