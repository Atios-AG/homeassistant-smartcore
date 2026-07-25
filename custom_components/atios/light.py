"""Light platform for Atios SmartCore (raw DALI control gear).

First cut exposes a single broadcast light so control is verifiable the moment
the device is connected. Per-address and per-group lights are created from an
options list once the bus is scanned/known — the plumbing is here; wire the
address list through an options flow or a static list in a follow-up.

Brightness is sent as a DAPC level; on/off use RECALL MAX / OFF. State is read
back with QUERY ACTUAL LEVEL where the target is a single address (broadcast
and group queries collide on the bus, so those stay optimistic).
"""

from __future__ import annotations

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_info import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AtiosConfigEntry
from .const import DOMAIN
from .dali import (
    Target,
    TargetType,
    dali_level_to_ha_brightness,
    ha_brightness_to_dali,
    level,
    off,
    query_actual_level,
    recall_max,
)
from .hub import AtiosHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AtiosConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    entities: list[AtiosLight] = [AtiosLight(hub, entry, Target.broadcast())]
    # TODO: append AtiosLight(hub, entry, Target.short(a)) for each discovered
    # short address once bus scan / options flow is in place.
    async_add_entities(entities)


class AtiosLight(LightEntity):
    """A DALI control-gear target exposed as an HA light."""

    _attr_has_entity_name = True
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(self, hub: AtiosHub, entry: AtiosConfigEntry, target: Target) -> None:
        self._hub = hub
        self._target = target
        self._attr_unique_id = f"{entry.unique_id}_{target.unique_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
            manufacturer="Atios",
            model="SmartCore",
            name=f"SmartCore ({hub.host})",
        )
        if target.type is TargetType.BROADCAST:
            self._attr_name = "All lights"
        elif target.type is TargetType.GROUP:
            self._attr_name = f"Group {target.number}"
        else:
            self._attr_name = f"Light {target.number}"
        # broadcast/group can't be reliably queried -> optimistic
        self._attr_assumed_state = target.type is not TargetType.SHORT
        self._attr_is_on = False
        self._attr_brightness: int | None = None

    async def async_turn_on(self, **kwargs) -> None:
        if ATTR_BRIGHTNESS in kwargs:
            dali = ha_brightness_to_dali(kwargs[ATTR_BRIGHTNESS])
            await self._hub.send_frame(level(self._target, dali))
            self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
        else:
            await self._hub.send_frame(recall_max(self._target))
            self._attr_brightness = 255
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._hub.send_frame(off(self._target))
        self._attr_is_on = False
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Poll actual level for single-address lights only."""
        if self._target.type is not TargetType.SHORT:
            return
        answer = await self._hub.send_frame(query_actual_level(self._target))
        if not answer:
            return
        lvl = answer[0]
        if lvl in (255, None):  # MASK / no answer
            return
        self._attr_is_on = lvl > 0
        self._attr_brightness = dali_level_to_ha_brightness(lvl) if lvl > 0 else None
