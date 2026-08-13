"""Event platform for Atios SmartCore DALI-2 input devices (push buttons).

Push-button instances found in the SmartCore's NVRAM device model are created
upfront with their configured device names. Buttons not in the model still
auto-appear as ``event`` entities the first time they are pressed: the monitor
stream is decoded (IEC 62386-103 / -301), and each unique
(short address, instance) signature gets its own entity with named gestures
(short_press, double_press, long_press_start, ...). Every decoded frame is also
re-emitted on the HA event bus as ``atios_dali_event`` for hand-built
automations and for calibrating non-button instance types.

The decoder is verified byte-for-byte against python-dali; see dali.py.
"""

from __future__ import annotations

import logging

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AtiosConfigEntry
from .const import DOMAIN, EVENT_DALI
from .dali import (
    PUSHBUTTON_EVENTS,
    decode_input_event,
)
from .hub import MonitorFrame
from .nvram import INSTANCE_PUSHBUTTON

_LOGGER = logging.getLogger(__name__)

# gestures an auto-created button entity can report
BUTTON_EVENT_TYPES = sorted(set(PUSHBUTTON_EVENTS.values()))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AtiosConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    manager = _ButtonManager(hass, entry, hub, async_add_entities)

    # pre-create buttons known from the NVRAM device model
    precreated: list[AtiosButtonEvent] = []
    for dev in hub.input_devices:
        for inst in dev.instances:
            if inst.type != INSTANCE_PUSHBUTTON:
                continue
            sig = inst.expected_signature(dev.address)
            if sig is None or sig in manager.buttons:
                continue
            entity = AtiosButtonEvent(
                entry, hub, signature=sig, label=f"{dev.name} button {inst.number}"
            )
            manager.buttons[sig] = entity
            precreated.append(entity)
    if precreated:
        async_add_entities(precreated)

    entry.async_on_unload(hub.add_monitor_listener(manager.on_monitor))


class _ButtonManager:
    """Routes decoded input events to per-button entities, creating them on demand."""

    def __init__(self, hass, entry, hub, async_add_entities) -> None:
        self._hass = hass
        self._entry = entry
        self._hub = hub
        self._add = async_add_entities
        self.buttons: dict[str, AtiosButtonEvent] = {}

    @callback
    def on_monitor(self, event: MonitorFrame) -> None:
        if getattr(event, "framing_error", False):
            return
        decoded = decode_input_event(list(event.data), event.bits)
        if decoded is None:
            return  # 16-bit control-gear traffic, not an input event

        payload = {
            "raw": decoded.raw,
            "bits": event.bits,
            "line": getattr(event, "line", None),
            "scheme": decoded.scheme.name.lower(),
            "short_address": decoded.short_address,
            "instance_type": decoded.instance_type,
            "instance_number": decoded.instance_number,
            "event_info": decoded.event_info,
            "gesture": decoded.gesture,
            "signature": decoded.signature,
        }
        # always publish the raw decoded event for automations / calibration
        self._hass.bus.async_fire(EVENT_DALI, payload)

        gesture = decoded.gesture
        if gesture is None:
            return  # not a (recognised) push-button gesture -> bus event only

        entity = self.buttons.get(decoded.signature)
        if entity is None:
            addr = decoded.short_address
            inst = decoded.instance_number
            label = f"Button {addr}" if addr is not None else "Button"
            if inst is not None:
                label += f".{inst}"
            entity = AtiosButtonEvent(
                self._entry, self._hub, signature=decoded.signature, label=label
            )
            self.buttons[decoded.signature] = entity
            self._add([entity])
            # first frame arrives before the entity is added to hass; replay it
            entity.queue_first(gesture, payload)
            return
        entity.report(gesture, payload)


class AtiosButtonEvent(EventEntity):
    """One DALI-2 push button, from the NVRAM model or discovered on the bus."""

    _attr_has_entity_name = True
    _attr_event_types = BUTTON_EVENT_TYPES
    _attr_should_poll = False

    def __init__(
        self, entry: AtiosConfigEntry, hub, *, signature: str, label: str
    ) -> None:
        self._signature = signature
        self._attr_name = label
        self._attr_unique_id = f"{entry.unique_id}_btn_{signature}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
        )
        self._pending: tuple[str, dict] | None = None

    def queue_first(self, gesture: str, payload: dict) -> None:
        """Hold the frame that triggered creation until we're added to hass."""
        self._pending = (gesture, payload)

    async def async_added_to_hass(self) -> None:
        if self._pending is not None:
            gesture, payload = self._pending
            self._pending = None
            self.report(gesture, payload)

    @callback
    def report(self, gesture: str, payload: dict) -> None:
        if self.hass is None:
            self._pending = (gesture, payload)
            return
        self._trigger_event(gesture, payload)
        self.async_write_ha_state()
