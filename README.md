# KEBA Wallbox for LoxBerry

A LoxBerry plugin that connects KEBA KeContact wallboxes to the Loxone Miniserver over MQTT.

It runs a small gateway between the wallboxes and the LoxBerry MQTT broker. The gateway talks to each box over its REST API (HTTPS, port 8443) and its UDP interface (port 7090), publishes the data as JSON on MQTT, and turns MQTT messages into commands for the box. MQTT Gateway V2 then forwards the values to the Miniserver.

## Documentation

- [LoxBerry Wiki (user documentation, German)](https://wiki.loxberry.de/plugins/keba/start)
- [Read endpoints with examples](https://wiki.loxberry.de/plugins/keba/endpoints_lesen)
- [Write endpoints and UDP commands with examples](https://wiki.loxberry.de/plugins/keba/endpoints_schreiben)

## Requirements

- LoxBerry 4.0.1.0 or newer
- MQTT Gateway V2
- A KEBA KeContact wallbox with REST API (tested with a P30, firmware 3.10.80, REST API 2.4.1)
- On the wallbox:
  - REST API enabled: in the wallbox web interface, System → API access settings, API state on.
  - UDP enabled: by DIP switch (P30: DSW1.3, other models see the manual). The web interface of the box then shows "Smart Home Interface ist aktiviert".
  - A fixed IP address or a DHCP reservation.

Either interface on its own works too: without UDP there are no display texts and no UDP commands such as `curr` or `setenergy`, without the REST API only the UDP commands remain.

## What the plugin does

- Overview tab: state of the gateway and of every box (REST API, UDP, firmware, charging state, plug, power and session energy), with restart and stop of the gateway.
- Settings tab: add, edit and remove boxes, test the connection before saving, open the web interface of a box.
- MQTT tab: base topic, one polling interval for everything (default 60 s), retain on/off, and the topics to subscribe to in MQTT Gateway V2.
- API endpoints tab: every endpoint the box offers, with description and topic, each one can be enabled for MQTT individually.
- Log files tab: gateway log and a diagnostics text with IP addresses, serial numbers, tokens and passwords masked, ready to paste into a forum post.

The whole web interface is protected by the LoxBerry SecurePIN.

## How it is put together

### Endpoints from the box itself

The plugin does not ship a fixed list of endpoints. When it connects to a box, it downloads the box's own `openapi.json` and builds the endpoint list from it, so it follows whatever the firmware of that box offers. The specification is cached per box and API version in `/dev/shm/keba/<serial>/` and only downloaded again when the API version changes.

Each endpoint gets a topic derived from its path, for example:

| Endpoint | Topic |
|---|---|
| `GET /v2/wallboxes/{serialNumber}` | `keba/<serial>/wallbox` |
| `GET /v2/sessions` | `keba/<serial>/sessions` |
| `POST /v2/wallboxes/{serialNumber}/start-charging` | `keba/<serial>/start-charging/post` |

A new box starts with five endpoints enabled: the wallbox status, start and stop charging, change availability and unlock. Everything else is opt-in. Payloads of write commands are validated against the schema from the box before they are sent.

Some endpoints can never be enabled: factory reset, reboot, firmware updates, login and token handling, network, routing and REST API settings, certificate changes, deleting or importing RFID cards, large exports, file downloads and `openapi.json` itself.

### Gateway process

The gateway is a single Python asyncio process (`bin/keba_gateway.py`) with one worker per box. It runs as the `loxberry` user and is started by `daemon/daemon.sh` at boot.

- Login uses the user and password of the box web interface. Access tokens (15 minutes) are refreshed with the refresh token; a new login only happens when that fails.
- A box that is off, refuses the login or has its API switched off does not affect the others. Logins are retried with a growing delay from 30 seconds to 10 minutes.
- UDP uses one shared socket on port 7090 for all boxes. Replies are matched to the box by its IP address.
- The gateway publishes its own state retained on `keba/gateway`, with a last will of `{"state": "stopped"}`.

`bin/watchdog.sh` runs every minute from cron. It restarts the gateway when the process is gone or its heartbeat is older than 3 minutes, and gives up after 5 restarts within 30 minutes. A stop from the web interface sets the flag `config/gateway_stopped`, which survives reboots and plugin upgrades until the next restart from the web interface.

The log level is taken from the LoxBerry plugin settings and picked up by the running gateway within a minute.

### Web interface

`webfrontend/htmlauth/index.cgi` renders the tabs with the LoxBerry Design System. All data comes from `webfrontend/htmlauth/ajax.cgi`, which calls `bin/keba_cli.py` for connection checks, endpoint lists and diagnostics. Passwords are handed to the CLI on stdin, never on the command line.

SecurePIN: `ajax.cgi` checks the PIN with `LoxBerry::System::check_securepin` and hands out a session token, stored as a file in `/dev/shm/keba/webui` and valid until it has not been used for an hour. Every other request needs that token.

### Configuration

Everything lives in `config/pluginconfig.json` (boxes with their credentials, enabled endpoints, MQTT settings), written atomically with mode 600 and under a lock shared by the web interface and the gateway. Runtime state (status, heartbeat, cached specifications, sessions) stays in `/dev/shm/keba` to spare the SD card.

## MQTT

All topics start with the base topic (default `keba`), followed by the serial number of the box.

| Topic | Direction | Content |
|---|---|---|
| `keba/gateway` | to Loxone | gateway state, retained |
| `keba/<serial>/status` | to Loxone | state of the box as seen by the plugin plus the main live values in fixed units, retained |
| `keba/<serial>/<endpoint>` | to Loxone | response of every enabled read endpoint as JSON |
| `keba/<serial>/<endpoint>/get` | from Loxone | read an enabled endpoint once |
| `keba/<serial>/<endpoint>/<method>` | from Loxone | command, `<method>` is `post`, `put`, `patch` or `delete` |
| `keba/<serial>/<endpoint>/<method>/result` | to Loxone | result of the command |
| `keba/<serial>/udp/set` | from Loxone | any UDP command as text |
| `keba/<serial>/display/set` | from Loxone | display text, plain or JSON with display duration |
| `keba/<serial>/udp/result` | to Loxone | reply to the last UDP or display command |

In MQTT Gateway V2 the values are subscribed individually; the gateway splits the JSON into one value per field. From Loxone, commands are sent with a virtual output to `/dev/udp/<loxberry-ip>/11884` and the command `publish <topic> <payload>`. Details and ready-made examples are in the wiki.

## Repository layout

| Path | Content |
|---|---|
| `bin/` | gateway (`keba_gateway.py`, `keba_box.py`, `keba_rest.py`, `keba_udp.py`, `keba_openapi.py`, `keba_config.py`), CLI for the web interface (`keba_cli.py`), watchdog |
| `webfrontend/htmlauth/` | `index.cgi` (pages) and `ajax.cgi` (JSON interface) |
| `templates/` | page templates, JavaScript, CSS and language files (German, English) |
| `daemon/`, `cron/` | start at boot, watchdog every minute |
| `postroot.sh`, `preupgrade.sh`, `postupgrade.sh`, `uninstall/` | installation lifecycle; `postroot.sh` installs `aiohttp`, `aiomqtt` and `paho-mqtt` with pip |
| `release.cfg`, `prerelease.cfg` | automatic plugin updates |

## Support

Questions and bug reports in the [LoxForum](https://www.loxforum.com/forum/projektforen/loxberry/plugins) or as an issue in this repository.
