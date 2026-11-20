#!/bin/bash
# Beim Deinstallieren: Gateway beenden, Laufzeitdaten in /dev/shm entfernen.
PIDFILE="/dev/shm/keba_gateway.pid"
if [ -f "$PIDFILE" ]; then
    PID="$(tr -dc '0-9' < "$PIDFILE")"
    if [ -n "$PID" ]; then
        kill -TERM "$PID" 2>/dev/null
        sleep 2
        kill -KILL "$PID" 2>/dev/null
    fi
    rm -f "$PIDFILE"
fi
rm -rf /dev/shm/keba
exit 0
