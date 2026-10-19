#!/usr/bin/env bash
# One command to run the whole dashboard.
#
#   cd ~/Documents/sdn-rl
#   bash start_dashboard.sh          # starts daemon + backend + frontend
#   (Ctrl+C in this terminal stops all three cleanly)
#
#   bash start_dashboard.sh stop     # from another terminal, if something is stuck
#
# Logs: dashboard_logs/daemon.log, backend.log, frontend.log
set -u
ROOT="${SDN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
cd "$ROOT" || exit 1
LOGDIR="$ROOT/dashboard_logs"
mkdir -p "$LOGDIR"
URL="http://localhost:5173"

stop_all() {
  pkill -f '[u]vicorn main:app' 2>/dev/null
  pkill -f '[h]ttp.server 5173' 2>/dev/null
  # '[l]ab_daemon' cannot match its own command line
  if sudo pgrep -f '[l]ab_daemon.py' >/dev/null 2>&1; then
    sudo pkill -TERM -f '[l]ab_daemon.py' 2>/dev/null
    for _ in $(seq 1 30); do
      sudo pgrep -f '[l]ab_daemon.py' >/dev/null 2>&1 || break
      sleep 1
    done
    # an old daemon (from before the shutdown fix) ignores TERM
    sudo pkill -KILL -f '[l]ab_daemon.py' 2>/dev/null
  fi
  sudo mn -c >/dev/null 2>&1
}

if [[ "${1:-}" == "stop" ]]; then
  echo "Stopping dashboard..."
  stop_all
  echo "Stopped."
  exit 0
fi

# ---- checks -----------------------------------------------------------
[[ -f dashboard/lab/lab_daemon.py ]]       || { echo "Run this from ~/Documents/sdn-rl (dashboard/lab/lab_daemon.py not found)"; exit 1; }
[[ -x dashboard/backend/venv/bin/uvicorn ]] || { echo "Backend venv missing: dashboard/backend/venv/bin/uvicorn"; exit 1; }
grep -q "class Server" dashboard/lab/lab_daemon.py || echo "WARNING: lab_daemon.py is the old version (no shutdown fix). Copy the new one in."
if [[ -f models/anomaly_model.joblib ]]; then
  echo "NOTE: the anomaly model is ON. The Compare tab needs it OFF:"
  echo "      mv models/anomaly_model.joblib models/anomaly_model.joblib.off"
fi

echo "Asking for sudo once (Mininet needs root)..."
sudo -v || exit 1

# ---- clean start ------------------------------------------------------
echo "Clearing anything left over from last time..."
stop_all

# keep sudo's ticket alive while we run
( while true; do sudo -n true 2>/dev/null; sleep 50; done ) &
KEEPALIVE=$!

cleanup() {
  trap - INT TERM EXIT
  echo; echo "Shutting down (Mininet and Ryu are torn down, about 5 s)..."
  kill "$KEEPALIVE" 2>/dev/null
  stop_all
  echo "Done."
  exit 0
}
trap cleanup INT TERM EXIT

# ---- start the three parts -------------------------------------------
sudo python3 dashboard/lab/lab_daemon.py >"$LOGDIR/daemon.log" 2>&1 &

( cd dashboard/backend && exec venv/bin/uvicorn main:app --port 8000 ) >"$LOGDIR/backend.log" 2>&1 &
BACKEND=$!

( cd dashboard/frontend && exec python3 -m http.server 5173 ) >"$LOGDIR/frontend.log" 2>&1 &
FRONTEND=$!

# ---- wait until each answers -----------------------------------------
wait_port() {  # name port pid
  for _ in $(seq 1 40); do
    if (exec 3<>/dev/tcp/127.0.0.1/"$2") 2>/dev/null; then exec 3>&- 3<&-; return 0; fi
    sleep 0.5
  done
  echo "FAILED: $1 did not start. Last log lines:"
  tail -n 15 "$LOGDIR/$1.log"
  return 1
}
wait_port daemon   9191 || exit 1
wait_port backend  8000 || exit 1
wait_port frontend 5173 || exit 1

echo
echo "Dashboard is running:  $URL"
echo "Press Ctrl+C here to stop everything."
command -v xdg-open >/dev/null && [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && \
  (xdg-open "$URL" >/dev/null 2>&1 &)

# stay in the foreground; stop if any part dies
while true; do
  if ! kill -0 "$BACKEND" 2>/dev/null;  then echo "Backend stopped. See $LOGDIR/backend.log";  exit 1; fi
  if ! kill -0 "$FRONTEND" 2>/dev/null; then echo "Frontend stopped. See $LOGDIR/frontend.log"; exit 1; fi
  if ! sudo pgrep -f '[l]ab_daemon.py' >/dev/null 2>&1; then echo "Daemon stopped. See $LOGDIR/daemon.log"; exit 1; fi
  sleep 3
done
