# CLAUDE.md: technical notes

Notes for contributors and AI coding assistants (Claude Code reads this file
automatically). Read it before changing `server.py`. User documentation is in
`README.md`.

## Design principles

- **Local API only.** The app talks to the Airzone gateway on the local
  network; no Airzone Cloud account. The Local API has no authentication or
  TLS, so the app must never encourage exposing it or the app to the
  internet: remote access is via a VPN (Tailscale, WireGuard…).
- **One file, Python standard library only** (`http.server`, `urllib`,
  `threading`, `json`). No dependencies, no build step. Keep it that way.
- Everything that must happen without a browser open (schedules, boost end,
  window pauses) runs in a **background thread on the server**.
- Fonts (Big Shoulders Display for numerals, Onest for UI, both OFL 1.1, from
  @fontsource) are **embedded as base64** in `FONTS` and served at
  `/fonts/<name>.woff2`, so the page works without internet access.
- The HVAC can be **reversible** (heating in winter, cooling in summer); any
  temperature logic must handle both directions.
- UI text is English, sentence case, plain words.

## Layout of `server.py`

config/constants → file helpers → Airzone client → temporary states
(boost/window) → boost → open-window detection → presets → schedules →
settings/backup → background loop → HTTP handler → `PAGE` (HTML/CSS/JS as one
raw string) → `FONTS` → `main()`.

## Airzone Local API (as used)

- Read: `POST {base}/hvac` with `{"systemID":0,"zoneID":0}` (all systems,
  response `{"systems":[{"data":[zones]}]}`); falls back to
  `{"systemID":1,"zoneID":0}` (response `{"data":[zones]}`).
- Write: `PUT {base}/hvac` with `{"systemID":s,"zoneID":z, <param>: value}`.
  Params used: `on` (0/1), `setpoint` (float), `mode` (int).
- Zone fields used: `systemID, zoneID, name, on, roomTemp, setpoint, minTemp,
  maxTemp, mode, modes, units (0 °C / 1 °F), humidity`.
- Modes: 1 Stop, 2 Cooling, 3 Heating, 4 Fan, 5 Dry, 7 Auto. The mode is
  system-wide and set on the main zone; only zones with a `modes` list get a
  mode selector.
- Zone key everywhere: `"systemID-zoneID"`, e.g. `"1-2"`.
- `base_url()` comes from settings (IP/port), falling back to the
  `AIRZONE_IP` / `AIRZONE_PORT` env vars. There is **no default IP**: with none
  set it raises `NO_ADDRESS`, which the page shows as a setup message.

### Request queue and spacing
- Gateways can drop a command that arrives while the previous one is still
  being applied. **All** requests go through `airzone_request()`, which holds
  `_api_lock` (one request at a time); after any PUT, the next request waits
  `WRITE_GAP` seconds (default 1.0, env var).
- All writes go through **`put_zone(system, zone, on=, setpoint=, mode=)`**,
  which sends **one parameter per PUT**: turning on → on, mode, setpoint;
  turning off (`on=0`) → mode, setpoint, then on. Never PUT several
  parameters in one body, and never call `airzone_request("PUT", …)` directly.
- A preset over N zones therefore takes about N seconds; the HTTP request
  waits for it.

### Remembered and offline zones
- `merge_known()` keeps every zone ever seen in `_known`, persisted to
  `zones.json` as `[{key, data, lastSeen}]` in first-seen order. Saved at once
  when a zone appears or drops out, otherwise every 5 minutes.
- A known zone missing from the gateway's answer is returned with its last
  data plus `offline: true` and `lastSeen` ("HH:MM", or "6 Nov HH:MM");
  `_offline` holds the offline keys.
- `list_zones(strict=True)` (used before acting) raises if the gateway is
  unreachable. `list_zones(strict=False)` (used by `/api/zones` and
  `/api/settings`) returns `(zones, error)`, with every remembered zone offline
  when the gateway is down; it raises only if nothing is remembered.
- `list_zones()` also applies display names (`zoneNames`), order
  (`zoneOrder`; unknown zones last) and `hidden` flags. Use it everywhere
  except the connection test.
- Offline handling: `apply_rule()` skips offline zones and returns their names
  (reported as "skipped, offline: …"); a single-zone schedule on an offline
  zone raises (logged as failed); `start_boost` refuses; `monitor_tick`
  ignores them; `check_expired` leaves a boost/pause on an offline zone until
  it reconnects, then restores it. `/api/zones/forget` only accepts offline
  zones.

## Data files (in `DATA_DIR`, `/data` in Docker)

- `schedules.json`: `[{id, enabled, time "HH:MM", days [0=Mon..6], zone
  ("all"|key), zoneName, action ("on"|"off"|"setpoint"|"preset"),
  setpoint|null, presetId?}]`
- `presets.json`: `[{id, name, rules:[{zone, zoneName, action, setpoint}]}]`;
  defaults comfort/night/away/alloff are created if the file is missing.
- `settings.json`: `{airzoneIp, airzonePort, zoneNames{key:alias},
  zoneOrder[keys], hiddenZones[keys], window{enabled, drop, minutes, pause,
  zones{key:bool}}}`, merged over `default_settings()`.
- `boosts.json` / `windows.json`: `{key: {name, until (epoch), restore{on,
  setpoint}, setpoint? (boost), drop? + direction? (window)}}`.
- `zones.json`: remembered zones (above).
- Writes are atomic (tmp + replace). Shared lock: `_lock` (RLock).
- Backup/restore (`/api/backup`) covers settings, presets and schedules only.

## Behaviour rules (keep consistent)

- `apply_rule()` (schedules and presets) ends any boost or window pause on the
  zones it touches, **without restoring**.
- A manual change (`update_zone`) of `on`/`setpoint` ends a window pause only;
  a boost continues and restores at its end.
- `current_restore()`: starting a boost or pause on a zone that already has one
  keeps the **original** restore state.
- Presets apply "all" rules first, then single-zone rules.
- **Hidden zones are display-only**: the page removes them from the Zones
  screen and from zone pickers (except where a rule already uses them); server
  logic and "All zones" actions still include them.
- Window detection (`monitor_tick`, every `SAMPLE_SECONDS`=60):
  `zone_direction()` returns "heat" (on + mode 3) or "cool" (on + mode 2);
  Auto, Fan, Dry, Stop and offline zones are not watched. History is in
  memory (`_history`, `_directions`), window = setting `minutes`. Change the
  wrong way: heat → max(history) − latest; cool → latest − min(history).
  History resets when the direction changes and on restart. Triggers when
  enabled, zone watched (default true), change ≥ threshold (setting key is
  named `drop` for compatibility) and not in `_cooldown` (15 min after a pause
  ends or is resumed).
- Background loop every `LOOP_SECONDS`=20: expire boosts and pauses, sample
  temperatures when due, run schedules once per matching minute (retried
  within the minute on failure).
- Activity log (`_activity`, last 30 entries, in memory) is shown in the
  Schedule tab.

## HTTP API

GET: `/` page, `/fonts/<name>.woff2`, `/api/zones` (zones, gatewayError,
boosts, windows), `/api/presets`, `/api/schedules` (+ activity, server time),
`/api/settings` (settings, zones, monitor readings, windows), `/api/backup`
(attachment).

POST: `/api/zone` (manual change), `/api/zones/forget`, `/api/presets`,
`/api/presets/delete`, `/api/presets/apply` (returns `skipped`),
`/api/boost {key, setpoint, minutes ∈ 30/60/120/180}`, `/api/boost/cancel`,
`/api/window/resume`, `/api/schedules`, `/api/schedules/delete`,
`/api/schedules/run`, `/api/settings` (partial update, validated by
`clean_settings`), `/api/settings/test`, `/api/backup/restore`.
Errors: 400 for validation, 502 for gateway problems, JSON `{"error": "..."}`.

## Front end (inside `PAGE`)

- Vanilla JS, four tabs (`zonesTab`, `presetsTab`, `schedTab`, `settingsTab`).
  Polls every 15 s for the visible tab; skips the Zones refresh while the user
  is adjusting a setpoint or the boost panel is open.
- Always escape server data with `esc()` when building HTML.
- Theme: an inline head script sets `html[data-mode]` (auto/light/dark, from
  `localStorage.mode`) and `html[data-scheme]` (light/dark, resolved with
  `prefers-color-scheme` when auto). CSS variables live on `:root` (dark) and
  `:root[data-scheme="light"]`. Wrap localStorage access in try/catch.
- Card state classes `heat`, `cool`, `off`, `boosted`, `paused`, `offline`
  drive the glow (`--glow`) and the state label colour.
- The glass look relies on `backdrop-filter`, with an `@supports not` fallback.
  Preset chips deliberately have no backdrop-filter (it draws a dark box inside
  the scrolling row).

## Testing without a real gateway

- Write a small **mock gateway** (a `ThreadingHTTPServer` on 127.0.0.1:3000)
  that answers `POST /api/v1/hvac` with a few zones (`systemID 0` →
  `{"systems":[{"data":[...]}]}`), applies `PUT` bodies to the matching zone,
  and offers test-only routes, e.g. to change a zone's `roomTemp`, make a zone
  disappear or return, and log PUT timestamps (to check `WRITE_GAP`).
- Start the app with fast timings:
  `AIRZONE_IP=127.0.0.1 SCHEDULE_FILE=/tmp/az/schedules.json SAMPLE_SECONDS=1
  LOOP_SECONDS=0.5 WRITE_GAP=0.05 python3 server.py`, then drive the API with
  curl. To test expiry, set `until` to 0 in `boosts.json` / `windows.json`.
- Check the page with Playwright (Chromium) at 390×844 in both
  `color_scheme="dark"` and `"light"`, collect `pageerror`s, and look at the
  screenshots.
- In shell test scripts, never use a bare `wait` while the mock or the app run
  in the background (it waits forever); wait on specific PIDs. Quote heredoc
  delimiters (`<<'EOF'`), or `$("id")` in the JS gets eaten by the shell.

## Ideas not built yet

Temperature history graphs (base for several others); installable PWA with
home-screen shortcuts; notifications (e.g. ntfy) for gateway offline, frost
risk, open window, failed schedule; holiday mode; per-zone temperature limits;
option to exclude hidden zones from "All zones"; undo after a preset; weekly
schedule grid; summer/winter values for presets and a seasonal heat/cool
autopilot; using Fan mode to even out temperatures between rooms; Dry instead
of Cool on humid days; per-room insulation report from overnight temperature
changes; electricity-tariff-aware pre-heating/pre-cooling; translations.
