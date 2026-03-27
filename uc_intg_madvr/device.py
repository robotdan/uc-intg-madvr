"""
madVR Envy device handler.

Dual-connection architecture:
- Listener connection (persistent): receives push notifications via readuntil(b'\\n').
- Command connection (lazy): sends commands, closes after idle timeout.

:copyright: (c) 2025 by Meir Miyara
:license: MPL-2.0, see LICENSE for more details.
"""

import asyncio
import logging
import socket
import time
from enum import IntEnum, StrEnum
from asyncio import AbstractEventLoop
from pyee.asyncio import AsyncIOEventEmitter

from uc_intg_madvr.config import MadVRConfig
from uc_intg_madvr.notifications import NotificationProcessor
from uc_intg_madvr import const

_LOG = logging.getLogger(__name__)


class EVENTS(IntEnum):
    UPDATE = 1


class PowerState(StrEnum):
    OFF = "OFF"
    ON = "ON"
    STANDBY = "STANDBY"
    UNKNOWN = "UNKNOWN"


class MadVRDevice:

    def __init__(self, config: MadVRConfig, loop: AbstractEventLoop | None = None):
        self._loop: AbstractEventLoop = loop or asyncio.get_running_loop()
        self.events = AsyncIOEventEmitter(self._loop)
        self._config = config
        self._state: PowerState = PowerState.UNKNOWN
        self._signal_info: str = "Unknown"
        self._notification_processor = NotificationProcessor()

        # Listener connection (persistent, for push notifications)
        self._listener_reader: asyncio.StreamReader | None = None
        self._listener_writer: asyncio.StreamWriter | None = None
        self._listener_connected = asyncio.Event()

        # Command connection (lazy, for sending commands)
        self._cmd_reader: asyncio.StreamReader | None = None
        self._cmd_writer: asyncio.StreamWriter | None = None
        self._cmd_lock = asyncio.Lock()
        self._cmd_last_used: float = 0.0
        self._cmd_idle_task: asyncio.Task | None = None

        # Sensor data — field order per madVR protocol: GPU, HDMI, CPU, Mainboard
        self._temperatures: list[int] = [0, 0, 0, 0]
        self._aspect_ratio: str = "Unknown"
        self._masking_ratio: str = "Unknown"
        self._aspect_ratio_mode: str = "Auto"
        self._outgoing_signal_info: dict | None = None
        self._active_profile: dict | None = None

        # Background tasks
        self._notification_listener_task: asyncio.Task | None = None
        self._listener_heartbeat_task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None

        # Reconnection state
        self._running = False
        self._reconnect_event = asyncio.Event()
        self._backoff_index = 0
        self._retry_start_time: float = 0.0
        self._power_off_time: float = 0.0  # for hysteresis
        self._fast_reconnect = False  # set by Restart/ReloadSoftware
        self._suspended = False  # True when UC Remote is in standby

    @property
    def identifier(self) -> str:
        return self._config.host.replace('.', '_')

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def state(self) -> PowerState:
        return self._state

    @property
    def signal_info(self) -> str:
        return self._signal_info

    @property
    def aspect_ratio(self) -> str:
        return self._aspect_ratio

    @property
    def masking_ratio(self) -> str:
        return self._masking_ratio

    @property
    def temperatures(self) -> list[int]:
        return self._temperatures

    @property
    def aspect_ratio_mode(self) -> str:
        return self._aspect_ratio_mode

    async def start(self):
        """Start all background tasks."""
        if self._running:
            return
        self._running = True

        if not self._config.mac_address:
            _LOG.info("[%s] No MAC address in config, will fetch when device is online", self.name)
        else:
            _LOG.info("[%s] MAC address loaded from config: %s", self.name, self._config.mac_address)

        self._create_background_tasks()
        _LOG.info("[%s] Started push notification listener", self.name)

    async def stop(self):
        """Stop all background tasks and close connections."""
        self._running = False

        await self._cancel_all_tasks()
        await self._disconnect_listener()
        await self._disconnect_cmd()
        _LOG.info("[%s] Stopped", self.name)

    async def suspend(self):
        """Suspend for UC Remote standby. Cancel tasks, close connections, preserve state."""
        if self._suspended:
            return
        self._suspended = True
        _LOG.info("[%s] Suspending (UC Remote entering standby)", self.name)

        await self._cancel_all_tasks()

        # Await disconnects (not fire-and-forget) to ensure _listener_connected is cleared
        await self._disconnect_listener()
        await self._disconnect_cmd()

        _LOG.info("[%s] Suspended — state preserved as %s", self.name, self._state.value)

    async def resume(self):
        """Resume after UC Remote wakes from standby. Recreate tasks, reconnect, resync."""
        if not self._suspended:
            return
        if not self._running:
            _LOG.warning("[%s] Cannot resume — device is stopped", self.name)
            return
        self._suspended = False
        _LOG.info("[%s] Resuming (UC Remote exiting standby)", self.name)

        # Reset backoff for immediate reconnection attempt
        self._reset_backoff()

        self._create_background_tasks()
        _LOG.info("[%s] Resumed — background tasks recreated, reconnecting", self.name)

    # ── Task Lifecycle Helpers ─────────────────────────────────────────

    async def _cancel_all_tasks(self):
        """Cancel all background tasks and await their completion."""
        for task in [self._notification_listener_task, self._listener_heartbeat_task,
                     self._ping_task, self._poll_task, self._cmd_idle_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    def _create_background_tasks(self):
        """Create all background tasks."""
        self._listener_heartbeat_task = self._loop.create_task(self._listener_heartbeat_loop())
        self._notification_listener_task = self._loop.create_task(self._notification_listener_loop())
        self._ping_task = self._loop.create_task(self._ping_loop())
        if self._config.polling_mode == "enabled":
            self._poll_task = self._loop.create_task(self._poll_loop())

    async def _interruptible_sleep(self, seconds: float):
        """Sleep that can be interrupted by _reconnect_event."""
        self._reconnect_event.clear()
        try:
            await asyncio.wait_for(self._reconnect_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass  # Normal timeout — sleep completed

    # ── Public API ──────────────────────────────────────────────────────

    async def send_command(self, command: str, power_intent: str | None = None) -> dict:
        """Send a command via the command connection. Triggers auto-recovery.

        Args:
            command: The madVR protocol command string.
            power_intent: "on" or "off" for CMD_STANDBY disambiguation in reactive recovery.
        """
        # Device is unreachable when in STANDBY or OFF (TCP port closed).
        if command == const.CMD_POWER_OFF and self._state in (PowerState.OFF, PowerState.STANDBY):
            _LOG.info("[%s] Device is already %s, power off successful", self.name, self._state.value)
            return {"success": True}

        if command == const.CMD_STANDBY and self._state in (PowerState.OFF, PowerState.STANDBY):
            if power_intent == "off":
                _LOG.info("[%s] Device already %s, off command successful", self.name, self._state.value)
                return {"success": True}
            _LOG.info("[%s] Device is %s, triggering Wake-on-LAN (background)", self.name, self._state.value)
            self._loop.create_task(self._wol_and_wait())
            # Return immediately — background task handles WOL + recovery.
            # Ping/listener tasks will update state when device comes online.
            return {"success": True}

        # Prevent Standby toggle: "Standby" is a toggle command on the madVR protocol.
        # With power_intent="on", sending it to an already-ON device would put it to sleep.
        if command == const.CMD_STANDBY and power_intent == "on" and self._state == PowerState.ON:
            _LOG.info("[%s] Device already ON, power on successful", self.name)
            return {"success": True}

        # Any user command triggers auto-recovery (resets backoff)
        self._reset_backoff()

        result = await self._send_cmd(command)

        # Reactive recovery: if command failed and we thought device was ON, state is stale.
        if not result["success"] and self._state == PowerState.ON:
            _LOG.warning("[%s] Device unreachable but state was ON, correcting to STANDBY", self.name)
            self._teardown_connections(PowerState.STANDBY)

            # Re-evaluate based on corrected state and caller intent
            if command == const.CMD_STANDBY:
                if power_intent == "on":
                    _LOG.info("[%s] Triggering WOL after state correction", self.name)
                    self._loop.create_task(self._wol_and_wait())
                    return {"success": True}
                else:
                    # power_intent="off" or None: device is already in desired state
                    _LOG.info("[%s] Device now in STANDBY (desired state for off)", self.name)
                    return {"success": True}
            elif command == const.CMD_POWER_OFF:
                _LOG.info("[%s] Device now in STANDBY (desired state for power off)", self.name)
                return {"success": True}
            # Any other command: device is off, can't execute — return original error

        return result

    def set_aspect_ratio_mode(self, mode: str):
        self._aspect_ratio_mode = mode
        self._emit_select_update()

    async def query_on_demand(self):
        """One-shot query for non-pushed data (temperatures). Used in on_demand polling mode."""
        if self._state in (PowerState.OFF, PowerState.STANDBY, PowerState.UNKNOWN):
            return
        result = await self._send_cmd(const.CMD_GET_TEMPERATURES)
        if result["success"] and result.get("data"):
            notification = self._notification_processor.parse(result["data"])
            if notification and notification["type"] == "Temperatures":
                self._handle_temperatures(notification)

    async def trigger_reconnect(self):
        """Signal auto-recovery: reset backoff and wake heartbeat loop for immediate reconnect."""
        self._reset_backoff()
        self._reconnect_event.set()  # Wake the heartbeat loop from any sleep
        if not self._listener_connected.is_set():
            _LOG.info("[%s] Auto-recovery triggered, reconnecting", self.name)

    # ── Listener Connection ─────────────────────────────────────────────

    async def _connect_listener(self) -> bool:
        """Establish the listener TCP connection with welcome validation."""
        try:
            _LOG.info("[%s] Connecting listener to %s:%d", self.name, self._config.host, self._config.port)
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._config.host, self._config.port),
                timeout=const.CONNECTION_TIMEOUT,
            )

            # Validate welcome message
            welcome_line = await asyncio.wait_for(reader.readuntil(b'\n'), timeout=const.COMMAND_TIMEOUT)
            welcome = welcome_line.decode().strip('\r\n')

            if not welcome.startswith(const.WELCOME_PREFIX):
                _LOG.error("[%s] Invalid welcome message: %s", self.name, welcome)
                writer.close()
                return False

            _LOG.info("[%s] Listener connected: %s", self.name, welcome)
            self._listener_reader = reader
            self._listener_writer = writer
            self._listener_connected.set()
            return True

        except Exception as e:
            _LOG.error("[%s] Listener connection failed: %s", self.name, e)
            await self._disconnect_listener()
            return False

    async def _disconnect_listener(self):
        """Close the listener connection."""
        self._listener_connected.clear()
        if self._listener_writer:
            try:
                self._listener_writer.close()
            except Exception:
                pass
            finally:
                self._listener_writer = None
                self._listener_reader = None

    async def _notification_listener_loop(self):
        """Read complete lines from the listener connection and dispatch notifications."""
        while self._running:
            try:
                # Wait for listener connection to be established
                await self._listener_connected.wait()
                if not self._running:
                    break

                reader = self._listener_reader
                if reader is None:
                    await asyncio.sleep(0.1)
                    continue

                try:
                    raw = await reader.readuntil(b'\n')
                except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError, OSError):
                    _LOG.warning("[%s] Listener connection lost", self.name)
                    await self._disconnect_listener()
                    continue

                # Use errors='replace' to handle non-UTF8 bytes from the device gracefully
                # rather than raising UnicodeDecodeError which would trigger error recovery
                line = raw.decode('utf-8', errors='replace').rstrip('\r\n')
                if not line:
                    continue

                _LOG.debug("[%s] Notification: %s", self.name, line)
                notification = self._notification_processor.parse(line)
                if notification:
                    self._dispatch_notification(notification)

            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOG.error("[%s] Notification listener error: %s", self.name, e)
                await asyncio.sleep(1)

    async def _listener_heartbeat_loop(self):
        """Send heartbeats on listener connection. Sole owner of listener lifecycle (establish + backoff)."""
        while self._running:
            try:
                if not self._listener_connected.is_set():
                    # Attempt to establish/re-establish listener connection
                    delay = self._get_reconnect_delay()
                    if delay is None:
                        # Backoff exhausted — wait for auto-recovery signal
                        _LOG.warning("[%s] Backoff exhausted, waiting for auto-recovery", self.name)
                        await self._interruptible_sleep(const.BACKOFF_DELAYS[-1])
                        continue

                    if delay > 0:
                        await self._interruptible_sleep(delay)

                    if not self._running:
                        break

                    if await self._connect_listener():
                        self._reset_backoff()
                        # Fetch MAC if needed, then sync state
                        if not self._config.mac_address:
                            await self._fetch_mac_address()
                        await self._sync_state_after_reconnect()
                    else:
                        self._advance_backoff()
                        continue

                # Send heartbeat
                writer = self._listener_writer
                if writer and not writer.is_closing():
                    try:
                        writer.write(b"Heartbeat\r\n")
                        await writer.drain()
                    except (ConnectionResetError, BrokenPipeError, OSError):
                        _LOG.warning("[%s] Heartbeat send failed, connection lost", self.name)
                        await self._disconnect_listener()
                        continue

                await self._interruptible_sleep(const.HEARTBEAT_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOG.error("[%s] Heartbeat loop error: %s", self.name, e)
                await asyncio.sleep(1)

    # ── Command Connection (Lazy) ───────────────────────────────────────

    async def _ensure_cmd_connected(self) -> bool:
        """Ensure the command connection is open. Lazy — connects on demand."""
        if self._cmd_writer and not self._cmd_writer.is_closing():
            return True

        try:
            _LOG.debug("[%s] Opening command connection", self.name)
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._config.host, self._config.port),
                timeout=const.CONNECTION_TIMEOUT,
            )

            # Validate welcome
            welcome_line = await asyncio.wait_for(reader.readuntil(b'\n'), timeout=const.COMMAND_TIMEOUT)
            welcome = welcome_line.decode().strip('\r\n')

            if not welcome.startswith(const.WELCOME_PREFIX):
                _LOG.error("[%s] Command connection: invalid welcome: %s", self.name, welcome)
                writer.close()
                return False

            self._cmd_reader = reader
            self._cmd_writer = writer
            self._cmd_last_used = time.monotonic()

            # Start idle timeout watcher
            if self._cmd_idle_task and not self._cmd_idle_task.done():
                self._cmd_idle_task.cancel()
            self._cmd_idle_task = self._loop.create_task(self._cmd_idle_watcher())

            _LOG.debug("[%s] Command connection established", self.name)
            return True

        except Exception as e:
            _LOG.error("[%s] Command connection failed: %s", self.name, e)
            await self._disconnect_cmd()
            return False

    async def _disconnect_cmd(self):
        """Close the command connection."""
        if self._cmd_writer:
            try:
                self._cmd_writer.close()
            except Exception:
                pass
            finally:
                self._cmd_writer = None
                self._cmd_reader = None

    async def _cmd_idle_watcher(self):
        """Close the command connection after idle timeout."""
        try:
            while self._cmd_writer and not self._cmd_writer.is_closing():
                elapsed = time.monotonic() - self._cmd_last_used
                remaining = const.COMMAND_IDLE_TIMEOUT - elapsed
                if remaining <= 0:
                    _LOG.debug("[%s] Command connection idle, closing", self.name)
                    async with self._cmd_lock:
                        await self._disconnect_cmd()
                    return
                await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            pass

    async def _send_cmd(self, command: str, timeout: float | None = None) -> dict:
        """Send a command on the command connection, discarding interleaved notifications.

        For query commands (Get*), the expected response prefix is looked up from
        const.RESPONSE_PREFIX so the reader can distinguish the actual response from
        interleaved push notifications that arrive on all open connections.
        """
        if timeout is None:
            timeout = const.COMMAND_TIMEOUT

        expected_prefix = const.RESPONSE_PREFIX.get(command)

        async with self._cmd_lock:
            try:
                if not await self._ensure_cmd_connected():
                    return {"success": False, "error": "Connection failed"}

                # Capture local references: _teardown_connections can clear the instance
                # attributes between await points (e.g. during drain()), which would cause
                # NoneType errors. Local refs keep the objects alive so the underlying
                # socket errors are raised instead, which are properly caught below.
                writer = self._cmd_writer
                reader = self._cmd_reader

                _LOG.debug("[%s] Sending: %s", self.name, command)
                writer.write(f"{command}\r\n".encode())
                await writer.drain()
                self._cmd_last_used = time.monotonic()

                # Read response, discarding any interleaved notifications
                try:
                    deadline = time.monotonic() + timeout
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise asyncio.TimeoutError()

                        raw = await asyncio.wait_for(
                            reader.readuntil(b'\n'),
                            timeout=remaining,
                        )
                        response = raw.decode().rstrip('\r\n')
                        _LOG.debug("[%s] Cmd received: %s", self.name, response)

                        # Check if this is a command response (OK, ERROR, or expected data)
                        if response.startswith(const.RESPONSE_OK):
                            if expected_prefix:
                                # Query commands: OK is sent before the data response, keep reading
                                _LOG.debug("[%s] OK received for query, waiting for data...", self.name)
                                continue
                            return {"success": True}
                        elif response.startswith(const.RESPONSE_ERROR):
                            error_msg = response.replace(const.RESPONSE_ERROR, "").strip().strip('"')
                            return {"success": False, "error": error_msg}
                        elif expected_prefix and response.startswith(expected_prefix):
                            return {"success": True, "data": response}
                        elif self._notification_processor.is_notification(response):
                            _LOG.debug("[%s] Discarding notification on cmd connection: %s", self.name, response)
                            continue
                        else:
                            # Unknown data response — return it
                            return {"success": True, "data": response}

                except asyncio.TimeoutError:
                    _LOG.warning("[%s] Command timeout: %s", self.name, command)
                    await self._disconnect_cmd()
                    return {"success": False, "error": "Timeout"}

                except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError, OSError):
                    # Power commands (Standby, PowerOff, Restart, ReloadSoftware) cause the
                    # device to close TCP connections. The command was sent successfully but
                    # the "OK" response may be lost. Treat as success for these commands.
                    if command in (const.CMD_STANDBY, const.CMD_POWER_OFF,
                                   const.CMD_RESTART, const.CMD_RELOAD_SOFTWARE):
                        _LOG.info("[%s] Connection lost after %s (expected)", self.name, command)
                        await self._disconnect_cmd()
                        return {"success": True}
                    _LOG.error("[%s] Command connection lost during read", self.name)
                    await self._disconnect_cmd()
                    return {"success": False, "error": "Connection lost"}

            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as e:
                _LOG.error("[%s] Network error: %s", self.name, e)
                await self._disconnect_cmd()
                return {"success": False, "error": f"Network error: {e.__class__.__name__}"}

            except Exception as e:
                _LOG.error("[%s] Command failed: %s", self.name, e)
                await self._disconnect_cmd()
                return {"success": False, "error": str(e)}

    # ── Notification Dispatch ───────────────────────────────────────────

    def _dispatch_notification(self, notification: dict):
        """Route a parsed notification to the appropriate handler."""
        ntype = notification["type"]

        if ntype == "IncomingSignalInfo":
            self._handle_incoming_signal(notification)
        elif ntype == "OutgoingSignalInfo":
            self._outgoing_signal_info = notification
            _LOG.debug("[%s] Stored outgoing signal info", self.name)
        elif ntype == "AspectRatio":
            self._handle_aspect_ratio(notification)
        elif ntype == "MaskingRatio":
            self._handle_masking_ratio(notification)
        elif ntype in NotificationProcessor.POWER_OFF_TYPES:
            self._handle_power_off(ntype)
        elif ntype in NotificationProcessor.RESTART_TYPES:
            self._handle_restart(ntype)
        elif ntype == "NoSignal":
            self._handle_no_signal()
        elif ntype == "Temperatures":
            self._handle_temperatures(notification)
        elif ntype == "ActivateProfile":
            self._active_profile = notification
            _LOG.info("[%s] Active profile changed: group=%s id=%s",
                      self.name, notification.get("profile_group"), notification.get("profile_id"))

    def _handle_incoming_signal(self, notification: dict):
        from ucapi.sensor import Attributes as SensorAttributes, States as SensorStates

        old_state = self._state
        self._state = PowerState.ON
        self._signal_info = notification.get("signal_info", "Signal Active")

        self.events.emit(EVENTS.UPDATE, self.identifier, {
            "state": self._state,
            "signal_info": self._signal_info,
        })

        sensor_id = f"sensor.{self.identifier}.signal"
        self.events.emit(EVENTS.UPDATE, sensor_id, {
            SensorAttributes.STATE: SensorStates.ON,
            SensorAttributes.VALUE: self._signal_info,
        })

        if old_state != PowerState.ON:
            _LOG.info("[%s] State: %s -> ON, Signal: %s", self.name, old_state, self._signal_info)

    def _handle_no_signal(self):
        """Handle NoSignal notification. Does NOT change power state.

        NoSignal means the device is on but has no input signal — it is not
        entering standby. Only PowerOff/Standby notifications change power state.
        """
        from ucapi.sensor import Attributes as SensorAttributes, States as SensorStates

        old_signal = self._signal_info
        self._signal_info = "No Signal"

        # Update signal info on media player without changing power state
        self.events.emit(EVENTS.UPDATE, self.identifier, {
            "signal_info": self._signal_info,
        })

        # Mark signal sensor as unavailable (no signal data to show)
        sensor_id = f"sensor.{self.identifier}.signal"
        self.events.emit(EVENTS.UPDATE, sensor_id, {
            SensorAttributes.STATE: SensorStates.UNAVAILABLE,
            SensorAttributes.VALUE: self._signal_info,
        })

        if old_signal != self._signal_info:
            _LOG.info("[%s] No signal (power state unchanged: %s)", self.name, self._state)

    def _handle_aspect_ratio(self, notification: dict):
        from ucapi.sensor import Attributes as SensorAttributes, States as SensorStates

        self._aspect_ratio = notification.get("display", "Unknown")

        sensor_id = f"sensor.{self.identifier}.aspect_ratio"
        self.events.emit(EVENTS.UPDATE, sensor_id, {
            SensorAttributes.STATE: SensorStates.ON,
            SensorAttributes.VALUE: self._aspect_ratio,
        })

    def _handle_masking_ratio(self, notification: dict):
        from ucapi.sensor import Attributes as SensorAttributes, States as SensorStates

        self._masking_ratio = notification.get("display", "Unknown")

        sensor_id = f"sensor.{self.identifier}.masking_ratio"
        self.events.emit(EVENTS.UPDATE, sensor_id, {
            SensorAttributes.STATE: SensorStates.ON,
            SensorAttributes.VALUE: self._masking_ratio,
        })

    def _handle_temperatures(self, notification: dict):
        from ucapi.sensor import Attributes as SensorAttributes, States as SensorStates

        self._temperatures = notification.get("temps", [0, 0, 0, 0])

        # Field order per protocol: GPU, HDMI, CPU, Mainboard
        temp_names = ["gpu", "hdmi", "cpu", "mainboard"]
        for idx, temp_name in enumerate(temp_names):
            if idx < len(self._temperatures):
                sensor_id = f"sensor.{self.identifier}.temp_{temp_name}"
                self.events.emit(EVENTS.UPDATE, sensor_id, {
                    SensorAttributes.STATE: SensorStates.ON,
                    SensorAttributes.VALUE: self._temperatures[idx],
                    SensorAttributes.UNIT: "°C",
                })

    def _handle_power_off(self, ntype: str):
        """Handle PowerOff/Standby notification — full connection teardown."""
        _LOG.info("[%s] Received %s notification", self.name, ntype)

        new_state = PowerState.OFF if ntype == "PowerOff" else PowerState.STANDBY
        self._power_off_time = time.monotonic()
        self._fast_reconnect = False

        self._teardown_connections(new_state)

    def _handle_restart(self, ntype: str):
        """Handle Restart/ReloadSoftware — teardown with fast reconnect."""
        _LOG.info("[%s] Received %s notification, fast reconnect in %ds",
                  self.name, ntype, const.FAST_RECONNECT_DELAY)

        self._fast_reconnect = True
        self._power_off_time = 0.0  # no hysteresis for restart

        self._teardown_connections(PowerState.OFF)

    def _teardown_connections(self, new_state: PowerState):
        """Close all connections, clear state, mark entities unavailable."""
        from ucapi.sensor import Attributes as SensorAttributes, States as SensorStates

        old_state = self._state
        self._state = new_state
        # Restart/ReloadSoftware also goes through this path with PowerState.OFF,
        # but _handle_restart sets _fast_reconnect=True so the device will reconnect quickly.
        self._signal_info = "Powered Off" if new_state == PowerState.OFF else "Standby"

        # Synchronously close connections and clear refs to prevent races.
        # close() is sync and must happen NOW so the heartbeat loop doesn't
        # try to reconnect before teardown completes.
        self._listener_connected.clear()
        if self._listener_writer:
            try:
                self._listener_writer.close()
            except Exception:
                pass
            self._listener_writer = None
            self._listener_reader = None

        if self._cmd_writer:
            try:
                self._cmd_writer.close()
            except Exception:
                pass
            self._cmd_writer = None
            self._cmd_reader = None

        # Emit power state change
        self.events.emit(EVENTS.UPDATE, self.identifier, {
            "state": self._state,
            "signal_info": self._signal_info,
        })

        # Mark all sensors unavailable
        sensor_ids = [
            f"sensor.{self.identifier}.signal",
            f"sensor.{self.identifier}.aspect_ratio",
            f"sensor.{self.identifier}.masking_ratio",
        ]
        temp_names = ["gpu", "hdmi", "cpu", "mainboard"]
        sensor_ids.extend(f"sensor.{self.identifier}.temp_{n}" for n in temp_names)

        for sid in sensor_ids:
            self.events.emit(EVENTS.UPDATE, sid, {
                SensorAttributes.STATE: SensorStates.UNAVAILABLE,
            })

        _LOG.info("[%s] State: %s -> %s, connections torn down", self.name, old_state, new_state)

    # ── Ping Task ───────────────────────────────────────────────────────

    async def _ping_loop(self):
        """Periodically check if the device is reachable. Handles hysteresis and auto-recovery."""
        while self._running:
            try:
                await asyncio.sleep(const.PING_INTERVAL)
                if not self._running:
                    break

                # Skip ping if listener is already connected
                if self._listener_connected.is_set():
                    continue

                # Apply power-off hysteresis
                if self._power_off_time > 0:
                    elapsed = time.monotonic() - self._power_off_time
                    if elapsed < const.POWER_OFF_HYSTERESIS:
                        continue

                # Try TCP connect
                reachable = await self._is_device_reachable()

                if reachable and not self._listener_connected.is_set():
                    _LOG.info("[%s] Ping: device is reachable, triggering reconnect", self.name)
                    self._reset_backoff()
                    self._reconnect_event.set()
                    self._power_off_time = 0.0

            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOG.debug("[%s] Ping error: %s", self.name, e)

    async def _is_device_reachable(self) -> bool:
        """Quick TCP connect/close to check if the Envy is accepting connections."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._config.host, self._config.port),
                timeout=3.0,
            )
            writer.close()
            return True
        except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
            return False

    # ── Poll Task ───────────────────────────────────────────────────────

    async def _poll_loop(self):
        """Poll non-pushed data (temperatures) at configured interval."""
        while self._running:
            try:
                interval = max(self._config.polling_interval, const.MIN_POLL_INTERVAL)
                await asyncio.sleep(interval)
                if not self._running:
                    break

                # Only poll when device is known-on and listener is connected
                if self._state in (PowerState.OFF, PowerState.UNKNOWN):
                    continue
                if not self._listener_connected.is_set():
                    continue

                result = await self._send_cmd(const.CMD_GET_TEMPERATURES)
                if result["success"] and result.get("data"):
                    notification = self._notification_processor.parse(result["data"])
                    if notification and notification["type"] == "Temperatures":
                        self._handle_temperatures(notification)

            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOG.error("[%s] Poll error: %s", self.name, e)

    # ── Reconnection / Backoff ──────────────────────────────────────────

    def _reset_backoff(self):
        """Reset reconnection backoff (auto-recovery trigger)."""
        self._backoff_index = 0
        self._retry_start_time = 0.0

    def _advance_backoff(self):
        """Move to the next backoff delay."""
        if self._retry_start_time == 0.0:
            self._retry_start_time = time.monotonic()
        if self._backoff_index < len(const.BACKOFF_DELAYS):
            self._backoff_index += 1

    def _get_reconnect_delay(self) -> float | None:
        """Get the next reconnect delay, or None if backoff is exhausted."""
        if self._fast_reconnect:
            self._fast_reconnect = False
            return const.FAST_RECONNECT_DELAY

        # Check if we've exceeded max retry duration
        if self._retry_start_time > 0:
            elapsed = time.monotonic() - self._retry_start_time
            if elapsed > const.MAX_RETRY_DURATION:
                return None

        if self._backoff_index == 0:
            return 0  # Immediate first attempt before entering backoff sequence
        return const.BACKOFF_DELAYS[min(self._backoff_index - 1, len(const.BACKOFF_DELAYS) - 1)]

    # ── Post-Reconnect Sync ─────────────────────────────────────────────

    async def _sync_state_after_reconnect(self):
        """Query full device state via command connection after listener reconnects."""
        _LOG.info("[%s] Syncing state after reconnect", self.name)

        # Signal info
        # GetIncomingSignalInfo returns "IncomingSignalInfo ..." or "NoSignal"
        result = await self._send_cmd(const.CMD_GET_SIGNAL_INFO)
        if result["success"] and result.get("data"):
            notification = self._notification_processor.parse(result["data"])
            if notification:
                self._dispatch_notification(notification)

        # Outgoing signal info
        result = await self._send_cmd(const.CMD_GET_OUTGOING_SIGNAL_INFO)
        if result["success"] and result.get("data"):
            notification = self._notification_processor.parse(result["data"])
            if notification:
                self._outgoing_signal_info = notification

        # Aspect ratio
        result = await self._send_cmd(const.CMD_GET_ASPECT_RATIO)
        if result["success"] and result.get("data"):
            notification = self._notification_processor.parse(result["data"])
            if notification:
                self._dispatch_notification(notification)

        # Masking ratio
        result = await self._send_cmd(const.CMD_GET_MASKING_RATIO)
        if result["success"] and result.get("data"):
            notification = self._notification_processor.parse(result["data"])
            if notification:
                self._dispatch_notification(notification)

        # Temperatures (if polling not disabled)
        if self._config.polling_mode != "disabled":
            result = await self._send_cmd(const.CMD_GET_TEMPERATURES)
            if result["success"] and result.get("data"):
                notification = self._notification_processor.parse(result["data"])
                if notification and notification["type"] == "Temperatures":
                    self._handle_temperatures(notification)

        # We successfully connected and queried the device — it's on.
        # This corrects stale state from STANDBY/OFF/UNKNOWN after a wake.
        if self._state != PowerState.ON:
            old_state = self._state
            self._state = PowerState.ON
            if self._signal_info in ("Unknown", "Standby", "Powered Off"):
                self._signal_info = "No Signal"
            self.events.emit(EVENTS.UPDATE, self.identifier, {
                "state": self._state,
                "signal_info": self._signal_info,
            })
            _LOG.info("[%s] State: %s -> ON (connected after sync)", self.name, old_state)

    # ── Select Entity Helper ────────────────────────────────────────────

    def _emit_select_update(self):
        from ucapi.select import Attributes as SelectAttributes, States as SelectStates

        select_id = f"select.{self.identifier}.aspect_ratio_mode"
        self.events.emit(EVENTS.UPDATE, select_id, {
            SelectAttributes.STATE: SelectStates.ON,
            SelectAttributes.CURRENT_OPTION: self._aspect_ratio_mode,
        })

    # ── MAC Address ─────────────────────────────────────────────────────

    async def _fetch_mac_address(self):
        _LOG.info("[%s] Fetching MAC address...", self.name)
        try:
            result = await self._send_cmd(const.CMD_GET_MAC_ADDRESS)
            if result["success"] and result.get("data"):
                response_data = result["data"]
                if "MacAddress" in response_data:
                    parts = response_data.split()
                    if len(parts) >= 2:
                        mac_address = parts[1]
                        self._config.set_mac_address(mac_address)
                        _LOG.info("[%s] MAC address stored: %s", self.name, mac_address)
                        return
                _LOG.warning("[%s] Could not parse MAC address from response", self.name)
            else:
                _LOG.warning("[%s] Failed to get MAC address: %s", self.name, result.get("error"))
        except Exception as e:
            _LOG.error("[%s] Exception fetching MAC address: %s", self.name, e)

    # ── Wake-on-LAN ────────────────────────────────────────────────────

    async def _wol_and_wait(self):
        """Background task: send WOL, update title, wait for device to come online."""
        self.events.emit(EVENTS.UPDATE, self.identifier, {
            "signal_info": "Waking up...",
        })

        result = await self._wake_on_lan()
        if result["success"]:
            # Wake the heartbeat loop so it reconnects immediately
            self._reconnect_event.set()
        else:
            _LOG.warning("[%s] WOL failed: %s — ping task will keep trying", self.name, result.get("error"))
            self.events.emit(EVENTS.UPDATE, self.identifier, {
                "signal_info": "Standby" if self._state == PowerState.STANDBY else "Powered Off",
            })

    def _send_wol_packet(self, magic_packet: bytes):
        """Send a single WOL magic packet via UDP broadcast."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(magic_packet, ('<broadcast>', 9))

    async def _wake_on_lan(self) -> dict:
        mac_address = self._config.mac_address

        if not mac_address:
            _LOG.error("[%s] No MAC address available for WOL", self.name)
            return {"success": False, "error": "No MAC address configured"}

        try:
            mac_with_colons = mac_address.replace("-", ":")
            mac_bytes = bytes.fromhex(mac_with_colons.replace(":", ""))
            magic_packet = b'\xff' * 6 + mac_bytes * 16

            # Send initial burst of WOL packets — UDP is unreliable and the
            # NIC may miss a single packet, especially during power transitions.
            _LOG.info("[%s] Sending WOL packets to MAC: %s", self.name, mac_with_colons)
            for _ in range(3):
                self._send_wol_packet(magic_packet)

            _LOG.info("[%s] WOL packets sent, waiting for device...", self.name)

            # WOL timing: 12s initial delay + 6 retries at 5s intervals = 42s max
            await asyncio.sleep(const.WOL_INITIAL_DELAY)

            for attempt in range(1, const.WOL_MAX_RETRIES + 1):
                total_wait = const.WOL_INITIAL_DELAY + (attempt - 1) * const.WOL_RETRY_INTERVAL
                _LOG.info("[%s] WOL attempt %d/%d (elapsed: %ds)",
                          self.name, attempt, const.WOL_MAX_RETRIES, total_wait)

                if await self._is_device_reachable():
                    _LOG.info("[%s] Wake-on-LAN successful after %ds", self.name, total_wait)
                    return {"success": True}

                # Re-send WOL packet on each retry — the NIC may need
                # repeated nudges to wake from deep standby.
                self._send_wol_packet(magic_packet)
                await asyncio.sleep(const.WOL_RETRY_INTERVAL)

            total_wait = const.WOL_INITIAL_DELAY + const.WOL_MAX_RETRIES * const.WOL_RETRY_INTERVAL
            return {"success": False, "error": f"Device failed to wake after {total_wait}s"}

        except Exception as e:
            _LOG.error("[%s] Wake-on-LAN failed: %s", self.name, e, exc_info=True)
            return {"success": False, "error": str(e)}
