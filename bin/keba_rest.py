"""REST-Client für die KEBA-API (https://<box>:8443).

Anmeldung als Zustandsmaschine: Login → Access-Token (15 min) → bei < 20 % Restlaufzeit
Refresh mit dem Refresh-Token (30 Tage, `Authorization: Bearer <refreshToken>`, Antwort nur
`accessToken`) → scheitert das, neuer Login. Scheitert der Login, wartet der Client mit
wachsendem Abstand (30 s … 10 min). Passwort und Tokens erscheinen nie im Log.
"""
import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Callable

import aiohttp

log = logging.getLogger("keba.rest")

TIMEOUT = aiohttp.ClientTimeout(total=10)
OPENAPI_TIMEOUT = aiohttp.ClientTimeout(total=60)
REFRESH_AT = 0.2        # Restlaufzeit-Anteil, ab dem erneuert wird
REFRESH_MARGIN = 60     # Refresh-Token muss noch so lange gelten


class RestError(Exception):
    pass


@dataclass
class RestResult:
    ok: bool
    status: int
    body: object
    error: str = ""


class Backoff:
    def __init__(self, start: float = 30, maximum: float = 600):
        self.start, self.maximum = start, maximum
        self._next = start

    def next(self) -> float:
        value = self._next
        self._next = min(self.maximum, self._next * 2)
        return value

    def reset(self) -> None:
        self._next = self.start


def jwt_times(token: str) -> tuple[float, float]:
    """(iat, exp) aus dem Payload eines JWT, ohne Signaturprüfung; (0, 0) wenn unlesbar."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = float(claims["exp"])
        return float(claims.get("iat", claims.get("nbf", exp))), exp
    except (IndexError, KeyError, TypeError, ValueError):
        return 0.0, 0.0


def _parse_body(raw: bytes, content_type: str):
    text = raw.decode("utf-8", errors="replace")
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


class KebaRest:
    def __init__(self, host: str, username: str, password: str, session: aiohttp.ClientSession,
                 port: int = 8443, scheme: str = "https", clock: Callable[[], float] = time.time):
        self.host, self.username, self.password = host, username, password
        self.session, self.clock = session, clock
        self.base = f"{scheme}://{host}:{port}"
        self.auth_state = "none"
        self.auth_error = ""
        self.token_expires = 0.0
        self.refresh_expires = 0.0
        self.retry_at = 0.0
        self.backoff = Backoff()
        self._access = ""
        self._refresh = ""
        self._token_issued = 0.0
        self._lock = asyncio.Lock()

    # ---- ohne Anmeldung ----------------------------------------------------------------
    async def _plain_get(self, path: str, timeout=TIMEOUT):
        try:
            async with self.session.get(self.base + path, ssl=False, timeout=timeout) as resp:
                raw = await resp.read()
                if resp.status != 200:
                    raise RestError(f"{path}: HTTP {resp.status}")
                return _parse_body(raw, resp.headers.get("Content-Type", ""))
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise RestError(f"{path}: {type(exc).__name__}") from exc

    async def fetch_openapi(self) -> dict:
        spec = await self._plain_get("/openapi.json", OPENAPI_TIMEOUT)
        if not isinstance(spec, dict) or "paths" not in spec:
            raise RestError("/openapi.json: keine OpenAPI-Beschreibung")
        return spec

    async def get_version(self) -> str:
        return str(await self._plain_get("/version")).strip().strip('"')

    async def get_serial(self) -> str:
        return str(await self._plain_get("/serialnumber")).strip().strip('"')

    # ---- Anmeldung ----------------------------------------------------------------------
    def _fail(self, state: str, detail: str) -> None:
        self.auth_state = state
        self.auth_error = detail
        self._access = ""
        delay = self.backoff.next()
        self.retry_at = self.clock() + delay
        log.warning("%s: Anmeldung fehlgeschlagen (%s: %s), neuer Versuch in %d s",
                    self.host, state, detail, delay)

    def _set_access(self, token: str) -> None:
        iat, exp = jwt_times(token)
        now = self.clock()
        self._access = token
        self.token_expires = exp or now + 900
        self._token_issued = iat if exp else now

    async def login(self) -> None:
        url = self.base + "/v2/jwt/login"
        payload = {"username": self.username, "password": self.password}
        try:
            async with self.session.post(url, json=payload, ssl=False, timeout=TIMEOUT) as resp:
                raw = await resp.read()
                status = resp.status
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            self._fail("unreachable", type(exc).__name__)
            return
        if status == 404:
            self._fail("api_off", "HTTP 404 auf /v2/jwt/login")
            return
        body = _parse_body(raw, "")
        if status in (401, 403):
            msg = body.get("msg", "") if isinstance(body, dict) else ""
            self._fail("auth_failed", f"HTTP {status} {msg}".strip())
            return
        if status != 200 or not isinstance(body, dict) or not body.get("accessToken"):
            self._fail("auth_failed", f"HTTP {status}, unerwartete Antwort")
            return
        self._set_access(body["accessToken"])
        self._refresh = body.get("refreshToken") or ""
        self.refresh_expires = jwt_times(self._refresh)[1] if self._refresh else 0.0
        self.auth_state, self.auth_error, self.retry_at = "ok", "", 0.0
        self.backoff.reset()
        log.info("%s: angemeldet, Access-Token bis %s", self.host,
                 time.strftime("%H:%M:%S", time.localtime(self.token_expires)))

    async def _refresh_token(self) -> bool:
        url = self.base + "/v2/jwt/refresh"
        headers = {"Authorization": "Bearer " + self._refresh}
        try:
            async with self.session.post(url, headers=headers, ssl=False, timeout=TIMEOUT) as resp:
                raw = await resp.read()
                status = resp.status
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            log.info("%s: Refresh fehlgeschlagen (%s)", self.host, type(exc).__name__)
            return False
        body = _parse_body(raw, "")
        if status != 200 or not isinstance(body, dict) or not body.get("accessToken"):
            log.info("%s: Refresh abgelehnt (HTTP %s), melde neu an", self.host, status)
            return False
        self._set_access(body["accessToken"])
        if body.get("refreshToken"):
            self._refresh = body["refreshToken"]
            self.refresh_expires = jwt_times(self._refresh)[1]
        log.info("%s: Access-Token erneuert", self.host)
        return True

    def _needs_renewal(self) -> bool:
        if not self._access:
            return True
        lifetime = max(1.0, self.token_expires - self._token_issued)
        return (self.token_expires - self.clock()) < REFRESH_AT * lifetime

    async def ensure_token(self) -> None:
        async with self._lock:
            if self.auth_state == "ok" and not self._needs_renewal():
                return
            if self.auth_state != "ok" and self.clock() < self.retry_at:
                return
            if (self.auth_state == "ok" and self._refresh
                    and self.refresh_expires - self.clock() > REFRESH_MARGIN
                    and await self._refresh_token()):
                return
            await self.login()

    # ---- Anfragen -----------------------------------------------------------------------
    async def _send(self, method: str, path: str, query, body):
        kwargs = {"ssl": False, "timeout": TIMEOUT,
                  "headers": {"Authorization": "Bearer " + self._access}}
        if query:
            kwargs["params"] = {k: v if isinstance(v, str) else json.dumps(v)
                                for k, v in query.items()}
        if body is not None:
            kwargs["json"] = body
        async with self.session.request(method, self.base + path, **kwargs) as resp:
            return resp.status, _parse_body(await resp.read(), resp.headers.get("Content-Type", ""))

    async def request(self, method: str, path: str, query: dict | None = None,
                      body: object = None) -> RestResult:
        await self.ensure_token()
        if self.auth_state != "ok":
            return RestResult(False, 0, None, self.auth_state)
        method = method.upper()
        try:
            status, data = await self._send(method, path, query, body)
            if status == 401:
                log.info("%s: %s %s → 401, melde neu an", self.host, method, path)
                async with self._lock:
                    await self.login()
                if self.auth_state != "ok":
                    return RestResult(False, 401, data, self.auth_state)
                status, data = await self._send(method, path, query, body)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            log.warning("%s: %s %s nicht erreichbar (%s)", self.host, method, path,
                        type(exc).__name__)
            return RestResult(False, 0, None, "unreachable")
        log.debug("%s: %s %s → %s", self.host, method, path, status)
        ok = 200 <= status < 300
        return RestResult(ok, status, data, "" if ok else f"HTTP {status}")
