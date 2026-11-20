"""Endpunktmodell aus der openapi.json einer KEBA-Box.

Jede Operation (METHODE Pfad) wird ein Operation-Eintrag mit MQTT-Topic, Status
(available/blocked) und den aufgelösten Schemas für Doku und Prüfung.
"""
import re
from dataclasses import dataclass, field

METHODS = ("get", "post", "put", "patch", "delete")
SERIAL_PARAMS = ("serialnumber", "serial")

# Beim Anlegen einer Box vorausgewählt; danach frei ab- und zuschaltbar wie jeder andere Endpunkt.
DEFAULT_OPS = (
    "GET /v2/wallboxes/{serialNumber}",
    "POST /v2/wallboxes/{serialNumber}/start-charging",
    "POST /v2/wallboxes/{serialNumber}/stop-charging",
    "POST /v2/wallboxes/{serialNumber}/change-availability",
    "POST /v2/wallboxes/{serialNumber}/unlock",
)

_DANGER = "gefährlich"
_EXPORT = "teurer Export"
_AUTH = "Anmeldung verwaltet das Gateway"
_ACCESS = "kann den Zugang zur Box abschneiden"

# (Methoden oder None für alle, Regex auf den Pfad, Grund)
_BLOCKLIST = [
    (None, r"/jwt/", _AUTH),
    (None, r"/systems/factoryreset", _DANGER),
    (None, r"/systems/reboot", _DANGER),
    (None, r"/wallboxes/\{[^}]+\}/reboot$", _DANGER),
    (None, r"/wallboxes/notify-reboot", _DANGER),
    (None, r"/updates(/|$)", _DANGER),
    ({"DELETE"}, r"^/v\d+/rfids/?$", _DANGER),
    (None, r"/rfids/import", _DANGER),
    (None, r"/configs/webui/https", _DANGER),
    ({"POST", "PUT", "PATCH", "DELETE"}, r"certificate", _DANGER),
    (None, r"/network_routes", _DANGER),
    ({"POST", "PUT", "PATCH", "DELETE"}, r"/configs/(restapi|network)(/|$)", _ACCESS),
    (None, r"/securitylogs/dismiss", _DANGER),
    (None, r"/diagnostics/export", _EXPORT),
    (None, r"/sessions/export", _EXPORT),
    (None, r"/rfids/export", _EXPORT),
    (None, r"/securitylogs/export", _EXPORT),
    (None, r"/mvas/export", _EXPORT),
]


@dataclass
class Operation:
    op: str
    method: str
    path: str
    tag: str
    summary: str
    description: str
    path_params: list[str]
    query_params: list[dict]
    body_schema: dict | None
    response_schema: dict | None
    topic: str
    base_status: str
    block_reason: str
    pollable: bool
    serial_param: str = ""
    body_content_type: str = ""


def _is_serial(name: str) -> bool:
    return name.lower() in SERIAL_PARAMS


def derive_topic(path: str) -> str:
    segs = [s for s in path.strip("/").split("/") if s]
    serial_idx = [i for i, s in enumerate(segs)
                  if s.startswith("{") and s.endswith("}") and _is_serial(s[1:-1])]
    if len(segs) >= 3 and segs[0] == "v2" and segs[1] == "wallboxes" and 2 in serial_idx:
        rest = segs[3:]
        if not rest:
            return "wallbox"
        segs = rest
    else:
        segs = [s for i, s in enumerate(segs) if i not in serial_idx]
        if segs and segs[0] == "v2":
            segs = segs[1:]
    segs = [s[1:-1] if s.startswith("{") and s.endswith("}") else s for s in segs]
    return re.sub(r"[^A-Za-z0-9_/-]", "_", "/".join(segs))


def block_reason(method: str, path: str) -> str:
    method = method.upper()
    for methods, pattern, reason in _BLOCKLIST:
        if methods is not None and method not in methods:
            continue
        if re.search(pattern, path):
            return reason
    return ""


def _resolve(node, spec: dict, seen: frozenset = frozenset(), depth: int = 0):
    """$ref in components auflösen; Zyklen und zu tiefe Bäume bleiben als $ref stehen."""
    if isinstance(node, list):
        return [_resolve(x, spec, seen, depth) for x in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str):
        if ref in seen or depth > 12:
            return {"$ref": ref}
        target = _lookup(ref, spec)
        if target is None:
            return {"$ref": ref}
        return _resolve(target, spec, seen | {ref}, depth + 1)
    return {k: _resolve(v, spec, seen, depth + 1) for k, v in node.items()}


def _lookup(ref: str, spec: dict):
    if not ref.startswith("#/"):
        return None
    node = spec
    for part in ref[2:].split("/"):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _response_schema(o: dict) -> dict | None:
    responses = o.get("responses") or {}
    for code in sorted(k for k in responses if str(k).startswith("2")):
        content = (responses[code] or {}).get("content") or {}
        schema = (content.get("application/json") or {}).get("schema")
        if schema is not None:
            return schema
    return None


def _body(o: dict) -> tuple[dict | None, str]:
    content = (o.get("requestBody") or {}).get("content") or {}
    if not content:
        return None, ""
    ctype = "application/json" if "application/json" in content else next(iter(content))
    return (content[ctype] or {}).get("schema") or {}, ctype


def parse_openapi(spec: dict) -> list[Operation]:
    ops: list[Operation] = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters") or []
        for method in METHODS:
            o = item.get(method)
            if not isinstance(o, dict):
                continue
            m = method.upper()
            params = [p for p in shared + (o.get("parameters") or []) if isinstance(p, dict)]
            serial_param, path_params, query_params = "", [], []
            for p in params:
                name = str(p.get("name", ""))
                if p.get("in") == "path":
                    if _is_serial(name):
                        serial_param = name
                    elif name not in path_params:
                        path_params.append(name)
                elif p.get("in") == "query":
                    query_params.append({"name": name, "required": bool(p.get("required")),
                                         "schema": _resolve(p.get("schema") or {}, spec)})
            body, ctype = _body(o)
            resp = _response_schema(o)
            key = f"{m} {path}"
            topic = derive_topic(path)
            reason = block_reason(m, path) or ("" if topic else "kein Datenendpunkt")
            status = "blocked" if reason else "available"
            pollable = (m == "GET" and not reason and not path_params
                        and not any(q["required"] for q in query_params))
            tags = o.get("tags") or []
            ops.append(Operation(
                op=key, method=m, path=path,
                tag=str(tags[0]) if tags else (path.strip("/").split("/") or [""])[0],
                summary=str(o.get("summary") or ""),
                description=str(o.get("description") or ""),
                path_params=path_params, query_params=query_params,
                body_schema=None if body is None else _resolve(body, spec),
                response_schema=None if resp is None else _resolve(resp, spec),
                topic=topic, base_status=status, block_reason=reason,
                pollable=pollable, serial_param=serial_param, body_content_type=ctype,
            ))
    _dedupe_topics(ops)
    return ops


def _dedupe_topics(ops: list[Operation]) -> None:
    """Gleiches (Topic, Methode) bei mehreren Operationen: v2 vor v1, kürzester Pfad behält das
    Topic; v1-Pfade bekommen `_v1`, weitere Kollisionen `_2`, `_3` …"""
    taken: set[tuple[str, str]] = set()
    order = sorted(ops, key=lambda o: (o.path.startswith("/v1"), len(o.path), o.path))
    for o in order:
        topic = o.topic
        if (topic, o.method) in taken and o.path.startswith("/v1") and not topic.startswith("v1/"):
            topic = f"{o.topic}_v1"
        n = 2
        while (topic, o.method) in taken:
            topic = f"{o.topic}_{n}"
            n += 1
        o.topic = topic
        taken.add((topic, o.method))


def op_status(op: Operation, enabled: set[str]) -> str:
    if op.base_status == "blocked":
        return "blocked"
    return "enabled" if op.op in enabled else "available"


def missing_enabled(ops: list[Operation], enabled: list[str]) -> list[str]:
    known = {o.op for o in ops}
    return [e for e in enabled if e not in known]


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(w[:1].upper() + w[1:] for w in rest)


_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(schema, value, spec: dict, where: str = "body", _depth: int = 0) -> list[str]:
    """Kleiner Validator für type/required/properties/enum/items/$ref/allOf/anyOf/oneOf."""
    if not isinstance(schema, dict) or not schema or _depth > 30:
        return []
    if "$ref" in schema:
        target = _lookup(schema["$ref"], spec)
        return [] if target is None else validate(target, value, spec, where, _depth + 1)
    errors: list[str] = []
    for sub in schema.get("allOf") or []:
        errors += validate(sub, value, spec, where, _depth + 1)
    for key in ("anyOf", "oneOf"):
        alts = schema.get(key) or []
        if alts and all(validate(s, value, spec, where, _depth + 1) for s in alts):
            errors.append(f"{where}: passt zu keiner erlaubten Form")
    typ = schema.get("type")
    if isinstance(typ, str) and typ in _TYPES and not _TYPES[typ](value):
        if not (value is None and schema.get("nullable")):
            return errors + [f"{where}: erwartet {typ}, bekommen {type(value).__name__}"]
    if "enum" in schema and isinstance(schema["enum"], list) and value not in schema["enum"]:
        errors.append(f"{where}: {value!r} nicht in {schema['enum']}")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for req in schema.get("required") or []:
            name = req if (req in props or not props) else _camel(req)
            if (props and name not in props) or name in value:
                continue
            errors.append(f"{where}: Pflichtfeld {name} fehlt")
        for name, sub in props.items():
            if name in value:
                errors += validate(sub, value[name], spec, f"{where}.{name}", _depth + 1)
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            errors += validate(schema["items"], item, spec, f"{where}[{i}]", _depth + 1)
    return errors
