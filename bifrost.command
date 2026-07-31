#!/bin/bash
# Bifrost — double-click to run.
#
# Starts both halves (API + UI) and opens the browser. It does NOT start Ragnarok:
# Bifrost asks which Ragnarok checkout to use on first run, remembers it, and
# launches it from the app. So this script only ever has to bring up Bifrost.
#
# Written to be run by someone who does not use a terminal: every failure prints
# what to do about it, and the window stays open so the message can be read.
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

VENV_DIR=".venv"
REQ_HASH_FILE="$VENV_DIR/.req_hash"
# Bifrost's own ports. Not 5173 (any other Vite project) and not 8000-something
# (Ragnarok itself) — an adjacent pair outside the common dev defaults.
API_PORT="${BIFROST_API_PORT:-8770}"
UI_PORT="${BIFROST_UI_PORT:-8771}"
# Exported so vite.config.ts derives its proxy target from the SAME value the API is
# started with; a literal there would break the UI whenever these are overridden.
export BIFROST_API_PORT="$API_PORT"
export BIFROST_UI_PORT="$UI_PORT"

# ── Failure reporting ─────────────────────────────────────────────────────────
# A double-clicked script whose window vanishes on error tells the user nothing,
# so hold it open and say what to fix.
die() {
  echo ""
  echo "──────────────────────────────────────────────────────────────"
  echo "  Bifrost could not start."
  echo ""
  echo "  $1"
  echo "──────────────────────────────────────────────────────────────"
  echo ""
  read -r -p "Press Return to close this window. " _ || true
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "$2"
}

# ── Dependencies ──────────────────────────────────────────────────────────────
need_cmd npm "Node.js is not installed. Get it from https://nodejs.org, then run this again."

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 \
     && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
    PYTHON="$candidate"; break
  fi
done
[ -n "$PYTHON" ] || die "Python 3.11 or newer is not installed. Get it from https://www.python.org/downloads/, then run this again."

# ── Backend environment ───────────────────────────────────────────────────────
# Rebuilt only when requirements.txt changes, so a normal start is instant. The
# hash is stored inside the venv, so deleting the venv also forgets it.
if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "Setting up Bifrost (first run — this takes a minute)…"
  rm -rf "$VENV_DIR"
  "$PYTHON" -m venv "$VENV_DIR" || die "Could not create a Python environment in $ROOT_DIR/$VENV_DIR."
fi

REQ_HASH="$(shasum requirements.txt 2>/dev/null | awk '{print $1}')"
if [ ! -f "$REQ_HASH_FILE" ] || [ "$(cat "$REQ_HASH_FILE" 2>/dev/null)" != "$REQ_HASH" ]; then
  echo "Installing Python dependencies…"
  "$VENV_DIR/bin/python" -m pip install --upgrade pip --quiet
  "$VENV_DIR/bin/python" -m pip install -r requirements.txt --quiet \
    || die "Python dependencies failed to install. Check your internet connection and try again."
  # The agent SDK is optional: without it the app still runs and the chat reports
  # a setup state rather than failing, so a missing SDK must not abort the launch.
  "$VENV_DIR/bin/python" -m pip install -r requirements-agent.txt --quiet 2>/dev/null \
    || echo "  (the agent SDK did not install — Bifrost will run, and the chat will explain what is missing)"
  echo "$REQ_HASH" > "$REQ_HASH_FILE"
fi

# ── Frontend dependencies ─────────────────────────────────────────────────────
if [ ! -d "frontend/node_modules" ]; then
  echo "Installing interface dependencies…"
  ( cd frontend && npm install --silent ) || die "Interface dependencies failed to install. Check your internet connection and try again."
fi

# ── Ports ─────────────────────────────────────────────────────────────────────
port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
if port_busy "$API_PORT"; then
  die "Port $API_PORT is already in use — Bifrost may already be running. Close it, or set BIFROST_API_PORT to a free port."
fi
if port_busy "$UI_PORT"; then
  die "Port $UI_PORT is already in use. Close whatever is using it, or set BIFROST_UI_PORT to a free port."
fi

# ── Run both halves ───────────────────────────────────────────────────────────
# One trap on the script's own exit stops both, so closing the window (or Ctrl-C)
# does not leave an orphaned server holding a port the next run needs.
API_PID=""
UI_PID=""
cleanup() {
  echo ""
  echo "Stopping Bifrost…"
  [ -n "$UI_PID" ] && kill "$UI_PID" 2>/dev/null
  [ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null
  wait 2>/dev/null
}
trap cleanup EXIT INT TERM

mkdir -p backend/data/logs

echo "Starting Bifrost API on http://127.0.0.1:$API_PORT …"
"$VENV_DIR/bin/python" -m uvicorn bifrost.main:app --host 127.0.0.1 --port "$API_PORT" \
  >> backend/data/logs/bifrost-api.log 2>&1 &
API_PID=$!

# Fail fast and loudly if the API died on startup, rather than opening a browser
# at a page that cannot load anything.
for _ in $(seq 1 40); do
  sleep 0.25
  curl -fsS "http://127.0.0.1:$API_PORT/api/health" >/dev/null 2>&1 && break
done
if ! curl -fsS "http://127.0.0.1:$API_PORT/api/health" >/dev/null 2>&1; then
  echo ""
  echo "The API did not come up. Its last lines:"
  tail -n 25 backend/data/logs/bifrost-api.log 2>/dev/null || true
  die "See backend/data/logs/bifrost-api.log for the full output."
fi

echo "Starting the interface on http://localhost:$UI_PORT …"
( cd frontend && npm run dev ) \
  >> backend/data/logs/bifrost-ui.log 2>&1 &
UI_PID=$!

for _ in $(seq 1 60); do
  sleep 0.25
  curl -fsS "http://localhost:$UI_PORT/" >/dev/null 2>&1 && break
done

echo ""
echo "──────────────────────────────────────────────────────────────"
echo "  Bifrost is running:  http://localhost:$UI_PORT"
echo ""
echo "  On first run it will ask where your Ragnarok folder is,"
echo "  then start the engine for you."
echo ""
echo "  Leave this window open. Close it to stop Bifrost."
echo "──────────────────────────────────────────────────────────────"
echo ""

command -v open >/dev/null 2>&1 && open "http://localhost:$UI_PORT" || true

# Wait on either half; if one dies, the trap takes the other down with it.
wait -n "$API_PID" "$UI_PID" 2>/dev/null || wait "$API_PID" 2>/dev/null
echo ""
echo "Bifrost stopped."
