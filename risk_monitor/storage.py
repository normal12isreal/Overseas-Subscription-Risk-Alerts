"""Durable incident authority and independent todo-reminder records."""

from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3

from .domain import Delivery, IncidentSeed, Solution, parse_time
from .mail import MailMessage


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "monitor.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS incidents (
                    event_id TEXT PRIMARY KEY, seed_json TEXT NOT NULL,
                    detected_at TEXT NOT NULL, refresh_due_at TEXT NOT NULL,
                    initial_done INTEGER NOT NULL DEFAULT 0,
                    refresh_done INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS reminders (
                    event_id TEXT PRIMARY KEY, seed_json TEXT NOT NULL,
                    solution_json TEXT NOT NULL, path TEXT NOT NULL,
                    created_at TEXT NOT NULL, due_at TEXT NOT NULL,
                    sent_at TEXT, cancelled_at TEXT);
                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_key TEXT PRIMARY KEY, receipt_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    run_key TEXT PRIMARY KEY, report_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mail_messages (
                    mailbox TEXT NOT NULL, message_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, received_at TEXT NOT NULL,
                    queued_at TEXT NOT NULL, status TEXT NOT NULL,
                    payload_json TEXT NOT NULL, event_ids_json TEXT NOT NULL,
                    PRIMARY KEY (mailbox, message_id));
                CREATE INDEX IF NOT EXISTS mail_content_lookup
                    ON mail_messages(mailbox, fingerprint);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(mail_messages)")}
            if "last_attempt_at" not in columns:
                db.execute("ALTER TABLE mail_messages ADD COLUMN last_attempt_at TEXT")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def register(self, seed: IncidentSeed, now: datetime, refresh_hours: float) -> dict:
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO incidents VALUES (?, ?, ?, ?, 0, 0)",
                       (seed.event_id, seed.model_dump_json(), now.isoformat(),
                        (now + timedelta(hours=refresh_hours)).isoformat()))
            row = db.execute("SELECT * FROM incidents WHERE event_id=?", (seed.event_id,)).fetchone()
            # First evidence/time owns this event. A different event needs a new source_ref.
            return dict(row)

    def incidents(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM incidents ORDER BY detected_at, event_id")]

    def mail_known(self, mailbox: str, message_id: str) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1 FROM mail_messages WHERE mailbox=? AND message_id=?",
                              (mailbox, message_id)).fetchone() is not None

    def queue_mail(self, message: MailMessage, now: datetime) -> bool:
        # Clipped bodies cannot prove two distinct messages are identical.
        fingerprint = message.fingerprint + (":" + message.message_id if message.truncated else "")
        with self.connect() as db:
            if db.execute("SELECT 1 FROM mail_messages WHERE mailbox=? AND message_id=?",
                          (message.mailbox, message.message_id)).fetchone():
                return False
            duplicate = db.execute("SELECT 1 FROM mail_messages WHERE mailbox=? AND fingerprint=?",
                                   (message.mailbox, fingerprint)).fetchone() is not None
            db.execute("INSERT INTO mail_messages (mailbox, message_id, fingerprint, received_at, "
                       "queued_at, status, payload_json, event_ids_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (message.mailbox, message.message_id, fingerprint, message.received_at.isoformat(),
                        now.isoformat(), "duplicate" if duplicate else "pending",
                        "{}" if duplicate else message.model_dump_json(), "[]"))
            return not duplicate

    def skip_mail(self, mailbox: str, message_id: str, received_at: datetime, now: datetime):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO mail_messages (mailbox, message_id, fingerprint, received_at, "
                       "queued_at, status, payload_json, event_ids_json) VALUES (?, ?, ?, ?, ?, 'skipped', '{}', '[]')",
                       (mailbox, message_id, "skipped:" + message_id, received_at.isoformat(), now.isoformat()))

    def pending_mail(self, limit: int) -> list[MailMessage]:
        with self.connect() as db:
            return [MailMessage.model_validate_json(row[0]) for row in db.execute(
                "SELECT payload_json FROM mail_messages WHERE status='pending' "
                "ORDER BY last_attempt_at IS NOT NULL, last_attempt_at, queued_at, message_id LIMIT ?",
                (limit,))]

    def mail_attempted(self, message: MailMessage, now: datetime):
        with self.connect() as db:
            db.execute("UPDATE mail_messages SET last_attempt_at=? WHERE mailbox=? AND message_id=?",
                       (now.isoformat(), message.mailbox, message.message_id))

    def mail_counts(self) -> dict:
        with self.connect() as db:
            return {row[0]: row[1] for row in db.execute("SELECT status, COUNT(*) FROM mail_messages GROUP BY status")}

    def complete_mail(self, message: MailMessage, seeds: list[IncidentSeed], now: datetime, hours: float):
        # Register ALL candidates and mark their source processed in one commit.
        # A crash cannot leave a processed email with only some events recorded.
        with self.connect() as db:
            row = db.execute("SELECT status FROM mail_messages WHERE mailbox=? AND message_id=?",
                             (message.mailbox, message.message_id)).fetchone()
            if not row or row[0] != "pending":
                raise ValueError("Mail must be queued and pending before registration")
            for seed in seeds:
                db.execute("INSERT OR IGNORE INTO incidents VALUES (?, ?, ?, ?, 0, 0)",
                           (seed.event_id, seed.model_dump_json(), now.isoformat(),
                            (now + timedelta(hours=hours)).isoformat()))
            db.execute("UPDATE mail_messages SET status='processed', payload_json='{}', event_ids_json=? "
                       "WHERE mailbox=? AND message_id=? AND status='pending'",
                       (json.dumps([s.event_id for s in seeds]), message.mailbox, message.message_id))

    def due_refreshes(self, now: datetime) -> list[dict]:
        return [row for row in self.incidents()
                if not row["refresh_done"] and parse_time(row["refresh_due_at"]) <= now]

    def mark_done(self, event_id: str, run_kind: str) -> None:
        field = {"initial": "initial_done", "refresh": "refresh_done"}[run_kind]
        with self.connect() as db:
            db.execute(f"UPDATE incidents SET {field}=1 WHERE event_id=?", (event_id,))

    def reminder(self, event_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM reminders WHERE event_id=?", (event_id,)).fetchone()
            return dict(row) if row else None

    def register_reminder(self, seed: IncidentSeed, solution: Solution, path: Path,
                          now: datetime, hours: float) -> dict:
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO reminders VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
                       (seed.event_id, seed.model_dump_json(), solution.model_dump_json(), str(path),
                        now.isoformat(), (now + timedelta(hours=hours)).isoformat()))
        return self.reminder(seed.event_id)

    def pending_reminders(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM reminders WHERE sent_at IS NULL AND cancelled_at IS NULL")]

    def close_reminder(self, event_id: str, now: datetime, *, sent: bool) -> None:
        field = "sent_at" if sent else "cancelled_at"
        with self.connect() as db:
            db.execute(f"UPDATE reminders SET {field}=? WHERE event_id=?", (now.isoformat(), event_id))

    def delivery(self, key: str) -> Delivery | None:
        with self.connect() as db:
            row = db.execute("SELECT receipt_json FROM deliveries WHERE delivery_key=?", (key,)).fetchone()
            return Delivery.model_validate_json(row[0]) if row else None

    def save_delivery(self, receipt: Delivery) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO deliveries VALUES (?, ?)",
                       (receipt.key, receipt.model_dump_json()))

    def run_report(self, key: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT report_json FROM runs WHERE run_key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def save_report(self, key: str, report: dict) -> None:
        write_json(self.directory / "reports" / (key.replace(":", "_") + ".json"), report)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO runs VALUES (?, ?)",
                       (key, json.dumps(report, ensure_ascii=False)))


@contextmanager
def process_lock(directory: Path):
    """Prevent overlapping invocations in this data directory (including watch)."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "run.lock").open("a+b") as handle:
        handle.seek(0)
        # Reading an already locked byte raises PermissionError on Windows.
        # Inspect metadata instead so contenders reach the nonblocking lock call.
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another monitor process is using this data directory") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
