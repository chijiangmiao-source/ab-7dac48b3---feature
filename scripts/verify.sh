#!/bin/sh
# Acceptance gate for the Compose `verify` service.
# Runs build checks, the unit/integration suite, then the HTTP smoke test
# against the running web service. Exits non-zero if anything fails.
set -eu

# Project root is the parent of this script's directory (container: /srv).
cd "$(dirname "$0")/.."

PYTHON="$(command -v python || command -v python3)"

echo "== [1/3] build check: compileall =="
"$PYTHON" -m compileall -q app scripts tests

echo "== [2/3] unit + integration tests =="
"$PYTHON" -m unittest discover -s tests -v

echo "== [3/3] HTTP smoke against ${WEB_URL:-http://web:8080} =="
"$PYTHON" scripts/smoke.py "${WEB_URL:-http://web:8080}"

echo
echo "VERIFY PASSED"
