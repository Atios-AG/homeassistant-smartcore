#!/usr/bin/env python3
"""Standalone DALI bus tracer for calibrating the Atios SmartCore integration.

Connects straight to a SmartCore's emulated Lunatone websocket, decodes every
monitored frame with the integration's own decoder, and prints it. Use this to
confirm button gestures without running Home Assistant:

    pip install lunatone-dali2-iot
    python3 tools/monitor.py 10.10.20.x

Press each physical button once per gesture and watch the decoded lines. If a
button uses the Device/Instance scheme (gesture shows as "?"), note its
event_info and map it via PUSHBUTTON_EVENTS in dali.py.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

# import the pure decoder from the integration without needing Home Assistant
_dali_path = Path(__file__).resolve().parent.parent / "custom_components" / "atios" / "dali.py"
_spec = importlib.util.spec_from_file_location("atios_dali", _dali_path)
dali = importlib.util.module_from_spec(_spec)
sys.modules["atios_dali"] = dali
_spec.loader.exec_module(dali)

from dali2iot import DaliAnswerEvent, DaliMonitorEvent, WebSocketClient


async def main(host: str) -> None:
    base = host if host.startswith("http") else f"http://{host}"
    print(f"connecting to {base} ... (Ctrl-C to stop)\n")
    async with WebSocketClient(base_url=base) as ws:
        async for event in ws:
            if isinstance(event, DaliMonitorEvent):
                data = list(event.data)
                hexs = " ".join(f"{b:02X}" for b in data)
                dec = dali.decode_input_event(data, event.bits)
                if dec is None:
                    print(f"[{event.bits:>2}b] {hexs:<12} gear/other")
                    continue
                g = dec.gesture or "?"
                print(
                    f"[{event.bits:>2}b] {hexs:<12} "
                    f"scheme={dec.scheme.name.lower():<15} "
                    f"addr={dec.short_address} inst#={dec.instance_number} "
                    f"type={dec.instance_type} info=0x{(dec.event_info or 0):03X} "
                    f"gesture={g}"
                )
            elif isinstance(event, DaliAnswerEvent):
                print(f"     answer line={event.line} data={event.dali_data}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python3 tools/monitor.py <smartcore-ip>")
        raise SystemExit(1)
    try:
        asyncio.run(main(sys.argv[1]))
    except KeyboardInterrupt:
        pass
