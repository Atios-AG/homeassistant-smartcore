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

Real Atios button couplers address themselves with the Device/Instance event
scheme, which carries the instance NUMBER on the wire but not its TYPE
(``InputEvent.instance_type`` is None, so ``InputEvent.gesture`` can't map to
a name by itself — see dali.py's docstring). Confirmed on hardware, they also
only ever send raw button_pressed/button_released (event_info 0/1): no
short/double/long code has been observed on the bus, even for long holds and
back-to-back taps. ``_ButtonManager`` resolves the instance type from the
NVRAM model (``hub.input_devices``) — falling back to "assume push button" when
the coupler isn't in the model at all — and synthesizes
short_press/double_press/long_press_start/repeat/stop from the raw
press/release timing (see ``_on_raw_press``). A coupler that DOES put the type
in the frame (Device scheme) and sends real gesture codes is dispatched
directly, unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from . import AtiosConfigEntry
from .const import DOMAIN, DOUBLE_PRESS_GAP_S, EVENT_DALI, LONG_PRESS_REPEAT_S, LONG_PRESS_START_S
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


class _PressPhase(Enum):
    IDLE = auto()
    PRESSED = auto()  # button down, long-start timer running
    LONG = auto()  # long_press_start already fired, repeat timer running
    WAIT_DOUBLE = auto()  # released once, waiting to see if a 2nd press follows


@dataclass
class _PressState:
    """Per-signature state for synthesizing gestures from raw press/release."""

    phase: _PressPhase = _PressPhase.IDLE
    pending_double: bool = False
    cancel_timer: Callable[[], None] | None = None


class _ButtonManager:
    """Routes decoded input events to per-button entities, creating them on demand."""

    def __init__(self, hass, entry, hub, async_add_entities) -> None:
        self._hass = hass
        self._entry = entry
        self._hub = hub
        self._add = async_add_entities
        self.buttons: dict[str, AtiosButtonEvent] = {}
        # (short_address, instance_number) -> instance type, from the NVRAM
        # model. Needed because Device/Instance-scheme frames don't carry the
        # type themselves.
        self._instance_types: dict[tuple[int, int], int] = {
            (dev.address, inst.number): inst.type
            for dev in hub.input_devices
            for inst in dev.instances
        }
        self._press_state: dict[str, _PressState] = {}

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
        if gesture is not None:
            # Device scheme: the frame itself carried a recognised gesture.
            self._dispatch(decoded.signature, gesture, payload)
            return

        if decoded.event_info not in (0, 1):
            return  # not a raw press/release either -> bus event only
        if decoded.short_address is None or decoded.instance_number is None:
            return
        key = (decoded.short_address, decoded.instance_number)
        inst_type = self._instance_types.get(key)
        if inst_type is not None and inst_type != INSTANCE_PUSHBUTTON:
            return  # a known non-button instance (sensor etc.) -> bus event only
        # inst_type is None when the coupler isn't in the NVRAM model — it has
        # not been commissioned, or the project wasn't saved in the configurator
        # (the same Write + Save Project caveat that leaves gear names empty).
        # Raw press/release with event_info 0/1 in practice only comes from push
        # buttons, so synthesize anyway instead of dropping the button entirely.

        self._on_raw_press(decoded.signature, is_press=decoded.event_info == 1, payload=payload)

    def _dispatch(self, signature: str, gesture: str, payload: dict) -> None:
        entity = self.buttons.get(signature)
        if entity is None:
            addr = payload.get("short_address")
            inst = payload.get("instance_number")
            label = f"Button {addr}" if addr is not None else "Button"
            if inst is not None:
                label += f".{inst}"
            entity = AtiosButtonEvent(self._entry, self._hub, signature=signature, label=label)
            self.buttons[signature] = entity
            self._add([entity])
            # first frame arrives before the entity is added to hass; replay it
            entity.queue_first(gesture, payload)
            return
        entity.report(gesture, payload)

    def _on_raw_press(self, signature: str, is_press: bool, payload: dict) -> None:
        """Turn a raw button_pressed/button_released pair into a named gesture.

        State machine per signature: PRESSED starts a long-start timer; if it
        fires while still held we're in LONG (and re-arm a repeat timer on
        every fire); release before that is a short candidate that waits
        DOUBLE_PRESS_GAP_S for a second press before committing to
        short_press, so a quick second press can upgrade it to double_press.
        """
        state = self._press_state.setdefault(signature, _PressState())

        def _cancel() -> None:
            if state.cancel_timer is not None:
                state.cancel_timer()
                state.cancel_timer = None

        if is_press:
            state.pending_double = state.phase is _PressPhase.WAIT_DOUBLE
            _cancel()
            state.phase = _PressPhase.PRESSED

            @callback
            def _long_start(_now: object) -> None:
                if state.phase is not _PressPhase.PRESSED:
                    return
                state.phase = _PressPhase.LONG
                self._dispatch(signature, "long_press_start", payload)
                _arm_repeat()

            @callback
            def _repeat(_now: object) -> None:
                if state.phase is not _PressPhase.LONG:
                    return
                self._dispatch(signature, "long_press_repeat", payload)
                _arm_repeat()

            def _arm_repeat() -> None:
                state.cancel_timer = async_call_later(self._hass, LONG_PRESS_REPEAT_S, _repeat)

            state.cancel_timer = async_call_later(self._hass, LONG_PRESS_START_S, _long_start)
            return

        # release
        _cancel()
        if state.phase is _PressPhase.LONG:
            state.phase = _PressPhase.IDLE
            self._dispatch(signature, "long_press_stop", payload)
            return

        if state.phase is not _PressPhase.PRESSED:
            # A release with no matching press (lost/duplicate frame, or a
            # release seen mid-hold at startup). Ignore it rather than
            # synthesizing a phantom short_press.
            return

        if state.pending_double:
            state.phase = _PressPhase.IDLE
            state.pending_double = False
            self._dispatch(signature, "double_press", payload)
            return

        # short candidate: wait to see if a second press follows
        state.phase = _PressPhase.WAIT_DOUBLE

        @callback
        def _resolve_short(_now: object) -> None:
            if state.phase is not _PressPhase.WAIT_DOUBLE:
                return
            state.phase = _PressPhase.IDLE
            self._dispatch(signature, "short_press", payload)

        state.cancel_timer = async_call_later(self._hass, DOUBLE_PRESS_GAP_S, _resolve_short)


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
