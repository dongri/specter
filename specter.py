#!/usr/bin/env python3
"""Specter: forward local macOS Slack notifications. Python 3.10+, standard library only."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import plistlib
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass

LOG = logging.getLogger("specter")
DEFAULTS = {
    "webhook_url": "https://discord.com/api/webhooks/WEBHOOK_ID/WEBHOOK_TOKEN",
    "poll_seconds": 2,
    "database_path": None,
    "slack_bundle_ids": ["com.tinyspeck.slackmacgap"],
    "include_regex": None,
    "exclude_regex": None,
}


class SetupError(Exception):
    pass


class DecodeError(Exception):
    pass


class DeliveryError(Exception):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def private_write(path, data):
    # Atomic replacement; the temporary file is never world-readable.
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def read_config(path, require_webhook=True):
    try:
        supplied = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SetupError("Cannot read the configuration file. Run init and check the JSON syntax.") from exc
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULTS):
        raise SetupError("The configuration contains unknown keys or is not an object. See config.example.json.")
    config = DEFAULTS | supplied
    seconds = config["poll_seconds"]
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0.5 <= seconds <= 60:
        raise SetupError("poll_seconds must be a number between 0.5 and 60.")
    ids = config["slack_bundle_ids"]
    if not isinstance(ids, list) or not ids or any(not isinstance(x, str) or not x for x in ids):
        raise SetupError("slack_bundle_ids must be a non-empty list of non-empty strings.")
    if config["database_path"] is not None and (not isinstance(config["database_path"], str) or not config["database_path"]):
        raise SetupError("database_path must be a non-empty path string or null.")
    for key in ("include_regex", "exclude_regex"):
        value = config[key]
        try:
            if value is not None:
                if not isinstance(value, str):
                    raise TypeError
                re.compile(value)
        except (re.error, TypeError) as exc:
            raise SetupError(f"{key} must be a valid regular expression string or null.") from exc
    if require_webhook:
        webhook_endpoint(config["webhook_url"])
    return config


def webhook_endpoint(url):
    # Only send to Discord HTTPS endpoints, and never log the secret URL.
    if not isinstance(url, str):
        raise SetupError("Set webhook_url to a Discord Webhook URL.")
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise SetupError("Set webhook_url to a valid Discord Webhook URL.") from exc
    if (parsed.scheme != "https" or parsed.netloc not in {"discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"}
            or not re.fullmatch(r"/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9_-]+", parsed.path)
            or parsed.fragment):
        raise SetupError("Set webhook_url to a valid Discord Webhook URL.")
    query = urllib.parse.parse_qs(parsed.query)
    query["wait"] = ["true"]  # Request confirmation rather than fire-and-forget.
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query, doseq=True)))


def database_candidates():
    yield Path.home() / "Library/Group Containers/group.com.apple.usernoted/db2/db"
    if sys.platform == "darwin":
        result = subprocess.run(["/usr/bin/getconf", "DARWIN_USER_DIR"], capture_output=True, text=True, check=True)
        yield Path(result.stdout.strip()) / "com.apple.notificationcenter/db2/db"


def find_database(config):
    if config["database_path"]:
        return Path(config["database_path"]).expanduser().resolve()
    for path in database_candidates():
        try:
            if path.is_file():
                return path
        except PermissionError as exc:
            raise SetupError("Access to the notification database was denied. Check Full Disk Access permissions.") from exc
    raise SetupError("Notification database not found. Receive a Slack notification and check Full Disk Access permissions. Set database_path if needed.")


def text_field(value):
    if value is None:
        return ""
    # Localized notification strings sometimes use a one-element array.
    if isinstance(value, list):
        value = value[0] if value else ""
    if not isinstance(value, str):
        raise DecodeError("Unsupported notification text format.")
    return value.replace("\x00", "").strip()


@dataclass(frozen=True)
class Notification:
    key: str
    title: str
    subtitle: str
    body: str

    @property
    def text(self):
        return "\n".join(x for x in (self.title, self.subtitle, self.body) if x)


def decode_notification(row):
    try:
        payload = plistlib.loads(row["data"])
        request = payload["req"]
        fields = [text_field(request.get(key, "")) for key in ("titl", "subt", "body")]
        if not any(fields):
            raise DecodeError("The notification has no title or body.")
    except (ValueError, TypeError, KeyError, AttributeError, plistlib.InvalidFileException, OverflowError) as exc:
        raise DecodeError("Unsupported notification data format.") from exc
    uuid = row["uuid"]
    identity = uuid.hex() if isinstance(uuid, bytes) else str(uuid or row["rec_id"])
    # Presentation flags and delivered_date can change without a new message.
    # A genuinely updated title/body on the same UUID is a new event.
    key = hashlib.sha256(json.dumps([row["bundle"], identity, *fields], ensure_ascii=False).encode()).hexdigest()
    return Notification(key, *fields)


def read_notifications(path, bundle_ids):
    try:
        # mode=ro preserves WAL visibility; immutable=1 would miss live updates.
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            placeholders = ",".join("?" for _ in bundle_ids)
            rows = db.execute(
                "SELECT r.rec_id, r.uuid, r.data, a.identifier AS bundle "
                "FROM record r JOIN app a ON a.app_id = r.app_id "
                f"WHERE a.identifier IN ({placeholders}) ORDER BY r.rec_id", bundle_ids).fetchall()
            return rows
    except sqlite3.Error as exc:
        raise SetupError("Cannot read the notification database. Check Full Disk Access, database_path, and the macOS database format.") from exc


def matches(notification, config):
    include, exclude = config["include_regex"], config["exclude_regex"]
    return ((include is None or re.search(include, notification.text) is not None)
            and (exclude is None or re.search(exclude, notification.text) is None))


def message_parts(notification):
    # Count UTF-16 units conservatively so emoji cannot overflow Discord's limit.
    header = "Slack notification\n"
    chunks, current, units = [], header, len(header)
    for char in notification.text:
        cost = 2 if ord(char) > 0xFFFF else 1
        if units + cost > 2000:
            chunks.append(current)
            current, units = header, len(header)
        current += char
        units += cost
    chunks.append(current)
    return chunks


class Relay:
    """Keep only the current notification snapshot and pending sends in memory."""

    def __init__(self):
        self.initialized = False
        self.seen = set()
        self.pending = deque()
        self.attempts = 0
        self.due = 0

    def capture(self, rows, config):
        current, added = set(), []
        for row in rows:
            try:
                notification = decode_notification(row)
                key = notification.key
            except DecodeError:
                raw = row["data"]
                raw = raw if isinstance(raw, bytes) else str(raw).encode()
                key = "invalid:" + hashlib.sha256(raw).hexdigest()
                if key not in self.seen and key not in current:
                    LOG.warning("Skipped an unsupported Slack notification (record %s).", row["rec_id"])
                notification = None
            if self.initialized and key not in self.seen and key not in current and notification and matches(notification, config):
                added.extend(message_parts(notification))
            current.add(key)
        if len(self.pending) + len(added) > 10000:
            raise SetupError("The pending message limit has been reached. Check the Discord connection.")
        self.pending.extend(added)
        # Drop hashes for records no longer in the notification DB to bound memory.
        self.seen = current
        if not self.initialized:
            self.initialized = True
            LOG.info("Skipped existing notifications. Forwarding new notifications from now on.")
        return len(added)

    def deliver_one(self, sender, now=None):
        now = time.time() if now is None else now
        if not self.pending or self.due > now:
            return False
        try:
            sender(self.pending[0])
        except DeliveryError as exc:
            if exc.retry_after is not None:
                delay = max(1, exc.retry_after, min(300, 2 ** min(self.attempts + 1, 9)))
                self.attempts += 1
                self.due = now + delay
                LOG.warning("Retrying delivery in %.1f seconds: %s", delay, exc)
                return False
            LOG.error("Stopped delivery of this notification: %s", exc)
        else:
            LOG.info("Sent to Discord.")
        self.pending.popleft()
        self.attempts = 0
        self.due = 0
        return True


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Discord:
    def __init__(self, url):
        self.url = webhook_endpoint(url)
        self.opener = urllib.request.build_opener(NoRedirect)
        self.next_send = 0

    def __call__(self, content):
        if time.time() < self.next_send:
            raise DeliveryError("Waiting for the Discord rate limit to reset.", self.next_send - time.time())
        payload = {"content": content, "allowed_mentions": {"parse": []}, "flags": 4}
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json", "User-Agent": "Specter/1.0"}, method="POST")
        try:
            with self.opener.open(request, timeout=10) as response:
                response.read()
                if response.headers.get("X-RateLimit-Remaining") == "0":
                    self.next_send = time.time() + positive_delay(response.headers.get("X-RateLimit-Reset-After"), 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry = exc.headers.get("Retry-After")
                try:
                    retry = json.loads(exc.read()).get("retry_after", retry)
                except (ValueError, AttributeError):
                    pass
                raise DeliveryError("Discord HTTP 429", positive_delay(retry, 5)) from None
            if exc.code >= 500 or exc.code in {408, 401, 403, 404}:
                delay = 300 if exc.code in {401, 403, 404} else 2
                raise DeliveryError(f"Discord HTTP {exc.code} (check the connection and Webhook settings)", delay) from None
            raise DeliveryError(f"Discord HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Exceptions can contain the Webhook URL. Only emit a generic message.
            raise DeliveryError("Failed to connect to Discord.", 2) from None


def positive_delay(value, fallback):
    try:
        number = float(value)
        if math.isfinite(number) and number > 0:
            return number
    except (ValueError, TypeError):
        pass
    return fallback


@contextlib.contextmanager
def locked(config_path):
    # The empty lock file prevents duplicate processes; no notification data is saved.
    path = config_path.with_name(".specter.lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SetupError("A process using this configuration directory is already running. Stop the other process first.") from exc
        yield


def run(config, config_path):
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    discord = Discord(config["webhook_url"])
    with locked(config_path):
        relay = Relay()
        LOG.info("Started monitoring notifications.")
        last_error = None
        while not stop.is_set():
            try:
                path = find_database(config)
                count = relay.capture(read_notifications(path, config["slack_bundle_ids"]), config)
                if count:
                    LOG.info("Received %s new messages.", count)
                if last_error:
                    LOG.info("Notification database monitoring has resumed.")
                last_error = None
            except SetupError as exc:
                if str(exc) != last_error:
                    LOG.error("%s", exc)
                last_error = str(exc)
            # Network outages do not stop capture; DB outages do not stop delivery.
            for _ in range(10):
                if stop.is_set() or not relay.deliver_one(discord):
                    break
            stop.wait(config["poll_seconds"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent / "config.json",
                        help="Configuration file (default: config.json in the same directory as specter.py).")
    parser.add_argument("command", choices=["init", "doctor", "preview", "run", "test"])
    args = parser.parse_args(argv)
    config_path = args.config.expanduser().resolve()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "init":
            if config_path.exists():
                raise SetupError("The configuration file already exists. Exiting without overwriting it.")
            config_path.parent.mkdir(parents=True, exist_ok=True)
            private_write(config_path, json.dumps(DEFAULTS, ensure_ascii=False, indent=2).encode() + b"\n")
            print(f"Created the configuration file: {config_path}\nEdit webhook_url before running Specter.")
            return 0
        config = read_config(config_path, args.command not in {"doctor", "preview"})
        if args.command in {"doctor", "preview"}:
            path = find_database(config)
            rows = read_notifications(path, config["slack_bundle_ids"])
            print(f"Notification database: {path}\nSlack notification records: {len(rows)}\nPython: {Path(sys.executable).resolve()}")
            decoded = 0
            for row in rows:
                try:
                    notification = decode_notification(row)
                    decoded += 1
                    if args.command == "preview" and row in rows[-5:]:
                        print(json.dumps({"title": notification.title, "subtitle": notification.subtitle, "body": notification.body,
                                          "matches": matches(notification, config)}, ensure_ascii=False))
                except DecodeError:
                    pass
            print(f"Decodable notifications: {decoded}/{len(rows)}")
            if rows and not decoded:
                raise SetupError("Cannot decode Slack notifications. This macOS/Slack notification format is not supported.")
        elif args.command == "run":
            run(config, config_path)
        elif args.command == "test":
            Discord(config["webhook_url"])("Specter: Discord Webhook connection test.")
            print("Sent the test notification.")
        return 0
    except (SetupError, DeliveryError) as exc:
        LOG.error("%s", exc)
        return 1
    except (OSError, sqlite3.Error, subprocess.SubprocessError):
        # Do not accidentally put notification bodies or secrets in tracebacks.
        LOG.error("A local file or system operation failed. Check permissions and available disk space.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
