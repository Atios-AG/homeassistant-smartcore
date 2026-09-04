"""Connection hub for a single Atios SmartCore.

Transport is native aiohttp — no external DALI library. The SmartCore exposes:

  * a confirmed HTTP endpoint ``POST /api/dali/iface`` that sends a raw DALI
    frame and (with ``wait_response``) returns the answer as
    ``{"success":true,"bus_busy":false,"collision_detected":false,"data":<byte>}``;
  * an emulated Lunatone DALI-2 IoT websocket at ``ws://<host>/`` that streams
    ``daliMonitor`` frames (bus traffic, incl. DALI-2 input/button events).
    Confirmed on fw 2.7.9: on connect it greets with ``{"type":"info"}``
    reporting name "dali-iot", protocolVersion 3.0.

Lights and status use the HTTP path (verified on device). The websocket is used
only to *receive* monitor frames for buttons; it is best-effort — if it can't be
reached, lights are unaffected and it retries quietly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

import aiohttp

from .const import (
    MONITOR_PING,
    MONITOR_PING_INTERVAL,
    MONITOR_START_REASSERT_S,
    RECONNECT_MAX,
    RECONNECT_MIN,
)
from .dali import Frame

_LOGGER = logging.getLogger(__name__)


@dataclass
class MonitorFrame:
    """A raw frame observed on the bus (decoded further by dali.decode_input_event)."""

    data: list[int]
    bits: int
    line: int | None = None
    framing_error: bool = False


def _parse_monitor_json(raw: str) -> MonitorFrame | None:
    """Parse one Lunatone-protocol ws message into an inbound MonitorFrame.

    Confirmed on fw 2.7.9, the SmartCore pushes each observed bus frame as::

        {"type":"daliMonitor","data":{"line":0,"bits":24,"externalSource":true,
         "data":[0,128,1], ...}, "timeSignature":{...}}

    ``externalSource`` is true for traffic that originated on the bus (input
    devices, gear answers) and false for the SmartCore's own output (our
    commands, and the arc-power frames it sends itself when a coupler is bound
    to a light). Only external frames are input-event candidates.

    Note the device ALSO mirrors every frame as a legacy plain-text line
    (``DALI:[IN],DA24 Evt,008001,NA``) on the same socket. That is the same
    frame, so it is deliberately ignored here — parsing both would deliver
    every button press twice and break gesture synthesis.
    """
    if not raw or not raw.lstrip().startswith("{"):
        return None
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(msg, dict) or msg.get("type") != "daliMonitor":
        return None
    data = msg.get("data")
    if not isinstance(data, dict):
        return None
    payload = data.get("data")
    if not isinstance(payload, list) or not payload:
        return None
    if not data.get("externalSource"):
        return None  # our own outgoing frame, not something on the bus
    try:
        octets = [int(b) & 0xFF for b in payload]
    except (TypeError, ValueError):
        return None
    bits = data.get("bits")
    return MonitorFrame(
        data=octets,
        bits=int(bits) if isinstance(bits, int) else len(octets) * 8,
        line=data.get("line"),
        framing_error=bool(data.get("framingError")),
    )


class AtiosHub:
    """Manage HTTP control and the (optional) monitor websocket for one SmartCore."""

    def __init__(self, host: str, line: int, session: aiohttp.ClientSession) -> None:
        self._base_url = host if host.startswith("http") else f"http://{host}"
        self._host = self._base_url.removeprefix("http://").removeprefix("https://")
        # Lunatone-emulation ws lives at the root path (confirmed fw 2.7.9;
        # tools/monitor.py probes / , /ws , /dali/ws ... and only / connects).
        self._ws_url = f"ws://{self._host}/"
        self._line = line
        self._session = session

        self._task: asyncio.Task | None = None
        self._closing = False
        self._ws_warned = False
        self._monitor_cbs: list[Callable[[MonitorFrame], None]] = []
        self._info: dict = {}
        self.control_devices: list = []  # nvram.ControlDevice, set at setup
        self.input_devices: list = []  # nvram.InputDevice, set at setup

    @property
    def host(self) -> str:
        return self._host

    @property
    def serial(self) -> str | None:
        return self._info.get("serial")

    @property
    def sw_version(self) -> str | None:
        return self._info.get("version_string")

    @property
    def info(self) -> dict:
        return self._info

    @property
    def latest_version(self) -> str | None:
        """Available firmware version, from the /ota_status 'latest' block.

        Populated after async_ota_check(); None until a check has run.
        """
        latest = self._info.get("latest")
        if isinstance(latest, dict):
            return latest.get("version_string")
        return None

    async def async_fetch_info(self) -> dict:
        """Read GET /ota_status (serial, firmware version, update flag)."""
        try:
            async with self._session.get(
                f"{self._base_url}/ota_status", timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                resp.raise_for_status()
                body = await resp.json(content_type=None)
                if isinstance(body, dict):
                    self._info = body
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Atios %s: ota_status fetch failed: %s", self._host, err)
        return self._info

    async def async_fetch_nvm_section(self, section: str) -> list[dict] | None:
        """Read one NVRAM section via GET /api/dali/nvm, following pagination.

        The web configurator pages with limit=4; we mirror that exactly since
        it is the only request shape confirmed against the firmware. Returns
        None when the endpoint is unreachable (older firmware), so callers can
        fall back.
        """
        devices: list[dict] = []
        offset = 0
        for _ in range(32):  # 32 * 4 = 128 > max 64 addresses + groups
            url = (
                f"{self._base_url}/api/dali/nvm"
                f"?section={section}&offset={offset}&limit=4"
            )
            try:
                async with self._session.get(
                    url, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    resp.raise_for_status()
                    body = await resp.json(content_type=None)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Atios %s: nvm fetch %s failed: %s", self._host, section, err
                )
                return None if not devices else devices
            if not isinstance(body, dict) or not body.get("success"):
                return None if not devices else devices
            devices.extend((body.get("data") or {}).get(section) or [])
            pagination = body.get("pagination") or {}
            if not pagination.get("has_more"):
                break
            offset += pagination.get("limit", 4)
        return devices

    async def async_trigger_ota(self) -> bool:
        """Start a firmware update via POST /cmd/ota_update.

        Confirmed by Atios. Note: only works if the device has no web password.
        """
        try:
            async with self._session.post(
                f"{self._base_url}/cmd/ota_update", timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                resp.raise_for_status()
                return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Atios %s: OTA trigger failed: %s", self._host, err)
            return False

    async def async_ota_check(self) -> None:
        """Ask the device to check for updates (POST /cmd/ota_update_check).

        Afterwards /ota_status carries a 'latest' block with the available
        version. Best-effort: firmware without the endpoint just 404s.
        """
        try:
            async with self._session.post(
                f"{self._base_url}/cmd/ota_update_check",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Atios %s: ota_update_check failed: %s", self._host, err)

    async def async_monitor_start(self) -> bool:
        """Enable the bus monitor stream (POST /cmd/dali_monitor_start).

        Confirmed on fw 2.7.9: the monitor stream is a GLOBAL device state, off
        by default. The device only broadcasts daliMonitor frames (incl. DALI-2
        input/button events) to its ws clients while this is enabled — the ws
        "ping" is merely keepalive, not the enable. The web UI's DALI Monitor
        "Start"/"Stop" buttons POST this and /cmd/dali_monitor_stop. Because the
        state is global, anyone pressing "Stop" in the web UI (or navigating
        away from that page) disables it for us too, so we re-assert it
        periodically from the keepalive loop rather than only once. Returns 200
        with an empty body.
        """
        try:
            async with self._session.post(
                f"{self._base_url}/cmd/dali_monitor_start",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
                return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Atios %s: dali_monitor_start failed: %s", self._host, err)
            return False

    # ---- lifecycle --------------------------------------------------------

    async def async_start(self) -> None:
        self._closing = False
        self._task = asyncio.create_task(self._run_monitor(), name=f"atios-{self._host}")

    async def async_stop(self) -> None:
        self._closing = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def add_monitor_listener(self, cb: Callable[[MonitorFrame], None]) -> Callable[[], None]:
        self._monitor_cbs.append(cb)
        return lambda: self._monitor_cbs.remove(cb)

    # ---- monitor websocket (receive-only, best-effort) --------------------

    async def _run_monitor(self) -> None:
        backoff = RECONNECT_MIN
        while not self._closing:
            keepalive: asyncio.Task | None = None
            try:
                async with self._session.ws_connect(
                    self._ws_url, heartbeat=30, timeout=aiohttp.ClientTimeout(total=10)
                ) as ws:
                    self._ws_warned = False
                    backoff = RECONNECT_MIN
                    _LOGGER.info("Atios %s: monitor websocket connected", self._host)
                    # Enabling the bus monitor is what actually makes the device
                    # stream frames (global state, POST /cmd/dali_monitor_start —
                    # see async_monitor_start). The ws "ping" is only keepalive.
                    # Do both on connect, then keep re-asserting from the
                    # keepalive task.
                    await self.async_monitor_start()
                    await ws.send_str(MONITOR_PING)
                    keepalive = asyncio.create_task(self._ws_keepalive(ws))
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._handle_ws_text(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                if self._closing:
                    break
                # Quiet: the ws endpoint may not be confirmed yet on this firmware.
                # Lights work over HTTP regardless; warn once, then debug.
                if not self._ws_warned:
                    _LOGGER.info(
                        "Atios %s: monitor websocket unavailable (%s). Lights work "
                        "over HTTP; button events start once the ws endpoint is "
                        "confirmed. Retrying quietly.",
                        self._host,
                        err,
                    )
                    self._ws_warned = True
                else:
                    _LOGGER.debug("Atios %s: ws retry (%s)", self._host, err)
            finally:
                if keepalive is not None:
                    keepalive.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await keepalive
            if not self._closing:
                await asyncio.sleep(backoff)
                backoff = min(RECONNECT_MAX, backoff * 2)

    async def _ws_keepalive(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Keep the ws alive (text ping) and keep the global monitor enabled.

        The monitor is a global device state (async_monitor_start): if the web
        UI's DALI Monitor is stopped, or another client disables it, our stream
        goes silent. Re-asserting start every MONITOR_START_REASSERT_S recovers
        from that without needing a ws reconnect.
        """
        ticks = 0
        reassert_every = max(1, MONITOR_START_REASSERT_S // MONITOR_PING_INTERVAL)
        try:
            while not self._closing:
                await asyncio.sleep(MONITOR_PING_INTERVAL)
                await ws.send_str(MONITOR_PING)
                ticks += 1
                if ticks % reassert_every == 0:
                    await self.async_monitor_start()
        except (asyncio.CancelledError, ConnectionResetError, aiohttp.ClientError):
            pass

    def _handle_ws_text(self, raw: str) -> None:
        """Parse a ws text message and dispatch monitor frames.

        Messages are Lunatone-protocol JSON (see _parse_monitor_json); the
        legacy ``DALI:...`` text mirror of the same frames is ignored so each
        bus event is delivered exactly once.
        """
        frame = _parse_monitor_json(raw)
        if frame is None:
            return
        for cb in list(self._monitor_cbs):
            try:
                cb(frame)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Atios monitor listener failed")

    # ---- sending / querying (HTTP, confirmed) -----------------------------

    async def send_frame(self, frame: Frame) -> list[int] | None:
        """Send a DALI frame over the confirmed HTTP endpoint.

        Returns the answer byte(s) as list[int] for QUERY frames, else None.
        """
        payload = {
            "repeat_twice": frame.send_twice,
            "wait_response": frame.wait_for_answer,
            "bits": frame.bits,
            "data": list(frame.data),
        }
        url = f"{self._base_url}/api/dali/iface"
        try:
            async with self._session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                resp.raise_for_status()
                if not frame.wait_for_answer:
                    return None
                body = await resp.json(content_type=None)
                # {"success":true,"bus_busy":false,"collision_detected":false,"data":255}
                if not isinstance(body, dict):
                    return None
                if body.get("collision_detected"):
                    _LOGGER.debug("Atios %s: DALI collision on %s", self._host, frame.data)
                answer = body.get("data")
                if answer is None:
                    return None
                if isinstance(answer, int):
                    return [answer]
                if isinstance(answer, list):
                    return answer
                return None
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Atios %s: HTTP send failed: %s", self._host, err)
            return None

    async def async_test_connection(self) -> bool:
        """Config-flow reachability check: gear-present query over HTTP."""
        from .dali import OP_QUERY_CONTROL_GEAR_PRESENT, Target, query

        result = await self.send_frame(query(Target.short(0), OP_QUERY_CONTROL_GEAR_PRESENT))
        if result is not None:
            return True
        # gear may not answer; treat a live HTTP endpoint as reachable
        try:
            async with self._session.get(
                f"{self._base_url}/api/dali/iface",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                return resp.status < 500
        except Exception:  # noqa: BLE001
            return False
