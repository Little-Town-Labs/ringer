#!/usr/bin/env bash
# T006 check. Run from the repo root. Needs docker and a python with psycopg.
# Throwaway Postgres only; never touches aegis-prod. Prints why it fails.
set -uo pipefail
PY="${RINGER_CHECK_PYTHON:-python3}"
D=scripts/central-evidence
fail=0
say() { echo "FAIL: $*"; fail=1; }

for f in schema.sql compose.yaml backup.sh README.md; do [ -s "$D/$f" ] || say "missing or empty $D/$f"; done
[ -s tests/test_central_schema.py ] || say "missing tests/test_central_schema.py"
[ $fail -eq 0 ] || exit 1

bash -n "$D/backup.sh" || say "backup.sh has a syntax error"
if grep -rn -E "100\.100\.|tail5c4f73|aegis|powerbox|ts\.net" "$D" tests/test_central_schema.py; then
  say "environment-specific value found in shipped files (see lines above)"
fi
diff <(grep -v '^[[:space:]]*--' "$D/schema.sql") \
     <(grep -v '^[[:space:]]*--' specs/002-shared-evidence/schema.sql) >/tmp/t006-schema.diff \
  || { say "schema.sql body differs from specs/002-shared-evidence/schema.sql (comments may differ, SQL must not):"; head -20 /tmp/t006-schema.diff; }
RINGER_OWNER_PW=x RINGER_BIND_ADDR=127.0.0.1 docker compose -f "$D/compose.yaml" config -q 2>&1 \
  || say "compose.yaml does not validate with RINGER_OWNER_PW and RINGER_BIND_ADDR set"
grep -q 'RINGER_BIND_ADDR' "$D/compose.yaml" || say "compose.yaml must take the bind address from RINGER_BIND_ADDR"

echo "-- without RINGER_TEST_PG_DSN the schema test must skip, not fail"
env -u RINGER_TEST_PG_DSN python3 -m unittest tests.test_central_schema 2>&1 | tail -4
env -u RINGER_TEST_PG_DSN python3 -m unittest tests.test_central_schema >/dev/null 2>&1 || say "tests.test_central_schema fails without a database (it must skip)"

echo "-- with a throwaway Postgres"
N="ringer-t006-$$"
cleanup() { docker rm -f "$N" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker run -d --rm --name "$N" --security-opt apparmor=unconfined -p 127.0.0.1::5432 \
  -e POSTGRES_PASSWORD=t -e POSTGRES_DB=ringer postgres:16-alpine >/dev/null || { say "could not start throwaway postgres"; exit 1; }
for _ in $(seq 1 40); do docker exec "$N" pg_isready -U postgres -d ringer >/dev/null 2>&1 && break; sleep 1; done
sleep 2
PORT=$(docker port "$N" 5432 | head -1 | sed 's/.*://')
export RINGER_TEST_PG_DSN="postgresql://postgres:t@127.0.0.1:${PORT}/ringer"
$PY -m unittest tests.test_central_schema -v 2>&1 | tail -40
$PY -m unittest tests.test_central_schema >/tmp/t006-integration.log 2>&1 || { say "schema integration test failed against throwaway postgres"; tail -40 /tmp/t006-integration.log; }
grep -q "skipped" /tmp/t006-integration.log && say "integration test was skipped even though RINGER_TEST_PG_DSN was set (is psycopg importable by $PY?)"
[ $fail -eq 0 ] && echo "PASS: assets present, no environment-specific values, schema matches, compose valid, integration test passed on a real Postgres"
exit $fail
