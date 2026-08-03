#!/usr/bin/env python3
"""Bring-up probe for the Atios SmartCore over the confirmed HTTP interface.

Uses POST /api/dali/iface (the channel Atios/community confirmed) to drive and
query the bus synchronously, so we can find commissioned addresses and learn the
exact HTTP response shape BEFORE trusting it in the integration.

Zero dependencies (stdlib urllib). Frame construction is reused from the
integration's dali.py, so this also validates that module against real hardware.

Usage:
    python3 tools/probe.py 10.10.10.6 info
    python3 tools/probe.py 10.10.10.6 raw 1 160        # send bytes [1,160], wait for answer
    python3 tools/probe.py 10.10.10.6 scan             # find commissioned short addresses
    python3 tools/probe.py 10.10.10.6 max              # broadcast GO TO MAX
    python3 tools/probe.py 10.10.10.6 off              # broadcast OFF
    python3 tools/probe.py 10.10.10.6 level 127 [addr] # DAPC level (broadcast or short addr)
    python3 tools/probe.py 10.10.10.6 query 0          # QUERY ACTUAL LEVEL of address 0
"""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# reuse the integration's frame builders
_p = Path(__file__).resolve().parent.parent / "custom_components" / "atios" / "dali.py"
_spec = importlib.util.spec_from_file_location("atios_dali", _p)
dali = importlib.util.module_from_spec(_spec)
sys.modules["atios_dali"] = dali
_spec.loader.exec_module(dali)
T = dali.Target


def _post(host: str, frame: dali.Frame, verbose: bool = False):
    payload = {
        "repeat_twice": frame.send_twice,
        "wait_response": frame.wait_for_answer,
        "bits": frame.bits,
        "data": list(frame.data),
    }
    url = f"http://{host}/api/dali/iface"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode(errors="replace")
            code = resp.status
    except urllib.error.HTTPError as e:
        body, code = e.read().decode(errors="replace"), e.code
    except Exception as e:  # noqa: BLE001
        return None, f"ERROR: {e}"
    if verbose:
        print(f"  -> HTTP {code}  body={body!r}")
    try:
        return json.loads(body), code
    except Exception:  # noqa: BLE001
        return body, code


def _answer_value(parsed):
    """Best-effort extraction of the answer byte(s) from an unknown response shape."""
    if isinstance(parsed, dict):
        for k in ("data", "dali_data", "response", "answer", "value", "result"):
            if k in parsed and parsed[k] not in (None, ""):
                return parsed[k]
    return parsed


def cmd_info(host):
    print("Sending QUERY CONTROL GEAR PRESENT to address 0 and dumping raw reply:")
    parsed, code = _post(host, dali.query(T.short(0), dali.OP_QUERY_CONTROL_GEAR_PRESENT), verbose=True)
    print("  parsed:", parsed)


def cmd_raw(host, args):
    data = [int(x, 0) for x in args]
    parsed, code = _post(host, dali.Frame(data=data, bits=16, wait_for_answer=True), verbose=True)
    print("  parsed:", parsed)


def cmd_scan(host):
    print("Scanning short addresses 0..63 (QUERY ACTUAL LEVEL)...")
    found = []
    first = True
    for a in range(64):
        parsed, code = _post(host, dali.query_actual_level(T.short(a)), verbose=first)
        first = False
        val = _answer_value(parsed)
        # a present gear returns a level byte; absent -> empty/None/no-answer
        has = val not in (None, "", [], {}) and not (isinstance(val, str) and val.strip() == "")
        if has:
            found.append((a, val))
            print(f"  addr {a:2d}: level={val}")
    print(f"\nCommissioned addresses: {[a for a,_ in found] or 'none found'}")
    if not found:
        print("  (nothing answered — check the LED controller is addressed, or the")
        print("   response shape differs: re-run `info` and paste the raw body)")


def cmd_max(host):
    _post(host, dali.recall_max(T.broadcast()), verbose=True)


def cmd_off(host):
    _post(host, dali.off(T.broadcast()), verbose=True)


def cmd_level(host, args):
    lvl = int(args[0])
    target = T.short(int(args[1])) if len(args) > 1 else T.broadcast()
    _post(host, dali.level(target, lvl), verbose=True)


def cmd_query(host, args):
    a = int(args[0])
    parsed, code = _post(host, dali.query_actual_level(T.short(a)), verbose=True)
    print("  parsed:", parsed)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(1)
    host, cmd, rest = sys.argv[1], sys.argv[2], sys.argv[3:]
    {
        "info": lambda: cmd_info(host),
        "raw": lambda: cmd_raw(host, rest),
        "scan": lambda: cmd_scan(host),
        "max": lambda: cmd_max(host),
        "off": lambda: cmd_off(host),
        "level": lambda: cmd_level(host, rest),
        "query": lambda: cmd_query(host, rest),
    }.get(cmd, lambda: print(f"unknown command: {cmd}\n{__doc__}"))()


if __name__ == "__main__":
    main()
