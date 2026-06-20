#!/usr/bin/env bash
# Post-deploy smoke test. Hits the public health endpoint and confirms the UI
# is served. Pass the base URL (default: the production host).
#
#   ./scripts/smoke.sh https://usage.90ten.life
set -euo pipefail

BASE="${1:-https://usage.90ten.life}"
echo "Smoke-testing ${BASE}"

echo -n "  GET /health ... "
health="$(curl -fsS "${BASE}/health")"
echo "${health}"
echo "${health}" | grep -q '"status":"ok"' || { echo "FAIL: health not ok"; exit 1; }

echo -n "  GET / (UI)  ... "
code="$(curl -fsS -o /dev/null -w '%{http_code}' "${BASE}/")"
echo "HTTP ${code}"
[ "${code}" = "200" ] || { echo "FAIL: UI not served"; exit 1; }

echo -n "  TLS cert    ... "
curl -fsS -o /dev/null "${BASE}/health" && echo "valid (HTTPS handshake ok)"

echo "Smoke test passed."
