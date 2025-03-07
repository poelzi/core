"""Config flow for Elk-M1 Control integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

import elkm1_lib as elkm1
from elkm1_lib.discovery import ElkSystem
import voluptuous as vol

from homeassistant import config_entries, exceptions
from homeassistant.components import dhcp
from homeassistant.const import (
    CONF_ADDRESS,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PREFIX,
    CONF_PROTOCOL,
    CONF_USERNAME,
)
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import DiscoveryInfoType
from homeassistant.util import slugify

from . import async_wait_for_elk_to_sync
from .const import CONF_AUTO_CONFIGURE, DISCOVER_SCAN_TIMEOUT, DOMAIN, LOGIN_TIMEOUT
from .discovery import (
    _short_mac,
    async_discover_device,
    async_discover_devices,
    async_update_entry_from_discovery,
)

CONF_DEVICE = "device"

SECURE_PORT = 2601

_LOGGER = logging.getLogger(__name__)

PROTOCOL_MAP = {
    "secure": "elks://",
    "TLS 1.2": "elksv1_2://",
    "non-secure": "elk://",
    "serial": "serial://",
}

VALIDATE_TIMEOUT = 35

BASE_SCHEMA = {
    vol.Optional(CONF_USERNAME, default=""): str,
    vol.Optional(CONF_PASSWORD, default=""): str,
}

SECURE_PROTOCOLS = ["secure", "TLS 1.2"]
ALL_PROTOCOLS = [*SECURE_PROTOCOLS, "non-secure", "serial"]
DEFAULT_SECURE_PROTOCOL = "secure"
DEFAULT_NON_SECURE_PROTOCOL = "non-secure"


async def validate_input(data: dict[str, str], mac: str | None) -> dict[str, str]:
    """Validate the user input allows us to connect.

    Data has the keys from DATA_SCHEMA with values provided by the user.
    """
    userid = data.get(CONF_USERNAME)
    password = data.get(CONF_PASSWORD)

    prefix = data[CONF_PREFIX]
    url = _make_url_from_data(data)
    requires_password = url.startswith("elks://") or url.startswith("elksv1_2")

    if requires_password and (not userid or not password):
        raise InvalidAuth

    elk = elkm1.Elk(
        {"url": url, "userid": userid, "password": password, "element_list": ["panel"]}
    )
    elk.connect()

    if not await async_wait_for_elk_to_sync(elk, LOGIN_TIMEOUT, VALIDATE_TIMEOUT, url):
        raise InvalidAuth

    short_mac = _short_mac(mac) if mac else None
    if prefix and prefix != short_mac:
        device_name = prefix
    elif mac:
        device_name = f"ElkM1 {short_mac}"
    else:
        device_name = "ElkM1"
    return {"title": device_name, CONF_HOST: url, CONF_PREFIX: slugify(prefix)}


def _make_url_from_data(data):
    if host := data.get(CONF_HOST):
        return host

    protocol = PROTOCOL_MAP[data[CONF_PROTOCOL]]
    address = data[CONF_ADDRESS]
    return f"{protocol}{address}"


def _placeholders_from_device(device: ElkSystem) -> dict[str, str]:
    return {
        "mac_address": _short_mac(device.mac_address),
        "host": f"{device.ip_address}:{device.port}",
    }


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Elk-M1 Control."""

    VERSION = 1

    def __init__(self):
        """Initialize the elkm1 config flow."""
        self._discovered_device: ElkSystem | None = None
        self._discovered_devices: dict[str, ElkSystem] = {}

    async def async_step_dhcp(self, discovery_info: dhcp.DhcpServiceInfo) -> FlowResult:
        """Handle discovery via dhcp."""
        self._discovered_device = ElkSystem(
            discovery_info.macaddress, discovery_info.ip, 0
        )
        return await self._async_handle_discovery()

    async def async_step_integration_discovery(
        self, discovery_info: DiscoveryInfoType
    ) -> FlowResult:
        """Handle integration discovery."""
        self._discovered_device = ElkSystem(
            discovery_info["mac_address"],
            discovery_info["ip_address"],
            discovery_info["port"],
        )
        return await self._async_handle_discovery()

    async def _async_handle_discovery(self) -> FlowResult:
        """Handle any discovery."""
        device = self._discovered_device
        assert device is not None
        mac = dr.format_mac(device.mac_address)
        host = device.ip_address
        await self.async_set_unique_id(mac)
        for entry in self._async_current_entries(include_ignore=False):
            if (
                entry.unique_id == mac
                or urlparse(entry.data[CONF_HOST]).hostname == host
            ):
                if async_update_entry_from_discovery(self.hass, entry, device):
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(entry.entry_id)
                    )
                return self.async_abort(reason="already_configured")
        self.context[CONF_HOST] = host
        for progress in self._async_in_progress():
            if progress.get("context", {}).get(CONF_HOST) == host:
                return self.async_abort(reason="already_in_progress")
        if not device.port:
            if discovered_device := await async_discover_device(self.hass, host):
                self._discovered_device = discovered_device
            else:
                return self.async_abort(reason="cannot_connect")
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm discovery."""
        assert self._discovered_device is not None
        self.context["title_placeholders"] = _placeholders_from_device(
            self._discovered_device
        )
        return await self.async_step_discovered_connection()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        if user_input is not None:
            if mac := user_input[CONF_DEVICE]:
                await self.async_set_unique_id(mac, raise_on_progress=False)
                self._discovered_device = self._discovered_devices[mac]
                return await self.async_step_discovered_connection()
            return await self.async_step_manual_connection()

        current_unique_ids = self._async_current_ids()
        current_hosts = {
            urlparse(entry.data[CONF_HOST]).hostname
            for entry in self._async_current_entries(include_ignore=False)
        }
        discovered_devices = await async_discover_devices(
            self.hass, DISCOVER_SCAN_TIMEOUT
        )
        self._discovered_devices = {
            dr.format_mac(device.mac_address): device for device in discovered_devices
        }
        devices_name: dict[str | None, str] = {
            mac: f"{_short_mac(device.mac_address)} ({device.ip_address})"
            for mac, device in self._discovered_devices.items()
            if mac not in current_unique_ids and device.ip_address not in current_hosts
        }
        if not devices_name:
            return await self.async_step_manual_connection()
        devices_name[None] = "Manual Entry"
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE): vol.In(devices_name)}),
        )

    async def _async_create_or_error(
        self, user_input: dict[str, Any], importing: bool
    ) -> tuple[dict[str, str] | None, FlowResult | None]:
        """Try to connect and create the entry or error."""
        if self._url_already_configured(_make_url_from_data(user_input)):
            return None, self.async_abort(reason="address_already_configured")

        try:
            info = await validate_input(user_input, self.unique_id)
        except asyncio.TimeoutError:
            return {CONF_HOST: "cannot_connect"}, None
        except InvalidAuth:
            return {CONF_PASSWORD: "invalid_auth"}, None
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            return {"base": "unknown"}, None

        if importing:
            return None, self.async_create_entry(title=info["title"], data=user_input)

        return None, self.async_create_entry(
            title=info["title"],
            data={
                CONF_HOST: info[CONF_HOST],
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_AUTO_CONFIGURE: True,
                CONF_PREFIX: info[CONF_PREFIX],
            },
        )

    async def async_step_discovered_connection(self, user_input=None):
        """Handle connecting the device when we have a discovery."""
        errors = {}
        device = self._discovered_device
        assert device is not None
        if user_input is not None:
            user_input[CONF_ADDRESS] = f"{device.ip_address}:{device.port}"
            if self._async_current_entries():
                user_input[CONF_PREFIX] = _short_mac(device.mac_address)
            else:
                user_input[CONF_PREFIX] = ""
            if device.port != SECURE_PORT:
                user_input[CONF_PROTOCOL] = DEFAULT_NON_SECURE_PROTOCOL
            errors, result = await self._async_create_or_error(user_input, False)
            if not errors:
                return result

        base_schmea = BASE_SCHEMA.copy()
        if device.port == SECURE_PORT:
            base_schmea[
                vol.Required(CONF_PROTOCOL, default=DEFAULT_SECURE_PROTOCOL)
            ] = vol.In(SECURE_PROTOCOLS)

        return self.async_show_form(
            step_id="discovered_connection",
            data_schema=vol.Schema(base_schmea),
            errors=errors,
            description_placeholders=_placeholders_from_device(device),
        )

    async def async_step_manual_connection(self, user_input=None):
        """Handle connecting the device when we need manual entry."""
        errors = {}
        if user_input is not None:
            # We might be able to discover the device via directed UDP
            # in case its on another subnet
            if device := await async_discover_device(
                self.hass, user_input[CONF_ADDRESS]
            ):
                await self.async_set_unique_id(dr.format_mac(device.mac_address))
                self._abort_if_unique_id_configured()
                user_input[CONF_ADDRESS] = f"{device.ip_address}:{device.port}"
            errors, result = await self._async_create_or_error(user_input, False)
            if not errors:
                return result

        return self.async_show_form(
            step_id="manual_connection",
            data_schema=vol.Schema(
                {
                    **BASE_SCHEMA,
                    vol.Required(CONF_ADDRESS): str,
                    vol.Optional(CONF_PREFIX, default=""): str,
                    vol.Required(
                        CONF_PROTOCOL, default=DEFAULT_SECURE_PROTOCOL
                    ): vol.In(ALL_PROTOCOLS),
                }
            ),
            errors=errors,
        )

    async def async_step_import(self, user_input):
        """Handle import."""
        if device := await async_discover_device(
            self.hass, urlparse(user_input[CONF_HOST]).hostname
        ):
            await self.async_set_unique_id(dr.format_mac(device.mac_address))
            self._abort_if_unique_id_configured()
        return (await self._async_create_or_error(user_input, True))[1]

    def _url_already_configured(self, url):
        """See if we already have a elkm1 matching user input configured."""
        existing_hosts = {
            urlparse(entry.data[CONF_HOST]).hostname
            for entry in self._async_current_entries()
        }
        return urlparse(url).hostname in existing_hosts


class InvalidAuth(exceptions.HomeAssistantError):
    """Error to indicate there is invalid auth."""
