"""SmartCore web interface as a Home Assistant sidebar panel.

Registers the built-in ``iframe`` panel pointing at the SmartCore web UI. Two
things commonly break an embedded web UI, so we probe for them and warn clearly
instead of showing a silently blank panel:

* mixed content — an ``http://`` UI cannot be framed inside an ``https://`` HA;
* frame-busting — ``X-Frame-Options: DENY``/``SAMEORIGIN`` or a restrictive
  ``Content-Security-Policy: frame-ancestors`` on the SmartCore response makes
  the browser refuse to embed it (this is the Atios-side header to relax).

The probe only logs; it never blocks setup, since headers can change with
firmware and the user may front the UI with a reverse proxy.
"""

from __future__ import annotations

import logging

import aiohttp
from homeassistant.components import frontend
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import (
    CONF_HOST,
    CONF_PANEL,
    CONF_PANEL_ADMIN,
    CONF_PANEL_ICON,
    CONF_PANEL_TITLE,
    CONF_PANEL_URL,
    DEFAULT_PANEL,
    DEFAULT_PANEL_ADMIN,
    DEFAULT_PANEL_ICON,
    DEFAULT_PANEL_TITLE,
)

_LOGGER = logging.getLogger(__name__)


def _url_path(entry: ConfigEntry) -> str:
    """Deterministic, URL-safe sidebar path unique per SmartCore."""
    return f"atios-{entry.entry_id[:8]}"


def _panel_url(entry: ConfigEntry) -> str:
    """Configured panel URL, defaulting to the plain web UI of the host."""
    opts = {**entry.data, **entry.options}
    url = opts.get(CONF_PANEL_URL)
    if url:
        return url
    host = opts[CONF_HOST]
    return host if host.startswith("http") else f"http://{host}/"


async def async_register_panel(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """(Re)register the iframe panel if enabled, warning about embedding issues."""
    opts = {**entry.data, **entry.options}
    if not opts.get(CONF_PANEL, DEFAULT_PANEL):
        async_remove_panel(hass, entry)
        return

    url = _panel_url(entry)
    _warn_mixed_content(hass, url)
    hass.async_create_task(_probe_embeddable(hass, url))

    frontend.async_register_built_in_panel(
        hass,
        "iframe",
        sidebar_title=opts.get(CONF_PANEL_TITLE, DEFAULT_PANEL_TITLE),
        sidebar_icon=opts.get(CONF_PANEL_ICON, DEFAULT_PANEL_ICON),
        frontend_url_path=_url_path(entry),
        config={"url": url},
        require_admin=opts.get(CONF_PANEL_ADMIN, DEFAULT_PANEL_ADMIN),
        update=True,  # overwrite an existing panel with the same path
    )
    _LOGGER.debug("Atios: panel registered at /%s -> %s", _url_path(entry), url)


def async_remove_panel(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the panel if present (safe to call when it was never added)."""
    path = _url_path(entry)
    if path in hass.data.get(frontend.DATA_PANELS, {}):
        frontend.async_remove_panel(hass, path)


def _warn_mixed_content(hass: HomeAssistant, url: str) -> None:
    if url.startswith("https://"):
        return
    try:
        ha_url = get_url(hass, prefer_external=False)
    except NoURLAvailableError:
        return
    if ha_url.startswith("https://"):
        _LOGGER.warning(
            "Atios panel URL %s is http:// but Home Assistant is served over "
            "https:// (%s). Browsers block http iframes inside https pages "
            "(mixed content); the panel will be blank. Put the SmartCore UI "
            "behind a TLS reverse proxy or access HA over http on the LAN.",
            url,
            ha_url,
        )


async def _probe_embeddable(hass: HomeAssistant, url: str) -> None:
    """GET the UI and log a clear reason if headers will block embedding."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    session = async_get_clientsession(hass)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            xfo = resp.headers.get("X-Frame-Options", "").upper()
            csp = resp.headers.get("Content-Security-Policy", "").lower()
    except Exception:  # noqa: BLE001 — probe is best-effort
        return

    if xfo in ("DENY", "SAMEORIGIN"):
        _LOGGER.warning(
            "Atios: SmartCore sends 'X-Frame-Options: %s', so the browser will "
            "refuse to embed its web UI in HA. Ask Atios to drop/relax this "
            "header (it's on their bounty list) or front the UI with a proxy "
            "that strips it.",
            xfo,
        )
    elif "frame-ancestors" in csp and "*" not in csp and "self" not in csp:
        _LOGGER.warning(
            "Atios: SmartCore's Content-Security-Policy restricts frame-ancestors "
            "(%s); the web UI may not embed in HA. Relax it on the device or via "
            "a reverse proxy.",
            csp,
        )
