"""
MadVR Sensor entities.

:copyright: (c) 2025 by Meir Miyara
:license: MPL-2.0, see LICENSE for more details.
"""

import logging
from typing import Any

from ucapi.sensor import Attributes, DeviceClasses, Sensor, States

from uc_intg_madvr.config import MadVRConfig
from uc_intg_madvr.device import MadVRDevice

_LOG = logging.getLogger(__name__)


class MadVRSignalSensor(Sensor):
    """MadVR signal info sensor."""

    def __init__(self, config: MadVRConfig, device: MadVRDevice):
        """Initialize sensor."""
        self._device = device
        self._config = config

        entity_id = f"sensor.{config.host.replace('.', '_')}.signal"

        super().__init__(
            entity_id,
            f"{config.name} Signal",
            [],
            {
                Attributes.STATE: States.UNAVAILABLE,
                Attributes.VALUE: "Unknown",
            },
            device_class=DeviceClasses.CUSTOM,
            options={"custom_unit": ""},
        )

        _LOG.info(f"Created signal sensor: {entity_id}")


class MadVRTemperatureSensor(Sensor):
    """MadVR temperature sensor."""

    def __init__(self, config: MadVRConfig, device: MadVRDevice, temp_index: int, temp_name: str):
        """Initialize temperature sensor.

        Args:
            config: MadVR configuration
            device: MadVR device instance
            temp_index: Index of temperature value (0=GPU, 1=HDMI, 2=CPU, 3=Mainboard)
            temp_name: Display name for the temperature sensor
        """
        self._device = device
        self._config = config
        self._temp_index = temp_index

        # Protocol field order: 0=GPU, 1=HDMI, 2=CPU, 3=Mainboard
        entity_id = f"sensor.{config.host.replace('.', '_')}.temp_{temp_name.lower()}"

        super().__init__(
            entity_id,
            f"{config.name} {temp_name} Temp",
            [],
            {
                Attributes.STATE: States.UNAVAILABLE,
                Attributes.VALUE: 0,
                Attributes.UNIT: "°C",
            },
            device_class=DeviceClasses.TEMPERATURE,
            options={"native_unit": "°C", "decimals": 0},
        )

        _LOG.info(f"Created temperature sensor: {entity_id} (index={temp_index})")


class MadVRAspectRatioSensor(Sensor):
    """MadVR aspect ratio sensor."""

    def __init__(self, config: MadVRConfig, device: MadVRDevice):
        """Initialize sensor."""
        self._device = device
        self._config = config

        entity_id = f"sensor.{config.host.replace('.', '_')}.aspect_ratio"

        super().__init__(
            entity_id,
            f"{config.name} Aspect Ratio",
            [],
            {
                Attributes.STATE: States.UNAVAILABLE,
                Attributes.VALUE: "Unknown",
            },
            device_class=DeviceClasses.CUSTOM,
            options={"custom_unit": ""},
        )

        _LOG.info(f"Created aspect ratio sensor: {entity_id}")


class MadVRMaskingRatioSensor(Sensor):
    """MadVR masking ratio sensor."""

    def __init__(self, config: MadVRConfig, device: MadVRDevice):
        """Initialize sensor."""
        self._device = device
        self._config = config

        entity_id = f"sensor.{config.host.replace('.', '_')}.masking_ratio"

        super().__init__(
            entity_id,
            f"{config.name} Masking Ratio",
            [],
            {
                Attributes.STATE: States.UNAVAILABLE,
                Attributes.VALUE: "Unknown",
            },
            device_class=DeviceClasses.CUSTOM,
            options={"custom_unit": ""},
        )

        _LOG.info(f"Created masking ratio sensor: {entity_id}")
