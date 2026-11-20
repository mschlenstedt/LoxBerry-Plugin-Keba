"""Plugin-Konfiguration (pluginconfig.json) laden und speichern.

Geschrieben wird nur bei Benutzeraktionen; die Datei enthält die Passwörter der
Boxen und bekommt deshalb Modus 600.
"""
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

MIN_INTERVAL = 5


class ConfigError(Exception):
    pass


@dataclass
class MqttConfig:
    base_topic: str = "keba"
    core_interval: int = 10
    default_interval: int = 60
    retain: bool = True


@dataclass
class EnabledOp:
    op: str
    interval: int | None = None


@dataclass
class BoxConfig:
    id: str
    name: str = ""
    host: str = ""
    username: str = ""
    password: str = ""
    active: bool = True
    serial: str = ""
    enabled: list[EnabledOp] = field(default_factory=list)


@dataclass
class Config:
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    boxes: list[BoxConfig] = field(default_factory=list)


def _interval(value, default):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(MIN_INTERVAL, value)


def _box(raw: dict) -> BoxConfig:
    enabled = []
    for e in raw.get("enabled") or []:
        if not isinstance(e, dict) or not e.get("op"):
            continue
        iv = e.get("interval")
        enabled.append(EnabledOp(str(e["op"]), None if iv is None else _interval(iv, None)))
    return BoxConfig(
        id=str(raw.get("id", "")),
        name=str(raw.get("name", "")),
        host=str(raw.get("host", "")),
        username=str(raw.get("username", "")),
        password=str(raw.get("password", "")),
        active=bool(raw.get("active", True)),
        serial=str(raw.get("serial", "")),
        enabled=enabled,
    )


def load_config(path: Path) -> Config:
    path = Path(path)
    if not path.exists():
        return Config()
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: kein JSON-Objekt")
    m = raw.get("mqtt") or {}
    d = MqttConfig()
    mqtt = MqttConfig(
        base_topic=str(m.get("base_topic") or d.base_topic).strip("/"),
        core_interval=_interval(m.get("core_interval"), d.core_interval),
        default_interval=_interval(m.get("default_interval"), d.default_interval),
        retain=bool(m.get("retain", d.retain)),
    )
    boxes = [_box(b) for b in raw.get("boxes") or [] if isinstance(b, dict)]
    return Config(mqtt=mqtt, boxes=boxes)


def save_config(path: Path, cfg: Config) -> None:
    path = Path(path)
    data = json.dumps(asdict(cfg), indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(prefix=".pluginconfig.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
