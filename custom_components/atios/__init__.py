"""The Atios SmartCore integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_LINE,
    DEFAULT_LINE,
    DOMAIN,
    SERVICE_RECALL_SCENE,
    SERVICE_SEND_FRAME,
)
from .dali import Frame, Target, goto_scene
from .hub import AtiosHub
from .nvram import parse_control_devices, parse_input_devices
from .panel import async_register_panel, async_remove_panel

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.SENSOR,
    Platform.UPDATE,
]

type AtiosConfigEntry = ConfigEntry[AtiosHub]


async def async_setup_entry(hass: HomeAssistant, entry: AtiosConfigEntry) -> bool:
    """Set up Atios SmartCore from a config entry."""
    session = async_get_clientsession(hass)
    hub = AtiosHub(entry.data[CONF_HOST], entry.data.get(CONF_LINE, DEFAULT_LINE), session)
    await hub.async_fetch_info()  # serial + firmware version for device info / update entity

    # Read the configured device model from NVRAM (same endpoint the web
    # configurator uses). On failure the platforms fall back to their legacy
    # behaviour (options address list / discover-on-first-event).
    raw_control = await hub.async_fetch_nvm_section("control_devices")
    raw_inputs = await hub.async_fetch_nvm_section("input_devices")
    if raw_control is not None:
        hub.control_devices = parse_control_devices(raw_control)
    if raw_inputs is not None:
        hub.input_devices = parse_input_devices(raw_inputs)

    await hub.async_start()
    entry.runtime_data = hub

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _register_services(hass)
    await async_register_panel(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: AtiosConfigEntry) -> None:
    """Reload on options change (recreates light entities and re-applies panel)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AtiosConfigEntry) -> bool:
    """Unload a config entry."""
    async_remove_panel(hass, entry)
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


def _register_services(hass: HomeAssistant) -> None:
    """Register domain services once."""
    if hass.services.has_service(DOMAIN, SERVICE_SEND_FRAME):
        return

    def _first_hub() -> AtiosHub:
        entries: list[AtiosConfigEntry] = hass.config_entries.async_entries(DOMAIN)
        loaded = [e for e in entries if getattr(e, "runtime_data", None)]
        if not loaded:
            raise RuntimeError("No Atios SmartCore configured")
        return loaded[0].runtime_data

    async def _send_frame(call: ServiceCall) -> None:
        hub = _first_hub()
        await hub.send_frame(
            Frame(
                data=list(call.data["data"]),
                bits=call.data.get("bits", 16),
                send_twice=call.data.get("send_twice", False),
                wait_for_answer=call.data.get("wait_for_answer", False),
            )
        )

    async def _recall_scene(call: ServiceCall) -> None:
        hub = _first_hub()
        scene = call.data["scene"]
        addr = call.data.get("address")
        group = call.data.get("group")
        if group is not None:
            target = Target.group(group)
        elif addr is not None:
            target = Target.short(addr)
        else:
            target = Target.broadcast()
        await hub.send_frame(goto_scene(target, scene))

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_FRAME,
        _send_frame,
        schema=vol.Schema(
            {
                vol.Required("data"): [vol.All(int, vol.Range(min=0, max=255))],
                vol.Optional("bits", default=16): vol.In([16, 24, 25]),
                vol.Optional("send_twice", default=False): cv.boolean,
                vol.Optional("wait_for_answer", default=False): cv.boolean,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RECALL_SCENE,
        _recall_scene,
        schema=vol.Schema(
            {
                vol.Required("scene"): vol.All(int, vol.Range(min=0, max=15)),
                vol.Exclusive("address", "target"): vol.All(int, vol.Range(min=0, max=63)),
                vol.Exclusive("group", "target"): vol.All(int, vol.Range(min=0, max=15)),
            }
        ),
    )
