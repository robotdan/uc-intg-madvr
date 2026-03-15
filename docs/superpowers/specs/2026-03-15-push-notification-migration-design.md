# Push Notification Migration Design

Migrate the madVR Envy integration from a polling-based sync strategy to push-based notifications, improving battery life, reducing latency, and aligning with the official madVR IP control protocol.

## References

- **madVR Envy IP Control Specification v1.1.3** — https://madvrenvy.com/wp-content/uploads/EnvyIpControl.pdf?r=113a
  - Defines the TCP protocol on port 44077, push notification types, heartbeat requirements (every 20s, 60s timeout), and all command/response formats.
- **py-madvr** (Home Assistant library) — https://github.com/iloveicedgreentea/py-madvr
  - Production-proven Python implementation using dual connections (listener + command pool), push notifications, and a `NotificationProcessor` class. Classified as "Local Push" by Home Assistant. Used as the primary architectural reference.
- **madVR Envy Home Assistant Integration** — https://www.home-assistant.io/integrations/madvr/
- **Control4 Envy Driver** — https://madvrenvy.com/wp-content/uploads/Envy-Control4.pdf
  - Official driver using persistent TCP connection with push events. Confirms the event-driven approach is the intended integration pattern.
- **RTI Envy Driver** — https://driverstore.rticontrol.com/driver/daniel-richer-madvr-envy
  - Another official driver using the same push-based TCP architecture.

## Problem

The current integration polls the madVR Envy every 10 seconds, sending 5 queries per cycle (`Heartbeat`, `GetIncomingSignalInfo`, `GetTemperatures`, `GetAspectRatio`, `GetMaskingRatio`). This approach:

- Wastes battery on the Unfolded Circle remote (unnecessary network activity every 10s)
- Introduces up to 10 seconds of latency for state changes
- Creates unnecessary load on the Envy (answering repeated queries)
- Ignores the device's native push notification capability

The madVR Envy IP control spec states: "As long as you stay connected, Envy will automatically notify you about any events." All professional integrations (Control4, Savant, RTI, Crestron) and the Home Assistant integration use this push-based approach.

## Connection Architecture

### Listener Connection (Persistent)

Dedicated TCP connection for receiving push notifications. Managed by two async tasks:

- **Notification listener task**: Continuous read loop using `readuntil(b'\r\n')` (or equivalent buffered readline) to ensure proper message framing. Each complete line is passed through `NotificationProcessor`, and state updates are emitted via PyEE.
- **Listener heartbeat task**: Sends `Heartbeat\r\n` every 20 seconds to keep the connection alive (the madVR spec closes connections after 60 seconds of inactivity; the py-madvr reference uses 30s intervals, but 20s provides more margin). This task is the sole owner of listener connection lifecycle — it establishes, monitors, and re-establishes the connection.

Both connections must validate the welcome message on connect. The Envy sends `WELCOME to Envy v{version}` immediately upon connection. If the welcome message is missing or malformed, the connection should be closed and retried — this prevents connecting to the wrong service.

This connection is the primary source of truth for device state.

### Command Connection (Lazy)

Used for user-initiated commands (remote buttons, aspect ratio changes, power) and optional polling queries. Unlike the listener connection, the command connection is **lazy**: it connects on first command, and closes after an idle timeout (e.g., 30 seconds of no commands). This avoids holding a TCP socket open 24/7 when no commands are being sent, which is important for battery life on the UC remote.

This matches the py-madvr reference implementation, which uses a connection pool with idle-timeout-based cleanup rather than a permanent heartbeat.

Uses the existing `asyncio.Lock` for command serialization.

## Push Notifications

The Envy pushes notifications over an open TCP connection. Each notification is a line terminated by `\r\n`, formatted as `Title param1 param2 ...\r\n`.

### Message Framing

TCP is a stream protocol — a single `read()` may return a partial line, or multiple lines concatenated together. The notification listener must use `readuntil(b'\r\n')` (or `readline()`) on the `asyncio.StreamReader` to ensure each notification is processed as a complete, properly-delimited line. This is critical for correctness and avoids the silent corruption that naive `read()` + `split('\r\n')` causes with partial messages.

### Handled Notifications

| Notification | Format | Maps To |
|---|---|---|
| `IncomingSignalInfo` | `IncomingSignalInfo {res} {framerate} {2D/3D} {colorspace} {bitdepth} {HDR} {colorimetry} {blacklevels} {aspectratio}` | Signal sensor, media player title, power state ON |
| `OutgoingSignalInfo` | `OutgoingSignalInfo {res} {framerate} {2D/3D} {colorspace} {bitdepth} {HDR} {colorimetry} {blacklevels}` | Stored for future use |
| `AspectRatio` | `AspectRatio {res} {decimal} {int} {name}` | Aspect ratio sensor, select entity |
| `MaskingRatio` | `MaskingRatio {res} {decimal} {int}` | Masking ratio sensor |
| `PowerOff` | `PowerOff` | Power state OFF, triggers connection teardown (see below) |
| `Standby` | `Standby` | Power state STANDBY, triggers connection teardown (see below) |
| `NoSignal` | `NoSignal` | Clear signal info |
| `Temperatures` | `Temperatures {gpu} {hdmi} {cpu} {mainboard}` | Temperature sensors (response to explicit query) |
| `ActivateProfile` / `ActiveProfile` | `ActivateProfile {name} {num}` | Logged, stored for future use |
| `OK` | `OK` | Ignored (command acknowledgment) |

Unknown notification types are logged at debug level and ignored. This future-proofs against new notification types added in firmware updates.

### Temperature Sensor Field Mapping

The madVR Envy sends temperatures in the order: GPU, HDMI, CPU, Mainboard. The current codebase labels these as GPU, CPU, Board, PSU — which is incorrect per the protocol and the py-madvr reference implementation. The `NotificationProcessor` will use the correct field names from the protocol. This means `sensor.py` and `driver.py` require a minor update to fix the sensor labels and IDs:

| Position | Envy Protocol | Current (Incorrect) | New (Correct) |
|---|---|---|---|
| 0 | GPU | GPU | GPU (unchanged) |
| 1 | HDMI | CPU | HDMI |
| 2 | CPU | Board | CPU |
| 3 | Mainboard | PSU | Mainboard |

### Notifications Not Handled (Known From Spec)

These exist in the spec but are not needed for current UC entities. They will appear in debug logs if received:

- Menu opened/closed events
- Remote button press events
- Restart / ReloadSoftware notifications
- ResetTemporary

These can be added incrementally if needed.

## PowerOff / Standby Connection Teardown

When a `PowerOff` or `Standby` notification is received, the Envy is shutting down its TCP server. Following the py-madvr reference implementation's proven approach:

1. Close the listener connection immediately
2. Close the command connection (if open)
3. Clear all device attribute state (preserve MAC address)
4. Mark all entities as unavailable
5. Set device power state to OFF or STANDBY
6. All background tasks enter a sleep/wait state

Recovery from this state is handled by the ping task (see below), which detects when the device becomes connectable again.

### Power-Off Hysteresis

After a PowerOff/Standby notification, there is a brief window where the Envy may still be connectable as it shuts down. To prevent the ping task from incorrectly marking the device as online during this window, a hysteresis period of 30 seconds is applied. The ping task will not mark the device as online until at least 30 seconds after the last PowerOff/Standby event. (The py-madvr reference uses 60 seconds, but 30 seconds is more appropriate for a battery-powered remote where responsiveness during activity switching matters.)

## Polling Configuration

For data not covered by push notifications (currently temperatures), a configurable polling mechanism is retained. The polling loop is generic — today it queries temperatures, but additional non-pushed queries can be added in the future without changing the config schema.

### Config Fields

Added to `madvr_config.json`:

```json
{
  "host": "192.168.1.100",
  "port": 44077,
  "name": "madVR Envy",
  "mac_address": "AA:BB:CC:DD:EE:FF",
  "polling_mode": "enabled",
  "polling_interval": 60
}
```

### Config Migration

Existing users upgrading from the polling-based version will have a `madvr_config.json` without the new fields. When `config.py` loads a config file missing `polling_mode` or `polling_interval`, it defaults to `"enabled"` and `60` respectively. This ensures no user-visible behavior change on upgrade — the integration continues to provide temperature data at a reasonable interval.

### Polling Modes

| Mode | Behavior | Battery Impact |
|---|---|---|
| `enabled` (default) | Queries non-pushed data every `polling_interval` seconds | Low (one query per interval) |
| `on_demand` | One-shot query when user subscribes to relevant sensor entities (see below) | Minimal |
| `disabled` | Never queries. Non-pushed sensors show unavailable | None |

### On-Demand Mode Behavior

In `on_demand` mode, a single query is fired when the UC remote subscribes to temperature sensor entities (via the existing `on_subscribe_entities` event in `driver.py`). This is a one-shot query — it returns the current temperature values but does not continue polling. The UC API does not provide an unsubscribe event, so continuous polling during subscription is not feasible. The user sees the temperature values as of the moment they navigated to the sensor screen; the values do not update until they navigate away and back.

### Polling Interval

- Only used when `polling_mode` is `enabled`
- Default: 60 seconds
- Minimum: 10 seconds
- Integer, in seconds

### Setup Integration

- `polling_mode` and `polling_interval` are added to the setup wizard in `setup.py` and the schema in `driver.json`
- UI copy explains this only affects state not received through push events (currently temperature), and that disabling improves battery life at the cost of losing temperature data. On-demand means temperature is only available when actively viewing it.
- Existing config values are preserved when re-running setup (setup only writes host/port/name; polling config is set separately via `set_polling_config()`)
- Config can be changed without re-setup by editing `madvr_config.json` directly; changes are picked up on next reconnect via `reload_from_disk()`

## Reconnection Strategy

### Listener Connection (Backoff)

1. On disconnect, attempt immediate reconnect
2. If that fails, retry with increasing delays: 5s, 10s, 30s, 60s
3. Continue retrying every 60s for up to 30 minutes total
4. After 30 minutes, stop retrying and mark entities as unavailable/error in UC
5. Auto-recovery triggers (reset backoff and retry):
   - User sends a command (presses a button on the remote)
   - UC reconnects to the integration (remote wakes from sleep)
6. On successful reconnect, do a one-time full state query via the **command connection** to sync up missed notifications: `GetIncomingSignalInfo`, `GetOutgoingSignalInfo`, `GetAspectRatio`, `GetMaskingRatio`, and `GetTemperatures` (if polling is not disabled). These sync queries use the command connection (not the listener connection) to keep the listener's concurrency model simple.

### Command Connection

- Lazy: connects on first command, closes after idle timeout (~30 seconds)
- If connection fails during a command, the user gets immediate feedback (command error)
- No background retry needed since commands are user-initiated

### Heartbeats

- Listener connection: `Heartbeat\r\n` every 20 seconds, sent by the listener heartbeat task
- Command connection: no dedicated heartbeat (lazy connection model — idle connections close naturally)
- Heartbeat failure on the listener connection triggers the backoff sequence above

### Ping Task (Out-of-Band Power-On Detection)

A low-frequency ping task attempts a TCP connection to the Envy every 30 seconds to detect if the device has been powered on by something other than the UC remote (physical remote, HDMI CEC, another integration, etc.). This is important because in a home theater setup, the Envy is frequently controlled by multiple systems.

- When the device is detected as connectable (and hysteresis window has passed), the listener heartbeat task is signaled to establish the listener connection
- When the device is detected as unreachable and was previously online, entities are marked unavailable
- The ping task runs continuously, even after the 30-minute backoff expires, since it is very lightweight (a single TCP connect/close)

## Async Task Structure

Background tasks created when the integration subscribes to entities:

| Task | Purpose | Runs When |
|---|---|---|
| `_notification_listener_task` | Reads complete lines from listener connection via `readuntil(b'\r\n')`, parses notifications, emits PyEE events | Always (waits for listener connection to be established; sleeps when device is off) |
| `_listener_heartbeat_task` | Sends heartbeat every 20s on listener connection. Sole owner of listener connection lifecycle (establish, re-establish with backoff) | Always |
| `_ping_task` | Attempts TCP connection every 30s to detect device availability. Handles power-off hysteresis. | Always (lightweight) |
| `_poll_task` | Sends `GetTemperatures` (and future non-pushed queries) every `polling_interval` seconds via command connection. Reads and parses responses directly from the command connection (responses do not flow through the notification listener). | Only when `polling_mode` is `enabled` |

### Task Lifecycle

- All tasks start when entities are subscribed
- All tasks are cancelled on driver shutdown
- `_notification_listener_task` and `_poll_task` sleep when device is off (detected via PowerOff/Standby notification) OR when the listener connection is not established (two separate conditions — device can be on but connection lost due to network issue)
- `_ping_task` runs continuously regardless of device state (its purpose is to detect state changes)
- `_listener_heartbeat_task` runs continuously but skips heartbeat sends when device is off (focuses on reconnection when device comes back)
- Tasks resume normal operation when device comes back online (detected by ping task) and listener connection is re-established (by heartbeat task)

### Concurrency

- Command connection uses `asyncio.Lock` to serialize commands (unchanged)
- Listener connection: the notification listener task reads; the heartbeat task writes heartbeats. Both are async (single-threaded), so no lock is needed. Post-reconnect sync queries go through the command connection, not the listener.
- Disconnect signaling: when the notification listener detects a broken connection (empty read, `ConnectionResetError`, `IncompleteReadError`), it clears a `notification_connected` event flag and sets reader/writer to `None`. The heartbeat task checks this flag on each iteration and re-establishes the connection when it is cleared.

## Files Modified

| File | Changes |
|---|---|
| `device.py` | Major refactor: add listener connection with `readuntil(b'\r\n')` read loop, listener heartbeat task with backoff, lazy command connection with idle timeout, ping task, power-off hysteresis, connection teardown on PowerOff/Standby. Replace poll loop with conditional poll task. Add `query_on_demand()` method. |
| `const.py` | Add `HEARTBEAT_INTERVAL` (20s), `DEFAULT_POLL_INTERVAL` (60s), `MIN_POLL_INTERVAL` (10s), `COMMAND_IDLE_TIMEOUT` (30s), `PING_INTERVAL` (30s), `POWER_OFF_HYSTERESIS` (30s), backoff timing constants (`BACKOFF_DELAYS = [5, 10, 30, 60]`), `MAX_RETRY_DURATION` (1800s/30min), `CMD_GET_OUTGOING_SIGNAL_INFO`. Remove old `POLL_INTERVAL` (10s). Note: the existing `HEARTBEAT_INTERVAL = 20.0` constant already exists but is unused by the current polling code; it will now be used by the listener heartbeat task. |
| `config.py` | Add `polling_mode` and `polling_interval` properties with defaults (`"enabled"` and `60`), `set_polling_config()` method. Handle missing fields gracefully for config migration. Update `set_config()` to preserve `polling_mode` and `polling_interval` when writing — the current implementation rebuilds the config dict from scratch, which would destroy these fields. |
| `setup.py` | Add polling mode and interval fields to setup wizard with explanatory UI copy. |
| `driver.json` | Add `polling_mode` (dropdown: enabled/on_demand/disabled) and `polling_interval` (number) to setup schema. |
| `driver.py` | Pass polling config to device on init. Handle on-demand temperature query on entity subscription. Wire up auto-recovery reconnect on UC connect event. |
| `sensor.py` | Fix temperature sensor labels and IDs: HDMI (was CPU), CPU (was Board), Mainboard (was PSU). GPU unchanged. |

## New File

| File | Purpose |
|---|---|
| `notifications.py` | `NotificationProcessor` class: parses incoming notification lines, routes by title, returns structured dict. Handles `ActivateProfile` and `ActiveProfile` as variants of the same notification. Keeps `device.py` focused on connection management. |

## Unchanged Files

| File | Reason |
|---|---|
| `select.py` | Aspect ratio updates arrive via events as before |
| `media_player.py` | Power/signal state via events as before |
| `remote.py` | Command sending interface unchanged |
