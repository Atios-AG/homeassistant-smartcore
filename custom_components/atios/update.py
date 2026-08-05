"""Firmware update entity for Atios SmartCore.

Reads installed firmware from GET /ota_status (``version_string``). The device
also exposes ``update_status`` and checks Atios' cloud (cloud.atios.ch) for the
latest build; a local "latest version" source isn't confirmed yet, so until
then we report installed == latest (shows "up to date") and surface the raw
``update_status`` as an attribute. Wiring a real latest-version source (an Atios
version endpoint or their GitHub releases) is the remaining half of bounty #3.

No install() is implemented: triggering an OTA blindly is unsafe, so firmware
updates stay a notification-only entity until Atios confirm the trigger API.
"""

from __future__ import annotations

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AtiosConfigEntry
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AtiosConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([AtiosFirmwareUpdate(entry.runtime_data, entry)])


class AtiosFirmwareUpdate(UpdateEntity):
    """SmartCore firmware version, reported from /ota_status."""

    _attr_has_entity_name = True
    _attr_name = "Firmware"
    _attr_should_poll = True
    _attr_supported_features = UpdateEntityFeature.INSTALL

    def __init__(self, hub, entry: AtiosConfigEntry) -> None:
        self._hub = hub
        self._attr_unique_id = f"{entry.unique_id}_firmware"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
            manufacturer="Atios",
            model="SmartCore",
            name=f"SmartCore ({hub.host})",
            serial_number=hub.serial,
            sw_version=hub.sw_version,
        )

    @property
    def installed_version(self) -> str | None:
        return self._hub.sw_version

    @property
    def latest_version(self) -> str | None:
        # No confirmed local "latest" source yet -> report up to date. Atios are
        # adding an 'ota_update_check' endpoint; once it reports the available
        # version, return it here and HA will show the update + Install button.
        return self._hub.sw_version

    @property
    def release_url(self) -> str | None:
        # Release notes live in the embedded SmartCore web UI.
        return f"http://{self._hub.host}/"

    @property
    def extra_state_attributes(self) -> dict:
        info = self._hub.info
        return {
            "update_status": info.get("update_status"),
            "firmware_channel": info.get("firmware_channel"),
            "version_number": info.get("version_number"),
        }

    async def async_install(self, version, backup, **kwargs) -> None:
        """Trigger the OTA update (POST /cmd/ota_update)."""
        await self._hub.async_trigger_ota()

    async def async_update(self) -> None:
        await self._hub.async_fetch_info()
