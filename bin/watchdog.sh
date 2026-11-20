#!/bin/bash
# KEBA-Gateway-Watchdog, jede Minute aus cron.01min (läuft als loxberry).
#
#  - Flag config/gateway_stopped gesetzt (Stopp in der WebUI) → nichts tun.
#  - Gateway-PID tot, oder Heartbeat älter als 180 s → Prozess beenden, neu starten.
#  - Mehr als 5 Neustarts in 30 min → aufgeben (bis das Fenster abgelaufen ist).
#
# Pfade lassen sich für Tests über KEBA_* überschreiben.

SELF="$(readlink -f "$0")"
FOLDER="$(basename "$(dirname "$SELF")")"
if [ -z "$LBHOMEDIR" ]; then
    LBHOMEDIR="${SELF%/bin/plugins/*}"
fi

SHMDIR="${KEBA_SHMDIR:-/dev/shm/keba}"
PIDFILE="${KEBA_PIDFILE:-/dev/shm/keba_gateway.pid}"
DAEMON="${KEBA_DAEMON:-$LBHOMEDIR/system/daemons/plugins/$FOLDER}"
FLAG="${KEBA_FLAG:-$LBHOMEDIR/config/plugins/$FOLDER/gateway_stopped}"
NOW="${KEBA_NOW:-$(date +%s)}"

STALE=180
MAX_RESTARTS=5
WINDOW=1800
RESTARTS="$SHMDIR/watchdog.restarts"
GAVEUP="$SHMDIR/watchdog.gaveup"

[ -f "$FLAG" ] && exit 0
mkdir -p "$SHMDIR"

PID=""
ALIVE=0
if [ -f "$PIDFILE" ]; then
    PID="$(tr -dc '0-9' < "$PIDFILE")"
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        ALIVE=1
    fi
fi

if [ "$ALIVE" = "1" ]; then
    HB=""
    if [ -f "$SHMDIR/heartbeat" ]; then
        HB="$(tr -dc '0-9' < "$SHMDIR/heartbeat")"
    fi
    # Noch kein Heartbeat (Gateway startet gerade): Alter der PID-Datei zählt.
    [ -z "$HB" ] && HB="$(stat -c %Y "$PIDFILE" 2>/dev/null || echo "$NOW")"
    if [ $((NOW - HB)) -le $STALE ]; then
        rm -f "$GAVEUP"
        exit 0
    fi
    REASON="Heartbeat seit $((NOW - HB)) s nicht erneuert"
else
    REASON="Gateway läuft nicht"
fi

# Neustart-Budget: nur Einträge der letzten 30 min zählen.
RECENT=""
if [ -f "$RESTARTS" ]; then
    while read -r T; do
        case "$T" in ''|*[!0-9]*) continue ;; esac
        [ $((NOW - T)) -lt $WINDOW ] && RECENT="$RECENT$T"$'\n'
    done < "$RESTARTS"
fi
printf '%s' "$RECENT" > "$RESTARTS"
COUNT="$(printf '%s' "$RECENT" | grep -c .)"

if [ "$COUNT" -ge "$MAX_RESTARTS" ]; then
    if [ ! -f "$GAVEUP" ]; then
        logger "KEBA: Watchdog gibt auf – $COUNT Neustarts in 30 min ($REASON)"
        echo "$NOW" > "$GAVEUP"
    fi
    exit 0
fi
rm -f "$GAVEUP"

if [ "$ALIVE" = "1" ]; then
    kill -TERM "$PID" 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$PID" 2>/dev/null || break
        sleep 1
    done
    kill -0 "$PID" 2>/dev/null && kill -KILL "$PID" 2>/dev/null
fi
rm -f "$PIDFILE"

echo "$NOW" >> "$RESTARTS"
logger "KEBA: Watchdog startet Gateway neu ($REASON)"
"$DAEMON" start
exit 0
