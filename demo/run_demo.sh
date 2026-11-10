#!/usr/bin/env bash
# One-command local demo:
#   - the ORIGINAL Conduit website on Postgres        -> http://localhost:3000
#   - the SAME website on the MongoDB backend         -> http://localhost:3100
#   - the pipeline dashboard (Streamlit)              -> http://localhost:8501
# Both backends start from identical data: Postgres is seeded deterministically, then migrated to MongoDB.
# Log in with any seeded user, e.g.  user1@example.com / password123
# Ctrl+C stops everything.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
LOG="$ROOT/logs/demo"
mkdir -p "$LOG"
PIDS=()
cleanup() {
  trap - EXIT INT TERM
  echo; echo "stopping..."
  for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; done
  for port in 3000 3001 3002 3100 8501; do fuser -k "$port/tcp" >/dev/null 2>&1 || true; done
}
trap cleanup EXIT INT TERM

echo "[1/5] databases (docker compose)"
docker compose up -d --wait >/dev/null

echo "[2/5] original backend (Sequelize + Postgres) on :3001"
for port in 3000 3001 3002 3100 8501; do fuser -k "$port/tcp" >/dev/null 2>&1 || true; done
(cd vendor/original/backend && PORT=3001 JWT_KEY=amp_local_dev_jwt_key DEV_DB_USERNAME=conduit DEV_DB_PASSWORD=conduit \
  DEV_DB_NAME=conduit DEV_DB_HOSTNAME=127.0.0.1 DEV_DB_DIALECT=postgres exec node index.js >"$LOG/original.log" 2>&1) &
PIDS+=($!)
until curl -sf localhost:3001/ >/dev/null; do sleep 0.5; done
sleep 2  # let sequelize.sync finish

echo "[3/5] seed Postgres (1000 articles) and migrate to MongoDB"
"$PY" -m pipeline.seed --articles 1000 >/dev/null
"$PY" -c "from pymongo import MongoClient; from pipeline.config import settings; MongoClient(settings.mongo_uri).drop_database('conduit')"
"$PY" -m pipeline.migrator --db conduit --fresh | grep -E '"rows_written"|"rows_per_second"'

echo "[4/5] MongoDB backend (Mongoose) on :3002 + both websites"
(cd apps/mongo-backend && PORT=3002 JWT_KEY=amp_local_dev_jwt_key MONGO_DB=conduit exec node index.js >"$LOG/mongo.log" 2>&1) &
PIDS+=($!)
until curl -sf localhost:3002/ >/dev/null; do sleep 0.5; done
VITE="$ROOT/vendor/original/node_modules/.bin/vite"
(PORT=3000 BACKEND=http://localhost:3001 exec "$VITE" --config demo/vite.config.mjs >"$LOG/web_postgres.log" 2>&1) &
PIDS+=($!)
(PORT=3100 BACKEND=http://localhost:3002 exec "$VITE" --config demo/vite.config.mjs >"$LOG/web_mongo.log" 2>&1) &
PIDS+=($!)

echo "[5/5] dashboard"
(exec "$ROOT/.venv/bin/streamlit" run demo/dashboard.py --server.headless true --server.port 8501 >"$LOG/dashboard.log" 2>&1) &
PIDS+=($!)
until curl -sf localhost:3100/ >/dev/null && curl -sf localhost:3000/ >/dev/null; do sleep 0.5; done

cat <<EOF

  Conduit on MongoDB (modernized):   http://localhost:3100
  Conduit on Postgres (original):    http://localhost:3000
  Pipeline dashboard:                http://localhost:8501

  Log in as user1@example.com / password123 (any userN@example.com works)
  Logs: logs/demo/   -   Ctrl+C to stop
EOF
# stay up while the services run (Ctrl+C stops everything)
while kill -0 "${PIDS[0]}" 2>/dev/null && kill -0 "${PIDS[1]}" 2>/dev/null; do sleep 2 & wait $!; done  # wait (not sleep) is interrupted immediately by Ctrl+C
echo "a backend exited - see logs/demo/"
