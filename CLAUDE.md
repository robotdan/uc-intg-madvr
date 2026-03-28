# CLAUDE.md

## Working Together

1. **Challenge assumptions.** Do not agree with me to be agreeable. Push back when something seems wrong. The goal is high-quality, accurate code — not consensus.
2. **Review everything.** Always review code and plans to ensure they solve the actual problem. Code should be idiomatic, clean, and well documented.
3. **Research, don't guess.** If you don't understand something — a protocol, an API, a behavior — research it. Check docs, source code, or the web. Never guess and present it as fact.

## Project Overview

A custom integration driver for the **madVR Envy** video processor, built for the **Unfolded Circle Remote** (UC Remote). The integration communicates with the Envy over TCP and exposes entities to the UC Remote's UI.

For UC Remote lifecycle events, entity types, and integration API details, see:
- https://github.com/unfoldedcircle/core-api/blob/main/doc/integration-driver/write-integration-driver.md
- https://github.com/unfoldedcircle/core-api/blob/main/doc/entities/README.md

## Key Design Decisions

**Two TCP connections, not one.** The Envy sends push notifications on all open connections. A single connection would interleave notifications with command responses, making response parsing unreliable. The listener connection receives notifications; the command connection sends commands and discards any interleaved notifications using `RESPONSE_PREFIX` matching.

**On wake, all state is stale.** The UC Remote suspends the integration process during sleep. `suspend()`/`resume()` tear down and rebuild connections; cached state cannot be trusted until re-synced.

**"Standby" is a toggle.** The madVR protocol uses the same `Standby` command for both ON and OFF. The `power_intent` parameter on `send_command` disambiguates intent so reactive recovery can re-route correctly (WOL for on, success for off) when the device is unreachable but state says ON.

## madVR Protocol Reference

- **Commands:** `"{command}\r\n"` — e.g., `Standby\r\n`, `GetTemperatures\r\n`
- **Success response:** `OK`
- **Error response:** `ERROR "{message}"`
- **Query responses:** Prefixed with the data type (e.g., `Temperatures 45 38 42 35`)
- **No discovery protocol** — IP must be provided manually during setup
- **Official spec:** https://madvrenvy.com/wp-content/uploads/EnvyIpControl.pdf
