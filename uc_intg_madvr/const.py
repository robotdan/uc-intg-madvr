"""
Constants for madVR Envy integration.

:copyright: (c) 2025 by Meir Miyara
:license: MPL-2.0, see LICENSE for more details.
"""

DEFAULT_PORT = 44077
CONNECTION_TIMEOUT = 10.0
COMMAND_TIMEOUT = 5.0

# Listener heartbeat: keeps persistent notification connection alive.
# madVR spec closes connections after 60s inactivity; 20s gives good margin.
HEARTBEAT_INTERVAL = 20.0

# Polling for non-pushed data (e.g. temperatures)
DEFAULT_POLL_INTERVAL = 60
MIN_POLL_INTERVAL = 10

# Command connection closes after this many seconds of no commands
COMMAND_IDLE_TIMEOUT = 30.0

# Ping task: TCP connect check to detect device power-on
PING_INTERVAL = 30.0

# After PowerOff/Standby, wait before treating device as connectable again
POWER_OFF_HYSTERESIS = 30.0

# Restart/ReloadSoftware: brief delay before reconnect attempt
FAST_RECONNECT_DELAY = 2.0

# Reconnection backoff sequence (seconds)
BACKOFF_DELAYS = [5, 10, 30, 60]

# Stop retrying after this many seconds total
MAX_RETRY_DURATION = 1800  # 30 minutes

# Welcome message prefix for connection validation
WELCOME_PREFIX = "WELCOME to Envy"

# Wake-on-LAN timing: 12s initial delay + 6 retries at 5s intervals = 42s max
WOL_INITIAL_DELAY = 12
WOL_MAX_RETRIES = 6
WOL_RETRY_INTERVAL = 5

CMD_POWER_OFF = "PowerOff"
CMD_STANDBY = "Standby"
CMD_RESTART = "Restart"
CMD_RELOAD_SOFTWARE = "ReloadSoftware"
CMD_HEARTBEAT = "Heartbeat"

CMD_OPEN_MENU = "OpenMenu"
CMD_CLOSE_MENU = "CloseMenu"

MENU_INFO = "Info"
MENU_SETTINGS = "Settings"
MENU_CONFIGURATION = "Configuration"
MENU_PROFILES = "Profiles"
MENU_TEST_PATTERNS = "TestPatterns"

CMD_KEY_PRESS = "KeyPress"
CMD_KEY_HOLD = "KeyHold"

KEY_UP = "UP"
KEY_DOWN = "DOWN"
KEY_LEFT = "LEFT"
KEY_RIGHT = "RIGHT"
KEY_OK = "OK"
KEY_BACK = "BACK"
KEY_MENU = "MENU"
KEY_POWER = "POWER"
KEY_INFO = "INFO"
KEY_SETTINGS = "SETTINGS"
KEY_INPUT = "INPUT"

KEY_RED = "RED"
KEY_GREEN = "GREEN"
KEY_BLUE = "BLUE"
KEY_YELLOW = "YELLOW"
KEY_MAGENTA = "MAGENTA"
KEY_CYAN = "CYAN"

CMD_SET_ASPECT_RATIO_MODE = "SetAspectRatioMode"

AR_AUTO = "Auto"
AR_HOLD = "Hold"
AR_4_3 = "4:3"
AR_16_9 = "16:9"
AR_1_85 = "1.85:1"
AR_2_00 = "2.00:1"
AR_2_20 = "2.20:1"
AR_2_35 = "2.35:1"
AR_2_40 = "2.40:1"
AR_2_55 = "2.55:1"
AR_2_76 = "2.76:1"

CMD_GET_SIGNAL_INFO = "GetIncomingSignalInfo"
CMD_GET_OUTGOING_SIGNAL_INFO = "GetOutgoingSignalInfo"
CMD_GET_ASPECT_RATIO = "GetAspectRatio"
CMD_GET_MASKING_RATIO = "GetMaskingRatio"
CMD_GET_TEMPERATURES = "GetTemperatures"
CMD_GET_MAC_ADDRESS = "GetMacAddress"

# Maps query commands to expected response prefixes. Used by the command
# connection to distinguish the actual response from interleaved push
# notifications that arrive on all open connections.
RESPONSE_PREFIX = {
    CMD_GET_SIGNAL_INFO: ("IncomingSignalInfo", "NoSignal"),
    CMD_GET_OUTGOING_SIGNAL_INFO: ("OutgoingSignalInfo",),
    CMD_GET_ASPECT_RATIO: ("AspectRatio",),
    CMD_GET_MASKING_RATIO: ("MaskingRatio",),
    CMD_GET_TEMPERATURES: ("Temperatures",),
    CMD_GET_MAC_ADDRESS: ("MacAddress",),
}

CMD_TOGGLE = "Toggle"
TOGGLE_TONE_MAP = "ToneMap"
TOGGLE_HIGHLIGHT_RECOVERY = "HighlightRecovery"
TOGGLE_SHADOW_RECOVERY = "ShadowRecovery"
TOGGLE_CONTRAST_RECOVERY = "ContrastRecovery"
TOGGLE_3DLUT = "3DLUT"
TOGGLE_SCREEN_BOUNDARIES = "ScreenBoundaries"
TOGGLE_HISTOGRAM = "Histogram"
TOGGLE_DEBUG_OSD = "DebugOSD"

CMD_TONE_MAP_ON = "ToneMapOn"
CMD_TONE_MAP_OFF = "ToneMapOff"

CMD_DISPLAY_MESSAGE = "DisplayMessage"
CMD_DISPLAY_ALERT = "DisplayAlertWindow"
CMD_CLOSE_ALERT = "CloseAlertWindow"

CMD_FORCE_1080P60 = "Force1080p60Output"
CMD_HOTPLUG = "Hotplug"
CMD_REFRESH_LICENSE = "RefreshLicenseInfo"

RESPONSE_OK = "OK"
RESPONSE_ERROR = "ERROR"
NO_SIGNAL = "NoSignal"