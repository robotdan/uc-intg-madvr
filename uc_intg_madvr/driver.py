"""
madVR Envy integration driver.

:copyright: (c) 2025 by Meir Miyara
:license: MPL-2.0, see LICENSE for more details.
"""

import asyncio
import logging
from typing import Any

import ucapi
from ucapi import DeviceStates, Events, EntityTypes

from uc_intg_madvr.config import MadVRConfig
from uc_intg_madvr.device import MadVRDevice, EVENTS as DeviceEvents, PowerState
from uc_intg_madvr.media_player import MadVRMediaPlayer
from uc_intg_madvr.remote import MadVRRemote
from uc_intg_madvr.sensor import (
    MadVRSignalSensor,
    MadVRTemperatureSensor,
    MadVRAspectRatioSensor,
    MadVRMaskingRatioSensor,
)
from uc_intg_madvr.select import MadVRAspectRatioSelect
from uc_intg_madvr.setup import MadVRSetup

_LOG = logging.getLogger(__name__)

api: ucapi.IntegrationAPI | None = None
_config: MadVRConfig | None = None
_device: MadVRDevice | None = None
_media_player: MadVRMediaPlayer | None = None
_remote: MadVRRemote | None = None
_sensors: list = []
_select: MadVRAspectRatioSelect | None = None


def _device_state_to_media_player_state(dev_state: PowerState) -> ucapi.media_player.States:
    """
    Convert device power state to media player state.

    Mapping:
    - PowerState.ON → States.ON (actively processing video)
    - PowerState.STANDBY → States.STANDBY (low power, network responsive)
    - PowerState.OFF → States.OFF (powered off)
    - PowerState.UNKNOWN → States.UNKNOWN
    """
    state_map = {
        PowerState.ON: ucapi.media_player.States.ON,
        PowerState.STANDBY: ucapi.media_player.States.STANDBY,
        PowerState.OFF: ucapi.media_player.States.OFF,
        PowerState.UNKNOWN: ucapi.media_player.States.UNKNOWN,
    }
    return state_map.get(dev_state, ucapi.media_player.States.UNKNOWN)


def _device_state_to_remote_state(dev_state: PowerState) -> ucapi.remote.States:
    """
    Convert device power state to remote state.

    Mapping:
    - PowerState.ON or STANDBY → States.ON (device is responsive)
    - PowerState.OFF → States.OFF (device is not responsive)
    - PowerState.UNKNOWN → States.UNKNOWN
    """
    if dev_state in (PowerState.ON, PowerState.STANDBY):
        return ucapi.remote.States.ON
    elif dev_state == PowerState.OFF:
        return ucapi.remote.States.OFF
    else:
        return ucapi.remote.States.UNKNOWN


async def on_device_update(identifier: str, update: dict[str, Any] | None) -> None:
    """Handle device state updates."""
    if not update:
        return

    _LOG.debug(f"Device update for {identifier}: {update}")

    # Handle media player updates
    if _media_player and identifier == _media_player.id.split('.')[1]:
        if api.configured_entities.contains(_media_player.id):
            mp_attributes = {}

            if "state" in update:
                mp_state = _device_state_to_media_player_state(update["state"])
                mp_attributes[ucapi.media_player.Attributes.STATE] = mp_state
                _LOG.info(f"Media Player state update: {update['state']} → {mp_state}")

            if "signal_info" in update:
                mp_attributes[ucapi.media_player.Attributes.MEDIA_TITLE] = update["signal_info"]

            if mp_attributes:
                api.configured_entities.update_attributes(_media_player.id, mp_attributes)

    # Handle remote updates
    if _remote and identifier == _remote.id.split('.')[1]:
        if api.configured_entities.contains(_remote.id):
            if "state" in update:
                remote_state = _device_state_to_remote_state(update["state"])
                remote_attributes = {
                    ucapi.remote.Attributes.STATE: remote_state
                }
                api.configured_entities.update_attributes(_remote.id, remote_attributes)

    # Handle sensor updates
    for sensor in _sensors:
        if identifier == sensor.id:
            if api.configured_entities.contains(sensor.id):
                api.configured_entities.update_attributes(sensor.id, update)

    # Handle select entity updates
    if _select and identifier == _select.id:
        if api.configured_entities.contains(_select.id):
            api.configured_entities.update_attributes(_select.id, update)


async def _initialize_entities():
    """Initialize device and entities."""
    global _device, _media_player, _remote, _sensors, _select

    if not _config or not _config.is_configured():
        _LOG.info("Integration not configured")
        return False

    try:
        _LOG.info("Initializing madVR device and entities...")

        loop = asyncio.get_running_loop()
        _device = MadVRDevice(_config, loop)

        _device.events.on(DeviceEvents.UPDATE, on_device_update)

        _media_player = MadVRMediaPlayer(_config, _device)
        _remote = MadVRRemote(_config, _device)

        # Create sensor entities — field order per madVR protocol: GPU, HDMI, CPU, Mainboard
        _sensors = [
            MadVRSignalSensor(_config, _device),
            MadVRTemperatureSensor(_config, _device, 0, "GPU"),
            MadVRTemperatureSensor(_config, _device, 1, "HDMI"),
            MadVRTemperatureSensor(_config, _device, 2, "CPU"),
            MadVRTemperatureSensor(_config, _device, 3, "Mainboard"),
            MadVRAspectRatioSensor(_config, _device),
            MadVRMaskingRatioSensor(_config, _device),
        ]

        # Create select entity
        _select = MadVRAspectRatioSelect(_config, _device)

        _LOG.info(f"Media Player features: {_media_player.features}")
        _LOG.info(f"Remote features: {_remote.features}")
        _LOG.info(f"Created {len(_sensors)} sensor entities")
        _LOG.info(f"Created select entity for aspect ratio mode")

        api.available_entities.clear()
        api.available_entities.add(_media_player)
        api.available_entities.add(_remote)

        for sensor in _sensors:
            api.available_entities.add(sensor)

        api.available_entities.add(_select)

        await _device.start()

        _LOG.info("Entities initialized successfully")
        return True

    except Exception as e:
        _LOG.error(f"Failed to initialize entities: {e}", exc_info=True)
        return False


async def on_setup_complete():
    """Called when setup is complete."""
    _LOG.info("Setup complete - initializing entities")

    if await _initialize_entities():
        await api.set_device_state(DeviceStates.CONNECTED)
        _LOG.info("Device state set to CONNECTED")
    else:
        await api.set_device_state(DeviceStates.ERROR)
        _LOG.error("Entity initialization failed")


async def on_connect() -> None:
    """Handle Remote connection. Triggers auto-recovery on reconnect."""
    global _config

    _LOG.info("Remote connected")

    if not _config:
        _config = MadVRConfig()

    _config.reload_from_disk()

    if _config.is_configured() and not _device:
        _LOG.info("Configuration found, reinitializing...")
        if await _initialize_entities():
            await api.set_device_state(DeviceStates.CONNECTED)
        else:
            await api.set_device_state(DeviceStates.ERROR)
    elif not _config.is_configured():
        await api.set_device_state(DeviceStates.DISCONNECTED)
    else:
        # UC remote reconnected — trigger auto-recovery (reset backoff)
        if _device:
            await _device.trigger_reconnect()
        await api.set_device_state(DeviceStates.CONNECTED)


async def on_disconnect() -> None:
    """Handle Remote disconnection."""
    _LOG.info("Remote disconnected")


async def on_subscribe_entities(entity_ids: list[str]):
    """Handle entity subscriptions."""
    _LOG.info(f"Entities subscription requested: {entity_ids}")

    for entity_id in entity_ids:
        if _media_player and entity_id == _media_player.id:
            # Media player gets state from push notifications automatically
            pass
        elif _remote and entity_id == _remote.id:
            if _device and api.configured_entities.contains(_remote.id):
                api.configured_entities.update_attributes(
                    _remote.id,
                    {ucapi.remote.Attributes.STATE: _device_state_to_remote_state(_device.state)}
                )
        else:
            # Check if subscribing to a temperature sensor in on_demand mode
            if _device and _config and _config.polling_mode == "on_demand":
                if "temp_" in entity_id:
                    await _device.query_on_demand()


async def main():
    """Main entry point."""
    global api, _config

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    )

    _LOG.info("Starting madVR Envy integration")

    try:
        loop = asyncio.get_running_loop()
        api = ucapi.IntegrationAPI(loop)

        api.listens_to(Events.CONNECT)(on_connect)
        api.listens_to(Events.DISCONNECT)(on_disconnect)
        api.listens_to(Events.SUBSCRIBE_ENTITIES)(on_subscribe_entities)

        _config = MadVRConfig()

        if _config.is_configured():
            _LOG.info("Found existing configuration, pre-initializing for reboot survival")
            loop.create_task(_initialize_entities())

        setup_handler = MadVRSetup(api, _config, on_setup_complete)

        await api.init("driver.json", setup_handler.handle_setup)

        _LOG.info("madVR integration initialized")

        await asyncio.Future()

    except asyncio.CancelledError:
        _LOG.info("Driver cancelled")
    except Exception as e:
        _LOG.error(f"Driver error: {e}", exc_info=True)
    finally:
        if _device:
            await _device.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _LOG.info("Driver stopped by user")
