import contextlib
import io
import json
from pathlib import Path
import plistlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import specter as relay


def record(rec_id=1, uuid=b"uuid-1", title="Alice (#alerts)", subtitle="Workspace", body="Please review this."):
    return {"rec_id": rec_id, "uuid": uuid, "bundle": "com.tinyspeck.slackmacgap",
            "data": plistlib.dumps({"req": {"titl": title, "subt": subtitle, "body": body}}, fmt=plistlib.FMT_BINARY)}


class SpecterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.forwarder = relay.Relay()
        self.config = dict(relay.DEFAULTS)

    def tearDown(self):
        self.temp.cleanup()

    def test_baseline_then_new_notification(self):
        old, new = record(), record(2, b"uuid-2")
        self.assertEqual(self.forwarder.capture([old], self.config), 0)
        self.assertEqual(self.forwarder.capture([old, new], self.config), 1)
        sent = []
        self.assertTrue(self.forwarder.deliver_one(sent.append))
        self.assertIn("Please review this.", sent[0])
        self.assertEqual(self.forwarder.capture([old, new], self.config), 0)
        self.assertFalse(self.forwarder.deliver_one(sent.append))

    def test_restart_discards_pending_and_skips_existing_notifications(self):
        self.forwarder.capture([], self.config)
        self.forwarder.capture([record()], self.config)
        self.assertEqual(len(self.forwarder.pending), 1)
        self.forwarder = relay.Relay()
        self.assertFalse(self.forwarder.deliver_one(lambda _: self.fail("unexpected send")))
        self.assertEqual(self.forwarder.capture([record()], self.config), 0)
        self.assertEqual(self.forwarder.capture([record(), record(2, b"u2")], self.config), 1)
        sent = []
        self.assertTrue(self.forwarder.deliver_one(sent.append))
        self.assertEqual(len(sent), 1)

    def test_same_uuid_with_updated_content_is_forwarded_once(self):
        self.forwarder.capture([record()], self.config)
        update = record(body="Updated message body.")
        self.assertEqual(self.forwarder.capture([update], self.config), 1)
        self.assertEqual(self.forwarder.capture([update], self.config), 0)

    def test_identical_text_with_different_uuid_is_not_lost(self):
        self.forwarder.capture([], self.config)
        self.assertEqual(self.forwarder.capture([record(), record(2, b"uuid-2")], self.config), 2)

    def test_filter_applies_before_enqueue(self):
        self.forwarder.capture([], self.config)
        self.config.update(include_regex=r"#alerts\b", exclude_regex="excluded")
        rows = [record(), record(2, b"u2", title="Alice (#random)"), record(3, b"u3", body="excluded")]
        self.assertEqual(self.forwarder.capture(rows, self.config), 1)

    def test_missing_uuid_uses_record_id(self):
        self.assertNotEqual(relay.decode_notification(record(uuid=None)).key,
                            relay.decode_notification(record(2, uuid=None)).key)

    def test_localized_fields_and_invalid_plists(self):
        note = relay.decode_notification(record(title=["Alice"], subtitle=None))
        self.assertEqual(note.title, "Alice")
        self.assertEqual(note.subtitle, "")
        with self.assertRaises(relay.DecodeError):
            relay.decode_notification(record(title={"unexpected": True}))
        bad = record()
        bad["data"] = b"not a plist"
        with self.assertRaises(relay.DecodeError):
            relay.decode_notification(bad)

    def test_bad_notification_does_not_drop_valid_neighbors(self):
        self.forwarder.capture([], self.config)
        bad = record()
        bad["data"] = b"invalid"
        with self.assertLogs(relay.LOG, level="WARNING") as logs:
            self.assertEqual(self.forwarder.capture([bad, record(2, b"u2")], self.config), 1)
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(self.forwarder.capture([bad, record(2, b"u2")], self.config), 0)

    def test_retry_preserves_order_and_keeps_capturing(self):
        self.forwarder.capture([], self.config)
        self.forwarder.capture([record()], self.config)
        def throttle(_):
            raise relay.DeliveryError("HTTP 429", 20)
        with self.assertLogs(relay.LOG, level="WARNING"):
            self.assertFalse(self.forwarder.deliver_one(throttle, now=100))
        self.forwarder.capture([record(), record(2, b"u2")], self.config)
        sent = []
        self.assertFalse(self.forwarder.deliver_one(sent.append, now=119))
        self.assertTrue(self.forwarder.deliver_one(sent.append, now=120))
        self.assertTrue(self.forwarder.deliver_one(sent.append, now=120))
        self.assertEqual(len(sent), 2)

    def test_permanent_failure_is_dropped_and_next_message_is_sent(self):
        self.forwarder.capture([], self.config)
        self.forwarder.capture([record(), record(2, b"u2", body="Next notification.")], self.config)
        def fail(_):
            raise relay.DeliveryError("HTTP 400")
        with self.assertLogs(relay.LOG, level="ERROR"):
            self.assertTrue(self.forwarder.deliver_one(fail))
        sent = []
        self.assertTrue(self.forwarder.deliver_one(sent.append))
        self.assertIn("Next notification.", sent[0])
        self.assertFalse(self.forwarder.deliver_one(sent.append))

    def test_long_emoji_messages_are_split_without_loss(self):
        note = relay.decode_notification(record(body="\U0001F600" * 2500))
        parts = relay.message_parts(note)
        self.assertEqual("".join(part.removeprefix("Slack notification\n") for part in parts), note.text)
        self.assertTrue(all(len(part.encode("utf-16-le")) // 2 <= 2000 for part in parts))

    def test_capture_and_delivery_do_not_open_storage(self):
        with patch.object(relay.sqlite3, "connect", side_effect=AssertionError("unexpected storage")):
            self.forwarder.capture([], self.config)
            self.forwarder.capture([record()], self.config)
            self.assertTrue(self.forwarder.deliver_one(lambda _: None))
        self.assertEqual(list(self.path.iterdir()), [])

    def test_removed_notification_hashes_are_released(self):
        self.forwarder.capture([record()], self.config)
        self.assertEqual(len(self.forwarder.seen), 1)
        self.forwarder.capture([], self.config)
        self.assertEqual(self.forwarder.seen, set())

    def test_full_queue_does_not_lose_unseen_notification(self):
        self.forwarder.capture([], self.config)
        self.forwarder.pending.extend(["pending"] * 10000)
        with self.assertRaises(relay.SetupError):
            self.forwarder.capture([record()], self.config)
        self.assertEqual(self.forwarder.seen, set())
        self.forwarder.pending.clear()
        self.assertEqual(self.forwarder.capture([record()], self.config), 1)

    def test_read_only_database_observes_live_wal_and_selects_only_slack(self):
        database = self.path / "notifications.db"
        with contextlib.closing(sqlite3.connect(database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.executescript("""
                CREATE TABLE app(app_id INTEGER PRIMARY KEY, identifier TEXT);
                CREATE TABLE record(rec_id INTEGER PRIMARY KEY, app_id INTEGER, uuid BLOB, data BLOB, delivered_date REAL);
                INSERT INTO app VALUES (1,'com.tinyspeck.slackmacgap'), (2,'other.app');
            """)
            for app_id in (1, 2):
                row = record(app_id)
                writer.execute("INSERT INTO record VALUES (?,?,?,?,?)", (app_id, app_id, row["uuid"], row["data"], 1))
            writer.commit()
            rows = relay.read_notifications(database, self.config["slack_bundle_ids"])
            self.assertEqual(len(rows), 1)
            self.assertEqual(relay.decode_notification(rows[0]).title, "Alice (#alerts)")
            row = record(3, b"u3")
            writer.execute("INSERT INTO record VALUES (3,1,?,?,2)", (row["uuid"], row["data"]))
            writer.commit()
            self.assertEqual(len(relay.read_notifications(database, self.config["slack_bundle_ids"])), 2)
            self.assertEqual(writer.execute("SELECT COUNT(*) FROM record").fetchone()[0], 3)

    def test_missing_database_is_not_created(self):
        path = self.path / "missing.db"
        with self.assertRaises(relay.SetupError):
            relay.read_notifications(path, self.config["slack_bundle_ids"])
        self.assertFalse(path.exists())

    def test_unsupported_schema_reports_setup_error(self):
        database = self.path / "unknown.db"
        sqlite3.connect(database).close()
        with self.assertRaises(relay.SetupError):
            relay.read_notifications(database, self.config["slack_bundle_ids"])

    def test_single_process_lock(self):
        with relay.locked(self.path / "config.json"):
            with self.assertRaises(relay.SetupError):
                with relay.locked(self.path / "config.json"):
                    pass


class DiscordTests(unittest.TestCase):
    URL = "https://discord.com/api/webhooks/123/fake-test-token"

    def test_endpoint_forces_confirmation_and_preserves_thread(self):
        endpoint = relay.webhook_endpoint(self.URL + "?wait=false&thread_id=123")
        self.assertIn("wait=true", endpoint)
        self.assertIn("thread_id=123", endpoint)

    def test_rejects_other_hosts_and_invalid_urls(self):
        for url in ["http://discord.com/api/webhooks/123/token", "https://evil.example/api/webhooks/123/token",
                    "https://discord.com@evil.example/api/webhooks/123/token", "https://[invalid", relay.DEFAULTS["webhook_url"]]:
            with self.subTest(url=url), self.assertRaises(relay.SetupError):
                relay.webhook_endpoint(url)

    def test_payload_disables_mentions_and_link_previews(self):
        sender = relay.Discord(self.URL)
        response = io.BytesIO(b'{}')
        response.headers = {}
        with patch.object(sender.opener, "open", return_value=response) as opener:
            sender("@everyone <@123> https://internal.example")
        request = opener.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(payload["flags"], 4)
        self.assertEqual(opener.call_args.kwargs["timeout"], 10)

    def test_429_uses_discord_retry_after(self):
        sender = relay.Discord(self.URL)
        error = urllib.error.HTTPError(self.URL, 429, "throttled", {}, io.BytesIO(b'{"retry_after": 12.5}'))
        with patch.object(sender.opener, "open", side_effect=error), self.assertRaises(relay.DeliveryError) as raised:
            sender("hello")
        self.assertEqual(raised.exception.retry_after, 12.5)
        self.assertNotIn("fake-test-token", str(raised.exception))

    def test_network_and_auth_errors_remain_retryable(self):
        for error in [urllib.error.URLError(self.URL), urllib.error.HTTPError(self.URL, 404, "missing", {}, None)]:
            sender = relay.Discord(self.URL)
            with patch.object(sender.opener, "open", side_effect=error), self.assertRaises(relay.DeliveryError) as raised:
                sender("hello")
            self.assertGreater(raised.exception.retry_after, 0)
            self.assertNotIn("fake-test-token", str(raised.exception))

    def test_success_rate_limit_headers_delay_next_send(self):
        sender = relay.Discord(self.URL)
        response = io.BytesIO(b'{}')
        response.headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "30"}
        with patch.object(sender.opener, "open", return_value=response):
            sender("first")
        with self.assertRaises(relay.DeliveryError) as raised:
            sender("second")
        self.assertGreater(raised.exception.retry_after, 29)

    def test_no_redirects(self):
        self.assertIsNone(relay.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example"))


class CommandTests(unittest.TestCase):
    def test_config_validation_and_private_init(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(relay.main(["--config", str(path), "init"]), 0)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            config = relay.read_config(path, require_webhook=False)
            for key, value in [("poll_seconds", float("nan")), ("poll_seconds", True),
                               ("slack_bundle_ids", []), ("include_regex", "["), ("database_path", 42)]:
                path.write_text(json.dumps(config | {key: value}))
                with self.subTest(key=key), self.assertRaises(relay.SetupError):
                    relay.read_config(path, require_webhook=False)

    def test_default_config_is_next_to_script(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            path = project / "config.json"
            config = relay.DEFAULTS | {"webhook_url": DiscordTests.URL, "poll_seconds": 11}
            path.write_text(json.dumps(config))
            with patch.object(relay, "__file__", str(project / "specter.py")), \
                 patch.object(relay, "run") as runner:
                self.assertEqual(relay.main(["run"]), 0)
            runner.assert_called_once_with(config, path.resolve())



if __name__ == "__main__":
    unittest.main()
