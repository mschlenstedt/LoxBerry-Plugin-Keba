"""UDP-Anbindung (Port 7090) für alle Boxen über einen Socket.

Die Box antwortet an Port 7090 des Absenders; Antworten werden über die Absender-IP der
Box zugeordnet. Pro Box läuft immer nur eine Anfrage (Lock), die Antwort erfüllt deren Future.
Pakete ohne offene Anfrage gehen an `on_unsolicited`.
"""
import asyncio
import json
import logging
import re
from typing import Callable

log = logging.getLogger("keba.udp")

ALLOWED_UDP = frozenset("curr currtime display ena failsafe i output report setdatetime "
                        "setenergy start stop unlock x2 x2src".split())
MAX_LEN = 100
DISPLAY_LEN = 23


def validate_udp_command(text: str) -> str:
    if not text or not text.strip():
        return "leerer Befehl"
    if len(text) > MAX_LEN:
        return f"Befehl länger als {MAX_LEN} Zeichen"
    if not text.isascii() or any(c in text for c in "\r\n\t\0"):
        return "nur ASCII-Zeichen ohne Zeilenumbruch erlaubt"
    word = text.split()[0]
    if word not in ALLOWED_UDP:
        return f"Befehl '{word}' nicht erlaubt ({' '.join(sorted(ALLOWED_UDP))})"
    return ""


def display_command(text: str) -> str:
    t = "".join(c if c.isascii() and c.isprintable() else "_" for c in text)
    t = t.replace(" ", "$")[:DISPLAY_LEN]
    return "display 1 10 30 0 " + t


def parse_report(text: str) -> dict:
    text = (text or "").strip()
    for candidate in (text, "{" + text + "}"):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        return data if isinstance(data, dict) else {}
    return {}


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, hub: "UdpHub"):
        self.hub = hub

    def datagram_received(self, data, addr):
        self.hub._received(data, addr)

    def error_received(self, exc):
        log.debug("UDP-Fehler: %s", exc)


class UdpHub:
    def __init__(self, port: int = 7090, box_port: int = 7090, timeout: float = 3.0,
                 bind_host: str = "0.0.0.0"):
        self.port, self.box_port, self.timeout, self.bind_host = port, box_port, timeout, bind_host
        self.state = "closed"
        self.on_unsolicited: Callable[[str, str], None] | None = None
        self._transport = None
        self._hosts: dict[str, str] = {}          # serial -> host
        self._by_ip: dict[str, str] = {}          # ip -> serial
        self._locks: dict[str, asyncio.Lock] = {}
        self._pending: dict[str, asyncio.Future] = {}

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _Protocol(self), local_addr=(self.bind_host, self.port))
        except OSError as exc:
            self.state = "port_busy"
            log.error("UDP-Port %d nicht verfügbar (%s) – UDP für alle Boxen aus", self.port, exc)
            return
        self.state = "ok"
        log.info("UDP-Port %d geöffnet", self.port)

    def register(self, serial: str, host: str) -> None:
        self.unregister(serial)
        self._hosts[serial] = host
        self._by_ip[host] = serial
        self._locks.setdefault(serial, asyncio.Lock())

    def unregister(self, serial: str) -> None:
        host = self._hosts.pop(serial, None)
        if host is not None and self._by_ip.get(host) == serial:
            del self._by_ip[host]
        fut = self._pending.pop(serial, None)
        if fut and not fut.done():
            fut.cancel()

    async def send(self, serial: str, text: str) -> str:
        host = self._hosts[serial]
        if self.state != "ok" or self._transport is None:
            raise ConnectionError(f"UDP nicht verfügbar ({self.state})")
        async with self._locks[serial]:
            fut = asyncio.get_running_loop().create_future()
            self._pending[serial] = fut
            try:
                self._transport.sendto(text.encode("ascii"), (host, self.box_port))
                log.debug("UDP → %s: %s", serial, text)
                async with asyncio.timeout(self.timeout):
                    return await fut
            except TimeoutError:
                log.info("UDP: keine Antwort von %s auf '%s'", serial, text.split()[0])
                raise
            finally:
                if self._pending.get(serial) is fut:
                    del self._pending[serial]

    def _received(self, data: bytes, addr) -> None:
        serial = self._by_ip.get(addr[0])
        text = data.decode("ascii", errors="replace")
        if serial is None:
            log.debug("UDP von unbekannter Adresse %s ignoriert", addr[0])
            return
        fut = self._pending.get(serial)
        if fut is not None and not fut.done():
            fut.set_result(text)
            return
        log.debug("UDP unaufgefordert von %s: %s", serial, re.sub(r"\s+", " ", text)[:120])
        if self.on_unsolicited:
            try:
                self.on_unsolicited(serial, text)
            except Exception:  # noqa: BLE001 — Rückruf darf den Empfang nicht stoppen
                log.exception("Fehler im UDP-Rückruf")

    async def close(self) -> None:
        for serial in list(self._hosts):
            self.unregister(serial)
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self.state = "closed"
