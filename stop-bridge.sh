#!/usr/bin/env bash
# Handoff Bridge Console stopper (ZC 2026-11-07; used by multi-ai/stop-bridge.cmd)
PIDFILE="$HOME/multi-ai-data/bridge-console.pid"
if [ ! -f "$PIDFILE" ]; then
  echo "no pidfile - server not running (or not started via start-bridge.cmd)"
  exit 0
fi
PID=$(cat "$PIDFILE")
if [ ! -d "/proc/$PID" ]; then
  echo "stale pidfile (process $PID already gone); removing"
  rm -f "$PIDFILE"
  exit 0
fi
echo "stopping pid $PID (SIGTERM)"
kill -TERM "$PID"
for i in 1 2 3 4 5 6 7 8; do
  sleep 1
  [ -d "/proc/$PID" ] || break
done
if [ -d "/proc/$PID" ]; then
  echo "still alive after 8s, sending SIGKILL"
  kill -9 "$PID"
else
  echo "exited cleanly"
fi
rm -f "$PIDFILE"
