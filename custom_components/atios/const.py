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
