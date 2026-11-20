#!/bin/bash
# Nach dem Upgrade (als loxberry): Sicherung zurückspielen, Gateway starten.
PTEMPDIR=$1
PFOLDER=$3
LBHOME=$5
BACKUP="/tmp/${PTEMPDIR}_upgrade"

if [ -d "$BACKUP/config/$PFOLDER" ]; then
    echo "<INFO> Stelle Konfiguration wieder her"
    # Spiegeln statt mischen: nur gesicherte Dateien bleiben (auch das gateway_stopped-Flag).
    rsync -a --delete "$BACKUP/config/$PFOLDER/" "$LBHOME/config/plugins/$PFOLDER/"
fi
[ -d "$BACKUP/log/$PFOLDER" ] && cp -p -r "$BACKUP/log/$PFOLDER/." "$LBHOME/log/plugins/$PFOLDER/"
[ -d "$BACKUP/data/$PFOLDER" ] && cp -p -r "$BACKUP/data/$PFOLDER/." "$LBHOME/data/plugins/$PFOLDER/"
rm -rf "$BACKUP"
chmod 600 "$LBHOME/config/plugins/$PFOLDER/pluginconfig.json" 2>/dev/null

DAEMON="$LBHOME/system/daemons/plugins/$PFOLDER"
if [ -f "$LBHOME/config/plugins/$PFOLDER/gateway_stopped" ]; then
    echo "<INFO> gateway_stopped gesetzt – Gateway bleibt aus"
elif [ -x "$DAEMON" ]; then
    LBHOMEDIR="$LBHOME" "$DAEMON" start
    echo "<OK> Gateway gestartet"
else
    echo "<WARNING> Daemon-Skript fehlt ($DAEMON) – Gateway startet beim nächsten Boot"
fi
exit 0
