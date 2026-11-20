#!/usr/bin/env python3
"""KEBA-Gateway: verbindet KEBA-Wallboxen (REST/UDP) mit dem LoxBerry-MQTT-Broker.

Gestartet von daemon/daemon.sh bzw. ajax.cgi über die Perl→Python-Log-Brücke:
  keba_gateway.py --configdir DIR --logfile FILE --logdbkey KEY --loglevel N [--shmdir DIR]
"""
import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import re
import signal
import ssl
import sys
import tempfile
import time
from pathlib import Path

from keba_config import BoxConfig, Config, ConfigError, MqttConfig, load_config

log = logging.getLogger("keba.gateway")

PID_FILE = Path("/dev/shm/keba_gateway.pid")
DEFAULT_SHMDIR = Path("/dev/shm/keba")
STATUS_MIN_INTERVAL = 2.0
HEARTBEAT_INTERVAL = 30.0
LOGLEVEL_CHECK_INTERVAL = 60.0
COMMAND_WORDS = frozenset({"set", "get", "post", "put", "patch", "delete"})
SET_TOPICS = frozenset({"udp/set", "display/set"})
OK_LEVEL = 25
logging.addLevelName(OK_LEVEL, "OK")


def route_message(base: str, topic: str, retain: bool) -> tuple[str, str] | None:
    """(serial, rest) für Befehls-Topics `<base>/<serial>/<rest>`; sonst None."""
    if retain:
        return None
    prefix = base.strip("/") + "/"
    if not topic.startswith(prefix):
        return None
    parts = topic[len(prefix):].split("/")
    if len(parts) < 3 or not parts[0] or parts[0] == "gateway":
        return None
    serial, rest = parts[0], parts[1:]
    if rest[-1] not in COMMAND_WORDS or any(not p for p in rest):
        return None
    rest_s = "/".join(rest)
    if rest[-1] == "set" and rest_s not in SET_TOPICS:
        return None
    return serial, rest_s


def default_worker_factory(gw: "Gateway", box: BoxConfig, mqtt: MqttConfig):
    from keba_box import BoxWorker
    from keba_rest import KebaRest

    rest = KebaRest(box.host, box.username, box.password, gw.session)
    return BoxWorker(box, mqtt, rest, gw.hub, lambda t, p, r: gw.publish(t, p, r), gw.shmdir)


class Gateway:
    def __init__(self, configdir: Path, shmdir: Path = DEFAULT_SHMDIR, worker_factory=None,
                 hub=None, version: str = "", clock=time.time, session=None):
        self.configdir, self.shmdir = Path(configdir), Path(shmdir)
        self.worker_factory = worker_factory or default_worker_factory
        self.hub, self.version, self.clock, self.session = hub, version, clock, session
        self.cfg = Config()
        self.workers: dict = {}
        self._box_cfg: dict[str, BoxConfig] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self.client = None
        self.since = clock()
        self._last_status_write = 0.0
        self._shutdown = asyncio.Event()
        self._reconnect = asyncio.Event()

    @property
    def base(self) -> str:
        return self.cfg.mqtt.base_topic

    # ---- Konfiguration und Worker ----------------------------------------------------------
    async def apply_config(self) -> None:
        try:
            cfg = load_config(self.configdir / "pluginconfig.json")
        except ConfigError as exc:
            log.error("Konfiguration nicht lesbar, behalte die laufende: %s", exc)
            return
        mqtt_changed = cfg.mqtt != self.cfg.mqtt
        if cfg.mqtt.base_topic != self.cfg.mqtt.base_topic and self.client is not None:
            self._reconnect.set()
        wanted = {b.id: b for b in cfg.boxes if b.active and b.id}
        for box_id in list(self.workers):
            if box_id not in wanted or mqtt_changed or wanted[box_id] != self._box_cfg.get(box_id):
                await self._stop_worker(box_id)
        self.cfg = cfg
        for box_id, b in wanted.items():
            if box_id not in self.workers:
                self._start_worker(b)
        log.info("Konfiguration geladen: %d Box(en) aktiv", len(self.workers))

    def _start_worker(self, box: BoxConfig) -> None:
        worker = self.worker_factory(self, box, self.cfg.mqtt)
        self.workers[box.id] = worker
        self._box_cfg[box.id] = box
        self._tasks[box.id] = asyncio.create_task(worker.run(), name=f"worker-{box.id}")
        log.info("Box %s (%s) gestartet", box.name or box.id, box.host)

    async def _stop_worker(self, box_id: str) -> None:
        worker = self.workers.pop(box_id)
        self._box_cfg.pop(box_id, None)
        task = self._tasks.pop(box_id, None)
        worker.stop()
        if task is not None:
            try:
                await asyncio.wait_for(task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
            except Exception:  # noqa: BLE001
                log.exception("Worker %s endete mit Fehler", box_id)
        if self.hub is not None and getattr(worker, "serial", ""):
            self.hub.unregister(worker.serial)
        log.info("Box %s angehalten", box_id)

    async def stop_workers(self) -> None:
        for box_id in list(self.workers):
            await self._stop_worker(box_id)

    # ---- Status-Dateien ---------------------------------------------------------------------
    def gateway_state(self) -> dict:
        return {"state": "running", "since": self.since, "version": self.version,
                "pid": os.getpid(), "mqtt": "connected" if self.client is not None else "disconnected",
                "udp": getattr(self.hub, "state", "unknown")}

    def write_status(self, force: bool = False) -> bool:
        now = self.clock()
        if not force and now - self._last_status_write < STATUS_MIN_INTERVAL:
            return False
        self._last_status_write = now
        boxes = {}
        for b in self.cfg.boxes:
            if b.id in self.workers:
                boxes[b.id] = {**self.workers[b.id].status(), "active": True}
            else:
                boxes[b.id] = {"id": b.id, "name": b.name, "serial": b.serial, "host": b.host,
                               "active": False}
        self._atomic_write("status.json", json.dumps({"gateway": self.gateway_state(),
                                                      "boxes": boxes}))
        return True

    def write_heartbeat(self) -> None:
        self._atomic_write("heartbeat", f"{time.time():.0f}\n")

    def _atomic_write(self, name: str, text: str) -> None:
        self.shmdir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{name}.", dir=str(self.shmdir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.shmdir / name)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ---- MQTT -------------------------------------------------------------------------------
    async def publish(self, topic: str, payload, retain: bool) -> None:
        client = self.client
        if client is None:
            return
        if not isinstance(payload, (str, bytes, bytearray)):
            payload = json.dumps(payload, ensure_ascii=False)
        try:
            await client.publish(f"{self.base}/{topic}", payload, qos=0, retain=retain)
        except Exception as exc:  # noqa: BLE001 — getrennt: Nachricht verwerfen
            log.debug("MQTT: %s verworfen (%s)", topic, exc)

    async def on_message(self, topic: str, payload: bytes, retain: bool) -> None:
        if isinstance(payload, str):
            payload = payload.encode()
        payload = payload or b""
        if topic == f"{self.base}/gateway/udpcheck/set" and not retain:
            await self._udpcheck(payload)
            return
        routed = route_message(self.base, topic, retain)
        if routed is None:
            return
        serial, rest = routed
        for worker in list(self.workers.values()):
            if worker.serial == serial:
                await worker.handle_command(rest, payload)
                return
        log.debug("MQTT: Befehl für unbekannte Box %s ignoriert", serial)

    async def _udpcheck(self, payload: bytes) -> None:
        from keba_udp import parse_report

        result = {"reqid": "", "ok": False, "serial": "", "error": ""}
        try:
            req = json.loads(payload.decode("utf-8"))
            result["reqid"] = str(req.get("reqid", ""))
            host = str(req["host"])
            if not re.fullmatch(r"[A-Za-z0-9.:-]{1,253}", host):
                raise ValueError("ungültige Adresse")
        except (ValueError, KeyError, AttributeError, UnicodeDecodeError) as exc:
            result["error"] = f"invalid request: {exc}"
            await self.publish("gateway/udpcheck/result", result, False)
            return
        try:
            reply = await self.hub.probe(host, "report 1")
            serial = str(parse_report(reply).get("Serial", ""))
            result.update(ok=bool(serial), serial=serial, error="" if serial else "keine Seriennummer")
        except TimeoutError:
            result["error"] = "timeout"
        except (ConnectionError, OSError) as exc:
            result["error"] = f"udp: {exc}"
        await self.publish("gateway/udpcheck/result", result, False)

    async def _mqtt_loop(self, broker: dict) -> None:
        import aiomqtt

        delay = 5.0
        while not self._shutdown.is_set():
            base = self.base
            self._reconnect.clear()
            will = aiomqtt.Will(f"{base}/gateway", json.dumps({"state": "stopped"}), qos=1, retain=True)
            try:
                async with aiomqtt.Client(**broker, will=will,
                                          identifier=f"keba-gateway-{os.getpid()}") as client:
                    self.client = client
                    delay = 5.0
                    log.log(OK_LEVEL, "MQTT verbunden (%s:%s)", broker["hostname"], broker["port"])
                    await client.publish(f"{base}/gateway", json.dumps(self.gateway_state()),
                                         qos=1, retain=True)
                    await client.subscribe(f"{base}/+/#")
                    for worker in list(self.workers.values()):
                        if worker.serial:
                            await self.publish(f"{worker.serial}/status", worker.status(), True)
                    reader = asyncio.create_task(self._read(client))
                    waits = [reader, asyncio.create_task(self._shutdown.wait()),
                             asyncio.create_task(self._reconnect.wait())]
                    await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                    for t in waits:
                        t.cancel()
                    if self._shutdown.is_set() or self._reconnect.is_set():
                        await client.publish(f"{base}/gateway", json.dumps({"state": "stopped"}),
                                             qos=1, retain=True)
                    if reader.done() and not reader.cancelled() and reader.exception():
                        raise reader.exception()
            except aiomqtt.MqttError as exc:
                log.warning("MQTT getrennt (%s), neuer Versuch in %.0f s", exc, delay)
            finally:
                self.client = None
            if self._shutdown.is_set():
                break
            if not self._reconnect.is_set():
                try:
                    await asyncio.wait_for(self._shutdown.wait(), delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(60.0, delay * 2)

    async def _read(self, client) -> None:
        async for msg in client.messages:
            payload = msg.payload if isinstance(msg.payload, (bytes, bytearray)) else \
                str(msg.payload or "").encode()
            asyncio.create_task(self.on_message(str(msg.topic), bytes(payload), bool(msg.retain)))

    async def _housekeeping(self, loglevel_watch) -> None:
        last_hb = last_lvl = 0.0
        while not self._shutdown.is_set():
            now = time.monotonic()
            try:
                self.write_status()
                if now - last_hb >= HEARTBEAT_INTERVAL:
                    self.write_heartbeat()
                    last_hb = now
                if loglevel_watch and now - last_lvl >= LOGLEVEL_CHECK_INTERVAL:
                    loglevel_watch()
                    last_lvl = now
            except Exception:  # noqa: BLE001
                log.exception("Fehler beim Schreiben der Statusdateien")
            try:
                await asyncio.wait_for(self._shutdown.wait(), STATUS_MIN_INTERVAL)
            except asyncio.TimeoutError:
                pass

    def shutdown(self) -> None:
        self._shutdown.set()

    async def run(self, broker: dict, loglevel_watch=None) -> None:
        await self.apply_config()
        mqtt_task = asyncio.create_task(self._mqtt_loop(broker))
        house_task = asyncio.create_task(self._housekeeping(loglevel_watch))
        await self._shutdown.wait()
        log.info("Gateway wird beendet")
        await self.stop_workers()
        await asyncio.gather(mqtt_task, house_task, return_exceptions=True)
        self.write_status(force=True)


# ---- Prozess ---------------------------------------------------------------------------------
_LB_TO_PY = {0: logging.CRITICAL + 10, 1: logging.CRITICAL, 2: logging.CRITICAL,
             3: logging.ERROR, 4: logging.WARNING, 5: OK_LEVEL, 6: logging.INFO, 7: logging.DEBUG}


def _py_level(lb_level) -> int:
    try:
        return _LB_TO_PY.get(int(lb_level), logging.DEBUG)
    except (TypeError, ValueError):
        return logging.DEBUG


def setup_logging(logfile: str, loglevel: int) -> logging.Handler:
    handler = (logging.handlers.WatchedFileHandler(logfile, mode="a", encoding="utf-8")
               if logfile else logging.StreamHandler(sys.stdout))
    handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d <%(levelname)s> %(message)s",
                                           datefmt="%H:%M:%S"))
    root = logging.getLogger("keba")
    root.handlers[:] = [handler]
    root.propagate = False
    root.setLevel(_py_level(loglevel))
    return handler


class LoglevelWatch:
    """Zieht eine Änderung des Loglevels in der Pluginverwaltung nach. Der erste gelesene Wert
    ist nur die Ausgangslage — ein von Hand mitgegebenes --loglevel bleibt, bis sich der Wert in
    der Pluginverwaltung tatsächlich ändert."""

    def __init__(self, handler: logging.Handler, read=None):
        self.handler, self.last = handler, None
        self.read = read or self._read

    @staticmethod
    def _read() -> str:
        from loxberry import system
        return str(system.pluginloglevel()).strip()

    def __call__(self) -> None:
        try:
            level = self.read()
        except Exception:  # noqa: BLE001
            return
        if not level:
            return
        if self.last is None:
            self.last = level
            return
        if level != self.last:
            self.last = level
            logging.getLogger("keba").setLevel(_py_level(level))
            _always(self.handler, f"Loglevel geändert auf {level}")


def _always(handler: logging.Handler, msg: str) -> None:
    """Wie LoxBerry::Log mit Severity < 0: unabhängig vom Loglevel schreiben."""
    rec = logging.LogRecord("keba", logging.INFO, "", 0, msg, (), None)
    handler.emit(rec)


def _logend(dbkey: str) -> None:
    if not dbkey or not re.fullmatch(r"\w+", dbkey):
        return
    os.system("perl -e 'use LoxBerry::Log; my $l = LoxBerry::Log->new(dbkey => \"%s\", "
              "append => 1); LOGEND \"Gateway stopped.\"; exit;'" % dbkey)


def broker_kwargs() -> dict:
    from loxberry.io import mqtt_connectiondetails

    d = mqtt_connectiondetails() or {}
    tls = str(d.get("tls", "0")) == "1"
    kwargs = {"hostname": d.get("brokerhost") or "localhost",
              "port": int((d.get("tls_brokerport") if tls else d.get("brokerport")) or 1883)}
    if d.get("brokeruser"):
        kwargs["username"] = d["brokeruser"]
    if d.get("brokerpass"):
        kwargs["password"] = d["brokerpass"]
    if tls:
        ctx = ssl.create_default_context()
        if str(d.get("tls_verify", "0")) != "1":
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        elif d.get("tls_cafile") and os.path.isfile(d["tls_cafile"]):
            ctx.load_verify_locations(d["tls_cafile"])
        kwargs["tls_context"] = ctx
    return kwargs


def _plugin_version() -> str:
    try:
        from loxberry import system
        return str(system.pluginversion() or "")
    except Exception:  # noqa: BLE001
        return ""


async def amain(args) -> int:
    import aiohttp
    from keba_udp import UdpHub

    handler = setup_logging(args.logfile, args.loglevel)
    loglevel_watch = LoglevelWatch(handler)

    version = _plugin_version()
    _always(handler, f"KEBA-Gateway {version} startet (PID {os.getpid()}, Loglevel {args.loglevel})")
    try:
        PID_FILE.write_text(str(os.getpid()))
    except OSError as exc:
        log.error("PID-Datei nicht schreibbar: %s", exc)
    hub = UdpHub()
    await hub.start()
    try:
        async with aiohttp.ClientSession() as session:
            gw = Gateway(Path(args.configdir), Path(args.shmdir), hub=hub, version=version,
                         session=session)
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGTERM, gw.shutdown)
            loop.add_signal_handler(signal.SIGINT, gw.shutdown)
            loop.add_signal_handler(signal.SIGHUP, lambda: asyncio.create_task(gw.apply_config()))
            await gw.run(broker_kwargs(), loglevel_watch)
    finally:
        await hub.close()
        try:
            if PID_FILE.read_text().strip() == str(os.getpid()):
                PID_FILE.unlink()
        except OSError:
            pass
        _always(handler, "KEBA-Gateway beendet")
        _logend(args.logdbkey)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="KEBA-Gateway")
    ap.add_argument("--configdir", required=True)
    ap.add_argument("--logfile", default="")
    ap.add_argument("--logdbkey", default="")
    ap.add_argument("--loglevel", type=int, default=7)
    ap.add_argument("--shmdir", default=str(DEFAULT_SHMDIR))
    args = ap.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
