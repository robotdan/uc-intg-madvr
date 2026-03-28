"""
Media Player entity for madVR Envy.

:copyright: (c) 2025 by Meir Miyara
:license: MPL-2.0, see LICENSE for more details.
"""

import logging
from typing import Any

from ucapi import StatusCodes
from ucapi.media_player import Attributes, Commands, DeviceClasses, Features, MediaPlayer, States

from uc_intg_madvr.config import MadVRConfig
from uc_intg_madvr.device import MadVRDevice
from uc_intg_madvr import const

_LOG = logging.getLogger(__name__)


class MadVRMediaPlayer(MediaPlayer):

    def __init__(self, config: MadVRConfig, device: MadVRDevice):
        self._config = config
        self._device = device

        entity_id = f"media_player.{config.host.replace('.', '_')}"

        features = [Features.ON_OFF]

        attributes = {
            Attributes.STATE: States.UNKNOWN,
            Attributes.MEDIA_TITLE: "Initializing...",
            Attributes.MEDIA_ARTIST: "",
            Attributes.MEDIA_ALBUM: "",
        }

        super().__init__(
            identifier=entity_id,
            name=f"{config.name} Status",
            features=features,
            attributes=attributes,
            device_class=DeviceClasses.RECEIVER,
            cmd_handler=self.command_handler,
        )

        _LOG.info(f"Created status display entity: {entity_id} (device_class=RECEIVER)")

    async def command_handler(
        self, entity: MediaPlayer, cmd_id: str, params: dict[str, Any] | None
    ) -> StatusCodes:
        _LOG.info(f"Media player command: {cmd_id}")

        try:
            if cmd_id == Commands.ON:
                # send_command handles WOL in the background and returns immediately
                await self._device.send_command(const.CMD_STANDBY, power_intent="on")
                return StatusCodes.OK

            elif cmd_id == Commands.OFF:
                # Use Standby instead of PowerOff for faster wake-up recovery.
                # PowerOff requires WOL + full boot; Standby wakes instantly via IR/WOL.
                # send_command handles all state guards (already-off short-circuit, Standby
                # toggle prevention, reactive recovery for stale state).
                # Full PowerOff is available via the Power UI page or 'Power Off' simple command.
                result = await self._device.send_command(const.CMD_STANDBY, power_intent="off")
                return StatusCodes.OK if result["success"] else StatusCodes.SERVER_ERROR
            
            else:
                _LOG.debug(f"Ignoring unsupported command: {cmd_id}")
                return StatusCodes.OK

        except Exception as e:
            _LOG.error(f"Command failed: {e}")
            return StatusCodes.SERVER_ERROR