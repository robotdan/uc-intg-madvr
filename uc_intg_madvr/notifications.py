"""
Notification processor for madVR Envy push notifications.

Parses incoming notification lines from the Envy's TCP connection and returns
structured dicts. Keeps device.py focused on connection management.

:copyright: (c) 2025 by Meir Miyara
:license: MPL-2.0, see LICENSE for more details.
"""

import logging

_LOG = logging.getLogger(__name__)


class NotificationProcessor:
    """Parses madVR Envy push notification lines into structured dicts."""

    # Notification types that trigger power/lifecycle events
    POWER_OFF_TYPES = frozenset({"PowerOff", "Standby"})
    RESTART_TYPES = frozenset({"Restart", "ReloadSoftware"})

    # All known notification prefixes (for command connection filtering)
    KNOWN_NOTIFICATIONS = frozenset({
        "IncomingSignalInfo", "OutgoingSignalInfo", "AspectRatio", "MaskingRatio",
        "PowerOff", "Standby", "Restart", "ReloadSoftware", "NoSignal",
        "Temperatures", "ActivateProfile",
        # Known but not handled — logged at debug and ignored
        "OpenMenu", "CloseMenu", "KeyPress", "KeyHold", "ResetTemporary",
        "DisplayChanged", "FirmwareUpdate", "MissingHeartbeat",
        "CreateProfileGroup", "CreateProfile", "DeleteProfileGroup", "DeleteProfile",
        "RenameProfileGroup", "RenameProfile", "ChangeOption", "InheritOption",
    })

    def parse(self, line: str) -> dict | None:
        """Parse a notification line and return a structured dict, or None if ignored.

        Returns dict with at least a "type" key. Additional keys depend on type.
        Returns None for OK acknowledgments and unknown notifications.
        """
        if not line:
            return None

        parts = line.split()
        title = parts[0]

        if title == "OK":
            return None

        if title == "IncomingSignalInfo":
            return self._parse_incoming_signal_info(parts)
        elif title == "OutgoingSignalInfo":
            return self._parse_outgoing_signal_info(parts)
        elif title == "AspectRatio":
            return self._parse_aspect_ratio(parts)
        elif title == "MaskingRatio":
            return self._parse_masking_ratio(parts)
        elif title in self.POWER_OFF_TYPES:
            return {"type": title}
        elif title in self.RESTART_TYPES:
            return {"type": title}
        elif title == "NoSignal":
            return {"type": "NoSignal"}
        elif title == "Temperatures":
            return self._parse_temperatures(parts)
        elif title == "ActivateProfile":
            return self._parse_activate_profile(parts)
        else:
            _LOG.debug("Unknown notification: %s", line)
            return None

    def is_notification(self, line: str) -> bool:
        """Check if a line is a known notification (for command connection filtering)."""
        if not line:
            return False
        title = line.split()[0]
        return title in self.KNOWN_NOTIFICATIONS

    def _parse_incoming_signal_info(self, parts: list[str]) -> dict:
        """Parse: IncomingSignalInfo {res} {framerate} {2D/3D} {colorspace} {bitdepth} {HDR} {colorimetry} {blacklevels} {aspectratio}"""
        result = {"type": "IncomingSignalInfo"}
        if len(parts) >= 2:
            result["resolution"] = parts[1]
        if len(parts) >= 3:
            result["framerate"] = parts[2]
        if len(parts) >= 4:
            result["3d_mode"] = parts[3]
        if len(parts) >= 5:
            result["colorspace"] = parts[4]
        if len(parts) >= 6:
            result["bitdepth"] = parts[5]
        if len(parts) >= 7:
            result["hdr"] = parts[6]
        if len(parts) >= 8:
            result["colorimetry"] = parts[7]
        if len(parts) >= 9:
            result["blacklevels"] = parts[8]
        if len(parts) >= 10:
            result["aspectratio"] = parts[9]
        # Build a human-readable signal description
        result["signal_info"] = " ".join(parts[1:5]) if len(parts) >= 5 else " ".join(parts[1:])
        return result

    def _parse_outgoing_signal_info(self, parts: list[str]) -> dict:
        """Parse: OutgoingSignalInfo {res} {framerate} {2D/3D} {colorspace} {bitdepth} {HDR} {colorimetry} {blacklevels}"""
        result = {"type": "OutgoingSignalInfo"}
        if len(parts) >= 2:
            result["resolution"] = parts[1]
        if len(parts) >= 3:
            result["framerate"] = parts[2]
        if len(parts) >= 4:
            result["3d_mode"] = parts[3]
        if len(parts) >= 5:
            result["colorspace"] = parts[4]
        if len(parts) >= 6:
            result["bitdepth"] = parts[5]
        if len(parts) >= 7:
            result["hdr"] = parts[6]
        if len(parts) >= 8:
            result["colorimetry"] = parts[7]
        if len(parts) >= 9:
            result["blacklevels"] = parts[8]
        return result

    def _parse_aspect_ratio(self, parts: list[str]) -> dict:
        """Parse: AspectRatio {res} {decimal} {int} {name}"""
        result = {"type": "AspectRatio"}
        if len(parts) >= 2:
            result["resolution"] = parts[1]
        if len(parts) >= 3:
            result["decimal"] = parts[2]
        if len(parts) >= 4:
            result["int_ratio"] = parts[3]
        if len(parts) >= 5:
            result["name"] = " ".join(parts[4:])  # name may contain spaces/quotes
        # Full display string (everything after "AspectRatio")
        result["display"] = " ".join(parts[1:]) if len(parts) > 1 else "Unknown"
        return result

    def _parse_masking_ratio(self, parts: list[str]) -> dict:
        """Parse: MaskingRatio {res} {decimal} {int}"""
        result = {"type": "MaskingRatio"}
        if len(parts) >= 2:
            result["resolution"] = parts[1]
        if len(parts) >= 3:
            result["decimal"] = parts[2]
        if len(parts) >= 4:
            result["int_ratio"] = parts[3]
        result["display"] = " ".join(parts[1:]) if len(parts) > 1 else "Unknown"
        return result

    def _parse_temperatures(self, parts: list[str]) -> dict:
        """Parse: Temperatures {gpu} {hdmi} {cpu} {mainboard} [{extra}...]

        Protocol warns future firmware may add fields — we tolerate extra trailing fields.
        """
        result = {"type": "Temperatures", "temps": []}
        # Field order per protocol: GPU, HDMI, CPU, Mainboard
        temp_names = ["gpu", "hdmi", "cpu", "mainboard"]
        for i, name in enumerate(temp_names):
            idx = i + 1  # skip "Temperatures" prefix
            if idx < len(parts):
                try:
                    result[name] = int(parts[idx])
                    result["temps"].append(int(parts[idx]))
                except ValueError:
                    _LOG.debug("Non-integer temperature field %s: %s", name, parts[idx])
                    result[name] = 0
                    result["temps"].append(0)
            else:
                result[name] = 0
                result["temps"].append(0)
        return result

    def _parse_activate_profile(self, parts: list[str]) -> dict:
        """Parse: ActivateProfile {profileGroup} {profileId}"""
        result = {"type": "ActivateProfile"}
        if len(parts) >= 2:
            result["profile_group"] = parts[1]
        if len(parts) >= 3:
            result["profile_id"] = parts[2]
        return result
