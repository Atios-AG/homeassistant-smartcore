"""Config flow for Atios SmartCore.

Manual host entry for now. Zeroconf discovery is stubbed and disabled until the
SmartCore's advertised mDNS service type is captured from real hardware
(``avahi-browse -rt _http._tcp`` etc.) — then the discovery step below can be
enabled by adding a ``zeroconf`` block to manifest.json.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    CONF_LINE,
    CONF_PANEL,
    CONF_PANEL_ADMIN,
    CONF_PANEL_ICON,
    CONF_PANEL_TITLE,
    CONF_PANEL_URL,
    DEFAULT_LINE,
    DEFAULT_PANEL,
    DEFAULT_PANEL_ADMIN,
    DEFAULT_PANEL_ICON,
    DEFAULT_PANEL_TITLE,
    DOMAIN,
)
from .hub import AtiosHub


class AtiosConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Atios SmartCore."""

    VERSION = 1
    _discovered_host: str = ""
    _discovered_name: str = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()

            session = async_get_clientsession(self.hass)
            hub = AtiosHub(host, user_input[CONF_LINE], session)
            if await hub.async_test_connection():
                return self.async_create_entry(
                    title=f"SmartCore ({host})", data=user_input
                )
            errors["base"] = "cannot_connect"

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Optional(CONF_LINE, default=DEFAULT_LINE): vol.All(
                    int, vol.Range(min=0, max=3)
                ),
            }
        )
    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a SmartCore found via mDNS.

        Defensive about TXT keys: the real Lunatone gateway advertises
        manufacturer/type/uid/device, but the SmartCore emulation may expose a
        different set. We take the host as ground truth and use a stable device
        id if one is advertised, else fall back to the host.
        """
        host = discovery_info.host
        props = {
            str(k).lower(): (v.decode() if isinstance(v, bytes) else v)
            for k, v in (discovery_info.properties or {}).items()
        }
        uid = props.get("uid") or props.get("serial") or props.get("serialnumber")
        unique = uid.replace("-", "") if uid else host
        await self.async_set_unique_id(unique)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})

        self._discovered_host = host
        name = (discovery_info.name or host).rsplit(".", 1)[0]
        self._discovered_name = name
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding a discovered SmartCore."""
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            hub = AtiosHub(self._discovered_host, DEFAULT_LINE, session)
            if await hub.async_test_connection():
                return self.async_create_entry(
                    title=f"SmartCore ({self._discovered_host})",
                    data={CONF_HOST: self._discovered_host, CONF_LINE: DEFAULT_LINE},
                )
            return self.async_abort(reason="cannot_connect")
        return self.async_show_form(
            step_id="zeroconf_confirm",
            description_placeholders={
                "name": self._discovered_name,
                "host": self._discovered_host,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> "AtiosOptionsFlow":
        return AtiosOptionsFlow()


class AtiosOptionsFlow(OptionsFlow):
    """Configure the SmartCore web-UI sidebar panel."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # blank URL -> fall back to the default (http://host/)
            if not user_input.get(CONF_PANEL_URL):
                user_input.pop(CONF_PANEL_URL, None)
            return self.async_create_entry(data=user_input)

        opts = {**self.config_entry.data, **self.config_entry.options}
        default_url = (
            opts.get(CONF_PANEL_URL)
            or f"http://{opts.get(CONF_HOST, '')}/"
        )
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_PANEL, default=opts.get(CONF_PANEL, DEFAULT_PANEL)
                ): bool,
                vol.Optional(CONF_PANEL_URL, default=default_url): str,
                vol.Optional(
                    CONF_PANEL_TITLE,
                    default=opts.get(CONF_PANEL_TITLE, DEFAULT_PANEL_TITLE),
                ): str,
                vol.Optional(
                    CONF_PANEL_ICON,
                    default=opts.get(CONF_PANEL_ICON, DEFAULT_PANEL_ICON),
                ): str,
                vol.Optional(
                    CONF_PANEL_ADMIN,
                    default=opts.get(CONF_PANEL_ADMIN, DEFAULT_PANEL_ADMIN),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
