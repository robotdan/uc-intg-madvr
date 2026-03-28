"""
Configuration management for madVR Envy integration.

:copyright: (c) 2025 by Meir Miyara
:license: MPL-2.0, see LICENSE for more details.
"""

import json
import logging
import os
from typing import Any

from uc_intg_madvr import const

_LOG = logging.getLogger(__name__)


class MadVRConfig:
    """Configuration manager for madVR Envy integration."""

    def __init__(self, config_dir: str = None):
        """Initialize configuration manager."""
        if config_dir is None:
            config_dir = os.getenv("UC_CONFIG_HOME") or os.getenv("HOME") or "./"
        
        self._config_dir = config_dir
        self._config_file = os.path.join(config_dir, "madvr_config.json")
        self._config: dict[str, Any] = {}
        self._load_config()

    def _load_config(self) -> None:
        """Load configuration from disk."""
        try:
            if os.path.exists(self._config_file):
                with open(self._config_file, "r", encoding="utf-8") as f:
                    self._config = json.load(f)
                _LOG.info("Configuration loaded from %s", self._config_file)
            else:
                _LOG.info("No configuration file found, using defaults")
                self._config = {}
        except Exception as e:
            _LOG.error("Failed to load configuration: %s", e)
            self._config = {}

    def reload_from_disk(self) -> None:
        """Reload configuration from disk (critical for reboot survival)."""
        _LOG.info("Reloading configuration from disk")
        self._load_config()

    def _save_config(self) -> None:
        """Save configuration to disk."""
        try:
            os.makedirs(self._config_dir, exist_ok=True)
            with open(self._config_file, "w", encoding="utf-8") as f:
                json.dump(self._config, f, indent=2)
            _LOG.info("Configuration saved to %s", self._config_file)
        except Exception as e:
            _LOG.error("Failed to save configuration: %s", e)

    def is_configured(self) -> bool:
        """Check if integration is configured."""
        return bool(self._config.get("host"))

    def set_config(self, host: str, port: int = None, name: str = None) -> None:
        """Set and save configuration, preserving polling and MAC fields."""
        if port is None:
            port = const.DEFAULT_PORT
        if name is None:
            name = "madVR Envy"

        self._config = {
            "host": host,
            "port": port,
            "name": name,
            "mac_address": self._config.get("mac_address"),
            "polling_mode": self._config.get("polling_mode", "enabled"),
            "polling_interval": self._config.get("polling_interval", const.DEFAULT_POLL_INTERVAL),
        }
        self._save_config()
        _LOG.info("Configuration updated: %s:%d", host, port)

    def set_polling_config(self, mode: str, interval: int) -> None:
        """Set polling configuration."""
        if mode not in ("enabled", "on_demand", "disabled"):
            _LOG.warning("Invalid polling mode '%s', defaulting to 'enabled'", mode)
            mode = "enabled"
        if interval < const.MIN_POLL_INTERVAL:
            interval = const.MIN_POLL_INTERVAL
        self._config["polling_mode"] = mode
        self._config["polling_interval"] = interval
        self._save_config()
        _LOG.info("Polling config updated: mode=%s, interval=%ds", mode, interval)

    @property
    def host(self) -> str | None:
        """Get configured host."""
        return self._config.get("host")

    @property
    def port(self) -> int:
        """Get configured port."""
        return self._config.get("port", const.DEFAULT_PORT)

    @property
    def name(self) -> str:
        """Get device name."""
        return self._config.get("name", "madVR Envy")

    @property
    def mac_address(self) -> str | None:
        """Get stored MAC address."""
        return self._config.get("mac_address")

    @property
    def polling_mode(self) -> str:
        """Get polling mode (enabled, on_demand, disabled). Defaults to enabled for migration."""
        return self._config.get("polling_mode", "enabled")

    @property
    def polling_interval(self) -> int:
        """Get polling interval in seconds. Defaults to 60 for migration."""
        return self._config.get("polling_interval", const.DEFAULT_POLL_INTERVAL)

    def set_mac_address(self, mac_address: str) -> None:
        """Store MAC address."""
        self._config["mac_address"] = mac_address
        self._save_config()

    def clear(self) -> None:
        """Clear configuration."""
        self._config = {}
        if os.path.exists(self._config_file):
            try:
                os.remove(self._config_file)
                _LOG.info("Configuration file removed")
            except Exception as e:
                _LOG.error("Failed to remove configuration file: %s", e)