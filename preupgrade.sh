#!/bin/bash
# Vor dem Upgrade (als loxberry): Gateway beenden, Config/Logs/Daten sichern.
PTEMPDIR=$1   # Temp-Ordner der Installation
PFOLDER=$3    # Pluginordner
LBHOME=$5     # LoxBerry-Basisordner

PIDFILE="/dev/shm/keba_gateway.pid"
if [ -f "$PIDFILE" ]; then
    echo "<INFO> Beende laufenden Gateway vor dem Upgrade"
    PID="$(tr -dc '0-9' < "$PIDFILE")"
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        kill -TERM "$PID" 2>/dev/null
        # Warten, bis er Port 7090 freigegeben hat – sonst bindet der neue Gateway ihn nicht
        for _ in $(seq 1 20); do
            kill -0 "$PID" 2>/dev/null || break
            sleep 1
        done
        kill -0 "$PID" 2>/dev/null && kill -KILL "$PID" 2>/dev/null
    fi
    rm -f "$PIDFILE"
fi

BACKUP="/tmp/${PTEMPDIR}_upgrade"
echo "<INFO> Sichere Konfiguration, Logs und Daten nach $BACKUP"
mkdir -p "$BACKUP/config" "$BACKUP/log" "$BACKUP/data"
cp -p -r "$LBHOME/config/plugins/$PFOLDER/" "$BACKUP/config"
cp -p -r "$LBHOME/log/plugins/$PFOLDER/" "$BACKUP/log" 2>/dev/null
cp -p -r "$LBHOME/data/plugins/$PFOLDER/" "$BACKUP/data" 2>/dev/null
exit 0
