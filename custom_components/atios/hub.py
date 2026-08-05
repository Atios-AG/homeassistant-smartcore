"""Connection hub for a single Atios SmartCore.

Transport is native aiohttp — no external DALI library. The SmartCore exposes:

  * a confirmed HTTP endpoint ``POST /api/dali/iface`` that sends a raw DALI
    frame and (with ``wait_response``) returns the answer as
    ``{"success":true,"bus_busy":false,"collision_detected":false,"data":<byte>}``;
  * an emulated Lunatone websocket that streams ``daliMonitor`` frames (bus
    traffic, incl. DALI-2 input/button events).

Lights and status use the HTTP path (verified on device). The websocket is used
only to *receive* monitor frames for buttons; it is best-effort — if it can't be
reached (endpoint/envelope not yet confirmed on this firmware), lights are
unaffected and it retries quietly. The exact ws URL/envelope get finalised with
tools/monitor.py once physical buttons exist.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

import aiohttp

from .const import RECONNECT_MAX, RECONNECT_MIN
from .dali import Frame

_LOGGER = logging.getLogger(__name__)


@dataclass
class MonitorFrame:
    """A raw frame observed on the bus (decoded further by dali.decode_input_event)."""

    data: list[int]
    bits: int
    line: int | None = None
    framing_error: bool = False


class AtiosHub:
    """Manage HTTP control and the (optional) monitor websocket for one SmartCore."""

    def __init__(self, host: str, line: int, session: aiohttp.ClientSession) -> None:
        self._base_url = host if host.startswith("http") else f"http://{host}"
        self._host = self._base_url.removeprefix("http://").removeprefix("https://")
        self._ws_url = f"ws://{self._host}/ws"  # confirmed on device (DevTools)
        self._line = line
        self._session = session

        self._task: asyncio.Task | None = None
        self._closing = False
        self._ws_warned = False
        self._monitor_cbs: list[Callable[[MonitorFrame], None]] = []
        self._info: dict = {}

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
            try:
                async with self._session.ws_connect(
                    self._ws_url, heartbeat=30, timeout=aiohttp.ClientTimeout(total=10)
                ) as ws:
                    self._ws_warned = False
                    backoff = RECONNECT_MIN
                    _LOGGER.info("Atios %s: monitor websocket connected", self._host)
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
            if not self._closing:
                await asyncio.sleep(backoff)
                backoff = min(RECONNECT_MAX, backoff * 2)

    def _handle_ws_text(self, raw: str) -> None:
        """Parse a ws text message and dispatch monitor frames.

        Envelope is the emulated Lunatone form ``{"type": ..., "data": {...}}``.
        We match any message whose type mentions 'monitor' and pull the raw frame
        bytes/bits defensively, since exact field names are confirmed with real
        hardware traces. Non-monitor messages are ignored here.
        """
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(msg, dict):
            return
        mtype = str(msg.get("type", "")).lower()
        if "monitor" not in mtype:
            return
        payload = msg.get("data", msg)
        if not isinstance(payload, dict):
            return
        data = payload.get("data") or payload.get("dali_data") or payload.get("frame")
        bits = payload.get("bits") or payload.get("number_of_bits")
        if not isinstance(data, list) or not isinstance(bits, int):
            return
        frame = MonitorFrame(
            data=[int(b) & 0xFF for b in data],
            bits=int(bits),
            line=payload.get("line"),
            framing_error=bool(payload.get("framing_error", False)),
        )
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
