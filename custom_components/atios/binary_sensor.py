"""Binary sensor platform: DALI-2 occupancy sensors (IEC 62386-303).

Occupancy instances found in the SmartCore's NVRAM device model become
``binary_sensor`` entities driven by the websocket bus monitor. State comes
from the part-303 event information bits: bit 1 = occupied/vacant,
bit 0 = movement.

NOTE: the bit mapping is implemented from the spec and not yet verified
against real part-303 hardware (see the bring-up checklist in
CONTRIBUTING.md). The raw event info is exposed as an attribute so it can be
calibrated from the HA log / `atios_dali_event` without a code change.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AtiosConfigEntry
from .const import DOMAIN
from .dali import decode_input_event
from .hub import MonitorFrame
from .nvram import INSTANCE_OCCUPANCY

OCCUPIED_BIT = 0b10
MOVEMENT_BIT = 0b01


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AtiosConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    entities: list[AtiosOccupancySensor] = []
    by_signature: dict[str, AtiosOccupancySensor] = {}
    for dev in hub.input_devices:
        for inst in dev.instances:
            if inst.type != INSTANCE_OCCUPANCY:
                continue
            sig = inst.expected_signature(dev.address)
            if sig is None or sig in by_signature:
                continue
            entity = AtiosOccupancySensor(
                entry, signature=sig, label=f"{dev.name} occupancy {inst.number}"
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


class AtiosOccupancySensor(BinarySensorEntity):
    """One DALI-2 occupancy sensor instance from the NVRAM model."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_should_poll = False

    def __init__(
        self, entry: AtiosConfigEntry, *, signature: str, label: str
    ) -> None:
        self._attr_name = label
        self._attr_unique_id = f"{entry.unique_id}_occ_{signature}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
        )
        self._attr_is_on = None  # unknown until the first event

    @callback
    def handle_event_info(self, event_info: int) -> None:
        self._attr_is_on = bool(event_info & OCCUPIED_BIT)
        self._attr_extra_state_attributes = {
            "movement": bool(event_info & MOVEMENT_BIT),
            "event_info": event_info,
        }
        if self.hass is not None:
            self.async_write_ha_state()
