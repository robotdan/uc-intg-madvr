"""
Setup flow handler for madVR Envy integration.

:copyright: (c) 2025 by Meir Miyara
:license: MPL-2.0, see LICENSE for more details.
"""

import logging
import asyncio
from typing import Callable, Awaitable

from ucapi import IntegrationSetupError, SetupAction, SetupComplete, SetupDriver
from ucapi.api_definitions import (
    AbortDriverSetup,
    DriverSetupRequest,
    RequestUserInput,
    UserDataResponse,
    SetupError,
)

from uc_intg_madvr.device import MadVRDevice
from uc_intg_madvr.config import MadVRConfig
from uc_intg_madvr import const

_LOG = logging.getLogger(__name__)


class MadVRSetup:
    """Setup flow manager for madVR integration."""

    def __init__(self, api, config: MadVRConfig, on_setup_complete: Callable[[], Awaitable[None]]):
        self._api = api
        self._config = config
        self._on_setup_complete = on_setup_complete
        _LOG.info("MadVRSetup initialized")

    async def handle_setup(self, msg: SetupDriver) -> SetupAction:
        """Handle setup flow messages."""
        _LOG.info("=" * 70)
        _LOG.info("SETUP: Received message type: %s", type(msg).__name__)
        _LOG.info("=" * 70)
        
        if isinstance(msg, DriverSetupRequest):
            _LOG.info("SETUP: Handling DriverSetupRequest")
            return RequestUserInput(
                title={"en": "madVR Envy Connection"},
                settings=[
                    {
                        "id": "host",
                        "label": {"en": "IP Address"},
                        "field": {"text": {"value": self._config.host if self._config.host else ""}}
                    },
                    {
                        "id": "port",
                        "label": {"en": "Port"},
                        "field": {"number": {"value": self._config.port if self._config.port else const.DEFAULT_PORT}}
                    },
                    {
                        "id": "name",
                        "label": {"en": "Device Name"},
                        "field": {"text": {"value": self._config.name if self._config.name else "madVR Envy"}}
                    },
                    {
                        "id": "polling_mode",
                        "label": {"en": "Polling Mode"},
                        "field": {
                            "dropdown": {
                                "value": self._config.polling_mode,
                                "items": [
                                    {"id": "enabled", "label": {"en": "Enabled (polls at interval)"}},
                                    {"id": "on_demand", "label": {"en": "On-demand (only when viewing)"}},
                                    {"id": "disabled", "label": {"en": "Disabled (saves battery)"}},
                                ]
                            }
                        }
                    },
                    {
                        "id": "polling_info",
                        "label": {"en": ""},
                        "field": {
                            "label": {
                                "value": {
                                    "en": "Polling is used for data not available via push notifications (currently temperature sensors only). Disabling polling improves battery life. On-demand fetches data only when actively viewing the sensor."
                                }
                            }
                        }
                    },
                    {
                        "id": "polling_interval",
                        "label": {"en": "Polling Interval (seconds)"},
                        "field": {"number": {"value": self._config.polling_interval, "min": const.MIN_POLL_INTERVAL}}
                    }
                ]
            )
        
        elif isinstance(msg, UserDataResponse):
            _LOG.info("SETUP: Handling UserDataResponse")
            _LOG.info("SETUP: Input values: %s", msg.input_values)
            action = await self._handle_user_input(msg.input_values)
            
            if isinstance(action, SetupComplete) and self._on_setup_complete:
                await self._on_setup_complete()
                
            return action
        
        elif isinstance(msg, AbortDriverSetup):
            _LOG.info("SETUP: Setup aborted by user")
            return SetupError(IntegrationSetupError.OTHER)
        
        else:
            _LOG.error("SETUP: Unknown message type: %s", type(msg).__name__)
            return SetupError(IntegrationSetupError.OTHER)

    async def _handle_user_input(self, input_values: dict[str, str]) -> SetupAction:
        """Process user input from setup form."""
        _LOG.info("SETUP: Processing user input")
        
        host = input_values.get("host", "").strip()
        port_str = input_values.get("port", str(const.DEFAULT_PORT))
        name = input_values.get("name", "madVR Envy").strip()
        
        _LOG.info("SETUP: Host=%s, Port=%s, Name=%s", host, port_str, name)
        
        if not host:
            _LOG.error("SETUP: No host provided")
            return SetupError(IntegrationSetupError.NOT_FOUND)
        
        try:
            port = int(port_str)
            if port < 1 or port > 65535:
                raise ValueError("Invalid port range")
        except (ValueError, TypeError) as e:
            _LOG.error("SETUP: Invalid port '%s': %s", port_str, e)
            return SetupError(IntegrationSetupError.OTHER)
        
        _LOG.info("SETUP: Testing connection to %s:%d", host, port)
        
        test_config = MadVRConfig()
        test_config.set_config(host, port, name)
        
        loop = asyncio.get_running_loop()
        test_device = MadVRDevice(test_config, loop)
        
        try:
            result = await test_device.send_command(const.CMD_HEARTBEAT)
            
            if not result["success"]:
                _LOG.error("SETUP: Failed to connect to madVR device")
                return SetupError(IntegrationSetupError.CONNECTION_REFUSED)
            
            _LOG.info("SETUP: Successfully connected to madVR device")
            
            _LOG.info("SETUP: Fetching MAC address for Wake-on-LAN...")
            await test_device._fetch_mac_address()
            
            if test_config.mac_address:
                _LOG.info("SETUP: MAC address retrieved: %s", test_config.mac_address)
            else:
                _LOG.warning("SETUP: Could not fetch MAC address, WOL may not work")
            
            await test_device.stop()

            self._config.set_config(host, port, name)
            if test_config.mac_address:
                self._config.set_mac_address(test_config.mac_address)

            # Save polling configuration
            polling_mode = input_values.get("polling_mode", "enabled")
            try:
                polling_interval = int(input_values.get("polling_interval", str(const.DEFAULT_POLL_INTERVAL)))
            except (ValueError, TypeError):
                polling_interval = const.DEFAULT_POLL_INTERVAL
            self._config.set_polling_config(polling_mode, polling_interval)

            _LOG.info("SETUP: Configuration saved successfully")
            _LOG.info("=" * 70)
            return SetupComplete()
            
        except Exception as e:
            _LOG.error("SETUP: Connection test failed: %s", e, exc_info=True)
            try:
                await test_device.stop()
            except Exception:
                pass
            return SetupError(IntegrationSetupError.CONNECTION_REFUSED)