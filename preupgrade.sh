#!/bin/bash
# Vor dem Upgrade (als loxberry): Gateway beenden, Config/Logs/Daten sichern.
PTEMPDIR=$1   # Temp-Ordner der Installation
PFOLDER=$3    # Pluginordner
LBHOME=$5     # LoxBerry-Basisordner

PIDFILE="/dev/shm/keba_gateway.pid"
if [ -f "$PIDFILE" ]; then
    echo "<INFO> Beende laufenden Gateway vor dem Upgrade"
    kill "$(tr -dc '0-9' < "$PIDFILE")" 2>/dev/null
    rm -f "$PIDFILE"
fi

BACKUP="/tmp/${PTEMPDIR}_upgrade"
echo "<INFO> Sichere Konfiguration, Logs und Daten nach $BACKUP"
mkdir -p "$BACKUP/config" "$BACKUP/log" "$BACKUP/data"
cp -p -r "$LBHOME/config/plugins/$PFOLDER/" "$BACKUP/config"
cp -p -r "$LBHOME/log/plugins/$PFOLDER/" "$BACKUP/log" 2>/dev/null
cp -p -r "$LBHOME/data/plugins/$PFOLDER/" "$BACKUP/data" 2>/dev/null
exit 0
