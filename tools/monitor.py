#!/usr/bin/env python3
"""Standalone DALI monitor/tracer for calibrating the Atios SmartCore.

Native aiohttp websocket client (no dali2iot). Connects to the SmartCore's
emulated Lunatone websocket, prints every message raw, and decodes anything that
looks like a monitored DALI frame with the integration's own decoder.

    pip install aiohttp
    python3 tools/monitor.py 10.10.10.6            # default ws://<host>/
    python3 tools/monitor.py 10.10.10.6 ws://10.10.10.6/dali/ws   # explicit URL

Use this to (a) discover the correct ws URL — try a few paths until one connects —
and (b) confirm the daliMonitor envelope by pressing physical buttons and reading
the raw JSON printed for each frame.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import aiohttp

_p = Path(__file__).resolve().parent.parent / "custom_components" / "atios" / "dali.py"
_spec = importlib.util.spec_from_file_location("atios_dali", _p)
dali = importlib.util.module_from_spec(_spec)
sys.modules["atios_dali"] = dali
_spec.loader.exec_module(dali)

# candidate ws paths to try if none given (SmartCore ws endpoint unconfirmed)
CANDIDATES = ["/", "/ws", "/dali/ws", "/api/ws", "/socket"]


async def _try(session, url):
    try:
        ws = await session.ws_connect(url, heartbeat=30, timeout=aiohttp.ClientTimeout(total=8))
        return ws
    except Exception as e:  # noqa: BLE001
        print(f"  {url}  -> {type(e).__name__}: {e}")
        return None


async def main(host: str, explicit: str | None) -> None:
    base = host.replace("http://", "").replace("https://", "").rstrip("/")
    async with aiohttp.ClientSession() as session:
        ws = None
        if explicit:
            print(f"connecting to {explicit} ...")
            ws = await _try(session, explicit)
        else:
            print("probing ws endpoints:")
            for path in CANDIDATES:
                url = f"ws://{base}{path}"
                ws = await _try(session, url)
                if ws is not None:
                    print(f"connected: {url}\n")
                    break
        if ws is None:
            print("\nno ws endpoint connected — try an explicit URL as 2nd arg, or")
            print("check the SmartCore docs / DALI Cockpit for the websocket path.")
            return

        print("listening — press physical buttons (Ctrl-C to stop)\n")
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            print("RAW:", msg.data)
            try:
                obj = json.loads(msg.data)
            except Exception:  # noqa: BLE001
                continue
            payload = obj.get("data", obj) if isinstance(obj, dict) else {}
            data = payload.get("data") or payload.get("dali_data") or payload.get("frame")
            bits = payload.get("bits") or payload.get("number_of_bits")
            if isinstance(data, list) and isinstance(bits, int):
                dec = dali.decode_input_event([int(b) & 0xFF for b in data], bits)
                if dec:
                    print(f"     decoded: addr={dec.short_address} type={dec.instance_type} "
                          f"gesture={dec.gesture or '?'}  scheme={dec.scheme.name.lower()}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 tools/monitor.py <host> [ws-url]")
        raise SystemExit(1)
    try:
        asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
    except KeyboardInterrupt:
        pass
