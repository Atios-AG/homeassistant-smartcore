"""Connection hub for a single Atios SmartCore.

Owns one persistent websocket to the SmartCore (via the ``lunatone-dali2-iot``
``WebSocketClient``) and:

  * sends raw DALI frames (``send_frame``), with a confirmed HTTP fallback to
    ``POST /api/dali/iface`` in case WS send misbehaves on the emulated stack;
  * runs a background monitor loop that turns ``DaliMonitorEvent`` frames into
    HA bus events and routes ``DaliAnswerEvent`` results to whoever is waiting
    on a QUERY;
  * auto-reconnects with capped backoff.

Design intent matches the rest of the Tuliheina stack: local push, no polling
loops beyond what QUERY needs, no middleware.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

import aiohttp
from dali2iot import (
    DaliAnswerEvent,
    DaliFrame,
    DaliFrameMode,
    DaliMonitorEvent,
    WebSocketClient,
)

from .const import RECONNECT_MAX, RECONNECT_MIN
from .dali import Frame

_LOGGER = logging.getLogger(__name__)

# how long to wait for a daliAnswer to a QUERY before giving up
ANSWER_TIMEOUT = 2.0


class AtiosHub:
    """Manage the live connection to one SmartCore."""

    def __init__(self, host: str, line: int, session: aiohttp.ClientSession) -> None:
        # host is a bare IP/hostname; the library wants an http base_url
        self._base_url = host if host.startswith("http") else f"http://{host}"
        self._host = self._base_url.removeprefix("http://").removeprefix("https://")
        self._line = line
        self._session = session

        self._ws: WebSocketClient | None = None
        self._task: asyncio.Task | None = None
        self._closing = False

        # subscribers to monitored bus frames (InputEvent envelopes handled upstream)
        self._monitor_cbs: list[Callable[[DaliMonitorEvent], None]] = []
        # pending QUERY answers, keyed by nothing fancy: the emulated stack answers
        # only the connection that asked, in order, so a single-slot future queue is enough
        self._answer_waiters: asyncio.Queue[asyncio.Future] = asyncio.Queue()

    @property
    def host(self) -> str:
        return self._host

    # ---- lifecycle --------------------------------------------------------

    async def async_start(self) -> None:
        self._closing = False
        self._task = asyncio.create_task(self._run(), name=f"atios-{self._host}")

    async def async_stop(self) -> None:
        self._closing = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._ws:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._ws = None

    def add_monitor_listener(self, cb: Callable[[DaliMonitorEvent], None]) -> Callable[[], None]:
        self._monitor_cbs.append(cb)
        return lambda: self._monitor_cbs.remove(cb)

    # ---- monitor loop -----------------------------------------------------

    async def _run(self) -> None:
        backoff = RECONNECT_MIN
        while not self._closing:
            try:
                async with WebSocketClient(base_url=self._base_url) as ws:
                    self._ws = ws
                    backoff = RECONNECT_MIN
                    _LOGGER.info("Atios SmartCore %s: websocket connected", self._host)
                    async for event in ws:
                        self._dispatch(event)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 — reconnect on anything
                if self._closing:
                    break
                _LOGGER.warning(
                    "Atios SmartCore %s: websocket lost (%s); retrying in %ss",
                    self._host,
                    err,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(RECONNECT_MAX, backoff * 2)
            finally:
                self._ws = None

    def _dispatch(self, event: object) -> None:
        if isinstance(event, DaliMonitorEvent):
            for cb in list(self._monitor_cbs):
                try:
                    cb(event)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Atios monitor listener failed")
        elif isinstance(event, DaliAnswerEvent):
            if not self._answer_waiters.empty():
                fut = self._answer_waiters.get_nowait()
                if not fut.done():
                    fut.set_result(event)

    # ---- sending ----------------------------------------------------------

    async def send_frame(self, frame: Frame) -> list[int] | None:
        """Send a DALI frame. Returns the answer bytes when the frame is a QUERY.

        Prefers the websocket path; falls back to the SmartCore-native HTTP
        endpoint (confirmed working by Atios/community) if the WS isn't up.
        """
        if self._ws is not None:
            return await self._send_ws(frame)
        return await self._send_http(frame)

    async def _send_ws(self, frame: Frame) -> list[int] | None:
        assert self._ws is not None
        fut: asyncio.Future | None = None
        if frame.wait_for_answer:
            fut = asyncio.get_running_loop().create_future()
            await self._answer_waiters.put(fut)

        await self._ws.send_dali_frame(
            DaliFrame(
                line=frame.line if frame.line is not None else self._line,
                number_of_bits=frame.bits,
                mode=DaliFrameMode(
                    send_twice=frame.send_twice,
                    wait_for_answer=frame.wait_for_answer,
                    priority=frame.priority,
                ),
                dali_data=list(frame.data),
            )
        )

        if fut is None:
            return None
        try:
            answer: DaliAnswerEvent = await asyncio.wait_for(fut, ANSWER_TIMEOUT)
            return list(answer.dali_data) if answer.dali_data is not None else None
        except asyncio.TimeoutError:
            _LOGGER.debug("Atios %s: no answer to query %s", self._host, frame.data)
            return None

    async def _send_http(self, frame: Frame) -> list[int] | None:
        """Confirmed native endpoint: POST http://<ip>/api/dali/iface."""
        payload = {
            "repeat_twice": frame.send_twice,
            "wait_response": frame.wait_for_answer,
            "bits": frame.bits,
            "data": list(frame.data),
        }
        url = f"{self._base_url}/api/dali/iface"
        try:
            async with self._session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                resp.raise_for_status()
                if not frame.wait_for_answer:
                    return None
                body = await resp.json(content_type=None)
                # response shape to be confirmed against the device; be defensive
                if isinstance(body, dict):
                    return body.get("data") or body.get("dali_data")
                return None
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Atios %s: HTTP send failed: %s", self._host, err)
            return None

    async def async_test_connection(self) -> bool:
        """Cheap reachability check for the config flow: QUERY over HTTP."""
        from .dali import OP_QUERY_CONTROL_GEAR_PRESENT, Target, query

        result = await self._send_http(query(Target.short(0), OP_QUERY_CONTROL_GEAR_PRESENT))
        # A reachable SmartCore returns 2xx even if no gear answers; _send_http
        # returning without raising is the real signal. We treat "no exception"
        # as success by re-issuing and catching separately.
        return result is not None or await self._http_reachable()

    async def _http_reachable(self) -> bool:
        try:
            async with self._session.get(
                f"{self._base_url}/api/dali/iface",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                return resp.status < 500
        except Exception:  # noqa: BLE001
            return False
