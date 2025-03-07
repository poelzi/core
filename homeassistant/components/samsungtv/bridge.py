"""samsungctl and samsungtvws bridge classes."""
from __future__ import annotations

from abc import ABC, abstractmethod
import contextlib
from typing import Any

from requests.exceptions import Timeout as RequestsTimeout
from samsungctl import Remote
from samsungctl.exceptions import AccessDenied, ConnectionClosed, UnhandledResponse
from samsungtvws import SamsungTVWS
from samsungtvws.exceptions import ConnectionFailure, HttpApiError
from websocket import WebSocketException

from homeassistant.const import (
    CONF_HOST,
    CONF_ID,
    CONF_METHOD,
    CONF_NAME,
    CONF_PORT,
    CONF_TIMEOUT,
    CONF_TOKEN,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.device_registry import format_mac

from .const import (
    CONF_DESCRIPTION,
    LEGACY_PORT,
    LOGGER,
    METHOD_LEGACY,
    METHOD_WEBSOCKET,
    RESULT_AUTH_MISSING,
    RESULT_CANNOT_CONNECT,
    RESULT_NOT_SUPPORTED,
    RESULT_SUCCESS,
    TIMEOUT_REQUEST,
    TIMEOUT_WEBSOCKET,
    VALUE_CONF_ID,
    VALUE_CONF_NAME,
    WEBSOCKET_PORTS,
)


def mac_from_device_info(info: dict[str, Any]) -> str | None:
    """Extract the mac address from the device info."""
    dev_info = info.get("device", {})
    if dev_info.get("networkType") == "wireless" and dev_info.get("wifiMac"):
        return format_mac(dev_info["wifiMac"])
    return None


async def async_get_device_info(
    hass: HomeAssistant,
    bridge: SamsungTVWSBridge | SamsungTVLegacyBridge | None,
    host: str,
) -> tuple[int | None, str | None, dict[str, Any] | None]:
    """Fetch the port, method, and device info."""
    return await hass.async_add_executor_job(_get_device_info, bridge, host)


def _get_device_info(
    bridge: SamsungTVWSBridge | SamsungTVLegacyBridge, host: str
) -> tuple[int | None, str | None, dict[str, Any] | None]:
    """Fetch the port, method, and device info."""
    if bridge and bridge.port:
        return bridge.port, bridge.method, bridge.device_info()

    for port in WEBSOCKET_PORTS:
        bridge = SamsungTVBridge.get_bridge(METHOD_WEBSOCKET, host, port)
        if info := bridge.device_info():
            return port, METHOD_WEBSOCKET, info

    bridge = SamsungTVBridge.get_bridge(METHOD_LEGACY, host, LEGACY_PORT)
    result = bridge.try_connect()
    if result in (RESULT_SUCCESS, RESULT_AUTH_MISSING):
        return LEGACY_PORT, METHOD_LEGACY, None

    return None, None, None


class SamsungTVBridge(ABC):
    """The Base Bridge abstract class."""

    @staticmethod
    def get_bridge(
        method: str, host: str, port: int | None = None, token: str | None = None
    ) -> SamsungTVLegacyBridge | SamsungTVWSBridge:
        """Get Bridge instance."""
        if method == METHOD_LEGACY or port == LEGACY_PORT:
            return SamsungTVLegacyBridge(method, host, port)
        return SamsungTVWSBridge(method, host, port, token)

    def __init__(self, method: str, host: str, port: int | None = None) -> None:
        """Initialize Bridge."""
        self.port = port
        self.method = method
        self.host = host
        self.token: str | None = None
        self._remote: Remote | None = None
        self._reauth_callback: CALLBACK_TYPE | None = None
        self._new_token_callback: CALLBACK_TYPE | None = None

    def register_reauth_callback(self, func: CALLBACK_TYPE) -> None:
        """Register a callback function."""
        self._reauth_callback = func

    def register_new_token_callback(self, func: CALLBACK_TYPE) -> None:
        """Register a callback function."""
        self._new_token_callback = func

    @abstractmethod
    def try_connect(self) -> str | None:
        """Try to connect to the TV."""

    @abstractmethod
    def device_info(self) -> dict[str, Any] | None:
        """Try to gather infos of this TV."""

    @abstractmethod
    def mac_from_device(self) -> str | None:
        """Try to fetch the mac address of the TV."""

    def is_on(self) -> bool:
        """Tells if the TV is on."""
        if self._remote is not None:
            self.close_remote()

        try:
            return self._get_remote() is not None
        except (
            UnhandledResponse,
            AccessDenied,
            ConnectionFailure,
        ):
            # We got a response so it's working.
            return True
        except OSError:
            # Different reasons, e.g. hostname not resolveable
            return False

    def send_key(self, key: str) -> None:
        """Send a key to the tv and handles exceptions."""
        try:
            # recreate connection if connection was dead
            retry_count = 1
            for _ in range(retry_count + 1):
                try:
                    self._send_key(key)
                    break
                except (
                    ConnectionClosed,
                    BrokenPipeError,
                    WebSocketException,
                ):
                    # BrokenPipe can occur when the commands is sent to fast
                    # WebSocketException can occur when timed out
                    self._remote = None
        except (UnhandledResponse, AccessDenied):
            # We got a response so it's on.
            LOGGER.debug("Failed sending command %s", key, exc_info=True)
        except OSError:
            # Different reasons, e.g. hostname not resolveable
            pass

    @abstractmethod
    def _send_key(self, key: str) -> None:
        """Send the key."""

    @abstractmethod
    def _get_remote(self, avoid_open: bool = False) -> Remote:
        """Get Remote object."""

    def close_remote(self) -> None:
        """Close remote object."""
        try:
            if self._remote is not None:
                # Close the current remote connection
                self._remote.close()
            self._remote = None
        except OSError:
            LOGGER.debug("Could not establish connection")

    def _notify_reauth_callback(self) -> None:
        """Notify access denied callback."""
        if self._reauth_callback is not None:
            self._reauth_callback()

    def _notify_new_token_callback(self) -> None:
        """Notify new token callback."""
        if self._new_token_callback is not None:
            self._new_token_callback()


class SamsungTVLegacyBridge(SamsungTVBridge):
    """The Bridge for Legacy TVs."""

    def __init__(self, method: str, host: str, port: int | None) -> None:
        """Initialize Bridge."""
        super().__init__(method, host, LEGACY_PORT)
        self.config = {
            CONF_NAME: VALUE_CONF_NAME,
            CONF_DESCRIPTION: VALUE_CONF_NAME,
            CONF_ID: VALUE_CONF_ID,
            CONF_HOST: host,
            CONF_METHOD: method,
            CONF_PORT: None,
            CONF_TIMEOUT: 1,
        }

    def mac_from_device(self) -> None:
        """Try to fetch the mac address of the TV."""
        return None

    def try_connect(self) -> str:
        """Try to connect to the Legacy TV."""
        config = {
            CONF_NAME: VALUE_CONF_NAME,
            CONF_DESCRIPTION: VALUE_CONF_NAME,
            CONF_ID: VALUE_CONF_ID,
            CONF_HOST: self.host,
            CONF_METHOD: self.method,
            CONF_PORT: None,
            # We need this high timeout because waiting for auth popup is just an open socket
            CONF_TIMEOUT: TIMEOUT_REQUEST,
        }
        try:
            LOGGER.debug("Try config: %s", config)
            with Remote(config.copy()):
                LOGGER.debug("Working config: %s", config)
                return RESULT_SUCCESS
        except AccessDenied:
            LOGGER.debug("Working but denied config: %s", config)
            return RESULT_AUTH_MISSING
        except UnhandledResponse as err:
            LOGGER.debug("Working but unsupported config: %s, error: %s", config, err)
            return RESULT_NOT_SUPPORTED
        except (ConnectionClosed, OSError) as err:
            LOGGER.debug("Failing config: %s, error: %s", config, err)
            return RESULT_CANNOT_CONNECT

    def device_info(self) -> None:
        """Try to gather infos of this device."""
        return None

    def _get_remote(self, avoid_open: bool = False) -> Remote:
        """Create or return a remote control instance."""
        if self._remote is None:
            # We need to create a new instance to reconnect.
            try:
                LOGGER.debug(
                    "Create SamsungTVLegacyBridge for %s (%s)", CONF_NAME, self.host
                )
                self._remote = Remote(self.config.copy())
            # This is only happening when the auth was switched to DENY
            # A removed auth will lead to socket timeout because waiting for auth popup is just an open socket
            except AccessDenied:
                self._notify_reauth_callback()
                raise
            except (ConnectionClosed, OSError):
                pass
        return self._remote

    def _send_key(self, key: str) -> None:
        """Send the key using legacy protocol."""
        if remote := self._get_remote():
            remote.control(key)

    def stop(self) -> None:
        """Stop Bridge."""
        LOGGER.debug("Stopping SamsungTVLegacyBridge")
        self.close_remote()


class SamsungTVWSBridge(SamsungTVBridge):
    """The Bridge for WebSocket TVs."""

    def __init__(
        self, method: str, host: str, port: int | None = None, token: str | None = None
    ) -> None:
        """Initialize Bridge."""
        super().__init__(method, host, port)
        self.token = token

    def mac_from_device(self) -> str | None:
        """Try to fetch the mac address of the TV."""
        info = self.device_info()
        return mac_from_device_info(info) if info else None

    def try_connect(self) -> str:
        """Try to connect to the Websocket TV."""
        for self.port in WEBSOCKET_PORTS:
            config = {
                CONF_NAME: VALUE_CONF_NAME,
                CONF_HOST: self.host,
                CONF_METHOD: self.method,
                CONF_PORT: self.port,
                # We need this high timeout because waiting for auth popup is just an open socket
                CONF_TIMEOUT: TIMEOUT_REQUEST,
            }

            result = None
            try:
                LOGGER.debug("Try config: %s", config)
                with SamsungTVWS(
                    host=self.host,
                    port=self.port,
                    token=self.token,
                    timeout=config[CONF_TIMEOUT],
                    name=config[CONF_NAME],
                ) as remote:
                    remote.open()
                    self.token = remote.token
                    if self.token is None:
                        config[CONF_TOKEN] = "*****"
                    LOGGER.debug("Working config: %s", config)
                    return RESULT_SUCCESS
            except WebSocketException as err:
                LOGGER.debug(
                    "Working but unsupported config: %s, error: %s", config, err
                )
                result = RESULT_NOT_SUPPORTED
            except (OSError, ConnectionFailure) as err:
                LOGGER.debug("Failing config: %s, error: %s", config, err)
        # pylint: disable=useless-else-on-loop
        else:
            if result:
                return result

        return RESULT_CANNOT_CONNECT

    def device_info(self) -> dict[str, Any] | None:
        """Try to gather infos of this TV."""
        if remote := self._get_remote(avoid_open=True):
            with contextlib.suppress(HttpApiError, RequestsTimeout):
                device_info: dict[str, Any] = remote.rest_device_info()
                return device_info

        return None

    def _send_key(self, key: str) -> None:
        """Send the key using websocket protocol."""
        if key == "KEY_POWEROFF":
            key = "KEY_POWER"
        if remote := self._get_remote():
            remote.send_key(key)

    def _get_remote(self, avoid_open: bool = False) -> Remote:
        """Create or return a remote control instance."""
        if self._remote is None:
            # We need to create a new instance to reconnect.
            try:
                LOGGER.debug(
                    "Create SamsungTVWSBridge for %s (%s)", CONF_NAME, self.host
                )
                self._remote = SamsungTVWS(
                    host=self.host,
                    port=self.port,
                    token=self.token,
                    timeout=TIMEOUT_WEBSOCKET,
                    name=VALUE_CONF_NAME,
                )
                if not avoid_open:
                    self._remote.open()
            # This is only happening when the auth was switched to DENY
            # A removed auth will lead to socket timeout because waiting for auth popup is just an open socket
            except ConnectionFailure:
                self._notify_reauth_callback()
            except (WebSocketException, OSError):
                self._remote = None
            else:
                if self.token != self._remote.token:
                    LOGGER.debug(
                        "SamsungTVWSBridge has provided a new token %s",
                        self._remote.token,
                    )
                    self.token = self._remote.token
                    self._notify_new_token_callback()
        return self._remote

    def stop(self) -> None:
        """Stop Bridge."""
        LOGGER.debug("Stopping SamsungTVWSBridge")
        self.close_remote()
