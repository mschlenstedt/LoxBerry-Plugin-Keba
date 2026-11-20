#!/bin/bash
# Läuft als root nach dem Kopieren der Plugin-Dateien: Python-Abhängigkeiten.
echo "<INFO> KEBA: installiere Python-Pakete (aiohttp, aiomqtt, paho-mqtt)"
PKGS='aiohttp aiomqtt>=2.0,<3.0 paho-mqtt>=1.6,<3.0'
# shellcheck disable=SC2086
pip3 install --quiet $PKGS 2>/dev/null || pip3 install --break-system-packages --quiet $PKGS
if [ $? -ne 0 ]; then
    echo "<FAIL> KEBA: pip3 install fehlgeschlagen"
    exit 1
fi
echo "<OK> KEBA: Python-Pakete installiert"
exit 0
