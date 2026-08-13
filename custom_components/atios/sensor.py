"""Sensor platform: DALI-2 light sensors (IEC 62386-304).

Light-sensor instances found in the SmartCore's NVRAM device model become
``sensor`` entities driven by the websocket bus monitor. The part-304 event
carries the illuminance as a 10-bit level in the event information field; we
expose that raw level as the state.

NOTE: converting the raw level to lux depends on the part-304 logarithmic
encoding and the instance's resolution setting, and is not implemented until
verified against real hardware (see CONTRIBUTING.md). The state is the raw
0..1023 level, clearly named as such.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AtiosConfigEntry
from .const import DOMAIN
from .dali import decode_input_event
from .hub import MonitorFrame
from .nvram import INSTANCE_LIGHT


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AtiosConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    entities: list[AtiosLightLevelSensor] = []
    by_signature: dict[str, AtiosLightLevelSensor] = {}
    for dev in hub.input_devices:
        for inst in dev.instances:
            if inst.type != INSTANCE_LIGHT:
                continue
            sig = inst.expected_signature(dev.address)
            if sig is None or sig in by_signature:
                continue
            entity = AtiosLightLevelSensor(
                entry, signature=sig, label=f"{dev.name} light level {inst.number}"
            )
            by_signature[sig] = entity
            entities.append(entity)
    if not entities:
        return
    async_add_entities(entities)

    @callback
    def on_monitor(event: MonitorFrame) -> None:
        if getattr(event, "framing_error", False):
            return
        decoded = decode_input_event(list(event.data), event.bits)
        if decoded is None or decoded.event_info is None:
            return
        entity = by_signature.get(decoded.signature)
        if entity is not None:
            entity.handle_event_info(decoded.event_info)

    entry.async_on_unload(hub.add_monitor_listener(on_monitor))


class AtiosLightLevelSensor(SensorEntity):
    """One DALI-2 light-sensor instance from the NVRAM model (raw level)."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = False

    def __init__(
        self, entry: AtiosConfigEntry, *, signature: str, label: str
    ) -> None:
        self._attr_name = label
        self._attr_unique_id = f"{entry.unique_id}_lux_{signature}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
        )
        self._attr_native_value = None

    @callback
    def handle_event_info(self, event_info: int) -> None:
        self._attr_native_value = event_info
        if self.hass is not None:
            self.async_write_ha_state()
