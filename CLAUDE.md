# CLAUDE.md

## Working Together

1. **Challenge assumptions.** Do not agree with me to be agreeable. Push back when something seems wrong. The goal is high-quality, accurate code — not consensus.
2. **Review everything.** Always review code and plans to ensure they solve the actual problem. Code should be idiomatic, clean, and well documented.
3. **Research, don't guess.** If you don't understand something — a protocol, an API, a behavior — research it. Check docs, source code, or the web. Never guess and present it as fact.

## Project Overview

A custom integration driver for the **madVR Envy** video processor, built for the **Unfolded Circle Remote** (UC Remote). The integration communicates with the Envy over TCP and exposes entities to the UC Remote's UI.

- **Language:** Python 3.11+
- **Async framework:** asyncio
- **UC API library:** `ucapi>=0.5.2`
- **Entry point:** `uc_intg_madvr/driver.py`
- **Config storage:** `$UC_CONFIG_HOME/madvr/madvr_config.json`

## Architecture

### Dual-Connection TCP Model

The Envy listens on TCP port 44077 (up to 16 concurrent connections). This integration maintains two connections:

- **Listener connection** (persistent) — Receives push notifications. Kept alive with heartbeats every 20s. Reconnects with exponential backoff (5s, 10s, 30s, 60s).
- **Command connection** (lazy) — Opened on demand for sending commands. Closes after 30s idle. Must discard interleaved push notifications when reading command responses.

Both connections validate a `WELCOME to Envy` banner on connect.

### UC Remote Lifecycle

The integration handles these UC API events:

| Event | Handler | Behavior |
|---|---|---|
| `CONNECT` | `on_connect()` | Reload config, initialize entities, trigger auto-recovery |
| `DISCONNECT` | `on_disconnect()` | Log only; device keeps running |
| `ENTER_STANDBY` | `on_enter_standby()` | Cancel background tasks, close connections, preserve device state |
| `EXIT_STANDBY` | `on_exit_standby()` | Recreate background tasks, reset backoff, reconnect |
| `SUBSCRIBE_ENTITIES` | `on_subscribe_entities()` | Push current cached state to subscribed entities |

The integration process runs on the UC Remote and is suspended when the Remote sleeps. On wake, all state should be considered potentially stale. The `suspend()`/`resume()` methods handle this lifecycle.

### Entities

| Entity | ID Pattern | Features |
|---|---|---|
| Media Player | `media_player.{host}` | ON_OFF |
| Remote | `remote.{host}` | ON_OFF, SEND_CMD (60+ commands, 7 UI pages) |
| Signal Sensor | `sensor.{host}.signal` | Current input signal info |
| Temperature Sensors (4) | `sensor.{host}.temp_{gpu,hdmi,cpu,mainboard}` | Device temperatures in C |
| Aspect Ratio Sensor | `sensor.{host}.aspect_ratio` | Detected aspect ratio |
| Masking Ratio Sensor | `sensor.{host}.masking_ratio` | Active masking ratio |
| Aspect Ratio Select | `select.{host}.aspect_ratio_mode` | Set aspect ratio mode |

### Push vs Poll

**Pushed by the madVR** (received on listener connection in real-time):
- Power state changes (PowerOff, Standby, Restart, ReloadSoftware)
- Signal info (IncomingSignalInfo, OutgoingSignalInfo, NoSignal)
- Aspect ratio and masking ratio changes
- Profile activation

**Must be polled:**
- Temperatures (GPU, HDMI, CPU, Mainboard) — the Envy does not push temperature updates. The `Temperatures` line format is shared between query responses and notifications, so the parser handles it for both command responses and (defensive) notification filtering on the command connection.

Polling modes: `enabled` (interval-based, default 60s), `on_demand` (query when UI requests), `disabled`.

### Wake-on-LAN

The Envy supports WOL from STANDBY or OFF states. The integration:
1. Sends a magic packet to the stored MAC address (UDP port 9)
2. Waits up to 42s (12s initial + 6 retries at 5s intervals)
3. Background tasks detect the device coming online and reconnect

### Stale State Recovery

Two mechanisms prevent stale state from causing errors:

1. **Standby lifecycle** — `suspend()` / `resume()` cleanly tear down and rebuild connections when the Remote sleeps/wakes.
2. **Reactive recovery** — If `send_command` fails because the device is unreachable but state says ON, it corrects to STANDBY and re-routes power commands (WOL for on, success for off).

The `power_intent` parameter on `send_command` disambiguates ON vs OFF since the madVR protocol uses "Standby" as a toggle for both.

## madVR Protocol Reference

- **Port:** 44077 (TCP)
- **Commands:** `"{command}\r\n"` — e.g., `Standby\r\n`, `GetTemperatures\r\n`
- **Success response:** `OK`
- **Error response:** `ERROR "{message}"`
- **Query responses:** Prefixed with the data type (e.g., `Temperatures 45 38 42 35`)
- **No discovery protocol** — IP must be provided manually during setup
- **Official spec:** [EnvyIpControl.pdf](https://madvrenvy.com/wp-content/uploads/EnvyIpControl.pdf)
