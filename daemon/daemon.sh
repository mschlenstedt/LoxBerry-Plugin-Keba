#!/bin/bash
# KEBA-Gateway: start | stop | restart | status
#
# Der Core legt diese Datei als system/daemons/plugins/<NAME> ab und ruft sie beim
# Boot als root auf. Der Gateway läuft immer als loxberry, damit PID-Datei, Logs und
# WebUI (ajax.cgi) denselben Besitzer haben.
#
#   start    startet, außer config/gateway_stopped ist gesetzt (Stopp aus der WebUI
#            überlebt so einen Neustart der Box). Entfernt das Flag nicht.
#   stop     beendet den Gateway und setzt das Flag.
#   restart  beendet den Gateway und startet neu (Flag bleibt unberührt).
if [ "$(id -u)" = "0" ]; then
    exec su loxberry --preserve-environment -c "$0 $*"
fi

ACTION="${1:-start}"
SELF="$(readlink -f "$0")"
PLUGIN_FOLDER="$(basename "$SELF")"
if [ -z "$LBHOMEDIR" ]; then
    LBHOMEDIR="${SELF%/system/daemons/*}"
fi

LBPBINDIR="$LBHOMEDIR/bin/plugins/$PLUGIN_FOLDER"
LBPCONFIGDIR="$LBHOMEDIR/config/plugins/$PLUGIN_FOLDER"
LBPLOGDIR="$LBHOMEDIR/log/plugins/$PLUGIN_FOLDER"
GATEWAY="$LBPBINDIR/keba_gateway.py"
PIDFILE="/dev/shm/keba_gateway.pid"
STOPPED_FLAG="$LBPCONFIGDIR/gateway_stopped"

kill_gateway() {
    if [ -f "$PIDFILE" ]; then
        PID="$(tr -dc '0-9' < "$PIDFILE")"
        if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
            kill -TERM "$PID" 2>/dev/null
            for _ in 1 2 3 4 5 6 7 8 9 10; do
                kill -0 "$PID" 2>/dev/null || break
                sleep 1
            done
            kill -0 "$PID" 2>/dev/null && kill -KILL "$PID" 2>/dev/null
        fi
        rm -f "$PIDFILE"
    fi
    # Waisen ohne PID-Datei
    pkill -KILL -u "$(id -u)" -f "$GATEWAY" 2>/dev/null
}

start_gateway() {
    if [ ! -f "$GATEWAY" ]; then
        logger "KEBA: Gateway nicht gefunden ($GATEWAY)"
        exit 1
    fi
    if [ -f "$PIDFILE" ] && kill -0 "$(tr -dc '0-9' < "$PIDFILE")" 2>/dev/null; then
        logger "KEBA: Gateway läuft bereits"
        return
    fi
    mkdir -p "$LBPLOGDIR"
    # Log-Session in Perl anlegen (package = Pluginordner, logdir ausdrücklich),
    # Dateiname, dbkey und Loglevel an Python weiterreichen.
    read -r LOGFILE LOGDBKEY LOGLEVEL < <(perl -e "
        use LoxBerry::Log;
        my \$log = LoxBerry::Log->new(name => 'gateway', package => '$PLUGIN_FOLDER',
                                      logdir => '$LBPLOGDIR', addtime => 1);
        \$log->LOGSTART('KEBA-Gateway startet');
        print \$log->{filename} . ' ' . (\$log->{dbkey} // 0) . ' ' . (\$log->loglevel // 7) . \"\n\";
    " 2>/dev/null)
    if [ -z "$LOGFILE" ]; then
        LOGFILE="$LBPLOGDIR/keba_gateway.log"
        LOGDBKEY="0"
    fi
    [ -z "$LOGLEVEL" ] && LOGLEVEL=7

    setsid python3 "$GATEWAY" \
        --configdir "$LBPCONFIGDIR" \
        --logfile   "$LOGFILE" \
        --logdbkey  "$LOGDBKEY" \
        --loglevel  "$LOGLEVEL" \
        </dev/null >>"$LOGFILE" 2>&1 &
    logger "KEBA: Gateway gestartet (PID $!)"
}

case "$ACTION" in
    start)
        if [ -f "$STOPPED_FLAG" ]; then
            logger "KEBA: gateway_stopped gesetzt – Gateway wird nicht gestartet"
            exit 0
        fi
        start_gateway
        ;;
    stop)
        touch "$STOPPED_FLAG"
        kill_gateway
        logger "KEBA: Gateway gestoppt"
        ;;
    restart)
        kill_gateway
        start_gateway
        ;;
    status)
        if [ -f "$PIDFILE" ] && kill -0 "$(tr -dc '0-9' < "$PIDFILE")" 2>/dev/null; then
            echo "running $(cat "$PIDFILE")"
        else
            echo "stopped"
        fi
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
exit 0
