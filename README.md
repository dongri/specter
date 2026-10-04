# Specter

Forward Slack notifications recorded on macOS to Discord through an incoming webhook.

Specter runs in your terminal using the Python standard library. It reads the macOS notification database in read-only mode; no custom Slack app or Slack API token is required.

```text
Slack -> macOS notification database -> Specter -> Discord
```

## Requirements

- macOS and Python 3.10 or later.
- The Slack desktop app, signed in and generating macOS notifications.
- A Discord incoming webhook.
- Access to a compatible macOS notification database.

The database format is undocumented and may change. Specter reads `app` and `record` tables and the `req.titl`, `req.subt`, and `req.body` fields in property-list data. Use `doctor` to check compatibility on your Mac.

## Setup

Run these commands from the repository directory.

### 1. Enable Slack notifications

Enable desktop notifications and message previews in Slack, and allow Slack notifications in macOS settings. Choose the activity you want Slack to notify you about. For example, use mentions and DMs as the default and enable all-message notifications for selected channels.

Specter does not independently detect Slack mentions or channel activity. It reads notification records for the configured application bundle identifiers.

### 2. Configure the Discord webhook

Create an incoming webhook for a Discord text channel. Then create and edit the local configuration:

```sh
python3 specter.py init
open -e ./config.json
```

Set `webhook_url` to your webhook URL. If `config.json` already exists, skip `init`; it refuses to overwrite an existing file.

`init` creates the configuration with owner-only read/write permissions. `config.json` is excluded from Git. Keep the webhook URL private.

### 3. Check notification access

Receive a Slack notification, then run:

```sh
python3 specter.py doctor
```

This prints the database path, matching record count, Python executable path, and decodable record count. Zero records does not confirm that notification capture works. If records exist but none can be decoded, the command reports an error.

If access is denied, check Full Disk Access permissions for your terminal and Python executable in macOS settings. Restart the terminal after changing permissions. To find the Python executable:

```sh
python3 -c 'import pathlib, sys; print(pathlib.Path(sys.executable).resolve())'
```

### 4. Test and run

Send a fixed test message to Discord, then start forwarding:

```sh
python3 specter.py test
python3 specter.py run
```

After `Skipped existing notifications` appears, receive a new Slack notification and check Discord. Logs appear in the terminal. Press **Ctrl+C** to stop, and keep the Mac awake while forwarding.

## Configuration

The default file is `config.json` next to `specter.py`, regardless of the working directory. [config.example.json](config.example.json) lists all supported options. Unknown keys are rejected.

| Option | Default | Description |
| --- | --- | --- |
| `webhook_url` | Placeholder; replace for `test` and `run` | Discord incoming webhook URL using HTTPS. |
| `poll_seconds` | `2` | Delay after each processing cycle, from 0.5 to 60 seconds. Reading and sending add to the time between polls. |
| `database_path` | `null` | Automatically detected; supply a database path to override detection. |
| `slack_bundle_ids` | `["com.tinyspeck.slackmacgap"]` | Non-empty list of application bundle identifiers to read. |
| `include_regex` | `null` | Require a match in the combined notification title, subtitle, and body. |
| `exclude_regex` | `null` | Reject notifications matching this expression. |

Filters use Python regular expressions, are case-sensitive by default, and inspect text rather than Slack channel or workspace IDs. If both filters are set, a notification must match `include_regex` and must not match `exclude_regex`.

Use another configuration file by placing `--config` before the command:

```sh
python3 specter.py --config /path/to/config.json run
```

Configuration is read once per invocation. Restart `run` to apply changes.

## Commands

| Command | Purpose |
| --- | --- |
| `init` | Create a configuration file without overwriting an existing one. |
| `doctor` | Read notification records and report decoding counts. |
| `preview` | Print decodable notifications among the five records with the highest record IDs, including filter matches. |
| `test` | Attempt to send one fixed Discord connection test message. |
| `run` | Collect and forward notifications until stopped. |

`preview` does not send messages. Its output is ordered by record ID, not notification timestamp, and may contain fewer than five notifications if records cannot be decoded.

## Behavior and limitations

- The first successful database read in each `run` invocation establishes the baseline. Existing records are skipped; subsequent unseen notification contents can be forwarded.
- Only the title, subtitle, and body stored in notification records are forwarded. Specter does not fetch Slack history, recover missing text, or download attachments. Records removed before a poll can be missed.
- Deduplication and pending messages exist only in memory. Deduplication hashes for records no longer in the latest accepted snapshot are discarded. Stopping or restarting loses pending messages and establishes a new baseline.
- There is no workspace-specific selection. Matching records from multiple Slack workspaces are included unless excluded by text filters or Slack notification settings.
- Outgoing messages start with `Slack notification`, are split to fit a 2,000 UTF-16-unit limit, and disable Discord mention parsing and link embeds.
- The Mac must be awake and the process running to forward notifications. Activity during downtime is not guaranteed to be recovered. Delivery is not guaranteed to occur exactly once: network timeouts, changes to notification text, or records disappearing and reappearing can cause another delivery.
- `run` creates an empty `.specter.lock` file next to the configuration to prevent concurrent runs from the same configuration directory. It does not save notification bodies or create its own notification database. Logs go to the terminal and omit notification text and webhook URLs; `preview` explicitly displays notification text.

## Troubleshooting

**Database access or decoding fails:** Run `doctor`, check permissions, and verify the database format. Automatic detection checks `~/Library/Group Containers/group.com.apple.usernoted/db2/db`, then the `com.apple.notificationcenter/db2/db` path under `getconf DARWIN_USER_DIR`. Set `database_path` if needed. Unsupported records are skipped during `run`.

**Notifications are not forwarded:** Check that Slack generates notification records and use `preview` to inspect text and filter matches. `run` logs database errors and continues trying; the first successful read still becomes the baseline.

**Discord delivery fails:** Check the webhook URL and terminal logs. During `run`, network failures, HTTP 408, and HTTP 5xx responses are retried with increasing delays, up to a 300-second backoff. HTTP 401, 403, and 404 responses are retried no sooner than 300 seconds later. HTTP 429 responses wait at least the reported retry delay, or a fallback delay when it is unavailable. Other HTTP errors drop the affected message and allow later messages to proceed. `test` makes a single send attempt and reports failures without retrying.

Retries hold the first pending message and delay later deliveries, while subsequent cycles can still collect notifications. If adding a batch would exceed 10,000 pending message parts, that batch is not queued and collection is tried again in later cycles. Records removed before they can be queued are lost. Restarting discards the queue.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests use synthetic notification databases and mocked HTTP responses. They do not read your Slack notifications or send messages to Discord.
