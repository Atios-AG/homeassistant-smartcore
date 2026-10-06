"""Constants for the Atios SmartCore integration."""

from __future__ import annotations

DOMAIN = "atios"

# config entry keys
CONF_HOST = "host"
CONF_LINE = "line"

# options (panel)
CONF_PANEL = "panel_enabled"
CONF_PANEL_URL = "panel_url"
CONF_PANEL_TITLE = "panel_title"
CONF_PANEL_ICON = "panel_icon"
CONF_PANEL_ADMIN = "panel_require_admin"

# legacy option: list[int] of DALI short addresses to expose as lights.
# Superseded by the NVRAM device model; still read as a fallback when the
# nvm endpoint is unavailable (older firmware).
CONF_LIGHTS = "lights"

DEFAULT_LINE = 0
DEFAULT_PANEL = True
DEFAULT_PANEL_TITLE = "SmartCore"
DEFAULT_PANEL_ICON = "mdi:lightbulb-group"
DEFAULT_PANEL_ADMIN = True

# how a raw daliMonitor input event is surfaced on the HA event bus
EVENT_DALI = f"{DOMAIN}_dali_event"

# service names
SERVICE_SEND_FRAME = "send_dali_frame"
SERVICE_RECALL_SCENE = "recall_scene"

# WS reconnect backoff (seconds)
RECONNECT_MIN = 2
RECONNECT_MAX = 60

# Repair issue raised while the SmartCore's "DALI IP Interface" setting is off.
# The daliMonitor stream on ws://<host>/ (every button/sensor event) is only
# sent while that setting is on; it is off by default.
ISSUE_DALI_IP_DISABLED = "dali_ip_interface_disabled"

# The Lunatone socket greets every client with {"type":"info"} right after the
# handshake, whatever the settings. No Lunatone JSON within this many seconds
# means the device will not stream on this socket at all: SmartCore firmware
# built on ESP-IDF v5.5.5/v6.0.1+ (seen on 3.2.2) never registers websocket
# clients, so it drops every message it would send them.
LUNATONE_GREETING_TIMEOUT_S = 10
ISSUE_LUNATONE_SILENT = "lunatone_socket_silent"

# Software gesture timing for Device/Instance-scheme couplers.
# Confirmed on real Atios button couplers (2026-08-27): the bus only ever
# carries raw button_pressed/button_released (event_info 0/1) — short_press/
# double_press/long_press_* codes never appear, even for long holds and
# rapid double-taps. So those gestures are synthesized in event.py from the
# press/release timing instead of being read off the wire.
LONG_PRESS_START_S = 0.4  # hold time before synthesizing long_press_start
LONG_PRESS_REPEAT_S = 0.4  # interval between long_press_repeat while held
DOUBLE_PRESS_GAP_S = 0.4  # max gap between a release and the next press to count as double_press
