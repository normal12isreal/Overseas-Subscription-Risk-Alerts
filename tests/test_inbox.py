import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from risk_monitor.cli import poll_once
from risk_monitor.config import Settings
from risk_monitor.delivery import Notifier
from risk_monitor.domain import IncidentSeed
from risk_monitor.gmail import API, GmailError, GmailReader, SCOPES, body_text, load_credentials, parse_message
from risk_monitor.inbox import InboxWorkflow, seeds_from_analysis
from risk_monitor.llm import DemoModel
from risk_monitor.mail import MailAnalysis, MailMessage
from risk_monitor.research import FixtureResearch
from risk_monitor.storage import Store
from risk_monitor.workflow import Workflow

NOW = datetime(2026, 10, 21, 4, 48, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent


def run(coro):
    return asyncio.run(coro)


def message(message_id="mail-1", **updates):
    values = dict(mailbox="account-one", message_id=message_id, received_at=NOW,
                  sender="notices@example.invalid", subject="giffgaff test notice",
                  body="SIM alpha will be deactivated in two months. SIM beta will be deactivated in two months.")
    return MailMessage(**(values | updates))


def candidates():
    return MailAnalysis(risks=[dict(service="giffgaff", account_ref=account,
        service_status="scheduled_deactivation", effective_at=NOW + timedelta(days=60),
        evidence=f"SIM {account} will be deactivated in two months.") for account in ("alpha", "beta")])


def part(text, mime="text/plain", charset="utf-8", **updates):
    return {"mimeType": mime, "headers": [{"name": "Content-Type", "value": f"{mime}; charset={charset}"}],
            "body": {"data": base64.urlsafe_b64encode(text.encode(charset)).decode().rstrip("=")}, **updates}


def raw_mail(mid, received=NOW, labels=None, text=None):
    payload = part(text if text is not None else "Notice " + mid)
    payload["headers"] += [{"name": "From", "value": "notices@example.invalid"},
                           {"name": "Subject", "value": "=?UTF-8?B?5rWL6K+V?="}]
    return {"id": mid, "internalDate": str(int(received.timestamp() * 1000)),
            "labelIds": labels if labels is not None else ["INBOX"], "payload": payload}


class Session:
    def __init__(self, pages, messages):
        self.pages, self.messages, self.calls = pages, messages, []

    def get(self, url, params, timeout):
        assert url.startswith(API) and timeout > 0
        self.calls.append((url, params))
        if url.endswith("/profile"):
            value = {"emailAddress": "owner@example.invalid"}
        elif url.endswith("/messages"):
            value = self.pages[params.get("pageToken", "first")]
        else:
            value = self.messages[url.rsplit("/", 1)[-1]]
        return type("Response", (), {"status_code": 200, "json": lambda _: value})()


def settings(tmp_path, **updates):
    return Settings(todo_directory=tmp_path / "todos", data_directory=tmp_path / "data", **updates)


def test_gmail_enforces_exact_two_hours_and_filters_labels_without_changing_mail(tmp_path):
    config = settings(tmp_path, gmail_max_messages=20)
    store = Store(config.data_directory)
    raws = {m["id"]: m for m in [
        raw_mail("start", NOW - timedelta(hours=2)), raw_mail("end"),
        raw_mail("old", NOW - timedelta(hours=2, milliseconds=1)),
        raw_mail("future", NOW + timedelta(milliseconds=1)),
        raw_mail("spam", labels=["SPAM"]), raw_mail("trash", labels=["TRASH"]),
        raw_mail("draft", labels=["DRAFT"]), raw_mail("sent", labels=["SENT"]),
        raw_mail("archived", labels=[]), raw_mail("self", labels=["SENT", "INBOX"])]}
    session = Session({"first": {"messages": [{"id": key} for key in raws]}}, raws)
    result = GmailReader(config, session).read(NOW, store)
    assert result["queued"] == 4 and result["skipped"] == 6
    assert {m.message_id for m in store.pending_mail(20)} == {"start", "end", "archived", "self"}
    query = next(params for url, params in session.calls if url.endswith("/messages"))
    assert f"after:{int((NOW - timedelta(hours=2)).timestamp()) - 1}" in query["q"]
    assert query["includeSpamTrash"] is False
    assert not store.mail_known(GmailReader(config, session).profile(), "future")


def test_mime_prefers_plain_text_decodes_html_and_ignores_attachments():
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "multipart/alternative", "parts": [part("café", charset="iso-8859-1"),
                                                          part("<p>duplicate</p>", "text/html")]},
        part("<head>hidden</head><p>Identity required</p><script>evil()</script>", "text/html"),
        part("attachment must not reach model", filename="private.txt")]}
    assert body_text(payload) == "café\nIdentity required"
    parsed = parse_message(raw_mail("unicode"), "box", 100)
    assert parsed.subject == "测试"
    clipped = parse_message(raw_mail("clip", text="x" * 120), "box", 100)
    assert len(clipped.body) == 100 and clipped.truncated


def test_pagination_skips_known_ids_before_body_limit_and_continues_next_poll(tmp_path):
    config = settings(tmp_path, gmail_max_messages=1)
    store = Store(config.data_directory)
    raws = {key: raw_mail(key) for key in ("known", "new-a", "new-b")}
    session = Session({"first": {"messages": [{"id": "known"}], "nextPageToken": "next"},
                       "next": {"messages": [{"id": "new-a"}, {"id": "new-b"}]}}, raws)
    reader = GmailReader(config, session)
    mailbox = reader.profile()
    store.queue_mail(parse_message(raws["known"], mailbox, 12000), NOW)
    first = reader.read(NOW, store)
    assert first["queued"] == 1 and first["has_more"]
    second = reader.read(NOW, store)
    assert second["queued"] == 1 and not second["has_more"]
    fetched = [url.rsplit("/", 1)[-1] for url, _ in session.calls if "/messages/" in url]
    assert fetched == ["new-a", "new-b"]


class QueueReader:
    def __init__(self, messages):
        self.messages = messages

    def read(self, now, store):
        return {"queued": sum(store.queue_mail(item, now) for item in self.messages)}


class Detector(DemoModel):
    def __init__(self):
        self.identified = []

    async def generate(self, schema, task, payload):
        if task == "identify":
            self.identified.append(payload["email"]["message_id"])
            return candidates() if "alpha" in payload["email"]["body"] else MailAnalysis(risks=[])
        return await super().generate(schema, task, payload)


def test_multiple_risks_and_no_risk_mail_flow_through_downstream_and_restart_dedupes(tmp_path):
    config = settings(tmp_path)
    store, model = Store(config.data_directory), Detector()
    reader = QueueReader([message(), message("copy"), message("offers", body="Weekly offers, no action required")])
    intake = InboxWorkflow(config, store, model, reader)
    first = run(intake.poll(NOW))
    assert first["identification"]["analyzed"] == 2
    assert first["identification"]["no_risk"] == 1
    assert len(first["identification"]["event_ids"]) == len(store.incidents()) == 2
    workflow = Workflow(config, store, model, FixtureResearch(ROOT / "examples/research-materials.json"),
                        Notifier(config, store))
    result = run(workflow.tick(NOW))
    assert len(result["events"]) == 2 and all(r["status"] == "completed" for r in result["events"])
    assert {r["seed"]["account_ref"] for r in result["events"]} == {"alpha", "beta"}
    assert all(r["seed"]["simulated"] is False for r in result["events"])
    assert len(list(config.todo_directory.glob("*.md"))) == 2
    original_due = [r["refresh_due_at"] for r in store.incidents()]
    restarted = Store(config.data_directory)
    again = run(InboxWorkflow(config, restarted, model, reader).poll(NOW + timedelta(minutes=10)))
    assert again["identification"]["analyzed"] == 0 and len(model.identified) == 2
    assert [r["refresh_due_at"] for r in restarted.incidents()] == original_due


def test_registration_failure_rolls_back_all_events_and_leaves_mail_pending(tmp_path):
    store = Store(tmp_path)
    mail = message()
    seeds = seeds_from_analysis(mail, candidates())
    store.queue_mail(mail, NOW)
    with store.connect() as db:
        db.execute(f"CREATE TRIGGER reject_second BEFORE INSERT ON incidents WHEN NEW.event_id='{seeds[1].event_id}' "
                   "BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.complete_mail(mail, seeds, NOW, 24)
    assert store.incidents() == [] and len(store.pending_mail(10)) == 1
    with store.connect() as db:
        db.execute("DROP TRIGGER reject_second")
    store.complete_mail(mail, seeds, NOW, 24)
    assert len(store.incidents()) == 2 and store.pending_mail(10) == []


def test_failed_analysis_is_not_no_risk_and_can_retry_cached_mail_after_two_hours(tmp_path):
    class Broken:
        async def generate(self, *args):
            raise RuntimeError("temporary failure")

    class Offline:
        def read(self, *args):
            raise GmailError("Gmail API 暂不可用（HTTP 503），稍后重试")

    config = settings(tmp_path)
    store = Store(config.data_directory)
    failed = run(InboxWorkflow(config, store, Broken(), QueueReader([message()])).poll(NOW))
    assert failed["identification"]["no_risk"] == 0 and failed["identification"]["pending"] == 1
    recovered = run(InboxWorkflow(config, store, Detector(), Offline()).poll(NOW + timedelta(hours=3)))
    assert recovered["mail"]["status"] == "failed"
    assert len(recovered["identification"]["event_ids"]) == 2
    assert store.mail_counts() == {"processed": 1}


def test_hallucinated_evidence_does_not_register_an_event(tmp_path):
    analysis = candidates()
    analysis.risks[0].evidence = "The model invented this warning."
    with pytest.raises(ValueError, match="quoted"):
        seeds_from_analysis(message(), analysis)


def test_content_deduplication_respects_mailbox_and_does_not_merge_truncated_messages(tmp_path):
    store = Store(tmp_path)
    assert store.queue_mail(message(), NOW)
    assert not store.queue_mail(message("same-content"), NOW)
    assert store.queue_mail(message("other-mailbox", mailbox="account-two"), NOW)
    assert store.queue_mail(message("clipped-a", truncated=True), NOW)
    assert store.queue_mail(message("clipped-b", truncated=True), NOW)
    assert store.mail_counts() == {"pending": 4, "duplicate": 1}


def test_missing_auth_is_actionable_and_does_not_block_due_todo_reminders(tmp_path, monkeypatch):
    config = settings(tmp_path, gmail_token_file=tmp_path / "missing-token.json")
    store = Store(config.data_directory)
    model = DemoModel()
    workflow = Workflow(config, store, model, FixtureResearch(ROOT / "examples/research-materials.json"),
                        Notifier(config, store))
    original = IncidentSeed(source_ref="seed", service="giffgaff", account_ref="alpha",
                            notice="simulated future deactivation", service_status="scheduled_deactivation")
    run(workflow.run([original], NOW))
    later = NOW + timedelta(hours=3)
    monkeypatch.setattr("risk_monitor.cli.utc_now", lambda: later)
    result = run(poll_once(config, store, model, workflow, later))
    assert result["mail"]["status"] == "failed" and "auth" in result["mail"]["reason"]
    assert result["reminders"][0]["status"] == "staged"
    assert store.reminder(original.event_id)["sent_at"] is None
    with pytest.raises(GmailError, match="auth"):
        load_credentials(config)
    assert SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]


def test_failed_mail_does_not_starve_later_mail_under_per_round_limit(tmp_path):
    class SelectiveDetector(Detector):
        async def generate(self, schema, task, payload):
            if payload["email"]["message_id"] == "broken":
                raise RuntimeError("bad email response")
            return await super().generate(schema, task, payload)

    config = settings(tmp_path, gmail_max_messages=1)
    store = Store(config.data_directory)
    reader = QueueReader([message("broken"), message("later", subject="A separate later notice")])
    inbox = InboxWorkflow(config, store, SelectiveDetector(), reader)
    assert run(inbox.poll(NOW))["identification"]["errors"]
    result = run(inbox.poll(NOW + timedelta(minutes=1)))
    assert len(result["identification"]["event_ids"]) == 2
    assert result["identification"]["pending"] == 1


def test_expired_credentials_refresh_with_timeout_and_revocation_requests_login(tmp_path, monkeypatch):
    from google.auth.exceptions import RefreshError
    import risk_monitor.gmail as gmail

    config = settings(tmp_path, gmail_token_file=tmp_path / "gmail-token.json")
    config.gmail_token_file.write_text("{}", encoding="utf-8")

    class Expired:
        valid, refresh_token, revoked = False, "test-refresh", False

        def has_scopes(self, scopes):
            return scopes == SCOPES

        def refresh(self, request):
            assert request.keywords["timeout"] == config.tool_timeout_seconds
            if self.revoked:
                raise RefreshError("upstream sensitive response")
            self.valid = True

        def to_json(self):
            return json.dumps({"token": "test-new-token"})

    credentials = Expired()
    monkeypatch.setattr(gmail.Credentials, "from_authorized_user_file", lambda *args: credentials)
    assert load_credentials(config).valid
    assert json.loads(config.gmail_token_file.read_text())["token"] == "test-new-token"
    credentials.valid, credentials.revoked = False, True
    with pytest.raises(GmailError, match="auth") as caught:
        load_credentials(config)
    assert "sensitive" not in str(caught.value)


def test_cancelled_service_is_urgent_even_when_model_suggests_ordinary(tmp_path):
    config = settings(tmp_path)
    store = Store(config.data_directory)
    workflow = Workflow(config, store, DemoModel(), FixtureResearch(ROOT / "examples/research-materials.json"),
                        Notifier(config, store))
    original = IncidentSeed(source_ref="cancelled-mail", service="giffgaff", account_ref="alpha",
                            notice="Your service has been cancelled", service_status="cancelled")
    result = run(workflow.run([original], NOW))[0]
    assert result["solution"]["priority"] == "urgent"
    assert result["delivery"]["kind"] == "notification" and result["delivery"]["status"] == "staged"
