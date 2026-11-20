"""Worker je Wallbox: Anmeldung, Endpunktmodell, Polling, Befehle, UDP.

Alle Topics sind relativ zum Basis-Topic; der Worker stellt `<serial>/` voran.
Ein Durchlauf von `step()` erledigt alles, was zum aktuellen Zeitpunkt fällig ist;
`run()` ruft `step()` im Takt auf, bis `stop()` kommt.
"""
import asyncio
import json
import logging
import time
import urllib.parse
from pathlib import Path
from typing import Awaitable, Callable

from keba_config import BoxConfig, MqttConfig
from keba_openapi import CORE_OPS, Operation, missing_enabled, op_status, parse_openapi, validate
from keba_rest import Backoff, KebaRest, RestError
from keba_udp import UdpHub, display_command, parse_report, validate_udp_command

log = logging.getLogger("keba.box")

Publish = Callable[[str, object, bool], Awaitable[None]]

CORE_GET = "GET /v2/wallboxes/{serialNumber}"
UDP_CHECK_INTERVAL = 300      # report 1 und Versionsprüfung
UDP_RETRY_INTERVAL = 60       # report 1, solange UDP nicht antwortet
COMMAND_METHODS = ("get", "post", "put", "patch", "delete")


def describe_operations(ops: list[Operation], enabled_ops: list, mqtt: MqttConfig) -> list[dict]:
    """Endpunktliste für Doku und WebUI, inklusive freigeschalteter, aber unbekannter Einträge."""
    enabled = {e.op for e in enabled_ops}
    intervals = {e.op: e.interval for e in enabled_ops}
    out = []
    for o in ops:
        interval = None
        if o.pollable and o.op == CORE_GET:
            interval = mqtt.core_interval
        elif o.pollable and o.op in enabled:
            interval = intervals.get(o.op) or mqtt.default_interval
        out.append({
            "op": o.op, "method": o.method, "path": o.path, "tag": o.tag,
            "summary": o.summary, "description": o.description, "topic": o.topic,
            "status": op_status(o, enabled), "pollable": o.pollable,
            "block_reason": o.block_reason, "path_params": o.path_params,
            "query_params": o.query_params, "body_schema": o.body_schema,
            "response_schema": o.response_schema, "interval": interval,
        })
    for key in missing_enabled(ops, [e.op for e in enabled_ops]):
        method, _, path = key.partition(" ")
        out.append({"op": key, "method": method, "path": path, "status": "missing",
                    "topic": "", "pollable": False, "interval": None})
    return out


def _result(ok: bool, status: int = 0, body=None, error: str = "") -> dict:
    return {"ok": ok, "status": status, "body": body, "error": error}


class BoxWorker:
    def __init__(self, box: BoxConfig, mqtt: MqttConfig, rest: KebaRest, udp: UdpHub,
                 publish: Publish, shmdir: Path, clock: Callable[[], float] = time.time):
        self.box, self.mqtt, self.rest, self.udp = box, mqtt, rest, udp
        self.publish, self.shmdir, self.clock = publish, Path(shmdir), clock
        self.serial = box.serial
        self.ops: list[Operation] = []
        self.spec: dict = {}
        self.version = ""
        self.error = ""
        self.udp_state = "unknown"
        self.firmware = ""
        self.model = ""
        self.last_poll = 0.0
        self.last_udp = 0.0
        self.online = False
        self.live: dict = {}
        self._ready = False
        self._setup_backoff = Backoff()
        self._next_setup = 0.0
        self._next_udp = 0.0
        self._due: dict[str, float] = {}
        self._session_kwh = None
        self._last_status = None
        self._cmd_lock = asyncio.Lock()
        self._stopped = asyncio.Event()
        if self.serial:
            self.udp.register(self.serial, box.host)

    # ---- Hilfen ----------------------------------------------------------------------------
    @property
    def enabled(self) -> list[str]:
        return [e.op for e in self.box.enabled]

    def _interval(self, op: str) -> int:
        if op == CORE_GET:
            return self.mqtt.core_interval
        for e in self.box.enabled:
            if e.op == op:
                return e.interval or self.mqtt.default_interval
        return self.mqtt.default_interval

    def _find(self, topic: str, method: str) -> Operation | None:
        for o in self.ops:
            if o.topic == topic and o.method == method:
                return o
        return None

    async def _pub(self, topic: str, payload, retain: bool = False) -> None:
        if not self.serial:
            return
        try:
            await self.publish(f"{self.serial}/{topic}", payload, retain)
        except Exception:  # noqa: BLE001 — ein MQTT-Problem darf den Worker nicht stoppen
            log.exception("%s: Veröffentlichen von %s fehlgeschlagen", self.box.name, topic)

    # ---- Einrichtung -----------------------------------------------------------------------
    async def _setup(self) -> None:
        """Anmeldung, Seriennummer, Version, openapi.json. Bleibt bis zum Erfolg wiederholbar."""
        await self.rest.ensure_token()
        if self.rest.auth_state != "ok":
            return
        try:
            serial = await self.rest.get_serial()
            if self.box.serial and serial != self.box.serial:
                self.error = "serial_mismatch"
                log.error("%s: Box meldet andere Seriennummer als konfiguriert", self.box.name)
                self._next_setup = self.clock() + self._setup_backoff.next()
                return
            if not self.serial:
                self.serial = serial
                self.udp.register(serial, self.box.host)
            await self._load_openapi(await self.rest.get_version())
        except RestError as exc:
            self.error = f"setup: {exc}"
            self._next_setup = self.clock() + self._setup_backoff.next()
            log.warning("%s: Einrichtung fehlgeschlagen (%s)", self.box.name, exc)
            return
        self.error = ""
        self._setup_backoff.reset()
        self._ready = True
        missing = missing_enabled(self.ops, self.enabled)
        if missing:
            log.warning("%s: freigeschaltet, aber in dieser Firmware unbekannt: %s",
                        self.box.name, ", ".join(missing))

    async def _load_openapi(self, version: str) -> None:
        cache = self.shmdir / self.serial
        spec_file, ver_file = cache / "openapi.json", cache / "version"
        spec = None
        if spec_file.exists() and ver_file.exists() and ver_file.read_text().strip() == version:
            try:
                spec = json.loads(spec_file.read_text())
            except ValueError:
                spec = None
        if spec is None:
            log.info("%s: lade openapi.json (API %s)", self.box.name, version)
            spec = await self.rest.fetch_openapi()
            cache.mkdir(parents=True, exist_ok=True)
            spec_file.write_text(json.dumps(spec))
            ver_file.write_text(version)
        self.spec, self.version = spec, version
        self.ops = parse_openapi(spec)

    async def _check_version(self) -> None:
        try:
            version = await self.rest.get_version()
        except RestError:
            return
        if version != self.version:
            log.info("%s: API-Version %s → %s, Endpunkte neu einlesen", self.box.name,
                     self.version, version)
            try:
                await self._load_openapi(version)
            except RestError as exc:
                log.warning("%s: openapi.json nicht ladbar (%s)", self.box.name, exc)

    # ---- Takt ------------------------------------------------------------------------------
    async def step(self) -> None:
        now = self.clock()
        if not self._ready and now >= self._next_setup:
            await self._setup()
        if self.serial and self.error != "serial_mismatch" and now >= self._next_udp:
            await self._udp_check()
            if self._ready:
                await self._check_version()
        if self._ready:
            await self._poll_due(now)
        await self._publish_status()

    async def run(self, tick: float = 1.0) -> None:
        log.info("%s: Worker gestartet", self.box.name)
        while not self._stopped.is_set():
            try:
                await self.step()
            except Exception:  # noqa: BLE001 — Worker läuft weiter, Fehler steht im Log
                log.exception("%s: Fehler im Worker", self.box.name)
            try:
                await asyncio.wait_for(self._stopped.wait(), tick)
            except asyncio.TimeoutError:
                pass
        log.info("%s: Worker beendet", self.box.name)

    def stop(self) -> None:
        self._stopped.set()

    async def _poll_due(self, now: float) -> None:
        targets = [CORE_GET] + [e for e in self.enabled if e != CORE_GET]
        for key in targets:
            op = next((o for o in self.ops if o.op == key), None)
            if op is None or not op.pollable or op_status(op, set(self.enabled)) == "blocked":
                continue
            if now < self._due.get(key, 0):
                continue
            self._due[key] = now + self._interval(key)
            res = await self.rest.request("GET", self._path(op, {}))
            if key == CORE_GET:
                await self._core_result(res)
            if res.ok:
                await self._pub(op.topic, res.body, self.mqtt.retain)
            else:
                log.info("%s: %s → %s", self.box.name, key, res.error)

    async def _core_result(self, res) -> None:
        self.last_poll = self.clock()
        self.online = res.ok or self.udp_state == "ok"
        if not res.ok or not isinstance(res.body, dict):
            return
        wb = res.body
        self.firmware = str(wb.get("firmwareVersion") or self.firmware)
        self.model = str(wb.get("model") or self.model)
        if self.udp_state == "ok":
            try:
                r3 = parse_report(await self.udp.send(self.serial, "report 3"))
                if "E pres" in r3:
                    self._session_kwh = round(float(r3["E pres"]) / 10000, 3)
            except (TimeoutError, ConnectionError, KeyError, ValueError, TypeError):
                pass
        meter = wb.get("meter") or {}
        power = meter.get("totalActivePower")
        self.live = {
            "state": wb.get("state"),
            "power_kw": None if power is None else round(float(power) / 1e6, 3),
            "session_kwh": self._session_kwh,
            "plug": wb.get("vehiclePlugged"),
        }

    async def _udp_check(self) -> None:
        if self.udp.state != "ok":
            self.udp_state = self.udp.state
            self._next_udp = self.clock() + UDP_CHECK_INTERVAL
            return
        try:
            report = parse_report(await self.udp.send(self.serial, "report 1"))
        except (TimeoutError, ConnectionError, KeyError):
            self.udp_state = "timeout"
            self._next_udp = self.clock() + UDP_RETRY_INTERVAL
            return
        self._next_udp = self.clock() + UDP_CHECK_INTERVAL
        if str(report.get("Serial", "")) != self.serial:
            self.udp_state = "serial_mismatch"
            return
        self.udp_state = "ok"
        self.last_udp = self.clock()
        self.firmware = self.firmware or str(report.get("Firmware", ""))
        self.model = self.model or str(report.get("Product", ""))
        if not self._ready:
            self.online = True

    # ---- Status ----------------------------------------------------------------------------
    def status(self) -> dict:
        return {
            "id": self.box.id, "name": self.box.name, "serial": self.serial,
            "host": self.box.host, "online": self.online,
            "api": self.rest.auth_state if self.rest.auth_state != "none" else "unknown",
            "udp": self.udp_state, "auth": self.rest.auth_state,
            "token_expires": self.rest.token_expires, "refresh_expires": self.rest.refresh_expires,
            "last_poll": self.last_poll, "last_udp": self.last_udp,
            "firmware": self.firmware, "model": self.model, "api_version": self.version,
            "op_count": len(self.ops),
            "enabled_count": sum(1 for o in self.ops if op_status(o, set(self.enabled)) == "enabled"),
            "missing": missing_enabled(self.ops, self.enabled) if self.ops else [],
            "error": self.error, "live": dict(self.live),
        }

    async def _publish_status(self) -> None:
        st = self.status()
        if st != self._last_status:
            self._last_status = st
            await self._pub("status", st, True)

    def operations(self) -> list[dict]:
        return describe_operations(self.ops, self.box.enabled, self.mqtt)

    # ---- Befehle ---------------------------------------------------------------------------
    def _path(self, op: Operation, params: dict) -> str:
        path = op.path
        if op.serial_param:
            path = path.replace("{" + op.serial_param + "}", urllib.parse.quote(self.serial, safe=""))
        for name in op.path_params:
            path = path.replace("{" + name + "}", urllib.parse.quote(str(params[name]), safe=""))
        return path

    async def handle_command(self, topic_rest: str, payload: bytes) -> None:
        async with self._cmd_lock:
            try:
                await self._command(topic_rest.strip("/"), payload or b"")
            except Exception:  # noqa: BLE001
                log.exception("%s: Fehler bei Befehl %s", self.box.name, topic_rest)

    async def _command(self, topic_rest: str, payload: bytes) -> None:
        if topic_rest in ("udp/set", "display/set"):
            await self._udp_command(topic_rest, payload)
            return
        topic, _, method = topic_rest.rpartition("/")
        if method not in COMMAND_METHODS or not topic:
            log.info("%s: unbekanntes Befehls-Topic %s", self.box.name, topic_rest)
            return
        reply = f"{topic}/{method}/result"
        if not self._ready:
            state = self.error or f"api {self.rest.auth_state}"
            await self._pub(reply, _result(False, error=f"not ready: {state}"))
            return
        op = self._find(topic, method.upper())
        if op is None:
            await self._pub(reply, _result(False, error="unknown operation"))
            return
        if op_status(op, set(self.enabled)) not in ("core", "enabled"):
            await self._pub(reply, _result(False, error="not enabled"))
            return
        if op.body_content_type and op.body_content_type != "application/json":
            await self._pub(reply, _result(False, error=f"unsupported content type {op.body_content_type}"))
            return
        try:
            params, query, body = self._unpack(op, payload)
        except ValueError as exc:
            await self._pub(reply, _result(False, error=f"invalid body: {exc}"))
            return
        if op.body_schema is not None:
            errors = validate(op.body_schema, body if body is not None else {}, self.spec)
            if errors:
                await self._pub(reply, _result(False, error="invalid body: " + "; ".join(errors[:5])))
                return
        log.info("%s: Befehl %s", self.box.name, op.op)
        res = await self.rest.request(op.method, self._path(op, params), query or None,
                                      body if op.body_schema is not None else None)
        await self._pub(reply, _result(res.ok, res.status, res.body, res.error))
        if res.ok and op.method == "GET":
            await self._pub(op.topic, res.body, self.mqtt.retain)

    def _unpack(self, op: Operation, payload: bytes):
        text = payload.decode("utf-8", errors="strict").strip() if payload else ""
        data = None
        if text:
            try:
                data = json.loads(text)
            except ValueError:
                raise ValueError("kein gültiges JSON") from None
        params, query, body = {}, {}, data
        uses_envelope = bool(op.path_params or op.query_params)
        if uses_envelope and (data is None or (isinstance(data, dict)
                                               and set(data) <= {"path", "query", "body"})):
            data = data or {}
            params = data.get("path") or {}
            query = data.get("query") or {}
            body = data.get("body")
            if not isinstance(params, dict) or not isinstance(query, dict):
                raise ValueError("path und query müssen Objekte sein")
        missing = [p for p in op.path_params if p not in params]
        if missing:
            raise ValueError("Pfadparameter fehlt: " + ", ".join(missing))
        known = {q["name"] for q in op.query_params}
        unknown = [k for k in query if k not in known]
        if unknown:
            raise ValueError("unbekannter Query-Parameter: " + ", ".join(unknown))
        absent = [q["name"] for q in op.query_params if q["required"] and q["name"] not in query]
        if absent:
            raise ValueError("Query-Parameter fehlt: " + ", ".join(absent))
        return params, query, body

    async def _udp_command(self, topic_rest: str, payload: bytes) -> None:
        text = payload.decode("utf-8", errors="replace").strip()
        cmd = display_command(text) if topic_rest == "display/set" else text
        err = validate_udp_command(cmd)
        if err:
            await self._pub("udp/result", _result(False, error=err))
            return
        try:
            reply = await self.udp.send(self.serial, cmd)
        except TimeoutError:
            await self._pub("udp/result", _result(False, error="timeout"))
            return
        except (ConnectionError, KeyError) as exc:
            await self._pub("udp/result", _result(False, error=f"udp: {exc}"))
            return
        log.info("%s: UDP-Befehl %s", self.box.name, cmd.split()[0])
        await self._pub("udp/result", _result(True, 0, reply.strip()))
