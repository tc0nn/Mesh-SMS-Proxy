# Mesh-SMS-proxy

A two-service bridge that lets **Meshtastic** mesh users send **SMS-style text messages** to the public cellular network without a dedicated SMS modem. Messages leave the mesh through a small Flask backend that delivers them via each carrier’s **email-to-SMS gateway** (SMTP). The same backend powers optional **weather lookups** for mesh bot commands.

Built for the **Louisiana Mesh Community** ([discord.louisianamesh.org](https://discord.louisianamesh.org)).

---

## What problem it solves

Off-grid or disaster-style mesh networks are great for local coordination, but people outside the mesh still live on phone SMS. Mesh-SMS-proxy closes that gap in one direction (mesh → phone):

1. A user DMs the mesh bot with structured text (`bot: sms …`).
2. The bot forwards the request to a trusted server on the internet.
3. The server emails the recipient’s carrier SMS gateway address (e.g. `5551234567@vtext.com`).
4. The carrier delivers the body as a normal text message.

No Twilio account, no SIM in the server—only SMTP and knowledge of carrier gateway domains. That keeps the bar low for community operators who already have Gmail or another SMTP relay.

The design also records **who sent the message** (Meshtastic node ID) and optional **GPS** from the sender’s last known position, so downstream humans can see provenance in the SMS body.

---

## Architecture

```
┌─────────────────────┐     serial or TCP      ┌──────────────────────────────┐
│  Meshtastic radio   │◄──────────────────────►│ meshtastic-communication-  │
│  (field node)       │   meshtastic Python    │ service.py (MeshBot)         │
└─────────────────────┘   library pubsub       └──────────────┬───────────────┘
                                                              │ HTTP POST
                                                              │ :5000
                                                              ▼
                                               ┌──────────────────────────────┐
                                               │ email-message-service.py     │
                                               │ (Flask)                      │
                                               │  • /send-email  → SMTP/SMS   │
                                               │  • /get-weather → OpenWeather│
                                               └──────────────┬───────────────┘
                                                              │ SMTP TLS
                                                              ▼
                                               ┌──────────────────────────────┐
                                               │ Carrier email-to-SMS       │
                                               │ (AT&T, Verizon, T-Mobile, …) │
                                               └──────────────────────────────┘
```

| Component | Role |
|-----------|------|
| **`meshtastic-communication-service.py`** | Long-running mesh client. Subscribes to `meshtastic.receive.text`, parses commands, sends DMs or channel broadcasts, calls the email service over HTTP. |
| **`email-message-service.py`** | Internet-facing (or LAN) Flask app. Sends mail, resolves weather, and exposes stub hooks for Meshtastic/MeshCore IP registration. |

The mesh bot defaults to **USB serial** (`USE_SERIAL = True`). Set `USE_SERIAL = False` and configure `TCP_HOST` to talk to a Meshtastic node over TCP instead.

---

## Features

### SMS proxy (DM only)

SMS commands are accepted **only in direct messages** to the bot. Broadcast attempts get a reminder to use DMs.

**Help**

```
bot: sms help
```

**Send format** (comma-separated fields, note the double comma `,,` between fields):

```
bot: sms <phone>,, <yes|no location>,, <carrier>,, <message text>
```

| Field | Description |
|-------|-------------|
| Phone | Destination number (digits as you would dial; no formatting enforced in code). |
| Location | `yes` — attach sender’s last known Meshtastic position if cached; `no` — omit coordinates. |
| Carrier | One of the supported providers (see below). |
| Message | Free text; included in the SMS body with metadata wrapper. |

**Supported carriers** (email gateway mapping in `email-message-service.py`):

| Provider (user input) | Email gateway pattern |
|----------------------|------------------------|
| `at&t` | `{number}@txt.att.net` |
| `verizon` | `{number}@vtext.com` |
| `t-mobile` / `tmobile` | `{number}@tmomail.net` |
| `google-fi` | `{number}@msg.fi.google.com` |
| `consumer-cellular` / `consumercellular` | `{number}@mailmymobile.net` |

The outbound SMS body is prefixed with Louisiana Mesh branding, **device ID**, **mesh type** (`moc`: `0` = Meshtastic, `1` = MeshCore reserved), and **GPS** when available.

### Weather (DM or broadcast)

```
bot: weather              # uses sender’s cached mesh position
bot: weather 70112        # uses US zip code → geocode → OpenWeather
bot: weather help
```

Temperature is converted from Kelvin to °F in the mesh bot before reply. Weather data is fetched by the email service (`/get-weather`) using an OpenWeather API key from config.

### Community / utility commands

Available in **DM** and on the **primary channel** (except SMS, DM-only):

| Command | Behavior |
|---------|----------|
| `bot: help` | Multi-part help text listing commands. |
| `bot: status` | “Online and listening”. |
| `bot: discord` | Discord invite link. |
| `bot: kofi` | Ko-fi shop link. |
| `ping` | `pong` |
| `:3` | `:3` |

---

## Configuration

### Email / weather service

Copy the example config and fill in secrets (never commit `config.json`—it is gitignored):

```bash
cp config_example.json config.json
```

| Key | Purpose |
|-----|---------|
| `smtp_server` | SMTP host (example uses `smtp.gmail.com`). |
| `smtp_port` | Typically `587` with STARTTLS. |
| `smtp_username` | SMTP login / From address. |
| `smtp_password` | App password or SMTP credential. |
| `openweather_api_key` | OpenWeather API key for `/get-weather`. |

Run the Flask service:

```bash
python3 email-message-service.py
```

Default: Flask debug mode on port **5000**.

### Mesh bot

Edit constants at the top of `meshtastic-communication-service.py`:

| Setting | Default | Meaning |
|---------|---------|---------|
| `USE_SERIAL` | `True` | USB serial to local radio; `False` for TCP. |
| `TCP_HOST` | `localhost` | Meshtastic TCP API host when serial is off. |
| `emailmessageservice_host` | `localhost` | Where Flask email service runs. |
| `emailmessageservice_port` | `5000` | Flask port. |

Install Python dependencies (minimum):

```bash
pip install meshtastic pubsub requests flask
```

Run the mesh bot (after the email service is up):

```bash
python3 meshtastic-communication-service.py
```

On start it prints the connected node long name and node ID, then blocks listening for packets.

---

## HTTP API (email service)

| Method | Path | Body | Response |
|--------|------|------|----------|
| `GET` | `/` | — | Plain-text status line. |
| `POST` | `/send-email` | `phone_number`, `message`, `device_id`, `gps_x`, `gps_y`, `moc`, `celluar_provider` | `200` on SMTP success. |
| `POST` | `/get-weather` | `zipcode` (0 = use GPS), `gps_x`, `gps_y` | OpenWeather JSON or error. |
| `POST` | `/connect-meshtastic` | `meshtastic_ip` | Registers client IP (global stub for future use). |
| `POST` | `/connect-meshcore` | `meshcore_ip` | Registers MeshCore client IP (stub; `moc=1` path not wired in mesh bot yet). |

The mesh bot only uses `/send-email` and `/get-weather` today.

---

## Location handling

For weather and optional SMS location:

1. The bot looks up `interface.nodes[sender_id]` for a cached `position` (`latitudeI` / `longitudeI` in 1e-7 degree integers).
2. If missing, it may call `interface.sendPosition(destinationId=sender_id)` to request an update (weather path); SMS may proceed with `0,0` if nothing is cached yet.

Operators should expect **best-effort** GPS—not real-time tracking unless nodes recently reported position.

---

## Security and operations notes

- **One-way bridge:** Mesh → SMS only; replies from the phone do not return to mesh in this codebase.
- **Trust boundary:** Anyone who can DM the bot can attempt to send SMS through your SMTP account. Deploy the mesh bot on a trusted node; restrict Flask to a private network or add authentication (not present today).
- **SMTP abuse:** Rate limiting, allowlists, and logging should be added for production; the reference implementation has none.
- **Secrets:** Keep `config.json` off git; use app-specific SMTP passwords.
- **Carrier limits:** Email-to-SMS gateways often truncate long messages and may delay delivery; behavior varies by carrier.
- **Work in progress:** Help text in the bot states commands are still evolving; report issues in Discord.

---

## MeshCore note

The email service accepts `moc` (`0` = Meshtastic, `1` = MeshCore) and exposes `/connect-meshcore`, but **`meshtastic-communication-service.py` is Meshtastic-only** today. A MeshCore-facing client would mirror the HTTP calls to `/send-email` with `moc: "1"`.

---

## Project status

This repository is an **early community tool**, not a polished product:

- README and packaging are minimal; configuration is mostly inline constants plus `config.json`.
- Some code paths contain typos (`elf.send_dm`, `tine.sleep`) that will break specific branches until fixed.
- Zip-based weather geocoding uses OpenWeather’s geo API with a numeric zip as `q=`—behavior may be imperfect outside supported regions.
- Flask runs with `debug=True` in the entrypoint—disable for production.

Despite that, the **core idea is clear**: a lightweight mesh command bot plus an SMTP shim to reach phones when the internet is available at the gateway.

---

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).

---

## Community

**Louisiana Mesh Community**

- Discord: [discord.louisianamesh.org](https://discord.louisianamesh.org)
- Support / stickers: [ko-fi.com/louisianameshcommunity](https://ko-fi.com/louisianameshcommunity/shop)

Built by Lenley Ngo for LMesh (attributed in the email service status string).
