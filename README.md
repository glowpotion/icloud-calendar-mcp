# icloud-calendar-mcp

An MCP server that lets an agent read, publish, edit and delete events —
including repeating ones — on iCloud calendars.

Apple ships no public REST API for iCloud Calendar, so this speaks **CalDAV**
against `https://caldav.icloud.com` using the [`caldav`](https://pypi.org/project/caldav/)
and [`icalendar`](https://pypi.org/project/icalendar/) libraries.

## Tools

| Tool | Input | Output |
| --- | --- | --- |
| `list_calendars` | — | `calendars`: name + CalDAV URL for each calendar on the account |
| `create_event` | `calendar_name`, `event_title`, `start_time`, `end_time`, `description?`, `location?`, `recurrence?` | `status`, `event_id`, plus the normalised times actually written |
| `list_events` | `calendar_name`, `start_date?`, `end_date?` | `count` and `events[]` — each with `title`, `start_time`, `end_time`, `event_id`, `location`, `description`, `all_day`, and `recurrence_id` for occurrences of a series |
| `get_event` | `calendar_name`, `event_id` | The event; for a series also `recurrence`, `excluded_occurrences` and `modified_occurrences` |
| `update_event` | `calendar_name`, `event_id`, any of `event_title`, `start_time`, `end_time`, `description`, `location`, `recurrence`; `occurrence?` | The updated event and its `scope` (`event`, `series` or `occurrence`) |
| `delete_event` | `calendar_name`, `event_id`, `occurrence?`, `and_following?` | What was `deleted`: `event`, `series`, `occurrence` or `occurrence_and_following` |

`list_calendars` exists because the other tools need an exact calendar name and
iCloud names are whatever the user typed in the Calendar app. Name matching is
case-insensitive and falls back to a unique substring match; an ambiguous or
unknown name comes back as an error listing the real calendars.

## Setup

### 1. Generate an app-specific password

Go to [appleid.apple.com](https://appleid.apple.com) → **Sign-In and Security** →
**App-Specific Passwords**. You will get something shaped `abcd-efgh-ijkl-mnop`.

A normal Apple ID password **cannot** authenticate against iCloud CalDAV while
two-factor authentication is switched on, and it is switched on for every modern
Apple account. This is the single most common reason the tool fails to connect.

### 2. Install and configure

You need [uv](https://docs.astral.sh/uv/getting-started/installation/); it
fetches Python 3.12+ for you if necessary.

```bash
git clone https://github.com/frizzy/icloud-calendar-mcp.git
cd icloud-calendar-mcp
cp .env.example .env   # then fill it in
uv sync
```

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `ICLOUD_USERNAME` | yes | — | Full iCloud email address |
| `ICLOUD_APP_PASSWORD` | yes | — | App-specific password |
| `CALDAV_DEFAULT_TIMEZONE` | no | `UTC` | IANA name used for input times with no offset, and for rendering times back |
| `CALDAV_URL` | no | `https://caldav.icloud.com` | Override for a non-iCloud CalDAV server |

### 3. Verify the connection

```bash
set -a && source .env && set +a
uv run icloud-calendar-mcp --check
```

This prints the calendars it can see and exits, so credential problems surface
before you wire anything up:

```
OK: connected to https://caldav.icloud.com as you@icloud.com
Default timezone: Europe/London
3 calendar(s):
  - Home
  - Work
  - Family
```

### 4. Register with the agent

By default the server speaks MCP over stdio, run on the same machine as the
client (to host it on another machine, see [Running on a Raspberry Pi](#running-on-a-raspberry-pi)).
Replace `/path/to/icloud-calendar-mcp` below with the directory you cloned
into. For Claude Code:

```bash
claude mcp add icloud-calendar \
  --env ICLOUD_USERNAME=you@icloud.com \
  --env ICLOUD_APP_PASSWORD=abcd-efgh-ijkl-mnop \
  --env CALDAV_DEFAULT_TIMEZONE=Europe/London \
  -- uv --directory /path/to/icloud-calendar-mcp run icloud-calendar-mcp
```

Or, as raw config for any host that takes the standard `mcpServers` shape:

```json
{
  "mcpServers": {
    "icloud-calendar": {
      "command": "uv",
      "args": ["--directory", "/path/to/icloud-calendar-mcp", "run", "icloud-calendar-mcp"],
      "env": {
        "ICLOUD_USERNAME": "you@icloud.com",
        "ICLOUD_APP_PASSWORD": "abcd-efgh-ijkl-mnop",
        "CALDAV_DEFAULT_TIMEZONE": "Europe/London"
      }
    }
  }
}
```

## Running on a Raspberry Pi

The same server can run as an always-on service on a Pi (or any systemd Linux
box), speaking MCP's streamable-HTTP transport and guarded by a bearer token.
By default it listens on `127.0.0.1` only, for an agent harness running on the
Pi itself; `--lan` opens it to your home network or Tailscale instead.

**Requirements on the Pi:** 64-bit Raspberry Pi OS (Bookworm or later),
SSH access from your machine, `rsync`, and [uv](https://docs.astral.sh/uv/):

```bash
ssh pi@raspberrypi.local 'curl -LsSf https://astral.sh/uv/install.sh | sh'
```

**Deploy** from this directory on your machine:

```bash
deploy/deploy.sh pi@raspberrypi.local          # agent on the Pi itself
deploy/deploy.sh --lan pi@raspberrypi.local    # clients elsewhere on the network
```

That copies the project to `~/icloud-calendar-mcp` on the Pi (never `.git` or
`.env`) and runs `deploy/install.sh` there, which:

1. installs the dependencies into a local `.venv` with `uv sync --frozen`;
2. on the first run, creates `/etc/icloud-calendar-mcp.env` (root-only, mode
   600) from your local `.env` — sent once and then deleted on the Pi — or by
   prompting if you have none, and generates a random `MCP_AUTH_TOKEN`;
3. checks the iCloud credentials with `--check`;
4. installs and starts a hardened `icloud-calendar-mcp` systemd service on
   port 8765, and prints the URL and token to give your agent.

Run the same command again to deploy an update; the configuration is kept.
`--lan` only matters on the first install; to switch afterwards, change
`MCP_HOST` in the env file (`127.0.0.1` or `0.0.0.0`) and restart.

**Connect the agent.** Any MCP client that supports streamable HTTP needs two
things: the URL `http://127.0.0.1:8765/mcp` (or `http://raspberrypi.local:8765/mcp`
with `--lan`) and the header `Authorization: Bearer <token>`. For Claude Code:

```bash
claude mcp add --transport http icloud-calendar http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer <token>"
```

If your harness can only launch local commands, skip the service entirely and
have it run `~/icloud-calendar-mcp/.venv/bin/icloud-calendar-mcp` over stdio,
passing `ICLOUD_USERNAME`, `ICLOUD_APP_PASSWORD` and `CALDAV_DEFAULT_TIMEZONE`
in its environment.

For clients that only launch local commands (such as Claude Desktop's
`mcpServers` config), bridge with
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote):

```json
{
  "mcpServers": {
    "icloud-calendar": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://raspberrypi.local:8765/mcp", "--allow-http",
               "--header", "Authorization:Bearer <token>"]
    }
  }
}
```

**Day to day, on the Pi:**

| Task | Command |
| --- | --- |
| Logs | `sudo journalctl -u icloud-calendar-mcp -f` |
| Status | `systemctl status icloud-calendar-mcp` |
| Change settings or rotate the token | `sudo nano /etc/icloud-calendar-mcp.env && sudo systemctl restart icloud-calendar-mcp` |
| Health check (no token needed) | `curl http://127.0.0.1:8765/healthz` |
| Uninstall | `sudo systemctl disable --now icloud-calendar-mcp && sudo rm /etc/systemd/system/icloud-calendar-mcp.service /etc/icloud-calendar-mcp.env && rm -rf ~/icloud-calendar-mcp` |

**Security notes.** On the default localhost bind nothing off the Pi can
connect, and the token keeps other local processes out. With `--lan`, traffic
is plain HTTP, so keep it on a network you trust or on Tailscale (which
encrypts it); don't port-forward it to the internet. Anyone holding the token
can read and change your calendars. To listen only on
the tailnet, set `MCP_HOST` to the Pi's Tailscale IP. `MCP_ALLOWED_HOSTS`
optionally restricts accepted `Host` headers as a DNS-rebinding defence. The
server refuses to bind beyond loopback without a token of at least 32
characters.

You can also run HTTP mode by hand anywhere:

```bash
MCP_AUTH_TOKEN=$(openssl rand -hex 32) uv run icloud-calendar-mcp --transport http --host 0.0.0.0
```

## How times are handled

iCloud is strict about time values, so the rules are deliberately narrow:

- **Input** is ISO 8601. `2026-07-01T14:00:00+01:00` is taken at its offset;
  `2026-07-01T14:00:00` has no offset and is read in `CALDAV_DEFAULT_TIMEZONE`
  rather than being silently treated as UTC.
- **On the wire**, everything is converted to UTC and written as `...Z`. That
  avoids `TZID=` parameters, which would otherwise oblige us to ship a matching
  `VTIMEZONE` component or risk iCloud misplacing the event.
- **Output** is rendered back in `CALDAV_DEFAULT_TIMEZONE` with an explicit
  offset, so the caller never has to guess what a bare timestamp meant.
- **All-day events**: pass date-only values (`2026-08-03`) for *both*
  `start_time` and `end_time`. `end_time` is the last day the event covers —
  the exclusive `DTEND` that iCalendar requires is added and stripped for you.

`list_events` defaults to a now → now + 30 days window and expands recurring
events, so each occurrence in range comes back separately (sharing a `UID`,
distinguished by `recurrence_id`). Ranges beyond 400 days are refused rather
than asking the server to expand an unbounded number of recurrences.

## Editing and deleting

`update_event` only touches the fields you pass. An empty string clears
`description` or `location`. Moving only `start_time` keeps the duration; moving
only `end_time` keeps the start. Switching between all-day and timed needs both.
Edits are made to the stored iCalendar data in place, so alarms, attendees and
Apple's own `X-APPLE-*` properties survive, and `SEQUENCE` is bumped.

iCloud refuses CalDAV's search-by-UID (HTTP 412), so events are found by
fetching `<uid>.ics` directly — the name both iCloud and this server use — and,
failing that, by scanning the calendar.

## Recurring events

`recurrence` takes an iCalendar
[RRULE](https://icalendar.org/iCalendar-RFC-5545/3-8-5-3-recurrence-rule.html),
with or without the `RRULE:` prefix:

| Want | `recurrence` |
| --- | --- |
| Every Monday and Wednesday | `FREQ=WEEKLY;BYDAY=MO,WE` |
| Ten working days | `FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;COUNT=10` |
| Fortnightly | `FREQ=WEEKLY;INTERVAL=2` |
| First of every month until the end of 2027 | `FREQ=MONTHLY;BYMONTHDAY=1;UNTIL=20271231` |

- `start_time`/`end_time` describe the first occurrence, which must itself match
  the rule — a Tuesday start with `BYDAY=MO` is refused rather than left to
  each client's interpretation.
- A date-only `UNTIL` on a timed series means "through the end of that day" and
  is converted to the UTC form RFC 5545 requires.
- Repeating timed events are the exception to the UTC rule above: they are
  written as `TZID=` local times with a `VTIMEZONE`, so a 09:00 meeting stays at
  09:00 when the clocks change. Events already pinned to a timezone (anything
  made in Apple Calendar) keep theirs when edited.

To act on one occurrence, pass its `recurrence_id` from `list_events` as
`occurrence` (a date alone also works when only one occurrence falls on it):

- `update_event(..., occurrence=...)` changes just that occurrence.
- `delete_event(..., occurrence=...)` removes just that occurrence.
- `delete_event(..., occurrence=..., and_following=true)` ends the series
  before it.

Without `occurrence`, `update_event` changes the whole series and
`delete_event` removes it. When a series is moved, its individually changed and
deleted occurrences move with it; any that no longer land on the new rule are
dropped and counted in `removed_exceptions`. `recurrence=""` turns a series
back into a single event.

## Errors

Tool calls return `{"status": "error", "error": "..."}` rather than raising, so
the agent can read the problem and correct itself. Messages name the specific
cause — a missing env var, an unparseable timestamp, an unknown calendar (with
the available names listed), an unknown `event_id`, an occurrence that is not
part of the series, an RRULE that does not fit the start, a read-only calendar,
or rejected credentials.

### Warnings

A successful call can still carry a `warnings` list when something worked but
not quite as asked, or had a side effect the user should hear about:

- the event has attendees — this tool never sends invitations, updates or
  cancellations, and edits to someone else's meeting can be overwritten by the
  organizer;
- a fixed offset (`-04:00`) given for a repeating event was anchored to a named
  timezone, so occurrences follow that zone's daylight saving;
- editing a series discarded exceptions that no longer fit, or left
  occurrences with their own time, title, description or location unchanged
  (named by `recurrence_id`, so they can be updated individually);
- changing the location removed Apple's map pin;
- the server would not expand repeating events, so `list_events` shows each
  series once;
- the event is an invitation to a single occurrence, or uses `EXRULE`.

Not supported: "this and all following" edits (end the series with
`delete_event(..., and_following=true)` and create a new one), managing
attendees, and reminders/tasks.

## Development

```bash
uv run pytest
```

The suite covers calendar-name resolution, timezone conversion in both
directions, all-day round-tripping, range validation, the recurrence-expansion
fallback for servers that reject `expand`, event lookup, partial updates,
recurring series (DST, per-occurrence edits and deletes, truncation, moving a
series with its exceptions), and the MCP tool schemas. It uses a
fake CalDAV calendar, so nothing touches the network.

## Layout

```
src/icloud_calendar_mcp/
  server.py           MCP tool definitions and CLI entry point
  http_app.py         Streamable-HTTP transport with bearer-token auth
  calendar_client.py  CalDAV connection, calendar and event lookup, read/write/delete
  recurrence.py       RRULE validation and occurrence matching
  config.py           Environment configuration
  timeutil.py         ISO 8601 parsing and normalisation
deploy/
  deploy.sh           Copy to a Pi over SSH and install/update there
  install.sh          Install or update the systemd service (runs on the Pi)
  icloud-calendar-mcp.service  systemd unit template
```

`ICloudCalendarClient` is importable on its own if you ever want the CalDAV
logic without the MCP layer.

## License

[MIT](LICENSE)
