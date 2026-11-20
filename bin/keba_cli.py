#!/usr/bin/env python3
"""Hilfsprogramm für die WebUI (ajax.cgi). Gibt immer genau ein JSON-Objekt aus.

  keba_cli.py [--configdir D] [--shmdir D] [--logdir D] check --host H --username U --password-stdin [--box-id ID]
  keba_cli.py ... operations --box-id ID
  keba_cli.py ... diag

Passwörter kommen nur über stdin, nie über die Kommandozeile, und werden nie ausgegeben.
"""
import argparse
import asyncio
import glob
import json
import os
import platform
import re
import sys
import time
import uuid
from pathlib import Path

from keba_config import ConfigError, load_config

DEFAULT_SHMDIR = "/dev/shm/keba"
PID_FILE = Path("/dev/shm/keba_gateway.pid")
UDP_TIMEOUT = 5.0


# ---- Maskierung für die Diagnose -------------------------------------------------------------
_RE_JWT = re.compile(r"eyJ[A-Za-z0-9_-]*(\.[A-Za-z0-9_-]*){0,2}")
_RE_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_RE_MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")
_RE_SERIAL = re.compile(r"\b(\d{4})\d{4}\b")


def mask(text: str, serials=(), secrets=()) -> str:
    for s in secrets:
        if s and len(s) >= 3:
            text = text.replace(s, "<geheim>")
    text = _RE_JWT.sub("<token>", text)
    text = _RE_MAC.sub("xx:xx:xx:xx:xx:xx", text)
    text = _RE_IPV4.sub("x.x.x.x", text)
    for sn in serials:
        if sn:
            text = text.replace(sn, sn[:4] + "****")
    return _RE_SERIAL.sub(r"\1****", text)


# ---- Verbindungsprüfung ----------------------------------------------------------------------
def _gateway_alive(pidfile: Path = PID_FILE) -> bool:
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


async def _udp_via_gateway(host: str, base: str) -> tuple[bool, str, str]:
    import aiomqtt
    from keba_gateway import broker_kwargs

    reqid = uuid.uuid4().hex[:12]
    async with aiomqtt.Client(**broker_kwargs(), identifier=f"keba-cli-{reqid}") as client:
        await client.subscribe(f"{base}/gateway/udpcheck/result")
        await client.publish(f"{base}/gateway/udpcheck/set", json.dumps({"host": host, "reqid": reqid}))
        async with asyncio.timeout(UDP_TIMEOUT):
            async for msg in client.messages:
                try:
                    data = json.loads(msg.payload)
                except (ValueError, TypeError):
                    continue
                if data.get("reqid") == reqid:
                    return bool(data.get("ok")), str(data.get("serial", "")), str(data.get("error", ""))
    return False, "", "keine Antwort"


async def _udp_direct(host: str) -> tuple[bool, str, str]:
    from keba_udp import UdpHub, parse_report

    hub = UdpHub(timeout=3.0)
    await hub.start()
    try:
        if hub.state != "ok":
            return False, "", "UDP-Port 7090 belegt"
        report = parse_report(await hub.probe(host, "report 1"))
        serial = str(report.get("Serial", ""))
        return bool(serial), serial, "" if serial else "keine Seriennummer"
    finally:
        await hub.close()


async def udp_check_default(host: str, base: str = "keba") -> tuple[bool, str, str]:
    if _gateway_alive():
        return await _udp_via_gateway(host, base)
    return await _udp_direct(host)


def _write_cache(shmdir: Path, serial: str, version: str, spec: dict) -> None:
    try:
        cache = Path(shmdir) / serial
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "openapi.json").write_text(json.dumps(spec))
        (cache / "version").write_text(version)
    except OSError:
        pass


async def check(host: str, username: str, password: str, port: int = 8443, scheme: str = "https",
                udp_check=None, shmdir: Path | str = DEFAULT_SHMDIR, base: str = "keba") -> dict:
    import aiohttp
    from keba_openapi import parse_openapi
    from keba_rest import KebaRest, RestError

    udp_check = udp_check or (lambda h: udp_check_default(h, base))
    steps, result = [], {"serial": "", "firmware": "", "model": "", "op_count": 0}

    def step(key, ok, value="", error="", t0=0.0):
        steps.append({"key": key, "ok": ok, "value": value, "error": error,
                      "ms": int((time.monotonic() - t0) * 1000)})

    async with aiohttp.ClientSession() as session:
        rest = KebaRest(host, username, password, session, port=port, scheme=scheme)
        t0 = time.monotonic()
        await rest.login()
        if rest.auth_state == "ok":
            step("login", True, "angemeldet", t0=t0)
        else:
            step("login", False, error=f"{rest.auth_state}: {rest.auth_error}", t0=t0)

        version = ""
        for key in ("serial", "version"):
            t0 = time.monotonic()
            try:
                value = await (rest.get_serial() if key == "serial" else rest.get_version())
                step(key, True, value, t0=t0)
                if key == "serial":
                    result["serial"] = value
                else:
                    version = value
            except RestError as exc:
                step(key, False, error=str(exc), t0=t0)

        t0 = time.monotonic()
        try:
            spec = await rest.fetch_openapi()
            ops = parse_openapi(spec)
            result["op_count"] = len(ops)
            step("openapi", True, f"{len(ops)} Endpunkte", t0=t0)
            if result["serial"] and version:
                _write_cache(Path(shmdir), result["serial"], version, spec)
        except RestError as exc:
            step("openapi", False, error=str(exc), t0=t0)

        if rest.auth_state == "ok" and result["serial"]:
            res = await rest.request("GET", f"/v2/wallboxes/{result['serial']}")
            if res.ok and isinstance(res.body, dict):
                result["firmware"] = str(res.body.get("firmwareVersion", ""))
                result["model"] = str(res.body.get("model", ""))

    t0 = time.monotonic()
    try:
        ok, serial, error = await udp_check(host)
    except (TimeoutError, asyncio.TimeoutError):
        ok, serial, error = False, "", "timeout"
    except Exception as exc:  # noqa: BLE001
        ok, serial, error = False, "", f"{type(exc).__name__}: {exc}"
    if ok and result["serial"] and serial != result["serial"]:
        ok, error = False, "UDP meldet andere Seriennummer"
    step("udp", ok, serial, error, t0=t0)
    if not result["serial"] and ok:
        result["serial"] = serial
    return {"steps": steps, **result}


# ---- Endpunktliste ---------------------------------------------------------------------------
async def operations(configdir: Path, shmdir: Path, box_id: str) -> dict:
    import aiohttp
    from keba_box import describe_operations
    from keba_openapi import missing_enabled, parse_openapi
    from keba_rest import KebaRest

    cfg = load_config(Path(configdir) / "pluginconfig.json")
    box = next((b for b in cfg.boxes if b.id == box_id), None)
    if box is None:
        return {"error": f"Box {box_id} nicht konfiguriert"}
    cache = Path(shmdir) / box.serial / "openapi.json" if box.serial else None
    spec, fetched_at, version = None, 0.0, ""
    if cache is not None and cache.exists():
        try:
            spec = json.loads(cache.read_text())
            fetched_at = cache.stat().st_mtime
            version = (cache.parent / "version").read_text().strip() if (cache.parent / "version").exists() else ""
        except (OSError, ValueError):
            spec = None
    if spec is None:
        async with aiohttp.ClientSession() as session:
            rest = KebaRest(box.host, box.username, box.password, session)
            spec = await rest.fetch_openapi()
            version = await rest.get_version()
            fetched_at = time.time()
            if box.serial:
                _write_cache(Path(shmdir), box.serial, version, spec)
    ops = parse_openapi(spec)
    firmware = ""
    try:
        status = json.loads((Path(shmdir) / "status.json").read_text())
        firmware = status.get("boxes", {}).get(box_id, {}).get("firmware", "")
    except (OSError, ValueError):
        pass
    return {"serial": box.serial, "firmware": firmware, "api_version": version,
            "fetched_at": fetched_at, "ops": describe_operations(ops, box.enabled, cfg.mqtt),
            "missing": missing_enabled(ops, [e.op for e in box.enabled])}


# ---- Diagnose --------------------------------------------------------------------------------
def _lb_versions() -> tuple[str, str]:
    try:
        from loxberry import system
        return str(system.pluginversion() or "?"), str(system.lbversion() or "?")
    except Exception:  # noqa: BLE001
        return "?", "?"


def _fmt_time(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts))) if ts else "–"
    except (TypeError, ValueError):
        return "–"


def diag(configdir: Path, shmdir: Path, logdir: Path | None) -> dict:
    try:
        cfg = load_config(Path(configdir) / "pluginconfig.json")
    except ConfigError:
        cfg = None
    boxes = cfg.boxes if cfg else []
    serials = [b.serial for b in boxes if b.serial]
    secrets = [b.password for b in boxes] + [b.username for b in boxes if len(b.username) > 5]
    plugin_v, lb_v = _lb_versions()
    lines = [f"KEBA-Plugin {plugin_v} · LoxBerry {lb_v} · Python {platform.python_version()}"]
    try:
        status = json.loads((Path(shmdir) / "status.json").read_text())
    except (OSError, ValueError):
        status = {}
    gw = status.get("gateway") or {}
    if gw:
        lines.append(f"Gateway {gw.get('state', '?')} seit {_fmt_time(gw.get('since'))} · "
                     f"MQTT {gw.get('mqtt', '?')} · UDP {gw.get('udp', '?')}")
    else:
        lines.append("Gateway: kein Status (läuft nicht?)")
    if (Path(shmdir) / "watchdog.gaveup").exists():
        lines.append("Watchdog: aufgegeben (zu viele Neustarts)")
    for box_id, st in (status.get("boxes") or {}).items():
        if not st.get("active", True):
            lines.append(f"Box {st.get('name') or box_id}: inaktiv")
            continue
        lines.append(
            f"Box {st.get('name') or box_id}  SN {st.get('serial', '')}  FW {st.get('firmware', '') or '?'}  "
            f"API {st.get('api', '?')}  UDP {st.get('udp', '?')}  Auth {st.get('auth', '?')}  "
            f"Token bis {_fmt_time(st.get('token_expires'))}"
            + (f"  Fehler {st['error']}" if st.get("error") else ""))
    if logdir:
        logs = sorted(glob.glob(str(Path(logdir) / "*gateway*.log")), key=os.path.getmtime)
        if logs:
            try:
                tail = Path(logs[-1]).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                tail = []
            problems = [l for l in tail if "<ERROR>" in l or "<WARNING>" in l or "<CRITICAL>" in l][-20:]
            lines.append(f"Letzte Warnungen/Fehler ({len(problems)}):" if problems
                         else "Keine Warnungen oder Fehler im aktuellen Log.")
            lines.extend(problems)
    return {"text": mask("\n".join(lines), serials=serials, secrets=secrets)}


# ---- Einstieg --------------------------------------------------------------------------------
def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="KEBA-Plugin Hilfsprogramm")
    ap.add_argument("--configdir", default="")
    ap.add_argument("--shmdir", default=DEFAULT_SHMDIR)
    ap.add_argument("--logdir", default="")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--host", required=True)
    c.add_argument("--username", default="admin")
    c.add_argument("--password-stdin", action="store_true")
    c.add_argument("--box-id", default="")
    c.add_argument("--port", type=int, default=8443)
    c.add_argument("--scheme", default="https", choices=("https", "http"))
    o = sub.add_parser("operations")
    o.add_argument("--box-id", required=True)
    sub.add_parser("diag")
    return ap


async def amain(argv=None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit:
        print(json.dumps({"error": "ungültige Argumente"}))
        return 0
    configdir = Path(args.configdir or ".")
    try:
        if args.cmd == "check":
            password = sys.stdin.read().rstrip("\r\n") if args.password_stdin else ""
            cfg = load_config(configdir / "pluginconfig.json")
            if not password and args.box_id:
                box = next((b for b in cfg.boxes if b.id == args.box_id), None)
                password = box.password if box else ""
            result = await check(args.host, args.username, password, port=args.port,
                                 scheme=args.scheme, shmdir=args.shmdir,
                                 udp_check=lambda h: udp_check_default(h, cfg.mqtt.base_topic))
        elif args.cmd == "operations":
            result = await operations(configdir, Path(args.shmdir), args.box_id)
        else:
            result = diag(configdir, Path(args.shmdir), Path(args.logdir) if args.logdir else None)
    except Exception as exc:  # noqa: BLE001 — WebUI bekommt immer JSON
        result = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    return asyncio.run(amain(argv))


if __name__ == "__main__":
    sys.exit(main())
