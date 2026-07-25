"""Unit tests for the pure DALI layer.

Golden frames were captured from python-dali (the canonical DALI library) and
are hard-coded here so the suite runs without that dependency.
Run: pytest tests/  (or: python3 tests/test_dali.py)
"""

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "atios_dali", Path(__file__).resolve().parent.parent / "custom_components" / "atios" / "dali.py"
)
dali = importlib.util.module_from_spec(_spec)
sys.modules["atios_dali"] = dali
_spec.loader.exec_module(dali)

T = dali.Target


def test_frame_builders():
    assert dali.goto_scene(T.broadcast(), 0).data == [0xFF, 0x10]
    assert dali.query(T.short(0), dali.OP_QUERY_CONTROL_GEAR_PRESENT).data == [1, 145]
    assert dali.off(T.broadcast()).data == [0xFF, 0x00]
    assert dali.level(T.short(5), 254).data == [0x0A, 254]
    assert dali.recall_max(T.group(3)).data == [0x87, 0x05]
    assert dali.query(T.short(0), 0).wait_for_answer is True


def test_brightness_mapping():
    assert dali.ha_brightness_to_dali(255) == 254
    assert dali.ha_brightness_to_dali(0) == 0
    assert dali.ha_brightness_to_dali(1) >= 1
    assert dali.dali_level_to_ha_brightness(254) == 255
    assert dali.dali_level_to_ha_brightness(0) == 0


# golden 24-bit event frames from python-dali (device scheme, addr=3)
_DEVICE_ADDR3 = {
    "button_released": [6, 4, 0],
    "button_pressed": [6, 4, 1],
    "short_press": [6, 4, 2],
    "double_press": [6, 4, 5],
    "long_press_start": [6, 4, 9],
    "long_press_repeat": [6, 4, 11],
    "long_press_stop": [6, 4, 12],
    "button_free": [6, 4, 14],
    "button_stuck": [6, 4, 15],
}


def test_decode_device_scheme():
    for gesture, frame in _DEVICE_ADDR3.items():
        d = dali.decode_input_event(frame, 24)
        assert d is not None
        assert d.scheme is dali.EventScheme.DEVICE
        assert d.short_address == 3
        assert d.instance_type == dali.INSTANCE_TYPE_PUSHBUTTON
        assert d.gesture == gesture, (gesture, d.gesture)


def test_decode_addr0():
    d = dali.decode_input_event([0, 4, 1], 24)
    assert d.short_address == 0 and d.gesture == "button_pressed"


def test_decode_device_instance_scheme():
    # ShortPress, addr=3, instance_number=2 -> [6, 136, 2]
    d = dali.decode_input_event([6, 136, 2], 24)
    assert d.scheme is dali.EventScheme.DEVICE_INSTANCE
    assert d.short_address == 3 and d.instance_number == 2
    assert d.event_info == 2
    assert d.gesture is None  # type not carried in this scheme
    assert dali.PUSHBUTTON_EVENTS[d.event_info] == "short_press"


def test_non_event_frame():
    assert dali.decode_input_event([0x0A, 0x0B], 16) is None
    assert dali.decode_input_event([1, 2], 24) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
